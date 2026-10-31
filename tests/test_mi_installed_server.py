"""Installed composed-server acceptance with synthetic, offline MI evidence."""

from __future__ import annotations

import http.client
import json
import os
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


class MIInstalledServerTests(unittest.TestCase):
    def test_installed_optional_dependency_and_authenticated_composed_server(self) -> None:
        uv = shutil.which("uv")
        assert uv is not None, "uv is required for installed-package acceptance"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"HOME": str(root), "PATH": os.environ.get("PATH", os.defpath),
                   "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv")),
                   "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1"}
            wheel_dir = root / "wheel"
            result = subprocess.run([uv, "build", "--wheel", "--out-dir", str(wheel_dir)],
                                    cwd=ROOT, env=env, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, "offline wheel build failed")
            wheel = next(wheel_dir.glob("*.whl"))
            venv = root / "venv"
            result = subprocess.run([uv, "venv", "--python", sys.executable, str(venv)],
                                    env=env, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, "temporary environment creation failed")
            python = venv / "bin" / "python"
            result = subprocess.run([uv, "pip", "install", "--python", str(python), f"{wheel}[mi]"],
                                    env=env, capture_output=True, timeout=120, check=False)
            self.assertEqual(0, result.returncode, "offline installed MI extra could not be resolved")

            data_dir = root / "state"
            store = ServerStore.open(data_dir)
            plane = ControlPlane(store=store, pbkdf2_iterations=1000)
            plane.service_bootstrap_admin(password="synthetic-administrator-password", confirm=True)
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
