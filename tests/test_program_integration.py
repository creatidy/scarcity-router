"""Integration tests for the M04 x M05 x M09 wave (program branch).

Covers exactly the cross-workstream seams the integration reconciles:

- ONE pairing system: the control API issues pairing codes, the M05
  worker protocol's TLS handshake (in-process and over a real loopback
  TCP listener) redeems them, control-API revocation and rotation act on
  the M05 store, and the superseded M09-side pairing tables are gone
  after the explicit schema migration — with unrelated data preserved.
- ONE Ollama translation: the worker's ``LoopbackOllamaAdapter`` default
  is the shared M04 translation core on the ``ollama`` preset, exercised
  against a synthetic loopback HTTP server (completion, streaming, tool
  calls, honest usage, refusal of unevidenced features).
- ONE process: execution surface + control API + web UI + optional
  worker-protocol listener + adapters composed from administrator
  configuration — and the honest-empty default deployment.
- Configuration authority: adapter construction only from the M09
  configuration document; credentials only from the store's dispatch-only
  reader; fail-closed validation; diagnostics and connection tests
  reflect real seam data without consuming quota.

Deterministic: synthetic servers, loopback-only sockets, injected clocks,
conspicuous SYNTHETIC secrets only.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import tempfile
import time
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override

from scarcity_router.control_api import ControlPlane, GenerationTester
from scarcity_router.control_server import build_parser as build_server_parser
from scarcity_router.codex_worker_evidence import CODEX_WORKER_CELL_VALUES
from scarcity_router.gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterStreamChunk,
    AdapterToolCall,
)
from scarcity_router.resource_state import (
    ResourceHealth,
    ResourceIdentity,
    ResourceRegistration,
    ResourceStateSnapshot,
)
from scarcity_router.routing_core import CompatibilityCell
from scarcity_router.server_composition import (
    CompositionError,
    build_adapter_registry,
    build_compatibility_cells,
    build_resource_bindings,
    validate_execution_configuration,
)
from scarcity_router.server_config import (
    ProviderEndpointConfig,
    ResourceConfig,
    ServerConfiguration,
)
from scarcity_router.selection_types import ModelIdentity
from scarcity_router.server_store import MIGRATIONS, ServerStore, canonical_store_path
from scarcity_router.worker_identity_store import default_worker_store_path
from scarcity_router.worker_protocol import SocketTransport

from scarcity_router.status import StatusCollectors
from scarcity_router.worker_local_adapters import LoopbackOllamaAdapter

from tests.openai_http_fixtures import ScriptedProviderServer
from tests.server_fixtures import (
    FAKE_PROVIDER_SECRET,
    T_NOW,
    ServerHarness,
)
from tests.worker_fixtures import ScriptedWorker

# ── Shared builders ───────────────────────────────────────────────────────────


def _zai_resource_document(
    *,
    resource_id: str = "zai-plan-1",
    channel: str = "server_direct_http",
    provider: str = "zai",
    model: str = "glm-5.3",
    entitlement: str = "subscription_included",
    variant: str | None = None,
    endpoint_id: str | None = "zai-http",
    worker_id: str | None = None,
    local_adapter_id: str | None = None,
    enabled: bool = True,
) -> dict[str, object]:
    """A configured resource document; binding keys set to None are omitted."""
    identity: dict[str, object] = {
        "resource_id": resource_id,
        "channel": channel,
        "provider": provider,
        "model": model,
        "entitlement": entitlement,
    }
    if variant is not None:
        identity["variant"] = variant
    registration: dict[str, object] = {
        "identity": identity,
        "freshness_ttl_seconds": 3600,
        "capabilities": {"context_limit_tokens": 272_000},
    }
    document: dict[str, object] = {"registration": registration, "enabled": enabled}
    if endpoint_id is not None:
        document["endpoint_id"] = endpoint_id
    if worker_id is not None:
        document["worker_id"] = worker_id
    if local_adapter_id is not None:
        document["local_adapter_id"] = local_adapter_id
    return document


def _csrf(plane: ControlPlane, cookie: str) -> dict[str, str]:
    token = plane.csrf_token_for_cookie(cookie)
    assert token is not None
    return {"X-Scarcity-CSRF": token}


def _apply_healthy_observation(
    plane: ControlPlane, resource_id: str = "zai-plan-1", *, realtime: bool = False
) -> None:
    identity = ResourceIdentity(
        resource_id=resource_id,
        channel="server_direct_http",
        provider="zai",
        model="glm-5.3",
        entitlement="subscription_included",
    )
    observed_at = (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        if realtime
        else T_NOW
    )
    plane.apply_resource_observation(
        ResourceStateSnapshot(
            schema_version=1,
            identity=identity,
            observed_at=observed_at,
            health=ResourceHealth(status="ok", diagnostics=()),
            quota_facts=(),
            promotions=(),
        )
    )


# ── Seam 1: the schema migration retires one pairing system ──────────────────


class MigrationFromSchemaOneTests(unittest.TestCase):
    """v1 stores migrate to v2: duplicate pairing table dropped, data kept."""

    def _make_v1_store(self, data_dir: Path) -> Path:
        """Hand-build a schema-version-1 store with unrelated live data."""
        import os

        _ = data_dir.mkdir(parents=True, exist_ok=True)
        path = canonical_store_path(data_dir)
        connection = sqlite3.connect(str(path))
        try:
            for statement in MIGRATIONS[0][1]:
                _ = connection.execute(statement)
            _ = connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
                (T_NOW,),
            )
            # Unrelated durable state a real v1 deployment would hold.
            _ = connection.execute(
                "INSERT INTO server_configuration (id, document, updated_at) "
                + "VALUES (1, ?, ?)",
                (json.dumps({"schema_version": 1}), T_NOW),
            )
            _ = connection.execute(
                "INSERT INTO administrator_identity (id, password_salt_hex, "
                + "password_hash_hex, pbkdf2_iterations, created_at, updated_at) "
                + "VALUES (1, 'ab', 'cd', 1, ?, ?)",
                (T_NOW, T_NOW),
            )
            _ = connection.execute(
                "INSERT INTO client_keys (client_id, key_hash, label, created_at) "
                + "VALUES ('client-a', ?, 'lab', ?)",
                ("a" * 64, T_NOW),
            )
            _ = connection.execute(
                "INSERT INTO provider_secrets (provider_id, secret, updated_at) "
                + "VALUES ('zai-http', 'SYNTHETIC-SECRET-VALUE', ?)",
                (T_NOW,),
            )
            _ = connection.execute(
                "INSERT INTO audit_records (recorded_at, payload) VALUES (?, ?)",
                (T_NOW, json.dumps({"kind": "synthetic"})),
            )
            # The superseded duplicate pairing rows (no convertible state).
            _ = connection.execute(
                "INSERT INTO worker_pairings (worker_id, label, status, "
                + "code_hash, token_hash, created_at, expires_at, "
                + "last_connected_at) VALUES ('worker-old', 'rig', 'paired', "
                + "NULL, ?, ?, NULL, ?)",
                ("b" * 64, T_NOW, T_NOW),
            )
            connection.commit()
        finally:
            connection.close()
        _ = os.chmod(path, 0o600)
        return path

    def test_migration_to_v2_drops_pairings_and_preserves_unrelated_data(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            _ = self._make_v1_store(data_dir)
            store = ServerStore.open(data_dir)
            self.addCleanup(store.close)
            # The store migrated forward, exactly to the current version.
            self.assertEqual(2, store.schema_version())
            _ = store.require_current_schema()
            # Unrelated data survived untouched.
            self.assertEqual(
                {"schema_version": 1}, store.load_configuration_document()
            )
            self.assertIsNotNone(store.admin_identity())
            self.assertEqual(
                ("client-a",),
                tuple(record.client_id for record in store.list_client_keys()),
            )
            self.assertEqual(
                "SYNTHETIC-SECRET-VALUE", store.get_provider_secret("zai-http")
            )
            self.assertEqual(1, store.audit_record_count())
            # The superseded table is gone at the SQL level.
            connection = sqlite3.connect(str(canonical_store_path(data_dir)))
            try:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    + "AND name='worker_pairings'"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual([], rows)


# ── Seam 1 + 3: pairing admin over M05 through the composed process ──────────


class PairingReconciliationTests(ServerHarness):
    """Issue over HTTP, redeem over the M05 protocol (real TCP listener)."""

    def test_pairing_over_real_listener_revocation_and_liveness(self) -> None:
        from scarcity_router.worker_protocol import (
            ErrorMessage,
            PairResultMessage,
        )

        self.onboard()
        # The composed process attaches its worker-protocol listener
        # (loopback, port 0) exactly as control_server does on request.
        listener = self.plane.worker_endpoint.attach_listener(
            host="127.0.0.1", port=0
        )
        self.addCleanup(listener.shutdown)
        listener.serve_in_background()

        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "bench rig"}
        )
        self.assertEqual(200, status)
        code = cast(str, cast("dict[str, object]", payload)["pairing_code"])

        raw = socket.create_connection(
            ("127.0.0.1", listener.bound_port), timeout=10
        )
        self.addCleanup(raw.close)
        worker = ScriptedWorker(SocketTransport(raw, recv_timeout_seconds=10))
        answer = worker.send_pair(code)
        assert isinstance(answer, PairResultMessage), answer
        worker_id = answer.worker_id
        credential = answer.credential
        # The device is connected now (real endpoint state).
        self.assertEqual(
            (worker_id,), self.plane.worker_endpoint.connected_worker_ids()
        )

        status, payload = self.admin_get("/control/workers")
        workers = cast("list[object]", cast("dict[str, object]", payload)["workers"])
        entry = cast("dict[str, object]", workers[0])
        self.assertEqual(worker_id, entry["worker_id"])
        self.assertEqual("active", entry["status"])
        self.assertIs(True, entry["connected"])
        self.assertIsNotNone(entry["last_connected_at"])

        # Revocation closes the live session; a NEW connection with the
        # same credential is rejected as revoked.
        status, _payload, _headers = self.exchange(
            "DELETE",
            f"/control/workers/{worker_id}",
            headers=_csrf(self.plane, self.cookie),
        )
        self.assertEqual(200, status)
        deadline = _deadline_seconds(5.0)
        while worker_id in self.plane.worker_endpoint.connected_worker_ids():
            self.assertLess(time.monotonic(), deadline, "session not closed after revoke")
            time.sleep(0.01)
        raw2 = socket.create_connection(
            ("127.0.0.1", listener.bound_port), timeout=10
        )
        self.addCleanup(raw2.close)
        rejected_worker = ScriptedWorker(SocketTransport(raw2, recv_timeout_seconds=10))
        rejected = rejected_worker.send_hello(worker_id, credential)
        assert isinstance(rejected, ErrorMessage), rejected
        self.assertEqual("credential_revoked", rejected.code)
        rejected_worker.transport.close()
        worker.transport.close()

    def test_pairing_code_is_bound_to_the_m05_store_only(self) -> None:
        self.onboard()
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "rig"}
        )
        self.assertEqual(200, status)
        code = cast(str, cast("dict[str, object]", payload)["pairing_code"])
        # The code exists ONLY in the M05 identity store (hash-only); the
        # server store has no pairing state at all.
        self.assertFalse(
            _table_exists(self.plane.store.path, "worker_pairings"),
            "the retired M09 pairing table must not exist in new stores",
        )
        identity_store_path = Path(default_worker_store_path(self.data_dir))
        self.assertTrue(identity_store_path.is_file())
        raw = identity_store_path.read_bytes()
        self.assertNotIn(code.encode("utf-8"), raw)


def _table_exists(path: Path, table: str) -> bool:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchall()
    finally:
        connection.close()
    return bool(rows)


def _deadline_seconds(seconds: float) -> float:
    return time.monotonic() + seconds


# ── Seam 3 + 4: composition and configuration authority ──────────────────────


class HonestEmptyDefaultTests(ServerHarness):
    def test_default_deployment_registers_no_adapters(self) -> None:
        self.onboard()
        application = self.plane.current_application()
        self.assertEqual((), application.adapters.registered_channels())
        self.assertEqual([], self.plane.state_document()["adapters_ready"])
        # Nothing is configured: a pinned dispatch is an honest
        # pin_target_not_found rejection, never a fake execution.
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(404, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual(
            "pin_target_not_found", cast("dict[str, object]", error)["code"]
        )

    def test_control_server_parser_defaults_to_no_worker_listener(self) -> None:
        parser = build_server_parser()
        arguments: dict[str, object] = vars(
            parser.parse_args(["--data-dir", "/tmp/x"])
        )
        self.assertIsNone(arguments["worker_listen_port"])
        arguments = cast(
            "dict[str, object]",
            vars(
                parser.parse_args(
                    ["--data-dir", "/tmp/x", "--worker-listen-port", "0"]
                )
            ),
        )
        self.assertEqual(0, arguments["worker_listen_port"])
        arguments = cast(
            "dict[str, object]",
            vars(
                parser.parse_args(
                    [
                        "--data-dir",
                        "/tmp/x",
                        "--worker-listen-host",
                        "0.0.0.0",
                        "--worker-listen-port",
                        "8790",
                    ]
                )
            ),
        )
        self.assertEqual("0.0.0.0", arguments["worker_listen_host"])


class CompositionFromConfigurationTests(ServerHarness):
    """Configuration → adapters; credentials only from the store."""

    def test_unknown_preset_is_rejected_with_remediation(self) -> None:
        self.onboard()
        status, payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "zai-http",
                "adapter_id": "openai_http",
                "base_url": "https://api.z.ai",
            },
        )
        self.assertEqual(400, status)
        message = str(cast("dict[str, object]", payload)["error"])
        self.assertIn("evidence-backed preset", message)

    def test_provider_resource_composes_the_http_adapter(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "zai-http",
                "adapter_id": "zai-coding-plan",
                "base_url": "https://api.z.ai",
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(200, status)
        status, _payload = self.admin_post(
            "/control/resources", _zai_resource_document()
        )
        self.assertEqual(200, status)
        application = self.plane.current_application()
        self.assertEqual(
            ("server_direct_http",), application.adapters.registered_channels()
        )
        # The binding's credential came from the store's dispatch-only
        # reader; nothing about it appears in any view.
        config = self.plane.configuration
        bindings = build_resource_bindings(
            config, provider_secret_reader=self.plane.store.get_provider_secret
        )
        binding = bindings["zai-plan-1"]
        self.assertEqual("zai-coding-plan", binding.preset.preset_id)
        self.assertIsNotNone(binding.credential)
        status, payload = self.admin_get("/control/providers")
        self.assertNotIn(FAKE_PROVIDER_SECRET, str(payload))

    def test_credentialless_requiring_preset_stays_unbound(self) -> None:
        """Honest unconfigured: no secret → no binding, no crash."""
        self.onboard()
        status, _payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "zai-http",
                "adapter_id": "zai-coding-plan",
                "base_url": "https://api.z.ai",
            },
        )
        self.assertEqual(200, status)
        status, _payload = self.admin_post(
            "/control/resources", _zai_resource_document()
        )
        self.assertEqual(200, status)
        # The channel adapter composes (other resources may bind), but the
        # credential-less resource has NO binding: dispatch refuses it
        # definitively and the connection test reports remediation.
        status, payload = self.admin_post(
            "/control/resources/zai-plan-1/connection-test", {}
        )
        self.assertEqual(200, status)
        document = cast("dict[str, object]", payload)
        self.assertEqual("failed", document["result"])
        checks = cast("list[object]", document["checks"])
        names = [
            cast("dict[str, object]", check)["check"] for check in checks
        ]
        self.assertIn("credential_present", names)

    def test_worker_resource_with_unknown_worker_is_rejected(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/resources",
            _zai_resource_document(
                resource_id="local-glm",
                channel="worker_bridged",
                worker_id="w-doesnotexist",
                endpoint_id=None,
                local_adapter_id="ollama",
            ),
        )
        self.assertEqual(400, status)

    def test_worker_bridged_adapter_composes_from_bindings(self) -> None:
        self.onboard()
        # Pair a worker over the protocol (in-process transport).
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "rig"}
        )
        self.assertEqual(200, status)
        code = cast("dict[str, object]", payload)["pairing_code"]
        worker_id = _pair_over_inprocess_endpoint(self.plane, cast(str, code))
        status, _payload = self.admin_post(
            "/control/resources",
            _zai_resource_document(
                resource_id="local-glm",
                channel="worker_bridged",
                provider="ollama",
                model="local",
                entitlement="local_ungated",
                worker_id=worker_id,
                endpoint_id=None,
                local_adapter_id="ollama",
            ),
        )
        self.assertEqual(200, status)
        application = self.plane.current_application()
        self.assertIn("worker_bridged", application.adapters.registered_channels())

    def test_worker_resource_without_local_adapter_id_registers_nothing(self) -> None:
        self.onboard()
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "rig"}
        )
        self.assertEqual(200, status)
        worker_id = _pair_over_inprocess_endpoint(
            self.plane, cast(str, cast("dict[str, object]", payload)["pairing_code"])
        )
        status, _payload = self.admin_post(
            "/control/resources",
            _zai_resource_document(
                resource_id="local-glm",
                channel="worker_bridged",
                provider="ollama",
                model="local",
                entitlement="local_ungated",
                worker_id=worker_id,
                endpoint_id=None,
            ),
        )
        self.assertEqual(200, status)
        self.assertEqual(
            (), self.plane.current_application().adapters.registered_channels()
        )

    def test_conflicting_matrix_evidence_is_rejected_at_save(self) -> None:
        """Conflicting evidence for one frozen D-043 key fails closed.

        Two server-direct resources representing the SAME physical model
        (openai/gpt-5.6-sol) through two DIFFERENT presets (evidenced vs
        generic) assert incompatible values for the same matrix key. The
        save is rejected with the typed composition error — naming the
        key, choosing no winner — and nothing is half-applied: the live
        configuration, the composed matrix and the durable store all keep
        the previously good state, so unrelated saves still succeed.
        """
        self.onboard()
        for provider_document in (
            {
                "provider_id": "openai-http",
                "adapter_id": "openai-api",
                "base_url": "https://api.openai.com",
                "secret": FAKE_PROVIDER_SECRET,
            },
            {
                "provider_id": "generic-http",
                "adapter_id": "generic-openai",
                "base_url": "https://relay.example.internal",
                "secret": FAKE_PROVIDER_SECRET,
            },
        ):
            status, payload = self.admin_post(
                "/control/providers", provider_document
            )
            self.assertEqual(200, status, payload)
        status, payload = self.admin_post(
            "/control/resources",
            _zai_resource_document(
                resource_id="sol-evidenced",
                provider="openai",
                model="gpt-5.6-sol",
                endpoint_id="openai-http",
            ),
        )
        self.assertEqual(200, status, payload)
        status, payload = self.admin_post(
            "/control/resources",
            _zai_resource_document(
                resource_id="sol-generic",
                provider="openai",
                model="gpt-5.6-sol",
                endpoint_id="generic-http",
            ),
        )
        self.assertEqual(400, status)
        message = str(cast("dict[str, object]", payload)["error"])
        self.assertIn("conflicting compatibility evidence", message)
        self.assertIn("server_direct_http", message)
        self.assertIn("gpt-5.6-sol", message)
        # Fail closed WITHOUT half-applying: the rejected resource is in
        # neither the live configuration nor the composed matrix, and the
        # legitimate resource's evidenced cells still serve unchanged.
        self.assertIsNone(self.plane.configuration.resource_by_id("sol-generic"))
        application_cells = self.plane.current_application().compatibility_cells
        application_keys = [
            (cell.channel, cell.provider, cell.model, cell.variant, cell.feature)
            for cell in application_cells
        ]
        self.assertEqual(len(application_keys), len(set(application_keys)))
        streaming = next(
            cell
            for cell in application_cells
            if (cell.channel, cell.provider, cell.model, cell.feature)
            == ("server_direct_http", "openai", "gpt-5.6-sol", "streaming")
        )
        self.assertEqual("PASS", streaming.value)
        # No poisoned state: an unrelated save still succeeds.
        status, payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "other-http",
                "adapter_id": "openai-api",
                "base_url": "https://api.other.example",
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(200, status, payload)


def _pair_over_inprocess_endpoint(plane: ControlPlane, code: str) -> str:
    import threading

    from scarcity_router.worker_protocol import PairResultMessage
    from tests.worker_fixtures import MemoryTransport

    server_side, worker_side = MemoryTransport.pair()
    endpoint = plane.worker_endpoint
    session = endpoint.attach_transport(server_side)
    endpoint.register_attached(session)
    thread = threading.Thread(target=session.run, daemon=True)
    thread.start()
    worker = ScriptedWorker(worker_side)
    answer = worker.send_pair(code)
    worker.transport.close()
    _ = thread.join(timeout=5)
    assert isinstance(answer, PairResultMessage), answer
    return answer.worker_id


class RealGatewayDispatchTests(ServerHarness):
    """Configured provider resource dispatches through the REAL gateway path.

    This class runs on a real clock: the M04 adapter computes socket
    timeouts against the real ``datetime.now``, so the whole composed
    stack (coordinator deadline, freshness evaluation, observations) must
    share that clock for the dispatch to be admission-honest.
    """

    @override
    def make_plane(
        self,
        data_dir: Path,
        *,
        collectors: StatusCollectors | None = None,
        generation_tester: GenerationTester | None = None,
    ) -> ControlPlane:
        from tests.server_fixtures import (
            PBKDF2_TEST_ITERATIONS,
            synthetic_collectors,
        )
        from scarcity_router.server_store import ServerStore
        from scarcity_router.server_ui import dispatch_ui

        store = ServerStore.open(data_dir)
        return ControlPlane(
            store=store,
            clock=None,
            collectors=(
                synthetic_collectors() if collectors is None else collectors
            ),
            pbkdf2_iterations=PBKDF2_TEST_ITERATIONS,
            version="0.1.0.test",
            own_origins=("http://127.0.0.1:8787",),
            generation_tester=generation_tester,
            ui_dispatcher=dispatch_ui,
        )

    def test_pinned_dispatch_reaches_the_configured_provider(self) -> None:
        self.onboard()
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        status, _payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "zai-http",
                "adapter_id": "zai-coding-plan",
                # Loopback plain HTTP: the bounded D-044 exception.
                "base_url": provider.origin,
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(200, status)
        status, _payload = self.admin_post(
            "/control/resources", _zai_resource_document()
        )
        self.assertEqual(200, status)
        _apply_healthy_observation(self.plane, realtime=True)
        provider.enqueue_completion(
            content="composed reply",
            usage={"prompt_tokens": 12, "completion_tokens": 5},
        )
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": "hello gateway"}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(200, status, payload)
        document = cast("dict[str, object]", payload)
        choices = cast("list[object]", document["choices"])
        message = cast("dict[str, object]", choices[0])["message"]
        self.assertEqual(
            "composed reply", cast("dict[str, object]", message)["content"]
        )
        # The request followed configuration exactly: preset endpoint path,
        # configured origin, store-held credential, router-loop marker.
        request = provider.last_request
        self.assertEqual("/chat/completions", request.path)
        self.assertEqual(f"Bearer {FAKE_PROVIDER_SECRET}", request.headers["authorization"])
        self.assertNotIn(FAKE_PROVIDER_SECRET, json.dumps(payload))

    def test_unwired_target_is_rejected_with_remediation(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "zai-http",
                "adapter_id": "zai-coding-plan",
                "base_url": "https://api.z.ai",
                "secret": FAKE_PROVIDER_SECRET,
            },
        )
        self.assertEqual(200, status)
        status, _payload = self.admin_post(
            "/control/resources", _zai_resource_document()
        )
        self.assertEqual(200, status)
        _apply_healthy_observation(self.plane, realtime=True)
        # A pin to an unknown resource is an explicit rejection, and the
        # scripted provider never received anything (nothing was queued).
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:other-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(404, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual(
            "pin_target_not_found", cast("dict[str, object]", error)["code"]
        )


# ── Seam 2: the worker's Ollama translation is the shared M04 core ───────────


class LoopbackOllamaEndToEndTests(unittest.TestCase):
    """The M04-core-backed default against a synthetic loopback server."""

    provider: ScriptedProviderServer
    adapter: LoopbackOllamaAdapter

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.provider = cast(ScriptedProviderServer, object())
        self.adapter = cast(LoopbackOllamaAdapter, object())

    @override
    def setUp(self) -> None:
        self.provider = ScriptedProviderServer()
        self.provider.start()
        self.addCleanup(self.provider.stop)
        identity = ResourceIdentity(
            resource_id="ollama-local",
            channel="worker_bridged",
            provider="ollama",
            model="local",
            entitlement="local_ungated",
        )
        self.adapter = LoopbackOllamaAdapter(
            resource=identity, port=self.provider.port
        )

    @staticmethod
    def _call(
        *,
        stream: bool = False,
        response_format: dict[str, object] | None = None,
        generation_params: dict[str, object] | None = None,
    ) -> AdapterCall:
        return AdapterCall(
            resource=ResourceIdentity(
                resource_id="ollama-local",
                channel="worker_bridged",
                provider="ollama",
                model="local",
                entitlement="local_ungated",
            ),
            model=ModelIdentity(provider="openai", model="local", variant="max"),
            messages=(AdapterMessage(role="user", content="hi"),),
            stream=stream,
            response_format=response_format,
            generation_params=generation_params or {},
        )

    def test_completion_with_honest_usage(self) -> None:
        self.provider.enqueue_completion(
            content="hello local",
            usage={"prompt_tokens": 3, "completion_tokens": 4},
        )
        result = self.adapter.invoke(
            self._call(),
            cancel_event=threading.Event(),
            deadline="2030-01-01T00:00:00.000Z",
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        assert result.message is not None
        self.assertEqual("hello local", result.message.content)
        usage = result.calls[0].provider_reported_usage
        assert usage is not None
        self.assertEqual((3, 4), (usage.prompt_tokens, usage.completion_tokens))
        self.assertEqual("/v1/chat/completions", self.provider.last_request.path)

    def test_streaming_emits_deltas_and_finish_in_order(self) -> None:
        from tests.openai_http_fixtures import completion_frame

        self.provider.enqueue_stream(
            [
                completion_frame(text="hel"),
                completion_frame(text="lo"),
                completion_frame(finish_reason="stop"),
                completion_frame(usage={"prompt_tokens": 2, "completion_tokens": 3}),
            ]
        )
        emitted: list[str] = []

        def emit(chunk: AdapterStreamChunk) -> None:
            if chunk.kind == "text_delta" and chunk.text is not None:
                emitted.append(chunk.text)
            else:
                emitted.append(f"<{chunk.kind}>")

        result = self.adapter.invoke(
            self._call(stream=True),
            cancel_event=threading.Event(),
            deadline="2030-01-01T00:00:00.000Z",
            emit=emit,
        )
        self.assertEqual("completed", result.status)
        self.assertEqual(["hel", "lo", "<finish>"], emitted)
        assert result.message is not None
        self.assertEqual("hello", result.message.content)
        # Usage stays honest: provider-reported through the shared core.
        usage = result.calls[0].provider_reported_usage
        assert usage is not None
        self.assertEqual((2, 3), (usage.prompt_tokens, usage.completion_tokens))

    def test_streamed_tool_fragments_yield_one_complete_call(self) -> None:
        from tests.openai_http_fixtures import completion_frame

        fragments: list[dict[str, object]] = [
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "lookup", "arguments": "{\"q\":"}},
            {"index": 0, "type": "function",
             "function": {"arguments": " \"ollama\"}"}},
        ]
        self.provider.enqueue_stream(
            [
                {
                    "id": "chatcmpl-synthetic",
                    "object": "chat.completion.chunk",
                    "created": 1_700_000_000,
                    "model": "synthetic-model",
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [fragments[0]]},
                         "finish_reason": None}
                    ],
                },
                {
                    "id": "chatcmpl-synthetic",
                    "object": "chat.completion.chunk",
                    "created": 1_700_000_000,
                    "model": "synthetic-model",
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [fragments[1]]},
                         "finish_reason": None}
                    ],
                },
                completion_frame(finish_reason="tool_calls"),
            ]
        )
        streamed_calls: list[AdapterToolCall | None] = []

        def emit(chunk: AdapterStreamChunk) -> None:
            if chunk.kind == "tool_call":
                streamed_calls.append(chunk.tool_call)

        result = self.adapter.invoke(
            self._call(stream=True),
            cancel_event=threading.Event(),
            deadline="2030-01-01T00:00:00.000Z",
            emit=emit,
        )
        self.assertEqual("completed", result.status)
        assert result.message is not None
        self.assertEqual(1, len(result.message.tool_calls))
        call = result.message.tool_calls[0]
        self.assertEqual("lookup", call.name)
        self.assertEqual('{"q": "ollama"}', call.arguments)
        # The streamed chunk carried the COMPLETE call too.
        self.assertEqual(1, len(streamed_calls))

    def test_json_schema_response_format_is_refused(self) -> None:
        result = self.adapter.invoke(
            self._call(response_format={"type": "json_schema"}),
            cancel_event=threading.Event(),
            deadline="2030-01-01T00:00:00.000Z",
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None
        self.assertIn("response_format", result.calls[0].note)
        self.assertEqual(0, self.provider.request_count)

    def test_unevidenced_generation_param_is_refused(self) -> None:
        result = self.adapter.invoke(
            self._call(generation_params={"parallel_tool_calls": True}),
            cancel_event=threading.Event(),
            deadline="2030-01-01T00:00:00.000Z",
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual(0, self.provider.request_count)

    def test_health_probe_reads_only_the_native_endpoint(self) -> None:
        self.provider.enqueue_json(
            document={"version": "0.13.3-synthetic"}
        )
        snapshots = self.adapter.resource_snapshots(T_NOW)
        self.assertEqual("ok", snapshots[0].health.status)
        self.assertEqual((), snapshots[0].quota_facts)
        self.assertEqual("/api/version", self.provider.last_request.path)


# ── Seam 4: diagnostics and connection tests use real seam data ──────────────


class DiagnosticsRealStateTests(ServerHarness):
    def test_connection_test_probes_provider_health_quota_free(self) -> None:
        self.onboard()
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        status, _payload = self.admin_post(
            "/control/providers",
            {
                "provider_id": "ollama-http",
                "adapter_id": "ollama",
                "base_url": provider.origin,
            },
        )
        self.assertEqual(200, status)
        status, _payload = self.admin_post(
            "/control/resources",
            _zai_resource_document(
                resource_id="net-ollama",
                provider="ollama",
                model="local",
                entitlement="local_ungated",
                endpoint_id="ollama-http",
            ),
        )
        self.assertEqual(200, status)
        provider.enqueue_json(document={"version": "0.13.3-synthetic"})
        status, payload = self.admin_post(
            "/control/resources/net-ollama/connection-test", {}
        )
        self.assertEqual(200, status)
        document = cast("dict[str, object]", payload)
        self.assertEqual("passed", document["result"])
        self.assertIs(False, document["quota_consumed"])
        checks: dict[object, dict[str, object]] = {
            cast("dict[str, object]", check)["check"]: check
            for check in cast("list[object]", document["checks"])
            if isinstance(check, dict)
        }
        self.assertIs(True, checks["provider_health"]["passed"])
        self.assertEqual("/api/version", provider.last_request.path)
        # No inference request was made.
        self.assertEqual(1, provider.request_count)

    def test_diagnostics_show_worker_connection_state(self) -> None:
        self.onboard()
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "rig"}
        )
        self.assertEqual(200, status)
        worker_id = _pair_over_inprocess_endpoint(
            self.plane, cast(str, cast("dict[str, object]", payload)["pairing_code"])
        )
        report = self.plane.build_diagnostics()
        self.assertEqual(1, len(report.workers))
        entry = report.workers[0]
        self.assertEqual(worker_id, entry["worker_id"])
        self.assertEqual("active", entry["status"])
        # Real endpoint state: the worker has connected at least once
        # (liveness stamp) even though its transport is closed in-process.
        self.assertIsNotNone(entry["last_connected_at"])


# ── Composition helpers against synthetic configuration (unit level) ─────────


class CompositionUnitTests(unittest.TestCase):
    def test_validate_rejects_unknown_preset(self) -> None:
        configuration = ServerConfiguration(
            providers=(
                ProviderEndpointConfig(
                    provider_id="p1",
                    adapter_id="openai_http",
                    base_url="https://api.z.ai",
                ),
            ),
        )
        with self.assertRaises(CompositionError) as caught:
            validate_execution_configuration(
                configuration, worker_id_exists=lambda _worker_id: True
            )
        self.assertIn("evidence-backed preset", str(caught.exception))
        # A loopback plain-HTTP Ollama origin composes cleanly.
        ok = ServerConfiguration(
            providers=(
                ProviderEndpointConfig(
                    provider_id="p1",
                    adapter_id="ollama",
                    base_url="http://127.0.0.1:11434",
                ),
            ),
        )
        validate_execution_configuration(
            ok, worker_id_exists=lambda _worker_id: False
        )

    def test_worker_reference_must_exist(self) -> None:
        configuration = ServerConfiguration(
            resources=(
                ResourceConfig(
                    registration=_registration("local-1"),
                    worker_id="w-absent",
                    local_adapter_id="ollama",
                ),
            ),
        )
        with self.assertRaises(CompositionError) as caught:
            validate_execution_configuration(
                configuration, worker_id_exists=lambda _worker_id: False
            )
        self.assertIn("pairing store", str(caught.exception))

    def test_empty_configuration_composes_an_empty_registry(self) -> None:
        from scarcity_router.gateway_adapters import AdapterRegistry
        from scarcity_router.resource_state import ResourceRegistry
        from scarcity_router.worker_endpoint import WorkerEndpoint
        from scarcity_router.worker_identity_store import WorkerIdentityStore

        with tempfile.TemporaryDirectory() as parent:
            worker_store = WorkerIdentityStore(Path(parent) / "w" / "w.sqlite3")
            self.addCleanup(worker_store.close)
            endpoint = WorkerEndpoint(
                identity_store=worker_store,
                registry=ResourceRegistry(),
                configured_owner=lambda _resource_id: None,
            )
            registry = build_adapter_registry(
                ServerConfiguration.neutral(),
                provider_secret_reader=lambda _provider_id: None,
                worker_endpoint=endpoint,
            )
            self.assertIsInstance(registry, AdapterRegistry)
            self.assertEqual((), registry.registered_channels())


def _registration(resource_id: str):  # type: ignore[no-untyped-def]
    from scarcity_router.resource_state import ResourceRegistration

    return ResourceRegistration(
        identity=ResourceIdentity(
            resource_id=resource_id,
            channel="worker_bridged",
            provider="ollama",
            model="local",
            entitlement="local_ungated",
        ),
        freshness_ttl_seconds=3600,
    )


# ── Matrix canonicalization at composition (D-042/D-043) ─────────────────────


def _identity_registration(
    resource_id: str,
    *,
    channel: str,
    provider: str,
    model: str,
    entitlement: str,
) -> ResourceRegistration:
    return ResourceRegistration(
        identity=ResourceIdentity(
            resource_id=resource_id,
            channel=channel,
            provider=provider,
            model=model,
            entitlement=entitlement,
        ),
        freshness_ttl_seconds=3600,
    )


def _codex_resource(
    resource_id: str,
    worker_id: str,
    *,
    model: str = "gpt-5.6-sol",
) -> ResourceConfig:
    """One configured Codex resource for the given physical model."""
    return ResourceConfig(
        registration=_identity_registration(
            resource_id,
            channel="worker_bridged",
            provider="openai",
            model=model,
            entitlement="subscription_included",
        ),
        worker_id=worker_id,
        local_adapter_id="codex",
    )


def _server_direct_resource(
    resource_id: str,
    endpoint_id: str,
    *,
    provider: str = "zai",
    model: str = "glm-5.3",
) -> ResourceConfig:
    return ResourceConfig(
        registration=_identity_registration(
            resource_id,
            channel="server_direct_http",
            provider=provider,
            model=model,
            entitlement="subscription_included",
        ),
        endpoint_id=endpoint_id,
    )


def _frozen_keys(
    cells: tuple[CompatibilityCell, ...],
) -> list[tuple[str, str, str, str | None, str]]:
    return [
        (cell.channel, cell.provider, cell.model, cell.variant, cell.feature)
        for cell in cells
    ]


class CompatibilityMatrixCanonicalizationTests(unittest.TestCase):
    """The composed matrix is canonicalized onto the frozen D-043 key.

    The key is ``(channel, provider, model, variant, feature)`` — never
    ``resource_id`` — because multiple resources may execute the same
    physical model, differing in worker/entitlement/pool/availability
    (D-042). Per-resource emission is therefore folded onto one cell per
    key: identical evidence merges silently, conflicting evidence fails
    closed, and distinct backends stay distinct.
    """

    def _cells(
        self,
        configuration: ServerConfiguration,
    ) -> tuple[CompatibilityCell, ...]:
        return build_compatibility_cells(
            configuration, provider_secret_reader=lambda _provider_id: "SYNTHETIC"
        )

    def test_two_codex_resources_same_physical_model_emit_one_cell_set(self) -> None:
        single = self._cells(
            ServerConfiguration(resources=(_codex_resource("codex-a", "w1"),))
        )
        both = self._cells(
            ServerConfiguration(
                resources=(
                    _codex_resource("codex-a", "w1"),
                    _codex_resource("codex-b", "w2"),
                )
            )
        )
        # One set of reviewed M06 cells for the shared physical model:
        # the second resource adds nothing (silent merge of identical
        # evidence), and no key is duplicated.
        self.assertEqual(single, both)
        keys = _frozen_keys(both)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(CODEX_WORKER_CELL_VALUES), len(both))
        self.assertEqual(
            {feature: value for feature, (value, _note) in CODEX_WORKER_CELL_VALUES.items()},
            {cell.feature: cell.value for cell in both},
        )

    def test_two_server_direct_resources_same_preset_deduplicate(self) -> None:
        single = self._cells(
            ServerConfiguration(
                providers=(
                    ProviderEndpointConfig(
                        provider_id="zai-http",
                        adapter_id="zai-coding-plan",
                        base_url="https://api.z.ai",
                    ),
                ),
                resources=(_server_direct_resource("zai-plan-1", "zai-http"),),
            )
        )
        both = self._cells(
            ServerConfiguration(
                providers=(
                    ProviderEndpointConfig(
                        provider_id="zai-http",
                        adapter_id="zai-coding-plan",
                        base_url="https://api.z.ai",
                    ),
                ),
                resources=(
                    _server_direct_resource("zai-plan-1", "zai-http"),
                    _server_direct_resource("zai-plan-2", "zai-http"),
                ),
            )
        )
        self.assertEqual(single, both)
        keys = _frozen_keys(both)
        self.assertEqual(len(keys), len(set(keys)))
        # Deterministic canonical order: sorted by the key tuple with an
        # absent variant first — the routing core's own ordering.
        self.assertEqual(
            keys,
            sorted(keys, key=lambda parts: (parts[0], parts[1], parts[2], parts[3] or "", parts[4])),
        )

    def test_conflicting_evidence_for_one_key_fails_closed(self) -> None:
        """Evidenced vs generic preset for the same backend: no winner."""
        configuration = ServerConfiguration(
            providers=(
                ProviderEndpointConfig(
                    provider_id="openai-http",
                    adapter_id="openai-api",
                    base_url="https://api.openai.com",
                ),
                ProviderEndpointConfig(
                    provider_id="generic-http",
                    adapter_id="generic-openai",
                    base_url="https://relay.example.internal",
                ),
            ),
            resources=(
                _server_direct_resource(
                    "sol-evidenced",
                    "openai-http",
                    provider="openai",
                    model="gpt-5.6-sol",
                ),
                _server_direct_resource(
                    "sol-generic",
                    "generic-http",
                    provider="openai",
                    model="gpt-5.6-sol",
                ),
            ),
        )
        with self.assertRaises(CompositionError) as caught:
            _ = self._cells(configuration)
        message = str(caught.exception)
        self.assertIn("conflicting compatibility evidence", message)
        # The error names the frozen key that cannot be represented.
        self.assertIn("server_direct_http", message)
        self.assertIn("openai", message)
        self.assertIn("gpt-5.6-sol", message)

    def test_distinct_backends_compose_cleanly_together(self) -> None:
        configuration = ServerConfiguration(
            providers=(
                ProviderEndpointConfig(
                    provider_id="zai-http",
                    adapter_id="zai-coding-plan",
                    base_url="https://api.z.ai",
                ),
                ProviderEndpointConfig(
                    provider_id="openai-http",
                    adapter_id="openai-api",
                    base_url="https://api.openai.com",
                ),
            ),
            resources=(
                _server_direct_resource("sol-1", "openai-http", provider="openai", model="gpt-5.6-sol"),
                _server_direct_resource("z1", "zai-http"),
            ),
        )
        # Distinct providers and models compose cleanly together.
        keys = _frozen_keys(self._cells(configuration))
        self.assertEqual(len(keys), len(set(keys)))

    def test_different_physical_models_stay_separate(self) -> None:
        both = self._cells(
            ServerConfiguration(
                resources=(
                    _codex_resource("codex-sol", "w1", model="gpt-5.6-sol"),
                    _codex_resource("codex-luna", "w2", model="gpt-5.6-luna"),
                )
            )
        )
        # No over-merging: each physical model keeps its full cell set.
        self.assertEqual(2 * len(CODEX_WORKER_CELL_VALUES), len(both))
        keys = _frozen_keys(both)
        self.assertEqual(len(keys), len(set(keys)))
        models = {model for (_ch, _p, model, _v, _f) in keys}
        self.assertEqual({"gpt-5.6-sol", "gpt-5.6-luna"}, models)

    def test_different_channels_stay_separate(self) -> None:
        configuration = ServerConfiguration(
            providers=(
                ProviderEndpointConfig(
                    provider_id="openai-http",
                    adapter_id="openai-api",
                    base_url="https://api.openai.com",
                ),
            ),
            resources=(
                _codex_resource("codex-sol", "w1"),
                _server_direct_resource(
                    "sol-direct",
                    "openai-http",
                    provider="openai",
                    model="gpt-5.6-sol",
                ),
            ),
        )
        cells = self._cells(configuration)
        keys = _frozen_keys(cells)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            {"worker_bridged", "server_direct_http"},
            {channel for (channel, _p, _m, _v, _f) in keys},
        )
        # Both channels carry cells for the same (provider, model): the
        # channel dimension, not the resource, separates them.
        self.assertIn(
            ("worker_bridged", "openai", "gpt-5.6-sol", None, "streaming"), keys
        )
        self.assertIn(
            ("server_direct_http", "openai", "gpt-5.6-sol", None, "streaming"), keys
        )


if __name__ == "__main__":
    _ = unittest.main()
