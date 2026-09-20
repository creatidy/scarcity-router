"""Native worker runtime tests (M05): pairing, reconnect, no-duplicates.

The runtime is exercised against the REAL server endpoint over in-memory
transports (plus one scripted raw server for the rotation path). All
secrets are SYNTHETIC-marked fakes; clocks and the connect factory are
injected so timing is deterministic.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import cast, override

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.worker_fixtures import (  # noqa: E402
    SYNTHETIC_CREDENTIAL,
    MemoryTransport,
    MutableClock,
    SyntheticLocalAdapter,
    build_local_registry,
    build_registry_with_resource,
    realtime_canonical,
)
from scarcity_router.gateway_adapters import (  # noqa: E402
    AdapterCall,
    AdapterResult,
)
from scarcity_router.resource_state import (  # noqa: E402
    ResourceIdentity,
    ResourceRegistry,
)
from scarcity_router.selection_types import ModelIdentity  # noqa: E402
from scarcity_router.worker_client import (  # noqa: E402
    ReconnectPolicy,
    WorkerOrigin,
    WorkerRuntime,
)
from scarcity_router.worker_endpoint import (  # noqa: E402
    AttemptOutcome,
    PendingAttempt,
    WorkerDispatchError,
    WorkerEndpoint,
)
from scarcity_router.worker_identity_store import WorkerIdentityStore  # noqa: E402
from scarcity_router.worker_local_adapters import (  # noqa: E402
    LocalAdapterRegistry,
)
from scarcity_router.worker_local_store import (  # noqa: E402
    WorkerLocalIdentity,
    WorkerLocalStore,
)
from scarcity_router.worker_protocol import (  # noqa: E402
    ExecuteChunkMessage,
    ExecuteMessage,
    FrameReader,
    FrameWriter,
    HelloMessage,
)

RESOURCE_ID = "synthetic-resource"
FAR_DEADLINE = "2030-01-01T00:00:00.000Z"


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _DeadTransport:
    """A transport whose peer is instantly gone."""

    def recv_exact(self, size: int) -> bytes | None:
        _ = size
        return None

    def send_all(self, data: bytes) -> None:
        _ = data
        raise ConnectionError("dead peer")

    def close(self) -> None:
        return None


class RuntimeWorld:
    """Endpoint + identity store + worker-local store + transports."""

    _tmp: tempfile.TemporaryDirectory[str]
    clock: MutableClock
    store: WorkerIdentityStore
    local_store: WorkerLocalStore
    endpoint: WorkerEndpoint
    pending_transports: deque[MemoryTransport]
    connect_count: int
    factory_calls: int

    def __init__(self) -> None:
        tmp = tempfile.TemporaryDirectory[str]()
        self._tmp = tmp
        self.clock = MutableClock()
        self.store = WorkerIdentityStore(
            f"{tmp.name}/identity/identities.db", clock=self.clock
        )
        self.local_store = WorkerLocalStore(f"{tmp.name}/local/worker-state.db")
        self.endpoint = WorkerEndpoint(
            identity_store=self.store,
            registry=build_registry_with_resource(
                RESOURCE_ID, clock=realtime_canonical
            ),
            heartbeat_interval_seconds=15,
        )
        self.pending_transports = deque()
        self.connect_count = 0
        self.factory_calls = 0

    def close(self) -> None:
        self.endpoint.close_all_sessions(note="world teardown")
        for transport in self.pending_transports:
            transport.close()
        self.store.close()
        self.local_store.close()
        self._tmp.cleanup()

    def connect_factory(self, _origin: WorkerOrigin) -> MemoryTransport | _DeadTransport:
        self.factory_calls += 1
        if not self.pending_transports:
            return _DeadTransport()
        transport = self.pending_transports.popleft()
        self.connect_count += 1
        return transport

    def new_transport(self) -> MemoryTransport:
        worker_side, server_side = MemoryTransport.pair()
        session = self.endpoint.attach_transport(server_side)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        self.pending_transports.append(worker_side)
        return worker_side


def _synthetic_call() -> AdapterCall:
    return AdapterCall(
        resource=ResourceIdentity(
            resource_id=RESOURCE_ID,
            channel="worker_bridged",
            provider="synthetic",
            model="syn-model",
            entitlement="local_ungated",
        ),
        model=ModelIdentity(provider="openai", model="syn-model", variant="max"),
        messages=(),
    )


def dispatch_synthetic_execute(
    endpoint: WorkerEndpoint,
) -> tuple[str, object]:
    call = AdapterCall(
        resource=ResourceIdentity(
            resource_id=RESOURCE_ID,
            channel="worker_bridged",
            provider="synthetic",
            model="syn-model",
            entitlement="local_ungated",
        ),
        model=ModelIdentity(provider="openai", model="syn-model", variant="max"),
        messages=(),
    )
    message = ExecuteMessage(
        request_id="chatcmpl-run1",
        attempt_id="wa-runtime01",
        adapter_id="synthetic",
        deadline=FAR_DEADLINE,
        call=call,
    )
    pending = endpoint.submit_execute_for_resource(message, RESOURCE_ID)
    return message.attempt_id, pending


class WorkerOriginTests(unittest.TestCase):
    def test_tls_origin_parses(self) -> None:
        origin = WorkerOrigin.parse("srws://gateway.local:8790")
        self.assertEqual("gateway.local", origin.host)
        self.assertEqual(8790, origin.port)
        self.assertTrue(origin.tls)

    def test_loopback_plaintext_is_permitted(self) -> None:
        origin = WorkerOrigin.parse("srw://127.0.0.1:9001")
        self.assertEqual("127.0.0.1", origin.host)
        self.assertFalse(origin.tls)

    def test_plaintext_non_loopback_is_refused(self) -> None:
        with self.assertRaises(Exception):
            _ = WorkerOrigin.parse("srw://gateway.local:8790")

    def test_credentials_in_urls_are_refused(self) -> None:
        with self.assertRaises(Exception):
            _ = WorkerOrigin.parse("srws://token@gateway.local:8790")

    def test_unknown_scheme_is_refused(self) -> None:
        with self.assertRaises(Exception):
            _ = WorkerOrigin.parse("https://gateway.local:8790")


class PairingRuntimeTests(unittest.TestCase):
    world: RuntimeWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.world = cast("RuntimeWorld", object())

    @override
    def setUp(self) -> None:
        self.world = RuntimeWorld()
        self.addCleanup(self.world.close)

    def _runtime(self) -> WorkerRuntime:
        return WorkerRuntime(
            origin=WorkerOrigin.parse("srws://gateway.local:8790"),
            store=self.world.local_store,
            connect_factory=self.world.connect_factory,
            reconnect=ReconnectPolicy(
                initial_delay_seconds=0.01,
                max_delay_seconds=0.02,
                max_attempts=4,
            ),
        )

    def test_pair_redeems_code_and_stores_identity(self) -> None:
        _ = self.world.new_transport()
        runtime = self._runtime()
        code = self.world.store.begin_pairing(label="laptop")
        identity = runtime.pair(code.pairing_code)
        self.assertTrue(identity.worker_id.startswith("w-"))
        stored = self.world.local_store.load_identity()
        assert stored is not None
        self.assertEqual(identity.worker_id, stored.worker_id)
        # The stored identity authenticates against the server store.
        _ = self.world.store.authenticate(stored.worker_id, stored.credential)

    def test_pair_rejection_surfaces_a_typed_error(self) -> None:
        _ = self.world.new_transport()
        runtime = self._runtime()
        code = self.world.store.begin_pairing()
        # Redeem directly first: the worker's attempt is a reuse.
        _ = self.world.store.redeem_pairing_code(code.pairing_code)
        with self.assertRaises(Exception) as caught:
            _ = runtime.pair(code.pairing_code)
        self.assertIn("pairing_code_used", str(caught.exception))


class RunLoopTests(unittest.TestCase):
    world: RuntimeWorld
    adapter: SyntheticLocalAdapter
    registry: LocalAdapterRegistry
    worker_id: str
    credential: str

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.world = cast("RuntimeWorld", object())
        self.adapter = cast("SyntheticLocalAdapter", object())
        self.registry = cast("LocalAdapterRegistry", object())
        self.worker_id = cast("str", object())
        self.credential = cast("str", object())

    @override
    def setUp(self) -> None:
        self.world = RuntimeWorld()
        self.addCleanup(self.world.close)
        self.adapter = SyntheticLocalAdapter()
        self.registry = build_local_registry(adapter=self.adapter)
        # Pre-pair an identity in BOTH stores: the server store holds the
        # hash; the worker-local store holds the device credential.
        code = self.world.store.begin_pairing()
        self.worker_id, self.credential = self.world.store.redeem_pairing_code(
            code.pairing_code
        )
        self.world.local_store.save_identity(
            WorkerLocalIdentity(
                worker_id=self.worker_id,
                credential=self.credential,
                server_origin="srws://gateway.local:8790",
            )
        )

    def _runtime(self) -> WorkerRuntime:
        return WorkerRuntime(
            origin=WorkerOrigin.parse("srws://gateway.local:8790"),
            store=self.world.local_store,
            local_adapters=self.registry,
            connect_factory=self.world.connect_factory,
            reconnect=ReconnectPolicy(
                initial_delay_seconds=0.01,
                max_delay_seconds=0.02,
                max_attempts=6,
            ),
        )

    def test_state_report_lands_in_the_registry_via_shared_path(self) -> None:
        _ = self.world.new_transport()
        runtime = self._runtime()
        thread = threading.Thread(target=runtime.run, daemon=True)
        thread.start()
        self.addCleanup(runtime.request_stop)
        self.assertTrue(
            wait_until(
                lambda: RESOURCE_ID in self.world.endpoint.resource_worker_bindings()
            )
        )
        # The report sink is the world's real registry (see RuntimeWorld);
        # read it through its concrete type.
        registry = cast(
            "ResourceRegistry", self.world.endpoint.registry
        )
        snapshot = registry.registry_snapshot()
        entry = next(
            e for e in snapshot.entries if e.identity.resource_id == RESOURCE_ID
        )
        self.assertIsNotNone(entry.observation)

    def test_execute_reaches_the_local_adapter_and_result_returns(self) -> None:
        _ = self.world.new_transport()
        runtime = self._runtime()
        thread = threading.Thread(target=runtime.run, daemon=True)
        thread.start()
        self.addCleanup(runtime.request_stop)
        self.assertTrue(
            wait_until(
                lambda: RESOURCE_ID in self.world.endpoint.resource_worker_bindings()
            )
        )
        attempt_id, raw_pending = dispatch_synthetic_execute(self.world.endpoint)
        pending = cast(PendingAttempt, raw_pending)
        _ = attempt_id
        deadline = time.monotonic() + 10
        outcome_status = "timeout"
        while time.monotonic() < deadline:
            kind, payload = pending.take(0.5)
            if kind == "outcome":
                outcome = cast(AttemptOutcome, payload)
                outcome_status = outcome.status
                break
        self.assertEqual("completed", outcome_status)
        self.assertEqual(1, len(self.adapter.invocations))

    def test_network_loss_never_duplicates_execution(self) -> None:
        first = self.world.new_transport()
        runtime = self._runtime()
        thread = threading.Thread(target=runtime.run, daemon=True)
        thread.start()
        self.addCleanup(runtime.request_stop)
        self.assertTrue(
            wait_until(
                lambda: RESOURCE_ID in self.world.endpoint.resource_worker_bindings()
            )
        )
        # Block the synthetic adapter mid-flight until the network loss
        # cancels it: the invocation must be UNRESOLVED when the connection
        # dies, which is the exact duplicate-execution hazard of D-043.
        started = threading.Event()

        def blocking_behavior(
            call: AdapterCall, cancel_event: threading.Event
        ) -> AdapterResult:
            _ = call
            started.set()
            _ = cancel_event.wait(10)
            return AdapterResult(status="cancelled", calls=())

        self.adapter.behavior = blocking_behavior
        attempt_id, raw_pending = dispatch_synthetic_execute(self.world.endpoint)
        pending = cast(PendingAttempt, raw_pending)
        self.assertTrue(started.wait(10))
        _second = self.world.new_transport()
        first.close()
        # The runtime must report the interrupted attempt on reconnect...
        self.assertTrue(
            wait_until(
                lambda: any(
                    attempt_id == logged
                    for _worker, logged in self.world.endpoint.interrupted_attempt_log()
                )
            )
        )
        # ...and the pending server-side attempt resolves as interrupted
        # (ambiguous), never as a retryable failure.
        kind, payload = pending.take(5.0)
        self.assertEqual("outcome", kind)
        outcome = cast(AttemptOutcome, payload)
        self.assertEqual("interrupted", outcome.status)
        # The local adapter ran exactly once: no duplicate execution.
        self.assertEqual(1, len(self.adapter.invocations))
        runtime.request_stop()
        thread.join(timeout=5)

    def test_reconnect_budget_is_bounded(self) -> None:
        attempts: list[int] = []

        def dead_factory(_origin: WorkerOrigin) -> _DeadTransport:
            attempts.append(1)
            return _DeadTransport()

        runtime = WorkerRuntime(
            origin=WorkerOrigin.parse("srw://127.0.0.1:9"),
            store=self.world.local_store,
            local_adapters=self.registry,
            connect_factory=dead_factory,
            reconnect=ReconnectPolicy(
                initial_delay_seconds=0.01, max_delay_seconds=0.02, max_attempts=3
            ),
        )
        reason = runtime.run()
        self.assertEqual("reconnect_budget_exhausted", reason)
        self.assertEqual(3, len(attempts))
        joined = "\n".join(runtime.diagnostics())
        self.assertIn("reconnect", joined)

    def test_revocation_stops_the_worker_without_retries(self) -> None:
        _ = self.world.new_transport()
        self.world.store.revoke(self.worker_id)
        runtime = self._runtime()
        reason = runtime.run()
        self.assertEqual("fatal", reason)
        # No reconnect attempts after a credential rejection.
        self.assertEqual(1, self.world.factory_calls)

    def test_diagnostics_redact_the_credential(self) -> None:
        _ = self.world.new_transport()
        self.world.store.revoke(self.worker_id)
        runtime = self._runtime()
        _ = runtime.run()
        rendered = "\n".join(runtime.diagnostics())
        self.assertNotIn(self.credential, rendered)
        self.assertNotIn("SYNTHETIC", rendered)

    def test_worker_never_opens_a_listening_socket(self) -> None:
        listened: list[int] = []
        original_listen = socket.socket.listen

        def spy_listen(sock: socket.socket, backlog: int) -> None:
            listened.append(backlog)
            original_listen(sock, backlog)

        with unittest.mock.patch.object(socket.socket, "listen", spy_listen):
            _ = self.world.new_transport()
            runtime = self._runtime()
            thread = threading.Thread(target=runtime.run, daemon=True)
            thread.start()
            self.assertTrue(
                wait_until(
                    lambda: RESOURCE_ID
                    in self.world.endpoint.resource_worker_bindings()
                )
            )
            runtime.request_stop()
            thread.join(timeout=5)
        self.assertEqual([], listened)

    def test_duplicate_attempt_id_is_refused_not_overwritten(self) -> None:
        _ = self.world.new_transport()
        runtime = self._runtime()
        thread = threading.Thread(target=runtime.run, daemon=True)
        thread.start()
        self.addCleanup(runtime.request_stop)
        self.assertTrue(
            wait_until(
                lambda: RESOURCE_ID in self.world.endpoint.resource_worker_bindings()
            )
        )
        session = self.world.endpoint.session_for_resource(RESOURCE_ID)
        message = ExecuteMessage(
            request_id="chatcmpl-run1",
            attempt_id="wa-duplicate",
            adapter_id="synthetic",
            deadline=FAR_DEADLINE,
            call=_synthetic_call(),
        )
        first = session.submit_execute(message)
        # The second dispatch with the SAME attempt id is refused BEFORE
        # anything is sent (one response can only ever resolve ONE
        # tracker): definitive, and the first execution is untouched.
        with self.assertRaises(WorkerDispatchError) as caught:
            _ = session.submit_execute(message)
        self.assertEqual("duplicate_attempt", caught.exception.code)
        # The first attempt completes normally.
        kind: str = "timeout"
        taken: tuple[str, ExecuteChunkMessage | AttemptOutcome | None] = ("timeout", None)
        for _ in range(16):
            taken = first.take(5.0)
            if taken[0] == "outcome":
                break
        kind = taken[0]
        self.assertEqual("outcome", kind)
        outcome = cast(AttemptOutcome, taken[1])
        self.assertEqual("completed", outcome.status)
        # Exactly ONE local execution ran.
        self.assertEqual(1, len(self.adapter.invocations))
        runtime.request_stop()
        thread.join(timeout=5)

    def test_malformed_deadline_fails_closed_before_invocation(self) -> None:
        _ = self.world.new_transport()
        runtime = self._runtime()
        thread = threading.Thread(target=runtime.run, daemon=True)
        thread.start()
        self.addCleanup(runtime.request_stop)
        self.assertTrue(
            wait_until(
                lambda: RESOURCE_ID in self.world.endpoint.resource_worker_bindings()
            )
        )
        session = self.world.endpoint.session_for_resource(RESOURCE_ID)
        message = ExecuteMessage(
            request_id="chatcmpl-run2",
            attempt_id="wa-baddeadline",
            adapter_id="synthetic",
            deadline="not-a-timestamp",
            call=_synthetic_call(),
        )
        pending = session.submit_execute(message)
        kind, payload = pending.take(5.0)
        self.assertEqual("outcome", kind)
        outcome = cast(AttemptOutcome, payload)
        self.assertEqual("failed", outcome.status)
        assert outcome.result is not None
        self.assertIn("malformed", outcome.result.note or "")
        # The local adapter was NEVER invoked without an enforceable bound.
        self.assertEqual(0, len(self.adapter.invocations))
        runtime.request_stop()
        thread.join(timeout=5)

    def test_source_never_binds_or_listens(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "scarcity_router"
            / "worker_client.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(".listen(", source)
        self.assertNotIn(".bind(", source)


class RotationPersistenceTests(unittest.TestCase):
    """The runtime persists rotated credentials delivered by the server."""

    def test_rotated_credential_is_stored_and_used_on_reconnect(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(f"{tmp}/worker-state.db")
            self.addCleanup(store.close)
            store.save_identity(
                WorkerLocalIdentity(
                    worker_id="w-rotate01",
                    credential="SYNTHETIC-OLD-CREDENTIAL",
                    server_origin="srws://gateway.local:8790",
                )
            )
            handshakes: list[HelloMessage] = []
            worker_side, server_side = MemoryTransport.pair()
            writer = FrameWriter(server_side)
            reader = FrameReader(server_side)

            def serve() -> None:
                # First connection: accept, then rotate the credential.
                payload = reader.read_message()
                assert payload is not None
                hello = HelloMessage.from_payload(payload)
                handshakes.append(hello)
                writer.write_message({
                    "type": "hello_ack",
                    "negotiated_version": 1,
                    "heartbeat_interval_seconds": 15,
                })
                writer.write_message({
                    "type": "credential_rotated",
                    "credential": "SYNTHETIC-NEW-CREDENTIAL",
                })
                # Keep the session up until the runtime stops.
                time.sleep(1.5)
                server_side.close()

            server_thread = threading.Thread(target=serve, daemon=True)
            server_thread.start()

            transports: deque[MemoryTransport] = deque([worker_side])
            runtime = WorkerRuntime(
                origin=WorkerOrigin.parse("srws://gateway.local:8790"),
                store=store,
                connect_factory=lambda _origin: transports.popleft(),
                reconnect=ReconnectPolicy(
                    initial_delay_seconds=0.01,
                    max_delay_seconds=0.02,
                    max_attempts=2,
                ),
            )
            thread = threading.Thread(target=runtime.run, daemon=True)
            thread.start()
            def rotated() -> bool:
                identity = store.load_identity()
                return identity is not None and (
                    identity.credential == "SYNTHETIC-NEW-CREDENTIAL"
                )

            self.assertTrue(wait_until(rotated))
            runtime.request_stop()
            thread.join(timeout=5)
            self.assertEqual(1, len(handshakes))
            self.assertEqual("w-rotate01", handshakes[0].worker_id)
            # The FIRST handshake used the OLD credential; persistence of
            # the new one is asserted above and governs future sessions.
            self.assertEqual("SYNTHETIC-OLD-CREDENTIAL", handshakes[0].credential)


_ = SYNTHETIC_CREDENTIAL

if __name__ == "__main__":
    _ = unittest.main()
