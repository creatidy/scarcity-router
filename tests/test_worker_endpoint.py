"""Worker-protocol endpoint tests (M05): pairing, auth, reports, dispatch.

All sessions run over in-memory transports against the real endpoint
code; the only sockets in this file belong to nothing (no listener is
started). Clocks are injected or frozen.
"""

from __future__ import annotations

import socket
import threading
import tempfile
import time
import unittest
from typing import cast, override
from collections.abc import Callable

from tests.worker_fixtures import (
    FrozenMonotonic,
    MemoryTransport,
    MutableClock,
    ScriptedWorker,
    build_registry_with_resource,
    build_worker_report,
)
from scarcity_router.gateway_adapters import AdapterCall
from scarcity_router.worker_identity_store import WorkerIdentityStore
from scarcity_router.resource_state import ResourceRegistry
from scarcity_router.worker_protocol import SocketTransport
from scarcity_router.worker_endpoint import (
    AttemptOutcome,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    LIVENESS_GRACE_INTERVALS,
    PendingAttempt,
    WorkerDispatchError,
    WorkerEndpoint,
    WorkerSession,
)
from scarcity_router.worker_protocol import (
    ERR_ATTEMPT_UNKNOWN,
    ERR_CREDENTIAL_REVOKED,
    ERR_INTERNAL,
    ERR_MALFORMED,
    ERR_PAIRING_EXPIRED,
    ERR_PAIRING_USED,
    ERR_PROTOCOL_VERSION,
    ERR_UNAUTHORIZED,
    ErrorMessage,
    ExecuteMessage,
    HelloAckMessage,
    PairResultMessage,
    HeartbeatAckMessage,
    StateReportAckMessage,
)

RESOURCE_ID = "synthetic-resource"


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class EndpointTestCase(unittest.TestCase):
    """Common world: store + registry + endpoint; sessions attach in-memory."""

    _tmp: tempfile.TemporaryDirectory[str]
    clock: MutableClock
    store: WorkerIdentityStore
    registry: ResourceRegistry
    monotonic: FrozenMonotonic
    endpoint: WorkerEndpoint
    owners: dict[str, str | None]
    threads: list[threading.Thread]

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.owners = cast("dict[str, str | None]", object())
        # Placeholders; setUp replaces them before each test body runs.
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.clock = cast("MutableClock", object())
        self.store = cast("WorkerIdentityStore", object())
        self.registry = cast("ResourceRegistry", object())
        self.monotonic = cast("FrozenMonotonic", object())
        self.endpoint = cast("WorkerEndpoint", object())
        self.threads = []

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = MutableClock()
        self.store = WorkerIdentityStore(
            f"{self._tmp.name}/identity/identities.db", clock=self.clock
        )
        self.addCleanup(self.store.close)
        self.registry = build_registry_with_resource(RESOURCE_ID)
        self.monotonic = FrozenMonotonic()
        # Administrator-simulated ownership: the test assigns the paired
        # device to the test resource explicitly (configuration is the
        # only ownership source — see OwnerResolver).
        self.owners = {}
        self.endpoint = WorkerEndpoint(
            identity_store=self.store,
            registry=self.registry,
            configured_owner=self.owners.get,
            heartbeat_interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            monotonic=self.monotonic,
        )
        self.threads = []

    def assign(self, resource_id: str, worker_id: str | None) -> None:
        """Simulate the administrator binding (or unbinding) a resource."""
        self.owners[resource_id] = worker_id

    def connect_worker(self) -> ScriptedWorker:
        worker_side, server_side = MemoryTransport.pair()
        session = self.endpoint.attach_transport(server_side)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        self.threads.append(thread)
        return ScriptedWorker(worker_side)

    def pair_worker(self, worker: ScriptedWorker) -> tuple[str, str]:
        code = self.store.begin_pairing(label="test-device")
        answer = worker.send_pair(code.pairing_code)
        assert isinstance(answer, PairResultMessage), answer
        return answer.worker_id, answer.credential

    def auth_worker(self, worker: ScriptedWorker, worker_id: str, credential: str) -> None:
        answer = worker.send_hello(worker_id, credential)
        assert isinstance(answer, HelloAckMessage), answer

    def bound_session(self) -> WorkerSession:
        session = self.endpoint.session_for_resource(RESOURCE_ID)
        self.assertTrue(session.authenticated)
        return session



class PairingEndpointTests(EndpointTestCase):
    def test_pairing_issues_a_working_identity(self) -> None:
        worker = self.connect_worker()
        worker_id, credential = self.pair_worker(worker)
        self.assertEqual((worker_id,), self.endpoint.connected_worker_ids())
        # The issued identity authenticates a fresh connection.
        second = self.connect_worker()
        self.auth_worker(second, worker_id, credential)

    def test_one_time_code_reuse_is_a_typed_protocol_error(self) -> None:
        worker = self.connect_worker()
        code = self.store.begin_pairing()
        first = worker.send_pair(code.pairing_code)
        assert isinstance(first, PairResultMessage)
        second = self.connect_worker()
        replay = second.send_pair(code.pairing_code)
        assert isinstance(replay, ErrorMessage)
        self.assertEqual(ERR_PAIRING_USED, replay.code)
        self.assertTrue(replay.fatal)
        _ = wait_until(lambda: not self.endpoint.connected_worker_ids())

    def test_expired_code_is_a_typed_protocol_error(self) -> None:
        worker = self.connect_worker()
        code = self.store.begin_pairing()
        self.clock.advance(seconds=601)
        answer = worker.send_pair(code.pairing_code)
        assert isinstance(answer, ErrorMessage), answer
        self.assertEqual(ERR_PAIRING_EXPIRED, answer.code)
        self.assertTrue(answer.fatal)

    def test_incompatible_protocol_version_fails_safely(self) -> None:
        worker = self.connect_worker()
        answer = worker.send_bad_version_hello("w-abc", "SYNTHETIC-CRED")
        assert isinstance(answer, ErrorMessage)
        self.assertEqual(ERR_PROTOCOL_VERSION, answer.code)
        self.assertTrue(answer.fatal)
        # The session never authenticates and the connection is closed.
        self.assertEqual((), self.endpoint.connected_worker_ids())

    def test_invalid_credential_is_rejected(self) -> None:
        worker = self.connect_worker()
        worker_id, credential = self.pair_worker(worker)
        impostor = self.connect_worker()
        answer = impostor.send_hello(worker_id, "SYNTHETIC-WRONG-CREDENTIAL")
        assert isinstance(answer, ErrorMessage)
        self.assertEqual(ERR_UNAUTHORIZED, answer.code)
        _ = credential

    def test_revoked_credential_is_rejected_on_next_connection(self) -> None:
        worker = self.connect_worker()
        worker_id, credential = self.pair_worker(worker)
        self.endpoint.revoke_worker(worker_id)
        _ = wait_until(lambda: worker_id not in self.endpoint.connected_worker_ids())
        next_worker = self.connect_worker()
        answer = next_worker.send_hello(worker_id, credential)
        assert isinstance(answer, ErrorMessage)
        self.assertEqual(ERR_CREDENTIAL_REVOKED, answer.code)


class SessionTests(EndpointTestCase):
    def test_heartbeat_is_acked(self) -> None:
        worker = self.connect_worker()
        _worker_id, _credential = self.pair_worker(worker)
        answer = worker.send_heartbeat(seq=1)
        assert isinstance(answer, HeartbeatAckMessage)
        self.assertEqual(1, answer.seq)

    def test_state_report_is_applied_through_the_m01_registry(self) -> None:
        worker = self.connect_worker()
        worker_id, _ = self.pair_worker(worker)
        self.assign(RESOURCE_ID, worker_id)
        answer = worker.send_state_report(
            build_worker_report(worker_id=worker_id, resource_id=RESOURCE_ID)
        )
        assert isinstance(answer, StateReportAckMessage)
        # The observation is IN the registry, via apply_worker_report.
        snapshot = self.registry.registry_snapshot()
        entry = next(e for e in snapshot.entries if e.identity.resource_id == RESOURCE_ID)
        self.assertIsNotNone(entry.observation)
        self.assertEqual(
            (worker_id,), (self.endpoint.observed_worker_bindings()[RESOURCE_ID],)
        )
        self.assertEqual((worker_id,), self.endpoint.connected_worker_ids())

    def test_state_report_for_unregistered_resource_is_rejected(self) -> None:
        worker = self.connect_worker()
        worker_id, _ = self.pair_worker(worker)
        answer = worker.send_state_report(
            build_worker_report(worker_id=worker_id, resource_id="unregistered-1")
        )
        assert isinstance(answer, ErrorMessage)
        self.assertFalse(answer.fatal)
        self.assertEqual({}, self.endpoint.observed_worker_bindings())

    def test_state_report_with_mismatched_worker_id_is_rejected(self) -> None:
        worker = self.connect_worker()
        _worker_id, _credential = self.pair_worker(worker)
        answer = worker.send_state_report(
            build_worker_report(worker_id="w-somebodyelse", resource_id=RESOURCE_ID)
        )
        assert isinstance(answer, ErrorMessage)
        self.assertFalse(answer.fatal)
        self.assertEqual({}, self.endpoint.observed_worker_bindings())

    def test_malformed_report_is_a_nonfatal_error(self) -> None:
        worker = self.connect_worker()
        worker_id, _credential = self.pair_worker(worker)
        answer = worker.send_state_report({"schema_version": 1})
        assert isinstance(answer, ErrorMessage)
        self.assertEqual(ERR_MALFORMED, answer.code)
        self.assertFalse(answer.fatal)
        # The session survives.
        self.assertIn(worker_id, self.endpoint.connected_worker_ids())

    def test_raising_inventory_sink_is_a_nonfatal_error_session_survives(self) -> None:
        # Review finding 1 (remediation): a SERVER-side adoption failure
        # must behave like the resource section — a non-fatal error frame,
        # the session stays answerable — never an unhandled exception on
        # the session thread.
        from scarcity_router.model_inventory import ModelInventoryReport

        worker = self.connect_worker()
        worker_id, _ = self.pair_worker(worker)

        def _boom(_inventory: object) -> None:
            raise ValueError("synthetic adoption failure")

        self.endpoint.inventory_sink = _boom
        inventory = ModelInventoryReport(worker_id=worker_id)
        answer = worker.send_state_report(
            {
                "schema_version": 1,
                "worker_id": worker_id,
                "reported_at": "2026-09-24T12:00:00.000Z",
                "resources": [],
            },
            inventories=(inventory.to_dict(),),
        )
        assert isinstance(answer, ErrorMessage)
        self.assertFalse(answer.fatal)
        self.assertEqual(ERR_INTERNAL, answer.code)
        # The session survived and can still be answered.
        self.assertIn(worker_id, self.endpoint.connected_worker_ids())
        self.endpoint.inventory_sink = None
        ack = worker.send_state_report(
            {
                "schema_version": 1,
                "worker_id": worker_id,
                "reported_at": "2026-09-24T12:00:00.000Z",
                "resources": [],
            }
        )
        assert isinstance(answer, ErrorMessage)
        self.assertIsInstance(ack, StateReportAckMessage)

    def test_foreign_source_inventory_is_rejected_whole(self) -> None:
        # Daybreak finding 1: an authenticated worker naming ANOTHER
        # worker's source in its inventory is rejected whole — it can
        # never adopt, poison or retire that source.
        from scarcity_router.model_inventory import (
            DiscoveredModel,
            ModelInventoryReport,
            SourceInventory,
        )

        worker = self.connect_worker()
        worker_id, _credential = self.pair_worker(worker)
        inventory = ModelInventoryReport(
            worker_id=worker_id,
            sources=(
                SourceInventory(
                    source_id="victims-source",
                    adapter_id="codex:victims-source",
                    kind="codex_subscription",
                    observed_at="2026-09-16T12:00:00.000Z",
                    auth_state="authenticated",
                    runtime_name="codex",
                    runtime_version="0.155.0",
                    models=(
                        DiscoveredModel("gpt-6-sol", ("high",)),
                    ),
                ),
            ),
        )
        answer = worker.send_state_report(
            {
                "schema_version": 1,
                "worker_id": worker_id,
                "reported_at": "2026-09-16T12:00:00.000Z",
                "resources": [],
            },
            inventories=(inventory.to_dict(),),
        )
        assert isinstance(answer, ErrorMessage)
        self.assertFalse(answer.fatal)
        # The session survives; nothing was adopted for the victim.
        self.assertIn(worker_id, self.endpoint.connected_worker_ids())

    def test_liveness_closes_silent_sessions(self) -> None:
        worker = self.connect_worker()
        worker_id, _ = self.pair_worker(worker)
        self.assertIn(worker_id, self.endpoint.connected_worker_ids())
        self.monotonic.advance(
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS * LIVENESS_GRACE_INTERVALS + 1
        )
        closed = self.endpoint.enforce_liveness()
        self.assertEqual((worker_id,), closed)
        _ = wait_until(lambda: () == self.endpoint.connected_worker_ids())
        # The transport sees EOF.
        self.assertIsNone(worker.read_server_message())

    def test_first_message_must_be_a_handshake(self) -> None:
        worker = self.connect_worker()
        worker.send_raw({"type": "heartbeat", "seq": 0})
        answer = worker.read_server_message()
        assert isinstance(answer, ErrorMessage)
        self.assertEqual(ERR_MALFORMED, answer.code)
        self.assertTrue(answer.fatal)

    def test_json_depth_bomb_is_rejected_without_leaking_the_session(self) -> None:
        worker = self.connect_worker()
        worker_id, _credential = self.pair_worker(worker)
        self.assertIn(worker_id, self.endpoint.connected_worker_ids())
        # A 60 KB frame of bare "[" cannot traverse the JSON parser:
        # json.loads raises RecursionError, which the framing layer must
        # turn into a typed fatal error followed by a CLEAN close --
        # session row removed, transport closed, no leaked thread.
        bomb = b"[" * 60000
        worker.send_raw_frame(len(bomb).to_bytes(4, "big") + bomb)
        answer = worker.read_server_message()
        assert isinstance(answer, ErrorMessage)
        self.assertEqual(ERR_MALFORMED, answer.code)
        self.assertTrue(answer.fatal)
        self.assertTrue(
            wait_until(lambda: () == self.endpoint.connected_worker_ids())
        )
        # The transport is closed after the error frame.
        self.assertIsNone(worker.read_server_message())

    def test_unknown_arbitrary_command_message_is_rejected(self) -> None:
        worker = self.connect_worker()
        worker_id, credential = self.pair_worker(worker)
        _ = worker_id, credential
        worker.send_raw({"type": "shell", "command": "rm -rf /"})
        answer = worker.read_server_message()
        assert isinstance(answer, ErrorMessage)
        self.assertEqual("unknown_message_type", answer.code)
        self.assertTrue(answer.fatal)


class ExecuteDispatchTests(EndpointTestCase):
    def dispatch(
        self, session: WorkerSession, request_id: str = "chatcmpl-1"
    ) -> tuple[str, PendingAttempt]:
        call: AdapterCall = _worker_call()
        message = ExecuteMessage(
            request_id=request_id,
            attempt_id="wa-test0001",
            adapter_id="synthetic",
            deadline="2030-01-01T00:00:00.000Z",
            call=call,
        )
        pending = session.submit_execute(message)
        return message.attempt_id, pending

    def test_chunks_and_result_flow_to_the_pending_attempt(self) -> None:
        worker = self.connect_worker()
        worker_id, credential = self.pair_worker(worker)
        _ = credential
        self.assign(RESOURCE_ID, worker_id)
        report_answer = worker.send_state_report(
            build_worker_report(worker_id=worker_id, resource_id=RESOURCE_ID)
        )
        from scarcity_router.worker_protocol import StateReportAckMessage

        assert isinstance(report_answer, StateReportAckMessage)
        session = self.bound_session()
        attempt_id, pending = self.dispatch(session)
        execute = worker.next_execute()
        self.assertEqual(attempt_id, execute.attempt_id)
        self.assertEqual(request_call_resource(execute), RESOURCE_ID)
        worker.send_chunk(attempt_id, {"kind": "text_delta", "text": " hel"})
        worker.send_chunk(attempt_id, {"kind": "text_delta", "text": "lo"})
        worker.complete(execute, stream=False)
        kind, payload = pending.take(5.0)
        self.assertEqual("chunk", kind)
        kind, payload = pending.take(5.0)
        self.assertEqual("chunk", kind)
        kind, payload = pending.take(5.0)
        self.assertEqual("outcome", kind)
        outcome = cast(AttemptOutcome, payload)
        self.assertEqual("completed", outcome.status)
        assert outcome.result is not None

    def test_disconnect_resolves_pending_attempt_as_interrupted(self) -> None:
        worker = self.connect_worker()
        worker_id, credential = self.pair_worker(worker)
        _ = credential
        self.assign(RESOURCE_ID, worker_id)
        _ = worker.send_state_report(
            build_worker_report(worker_id=worker_id, resource_id=RESOURCE_ID)
        )
        session = self.bound_session()
        _attempt_id, pending = self.dispatch(session)
        _ = worker.next_execute()
        # The network vanishes mid-execution.
        worker.transport.close()
        kind, payload = pending.take(5.0)
        self.assertEqual("outcome", kind)
        outcome = cast(AttemptOutcome, payload)
        self.assertEqual("interrupted", outcome.status)

    def test_result_for_unknown_attempt_is_answered_typed(self) -> None:
        worker = self.connect_worker()
        worker_id, credential = self.pair_worker(worker)
        _ = credential
        self.assign(RESOURCE_ID, worker_id)
        _ = worker.send_state_report(
            build_worker_report(worker_id=worker_id, resource_id=RESOURCE_ID)
        )
        worker.send_raw({
            "type": "execute_result",
            "attempt_id": "wa-unknown00",
            "status": "completed",
            "calls": [],
            "message": {"role": "assistant", "content": "x"},
            "finish_reason": "stop",
        })
        answer = worker.read_server_message()
        assert isinstance(answer, ErrorMessage)
        self.assertEqual(ERR_ATTEMPT_UNKNOWN, answer.code)
        self.assertFalse(answer.fatal)

    def test_unbound_resource_cannot_be_dispatched(self) -> None:
        with self.assertRaises(WorkerDispatchError) as caught:
            _ = self.endpoint.session_for_resource(RESOURCE_ID)
        self.assertEqual("resource_unbound", caught.exception.code)

    def test_worker_interruption_report_is_recorded(self) -> None:
        worker = self.connect_worker()
        worker_id, credential = self.pair_worker(worker)
        _ = credential
        self.assign(RESOURCE_ID, worker_id)
        _ = worker.send_state_report(
            build_worker_report(worker_id=worker_id, resource_id=RESOURCE_ID)
        )
        worker.send_interrupted("wa-lost0001")
        _ = wait_until(lambda: len(self.endpoint.interrupted_attempt_log()) == 1)
        logged_worker, attempt_id = self.endpoint.interrupted_attempt_log()[0]
        self.assertEqual(worker_id, logged_worker)
        self.assertEqual("wa-lost0001", attempt_id)


class ListenerTests(EndpointTestCase):
    """The real TCP listener path: pairing through execution, loopback."""

    @override
    def setUp(self) -> None:
        super().setUp()

    def test_loopback_listener_serves_pairing_and_execution(self) -> None:
        listener = self.endpoint.attach_listener(host="127.0.0.1", port=0)
        self.addCleanup(listener.shutdown)
        listener.serve_in_background()
        raw = socket.create_connection(("127.0.0.1", listener.bound_port), timeout=10)
        self.addCleanup(raw.close)
        worker = ScriptedWorker(SocketTransport(raw, recv_timeout_seconds=10))
        # Pairing over the real listener yields a working identity...
        code = self.store.begin_pairing(label="listener-test")
        answer = worker.send_pair(code.pairing_code)
        assert isinstance(answer, PairResultMessage), answer
        self.assertEqual((answer.worker_id,), self.endpoint.connected_worker_ids())
        # ...the administrator assigns the device; the state report is
        # observed as availability evidence...
        self.assign(RESOURCE_ID, answer.worker_id)
        report_answer = worker.send_state_report(
            build_worker_report(worker_id=answer.worker_id, resource_id=RESOURCE_ID)
        )
        assert isinstance(report_answer, StateReportAckMessage), report_answer
        # ...and a server-side dispatch reaches the worker end to end.
        call: AdapterCall = _worker_call()
        message = ExecuteMessage(
            request_id="chatcmpl-listen1",
            attempt_id="wa-listener01",
            adapter_id="synthetic",
            deadline="2030-01-01T00:00:00.000Z",
            call=call,
        )
        session = self.endpoint.session_for_resource(RESOURCE_ID)
        pending = session.submit_execute(message)
        execute = worker.next_execute()
        self.assertEqual("wa-listener01", execute.attempt_id)
        worker.complete(execute, stream=False)
        kind, payload = pending.take(5.0)
        self.assertEqual("outcome", kind)
        outcome = cast(AttemptOutcome, payload)
        self.assertEqual("completed", outcome.status)
        worker.transport.close()

    def test_pre_auth_failures_do_not_wedge_the_listener(self) -> None:
        # Round-2 review: every ACCEPTED connection holds a session row
        # from before authentication; a pre-auth failure that failed to
        # release its row pinned the bounded table until the endpoint
        # refused every legitimate connection with endpoint_busy.
        listener = self.endpoint.attach_listener(host="127.0.0.1", port=0)
        self.addCleanup(listener.shutdown)
        listener.serve_in_background()
        address = ("127.0.0.1", listener.bound_port)

        def pre_auth_connection(frame: bytes | None) -> None:
            raw = socket.create_connection(address, timeout=10)
            try:
                if frame is not None:
                    _ = raw.sendall(len(frame).to_bytes(4, "big") + frame)
            finally:
                raw.close()

        # Depth bombs and plain pre-auth disconnects: each must end in a
        # clean close that RELEASES its session row.
        bomb = b"[" * 60000
        for _ in range(80):
            pre_auth_connection(bomb)
        for _ in range(20):
            pre_auth_connection(None)
        # The bounded table drains back to empty...
        self.assertTrue(
            wait_until(lambda: self.endpoint.session_count() == 0, timeout=30)
        )
        # ...and the endpoint still serves legitimate connections.
        raw = socket.create_connection(address, timeout=10)
        self.addCleanup(raw.close)
        worker = ScriptedWorker(SocketTransport(raw, recv_timeout_seconds=10))
        code = self.store.begin_pairing(label="after-storm")
        answer = worker.send_pair(code.pairing_code)
        assert isinstance(answer, PairResultMessage), answer
        self.assertEqual(
            (answer.worker_id,), self.endpoint.connected_worker_ids()
        )
        worker.transport.close()


# ── Helpers shared with the adapter tests ─────────────────────────────────────


def _worker_call() -> AdapterCall:
    from scarcity_router.resource_state import ResourceIdentity
    from scarcity_router.selection_types import ModelIdentity

    return AdapterCall(
        resource=ResourceIdentity(
            resource_id=RESOURCE_ID,
            channel="worker_bridged",
            provider="openai",
            model="syn-model",
            entitlement="local_ungated",
        ),
        model=ModelIdentity(provider="openai", model="syn-model", variant="max"),
        messages=(),
    )


def request_call_resource(execute: ExecuteMessage) -> str:
    _ = execute
    return RESOURCE_ID


if __name__ == "__main__":
    _ = unittest.main()
