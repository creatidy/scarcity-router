"""M10 security acceptance suite (issue #95; D-044 threat model).

Security failures here are program blockers, not documentation notes.
Each test maps to a row of docs/m10-security-acceptance.md; where an
earlier module already proves a property, this suite re-proves the
critical ones through the REAL surfaces (composed server, real worker
endpoint, real TLS) and the matrix records both.

Deterministic and synthetic: no live provider, no paid inference, no
real credentials (every secret is a conspicuous SYNTHETIC fake).
"""

from __future__ import annotations

import json
import socket
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

from scarcity_router.gateway_server import (
    GATEWAY_ORIGIN_HEADER,
    load_client_key_directory,
)
from scarcity_router.resource_state import ResourceStateSnapshot
from scarcity_router.server_store import STORE_FILE_NAME
from tests.m10_fixtures import RealTimeServerHarness
from tests.openai_http_fixtures import ScriptedProviderServer
from tests.server_fixtures import FAKE_ADMIN_PASSWORD, FAKE_PROVIDER_SECRET


def _zai_resource() -> dict[str, object]:
    return {
        "registration": {
            "identity": {
                "resource_id": "zai-plan-1",
                "channel": "server_direct_http",
                "provider": "zai",
                "model": "glm-5.3",
                "entitlement": "subscription_included",
            },
            "freshness_ttl_seconds": 3600,
            "capabilities": {"context_limit_tokens": 272_000},
        },
        "enabled": True,
        "endpoint_id": "zai-http",
    }


def _observation() -> ResourceStateSnapshot:
    from datetime import datetime, timezone

    from scarcity_router.resource_state import (
        ResourceHealth,
        ResourceIdentity,
    )

    identity = ResourceIdentity(
        resource_id="zai-plan-1",
        channel="server_direct_http",
        provider="zai",
        model="glm-5.3",
        entitlement="subscription_included",
    )
    return ResourceStateSnapshot(
        schema_version=1,
        identity=identity,
        observed_at=datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        health=ResourceHealth(status="ok", diagnostics=()),
        quota_facts=(),
        promotions=(),
    )


class AuthBoundaryTests(RealTimeServerHarness):
    """Unauthenticated and cross-class access fails closed."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_unauthenticated_control_and_admin_are_rejected(self) -> None:
        for method, path in (
            ("GET", "/control/state"),
            ("GET", "/control/export"),
            ("GET", "/control/diagnostics"),
            ("GET", "/control/clients"),
            ("GET", "/control/workers"),
        ):
            with self.subTest(path=path):
                status, _payload, _headers = self.exchange(
                    method, path, with_session=False
                )
                self.assertEqual(401, status)
        # The web UI redirects unauthenticated browsers to login — it
        # never renders administration content.
        status, _payload, _headers = self.exchange("GET", "/admin", with_session=False)
        self.assertIn(status, (303, 307))

    def test_second_bootstrap_is_refused_forever(self) -> None:
        status, _payload, _headers = self.exchange(
            "POST",
            "/control/bootstrap/admin",
            {"password": FAKE_ADMIN_PASSWORD, "confirm": True},
        )
        self.assertEqual(409, status)

    def test_inference_client_key_cannot_administer(self) -> None:
        for path in ("/control/state", "/control/export", "/control/clients"):
            status, _payload, _headers = self.exchange(
                "GET",
                path,
                headers={"Authorization": f"Bearer {self.client_key}"},
                with_session=False,
            )
            self.assertEqual(401, status, path)

    def test_admin_session_cannot_execute_inference(self) -> None:
        # The identity classes are separate: the session cookie authorizes
        # nothing under the execution surface...
        status, _payload, _headers = self.exchange("GET", "/v1/models")
        self.assertEqual(401, status)
        # ...and there is no shared credential: the bearer key alone never
        # grants administration, the session alone never grants execution.
        status, _payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(401, status)

    def test_mutations_without_csrf_are_refused(self) -> None:
        status, _payload, _headers = self.exchange(
            "POST", "/control/clients", {"label": "no-csrf"}
        )
        self.assertEqual(403, status)
        status, _payload, _headers = self.exchange(
            "DELETE", "/control/providers/zai-http"
        )
        self.assertEqual(403, status)

    def test_wrong_admin_password_is_rejected(self) -> None:
        status, _payload, _headers = self.exchange(
            "POST", "/control/session", {"password": "totally-wrong-password"}
        )
        self.assertEqual(401, status)


class RevocationTests(RealTimeServerHarness):
    """Revocation exists for every identity class and is immediate."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_revoked_inference_key_is_rejected_immediately(self) -> None:
        issued = self.plane.service_issue_client_key({"label": "doomed"})
        doomed_key = cast(str, issued["api_key"])
        doomed_id = cast(str, issued["client_id"])
        status, _payload, _headers = self.exchange(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {doomed_key}"}
        )
        self.assertEqual(200, status)
        self.plane.service_revoke_client_key(doomed_id)
        status, _payload, _headers = self.exchange(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {doomed_key}"}
        )
        self.assertEqual(401, status)

    def test_revoked_worker_credential_is_rejected_on_next_connection(self) -> None:
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "device"}
        )
        self.assertEqual(200, status)
        code = cast("dict[str, object]", payload)["pairing_code"]
        # Pair via the endpoint and keep the returned credential...
        from scarcity_router.worker_protocol import SocketTransport
        from tests.worker_fixtures import ScriptedWorker

        self.plane.worker_endpoint  # noqa: B018 - endpoint readiness

        raw = socket.create_connection(
            ("127.0.0.1", self._plain_worker_port()), timeout=10
        )
        worker = ScriptedWorker(SocketTransport(raw))
        answer = worker.send_pair(str(code))
        worker_id = getattr(answer, "worker_id", None)
        credential = getattr(answer, "credential", None)
        assert isinstance(worker_id, str) and isinstance(credential, str)
        worker.transport.close()
        # ...then revoke the device through the administration surface.
        self.plane.service_revoke_worker(worker_id)
        # The revoked credential is refused on the next connection.
        raw2 = socket.create_connection(
            ("127.0.0.1", self._plain_worker_port()), timeout=10
        )
        worker2 = ScriptedWorker(SocketTransport(raw2))
        try:
            hello = worker2.send_hello(worker_id, credential)
            from scarcity_router.worker_protocol import ErrorMessage

            assert isinstance(hello, ErrorMessage)
            self.assertEqual("credential_revoked", hello.code)
        finally:
            worker2.transport.close()

    _worker_port: int = 0

    def _plain_worker_port(self) -> int:
        # attach_listener refuses a second listener, so bind lazily once.
        if not self._worker_port:
            listener = self.plane.worker_endpoint.attach_listener(
                host="127.0.0.1", port=0, tls_context=None
            )
            listener.serve_in_background()
            self.addCleanup(listener.shutdown)
            self._worker_port = listener.bound_port
        return self._worker_port


class NetworkDisciplineTests(RealTimeServerHarness):
    """Router-loop, redirect and request-boundary discipline."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_router_loop_marker_is_refused_at_ingress(self) -> None:
        status, payload, _headers = self.exchange(
            "GET",
            "/v1/models",
            headers={
                "Authorization": f"Bearer {self.client_key}",
                GATEWAY_ORIGIN_HEADER: "1",
            },
        )
        self.assertEqual(400, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual(
            "router_loop_detected", cast("dict[str, object]", error)["code"]
        )
        # The marker also protects the control and liveness paths.
        status, _payload, _headers = self.exchange(
            "GET", "/healthz", headers={GATEWAY_ORIGIN_HEADER: "1"}
        )
        self.assertEqual(400, status)

    def test_configuring_the_router_as_its_own_provider_is_refused(self) -> None:
        status, payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "self-loop",
                "adapter_id": "generic-openai",
                "base_url": "http://127.0.0.1:8787",
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(400, status)
        self.assertIn("router", str(payload).lower())

    def test_client_requests_carry_no_provider_url_or_credentials(self) -> None:
        # The strict parser refuses unknown fields: a client cannot inject
        # a provider URL, a credential, or any other dispatch parameter.
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": "hi"}],
                "base_url": "http://127.0.0.1:9",
                "api_key": FAKE_PROVIDER_SECRET,
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(400, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual(
            "unknown_parameter", cast("dict[str, object]", error)["code"]
        )

    def test_plain_http_non_loopback_provider_is_refused(self) -> None:
        status, payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "insecure",
                "adapter_id": "generic-openai",
                "base_url": "http://provider.example.com:8080",
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(400, status)
        self.assertIn("https", str(payload).lower())

    def test_credentials_in_urls_are_refused(self) -> None:
        status, _payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "url-creds",
                "adapter_id": "generic-openai",
                "base_url": "https://user:pw@api.example.com",
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(400, status)

    def test_cross_origin_redirect_is_refused_and_never_followed(self) -> None:
        """SSRF/redirect discipline end to end (D-044)."""
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        status, _payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "zai-http",
                "adapter_id": "zai-coding-plan",
                "base_url": provider.origin,
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(200, status)
        status, _payload = self.admin_post("/control/resources", _zai_resource())
        self.assertEqual(200, status)
        self.plane.apply_resource_observation(_observation())
        # The configured origin answers with a redirect to another host.
        provider.enqueue_redirect(302, "https://evil.example.com/harvest")
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
            timeout=60,
        )
        # The dispatch fails honestly at the backend boundary...
        self.assertEqual(502, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual("backend_failure", cast("dict[str, object]", error)["code"])
        # Exactly one outbound request: the redirect was REFUSED, never
        # followed, and the credential never left the configured origin.
        self.assertEqual(1, provider.request_count)


class ProtocolVocabularyTests(RealTimeServerHarness):
    """No generic shell/ssh/command surface exists anywhere."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_worker_protocol_rejects_arbitrary_command_messages(self) -> None:
        from tests.worker_fixtures import ScriptedWorker
        from scarcity_router.worker_protocol import SocketTransport

        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "device"}
        )
        self.assertEqual(200, status)
        code = cast("dict[str, object]", payload)["pairing_code"]
        listener = self.plane.worker_endpoint.attach_listener(
            host="127.0.0.1", port=0, tls_context=None
        )
        listener.serve_in_background()
        self.addCleanup(listener.shutdown)
        worker = ScriptedWorker(
            SocketTransport(
                socket.create_connection(("127.0.0.1", listener.bound_port), timeout=10)
            )
        )
        try:
            answer = worker.send_pair(str(code))
            self.assertIsNotNone(getattr(answer, "worker_id", None))
            # An unknown "command" message class is a typed rejection
            # (the closed vocabulary has no execution escape hatch).
            worker.send_raw({"type": "shell", "command": "rm -rf /"})
            message = worker.read_server_message()
            from scarcity_router.worker_protocol import ErrorMessage

            self.assertIsInstance(message, ErrorMessage)
            self.assertNotIn("rm -rf", str(getattr(message, "message", "")))
        finally:
            worker.transport.close()

    def test_no_shell_ssh_endpoint_exists_on_any_http_surface(self) -> None:
        for path in (
            "/shell",
            "/ssh",
            "/v1/shell",
            "/control/shell",
            "/control/exec",
            "/v1/exec",
        ):
            status, _payload, _headers = self.exchange("POST", path, {})
            # Any fail-closed answer (unauthenticated/forbidden/unknown/
            # method-not-allowed) is acceptable; 200 would be a breach.
            self.assertIn(status, (401, 403, 404, 405), path)


class AdmissionBoundTests(RealTimeServerHarness):
    """Oversized and malformed input is bounded and fails safely."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_oversized_request_body_is_refused(self) -> None:
        # Default limit is 1 MiB; a body over it is refused at the edge.
        big = "x" * (1_048_576 + 1024)
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": big}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
            raw_body=json.dumps(
                {
                    "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                    "messages": [{"role": "user", "content": big}],
                }
            ).encode("utf-8"),
        )
        self.assertEqual(413, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual(
            "request_too_large", cast("dict[str, object]", error)["code"]
        )

    def test_deeply_nested_json_is_refused(self) -> None:
        payload = ("[" * 200) + ("]" * 200)
        status, _payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            raw_body=payload.encode("utf-8"),
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(400, status)

    def test_malformed_json_is_refused_without_traceback(self) -> None:
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            raw_body=b"{not json",
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(400, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual("invalid_json", cast("dict[str, object]", error)["code"])


class StoreDisciplineTests(RealTimeServerHarness):
    """Malformed store state fails safe; permissions stay strict."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_future_configuration_schema_fails_closed(self) -> None:
        document = self.plane.configuration.to_document()
        document["schema_version"] = 99
        with self.assertRaises(Exception):
            from scarcity_router.server_config import ServerConfiguration

            _ = ServerConfiguration.from_document(document)

    def test_store_permissions_are_enforced(self) -> None:
        database = self.data_dir / STORE_FILE_NAME
        mode = stat.S_IMODE(database.stat().st_mode)
        self.assertEqual(0o600, mode & 0o777)
        directory_mode = stat.S_IMODE(self.data_dir.stat().st_mode)
        self.assertEqual(0o700, directory_mode & 0o777)

    def test_loose_client_keys_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            loose = Path(parent) / "keys.json"
            _ = loose.write_text('{"a": "' + "ab" * 32 + '"}', encoding="utf-8")
            _ = loose.chmod(0o644)
            with self.assertRaises(ValueError):
                _ = load_client_key_directory(loose)

    def test_worker_store_rejects_unknown_schema(self) -> None:
        from scarcity_router.worker_local_store import WorkerLocalStore

        with tempfile.TemporaryDirectory() as parent:
            database = Path(parent) / "worker-state.db"
            connection = sqlite3.connect(database)
            try:
                _ = connection.execute(
                    "CREATE TABLE worker_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                _ = connection.execute(
                    "INSERT INTO worker_state VALUES ('schema_version', '999', 'x')"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(ValueError):
                _ = WorkerLocalStore(database)


if __name__ == "__main__":
    _ = unittest.main()
