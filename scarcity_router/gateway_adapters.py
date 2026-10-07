"""Execution-adapter seam: the dispatch boundary of the coordinator (M03).

The coordinator owns the execution lifecycle; execution adapters own the
concrete execution surfaces. Per D-041/D-003 provider differences stop at
the adapter edge: the coordinator never parses provider payloads and never
speaks a provider protocol. This module defines the seam that M04 (generic
OpenAI-compatible HTTP adapter and Ollama), M05 (worker transport) and M06
(Codex adapter) implement — and nothing else about them.

Seam contract:

- :class:`ExecutionAdapter` — the protocol every adapter implements. It
  declares the execution channel it serves (a closed D-042 member of
  ``EXECUTION_CHANNELS``), its stable name and version (both recorded in
  the audit trail and in the compatibility matrix's adapter columns), and
  one synchronous ``execute`` method.
- :class:`AdapterCall` — everything one dispatch consumes: the EXACT
  admitted target (resource identity + physical model identity, never
  re-chosen), the client messages in their validated normalized form (the
  coordinator does not interpret message content), the tools, the
  structured-output specification, the reasoning controls, the output
  token ceiling and the normalized generation parameters.
- :class:`ExecutionContext` — the per-dispatch bounds: the request id, the
  absolute deadline computed from the admission limits, the cancellation
  event and the streaming emitter. Adapters are REQUIRED to honor the
  deadline and to observe the cancellation event (client disconnect
  propagation, D-043); the coordinator reports each channel's
  cancellation support honestly — support is a compatibility-matrix fact,
  never an adapter attribute.
- :class:`AdapterResult` and :class:`CallObservation` — the outcome: a
  normalized assistant message, a normalized finish reason, and one
  observation per internal provider call with its usage. One external
  request may legitimately produce several internal calls; the observation
  list is how multi-call fan-out stays honest (D-043). Provider-reported
  and estimated usage stay distinguishable per call.
- :class:`AdapterStreamChunk` — the normalized streaming vocabulary
  (text deltas, complete tool calls, finish, usage). Chunk granularity is
  the adapter's business; the coordinator never splits or merges provider
  frames.
- :class:`AdapterPermanentError` / :class:`AdapterAmbiguousError` /
  :class:`AdapterTimeoutError` — the typed failure vocabulary. A permanent
  error is a definitive failure of this attempt; an ambiguous error means
  the adapter cannot say whether the backend consumed the request — the
  coordinator then refuses to retry blindly (no duplicate inference
  consumption, D-043) and reports the ambiguity. A timeout is definitive
  for the caller and recorded as ``timed_out``.

There is deliberately NO production adapter in this module: registering a
fake adapter as a supported surface would misrepresent capability. The
deterministic synthetic adapter used by the test suite lives with the
tests.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .gateway_contracts import UsageAccounting, UsageTokens
from .gateway_validation import (
    v_bool,
    v_canonical_ts,
    v_enum,
    v_instance,
    v_int,
    v_safe_id,
    v_str,
    v_str_object_mapping,
    v_text,
)
from .resource_state import EXECUTION_CHANNELS, ResourceIdentity
from .selection_types import ModelIdentity

# ── Frozen value sets ─────────────────────────────────────────────────────────

# Normalized finish reasons (the OpenAI-compatible subset the surface v1
# supports; ``cancelled`` reports a client-initiated or deadline stop).
FINISH_STOP = "stop"
FINISH_TOOL_CALLS = "tool_calls"
FINISH_LENGTH = "length"
FINISH_CANCELLED = "cancelled"

FINISH_REASONS: frozenset[str] = frozenset({
    FINISH_STOP,
    FINISH_TOOL_CALLS,
    FINISH_LENGTH,
    FINISH_CANCELLED,
})

# Closed per-call observation status.
CALL_COMPLETED = "completed"
CALL_FAILED = "failed"
CALL_CANCELLED = "cancelled"
CALL_UNKNOWN = "unknown"

CALL_STATUSES: frozenset[str] = frozenset({
    CALL_COMPLETED,
    CALL_FAILED,
    CALL_CANCELLED,
    CALL_UNKNOWN,
})

# Closed normalized stream-chunk kinds.
CHUNK_TEXT_DELTA = "text_delta"
CHUNK_REASONING_DELTA = "reasoning_delta"
CHUNK_TOOL_CALL = "tool_call"
CHUNK_FINISH = "finish"
CHUNK_USAGE = "usage"

CHUNK_KINDS: frozenset[str] = frozenset({
    CHUNK_TEXT_DELTA,
    CHUNK_REASONING_DELTA,
    CHUNK_TOOL_CALL,
    CHUNK_FINISH,
    CHUNK_USAGE,
})

# Closed adapter-result status values.
RESULT_STATUSES: frozenset[str] = frozenset({
    CALL_COMPLETED,
    CALL_CANCELLED,
    CALL_FAILED,
})


# ── Dispatch inputs ───────────────────────────────────────────────────────────


def _empty_mapping() -> Mapping[str, object]:
    return {}


@dataclass(frozen=True)
class AdapterToolCall:
    """One tool call carried in a conversation message (client-side tools).

    Client-supplied tools always return to the CLIENT as ``tool_calls``
    (D-043): the router never executes them. ``arguments`` is the JSON
    arguments TEXT exactly as the conversation carries it — opaque to the
    coordinator, meaningful only to the client that issued the tool call.
    """

    id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        _ = v_text(self.id, "adapter_tool_call.id", max_len=256)
        _ = v_text(self.name, "adapter_tool_call.name", max_len=256)
        _ = v_text(self.arguments, "adapter_tool_call.arguments", max_len=1_048_576)


@dataclass(frozen=True)
class AdapterMessage:
    """One client conversation message (opaque, validated structure only).

    The coordinator validates the message envelope (roles, text content,
    tool-call ids) at the request boundary and passes the validated values
    through unchanged. Adapter implementations translate them into their
    backend's wire format; the coordinator never reads their content.

    ``reasoning`` is the provider's opaque reasoning output on an assistant
    message (issue #158): translated per the preset's evidenced response
    shape, transported verbatim, and rendered to the client under the
    documented additive semantics. It is never parsed, never used for
    routing and never merged into ``content``. The request-history wire
    builder refuses to forward it — no preset evidences reasoning
    re-injection, so a request carrying it fails rather than silently
    drops it.
    """

    role: str
    content: str | None = None
    tool_calls: tuple[AdapterToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None

    def __post_init__(self) -> None:
        _ = v_safe_id(self.role, "adapter_message.role")
        if self.content is not None:
            content = v_str(self.content, "adapter_message.content")
            if len(content) > 16_777_216:
                raise ValueError(
                    "adapter_message.content: exceeds maximum length 16777216"
                )
        if self.reasoning is not None:
            reasoning = v_str(self.reasoning, "adapter_message.reasoning")
            if len(reasoning) > 16_777_216:
                raise ValueError(
                    "adapter_message.reasoning: exceeds maximum length 16777216"
                )
        if self.tool_call_id is not None:
            _ = v_text(self.tool_call_id, "adapter_message.tool_call_id", max_len=256)
        if self.name is not None:
            _ = v_text(self.name, "adapter_message.name", max_len=256)
        for call in self.tool_calls:
            _ = v_instance(call, AdapterToolCall, "adapter_message.tool_calls")


@dataclass(frozen=True)
class AdapterCall:
    """Everything one dispatch consumes (the exact admitted target)."""

    resource: ResourceIdentity
    model: ModelIdentity
    messages: tuple[AdapterMessage, ...]
    stream: bool = False
    tools: tuple[Mapping[str, object], ...] = ()
    tool_choice: object = None
    response_format: Mapping[str, object] | None = None
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    generation_params: Mapping[str, object] = field(default_factory=_empty_mapping)

    def __post_init__(self) -> None:
        _ = v_instance(self.resource, ResourceIdentity, "adapter_call.resource")
        _ = v_instance(self.model, ModelIdentity, "adapter_call.model")
        _ = v_bool(self.stream, "adapter_call.stream")
        for message in self.messages:
            _ = v_instance(message, AdapterMessage, "adapter_call.messages")
        for tool in self.tools:
            _ = v_str_object_mapping(tool, "adapter_call.tools")
        if self.response_format is not None:
            _ = v_str_object_mapping(
                self.response_format, "adapter_call.response_format"
            )
        if self.reasoning_effort is not None:
            _ = v_safe_id(self.reasoning_effort, "adapter_call.reasoning_effort")
        if self.max_output_tokens is not None:
            _ = v_int(self.max_output_tokens, "adapter_call.max_output_tokens", lo=1)
        _ = v_str_object_mapping(
            self.generation_params, "adapter_call.generation_params"
        )


@dataclass(frozen=True)
class ExecutionContext:
    """The per-dispatch bounds every adapter must honor (D-043/D-044).

    ``deadline`` is the absolute canonical timestamp computed from the
    admission limits; ``cancel_event`` is set when the client disconnects
    or the coordinator otherwise stops the execution. Adapters observe the
    event cooperatively (they run on the caller's thread) and return an
    ``AdapterResult`` with status ``cancelled`` or raise
    :class:`AdapterTimeoutError` — they never hang past the deadline.

    ``emit_chunk`` is the streaming emitter: exactly one normalized
    :class:`AdapterStreamChunk` per call, in order, only while the
    dispatch is streaming. When the client connection drops, the emitter
    raises :class:`ClientDisconnectedError` through the adapter; the
    adapter lets it propagate after observing the cancel event (cleanup is
    the adapter's responsibility, duplicated inference is the adapter's
    risk to avoid — the coordinator never re-dispatches). ``emit_chunk``
    is ``None`` for non-streaming dispatches; an adapter must not call it
    then.
    """

    request_id: str
    deadline: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    emit_chunk: Callable[["AdapterStreamChunk"], None] | None = None
    #: D-062 continuation registration seam (review round 2, finding 4).
    #: A continuation-capable adapter MUST call this with the suspension
    #: handle BEFORE the tool_call id becomes observable (before a
    #: ``tool_call`` chunk is emitted and before a suspension result is
    #: returned): the invariant is "if the client can observe the token,
    #: it already names a live registered continuation". ``False`` means
    #: the gateway could not register (pending bound) — the adapter then
    #: cancels the suspended turn and fails the attempt typed, exposing
    #: nothing. ``None`` means this dispatch has no continuation surface
    #: (same bounded failure).
    register_continuation: Callable[["SuspensionHandle", str], bool] | None = None

    def __post_init__(self) -> None:
        _ = v_safe_id(self.request_id, "execution_context.request_id")
        _ = v_canonical_ts(self.deadline, "execution_context.deadline")

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


# ── Dispatch outputs ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CallObservation:
    """One internal provider call's honest observation (D-043).

    A multi-call fan-out produces one observation per call, in call order.
    ``provider_reported_usage`` carries backend-reported token counts;
    ``estimated_usage`` carries the adapter's own estimate — both may be
    absent (usage reporting may be unevidenced), and they are never merged
    into one indistinguishable number. ``note`` is a short safe diagnostic
    (no payload excerpts, no request content).
    """

    call_index: int
    started_at: str
    ended_at: str
    status: str
    provider_reported_usage: UsageTokens | None = None
    estimated_usage: UsageTokens | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        _ = v_int(self.call_index, "call_observation.call_index", lo=0)
        _ = v_canonical_ts(self.started_at, "call_observation.started_at")
        _ = v_canonical_ts(self.ended_at, "call_observation.ended_at")
        _ = v_enum(self.status, CALL_STATUSES, "call_observation.status")
        if self.provider_reported_usage is not None:
            _ = v_instance(
                self.provider_reported_usage,
                UsageTokens,
                "call_observation.provider_reported_usage",
            )
        if self.estimated_usage is not None:
            _ = v_instance(
                self.estimated_usage, UsageTokens, "call_observation.estimated_usage"
            )
        if self.note is not None:
            _ = v_text(self.note, "call_observation.note", max_len=200)

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "call_index": self.call_index,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
        }
        if self.provider_reported_usage is not None:
            out["provider_reported_usage"] = self.provider_reported_usage.to_dict()
        if self.estimated_usage is not None:
            out["estimated_usage"] = self.estimated_usage.to_dict()
        if self.note is not None:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class AdapterStreamChunk:
    """One normalized stream chunk emitted by an adapter.

    ``text_delta`` carries incremental assistant text; ``reasoning_delta``
    carries incremental reasoning text (issue #158 — a distinct kind, so
    reasoning is never merged into normal content); ``tool_call`` carries
    one complete tool call (the coordinator never fragments or reassembles
    tool calls); ``finish`` carries the terminal finish reason (exactly
    once, last content chunk); ``usage`` carries aggregated token usage
    for the whole response. The OpenAI wire mapping renders these into
    ``chat.completion.chunk`` SSE frames.
    """

    kind: str
    text: str | None = None
    tool_call: AdapterToolCall | None = None
    finish_reason: str | None = None
    usage: UsageTokens | None = None

    def __post_init__(self) -> None:
        _ = v_enum(self.kind, CHUNK_KINDS, "adapter_stream_chunk.kind")
        if self.kind == CHUNK_TEXT_DELTA:
            _ = v_text(self.text, "adapter_stream_chunk.text", max_len=1_048_576)
        elif self.kind == CHUNK_REASONING_DELTA:
            _ = v_text(self.text, "adapter_stream_chunk.text", max_len=1_048_576)
        elif self.kind == CHUNK_TOOL_CALL:
            _ = v_instance(
                self.tool_call, AdapterToolCall, "adapter_stream_chunk.tool_call"
            )
        elif self.kind == CHUNK_FINISH:
            _ = v_enum(
                self.finish_reason, FINISH_REASONS, "adapter_stream_chunk.finish_reason"
            )
        elif self.kind == CHUNK_USAGE:
            _ = v_instance(self.usage, UsageTokens, "adapter_stream_chunk.usage")
        if (
            self.text is not None
            and self.kind not in (CHUNK_TEXT_DELTA, CHUNK_REASONING_DELTA)
        ):
            raise ValueError(
                "adapter_stream_chunk: text is only valid on a text_delta "
                + "or reasoning_delta chunk"
            )
        if self.tool_call is not None and self.kind != CHUNK_TOOL_CALL:
            raise ValueError(
                "adapter_stream_chunk: tool_call is only valid on a tool_call chunk"
            )
        if self.finish_reason is not None and self.kind != CHUNK_FINISH:
            raise ValueError(
                "adapter_stream_chunk: finish_reason is only valid on a finish chunk"
            )
        if self.usage is not None and self.kind != CHUNK_USAGE:
            raise ValueError(
                "adapter_stream_chunk: usage is only valid on a usage chunk"
            )


@dataclass(frozen=True)
class AdapterResult:
    """The outcome of one dispatch (normalized, provider-free)."""

    status: str
    calls: tuple[CallObservation, ...]
    message: AdapterMessage | None = None
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        _ = v_enum(self.status, RESULT_STATUSES, "adapter_result.status")
        ordered = tuple(sorted(self.calls, key=lambda call: call.call_index))
        if ordered != self.calls:
            raise ValueError("adapter_result.calls: must be ordered by call_index")
        seen: set[int] = set()
        for call in self.calls:
            _ = v_instance(call, CallObservation, "adapter_result.calls")
            if call.call_index in seen:
                raise ValueError("adapter_result.calls: duplicate call_index")
            seen.add(call.call_index)
        if self.status == CALL_COMPLETED and self.message is None:
            raise ValueError(
                "adapter_result: a completed result carries its message"
            )
        if self.finish_reason is not None:
            _ = v_enum(
                self.finish_reason, FINISH_REASONS, "adapter_result.finish_reason"
            )
        if self.status == CALL_COMPLETED and self.finish_reason is None:
            raise ValueError(
                "adapter_result: a completed result carries a finish reason"
            )


@dataclass(frozen=True)
class CompletionOutcome:
    """The coordinator's success outcome for one completed execution.

    The OpenAI-compatible wire mapping renders this into the
    ``chat.completion`` response object. ``model_echo`` is the client's
    original ``model`` string (alias or pinned reference) — echoing what
    the client asked for, never silently naming a different backend. The
    ``x_scarcity_router`` extension fields (request id, decision id, usage
    source) let clients correlate responses with the audit trail; the
    usage accounting keeps provider-reported and estimated numbers
    distinguishable (D-043).
    """

    request_id: str
    created: int
    model_echo: str
    message: AdapterMessage
    finish_reason: str
    usage: UsageAccounting
    decision_id: str | None = None

    def __post_init__(self) -> None:
        _ = v_safe_id(self.request_id, "completion_outcome.request_id")
        _ = v_int(self.created, "completion_outcome.created", lo=0)
        _ = v_text(self.model_echo, "completion_outcome.model_echo", max_len=512)
        _ = v_instance(self.message, AdapterMessage, "completion_outcome.message")
        _ = v_enum(
            self.finish_reason, FINISH_REASONS, "completion_outcome.finish_reason"
        )
        _ = v_instance(self.usage, UsageAccounting, "completion_outcome.usage")
        if self.decision_id is not None:
            _ = v_safe_id(self.decision_id, "completion_outcome.decision_id")


# ── The adapter protocol and registry ─────────────────────────────────────────


@runtime_checkable
class ExecutionAdapter(Protocol):
    """The dispatch seam implemented by M04/M05/M06 adapters.

    ``channel`` is the D-042 execution channel this adapter serves (the
    registry keys adapters by it); ``adapter_name``/``adapter_version``
    identify the adapter for the audit trail and for compatibility-matrix
    evidence columns. ``execute`` is synchronous, runs on the caller's
    thread, must honor ``context.deadline``, must observe
    ``context.cancel_event``, and — for streaming dispatches — must emit
    normalized :class:`AdapterStreamChunk` values through
    ``context.emit_chunk`` in order.
    """

    channel: str
    adapter_name: str
    adapter_version: str

    def execute(self, call: AdapterCall, context: ExecutionContext) -> AdapterResult: ...


class AdapterRegistry:
    """Channel-keyed adapter registry (one adapter per execution channel).

    The coordinator resolves the admitted target's channel through this
    registry and dispatches; it never instantiates or configures adapters
    itself and never falls back to another channel. Registering nothing
    for a channel is an honest "no adapter configured" state that fails
    explicitly at dispatch.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ExecutionAdapter] = {}

    def register(self, adapter: ExecutionAdapter) -> None:
        channel = getattr(adapter, "channel", None)
        _ = v_enum(channel, EXECUTION_CHANNELS, "adapter_registry.adapter channel")
        assert isinstance(channel, str)
        _ = v_safe_id(
            getattr(adapter, "adapter_name", None), "adapter_registry.adapter_name"
        )
        _ = v_text(
            getattr(adapter, "adapter_version", None),
            "adapter_registry.adapter_version",
            max_len=128,
        )
        if channel in self._adapters:
            raise ValueError(
                f"adapter_registry: an adapter for channel {channel!r} is "
                + "already registered"
            )
        self._adapters[channel] = adapter

    def resolve(self, channel: str) -> ExecutionAdapter | None:
        _ = v_enum(channel, EXECUTION_CHANNELS, "adapter_registry.channel")
        return self._adapters.get(channel)

    def registered_channels(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


class AdapterPermanentError(Exception):
    """A definitive dispatch failure of this attempt (no ambiguity).

    The backend definitively refused or failed before producing a
    consumable response. The coordinator fails the request closed with an
    explicit error and NEVER re-routes or retries on another target
    (D-043).
    """


class AdapterAmbiguousError(Exception):
    """The adapter cannot tell whether the backend consumed the request.

    Retrying blindly could duplicate inference consumption, so the
    coordinator refuses to retry (D-043: exactly-once is not promised, but
    no blind duplication either), records the ambiguous outcome in the
    audit trail and reports an explicit error to the client.
    """


class AdapterTimeoutError(Exception):
    """The dispatch exceeded its deadline (definitive for the caller).

    Recorded as ``timed_out``; the adapter is responsible for propagating
    the stop to its backend where the channel supports cancellation.
    """


class ClientDisconnectedError(Exception):
    """The client connection dropped while the execution was running.

    Raised by the streaming emitter at the HTTP boundary; the coordinator
    treats it as cancellation (D-043): it sets the context's cancel event,
    lets the adapter unwind cooperatively and records the request as
    ``cancelled``.
    """


class ContinuationLostError(Exception):
    """The suspended execution is gone before its tool result reached it.

    The D-062 suspension was lost (worker disconnect, App Server process
    death, gateway restart, expiry) BEFORE the harness's tool result was
    delivered, so nothing consumed it and nothing can be duplicated: the
    coordinator fails the continuation closed with a typed
    not-found/lost error and never reconstructs or re-executes (D-062).
    A loss AFTER delivery raises :class:`AdapterAmbiguousError` instead —
    the result may have been consumed.
    """


@dataclass(frozen=True)
class SuspensionHandle:
    """The adapter-side identity of one suspended backend execution.

    Held by the continuation-capable adapter and referenced through the
    registry record; ``continuation_token`` is the opaque server-issued
    id the harness sees as the ``tool_call_id`` — every other member is
    internal and never client-visible.
    """

    continuation_token: str
    attempt_id: str
    resource_id: str
    call_id: str
    tool_name: str

    def __post_init__(self) -> None:
        _ = v_safe_id(self.attempt_id, "suspension_handle.attempt_id")
        _ = v_safe_id(self.resource_id, "suspension_handle.resource_id")
        _ = v_text(self.continuation_token, "suspension_handle.token", max_len=256)
        _ = v_text(self.call_id, "suspension_handle.call_id", max_len=256)
        _ = v_text(self.tool_name, "suspension_handle.tool_name", max_len=256)


@dataclass(frozen=True)
class ToolSuspension:
    """A NEW client-tool suspension produced during a continuation leg.

    Sequential tool rounds (the model asks for a further tool after the
    first result resumed the same turn) surface as this outcome: the
    caller ends the current HTTP leg with the new ``tool_calls`` response
    and registers the next continuation, exactly as for the initial leg.
    """

    continuation_token: str
    tool_name: str
    arguments: str
    content: str | None = None

    def __post_init__(self) -> None:
        _ = v_text(self.continuation_token, "tool_suspension.token", max_len=256)
        _ = v_text(self.tool_name, "tool_suspension.tool_name", max_len=256)
        _ = v_text(self.arguments, "tool_suspension.arguments", max_len=1_048_576)
        if self.content is not None:
            _ = v_text(self.content, "tool_suspension.content", max_len=16_777_216)


@runtime_checkable
class ContinuationCapableAdapter(Protocol):
    """The D-062 continuation surface an adapter may additionally implement.

    ``suspension_handle`` resolves a ``tool_call`` id the adapter issued
    on a suspension it produced. ``deliver_tool_result`` transports one
    harness tool result into the EXACT suspended execution and waits for
    the SAME turn's terminal outcome — or a further :class:`ToolSuspension`
    for sequential tool rounds. ``cancel_suspension`` stops the suspended
    turn best-effort (registry expiry/cancellation). ``suspension_alive``
    reports whether the suspended execution is still tracked (the
    registry's liveness probe). Implementations raise
    :class:`ContinuationLostError` when the suspension died before the
    result was delivered and :class:`AdapterAmbiguousError` when it died
    after.
    """

    def suspension_handle(self, continuation_token: str) -> SuspensionHandle | None: ...

    def deliver_tool_result(
        self,
        handle: SuspensionHandle,
        content: str,
        context: ExecutionContext,
    ) -> "AdapterResult | ToolSuspension": ...

    def cancel_suspension(self, handle: SuspensionHandle) -> None: ...

    def suspension_alive(self, handle: SuspensionHandle) -> bool: ...


@runtime_checkable
class ContinuationBindingAdapter(Protocol):
    """Optional non-secret per-resource authority identity for live rebuilds.

    Equal tuples authorize retaining the original suspended adapter/handle,
    not rerouting. Unknown bindings cannot resume through a different instance.
    This internal seam changes no client or worker wire contract.
    """

    def continuation_binding(self, resource_id: str) -> tuple[str, ...] | None: ...


__all__ = [
    "CALL_CANCELLED",
    "CALL_COMPLETED",
    "CALL_FAILED",
    "CALL_STATUSES",
    "CALL_UNKNOWN",
    "CHUNK_FINISH",
    "CHUNK_KINDS",
    "CHUNK_REASONING_DELTA",
    "CHUNK_TEXT_DELTA",
    "CHUNK_TOOL_CALL",
    "CHUNK_USAGE",
    "FINISH_CANCELLED",
    "FINISH_LENGTH",
    "FINISH_REASONS",
    "FINISH_STOP",
    "FINISH_TOOL_CALLS",
    "RESULT_STATUSES",
    "AdapterAmbiguousError",
    "AdapterCall",
    "AdapterMessage",
    "AdapterPermanentError",
    "AdapterRegistry",
    "AdapterResult",
    "AdapterStreamChunk",
    "AdapterTimeoutError",
    "AdapterToolCall",
    "CallObservation",
    "ClientDisconnectedError",
    "CompletionOutcome",
    "ContinuationCapableAdapter",
    "ContinuationBindingAdapter",
    "ContinuationLostError",
    "ExecutionContext",
    "ExecutionAdapter",
    "SuspensionHandle",
    "ToolSuspension",
]
