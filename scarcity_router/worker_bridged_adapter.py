"""The ``worker_bridged`` execution adapter: coordinator → worker bridge (M05).

Registers into the M03 :class:`~scarcity_router.gateway_adapters.AdapterRegistry`
for the ``worker_bridged`` execution channel (D-042) and dispatches one
admitted :class:`~scarcity_router.gateway_adapters.AdapterCall` to the
connected worker through the worker protocol (issue #90). The adapter is a
translator and a courier — nothing else: it performs no routing, never
re-ranks, never substitutes a target and never retries.

Failure semantics are the honest ones D-043 requires:

- **Before anything is sent** (no bound worker, worker offline, no local
  adapter configured for the resource): a definitive
  :class:`~scarcity_router.gateway_adapters.AdapterPermanentError` — the
  coordinator fails the request closed; nothing was consumed.
- **After the execute frame was sent** (connection lost, delivery
  failure, the worker reported the attempt interrupted): an
  :class:`~scarcity_router.gateway_adapters.AdapterAmbiguousError` — the
  coordinator reports ``ambiguous_execution_state`` and nothing is ever
  re-dispatched. A disconnect during execution is an ambiguous outcome,
  never a blind re-execution: the request id + attempt id pair ensures a
  reconnecting worker can only ever *report* a lost attempt, and the
  server can only ever mark it ambiguous.
- **Deadline exceeded**: :class:`~scarcity_router.gateway_adapters.AdapterTimeoutError`
  (definitive for the caller); a cancellation notice is still delivered
  to the worker best-effort.
- **Cancellation** propagates: when the coordinator's cancel event is
  observed, the worker receives a ``cancel`` message for the attempt and
  the attempt's outcome is reported honestly (``cancelled``, or ambiguous
  if the connection died first).
- **Streaming chunks** flow through in arrival order via the context's
  emitter; usage observations ride the terminal result unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import cast

import secrets
import threading

from .gateway_adapters import (
    AdapterAmbiguousError,
    AdapterCall,
    AdapterMessage,
    AdapterPermanentError,
    AdapterResult,
    AdapterStreamChunk,
    AdapterTimeoutError,
    AdapterToolCall,
    CallObservation,
    CHUNK_FINISH,
    CHUNK_TOOL_CALL,
    ClientDisconnectedError,
    ContinuationLostError,
    ExecutionContext,
    FINISH_TOOL_CALLS,
    SuspensionHandle,
    ToolSuspension,
)

from .gateway_validation import v_canonical_ts, v_instance, v_safe_id
from .worker_endpoint import (
    AttemptOutcome,
    PendingAttempt,
    WorkerDispatchError,
    WorkerEndpoint,
    WorkerSession,
)
from .worker_protocol import (
    ExecuteChunkMessage,
    ExecuteMessage,
    ExecuteToolCallMessage,
    WorkerProtocolError,
    call_observation_from_dict,
    chunk_from_dict,
    message_from_dict,
)

ADAPTER_NAME = "worker-bridged"
ADAPTER_VERSION = "1.0.0"

# How often the dispatch loop wakes to observe cancellation/deadline while
# waiting for worker traffic (bounded polling, never a busy spin).
DISPATCH_POLL_SECONDS = 0.05

#: The bounded count of D-060 suspensions this adapter instance tracks at
#: once. Each suspension also holds one endpoint pending-attempt slot (the
#: tighter per-worker bound), so this only caps the shared table; a
#: suspension arriving at the bound fails the attempt honestly instead of
#: growing state without limit.
MAX_PENDING_SUSPENSIONS = 64

AttemptIdFactory = Callable[[], str]
NowFactory = Callable[[], datetime]


def default_attempt_id() -> str:
    """One fresh server-issued attempt id (safe-id grammar)."""
    return f"wa-{uuid.uuid4().hex[:20]}"


class WorkerBridgedAdapter:
    """The M03 execution adapter for the ``worker_bridged`` channel.

    ``resource_adapter_map`` is administrator configuration (composed
    from M09's resource ``local_adapter_id`` bindings by
    :mod:`scarcity_router.server_composition`): it binds each
    ``worker_bridged`` resource id to the worker-local adapter id that
    must execute it. A resource without a configured local adapter fails
    closed here, before anything is dispatched.
    """

    channel: str = "worker_bridged"
    adapter_name: str = ADAPTER_NAME
    adapter_version: str = ADAPTER_VERSION

    def __init__(
        self,
        endpoint: WorkerEndpoint,
        *,
        resource_adapter_map: Mapping[str, str],
        attempt_id_factory: AttemptIdFactory = default_attempt_id,
        now: NowFactory | None = None,
        poll_seconds: float = DISPATCH_POLL_SECONDS,
    ) -> None:
        self._endpoint: WorkerEndpoint = endpoint
        self._resource_adapter_map: dict[str, str] = {
            v_safe_id(resource_id, "resource_adapter_map key"): v_safe_id(
                adapter_id, "resource_adapter_map value"
            )
            for resource_id, adapter_id in resource_adapter_map.items()
        }
        self._attempt_id_factory: AttemptIdFactory = attempt_id_factory
        self._now: NowFactory = now if now is not None else _utcnow
        self._poll_seconds: float = poll_seconds
        # D-060: the one suspended-execution table of this adapter
        # instance (token -> handle). Guarded by its own lock: the
        # dispatch threads, the continuation path and the registry's
        # reaper callbacks all touch it.
        self._suspended_lock: threading.Lock = threading.Lock()
        self._suspended: dict[str, SuspensionHandle] = {}

    # ── The M03 adapter entry point ──────────────────────────────────

    def execute(self, call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        _ = v_instance(call, AdapterCall, "worker_bridged.call")
        _ = v_instance(context, ExecutionContext, "worker_bridged.context")
        resource_id = call.resource.resource_id
        adapter_id = self._resource_adapter_map.get(resource_id)
        if adapter_id is None:
            # Configuration gap, known before anything is sent: definitive.
            raise AdapterPermanentError(
                "no local adapter is configured for the selected resource"
            )
        attempt_id = v_safe_id(self._attempt_id_factory(), "worker_bridged.attempt_id")
        message = ExecuteMessage(
            request_id=context.request_id,
            attempt_id=attempt_id,
            adapter_id=adapter_id,
            deadline=v_canonical_ts(context.deadline, "worker_bridged.deadline"),
            call=call,
        )
        pending = self._begin(message, resource_id)
        return self._await_outcome(
            call=call, context=context, pending=pending, resource_id=resource_id
        )

    def _begin(self, message: ExecuteMessage, resource_id: str) -> PendingAttempt:
        """Deliver the execute message or fail with the honest error kind."""
        try:
            return self._endpoint.submit_execute_for_resource(message, resource_id)
        except WorkerDispatchError as exc:
            if exc.code == "worker_connection_lost":
                # The frame may have been (partially) delivered; the
                # attempt's fate is unknown -- ambiguous, never retried.
                raise AdapterAmbiguousError(
                    "the execute request could not be delivered deterministically"
                ) from None
            if exc.code == "duplicate_attempt":
                raise AdapterPermanentError(
                    "an attempt with this id is already in flight"
                ) from None
            raise AdapterPermanentError(
                "no connected worker can execute the selected resource"
            ) from None

    def _await_outcome(
        self,
        *,
        call: AdapterCall,
        context: ExecutionContext,
        pending: PendingAttempt,
        resource_id: str,
    ) -> AdapterResult:
        _ = call
        cancel_sent = False
        while True:
            if context.cancelled and not cancel_sent:
                pending.cancel_requested = True
                self._send_cancel_best_effort(pending.attempt_id, resource_id)
                cancel_sent = True
            kind, payload = pending.take(self._poll_seconds)
            if kind == "chunk":
                chunk = self._chunk_from(cast(object, payload))
                if context.cancelled:
                    return AdapterResult(status="cancelled", calls=())
                if context.emit_chunk is not None:
                    try:
                        context.emit_chunk(chunk)
                    except ClientDisconnectedError:
                        # The coordinator's emitter set the cancel event;
                        # tell the worker to stop, then propagate honestly.
                        if not cancel_sent:
                            pending.cancel_requested = True
                            self._send_cancel_best_effort(
                                pending.attempt_id, resource_id
                            )
                            cancel_sent = True
                        raise
                continue
            if kind == "suspension":
                # D-060: the worker's backend turn suspended on a
                # CLIENT-owned tool call. Everything queued before this
                # event belonged to the initial leg (already emitted);
                # the attempt stays tracked endpoint-side until the
                # turn's terminal result, and the harness continues the
                # turn through the continuation path. The external
                # tool_call id is an opaque server-issued token — the
                # backend's own call id never leaves this adapter.
                suspension = cast(ExecuteToolCallMessage, payload)
                handle = self._register_suspension(suspension, resource_id)
                if handle is None:
                    # The suspension table is at its bound: the attempt
                    # cannot be continued, so it fails honestly here
                    # (the worker's own deadline stops the backend turn).
                    self._send_cancel_best_effort(pending.attempt_id, resource_id)
                    raise AdapterPermanentError(
                        "the gateway is at its pending client-tool bound"
                    )
                tool_call = AdapterToolCall(
                    id=handle.continuation_token,
                    name=suspension.name,
                    arguments=suspension.arguments,
                )
                if context.emit_chunk is not None and not context.cancelled:
                    # The streamed initial leg carries the complete
                    # tool_call and the explicit tool_calls finish frame
                    # (the stable non-tool path keeps its recorded
                    # no-finish-frame shape).
                    context.emit_chunk(
                        AdapterStreamChunk(kind=CHUNK_TOOL_CALL, tool_call=tool_call)
                    )
                    context.emit_chunk(
                        AdapterStreamChunk(
                            kind=CHUNK_FINISH, finish_reason=FINISH_TOOL_CALLS
                        )
                    )
                message = AdapterMessage(
                    role="assistant",
                    content=suspension.content,
                    tool_calls=(tool_call,),
                )
                return AdapterResult(
                    status="completed",
                    # The provider call is OPEN, not finished: an
                    # ``unknown`` observation with no usage marks the
                    # suspended call honestly (D-043: a completed record
                    # carries at least one call) while the turn's single
                    # usage-bearing observation arrives only on the
                    # terminal continuation record — nothing is
                    # double-counted.
                    calls=(
                        CallObservation(
                            call_index=0,
                            started_at=_utcnow().isoformat(
                                timespec="milliseconds"
                            ).replace("+00:00", "Z"),
                            ended_at=_utcnow().isoformat(
                                timespec="milliseconds"
                            ).replace("+00:00", "Z"),
                            status="unknown",
                            note=(
                                "the backend turn is suspended for a "
                                + "client-owned tool call"
                            ),
                        ),
                    ),
                    message=message,
                    finish_reason=FINISH_TOOL_CALLS,
                )
            if kind == "outcome":
                outcome = cast(AttemptOutcome, payload)
                if outcome.status == "interrupted":
                    raise AdapterAmbiguousError(
                        "the worker connection was lost during execution; the "
                        + "attempt's outcome is unknown and was not retried"
                    )
                return self._result_from(outcome)
            # kind == "timeout": observe the deadline, then keep waiting.
            if self._now() >= _parse_deadline(context.deadline):
                self._send_cancel_best_effort(pending.attempt_id, resource_id)
                raise AdapterTimeoutError(
                    "the worker-bridged execution exceeded its time limit"
                )

    # ── D-060 continuation surface (ContinuationCapableAdapter) ──────

    def _register_suspension(
        self, suspension: ExecuteToolCallMessage, resource_id: str
    ) -> SuspensionHandle | None:
        token = "srct-" + secrets.token_hex(16)
        handle = SuspensionHandle(
            continuation_token=token,
            attempt_id=suspension.attempt_id,
            resource_id=resource_id,
            call_id=suspension.call_id,
            tool_name=suspension.name,
        )
        with self._suspended_lock:
            if len(self._suspended) >= MAX_PENDING_SUSPENSIONS:
                return None
            self._suspended[token] = handle
        return handle

    def _drop_suspension(self, token: str) -> None:
        with self._suspended_lock:
            _ = self._suspended.pop(token, None)

    def suspension_handle(self, continuation_token: str) -> SuspensionHandle | None:
        """The handle for a suspension this adapter issued, or ``None``."""
        with self._suspended_lock:
            return self._suspended.get(continuation_token)

    def suspension_alive(self, handle: SuspensionHandle) -> bool:
        """Whether the suspended attempt is still tracked endpoint-side.

        A closed worker session (or a worker that already reported the
        attempt terminal) resolves the tracker — the continuation is
        honestly dead and nothing can resume it.
        """
        try:
            session = self._endpoint.session_for_resource(handle.resource_id)
        except WorkerDispatchError:
            return False
        return session.pending_attempt(handle.attempt_id) is not None

    def cancel_suspension(self, handle: SuspensionHandle) -> None:
        """Stop one suspended turn best-effort (registry expiry/cancel)."""
        self._drop_suspension(handle.continuation_token)
        self._send_cancel_best_effort(handle.attempt_id, handle.resource_id)

    def deliver_tool_result(
        self,
        handle: SuspensionHandle,
        content: str,
        context: ExecutionContext,
    ) -> "AdapterResult | ToolSuspension":
        """Resume the EXACT suspended turn with one harness tool result.

        Delivers the result into the same attempt and waits for that
        turn's terminal outcome (or a further sequential suspension).
        Loss BEFORE delivery raises :class:`ContinuationLostError`
        (nothing consumed the result); loss AFTER it raises
        :class:`AdapterAmbiguousError`. There is no retry, no
        reconstruction and no fallback — D-060's sticky-exact discipline.
        """
        self._v_context(context)
        session = self._session_for(handle)
        pending = session.pending_attempt(handle.attempt_id)
        if pending is None:
            # The attempt already resolved (worker terminal/interrupt
            # report, session loss, expiry): the result was never
            # delivered and nothing was consumed.
            self._drop_suspension(handle.continuation_token)
            raise ContinuationLostError(
                "the suspended execution is no longer tracked by its worker"
            )
        delivery = session.send_tool_result(
            handle.attempt_id, handle.call_id, content
        )
        if delivery == WorkerSession.TOOL_RESULT_NOT_SENT:
            self._drop_suspension(handle.continuation_token)
            raise ContinuationLostError(
                "the worker session closed before the tool result was delivered"
            )
        if delivery == WorkerSession.TOOL_RESULT_AMBIGUOUS:
            self._drop_suspension(handle.continuation_token)
            raise AdapterAmbiguousError(
                "the tool result could not be delivered deterministically; "
                + "the suspended execution may have consumed it"
            )
        return self._await_resume(handle, pending, context)

    def _await_resume(
        self,
        handle: SuspensionHandle,
        pending: PendingAttempt,
        context: ExecutionContext,
    ) -> "AdapterResult | ToolSuspension":
        cancel_sent = False
        while True:
            if context.cancelled and not cancel_sent:
                pending.cancel_requested = True
                self._send_cancel_best_effort(handle.attempt_id, handle.resource_id)
                cancel_sent = True
            kind, payload = pending.take(self._poll_seconds)
            if kind == "chunk":
                chunk = self._chunk_from(cast(object, payload))
                if context.cancelled:
                    self._drop_suspension(handle.continuation_token)
                    return AdapterResult(status="cancelled", calls=())
                if context.emit_chunk is not None:
                    try:
                        context.emit_chunk(chunk)
                    except ClientDisconnectedError:
                        if not cancel_sent:
                            pending.cancel_requested = True
                            self._send_cancel_best_effort(
                                handle.attempt_id, handle.resource_id
                            )
                            cancel_sent = True
                        raise
                continue
            if kind == "suspension":
                # A sequential tool round on the SAME turn: end this leg
                # with the new suspension (the caller registers the next
                # continuation exactly as for the initial leg).
                suspension = cast(ExecuteToolCallMessage, payload)
                next_handle = self._register_suspension(
                    suspension, handle.resource_id
                )
                self._drop_suspension(handle.continuation_token)
                if next_handle is None:
                    self._send_cancel_best_effort(
                        handle.attempt_id, handle.resource_id
                    )
                    raise AdapterPermanentError(
                        "the gateway is at its pending client-tool bound"
                    )
                if context.emit_chunk is not None and not context.cancelled:
                    context.emit_chunk(
                        AdapterStreamChunk(
                            kind=CHUNK_TOOL_CALL,
                            tool_call=AdapterToolCall(
                                id=next_handle.continuation_token,
                                name=suspension.name,
                                arguments=suspension.arguments,
                            ),
                        )
                    )
                    context.emit_chunk(
                        AdapterStreamChunk(
                            kind=CHUNK_FINISH, finish_reason=FINISH_TOOL_CALLS
                        )
                    )
                return ToolSuspension(
                    continuation_token=next_handle.continuation_token,
                    tool_name=suspension.name,
                    arguments=suspension.arguments,
                    content=suspension.content,
                )
            if kind == "outcome":
                self._drop_suspension(handle.continuation_token)
                outcome = cast(AttemptOutcome, payload)
                if outcome.status == "interrupted":
                    # Lost AFTER delivery: the result may have been
                    # consumed; honest ambiguity, never a re-execution.
                    raise AdapterAmbiguousError(
                        "the worker connection was lost after the tool result "
                        + "was delivered; the outcome is unknown and was not "
                        + "retried"
                    )
                return self._result_from(outcome)
            if self._now() >= _parse_deadline(context.deadline):
                self._drop_suspension(handle.continuation_token)
                self._send_cancel_best_effort(handle.attempt_id, handle.resource_id)
                raise AdapterTimeoutError(
                    "the continued execution exceeded its time limit"
                )

    def _v_context(self, context: ExecutionContext) -> None:
        _ = v_instance(context, ExecutionContext, "worker_bridged.context")

    def _session_for(self, handle: SuspensionHandle) -> WorkerSession:
        try:
            return self._endpoint.session_for_resource(handle.resource_id)
        except WorkerDispatchError as exc:
            raise ContinuationLostError(
                "no worker session can deliver the tool result for the "
                + "suspended execution"
            ) from exc

    def _chunk_from(self, payload: object) -> AdapterStreamChunk:
        if not isinstance(payload, ExecuteChunkMessage):
            raise AdapterPermanentError("the worker streamed an invalid chunk")
        try:
            return chunk_from_dict(dict(payload.chunk))
        except WorkerProtocolError as exc:
            raise AdapterPermanentError(
                "the worker streamed an invalid chunk"
            ) from exc

    def _result_from(self, outcome: AttemptOutcome) -> AdapterResult:
        result = outcome.result
        if result is None:  # pragma: no cover - resolve() contract
            raise AdapterPermanentError("the worker reported an unusable result")
        calls = _calls_from(result.calls)
        if result.status == "failed":
            raise AdapterPermanentError(outcome_note(outcome))
        if result.status == "cancelled":
            return AdapterResult(status="cancelled", calls=calls)
        # status == "completed": a completed result carries message + reason.
        try:
            message = (
                message_from_dict(dict(result.message))
                if result.message is not None
                else None
            )
        except WorkerProtocolError as exc:
            raise AdapterPermanentError(
                "the worker reported an invalid execution result"
            ) from exc
        finish_reason = result.finish_reason
        if message is None or finish_reason is None:
            raise AdapterPermanentError(
                "the worker reported an incomplete execution result"
            )
        return AdapterResult(
            status="completed",
            calls=calls,
            message=message,
            finish_reason=finish_reason,
        )

    def _send_cancel_best_effort(self, attempt_id: str, resource_id: str) -> None:
        """Deliver a cancel notice to the bound worker; never raise."""
        try:
            session = self._endpoint.session_for_resource(resource_id)
        except WorkerDispatchError:
            return
        session.send_cancel(attempt_id)


def outcome_note(outcome: AttemptOutcome) -> str:
    """A safe one-line note from a terminal worker outcome."""
    if outcome.note:
        return outcome.note[:200]
    if outcome.result is not None and outcome.result.note:
        return outcome.result.note[:200]
    return "the worker-reported execution failed"


def _calls_from(raw_calls: tuple[Mapping[str, object], ...]) -> tuple[CallObservation, ...]:
    observations: list[CallObservation] = []
    for raw in raw_calls:
        try:
            observations.append(call_observation_from_dict(dict(raw)))
        except WorkerProtocolError as exc:
            raise AdapterPermanentError(
                "the worker reported an invalid usage observation"
            ) from exc
    return tuple(observations)


def _parse_deadline(deadline: str) -> datetime:
    _ = v_canonical_ts(deadline, "worker_bridged.deadline")
    return datetime.fromisoformat(deadline[:-1] + "+00:00")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "DISPATCH_POLL_SECONDS",
    "MAX_PENDING_SUSPENSIONS",
    "AttemptIdFactory",
    "WorkerBridgedAdapter",
    "default_attempt_id",
]
