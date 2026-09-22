"""Loopback translation for the worker's Ollama local adapter (M04 x M05).

**ONE OpenAI-compatible translation for the worker side.** Since the M04
integration this module is NOT a second semantic implementation: the
production default (:class:`OpenAICompatibleLoopbackTranslation`) is a
thin adaptation layer over the shared wire-translation core
:mod:`scarcity_router.providers.openai_http_core` — the exact functions
the server-direct HTTP adapter uses
(:func:`build_chat_completion_request`,
:class:`SseStreamParser`, :func:`interpret_stream_frame`,
:class:`ToolCallAccumulator`,
:func:`parse_chat_completion_response`) — bound to the evidence-backed
``ollama`` preset
:class:`~scarcity_router.providers.openai_http_core.TranslationPolicy`.
The transports differ (loopback HTTP on the worker host vs the server's
direct connection); the semantics are the same one implementation, so
``docs/providers.md``'s "never two Ollama implementations" rule holds
for the worker-bridged path too.

What the adaptation layer guarantees (all inherited from the core):

- **Complete tool calls only** — provider tool-call deltas are
  accumulated by :class:`ToolCallAccumulator` and flushed once at stream
  end; the normalized ``tool_call`` chunk always carries a complete call.
- **Honest usage** — provider-reported usage or absent usage, never a
  fabricated zero (:func:`extract_usage` semantics of the core).
- **Fail-closed unevidenced features** — under the ``ollama`` preset a
  ``json_schema`` response format is refused (M04's dated evidence
  documents JSON-mode only), ``tool_choice`` values other than the
  de-facto ``auto`` default are refused, and unevidenced generation
  parameters are refused — before any byte is sent.
- **Streaming** — SSE framing, keep-alive comments, the ``[DONE]``
  sentinel and the evidenced usage shapes all go through
  :class:`SseStreamParser`.

The :class:`LoopbackTranslation` Protocol stays the narrow replaceable
seam so tests can inject fully synthetic translations; the streaming
direction is a per-response *session* (the core parses SSE
incrementally and accumulates tool calls, so a stateless
line-at-a-time function cannot express it).

Failure typing: every failure is a
:class:`~scarcity_router.worker_protocol.WorkerProtocolError` — a typed
translation failure the loopback adapter and the worker runtime turn
into a failed execution result, never a dead execution thread — in BOTH
directions:

- **Non-streaming**, the response body is parsed on this side of the
  core's document boundary, so the deep-nesting ``RecursionError``
  hardening lives here (:func:`_parse_object`).
- **Streaming**, the shared core's SSE parser raises a BARE
  ``RecursionError`` from its ``json.loads`` on deeply nested frames
  (``TranslationError`` does not cover it: ``RecursionError`` is not a
  ``ValueError``). The adaptation layer re-types it into the same typed
  translation failure — the approved core module stays byte-identical,
  so the re-typing belongs to this layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast

from .gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterStreamChunk,
    CHUNK_FINISH,
    CHUNK_TEXT_DELTA,
    CHUNK_TOOL_CALL,
    CHUNK_USAGE,
)
from .gateway_contracts import UsageTokens
from .providers.openai_http_core import (
    SseStreamParser,
    ToolCallAccumulator,
    TranslationError,
    TranslationPolicy,
    build_chat_completion_request,
    interpret_stream_frame,
    parse_chat_completion_response,
)
from .providers.openai_http_presets import OLLAMA_PRESET
from .worker_protocol import WorkerProtocolError

_LOOPBACK_REQUEST_LIMIT_BYTES = 8 * 1_048_576

_LOOPBACK_STREAM_TOTAL_LIMIT_BYTES = 64 * 1_048_576


@dataclass(frozen=True)
class LoopbackHTTPRequest:
    """One request the adapter's transport must deliver (loopback only)."""

    method: str
    path: str
    body: bytes | None  # None for GET-style probes

    @property
    def body_text(self) -> str:
        if self.body is None:
            return ""
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class LoopbackHTTPResponse:
    """One transport-delivered response (status + body bytes)."""

    status: int
    body: bytes
    content_type: str = "application/json"

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class LoopbackStreamSession(Protocol):
    """One streaming response's translation session.

    The adapter feeds every SSE line of the response in arrival order and
    then closes the session; each call returns the normalized chunks the
    core produced (text deltas incrementally; complete tool calls, the
    finish reason and usage at stream end). Failures are typed
    :class:`~scarcity_router.worker_protocol.WorkerProtocolError`.
    """

    def feed_line(self, line: str) -> tuple[AdapterStreamChunk, ...]: ...

    def close(self) -> tuple[AdapterStreamChunk, ...]: ...


class LoopbackTranslation(Protocol):
    """The narrow seam between the typed M03 call and the loopback HTTP API.

    Implementations translate one direction each way and nothing else:
    build a request from an :class:`AdapterCall`, and turn responses back
    into the normalized vocabulary. No policy, no retry, no networking.
    The production implementation adapts the shared M04 translation core;
    tests may inject synthetic translations.
    """

    def build_chat_request(self, call: AdapterCall) -> LoopbackHTTPRequest: ...

    def build_stream_request(self, call: AdapterCall) -> LoopbackHTTPRequest: ...

    def build_probe_request(self) -> LoopbackHTTPRequest: ...

    def parse_response(
        self, response: LoopbackHTTPResponse
    ) -> tuple[AdapterMessage, str, UsageTokens | None]: ...

    def open_stream_session(self) -> LoopbackStreamSession: ...


class _CoreStreamSession:
    """One :class:`SseStreamParser` run bound to one streamed response.

    Lines are re-encoded to UTF-8 (the loopback body was decoded only to
    be split into lines) and fed to the shared parser; frames are
    interpreted with :func:`interpret_stream_frame`. Tool-call deltas
    accumulate in a :class:`ToolCallAccumulator` and are flushed as
    COMPLETE calls at ``close()``, followed by the finish chunk and the
    usage chunk — the same order the server-direct adapter emits.

    Failure typing: the shared core signals protocol drift with
    :class:`TranslationError`, but its SSE ``json.loads`` raises a BARE
    ``RecursionError`` on a deeply nested frame. This layer catches both
    and re-types them into
    :class:`~scarcity_router.worker_protocol.WorkerProtocolError`, so a
    hostile or drifted local endpoint can never kill the worker's
    per-attempt execution thread with an untyped exception.
    """

    def __init__(self, policy: TranslationPolicy) -> None:
        self._parser: SseStreamParser = SseStreamParser(
            max_total_bytes=_LOOPBACK_STREAM_TOTAL_LIMIT_BYTES
        )
        self._accumulator: ToolCallAccumulator = ToolCallAccumulator()
        self._policy: TranslationPolicy = policy

    def feed_line(self, line: str) -> tuple[AdapterStreamChunk, ...]:
        try:
            frames = self._parser.feed((line + "\n").encode("utf-8"))
        except (TranslationError, RecursionError) as exc:
            raise WorkerProtocolError("internal_error", str(exc)) from None
        return self._chunks_for(frames)

    def close(self) -> tuple[AdapterStreamChunk, ...]:
        try:
            frames = self._parser.close()
            tool_calls = self._accumulator.complete()
        except (TranslationError, RecursionError) as exc:
            raise WorkerProtocolError("internal_error", str(exc)) from None
        chunks = list(self._chunks_for(frames))
        for tool_call in tool_calls:
            chunks.append(AdapterStreamChunk(kind=CHUNK_TOOL_CALL, tool_call=tool_call))
        return tuple(chunks)

    def _chunks_for(
        self, frames: tuple[dict[str, object], ...]
    ) -> tuple[AdapterStreamChunk, ...]:
        chunks: list[AdapterStreamChunk] = []
        for frame in frames:
            try:
                view = interpret_stream_frame(frame)
                for fragment in view.tool_fragments:
                    self._accumulator.add_fragment(fragment)
            except (TranslationError, RecursionError) as exc:
                raise WorkerProtocolError("internal_error", str(exc)) from None
            if view.text_delta:
                chunks.append(
                    AdapterStreamChunk(kind=CHUNK_TEXT_DELTA, text=view.text_delta)
                )
            if view.finish_reason is not None:
                chunks.append(
                    AdapterStreamChunk(
                        kind=CHUNK_FINISH, finish_reason=view.finish_reason
                    )
                )
            if view.usage is not None:
                chunks.append(AdapterStreamChunk(kind=CHUNK_USAGE, usage=view.usage))
        return tuple(chunks)


class OpenAICompatibleLoopbackTranslation:
    """The production loopback translation: the M04 core on the ``ollama`` preset.

    Builds the request with :func:`build_chat_completion_request` under
    the preset's evidenced :class:`TranslationPolicy` (default: the
    ``ollama`` preset; an administrator may bind another evidence-backed
    preset's policy at construction) and parses responses with the
    shared :func:`parse_chat_completion_response` / :class:`SseStreamParser`.
    No second OpenAI-compatible semantic implementation exists worker-side.
    """

    def __init__(self, policy: TranslationPolicy | None = None) -> None:
        self._policy: TranslationPolicy = (
            policy if policy is not None else OLLAMA_PRESET.policy
        )

    @property
    def policy(self) -> TranslationPolicy:
        return self._policy

    def build_chat_request(self, call: AdapterCall) -> LoopbackHTTPRequest:
        return self._request(call)

    def build_stream_request(self, call: AdapterCall) -> LoopbackHTTPRequest:
        # The core reads ``call.stream`` itself, so the wire document of a
        # streaming request is built the same way; the adapter only uses
        # the method pair to pick the response-parsing path.
        return self._request(call)

    def build_probe_request(self) -> LoopbackHTTPRequest:
        """A lightweight reachability probe (never sends prompt content).

        Uses the preset's evidenced health endpoint, else its discovery
        endpoint (both native read-only Ollama reads that never consume
        inference quota). A preset evidencing neither has no honest probe.
        """
        path = self._policy.health_path or self._policy.discovery_path
        if path is None:
            raise WorkerProtocolError(
                "internal_error",
                f"preset {self._policy.preset_id!r} evidences no probe endpoint",
            )
        return LoopbackHTTPRequest(method="GET", path=path, body=None)

    def parse_response(
        self, response: LoopbackHTTPResponse
    ) -> tuple[AdapterMessage, str, UsageTokens | None]:
        """Parse one whole-document response (non-streaming) via the core."""
        if response.status != 200:
            raise WorkerProtocolError(
                "internal_error",
                f"the loopback endpoint answered HTTP {response.status}",
            )
        document = _parse_object(
            response.text, "the loopback endpoint sent a malformed response"
        )
        try:
            parsed = parse_chat_completion_response(document, self._policy)
        except TranslationError as exc:
            raise WorkerProtocolError("internal_error", str(exc)) from None
        return parsed.message, parsed.finish_reason, parsed.usage

    def open_stream_session(self) -> LoopbackStreamSession:
        return _CoreStreamSession(self._policy)

    def _request(self, call: AdapterCall) -> LoopbackHTTPRequest:
        try:
            body_document = build_chat_completion_request(call, self._policy)
        except TranslationError as exc:
            # A request feature the preset does not evidence is refused
            # before any byte is built or sent (fail closed).
            raise WorkerProtocolError("malformed_message", str(exc)) from None
        body = json.dumps(body_document, allow_nan=False).encode("utf-8")
        if len(body) > _LOOPBACK_REQUEST_LIMIT_BYTES:
            raise WorkerProtocolError(
                "malformed_message",
                "the translated loopback request exceeds the transport bound",
            )
        return LoopbackHTTPRequest(
            method="POST", path=self._policy.endpoint_path, body=body
        )


def _parse_object(text: str, message: str) -> dict[str, object]:
    """Strictly parse one JSON object; anything else fails closed."""
    try:
        parsed = cast("object", json.loads(text))
    except json.JSONDecodeError:
        raise WorkerProtocolError("internal_error", message) from None
    except RecursionError:
        # A deeply nested local-endpoint response raises RecursionError,
        # not ValueError: it must become a typed translation failure so
        # the execution thread returns a failed result instead of dying.
        raise WorkerProtocolError(
            "internal_error", "the loopback response exceeds the JSON nesting bound"
        ) from None
    if not isinstance(parsed, dict):
        raise WorkerProtocolError("internal_error", message)
    return cast("dict[str, object]", parsed)


__all__ = [
    "LoopbackHTTPRequest",
    "LoopbackHTTPResponse",
    "LoopbackStreamSession",
    "LoopbackTranslation",
    "OpenAICompatibleLoopbackTranslation",
]
