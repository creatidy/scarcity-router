"""OpenAI-compatible execution-surface v1 wire mapping (M03, D-045).

Owns the strict parsing of ``POST /v1/chat/completions`` request bodies,
the inference of request-structure capability requirements, and the
rendering of OpenAI-shaped responses and SSE chunks. This module is wire
mapping only: it owns no routing, no policy, no provider semantics and no
transport. Its error vocabulary is the execution surface's own
OpenAI-compatible one (:class:`~scarcity_router.gateway_contracts.GatewayError`),
never the machine-interface v1 vocabulary (D-045).

Strictness rules (frozen here):

- request bodies arrive pre-parsed by the transport's strict JSON reader
  (duplicate keys and non-finite JSON constants are rejected before this
  module runs);
- the top-level key set is a closed allowlist of the OpenAI
  chat-completion fields execution surface v1 defines; anything else is
  ``unknown_parameter`` — an execution surface never silently ignores
  request semantics it does not implement;
- ``n`` must be absent or 1 (per-choice fan-out is a later explicit
  sub-scope); ``logprobs``/``top_logprobs``/``logit_bias``/
  ``service_tier`` and other unimplemented parameters are rejected the
  same way;
- ``max_tokens`` and ``max_completion_tokens`` are mutually exclusive
  (both is an error), matching the upstream convention;
- content parts (multimodal arrays) are rejected in v1: text content
  only;
- the Responses API (``/v1/responses``) is NOT implemented and is never
  faked; it is a later explicit sub-scope with a documented supported
  subset (D-043).

Capability inference (D-042/D-043): request STRUCTURE yields
compatibility requirements, never ranking inputs and never an LLM request
classifier — tools present require ``tool_calls``; ``role: "tool"``
messages require ``tool_results``; more than one message requires
``roles_history``; a structured ``response_format`` requires
``structured_output``; ``stream: true`` requires ``streaming``; a
``reasoning_effort`` value requires ``reasoning_controls``. The context
floor is a coarse conservative estimate (``ceil(chars / 4)``) documented
as such; it is a floor, never a ranking signal.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from collections.abc import Mapping
from typing import cast

from .gateway_validation import v_str_object_mapping
from .gateway_adapters import (
    CHUNK_FINISH,
    CHUNK_TEXT_DELTA,
    CHUNK_TOOL_CALL,
    CHUNK_USAGE,
    AdapterMessage,
    AdapterStreamChunk,
    AdapterToolCall,
    CompletionOutcome,
)
from .gateway_contracts import GatewayError

# ── Frozen vocabularies ───────────────────────────────────────────────────────

CHAT_COMPLETION_ALLOWED_KEYS: frozenset[str] = frozenset({
    "model",
    "messages",
    "stream",
    "stream_options",
    "tools",
    "tool_choice",
    "response_format",
    "reasoning_effort",
    "max_completion_tokens",
    "max_tokens",
    "temperature",
    "top_p",
    "stop",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "parallel_tool_calls",
    "user",
    "metadata",
})

MESSAGE_ROLES: frozenset[str] = frozenset({
    "system",
    "developer",
    "user",
    "assistant",
    "tool",
})

MESSAGE_ALLOWED_KEYS: frozenset[str] = frozenset({
    "role",
    "content",
    "name",
    "tool_call_id",
    "tool_calls",
})

REASONING_EFFORTS: frozenset[str] = frozenset({
    "minimal",
    "low",
    "medium",
    "high",
})

RESPONSE_FORMAT_TYPES: frozenset[str] = frozenset({
    "text",
    "json_object",
    "json_schema",
})

STRUCTURED_OUTPUT_TYPES: frozenset[str] = frozenset({
    "json_object",
    "json_schema",
})

MAX_METADATA_ENTRIES = 16
MAX_STOP_SEQUENCES = 4
MAX_TOOLS = 128
MAX_MESSAGES = 1024

OWNED_BY = "scarcity-router"


def _err(message: str, *, code: str | None = None, param: str | None = None) -> GatewayError:
    return GatewayError.invalid_request(message, code=code, param=param)


def _v_object_list(value: object, param: str) -> list[object]:
    if not isinstance(value, list):
        raise _err(f"{param} must be an array", param=param)
    return cast("list[object]", value)


def _v_number(value: object, param: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(f"{param} must be a number", param=param)
    if isinstance(value, float) and not math.isfinite(value):
        raise _err(f"{param} must be a finite number", param=param)
    return value


def _v_str_field(value: object, param: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise _err(f"{param} must be a string", param=param)
    if not value:
        raise _err(f"{param} must not be empty", param=param)
    if len(value) > max_len:
        raise _err(f"{param} exceeds the maximum length", param=param)
    return value


def _v_int_field(value: object, param: str, *, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(f"{param} must be an integer", param=param)
    if value < lo or value > hi:
        raise _err(f"{param} is out of the accepted range", param=param)
    return value


# ── Parsed request and capability view ────────────────────────────────────────


@dataclass(frozen=True)
class RequestCapabilities:
    """Compatibility requirements inferred from request STRUCTURE (D-042).

    These gate admission against the selected target's OpenAI
    compatibility matrix; they are never ranking inputs. A missing or
    ``UNKNOWN``/``UNSUPPORTED`` matrix cell for a required feature fails
    closed (D-043).
    """

    requires_tool_calls: bool
    requires_tool_results: bool
    requires_roles_history: bool
    requires_structured_output: bool
    requires_streaming: bool
    requires_reasoning_controls: bool
    estimated_input_tokens: int | None
    requested_output_tokens: int | None


@dataclass(frozen=True)
class ChatCompletionRequest:
    """One strictly parsed ``chat/completions`` request."""

    model: str
    messages: tuple[AdapterMessage, ...]
    stream: bool
    include_usage: bool
    capabilities: RequestCapabilities
    tools: tuple[Mapping[str, object], ...] = ()
    tool_choice: object = None
    response_format: Mapping[str, object] | None = None
    reasoning_effort: str | None = None
    generation_params: Mapping[str, object] | None = None
    metadata: Mapping[str, str] | None = None


def parse_chat_completion_request(document: object) -> ChatCompletionRequest:
    """Parse and validate one request body; raises :class:`GatewayError`."""
    document = v_str_object_mapping(document, "request body")
    extra = set(document.keys()) - CHAT_COMPLETION_ALLOWED_KEYS
    if extra:
        raise _err(
            "unknown request parameter",
            code="unknown_parameter",
            param=sorted(extra)[0],
        )
    model = _v_str_field(document.get("model"), "model", max_len=512)
    raw_messages = _v_object_list(document.get("messages"), "messages")
    if not raw_messages:
        raise _err("messages must be a non-empty array", param="messages")
    if len(raw_messages) > MAX_MESSAGES:
        raise _err("messages exceeds the maximum conversation length", param="messages")
    messages = tuple(
        _parse_message(item, index) for index, item in enumerate(raw_messages)
    )

    stream = document.get("stream", False)
    if not isinstance(stream, bool):
        raise _err("stream must be a boolean", param="stream")
    include_usage = False
    if "stream_options" in document:
        if not stream:
            raise _err(
                "stream_options requires stream to be true",
                param="stream_options",
            )
        raw_options = v_str_object_mapping(
            document.get("stream_options"), "stream_options"
        )
        extra_options = set(raw_options.keys()) - {"include_usage"}
        if extra_options:
            raise _err(
                "unknown stream_options parameter",
                code="unknown_parameter",
                param=f"stream_options.{sorted(extra_options)[0]}",
            )
        include_usage = raw_options.get("include_usage", False)
        if not isinstance(include_usage, bool):
            raise _err(
                "stream_options.include_usage must be a boolean",
                param="stream_options.include_usage",
            )

    tools: tuple[Mapping[str, object], ...] = ()
    if "tools" in document:
        raw_tools = _v_object_list(document.get("tools"), "tools")
        if len(raw_tools) > MAX_TOOLS:
            raise _err("tools exceeds the maximum count", param="tools")
        tools = tuple(
            _parse_tool(tool, index) for index, tool in enumerate(raw_tools)
        )

    tool_choice, forced_tool_choice = _parse_tool_choice(
        document.get("tool_choice"), present="tool_choice" in document
    )

    response_format: Mapping[str, object] | None = None
    if "response_format" in document:
        raw_format = v_str_object_mapping(
            document.get("response_format"), "response_format"
        )
        format_type = raw_format.get("type")
        if format_type not in RESPONSE_FORMAT_TYPES:
            raise _err(
                "unsupported response_format type",
                code="unsupported_parameter",
                param="response_format.type",
            )
        extra_format = set(raw_format.keys()) - {"type", "json_schema"}
        if extra_format:
            raise _err(
                "unknown response_format parameter",
                code="unknown_parameter",
                param=f"response_format.{sorted(extra_format)[0]}",
            )
        if format_type == "json_schema":
            _ = v_str_object_mapping(
                raw_format.get("json_schema"), "response_format.json_schema"
            )
        response_format = raw_format

    reasoning_effort: str | None = None
    if "reasoning_effort" in document:
        raw_effort = document.get("reasoning_effort")
        if not isinstance(raw_effort, str) or raw_effort not in REASONING_EFFORTS:
            raise _err(
                "unsupported reasoning_effort value",
                code="unsupported_parameter",
                param="reasoning_effort",
            )
        reasoning_effort = raw_effort

    requested_output = _parse_output_limit(document)

    generation: dict[str, object] = {}
    if "temperature" in document:
        value = _v_number(document.get("temperature"), "temperature")
        if not 0 <= float(value) <= 2:
            raise _err("temperature is out of the accepted range", param="temperature")
        generation["temperature"] = value
    if "top_p" in document:
        value = _v_number(document.get("top_p"), "top_p")
        if not 0 <= float(value) <= 1:
            raise _err("top_p is out of the accepted range", param="top_p")
        generation["top_p"] = value
    if "frequency_penalty" in document:
        value = _v_number(document.get("frequency_penalty"), "frequency_penalty")
        if not -2 <= float(value) <= 2:
            raise _err(
                "frequency_penalty is out of the accepted range",
                param="frequency_penalty",
            )
        generation["frequency_penalty"] = value
    if "presence_penalty" in document:
        value = _v_number(document.get("presence_penalty"), "presence_penalty")
        if not -2 <= float(value) <= 2:
            raise _err(
                "presence_penalty is out of the accepted range",
                param="presence_penalty",
            )
        generation["presence_penalty"] = value
    if "stop" in document:
        generation["stop"] = _parse_stop(document.get("stop"))
    if "seed" in document:
        generation["seed"] = _v_int_field(document.get("seed"), "seed", lo=0, hi=2**31 - 1)
    if "parallel_tool_calls" in document:
        value = document.get("parallel_tool_calls")
        if not isinstance(value, bool):
            raise _err("parallel_tool_calls must be a boolean", param="parallel_tool_calls")
        generation["parallel_tool_calls"] = value

    metadata: dict[str, str] | None = None
    if "metadata" in document:
        raw_metadata = v_str_object_mapping(document.get("metadata"), "metadata")
        if len(raw_metadata) > MAX_METADATA_ENTRIES:
            raise _err("metadata exceeds the maximum entry count", param="metadata")
        metadata = {}
        for key, value in raw_metadata.items():
            if not key or len(key) > 64:
                raise _err("metadata keys must be non-empty strings", param="metadata")
            if not isinstance(value, str) or len(value) > 512:
                raise _err(
                    "metadata values must be strings", param="metadata"
                )
            metadata[key] = value

    # ``n`` is not in the allowlist above, so per-choice sampling is
    # rejected as an unknown parameter; nothing else is deferred here.

    requires_tool_calls = bool(tools) or tool_choice == "required" or forced_tool_choice
    requires_tool_results = any(message.role == "tool" for message in messages)
    requires_roles_history = len(messages) > 1
    requires_structured_output = (
        response_format is not None
        and cast(str, response_format.get("type")) in STRUCTURED_OUTPUT_TYPES
    )
    estimated = estimate_input_tokens(messages, tools)
    return ChatCompletionRequest(
        model=model,
        messages=messages,
        stream=stream,
        include_usage=include_usage,
        capabilities=RequestCapabilities(
            requires_tool_calls=requires_tool_calls,
            requires_tool_results=requires_tool_results,
            requires_roles_history=requires_roles_history,
            requires_structured_output=requires_structured_output,
            requires_streaming=stream,
            requires_reasoning_controls=reasoning_effort is not None,
            estimated_input_tokens=estimated,
            requested_output_tokens=requested_output,
        ),
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
        generation_params=generation,
        metadata=metadata,
    )


def _parse_message(item: object, index: int) -> AdapterMessage:
    item = v_str_object_mapping(item, f"messages[{index}]")
    extra = set(item.keys()) - MESSAGE_ALLOWED_KEYS
    if extra:
        raise _err(
            "unknown message field",
            code="unknown_parameter",
            param=f"messages[{index}].{sorted(extra)[0]}",
        )
    role = item.get("role")
    if not isinstance(role, str) or role not in MESSAGE_ROLES:
        raise _err(
            f"messages[{index}].role is not a supported role",
            code="unsupported_parameter",
            param="messages",
        )
    raw_content = item.get("content")
    content: str | None = None
    if raw_content is not None:
        if isinstance(raw_content, list):
            raise _err(
                "content parts are not part of execution surface v1",
                code="unsupported_parameter",
                param="messages",
            )
        if not isinstance(raw_content, str):
            raise _err(f"messages[{index}].content must be a string", param="messages")
        content = raw_content
    elif role != "assistant":
        # Only assistant messages may carry no content (tool_calls only).
        raise _err(
            f"messages[{index}].content is required for role {role!r}",
            param="messages",
        )
    name = item.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 256):
        raise _err(f"messages[{index}].name must be a string", param="messages")
    tool_call_id = item.get("tool_call_id")
    if role == "tool":
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise _err(
                "tool messages require tool_call_id",
                param=f"messages[{index}].tool_call_id",
            )
    elif tool_call_id is not None:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise _err(
                f"messages[{index}].tool_call_id must be a string",
                param="messages",
            )
    tool_calls: tuple[AdapterToolCall, ...] = ()
    if "tool_calls" in item:
        if role != "assistant":
            raise _err(
                "only assistant messages carry tool_calls",
                param=f"messages[{index}].tool_calls",
            )
        raw_calls = item.get("tool_calls")
        if not isinstance(raw_calls, list):
            raise _err(
                f"messages[{index}].tool_calls must be an array",
                param="messages",
            )
        tool_calls = tuple(
            _parse_history_tool_call(call, index, call_index)
            for call_index, call in enumerate(cast("list[object]", raw_calls))
        )
    if role == "assistant" and content is None and not tool_calls:
        raise _err(
            "assistant messages require content or tool_calls",
            param=f"messages[{index}].content",
        )
    return AdapterMessage(
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
        name=name if isinstance(name, str) else None,
    )


def _parse_history_tool_call(item: object, message_index: int, call_index: int) -> AdapterToolCall:
    item = v_str_object_mapping(
        item, f"messages[{message_index}].tool_calls[{call_index}]"
    )
    extra = set(item.keys()) - {"id", "type", "function"}
    if extra:
        raise _err(
            "unknown tool_call field",
            code="unknown_parameter",
            param=f"messages[{message_index}].tool_calls[{call_index}]",
        )
    if item.get("type") != "function":
        raise _err(
            "only function tool_calls are supported",
            code="unsupported_parameter",
            param="messages",
        )
    raw_function = v_str_object_mapping(
        item.get("function"),
        f"messages[{message_index}].tool_calls[{call_index}].function",
    )
    call_id = item.get("id")
    name = raw_function.get("name")
    arguments = raw_function.get("arguments", "")
    if not isinstance(call_id, str) or not call_id:
        raise _err("tool_call id is required", param="messages")
    if not isinstance(name, str) or not name:
        raise _err("tool_call function name is required", param="messages")
    if not isinstance(arguments, str):
        raise _err("tool_call arguments must be a JSON string", param="messages")
    return AdapterToolCall(id=call_id, name=name, arguments=arguments)


def _parse_tool(item: object, index: int) -> Mapping[str, object]:
    item = v_str_object_mapping(item, f"tools[{index}]")
    if item.get("type") != "function":
        raise _err(
            "only function tools are supported by execution surface v1",
            code="unsupported_parameter",
            param="tools",
        )
    raw_function = v_str_object_mapping(item.get("function"), f"tools[{index}].function")
    name = raw_function.get("name")
    if not isinstance(name, str) or not name:
        raise _err(f"tools[{index}].function.name is required", param="tools")
    _ = v_str_object_mapping(
        raw_function.get("parameters", {}), f"tools[{index}].function.parameters"
    )
    return item


def _parse_tool_choice(value: object, *, present: bool) -> tuple[object, bool]:
    """The parsed tool_choice plus whether it forces tool use (admission)."""
    if not present or value is None:
        return ("auto" if present else None), False
    if isinstance(value, str):
        if value in ("none", "auto", "required"):
            return value, value == "required"
        raise _err(
            "unsupported tool_choice value",
            code="unsupported_parameter",
            param="tool_choice",
        )
    narrowed: Mapping[str, object] | None = None
    try:
        narrowed = v_str_object_mapping(value, "tool_choice")
    except GatewayError:
        narrowed = None
    if narrowed is not None:
        extra = set(narrowed.keys()) - {"type", "function"}
        if extra:
            raise _err(
                "unknown tool_choice field",
                code="unknown_parameter",
                param=f"tool_choice.{sorted(extra)[0]}",
            )
        if narrowed.get("type") != "function":
            raise _err(
                "only function tool_choice is supported",
                code="unsupported_parameter",
                param="tool_choice",
            )
        raw_function = v_str_object_mapping(
            narrowed.get("function"), "tool_choice.function"
        )
        if not isinstance(raw_function.get("name"), str):
            raise _err(
                "tool_choice.function.name is required", param="tool_choice"
            )
        return narrowed, True
    raise _err("tool_choice must be a string or an object", param="tool_choice")


def _parse_output_limit(document: Mapping[str, object]) -> int | None:
    has_legacy = "max_tokens" in document
    has_modern = "max_completion_tokens" in document
    if has_legacy and has_modern:
        raise _err(
            "max_tokens and max_completion_tokens are mutually exclusive",
            param="max_tokens",
        )
    if has_legacy:
        return _v_int_field(document.get("max_tokens"), "max_tokens", lo=1, hi=1_000_000)
    if has_modern:
        return _v_int_field(
            document.get("max_completion_tokens"), "max_completion_tokens", lo=1, hi=1_000_000
        )
    return None


def _parse_stop(value: object) -> object:
    if isinstance(value, str):
        if not value or len(value) > 256:
            raise _err("stop must be a non-empty string", param="stop")
        return value
    if isinstance(value, list):
        sequences = cast("list[object]", value)
        if not sequences or len(sequences) > MAX_STOP_SEQUENCES:
            raise _err(
                f"stop accepts at most {MAX_STOP_SEQUENCES} sequences",
                param="stop",
            )
        for item in sequences:
            if not isinstance(item, str) or not item or len(item) > 256:
                raise _err("stop sequences must be non-empty strings", param="stop")
        return sequences
    raise _err("stop must be a string or an array of strings", param="stop")


def _content_chars(message: AdapterMessage) -> int:
    total = len(message.content) if message.content is not None else 0
    total += len(message.tool_call_id) if message.tool_call_id is not None else 0
    for call in message.tool_calls:
        total += len(call.id) + len(call.name) + len(call.arguments)
    return total


def estimate_input_tokens(
    messages: tuple[AdapterMessage, ...],
    tools: tuple[Mapping[str, object], ...],
) -> int | None:
    """A coarse conservative context floor for the request (documented).

    Counts every character the conversation and the tool definitions
    contribute and divides by four — the usual LOWER-bound ratio for
    English text, which deliberately under-estimates CJK and dense
    punctuation. The value is a requirement FLOOR for admission (context
    must be at least this large), never a ranking signal and never an
    exact count. ``None`` when there is nothing to count.
    """
    total = sum(_content_chars(message) for message in messages)
    for tool in tools:
        total += len(json.dumps(tool, sort_keys=True, ensure_ascii=False))
    if total == 0:
        return None
    return -(-total // 4)


# ── Response rendering ────────────────────────────────────────────────────────


def models_list_payload(aliases: tuple[str, ...]) -> dict[str, object]:
    """The ``GET /v1/models`` payload: one entry per configured alias."""
    return {
        "object": "list",
        "data": [
            {
                "id": alias,
                "object": "model",
                "created": 0,
                "owned_by": OWNED_BY,
            }
            for alias in aliases
        ],
    }


def _message_payload(message: AdapterMessage) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role}
    payload["content"] = message.content if message.content is not None else None
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _usage_payload(outcome: CompletionOutcome) -> dict[str, object]:
    accounting = outcome.usage
    if accounting.provider_reported_usage is not None:
        tokens = accounting.provider_reported_usage
    elif accounting.estimated_usage is not None:
        tokens = accounting.estimated_usage
    else:
        tokens = None
    rendered: dict[str, object] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    if tokens is not None:
        rendered["prompt_tokens"] = tokens.prompt_tokens
        rendered["completion_tokens"] = tokens.completion_tokens
        rendered["total_tokens"] = tokens.total_tokens
    return rendered


def chat_completion_payload(outcome: CompletionOutcome) -> dict[str, object]:
    """Render one completed execution as a ``chat.completion`` object."""
    return {
        "id": outcome.request_id,
        "object": "chat.completion",
        "created": outcome.created,
        "model": outcome.model_echo,
        "choices": [
            {
                "index": 0,
                "message": _message_payload(outcome.message),
                "finish_reason": outcome.finish_reason,
            }
        ],
        "usage": _usage_payload(outcome),
        "x_scarcity_router": {
            "request_id": outcome.request_id,
            "decision_id": outcome.decision_id,
            "usage_source": outcome.usage.usage_source,
            "estimated_usage": (
                outcome.usage.estimated_usage.to_dict()
                if outcome.usage.estimated_usage is not None
                else None
            ),
        },
    }


def chunk_payload(
    *,
    request_id: str,
    created: int,
    model_echo: str,
    chunk: AdapterStreamChunk,
    tool_call_index: int,
) -> dict[str, object]:
    """Render one normalized stream chunk as a ``chat.completion.chunk``."""
    delta: dict[str, object] = {}
    usage: dict[str, object] | None = None
    choices: list[dict[str, object]]
    if chunk.kind == CHUNK_TEXT_DELTA:
        delta["content"] = chunk.text
        choices = [{"index": 0, "delta": delta, "finish_reason": None}]
    elif chunk.kind == CHUNK_TOOL_CALL:
        assert chunk.tool_call is not None
        delta["tool_calls"] = [
            {
                "index": tool_call_index,
                "id": chunk.tool_call.id,
                "type": "function",
                "function": {
                    "name": chunk.tool_call.name,
                    "arguments": chunk.tool_call.arguments,
                },
            }
        ]
        choices = [{"index": 0, "delta": delta, "finish_reason": None}]
    elif chunk.kind == CHUNK_FINISH:
        choices = [{"index": 0, "delta": {}, "finish_reason": chunk.finish_reason}]
    elif chunk.kind == CHUNK_USAGE:
        assert chunk.usage is not None
        usage = dict[str, object](chunk.usage.to_dict())
        choices = []
    else:  # pragma: no cover - construction validated
        raise ValueError(f"chunk_payload: unsupported chunk kind {chunk.kind!r}")
    payload: dict[str, object] = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_echo,
        "choices": choices,
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


__all__ = [
    "CHAT_COMPLETION_ALLOWED_KEYS",
    "MAX_MESSAGES",
    "MAX_STOP_SEQUENCES",
    "MAX_TOOLS",
    "MESSAGE_ROLES",
    "REASONING_EFFORTS",
    "ChatCompletionRequest",
    "RequestCapabilities",
    "chat_completion_payload",
    "chunk_payload",
    "estimate_input_tokens",
    "models_list_payload",
    "parse_chat_completion_request",
]
