"""M10 end-to-end acceptance suite, part 2: execution paths (issue #95).

Real composed-server dispatch against SYNTHETIC backends only — no live
provider, no paid inference, no quota:

6.  generic HTTP provider end to end (M04 adapter against a loopback
    synthetic origin), streaming and non-streaming;
7.  direct Ollama-compatible backend (the ``ollama`` preset) end to end;
8.  worker pairing through the real endpoint over verified TLS (one-time
    code redeemed inside the protocol handshake);
9.  worker-bridged Ollama execution end to end (composed server →
    coordinator → real worker session → local adapter → synthetic Ollama
    origin → client);
10. exact resource ownership (D-049): a second worker can neither report
    nor execute a resource it does not own;
11. cancellation propagates (client disconnect → cancel at the worker);
12. ambiguous worker disconnect resolves honestly and never blindly
    duplicates execution on reconnect.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from scarcity_router.gateway_server import GatewayHTTPServer

from scarcity_router.resource_state import (
    ResourceIdentity,
    ResourceStateSnapshot,
)
from scarcity_router.worker_bridged_adapter import WorkerBridgedAdapter
from scarcity_router.worker_client import WorkerOrigin, WorkerRuntime
from scarcity_router.worker_endpoint import (
    WorkerEndpoint,
    build_tls_context as build_worker_tls,
)
from scarcity_router.worker_identity_store import WorkerIdentityStore
from scarcity_router.worker_local_adapters import (
    LocalAdapterRegistry,
    LoopbackOllamaAdapter,
)
from scarcity_router.worker_local_store import WorkerLocalStore
from scarcity_router.worker_protocol import SocketTransport, StateReportAckMessage

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from scarcity_router.gateway_server import GatewayHTTPServer

from tests.gateway_fixtures import (
    T_EVAL,
    T_NOW as GATEWAY_T_NOW,
    build_cells,
    build_registry,
    make_application,
)
from tests.m10_fixtures import (
    PROMPT_MARKER,
    RESPONSE_MARKER,
    RealTimeServerHarness,
    TlsMaterials,
    wait_until,
)
from tests.openai_http_fixtures import ScriptedProviderServer
from tests.server_fixtures import FAKE_PROVIDER_SECRET
from tests.worker_fixtures import MutableClock, ScriptedWorker, build_worker_report

ZAI_RESOURCE_DOCUMENT = {
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


def _bridged_resource_document(worker_id: str) -> dict[str, object]:
    """One worker-bridged resource configuration.

    The registry-facing identity uses a catalog provider (the model the
    worker's loopback endpoint SERVES): pinned references construct a
    catalog ModelIdentity, and worker reports must match the registered
    identity exactly.
    """
    return {
        "registration": {
            "identity": {
                "resource_id": "e2e-ollama",
                "channel": "worker_bridged",
                "provider": "zai",
                "model": "glm-5.3",
                "entitlement": "subscription_included",
            },
            "freshness_ttl_seconds": 300,
            "capabilities": {"context_limit_tokens": 272_000},
        },
        "enabled": True,
        "worker_id": worker_id,
        "local_adapter_id": "ollama",
    }


def _server_observation(
    resource_id: str, provider: str, model: str
) -> ResourceStateSnapshot:
    from datetime import datetime, timezone

    from scarcity_router.resource_state import (
        ResourceHealth,
        ResourceIdentity,
    )

    identity = ResourceIdentity(
        resource_id=resource_id,
        channel="server_direct_http",
        provider=provider,
        model=model,
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


class _ServerDirectWorld(RealTimeServerHarness):
    """Harness + one synthetic OpenAI-compatible origin, wired by config."""

    provider: ScriptedProviderServer

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.provider = cast(ScriptedProviderServer, object())

    def wire_provider(
        self, *, adapter_id: str = "generic-openai", requires_secret: bool = True
    ) -> ScriptedProviderServer:
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        document: dict[str, object] = {
            "provider_id": "zai-http",
            "adapter_id": adapter_id,
            "base_url": provider.origin,
        }
        if requires_secret:
            document["secret"] = FAKE_PROVIDER_SECRET
        status, payload = self.admin_post("/control/providers", document)
        assert status == 200, payload
        return provider


# ── Scenarios 6-7: server-direct HTTP backends ────────────────────────────────


class ServerDirectEndToEndTests(_ServerDirectWorld):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def _configure_resource(self) -> None:
        status, payload = self.admin_post("/control/resources", ZAI_RESOURCE_DOCUMENT)
        self.assertEqual(200, status, payload)
        self.plane.apply_resource_observation(
            _server_observation("zai-plan-1", "zai", "glm-5.3")
        )

    def test_scenario_06_generic_http_provider_nonstreaming(self) -> None:
        provider = self.wire_provider(adapter_id="generic-openai")
        self._configure_resource()
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
        document = cast("dict[str, object]", payload)
        choices = cast("list[object]", document["choices"])
        message = cast("dict[str, object]", choices[0])["message"]
        self.assertEqual(
            RESPONSE_MARKER, cast("dict[str, object]", message)["content"]
        )
        # Configuration followed exactly: preset path, configured origin,
        # store-held credential, router-loop marker stamped.
        request = provider.last_request
        self.assertEqual("/v1/chat/completions", request.path)
        self.assertEqual(
            f"Bearer {FAKE_PROVIDER_SECRET}", request.headers["authorization"]
        )
        self.assertIn("x-scarcity-router-gateway", request.headers)
        # The provider payload carries the prompt (the one authorized
        # target) — and nothing else in the response re-exports secrets.
        self.assertNotIn(FAKE_PROVIDER_SECRET, json.dumps(payload))

    def test_scenario_06_streaming_fails_closed_without_matrix_evidence(self) -> None:
        """A streaming request is refused before inference when the
        deployment's compatibility matrix carries no evidence (D-043:
        UNKNOWN cells fail closed). The composed deployment registers no
        cells, so ``stream: true`` is an explicit 400 — never a silent
        degradation — and the synthetic provider is never contacted.
        The evidenced streaming proof runs on the M03 surface in
        test_e2e_acceptance (scenario 5)."""
        provider = self.wire_provider(adapter_id="generic-openai")
        self._configure_resource()
        connection = self.open_stream(
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            bearer=self.client_key,
        )
        try:
            response = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        self.assertEqual(400, response.status)
        self.assertIn("compatibility_unknown", raw)
        self.assertEqual(0, provider.request_count)

    def test_scenario_07_ollama_preset_direct_requires_no_credential(self) -> None:
        provider = self.wire_provider(adapter_id="ollama", requires_secret=False)
        status, payload = self.admin_post("/control/resources", ZAI_RESOURCE_DOCUMENT)
        self.assertEqual(200, status, payload)
        self.plane.apply_resource_observation(
            _server_observation("zai-plan-1", "zai", "glm-5.3")
        )
        provider.enqueue_completion(
            content="local reply",
            usage={"prompt_tokens": 4, "completion_tokens": 2},
        )
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(200, status, payload)
        self.assertEqual("/v1/chat/completions", provider.last_request.path)
        # The ollama preset requires no credential: no Authorization walks
        # to the local origin.
        self.assertNotIn("authorization", provider.last_request.headers)


# ── Scenarios 8-12: the real worker path over TLS ─────────────────────────────


class WorkerWorld(RealTimeServerHarness):
    """Composed server + TLS worker-protocol listener + worker-side tools.

    The worker connects with REAL verified-TLS sockets whose trust anchor
    is the ephemeral test CA; the verification behavior (right CA works,
    wrong CA fails) is exactly the production behavior modulo the trust
    anchor distribution.
    """

    tls: TlsMaterials
    worker_port: int

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.tls = cast(TlsMaterials, object())
        self.worker_port = 0

    def attach_worker_listener(self) -> int:
        self.tls = TlsMaterials()
        self.addCleanup(self.tls.cleanup)
        context = build_worker_tls(
            str(self.tls.server_cert), str(self.tls.server_key)
        )
        listener = self.plane.worker_endpoint.attach_listener(
            host="127.0.0.1", port=0, tls_context=context
        )
        listener.serve_in_background()
        self.addCleanup(listener.shutdown)
        self.worker_port = listener.bound_port
        return self.worker_port

    def worker_origin(self) -> WorkerOrigin:
        return WorkerOrigin.parse(f"srws://127.0.0.1:{self.worker_port}")

    def connect_transport(
        self, context: ssl.SSLContext
    ) -> SocketTransport:
        raw = socket.create_connection(("127.0.0.1", self.worker_port), timeout=10)
        try:
            wrapped = context.wrap_socket(raw, server_hostname="127.0.0.1")
            return SocketTransport(wrapped)
        except BaseException:
            raw.close()
            raise

    def open_worker_store(self, name: str) -> WorkerLocalStore:
        directory = Path(tempfile.mkdtemp(prefix=f"scarcity-router-{name}-"))
        self.addCleanup(lambda: _rmtree(directory))
        return WorkerLocalStore(directory / "worker-state.db")

    def paired_runtime(
        self,
        *,
        store: WorkerLocalStore,
        label: str,
        local_adapters: LocalAdapterRegistry | None = None,
        context: ssl.SSLContext | None = None,
    ) -> tuple[WorkerRuntime, str]:
        """Issue a pairing code and redeem it over the REAL TLS endpoint."""
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": label}
        )
        assert status == 200, payload
        code = cast("dict[str, object]", payload)["pairing_code"]
        origin = self.worker_origin()

        def factory(origin_ref: WorkerOrigin) -> SocketTransport:
            client_context = (
                context if context is not None else self.tls.client_context()
            )
            raw = socket.create_connection((origin_ref.host, origin_ref.port), timeout=10)
            try:
                wrapped = client_context.wrap_socket(
                    raw, server_hostname=origin_ref.host
                )
                return SocketTransport(wrapped)
            except BaseException:
                raw.close()
                raise

        runtime = WorkerRuntime(
            origin=origin,
            store=store,
            local_adapters=local_adapters,
            # Only the INITIAL report: periodic reports would consume the
            # synthetic origin's scripted behaviors mid-test.
            state_report_interval_seconds=0,
            connect_factory=factory,
        )
        identity = runtime.pair(str(code))
        return runtime, identity.worker_id


def _rmtree(directory: Path) -> None:
    import shutil

    _ = shutil.rmtree(directory, ignore_errors=True)


class WorkerPairingTests(WorkerWorld):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()
        _ = self.attach_worker_listener()

    def test_scenario_08_pairing_over_real_tls(self) -> None:
        store = self.open_worker_store("pair")
        runtime, worker_id = self.paired_runtime(store=store, label="laptop")
        identity = store.load_identity()
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(worker_id, identity.worker_id)
        self.assertTrue(identity.credential)
        self.assertEqual(
            f"srws://127.0.0.1:{self.worker_port}", identity.server_origin
        )
        # The workers listing shows the device with a real connection state
        # (not connected right now — the runtime only paired).
        listing = self.plane.worker_admin.list_workers()
        self.assertTrue(any(row.worker_id == worker_id for row in listing))
        _ = runtime
        store.close()

    def test_scenario_08_wrong_ca_fails_the_handshake(self) -> None:
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "hostile"}
        )
        self.assertEqual(200, status)
        code = cast("dict[str, object]", payload)["pairing_code"]
        origin = self.worker_origin()
        store = self.open_worker_store("wrongca")
        runtime = WorkerRuntime(
            origin=origin,
            store=store,
            connect_factory=lambda o: self.connect_transport(
                self.tls.wrong_ca_context()
            ),
        )
        with self.assertRaises(Exception) as ctx:
            _ = runtime.pair(str(code))
        # The failure is the TLS verification failure, not a pairing error.
        self.assertNotIn("pairing", str(ctx.exception).lower())
        self.assertIsNone(store.load_identity())
        store.close()


class WorkerBridgedExecutionTests(WorkerWorld):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()
        _ = self.attach_worker_listener()

    def _ollama_registry(self, provider_port: int) -> LocalAdapterRegistry:
        identity = ResourceIdentity(
            resource_id="e2e-ollama",
            channel="worker_bridged",
            provider="zai",
            model="glm-5.3",
            entitlement="subscription_included",
        )
        adapter = LoopbackOllamaAdapter(
            resource=identity, host="127.0.0.1", port=provider_port
        )
        registry = LocalAdapterRegistry()
        registry.register(adapter)
        return registry

    def _configure_ollama_resource(self, worker_id: str) -> None:
        status, payload = self.admin_post(
            "/control/resources", _bridged_resource_document(worker_id)
        )
        self.assertEqual(200, status, payload)

    def test_scenario_09_worker_bridged_ollama_end_to_end(self) -> None:
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        store = self.open_worker_store("bridge")
        runtime, worker_id = self.paired_runtime(
            store=store,
            label="gpu-box",
            local_adapters=self._ollama_registry(provider.port),
        )
        self._configure_ollama_resource(worker_id)
        # The initial state report probes the local endpoint's health.
        provider.enqueue_json(200, {"version": "0.0.0-synthetic"})
        session_reason: list[str] = []
        session = threading.Thread(
            target=lambda: session_reason.append(runtime.run()), daemon=True
        )
        session.start()
        try:
            # The initial state report lands through the real session.
            wait_until(
                lambda: any(
                    snapshot.identity.resource_id == "e2e-ollama"
                    for snapshot in self.plane._observations.values()  # pyright: ignore[reportPrivateUsage] - acceptance reads the seam
                ),
                timeout=15,
                message="worker state report never landed",
            )
            provider.enqueue_completion(
                content=RESPONSE_MARKER,
                usage={"prompt_tokens": 6, "completion_tokens": 3},
            )
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "sr-pin:e2e-ollama/zai/glm-5.3/max",
                    "messages": [{"role": "user", "content": PROMPT_MARKER}],
                },
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            document = cast("dict[str, object]", payload)
            choices = cast("list[object]", document["choices"])
            message = cast("dict[str, object]", choices[0])["message"]
            self.assertEqual(
                RESPONSE_MARKER, cast("dict[str, object]", message)["content"]
            )
            # The synthetic Ollama origin actually served the completion
            # (plus the initial report's health probe).
            completions = [
                request
                for request in provider.requests
                if request.method == "POST" and request.path == "/v1/chat/completions"
            ]
            self.assertEqual(1, len(completions))
        finally:
            runtime.request_stop()
            _ = session.join(timeout=15)
            store.close()

    def test_scenario_10_ownership_is_exact(self) -> None:
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        store_a = self.open_worker_store("owner")
        runtime_a, worker_a = self.paired_runtime(
            store=store_a,
            label="owner",
            local_adapters=self._ollama_registry(provider.port),
        )
        store_b = self.open_worker_store("other")
        runtime_b, _worker_b = self.paired_runtime(store=store_b, label="other")
        # The resource is owned by worker A only.
        self._configure_ollama_resource(worker_a)
        # The initial state report probes the local endpoint's health.
        provider.enqueue_json(200, {"version": "0.0.0-synthetic"})

        sessions: list[threading.Thread] = []
        reason_a: list[str] = []
        reason_b: list[str] = []
        sessions.append(
            threading.Thread(target=lambda: reason_a.append(runtime_a.run()), daemon=True)
        )
        sessions.append(
            threading.Thread(target=lambda: reason_b.append(runtime_b.run()), daemon=True)
        )
        for session in sessions:
            session.start()
        try:
            wait_until(
                lambda: any(
                    snapshot.identity.resource_id == "e2e-ollama"
                    for snapshot in self.plane._observations.values()  # pyright: ignore[reportPrivateUsage] - acceptance reads the seam
                ),
                timeout=15,
                message="owner report never landed",
            )
            # Worker B reporting worker A's resource is REJECTED by the
            # endpoint (configured ownership is the only authority).
            status_b, payload_b = self._report_via_protocol(
                credential_store=store_b,
                resource_id="e2e-ollama",
            )
            self.assertEqual(1, status_b)
            # The endpoint rejects the ENTIRE report fail-closed.
            self.assertIn("not configured for this worker", str(payload_b))

            # A pinned execution reaches ONLY the owner.
            provider.enqueue_completion(
                content="owned reply",
                usage={"prompt_tokens": 5, "completion_tokens": 2},
            )
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "sr-pin:e2e-ollama/zai/glm-5.3/max",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            completions = [
                request
                for request in provider.requests
                if request.method == "POST" and request.path == "/v1/chat/completions"
            ]
            self.assertEqual(1, len(completions))
        finally:
            runtime_a.request_stop()
            runtime_b.request_stop()
            for session in sessions:
                _ = session.join(timeout=15)
            store_a.close()
            store_b.close()

    def _report_via_protocol(
        self, *, credential_store: WorkerLocalStore, resource_id: str
    ) -> tuple[int | None, object]:
        """One raw protocol session: hello, then a hijacked state report."""
        identity = credential_store.load_identity()
        assert identity is not None
        transport = self.connect_transport(self.tls.client_context())
        worker = ScriptedWorker(transport)
        try:
            hello = worker.send_hello(identity.worker_id, identity.credential)
            if hello is None:
                return None, None
            report = build_worker_report(
                worker_id=identity.worker_id,
                resource_id=resource_id,
                provider="zai",
                model="glm-5.3",
                entitlement="subscription_included",
            )
            message = worker.send_state_report(report)
            detail = getattr(message, "message", None)
            if isinstance(detail, str):
                return 1, detail
            return 0, "ack"
        finally:
            transport.close()




# ── Scenarios 11-12: cancellation and ambiguous disconnect over real sockets ──
#
# The worker transport is a REAL TCP socket and dispatch runs through the
# REAL coordinator and WorkerBridgedAdapter. The one synthetic input is
# the compatibility matrix, supplied as explicit evidence cells exactly
# like ``gateway_fixtures``: an empty matrix refuses every streaming
# request closed by design (D-043) — the M09-composed deployment behaves
# exactly that way, which test_e2e_acceptance pins.

RESOURCE_ID = "openai-worker"
ADAPTER_ID = "synthetic"
EVIDENCED_API_KEY = "sk-sr-e2e-worker-key-0"
PIN_MODEL = "sr-pin:openai-worker/openai/gpt-5.6-luna/max"


class EvidencedWorkerWorld(unittest.TestCase):
    """Bare M03 gateway + evidenced matrix + real worker endpoint."""

    endpoint: WorkerEndpoint
    identity_store: WorkerIdentityStore
    assignment: dict[str, str | None]
    server: "GatewayHTTPServer"
    thread: threading.Thread
    http_port: int
    worker_port: int
    workers: list[ScriptedWorker]

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.endpoint = cast(WorkerEndpoint, object())
        self.identity_store = cast(WorkerIdentityStore, object())
        self.assignment = {}
        self.server = cast("GatewayHTTPServer", object())
        self.thread = cast(threading.Thread, object())
        self.http_port = 0
        self.worker_port = 0
        self.workers = []

    @override
    def setUp(self) -> None:
        from scarcity_router.gateway_contracts import ClientKeyDirectory
        from scarcity_router.gateway_server import make_gateway_server

        directory = Path(tempfile.mkdtemp(prefix="scarcity-router-e2e-worker-"))
        self.addCleanup(lambda: _rmtree(directory))
        self.assignment = {}
        self.identity_store = WorkerIdentityStore(
            directory / "identities.db", clock=MutableClock()
        )
        self.addCleanup(self.identity_store.close)
        self.endpoint = WorkerEndpoint(
            identity_store=self.identity_store,
            registry=build_registry(with_worker=True),
            configured_owner=self.assignment.get,
            heartbeat_interval_seconds=15,
        )
        self.workers = []
        application = make_application(
            registry=build_registry(with_worker=True),
            cells=build_cells(include_worker=True),
            adapters=[
                WorkerBridgedAdapter(
                    self.endpoint,
                    resource_adapter_map={RESOURCE_ID: ADAPTER_ID},
                    # The fixture clock drives the coordinator's deadlines;
                    # the adapter must share it (real wall time would see
                    # a fixture deadline as already past).
                    now=lambda: T_EVAL,
                )
            ],
            client_key_directory=ClientKeyDirectory.from_secrets(
                {"e2e-client": EVIDENCED_API_KEY}
            ),
        )
        self.server = make_gateway_server(application, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.http_port = cast("tuple[str, int]", self.server.server_address)[1]
        listener = self.endpoint.attach_listener(host="127.0.0.1", port=0)
        listener.serve_in_background()
        self.addCleanup(listener.shutdown)
        self.worker_port = listener.bound_port

    @override
    def tearDown(self) -> None:
        self.endpoint.close_all_sessions(note="e2e teardown")
        for worker in self.workers:
            _ = worker.transport.close()
        self.server.shutdown()
        _ = self.thread.join(timeout=10)

    def connect_scripted(self) -> ScriptedWorker:
        raw = socket.create_connection(("127.0.0.1", self.worker_port), timeout=10)
        scripted = ScriptedWorker(SocketTransport(raw))
        self.workers.append(scripted)
        return scripted

    def pair_worker(self) -> tuple[ScriptedWorker, str, str]:
        """Pair one device over the real endpoint; bind and report."""
        scripted = self.connect_scripted()
        pairing = self.identity_store.begin_pairing(label="e2e")
        answer = scripted.send_pair(pairing.pairing_code)
        credential = getattr(answer, "credential", None)
        worker_id = getattr(answer, "worker_id", None)
        assert isinstance(credential, str) and isinstance(worker_id, str), answer
        # The administrator binds the device BEFORE its report
        # (configuration is the only ownership source, D-049).
        self.assignment[RESOURCE_ID] = worker_id
        report_answer = scripted.send_state_report(
            build_worker_report(
                worker_id=worker_id,
                resource_id=RESOURCE_ID,
                provider="openai",
                model="gpt-5.6-luna",
                entitlement="subscription_included",
                reported_at=GATEWAY_T_NOW,
            )
        )
        self.assertIsInstance(report_answer, StateReportAckMessage)
        return scripted, worker_id, credential

    def open_stream(self) -> http.client.HTTPConnection:
        """Send the streaming request; headers arrive with the first chunk.

        The caller fetches ``connection.getresponse()`` only after the
        worker emits its first chunk (the gateway defers the 200 until
        then, so calling it earlier would block).
        """
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.http_port, timeout=30
        )
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {
                    "model": PIN_MODEL,
                    "messages": [{"role": "user", "content": PROMPT_MARKER}],
                    "stream": True,
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {EVIDENCED_API_KEY}",
            },
        )
        return connection


    def open_raw_stream(self) -> socket.socket:
        """Send the streaming request over a raw client socket.

        The caller reads SSE frames directly and may half-close the write
        side to simulate a client disconnect deterministically (a clean
        FIN — the exact EOF the gateway's disconnect check looks for).
        """
        body = json.dumps(
            {
                "model": PIN_MODEL,
                "messages": [{"role": "user", "content": PROMPT_MARKER}],
                "stream": True,
            }
        ).encode("utf-8")
        raw = socket.create_connection(("127.0.0.1", self.http_port), timeout=30)
        request = (
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            f"Authorization: Bearer {EVIDENCED_API_KEY}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        _ = raw.sendall(request + body)
        return raw

    @staticmethod
    def read_until_headers(sock: socket.socket) -> bytes:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            piece = sock.recv(4096)
            if not piece:
                break
            buffer += piece
        return buffer


class CancellationTests(EvidencedWorkerWorld):
    def test_scenario_11_client_disconnect_propagates_cancel(self) -> None:
        worker, _worker_id, _credential = self.pair_worker()
        raw = self.open_raw_stream()
        try:
            execute = worker.next_execute(timeout=15)
            # Chunk 1 commits the SSE stream; the client reads it. The
            # frames may arrive together with the headers (single TCP
            # segment), so this read tolerates a short timeout.
            worker.send_chunk(execute.attempt_id, {"kind": "text_delta", "text": "par"})
            # Generous read window: CI machines can pause processes.
            _ = raw.settimeout(60.0)
            head = self.read_until_headers(raw)
            self.assertIn(" 200 ", head.decode("ascii", errors="replace"))
            _ = raw.settimeout(2.0)
            try:
                first = raw.recv(4096)
            except (TimeoutError, OSError):
                first = b""  # already consumed with the headers
            self.assertTrue(first or b"chunk" in head)
            _ = raw.settimeout(0.5)
            # The client disconnects: a clean FIN on the request socket.
            _ = raw.shutdown(socket.SHUT_WR)
            # The next worker chunk drives the gateway's disconnect check;
            # the cancellation must propagate back to the worker. The
            # reader thread records the cancel while the test drains.
            worker.start_reader()
            worker.send_chunk(execute.attempt_id, {"kind": "text_delta", "text": "tial"})
            _ = raw.settimeout(0.5)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not worker.cancels:
                try:
                    piece = raw.recv(4096)  # drain; may EOF or time out
                    if not piece:
                        break  # the server closed its side after cancelling
                except (TimeoutError, OSError):
                    continue
            self.assertTrue(worker.cancels, "cancel never reached the worker")
        finally:
            raw.close()

    def test_scenario_11_cancel_reaches_exactly_the_owning_worker(self) -> None:
        worker, _worker_id, _credential = self.pair_worker()
        bystander = self.connect_scripted()
        raw = self.open_raw_stream()
        try:
            execute = worker.next_execute(timeout=15)
            worker.send_chunk(execute.attempt_id, {"kind": "text_delta", "text": "par"})
            _head = self.read_until_headers(raw)
            _ = raw.recv(4096)
            _ = raw.shutdown(socket.SHUT_WR)
            worker.start_reader()
            worker.send_chunk(execute.attempt_id, {"kind": "text_delta", "text": "tial"})
            _ = raw.settimeout(0.5)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not worker.cancels:
                try:
                    piece = raw.recv(4096)
                    if not piece:
                        break
                except (TimeoutError, OSError):
                    continue
            self.assertTrue(worker.cancels)
            # No cancel (or anything else) ever reaches a second session.
            self.assertEqual([], bystander.cancels)
        finally:
            raw.close()


class AmbiguousDisconnectTests(EvidencedWorkerWorld):
    def test_scenario_12_disconnect_resolves_interrupted_and_never_duplicates(
        self,
    ) -> None:
        worker, worker_id, credential = self.pair_worker()
        raw = self.open_raw_stream()
        execute = worker.next_execute(timeout=15)
        # The worker vanishes mid-execution without any result.
        worker.transport.close()
        raw.close()

        # The replacement connection authenticates with the SAME
        # credential, but the interrupted attempt is never re-dispatched.
        worker2 = self.connect_scripted()
        hello = worker2.send_hello(worker_id, credential)
        self.assertIsNotNone(hello)
        got_execute = False
        try:
            _ = worker2.next_execute(timeout=2.0)
            got_execute = True
        except (AssertionError, TimeoutError):
            # AssertionError: clean EOF; TimeoutError: the read window
            # elapsed with nothing but silence — either way no execute.
            got_execute = False
        self.assertFalse(got_execute, "a duplicate execute reached the new session")
        _ = execute


if __name__ == "__main__":
    _ = unittest.main()
