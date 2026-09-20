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

from .gateway_adapters import (
    AdapterAmbiguousError,
    AdapterCall,
    AdapterPermanentError,
    AdapterResult,
    AdapterStreamChunk,
    AdapterTimeoutError,
    CallObservation,
    ClientDisconnectedError,
    ExecutionContext,
)
from .gateway_validation import v_canonical_ts, v_instance, v_safe_id
from .worker_endpoint import AttemptOutcome, PendingAttempt, WorkerDispatchError, WorkerEndpoint
from .worker_protocol import (
    ExecuteChunkMessage,
    ExecuteMessage,
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

AttemptIdFactory = Callable[[], str]
NowFactory = Callable[[], datetime]


def default_attempt_id() -> str:
    """One fresh server-issued attempt id (safe-id grammar)."""
    return f"wa-{uuid.uuid4().hex[:20]}"


class WorkerBridgedAdapter:
    """The M03 execution adapter for the ``worker_bridged`` channel.

    ``resource_adapter_map`` is administrator configuration (M09 owns its
    UX): it binds each ``worker_bridged`` resource id to the worker-local
    adapter id that must execute it. A resource without a configured local
    adapter fails closed here, before anything is dispatched.
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
    "AttemptIdFactory",
    "WorkerBridgedAdapter",
    "default_attempt_id",
]
