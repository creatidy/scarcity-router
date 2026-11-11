"""Opt-in single-host Compose acceptance with a synthetic host worker.

Run explicitly with SR_DOCKER_ACCEPTANCE=1; this builds an image and owns a
uniquely named Compose project and volume. No provider credentials are used.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from urllib.parse import urlencode
from pathlib import Path
from typing import cast

from scarcity_router.resource_state import ResourceIdentity
from scarcity_router.worker_client import WorkerOrigin, WorkerRuntime
from scarcity_router.worker_local_adapters import LocalAdapterRegistry, LoopbackOllamaAdapter
from scarcity_router.worker_local_store import WorkerLocalStore
from tests.openai_http_fixtures import ScriptedProviderServer
from tests.test_e2e_execution import _bridged_resource_document  # pyright: ignore[reportPrivateUsage]

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("SR_DOCKER_ACCEPTANCE") == "1", "explicit Docker acceptance only")
class LocalDockerAcceptance(unittest.TestCase):
    def test_compose_admin_worker_and_synthetic_execution(self) -> None:
        project = "sr224-accept-" + uuid.uuid4().hex[:12]
        compose = ["docker", "compose", "-p", project, "-f", str(ROOT / "examples/docker-compose.yml")]
        home = tempfile.TemporaryDirectory(prefix="sr224-docker-home-")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home.name}

        def docker(*args: str) -> str:
            result = subprocess.run(
                [*compose, *args], cwd=ROOT, env=env, capture_output=True,
                text=True, timeout=300, check=False,
            )
            if result.returncode:
                self.fail(f"Docker Compose {args[0]} failed with exit {result.returncode}")
            return result.stdout

        def request(method: str, path: str, payload: object = None,
                    headers: dict[str, str] | None = None) -> tuple[int, bytes, list[tuple[str, str]]]:
            connection = http.client.HTTPConnection("127.0.0.1", 8787, timeout=30)
            body = payload if isinstance(payload, bytes) else (None if payload is None else json.dumps(payload).encode())
            try:
                connection.request(method, path, body=body, headers={
                    **({"Content-Type": "application/json"} if body is not None else {}),
                    **(headers or {}),
                })
                response = connection.getresponse()
                return response.status, response.read(), response.getheaders()
            finally:
                connection.close()

        try:
            _ = docker("up", "-d", "--build")
            deadline = time.monotonic() + 30
            while True:
                try:
                    status, _, _ = request("GET", "/healthz")
                    if status == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    self.fail("Compose listener did not become ready")
                time.sleep(0.1)

            status, _, _ = request("GET", "/admin")
            self.assertIn(status, (200, 303))
            claim = docker("exec", "-T", "scarcity-router", "python", "-m",
                           "scarcity_router.control_server", "--data-dir", "/data", "--issue-bootstrap-claim").strip()
            form = urlencode({"claim": claim, "password": "synthetic-password-for-local-acceptance", "confirm": "yes"}).encode()
            form_headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://attacker.example"}
            status, _, _ = request("POST", "/admin/onboarding", form, form_headers)
            self.assertEqual(403, status)
            form_headers["Origin"] = "http://127.0.0.1:8787"
            status, _, headers = request("POST", "/admin/onboarding", form, form_headers)
            self.assertEqual(303, status)
            cookie = next(value.split(";", 1)[0] for name, value in headers if name.lower() == "set-cookie")
            status, page, _ = request("GET", "/admin/clients", headers={"Cookie": cookie})
            self.assertEqual(200, status)
            match = re.search(rb'name="csrf" value="([^"]+)"', page)
            self.assertIsNotNone(match)
            assert match is not None
            admin = {"Cookie": cookie, "X-Scarcity-CSRF": match.group(1).decode()}
            status, _, _ = request("POST", "/control/clients", {"label": "test"}, {"Cookie": cookie})
            self.assertEqual(403, status)
            status, data, _ = request("POST", "/control/clients", {"label": "test"}, admin)
            self.assertEqual(200, status)
            client_key = cast(str, cast("dict[str, object]", json.loads(data))["api_key"])
            status, _, _ = request("GET", "/v1/models")
            self.assertEqual(401, status)
            status, _, _ = request("GET", "/v1/models", headers={"Cookie": cookie})
            self.assertEqual(401, status)
            status, _, _ = request("GET", "/control/state", headers={"Authorization": f"Bearer {client_key}"})
            self.assertEqual(401, status)
            status, _, _ = request("GET", "/admin", headers={"Host": "attacker.example"})
            self.assertEqual(400, status)
            status, _, _ = request("GET", "/v1/models", headers={"Authorization": f"Bearer {client_key}"})
            self.assertEqual(200, status)
            status, data, _ = request("POST", "/control/workers/pairing-codes", {"label": "test worker"}, admin)
            self.assertEqual(200, status)
            pairing_code = cast(str, cast("dict[str, object]", json.loads(data))["pairing_code"])

            provider = ScriptedProviderServer()
            provider.start()
            try:
                with tempfile.TemporaryDirectory(prefix="sr224-worker-") as directory:
                    store = WorkerLocalStore(Path(directory) / "worker-state.db")
                    identity = ResourceIdentity(resource_id="e2e-ollama", channel="worker_bridged",
                                                provider="zai", model="glm-5.3", entitlement="subscription_included")
                    adapters = LocalAdapterRegistry()
                    adapters.register(LoopbackOllamaAdapter(resource=identity, host="127.0.0.1", port=provider.port))
                    runtime = WorkerRuntime(origin=WorkerOrigin.parse("srw://127.0.0.1:8790"), store=store,
                                            local_adapters=adapters, state_report_interval_seconds=0)
                    try:
                        worker = runtime.pair(pairing_code)
                        status, _, _ = request("POST", "/control/resources", _bridged_resource_document(worker.worker_id), admin)
                        self.assertEqual(200, status)
                        provider.enqueue_json(200, {"version": "0.0.0-synthetic"})
                        worker_thread = threading.Thread(target=runtime.run, daemon=True)
                        worker_thread.start()
                        try:
                            deadline = time.monotonic() + 20
                            blocked = "unknown"
                            while True:
                                status, data, _ = request("GET", "/control/resources", headers=admin)
                                if status == 200:
                                    document = cast("dict[str, object]", json.loads(data))
                                    resources = cast("list[dict[str, object]]", document["resources"])
                                    for resource in resources:
                                        if resource["resource_id"] == "e2e-ollama":
                                            ladder = cast("dict[str, object]", resource["ladder"])
                                            if ladder["available"] is True:
                                                break
                                            blocked = str(ladder["first_blocked_stage"])
                                    else:
                                        ladder = {}
                                    if ladder.get("available") is True:
                                        break
                                if time.monotonic() >= deadline:
                                    self.fail(f"synthetic worker did not report ready; blocked stage: {blocked}")
                                time.sleep(0.1)
                            provider.enqueue_completion(content="synthetic local reply", usage={"prompt_tokens": 4, "completion_tokens": 2})
                            status, data, _ = request("POST", "/v1/chat/completions", {
                                "model": "sr-pin:e2e-ollama/zai/glm-5.3/unconfigured",
                                "messages": [{"role": "user", "content": "synthetic prompt"}],
                            }, {"Authorization": f"Bearer {client_key}"})
                            self.assertEqual(200, status)
                            self.assertIn(b"synthetic local reply", data)
                        finally:
                            runtime.request_stop()
                            worker_thread.join(timeout=15)
                    finally:
                        store.close()
            finally:
                provider.stop()
        finally:
            try:
                _ = docker("down", "--volumes", "--rmi", "local")
            finally:
                home.cleanup()
