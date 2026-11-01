"""Installed composed-server acceptance with synthetic, offline MI evidence."""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast

from scarcity_router.control_api import ControlPlane
from scarcity_router.server_store import ServerStore
from tests import test_mi_consumer as fixture
from tests.test_mi_server_config import configuration

ROOT = Path(__file__).resolve().parents[1]
MI_COMMIT = "d65891668b7e79f7b666c021f02a1b4bf8c4ca9c"


def uv_diagnostic(result: subprocess.CompletedProcess[bytes]) -> str:
    """Show bounded diagnostics without URLs, filesystem paths or control codes."""
    output = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")[-4000:]
    output = re.sub(r"https?://\S+", "<url>", output)
    output = re.sub(r"(?<![\w])/(?:[^\s:/]+/)*[^\s:]*", "<path>", output)
    output = "".join(c if c.isprintable() or c == "\n" else "?" for c in output)
    return f"uv exit {result.returncode}:\n{output}"


class MIInstalledServerTests(unittest.TestCase):
    def test_uv_failure_diagnostics_redact_urls_and_paths(self) -> None:
        result = subprocess.CompletedProcess([], 1, b"", b"error: https://user:secret@example.test/repo?token=secret /private/location/file\nBecause mcp was not found in the cache")
        message = uv_diagnostic(result)
        self.assertIn("mcp was not found in the cache", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("/private/location", message)

    def test_installed_optional_dependency_and_authenticated_composed_server(self) -> None:
        uv = shutil.which("uv")
        assert uv is not None, "uv is required for installed-package acceptance"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"HOME": str(root), "PATH": os.environ.get("PATH", os.defpath),
                   "UV_CACHE_DIR": str(root / "cache"), "UV_DEFAULT_INDEX": "https://pypi.org/simple",
                   "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1"}
            offline = {**env, "UV_OFFLINE": "1"}
            result = subprocess.run([uv, "export", "--locked", "--only-group", "dev", "--no-emit-project", "--no-hashes"],
                                    cwd=ROOT, env=offline, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, uv_diagnostic(result))
            constraints = root / "constraints.txt"
            _ = constraints.write_bytes(result.stdout)
            wheel_dir = root / "wheel"
            result = subprocess.run([uv, "build", "--wheel", "--out-dir", str(wheel_dir)],
                                    cwd=ROOT, env=env, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, uv_diagnostic(result))
            wheel = next(wheel_dir.glob("*.whl"))
            preparation = root / "preparation"
            result = subprocess.run([uv, "venv", "--python", sys.executable, str(preparation)],
                                    env=env, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, uv_diagnostic(result))
            result = subprocess.run([uv, "pip", "install", "--python", str(preparation / "bin" / "python"),
                                     "--constraint", str(constraints), f"{wheel}[mi]"],
                                    env=env, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, uv_diagnostic(result))
            venv = root / "venv"
            result = subprocess.run([uv, "venv", "--python", sys.executable, str(venv)],
                                    env=offline, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, uv_diagnostic(result))
            python = venv / "bin" / "python"
            result = subprocess.run([uv, "pip", "install", "--python", str(python), "--constraint", str(constraints), f"{wheel}[mi]"],
                                    env=offline, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, uv_diagnostic(result))
            result = subprocess.run([str(python), "-c", "import importlib.metadata as m, json, model_intelligence; "
                                     + "print(json.dumps([m.distribution('model-intelligence').read_text('direct_url.json'), model_intelligence.__file__]))"],
                                    cwd=root, env=offline, capture_output=True, timeout=30, check=False)
            self.assertEqual(0, result.returncode, uv_diagnostic(result))
            provenance, installed_path = cast(list[str], json.loads(result.stdout))
            direct = cast(dict[str, object], json.loads(provenance))
            self.assertEqual("https://forgejo.creatidy.com/Creatidy/model-intelligence.git", direct["url"])
            self.assertEqual(MI_COMMIT, cast(dict[str, str], direct["vcs_info"])["commit_id"])
            self.assertTrue(Path(installed_path).resolve().is_relative_to(venv.resolve()))

            data_dir = root / "state"
            wrong_store_claim = subprocess.run(
                [str(python), "-I", "-m", "scarcity_router.control_server", "--issue-bootstrap-claim"],
                cwd=root, env=offline, capture_output=True, timeout=30, check=False,
            )
            self.assertEqual(0, wrong_store_claim.returncode, "default-store issuance failed")
            wrong_claim = wrong_store_claim.stdout.decode().strip()
            issued_claim = subprocess.run(
                [str(python), "-I", "-m", "scarcity_router.control_server", "--data-dir", str(data_dir), "--issue-bootstrap-claim"],
                cwd=root, env=offline, capture_output=True, timeout=30, check=False,
            )
            self.assertEqual(0, issued_claim.returncode, "installed claim issuance failed")
            claim = issued_claim.stdout.decode().strip()
            self.assertTrue(claim)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                bootstrap_port = cast(tuple[str, int], probe.getsockname())[1]
            bootstrap_server = subprocess.Popen(
                [str(python), "-I", "-m", "scarcity_router.control_server", "--port", str(bootstrap_port), "--data-dir", str(data_dir)],
                cwd=root, env=offline, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 20
                status = 0
                while time.monotonic() < deadline:
                    if bootstrap_server.poll() is not None:
                        self.fail("installed bootstrap server exited before serving")
                    connection = http.client.HTTPConnection("127.0.0.1", bootstrap_port, timeout=2)
                    try:
                        connection.request("POST", "/control/bootstrap/admin", body=json.dumps({
                            "password": "synthetic-administrator-password", "confirm": True, "claim": wrong_claim,
                        }), headers={"Content-Type": "application/json"})
                        rejected = connection.getresponse()
                        _ = rejected.read()
                        self.assertEqual(401, rejected.status)
                        connection.request("POST", "/control/bootstrap/admin", body=json.dumps({
                            "password": "synthetic-administrator-password", "confirm": True, "claim": claim,
                        }), headers={"Content-Type": "application/json"})
                        reply = connection.getresponse()
                        _ = reply.read()
                        status = reply.status
                        break
                    except OSError:
                        time.sleep(0.05)
                    finally:
                        connection.close()
                self.assertEqual(200, status)
            finally:
                bootstrap_server.send_signal(signal.SIGINT)
                try:
                    _ = bootstrap_server.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    bootstrap_server.kill()
                    _ = bootstrap_server.wait(timeout=10)
            store = ServerStore.open(data_dir)
            plane = ControlPlane(store=store, pbkdf2_iterations=1000)
            issued = plane.service_issue_client_key({"label": "synthetic-acceptance"})
            client_key = cast(str, issued["api_key"])
            plane.close_worker_store()
            store.close()

            publication = root / "publication.json"
            _ = publication.write_bytes(fixture.encoded(fixture.public())[0])
            trust = root / "trust.json"
            _ = trust.write_text(json.dumps(configuration(publication)))
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = cast(tuple[str, int], probe.getsockname())[1]
            process = subprocess.Popen(
                [str(python), "-m", "scarcity_router.control_server", "--host", "127.0.0.1", "--port", str(port),
                 "--data-dir", str(data_dir), "--mi-config", str(trust)],
                cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 20
                status = 0
                response: dict[str, object] = {}
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("installed composed server exited before serving")
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    try:
                        request: dict[str, object] = {"requirement": {"task_level": "L0", "capability_minima": {}, "hard_constraints": {}}}
                        connection.request("POST", "/v1/select", body=json.dumps(request),
                                           headers={"Content-Type": "application/json", "Authorization": f"Bearer {client_key}"})
                        reply = connection.getresponse()
                        status = reply.status
                        response = cast(dict[str, object], json.loads(reply.read()))
                        break
                    except OSError:
                        time.sleep(0.05)
                    finally:
                        connection.close()
                self.assertEqual(200, status)
                self.assertEqual("no_solution", cast(dict[str, object], response["route"])["status"])
                reference = cast(dict[str, str], response["mi_utilization"])
                self.assertTrue(reference["identifier"].startswith("urn:mi:utilization:sha256:"))
                self.assertNotIn("context", response)
            finally:
                process.send_signal(signal.SIGINT)
                try:
                    _ = process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _ = process.wait(timeout=10)
            reopened = ServerStore.open(data_dir)
            try:
                self.assertIsNotNone(reopened.get_mi_receipt(reference["identifier"].split(":")[-1]))
            finally:
                reopened.close()


if __name__ == "__main__":
    _ = unittest.main()
