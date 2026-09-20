"""Provisional loopback translation for the Ollama local adapter (M05 seam).

**CROSS-WORKSTREAM SEAM — READ THIS BEFORE EXTENDING.**

The M04 workstream (issue #89) owns the authoritative OpenAI-compatible
translation semantics for Ollama (direct and worker-bridged transport).
M05 and M04 run in parallel branches, and M05 must not duplicate (or
pre-empt) that semantic core. This module therefore defines the NARROW
replaceable call-site the worker's loopback adapter consumes
(:class:`LoopbackTranslation`) plus a MINIMAL, PROVISIONAL default
(:class:`ProvisionalOpenAITranslation`) that is sufficient for transport
bring-up and tests only.

When M04 lands, its shared translation core replaces the default by
constructing :class:`~scarcity_router.worker_local_adapters.LoopbackOllamaAdapter`
with the M04 translation object — one construction-site change, no
protocol change, and no second long-lived Ollama semantic implementation.
The provisional default is deliberately small, clearly labelled, and
fails closed on anything it does not fully understand.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from .gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterStreamChunk,
    AdapterToolCall,
    CHUNK_FINISH,
    CHUNK_TEXT_DELTA,
    CHUNK_USAGE,
    FINISH_LENGTH,
    FINISH_STOP,
)
from .gateway_contracts import UsageTokens
from .worker_protocol import WorkerProtocolError

_LOOPBACK_REQUEST_LIMIT_BYTES = 8 * 1_048_576


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


class LoopbackTranslation(Protocol):
    """The narrow seam between the typed M03 call and the loopback HTTP API.

    Implementations translate one direction each way and nothing else:
    build a request from an :class:`AdapterCall`, and turn responses back
    into the normalized vocabulary. No policy, no retry, no networking.
    """

    def build_chat_request(self, call: AdapterCall) -> LoopbackHTTPRequest: ...

    def build_stream_request(self, call: AdapterCall) -> LoopbackHTTPRequest: ...

    def build_probe_request(self) -> LoopbackHTTPRequest: ...

    def parse_response(
        self, response: LoopbackHTTPResponse
    ) -> tuple[AdapterMessage, str, UsageTokens | None]: ...

    def parse_stream_line(self, line: str) -> AdapterStreamChunk | None: ...


class ProvisionalOpenAITranslation:
    """MINIMAL provisional translation for transport bring-up (NOT M04).

    Translates the typed call to one OpenAI-compatible
    ``POST /v1/chat/completions`` request against the loopback endpoint
    and parses a JSON response or an SSE stream back into the normalized
    vocabulary. Semantics it cannot fully honor are dropped DEFENSIVELY:
    an unsupported structured-output request makes the translation fail
    closed rather than silently degrade. Replace with the shared M04
    translation core at the adapter construction site.
    """

    chat_path: str = "/v1/chat/completions"
    probe_path: str = "/api/tags"

    def build_chat_request(self, call: AdapterCall) -> LoopbackHTTPRequest:
        body = json.dumps(
            self._request_payload(call, stream=False), allow_nan=False
        ).encode("utf-8")
        return LoopbackHTTPRequest(
            method="POST", path=self.chat_path, body=self._bounded(body)
        )

    def build_stream_request(self, call: AdapterCall) -> LoopbackHTTPRequest:
        body = json.dumps(
            self._request_payload(call, stream=True), allow_nan=False
        ).encode("utf-8")
        return LoopbackHTTPRequest(
            method="POST", path=self.chat_path, body=self._bounded(body)
        )

    def build_probe_request(self) -> LoopbackHTTPRequest:
        """A lightweight reachability probe (never sends prompt content)."""
        return LoopbackHTTPRequest(method="GET", path=self.probe_path, body=None)

    @staticmethod
    def _bounded(body: bytes) -> bytes:
        if len(body) > _LOOPBACK_REQUEST_LIMIT_BYTES:
            raise WorkerProtocolError(
                "malformed_message",
                "the translated loopback request exceeds the transport bound",
            )
        return body

    def _request_payload(self, call: AdapterCall, *, stream: bool) -> dict[str, object]:
        if call.response_format is not None and call.response_format.get("type") not in (
            None,
            "text",
        ):
            raise WorkerProtocolError(
                "malformed_message",
                "the provisional translation does not implement structured "
                + "output; refusing to silently drop the semantics",
            )
        payload: dict[str, object] = {
            "model": call.model.model,
            "messages": [_message_payload(message) for message in call.messages],
            "stream": stream,
        }
        if call.max_output_tokens is not None:
            payload["max_tokens"] = call.max_output_tokens
        for key in ("temperature", "top_p", "stop", "seed"):
            value = call.generation_params.get(key)
            if value is not None:
                payload[key] = value
        if call.tools:
            payload["tools"] = [dict(tool) for tool in call.tools]
        return payload

    def parse_response(
        self, response: LoopbackHTTPResponse
    ) -> tuple[AdapterMessage, str, UsageTokens | None]:
        """Parse one whole-document response (non-streaming)."""
        if response.status != 200:
            raise WorkerProtocolError(
                "internal_error",
                f"the loopback endpoint answered HTTP {response.status}",
            )
        document = _parse_object(response.text, "the loopback endpoint sent a malformed response")
        raw_choices = document.get("choices")
        if not isinstance(raw_choices, list) or not raw_choices:
            raise WorkerProtocolError(
                "internal_error", "the loopback response carries no choices"
            )
        choices = cast("list[object]", raw_choices)
        first = _object_item(choices, 0, "the loopback response carries no choices")
        message_doc = first.get("message")
        if not isinstance(message_doc, Mapping):
            raise WorkerProtocolError(
                "internal_error", "the loopback response carries no message"
            )
        message_mapping = cast("Mapping[str, object]", message_doc)
        content = message_mapping.get("content")
        finish_reason = first.get("finish_reason")
        tool_calls_doc = message_mapping.get("tool_calls")
        tool_calls: tuple[AdapterToolCall, ...] = ()
        if isinstance(tool_calls_doc, list) and tool_calls_doc:
            parsed_calls: list[AdapterToolCall] = []
            for raw in cast("list[object]", tool_calls_doc):
                if not isinstance(raw, Mapping):
                    continue
                raw_mapping = cast("Mapping[str, object]", raw)
                function = raw_mapping.get("function")
                if not isinstance(function, Mapping):
                    continue
                function_mapping = cast("Mapping[str, object]", function)
                arguments = function_mapping.get("arguments")
                parsed_calls.append(
                    AdapterToolCall(
                        id=str(raw_mapping.get("id", "")),
                        name=str(function_mapping.get("name", "")),
                        arguments=(
                            arguments
                            if isinstance(arguments, str)
                            else json.dumps(arguments, allow_nan=False)
                        ),
                    )
                )
            tool_calls = tuple(parsed_calls)
        message = AdapterMessage(
            role="assistant",
            content=content if isinstance(content, str) else None,
            tool_calls=tool_calls,
        )
        if finish_reason == "tool_calls" and tool_calls:
            reason = "tool_calls"
        elif finish_reason in (None, "stop"):
            reason = FINISH_STOP
        else:
            reason = FINISH_LENGTH
        usage = self._usage_from(document.get("usage"))
        return message, reason, usage

    def parse_stream_line(self, line: str) -> AdapterStreamChunk | None:
        """Parse one SSE ``data:`` line of an OpenAI-compatible stream.

        Returns ``None`` for keep-alives and the ``[DONE]`` sentinel.
        """
        stripped = line.strip()
        if not stripped.startswith("data:"):
            return None
        payload_text = stripped[len("data:") :].strip()
        if payload_text == "[DONE]":
            return None
        document = _parse_object(payload_text, "the loopback stream sent a malformed frame")
        usage = self._usage_from(document.get("usage"))
        if usage is not None:
            return AdapterStreamChunk(kind=CHUNK_USAGE, usage=usage)
        raw_choices = document.get("choices")
        if not isinstance(raw_choices, list) or not raw_choices:
            return None
        choices = cast("list[object]", raw_choices)
        first = _object_item(choices, 0, "the loopback stream frame carries no choices")
        delta = first.get("delta")
        if not isinstance(delta, Mapping):
            return None
        delta_mapping = cast("Mapping[str, object]", delta)
        finish_reason = first.get("finish_reason")
        if finish_reason:
            reason = (
                finish_reason
                if finish_reason in ("stop", "length", "tool_calls", "cancelled")
                else FINISH_LENGTH
            )
            return AdapterStreamChunk(kind=CHUNK_FINISH, finish_reason=reason)
        content = delta_mapping.get("content")
        if isinstance(content, str) and content:
            return AdapterStreamChunk(kind=CHUNK_TEXT_DELTA, text=content)
        return None

    def _usage_from(self, raw: object) -> UsageTokens | None:
        if not isinstance(raw, Mapping):
            return None
        usage_mapping = cast("Mapping[str, object]", raw)
        prompt = usage_mapping.get("prompt_tokens")
        completion = usage_mapping.get("completion_tokens")
        if not isinstance(prompt, int) or isinstance(prompt, bool):
            return None
        if not isinstance(completion, int) or isinstance(completion, bool):
            return None
        return UsageTokens(prompt_tokens=prompt, completion_tokens=completion)


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


def _object_item(
    choices: list[object], index: int, message: str
) -> Mapping[str, object]:
    if not choices:
        raise WorkerProtocolError("internal_error", message)
    item = choices[index]
    if not isinstance(item, Mapping):
        raise WorkerProtocolError("internal_error", message)
    return cast("Mapping[str, object]", item)


def _message_payload(message: AdapterMessage) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role}
    if message.content is not None:
        payload["content"] = message.content
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        payload["name"] = message.name
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return payload


__all__ = [
    "LoopbackHTTPRequest",
    "LoopbackHTTPResponse",
    "LoopbackTranslation",
    "ProvisionalOpenAITranslation",
]
