"""Worker-bridged adapter tests (M05): honest dispatch over the protocol.

Includes the end-to-end coordinator integration: a GatewayApplication
(the M03 coordinator with real routing) dispatching a pinned request
through :class:`WorkerBridgedAdapter` over the real endpoint to a
scripted worker. Disconnect ambiguity, cancellation propagation,
timeout and no-duplicate-dispatch semantics are all covered.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, override

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.gateway_fixtures import (  # noqa: E402
    T_NOW as GATEWAY_T_NOW,
    build_cells,
    build_registry,
    make_application,
    audit_records,
)
from tests.worker_fixtures import (  # noqa: E402
    MemoryTransport,
    MutableClock,
    ScriptedWorker,
    build_worker_report,
)
from scarcity_router.gateway_adapters import (  # noqa: E402
    AdapterAmbiguousError,
    AdapterCall,
    AdapterPermanentError,
    AdapterResult,
    AdapterStreamChunk,
    AdapterTimeoutError,
    ClientDisconnectedError,
    ExecutionContext,
)
from scarcity_router.gateway_coordinator import GatewayApplication  # noqa: E402
from scarcity_router.gateway_openai import ChatCompletionRequest  # noqa: E402
from scarcity_router.gateway_contracts import GatewayError  # noqa: E402
from scarcity_router.gateway_adapters import CompletionOutcome as CompletionOutcomeContract  # noqa: E402
from scarcity_router.resource_state import ResourceIdentity  # noqa: E402
from scarcity_router.selection_types import ModelIdentity  # noqa: E402
from scarcity_router.worker_bridged_adapter import (
    NowFactory,
    WorkerBridgedAdapter,
)
from scarcity_router.worker_endpoint import PendingAttempt, WorkerEndpoint
from scarcity_router.worker_identity_store import WorkerIdentityStore  # noqa: E402
from scarcity_router.worker_protocol import (  # noqa: E402
    PairResultMessage,
    StateReportAckMessage,
)

RESOURCE_ID = "openai-worker"
ADAPTER_ID = "synthetic"


class AdapterWorld:
    """Endpoint + bound scripted worker + the adapter under test."""

    _tmp: tempfile.TemporaryDirectory[str]
    clock: MutableClock
    store: WorkerIdentityStore
    endpoint: WorkerEndpoint
    workers: list[ScriptedWorker]
    worker: ScriptedWorker

    def __init__(self) -> None:
        tmp = tempfile.TemporaryDirectory[str]()
        self._tmp = tmp
        self.clock = MutableClock()
        self.store = WorkerIdentityStore(
            f"{tmp.name}/identity/identities.db", clock=self.clock
        )
        # Administrator-simulated ownership (configuration is the only
        # source; the world assigns each bound device explicitly).
        self.owners: dict[str, str | None] = {}
        self.endpoint = WorkerEndpoint(
            identity_store=self.store,
            registry=build_registry(with_worker=True),
            configured_owner=self.owners.get,
            heartbeat_interval_seconds=15,
        )
        self.workers = []
        self.worker = self._connect_and_bind()

    def close(self) -> None:
        self.endpoint.close_all_sessions(note="world teardown")
        for worker in self.workers:
            worker.transport.close()
        self.store.close()
        self._tmp.cleanup()

    def _connect_and_bind(self) -> ScriptedWorker:
        worker_side, server_side = MemoryTransport.pair()
        session = self.endpoint.attach_transport(server_side)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        scripted = ScriptedWorker(worker_side)
        self.workers.append(scripted)
        code = self.store.begin_pairing(label="adapter-test")
        answer = scripted.send_pair(code.pairing_code)
        assert isinstance(answer, PairResultMessage), answer
        # The administrator binds this device to the world's resource
        # before its report (configuration is the only ownership source).
        self.owners[RESOURCE_ID] = answer.worker_id
        report_answer = scripted.send_state_report(
            build_worker_report(
                worker_id=answer.worker_id,
                resource_id=RESOURCE_ID,
                provider="openai",
                model="gpt-5.6-luna",
                entitlement="subscription_included",
                reported_at=GATEWAY_T_NOW,
            )
        )
        assert isinstance(report_answer, StateReportAckMessage), report_answer
        return scripted

    def reconnect(self) -> ScriptedWorker:
        """A replacement bound worker connection (network restart)."""
        return self._connect_and_bind()

    def make_adapter(
        self,
        *,
        now: NowFactory | None = None,
        poll_seconds: float = 0.05,
    ) -> WorkerBridgedAdapter:
        return WorkerBridgedAdapter(
            self.endpoint,
            resource_adapter_map={RESOURCE_ID: ADAPTER_ID},
            now=now,
            poll_seconds=poll_seconds,
        )


def make_context(
    *,
    request_id: str = "chatcmpl-test1",
    deadline: str = "2030-01-01T00:00:00.000Z",
    emit: Callable[[AdapterStreamChunk], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        request_id=request_id,
        deadline=deadline,
        cancel_event=cancel_event if cancel_event is not None else threading.Event(),
        emit_chunk=emit,
    )


def make_call(*, stream: bool = False) -> AdapterCall:
    return AdapterCall(
        resource=ResourceIdentity(
            resource_id=RESOURCE_ID,
            channel="worker_bridged",
            provider="openai",
            model="gpt-5.6-luna",
            entitlement="subscription_included",
        ),
        model=ModelIdentity(provider="openai", model="gpt-5.6-luna", variant="max"),
        messages=(),
        stream=stream,
    )


def dispatch_in_thread(
    adapter: WorkerBridgedAdapter, context: ExecutionContext
) -> tuple[threading.Thread, dict[str, object]]:
    """Run adapter.execute on a daemon thread; capture outcome or error."""
    captured: dict[str, object] = {}

    def run() -> None:
        try:
            captured["result"] = adapter.execute(make_call(), context)
        except BaseException as exc:  # noqa: BLE001 - captured on purpose
            captured["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, captured


class DispatchRun:
    """Lazy handle over a background dispatch: read AFTER join."""

    def __init__(
        self, thread: threading.Thread, captured: dict[str, object]
    ) -> None:
        self.thread: threading.Thread = thread
        self._captured: dict[str, object] = captured

    def join(self, timeout: float) -> None:
        self.thread.join(timeout=timeout)

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()

    @property
    def result(self) -> AdapterResult | None:
        outcome = self._captured.get("result")
        return cast("AdapterResult | None", outcome)

    @property
    def error(self) -> BaseException | None:
        failure = self._captured.get("error")
        return cast("BaseException | None", failure)


def run_dispatch(
    adapter: WorkerBridgedAdapter, context: ExecutionContext
) -> DispatchRun:
    thread, captured = dispatch_in_thread(adapter, context)
    return DispatchRun(thread, captured)


class AdapterHappyPathTests(unittest.TestCase):
    world: AdapterWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.world = cast("AdapterWorld", object())

    @override
    def setUp(self) -> None:
        self.world = AdapterWorld()
        self.addCleanup(self.world.close)

    def test_execute_streams_chunks_and_completes_with_usage(self) -> None:
        adapter = self.world.make_adapter()
        chunks: list[str] = []

        def emit(chunk: AdapterStreamChunk) -> None:
            if chunk.text is not None:
                chunks.append(chunk.text)

        run = run_dispatch(adapter, make_context(emit=emit))
        execute = self.world.worker.next_execute()
        self.world.worker.complete(execute)
        run.join(10)
        self.assertFalse(run.alive)
        result = run.result
        assert result is not None
        self.assertEqual("completed", result.status)
        assert result.message is not None
        self.assertEqual("worker reply", result.message.content)
        self.assertEqual("stop", result.finish_reason)
        reported = result.calls[0].provider_reported_usage
        assert reported is not None
        self.assertEqual((5, 7), (reported.prompt_tokens, reported.completion_tokens))
        self.assertEqual(["work", "er reply"], chunks)

    def test_worker_failure_is_a_permanent_error(self) -> None:
        adapter = self.world.make_adapter()
        run = run_dispatch(adapter, make_context())
        execute = self.world.worker.next_execute()
        self.world.worker.fail(execute, "synthetic refusal")
        run.join(10)
        self.assertFalse(run.alive)
        self.assertIsInstance(run.error, AdapterPermanentError)

class AdapterDispatchRejectionTests(unittest.TestCase):
    world: AdapterWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.world = cast("AdapterWorld", object())

    @override
    def setUp(self) -> None:
        self.world = AdapterWorld()
        self.addCleanup(self.world.close)

    def test_missing_adapter_configuration_is_permanent_before_dispatch(self) -> None:
        adapter = WorkerBridgedAdapter(self.world.endpoint, resource_adapter_map={})
        with self.assertRaises(AdapterPermanentError):
            _ = adapter.execute(make_call(), make_context())
        # Nothing was ever sent to the worker.
        self.assertEqual(0, self.world.worker.received_execute_count)

    def test_worker_offline_is_permanent_before_dispatch(self) -> None:
        # A fresh endpoint with no connected worker: resource_unbound.
        endpoint = WorkerEndpoint(
            identity_store=self.world.store,
            registry=build_registry(with_worker=True),
            configured_owner=lambda _resource_id: None,
            heartbeat_interval_seconds=15,
        )
        adapter = WorkerBridgedAdapter(
            endpoint, resource_adapter_map={RESOURCE_ID: ADAPTER_ID}
        )
        with self.assertRaises(AdapterPermanentError):
            _ = adapter.execute(make_call(), make_context())


class AdapterAmbiguityTests(unittest.TestCase):
    world: AdapterWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.world = cast("AdapterWorld", object())

    @override
    def setUp(self) -> None:
        self.world = AdapterWorld()
        self.addCleanup(self.world.close)

    def test_disconnect_during_execution_is_ambiguous(self) -> None:
        adapter = self.world.make_adapter()
        run = run_dispatch(adapter, make_context())
        _ = self.world.worker.next_execute()
        # The network dies after the execute was delivered.
        self.world.worker.transport.close()
        run.join(10)
        self.assertFalse(run.alive)
        self.assertIsInstance(run.error, AdapterAmbiguousError)

    def test_no_duplicate_dispatch_on_reconnect(self) -> None:
        adapter = self.world.make_adapter()
        run = run_dispatch(adapter, make_context())
        execute = self.world.worker.next_execute()
        self.world.worker.transport.close()
        run.join(10)
        self.assertIsInstance(run.error, AdapterAmbiguousError)
        # A replacement connection reports the interrupted attempt; the
        # server must NOT send the execute again.
        replacement = self.world.reconnect()
        replacement.start_reader()
        replacement.send_interrupted(execute.attempt_id)
        self.assertTrue(
            wait_until(
                lambda: len(self.world.endpoint.interrupted_attempt_log()) >= 1
            )
        )
        self.assertEqual(1, len(self.world.endpoint.interrupted_attempt_log()))
        # One execute, total, across both connections.
        total = (
            self.world.worker.received_execute_count
            + replacement.received_execute_count
        )
        self.assertEqual(1, total)

    def test_deadline_exceeded_is_a_timeout(self) -> None:
        eval_clock = MutableClock()
        adapter = self.world.make_adapter(now=eval_clock, poll_seconds=0.01)
        context = make_context(deadline="2026-09-20T12:00:30.000Z")
        run = run_dispatch(adapter, context)
        _ = self.world.worker.next_execute()
        # The coordinator's clock moves past the admission deadline.
        eval_clock.advance(seconds=120)
        run.join(10)
        self.assertFalse(run.alive)
        self.assertIsInstance(run.error, AdapterTimeoutError)


class AdapterCancellationTests(unittest.TestCase):
    world: AdapterWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.world = cast("AdapterWorld", object())

    @override
    def setUp(self) -> None:
        self.world = AdapterWorld()
        self.addCleanup(self.world.close)

    def test_cancellation_propagates_and_reports_cancelled(self) -> None:
        adapter = self.world.make_adapter()
        cancel_event = threading.Event()
        context = make_context(cancel_event=cancel_event)
        worker = self.world.worker
        worker.start_reader()
        thread, captured = dispatch_in_thread(adapter, context)
        execute = worker.next_execute()
        # The client disconnects: the coordinator sets the cancel event.
        cancel_event.set()
        # The cancel notice reaches the worker...
        self.assertTrue(self.world.worker.wait_for_cancel())
        self.assertEqual([execute.attempt_id], self.world.worker.cancels)
        # ...and the worker reports the cancelled outcome honestly.
        worker.send_raw({
            "type": "execute_result",
            "attempt_id": execute.attempt_id,
            "status": "cancelled",
            "calls": [],
        })
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        outcome = captured.get("result")
        assert isinstance(outcome, AdapterResult)
        self.assertEqual("cancelled", outcome.status)


class CoordinatorIntegrationTests(unittest.TestCase):
    """GatewayApplication -> WorkerBridgedAdapter -> endpoint -> worker."""

    world: AdapterWorld
    adapter: WorkerBridgedAdapter
    application: GatewayApplication

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.world = cast("AdapterWorld", object())
        self.adapter = cast("WorkerBridgedAdapter", object())
        self.application = cast("GatewayApplication", object())

    @override
    def setUp(self) -> None:
        self.world = AdapterWorld()
        self.addCleanup(self.world.close)
        self.adapter = self.world.make_adapter()
        self.application = make_application(
            registry=build_registry(with_worker=True),
            cells=build_cells(include_worker=True),
            adapters=[self.adapter],
        )

    def _pinned_request(self) -> ChatCompletionRequest:
        from tests.gateway_fixtures import parse_chat_request

        request: ChatCompletionRequest = parse_chat_request({
            "model": f"sr-pin:{RESOURCE_ID}/openai/gpt-5.6-luna/max",
            "messages": [{"role": "user", "content": "hello"}],
        })
        return request

    def test_pinned_request_completes_through_the_worker(self) -> None:
        request = self._pinned_request()
        captured: dict[str, object] = {}

        def run() -> None:
            captured["outcome"] = self.application.execute(
                client_id="client-a",
                request=request,  # type: ignore[arg-type]
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        execute = self.world.worker.next_execute()
        self.world.worker.complete(execute, stream=False)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        outcome = captured["outcome"]
        assert isinstance(outcome, CompletionOutcomeContract)
        self.assertEqual("worker reply", outcome.message.content)
        self.assertEqual("stop", outcome.finish_reason)
        self.assertIsNotNone(outcome.usage.provider_reported_usage)

    def test_worker_disconnect_is_ambiguous_and_never_retried(self) -> None:
        request = self._pinned_request()
        errors: list[GatewayError] = []

        def run() -> None:
            try:
                _ = self.application.execute(
                    client_id="client-a",
                    request=request,  # type: ignore[arg-type]
                )
            except GatewayError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        _ = self.world.worker.next_execute()
        self.world.worker.transport.close()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(errors))
        self.assertEqual("ambiguous_execution_state", errors[0].code)
        # The audit trail records the ambiguous outcome exactly once.
        records = [r for r in audit_records(self.application) if r.selected_target]
        self.assertEqual(1, len(records))
        self.assertEqual("failed_ambiguous", records[0].result_status)
        # Exactly one execute was ever dispatched.
        self.assertEqual(1, self.world.worker.received_execute_count)

    def test_worker_rejection_is_backend_failure(self) -> None:
        request = self._pinned_request()
        errors: list[GatewayError] = []

        def run() -> None:
            try:
                _ = self.application.execute(
                    client_id="client-a",
                    request=request,  # type: ignore[arg-type]
                )
            except GatewayError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        execute = self.world.worker.next_execute()
        self.world.worker.fail(execute, "synthetic refusal")
        thread.join(timeout=15)
        self.assertEqual("backend_failure", errors[0].code)

    def test_client_disconnect_propagates_to_worker(self) -> None:
        from tests.gateway_fixtures import parse_chat_request

        request = parse_chat_request({
            "model": f"sr-pin:{RESOURCE_ID}/openai/gpt-5.6-luna/max",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })
        errors: list[BaseException] = []

        def emit(_chunk: AdapterStreamChunk) -> None:
            raise ClientDisconnectedError()

        def run() -> None:
            try:
                _ = self.application.execute(
                    client_id="client-a",
                    request=request,  # type: ignore[arg-type]
                    emit_chunk=emit,
                )
            except BaseException as exc:  # noqa: BLE001 - captured
                errors.append(exc)

        self.world.worker.start_reader()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        execute = self.world.worker.next_execute()
        # The worker streams its first chunk; the HTTP boundary then sees
        # the client is gone, which sets the cancel event through the
        # coordinator's emitter.
        self.world.worker.send_chunk(
            execute.attempt_id, {"kind": "text_delta", "text": "hel"}
        )
        self.assertTrue(self.world.worker.wait_for_cancel())
        self.assertEqual([execute.attempt_id], self.world.worker.cancels)
        self.world.worker.transport.close()
        thread.join(timeout=10)
        self.assertTrue(
            any(isinstance(e, ClientDisconnectedError) for e in errors),
            f"expected client disconnect, got {errors}",
        )


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


_ = PendingAttempt

if __name__ == "__main__":
    _ = unittest.main()
