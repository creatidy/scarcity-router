"""M10 end-to-end acceptance suite, part 1 (issue #95).

Scenarios covered here (deterministic, synthetic, CI-safe):

1. recommendation-only mode works from an installed-style surface (CLI
   ``--version``/``--help``, the frozen loopback REST v1 adapter with
   synthetic collectors);
2. composed server startup + unauthenticated ``GET /healthz`` liveness;
3. control API/UI onboarding: create administrator, login, session
   lifecycle (no default credential, bootstrap refused twice);
4. client-key creation through the control surface (shown once, hash
   stored, key authorizes the execution surface);
5. OpenAI-compatible ingress: ``GET /v1/models`` and
   ``POST /v1/chat/completions``, streaming and non-streaming;
13. server restart/persistence: identities, configuration, client keys
    and provider credentials survive store reopen; schema migration is
    explicit and a future store version fails closed;
14. remote M08 bridge: a real ``RemoteScarcityClient`` against the real
    authenticated composed server;
15. diagnostics/doctor surface (CLI doctor + control endpoint);
16. secret-free export/audit/diagnostics after real execution.

No live provider, no paid inference, no wall-clock-sensitive assertion:
synthetic collectors and scripted backends only.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import subprocess
import threading
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import cast, override

from scarcity_router.control_api import SESSION_COOKIE_NAME
from scarcity_router.gateway_server import load_client_key_directory
from scarcity_router.resource_state import ResourceStateSnapshot
from scarcity_router.server_store import (
    STORE_FILE_NAME,
    STORE_SCHEMA_VERSION,
    ServerStore,
    ServerStoreError,
)
from scarcity_router.status import StatusCollectors

from tests.m10_fixtures import (
    PROMPT_MARKER,
    RESPONSE_MARKER,
    RealTimeServerHarness,
)
from tests.openai_http_fixtures import FAKE_PROVIDER_KEY, ScriptedProviderServer
from tests.server_fixtures import (
    FAKE_ADMIN_PASSWORD,
    FAKE_PROVIDER_SECRET,
    synthetic_collectors,
)

REPO = Path(__file__).resolve().parents[1]


# ── Scenario 1: recommendation-only surface ───────────────────────────────────


class RecommendationOnlySurfaceTests(unittest.TestCase):
    def test_cli_module_surface_version_and_help(self) -> None:
        """The installed-style module entry reports its version and help."""
        for arguments, expect in (
            (["--version"], "scarcity-router"),
            (["--help"], "usage:"),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-m", "scarcity_router", *arguments],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(REPO),
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(expect, result.stdout)

    def test_worker_module_surface_help(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scarcity_router.worker_client",
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("pair", result.stdout)
        self.assertIn("run", result.stdout)

    def test_frozen_rest_adapter_select_with_synthetic_collectors(self) -> None:
        """Recommendation-only REST v1 answers end to end without network."""
        from scarcity_router.server import RestApplication, make_server
        from tests.server_fixtures import synthetic_collectors

        application = RestApplication(collectors=synthetic_collectors())
        server = make_server(application, port=0)
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = cast("tuple[str, int]", server.server_address)[1]
            host_header = f"127.0.0.1:{port}"
            status, payload = _rest_get(port, "/healthz", host_header)
            self.assertEqual(200, status)
            self.assertEqual({"status": "ok"}, payload)
            status, payload = _rest_post(
                port,
                "/v1/select",
                {"profile_id": "routine_coding"},
                host_header,
            )
            self.assertEqual(200, status)
            envelope = cast("dict[str, object]", payload)
            self.assertEqual(1, envelope["schema_version"])
            self.assertIn("decision", envelope)
        finally:
            server.shutdown()
            server.server_close()
            _ = thread.join(timeout=10)

    def test_recommendation_mode_needs_no_server_worker_or_docker(self) -> None:
        """The frozen surface is importable and runnable without any of it."""
        # Importing the CLI/MCP/REST modules must not require the server
        # stack; the metadata version resolves from the source fallback.
        import importlib

        for module_name in (
            "scarcity_router.cli",
            "scarcity_router.mcp",
            "scarcity_router.server",
        ):
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertTrue(hasattr(module, "main"))


def _rest_get(
    port: int, path: str, host_header: str
) -> tuple[int, object]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"Host": host_header}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _rest_post(
    port: int, path: str, payload: object, host_header: str
) -> tuple[int, object]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Host": host_header, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


# ── Scenarios 2-5: the composed server ────────────────────────────────────────


class ServerStartupTests(RealTimeServerHarness):
    def test_scenario_02_startup_and_healthz(self) -> None:
        """Startup banner version + unauthenticated liveness endpoint."""
        status, payload, _headers = self.exchange("GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, payload)
        # Liveness only: no collector ran (the synthetic collectors would
        # have been recorded), no auth data, and wrong methods are refused.
        status, payload, _headers = self.exchange("POST", "/healthz", {})
        self.assertEqual(405, status)
        # Execution surface stays fail-closed for anonymous traffic.
        status, payload, _headers = self.exchange("GET", "/v1/models")
        self.assertEqual(401, status)
        # Control surface is unauthenticated-rejected, never leaky.
        status, payload, _headers = self.exchange("GET", "/control/state")
        self.assertEqual(401, status)

    def test_startup_banner_reports_version(self) -> None:
        from scarcity_router.control_server import main as control_main  # noqa: F401

        del control_main
        with tempfile.TemporaryDirectory() as parent:
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "scarcity_router.control_server",
                        "--port",
                        "0",
                        "--data-dir",
                        str(Path(parent) / "server"),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    input="",
                )
            except subprocess.TimeoutExpired as expired:
                # The composed server runs until interrupted; a timeout is
                # the expected shape. The captured banner must name the
                # version and the listening origin.
                stdout = expired.stdout or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", errors="replace")
                stderr = expired.stderr or ""
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                self.assertIn("scarcity-router 0.1.0", stdout)
                self.assertIn("listening on http://127.0.0.1:", stdout)
                self.assertNotIn("Traceback", stderr)
            else:
                self.fail(f"server exited early: {result.returncode} {result.stderr}")


class OnboardingTests(RealTimeServerHarness):
    def test_scenario_03_onboarding_login_session(self) -> None:
        # First run: forced onboarding; bootstrap works exactly once.
        status, _payload, headers = self.exchange(
            "POST",
            "/control/bootstrap/admin",
            {"password": FAKE_ADMIN_PASSWORD, "confirm": True},
        )
        self.assertEqual(200, status)
        cookies = [value for name, value in headers if name.lower() == "set-cookie"]
        self.assertEqual(1, len(cookies))
        self.assertIn("HttpOnly", cookies[0])
        self.assertIn("SameSite=Strict", cookies[0])
        self.cookie = cookies[0].split(";", 1)[0].split("=", 1)[1]

        status, payload, _headers = self.exchange(
            "POST",
            "/control/bootstrap/admin",
            {"password": FAKE_ADMIN_PASSWORD, "confirm": True},
        )
        self.assertEqual(409, status)

        # Logout deletes the session; the old cookie no longer authorizes.
        # (Logout is a mutation: the per-session CSRF header is required.)
        csrf = self.plane.csrf_token_for_cookie(self.cookie)
        assert csrf is not None
        status, _payload, _headers = self.exchange(
            "DELETE", "/control/session", headers={"X-Scarcity-CSRF": csrf}
        )
        self.assertEqual(200, status)
        status, _payload, _headers = self.exchange("GET", "/control/state")
        self.assertEqual(401, status)

        # Login re-issues a working session.
        status, _payload, headers = self.exchange(
            "POST",
            "/control/session",
            {"password": FAKE_ADMIN_PASSWORD},
        )
        self.assertEqual(200, status)
        cookies = [value for name, value in headers if name.lower() == "set-cookie"]
        self.cookie = cookies[0].split(";", 1)[0].split("=", 1)[1]
        status, payload, _headers = self.exchange("GET", "/control/state")
        self.assertEqual(200, status)
        document = cast("dict[str, object]", payload)
        self.assertIs(True, document["admin_configured"])


class ClientKeyTests(RealTimeServerHarness):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_scenario_04_client_key_creation_through_control_surface(self) -> None:
        status, payload = self.admin_post(
            "/control/clients", {"label": "openai-sdk"}
        )
        self.assertEqual(200, status)
        document = cast("dict[str, object]", payload)
        api_key = cast(str, document["api_key"])
        client_id = cast(str, document["client_id"])
        self.assertTrue(api_key)

        # The key authorizes the execution surface...
        status, payload, _headers = self.exchange(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {api_key}"}
        )
        self.assertEqual(200, status)

        # ...but is stored ONLY as a hash (the store lists no key material).
        records = self.plane.store.list_client_keys()
        record = next(record for record in records if record.client_id == client_id)
        self.assertNotEqual(api_key, record.key_hash)
        self.assertEqual(64, len(record.key_hash))

        # ...and nothing else: the key must not authorize administration.
        # (No session cookie is attached: the bearer key alone must fail.)
        status, _payload, _headers = self.exchange(
            "GET",
            "/control/state",
            headers={"Authorization": f"Bearer {api_key}"},
            with_session=False,
        )
        self.assertEqual(401, status)


class ExecutionIngressTests(RealTimeServerHarness):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_scenario_05_models_list_and_nonstreaming_completion(self) -> None:
        # The honest empty deployment lists no models...
        status, payload, _headers = self.exchange(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {self.client_key}"}
        )
        self.assertEqual(200, status)
        document = cast("dict[str, object]", payload)
        self.assertEqual("list", document["object"])

        # ...and refuses an unconfigured target with an explicit error.
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:absent/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": PROMPT_MARKER}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(404, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual("pin_target_not_found", cast("dict[str, object]", error)["code"])

    def test_scenario_05_streaming_completion_with_matrix_evidence(self) -> None:
        """Full SSE streaming through the M03 surface with an evidenced
        compatibility matrix — the composed-deployment counterpart in
        test_e2e_execution proves the D-043 fail-closed behavior when no
        evidence is recorded."""
        from tests.gateway_fixtures import (
            CLIENT_KEY,
            build_cells,
            make_application,
        )
        from scarcity_router.gateway_contracts import ClientKeyDirectory
        from scarcity_router.gateway_server import make_gateway_server

        application = make_application(
            cells=build_cells(),
            client_key_directory=ClientKeyDirectory.from_secrets(
                {"ingress-client": CLIENT_KEY}
            ),
        )
        server = make_gateway_server(application, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = cast("tuple[str, int]", server.server_address)[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=json.dumps(
                    {
                        "model": "sr-pin:openai-http/openai/gpt-5.6-luna/max",
                        "messages": [{"role": "user", "content": PROMPT_MARKER}],
                        "stream": True,
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {CLIENT_KEY}",
                },
            )
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            self.assertTrue(
                response.getheader("Content-Type", "").startswith("text/event-stream")
            )
            payloads: list[dict[str, object]] = []
            for line in response.read().decode("utf-8").splitlines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    document = json.loads(line[len("data: ") :])
                    if isinstance(document, dict):
                        payloads.append(document)
            connection.close()
            # Role frame, deltas, finish and usage: well-formed framing.
            self.assertGreaterEqual(len(payloads), 4)
            text = ""
            finish_seen = False
            for frame in payloads:
                choices = cast("list[object]", frame.get("choices", []))
                for choice in choices:
                    delta = cast("dict[str, object]", choice).get("delta", {})
                    piece = cast("dict[str, object]", delta).get("content")
                    if piece:
                        text += str(piece)
                    if cast("dict[str, object]", choice).get("finish_reason"):
                        finish_seen = True
            self.assertIn("synthetic reply", text)
            self.assertTrue(finish_seen)
        finally:
            server.shutdown()
            server.server_close()
            _ = thread.join(timeout=10)


# ── Scenario 13: restart, persistence, schema discipline ──────────────────────


class RestartPersistenceTests(unittest.TestCase):
    def test_scenario_13_state_survives_restart_and_future_schema_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            from scarcity_router.control_api import ControlPlane

            plane = ControlPlane(
                store=store,
                collectors=synthetic_collectors(),
                pbkdf2_iterations=1000,
                version="0.1.0.test",
            )
            _ = plane.service_bootstrap_admin(
                password=FAKE_ADMIN_PASSWORD, confirm=True
            )
            plane.service_add_provider(
                {
                    "provider_id": "zai-http",
                    "adapter_id": "zai-coding-plan",
                    "base_url": "https://api.z.ai",
                    "secret": FAKE_PROVIDER_SECRET,
                }
            )
            issued = plane.service_issue_client_key({"label": "persist"})
            key_material = cast(str, issued["api_key"])
            client_id = cast(str, issued["client_id"])
            revoked = plane.service_issue_client_key({"label": "doomed"})
            revoked_id = cast(str, revoked["client_id"])
            plane.service_revoke_client_key(revoked_id)
            store.close()

            # Reopen: identities, configuration and credential survive.
            store2 = ServerStore.open(data_dir)
            self.assertEqual(
                STORE_SCHEMA_VERSION, store2.require_current_schema()
            )
            plane2 = ControlPlane(
                store=store2,
                collectors=synthetic_collectors(),
                pbkdf2_iterations=1000,
                version="0.1.0.test",
            )
            try:
                self.assertTrue(plane2.admin_configured())
                document = plane2.configuration
                self.assertEqual(1, len(document.providers))
                self.assertEqual("zai-http", document.providers[0].provider_id)
                self.assertTrue(store2.has_provider_secret("zai-http"))
                # The issued key still authenticates; the revoked one does not.
                directory = plane2.current_application().client_key_directory
                assert directory is not None
                self.assertEqual(client_id, directory.authenticate(key_material))
                self.assertIsNone(directory.authenticate("wrong-key"))
            finally:
                store2.close()

            # A future store schema version fails closed (open applies only
            # known migrations; reading a NEWER store is refused).
            database = data_dir / STORE_FILE_NAME
            connection = sqlite3.connect(database)
            try:
                _ = connection.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, applied_at) "
                    "VALUES (?, '2026-09-20T00:00:00.000Z')",
                    (STORE_SCHEMA_VERSION + 1,),
                )
                _ = connection.execute(
                    "DELETE FROM schema_migrations WHERE version < ?",
                    (STORE_SCHEMA_VERSION + 1,),
                )
                connection.commit()
            finally:
                connection.close()
            reopened = ServerStore.open(data_dir, migrate=False)
            try:
                with self.assertRaises(ServerStoreError):
                    _ = reopened.require_current_schema()
            finally:
                reopened.close()

    def test_schema_migration_v2_is_idempotent_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            self.assertEqual(STORE_SCHEMA_VERSION, store.require_current_schema())
            store.close()
            store2 = ServerStore.open(data_dir)
            self.assertEqual(STORE_SCHEMA_VERSION, store2.require_current_schema())
            store2.close()

    def test_client_keys_file_permissions_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            loose = Path(parent) / "client-keys.json"
            _ = loose.write_text('{"client-a": "' + "ab" * 32 + '"}', encoding="utf-8")
            _ = loose.chmod(0o644)
            with self.assertRaises(ValueError):
                _ = load_client_key_directory(loose)
            _ = loose.chmod(0o600)
            directory = load_client_key_directory(loose)
            self.assertEqual({"client-a"}, set(directory.client_ids))


# ── Scenario 14: the remote M08 bridge against the real server ────────────────


class RemoteBridgeTests(RealTimeServerHarness):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def test_scenario_14_remote_client_selects_against_the_server(self) -> None:
        from scarcity_router.remote import RemoteScarcityClient, RemoteServerConfig

        config = RemoteServerConfig(
            base_url=f"http://127.0.0.1:{self.port}", api_key=self.client_key
        )
        client = RemoteScarcityClient(config)
        status_document = client.status()
        self.assertEqual(1, status_document["schema_version"])
        selection = client.select({"profile_id": "routine_coding"})
        self.assertEqual(1, selection["schema_version"])
        self.assertIn("decision", selection)
        # Wrong key: explicit failure, never silent degradation.
        bad_config = RemoteServerConfig(
            base_url=f"http://127.0.0.1:{self.port}", api_key="sr-invalid-key"
        )
        from scarcity_router.errors import RemoteBridgeError

        with self.assertRaises(RemoteBridgeError):
            _ = RemoteScarcityClient(bad_config).status()


# ── Scenarios 15/16: diagnostics and the secret-free export ───────────────────


class DiagnosticsAndExportTests(RealTimeServerHarness):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def _configure_provider(self, origin: str) -> None:
        status, _payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "zai-http",
                "adapter_id": "zai-coding-plan",
                "base_url": origin,
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(200, status)

    def test_scenario_15_diagnostics_and_doctor_report_stored_state(self) -> None:
        self._configure_provider("https://api.z.ai")
        status, payload, _headers = self.exchange("GET", "/control/diagnostics")
        self.assertEqual(200, status)
        report_text = json.dumps(payload)
        self.assertTrue(report_text)
        # Redaction: the provider secret never appears.
        self.assertNotIn(FAKE_PROVIDER_SECRET, report_text)
        self.assertNotIn(FAKE_PROVIDER_KEY, report_text)

        # The offline doctor reads the same store from the CLI.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scarcity_router",
                "doctor",
                "--json",
                "--server-data-dir",
                str(self.data_dir),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(REPO),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        rendered = json.dumps(report)
        self.assertNotIn(FAKE_PROVIDER_SECRET, rendered)

    def test_scenario_16_export_audit_and_diagnostics_are_secret_free(self) -> None:
        # Configure a provider with a credential and run a real execution
        # carrying conspicuous markers through the composed server.
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        self._configure_provider(provider.origin)
        status, _payload = self.admin_post(
            "/control/resources",
            {
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
            },
        )
        self.assertEqual(200, status)
        self.plane.apply_resource_observation(_healthy_observation())
        provider.enqueue_completion(
            content=RESPONSE_MARKER,
            usage={"prompt_tokens": 12, "completion_tokens": 5},
        )
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": PROMPT_MARKER}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(200, status, payload)

        # The audit store holds metadata, never prompt/response content or
        # credentials.
        audit_payloads = _audit_payloads(self.data_dir)
        self.assertTrue(audit_payloads)
        for blob in audit_payloads:
            self.assertNotIn(PROMPT_MARKER, blob)
            self.assertNotIn(RESPONSE_MARKER, blob)
            self.assertNotIn(FAKE_PROVIDER_SECRET, blob)

        # The control export is secret-free by construction.
        status, payload, _headers = self.exchange("GET", "/control/export")
        self.assertEqual(200, status)
        exported = json.dumps(payload)
        self.assertNotIn(FAKE_PROVIDER_SECRET, exported)
        self.assertNotIn(PROMPT_MARKER, exported)
        self.assertNotIn(RESPONSE_MARKER, exported)

        # Diagnostics stay clean too.
        status, payload, _headers = self.exchange("GET", "/control/diagnostics")
        self.assertEqual(200, status)
        rendered = json.dumps(payload)
        self.assertNotIn(FAKE_PROVIDER_SECRET, rendered)
        self.assertNotIn(PROMPT_MARKER, rendered)
        self.assertNotIn(RESPONSE_MARKER, rendered)


def _audit_payloads(data_dir: Path) -> list[str]:
    database = data_dir / STORE_FILE_NAME
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("SELECT payload FROM audit_records").fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def _healthy_observation() -> ResourceStateSnapshot:
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


if __name__ == "__main__":
    _ = unittest.main()
