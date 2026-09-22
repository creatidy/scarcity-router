"""OpenAI-compatible wire translation core (M04, issue #89).

ONE semantic implementation of the OpenAI chat-completions wire protocol,
shared by every transport that serves an OpenAI-compatible backend: the
server-direct HTTP adapter (M04, this program) and the worker-bridged path
(M05) invoke exactly these functions, so two transports are never two
implementations (``docs/providers.md``, execution-adapter discipline).

The module is deliberately import-clean of the execution coordinator and
of any transport: it translates, it never speaks HTTP and never routes.

Responsibilities:

- **Request build** (:func:`build_chat_completion_request`): the
  coordinator's validated :class:`~scarcity_router.gateway_adapters.AdapterCall`
  becomes one provider request document under a typed
  :class:`TranslationPolicy`. Provider differences are ONLY the policy's
  evidenced knobs (endpoint path, max-tokens field name, reasoning-control
  mapping, tool-choice/response-format restrictions, evidenced generation
  parameters). A request feature a preset does not evidence is EXPLICITLY
  rejected — never silently dropped and never forwarded on a guess.
- **Response parse** (:func:`parse_chat_completion_response`): strict,
  schema-validating parse of one ``chat.completion`` object into the
  normalized assistant message, finish reason and provider-reported
  usage. Unknown fields are tolerated; changed REQUIRED semantics are
  protocol drift (:class:`TranslationError`, fail closed).
- **SSE parse** (:class:`SseStreamParser`): incremental, bounded
  ``text/event-stream`` framing into JSON frames, tolerating the
  evidenced per-provider shape differences (usage on a final chunk with an
  empty choices array, on a final chunk with a repeated finish reason, or
  attached to the last content chunk).
- **Tool-call delta accumulation** (:class:`ToolCallAccumulator`):
  provider tool-call fragments are accumulated into COMPLETE tool calls —
  the normalized ``tool_call`` stream chunk and the coordinator both
  require complete calls, never fragments.
- **Error/usage mapping** (:func:`provider_error_note`,
  :func:`extract_usage`): provider statuses map to safe structural notes
  (bounded, vocabulary-checked tokens only — never provider message text,
  which may echo request content; never credentials).

Security: nothing here touches credentials, sockets or URLs beyond the
policy's static endpoint path; notes and drift messages carry parameter
NAMES and status codes only, never content values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from collections.abc import Mapping
from codecs import IncrementalDecoder, getincrementaldecoder
from typing import ClassVar, cast

from ..gateway_adapters import (
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_TOOL_CALLS,
    AdapterCall,
    AdapterMessage,
    AdapterToolCall,
)
from ..gateway_contracts import UsageTokens

# ── Frozen vocabularies ───────────────────────────────────────────────────────

#: The reasoning-effort values execution surface v1 accepts (M03 parser).
REQUEST_EFFORTS: frozenset[str] = frozenset({"minimal", "low", "medium", "high"})

_FINISH_MAP: Mapping[str, str] = {
    "stop": FINISH_STOP,
    "length": FINISH_LENGTH,
    "tool_calls": FINISH_TOOL_CALLS,
}

_SAFE_TOKEN_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)

#: Upper bound for one provider error code token copied into a note.
_MAX_ERROR_TOKEN_LENGTH = 64

#: Upper bound for one SSE event's data payload; a larger frame is drift.
MAX_SSE_FRAME_BYTES = 8 * 1024 * 1024

#: The empty-arguments normalization: a tool call without arguments is the
#: empty JSON object (the OpenAI client convention); the normalized tool-call
#: contract requires non-empty arguments text.
EMPTY_ARGUMENTS = "{}"

#: Structural request fields the translation core itself owns. A
#: ``generation_params`` entry carrying one of these names would overwrite
#: a field this module sets (or will set after future ingress extensions),
#: so it is rejected before anything is built — defense in depth, never a
#: silent overwrite path.
RESERVED_REQUEST_KEYS: frozenset[str] = frozenset({
    "model",
    "messages",
    "stream",
    "stream_options",
    "tools",
    "tool_choice",
    "response_format",
    "reasoning_effort",
    "reasoning",
    "thinking",
    "max_tokens",
    "max_completion_tokens",
})


class TranslationError(Exception):
    """A request-shape refusal or protocol-drift failure (fail closed).

    Raised by the translation core when a request feature has no evidenced
    mapping for the preset's policy or when a provider response violates
    the evidenced schema. Adapters translate this into a definitive
    :class:`~scarcity_router.gateway_adapters.AdapterPermanentError`.
    Messages name parameters and structure only — never content values.
    """


# ── Translation policy ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TranslationPolicy:
    """The evidenced provider differences of one preset (adapter edge).

    Every field is a decision with dated evidence behind it (see
    :mod:`scarcity_router.providers.openai_http_presets`); there is no
    "auto-detect provider" path and no per-provider code branch outside
    this policy. Knobs:

    - ``endpoint_path`` — the evidenced chat-completions path appended to
      the configured origin (paths are preset facts, never client input).
    - ``max_tokens_field`` — the evidenced output-limit field name
      (``max_completion_tokens`` for the OpenAI preset, ``max_tokens``
      where providers document only the legacy name).
    - ``developer_role`` — ``pass`` only where the provider documents the
      ``developer`` role; ``reject`` otherwise (explicit refusal, never a
      silent rewrite into another role).
    - ``tool_choice_policy`` — ``pass`` (documented parameter), ``auto_only``
      (only the default/auto value is evidenced; anything else is refused),
      or ``omit_auto`` (the parameter is undocumented; an explicit ``auto``
      — the de-facto default — is omitted as a documented mapping and any
      other value is refused).
    - ``response_format_policy`` — ``pass`` or ``json_object_only`` (only
      the JSON-mode formats are evidenced; ``json_schema`` is refused).
    - ``reasoning_policy`` / ``reasoning_value_map`` — the evidenced
      reasoning-control parameter shape and the documented value mapping
      from the surface's closed effort set.
    - ``unevidenced_generation_params`` — generation parameters the
      preset's evidence does not document; sending one is an explicit
      request-shape refusal (fail closed), never a silent forward.
    - ``omitted_generation_params`` — parameters the provider itself
      documents as removed/no-op; omitted as a documented mapping.
    - ``stream_options`` — ``request_include_usage`` sends
      ``stream_options.include_usage`` (documented); ``omit`` never sends
      the parameter (usage presence is then whatever the provider does,
      reported honestly).
    """

    preset_id: str
    endpoint_path: str
    max_tokens_field: str
    developer_role: str
    tool_choice_policy: str
    response_format_policy: str
    reasoning_policy: str
    reasoning_value_map: Mapping[str, str] = field(
        default_factory=lambda: dict[str, str]()
    )
    unevidenced_generation_params: frozenset[str] = frozenset()
    omitted_generation_params: frozenset[str] = frozenset()
    stream_options: str = "request_include_usage"
    requires_credential: bool = True
    discovery_path: str | None = None
    health_path: str | None = None

    _DEVELOPER_ROLES: ClassVar[frozenset[str]] = frozenset({"pass", "reject"})
    _TOOL_CHOICE_POLICIES: ClassVar[frozenset[str]] = frozenset({
        "pass",
        "auto_only",
        "omit_auto",
    })
    _RESPONSE_FORMAT_POLICIES: ClassVar[frozenset[str]] = frozenset({
        "pass",
        "json_object_only",
    })
    _REASONING_POLICIES: ClassVar[frozenset[str]] = frozenset({
        "reasoning_effort",
        "openrouter_reasoning",
        "thinking_deepseek",
        "thinking_zai",
    })
    _STREAM_OPTIONS: ClassVar[frozenset[str]] = frozenset({
        "request_include_usage",
        "omit",
    })

    def __post_init__(self) -> None:
        if not self.preset_id or len(self.preset_id) > 64:
            raise ValueError("translation_policy.preset_id: invalid preset id")
        if not self.endpoint_path.startswith("/") or any(
            character in self.endpoint_path for character in " ?#"
        ):
            raise ValueError(
                "translation_policy.endpoint_path: must be a path starting "
                + "with '/' (origin comes from configuration)"
            )
        if self.max_tokens_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError(
                "translation_policy.max_tokens_field: unsupported field name"
            )
        if self.developer_role not in self._DEVELOPER_ROLES:
            raise ValueError("translation_policy.developer_role: unsupported policy")
        if self.tool_choice_policy not in self._TOOL_CHOICE_POLICIES:
            raise ValueError("translation_policy.tool_choice_policy: unsupported")
        if self.response_format_policy not in self._RESPONSE_FORMAT_POLICIES:
            raise ValueError(
                "translation_policy.response_format_policy: unsupported policy"
            )
        if self.reasoning_policy not in self._REASONING_POLICIES:
            raise ValueError("translation_policy.reasoning_policy: unsupported policy")
        for requested, provider_value in self.reasoning_value_map.items():
            if requested not in REQUEST_EFFORTS:
                raise ValueError(
                    f"translation_policy.reasoning_value_map: unsupported "
                    + f"requested effort {requested!r}"
                )
            if provider_value.__class__ is not str or not provider_value:
                raise ValueError(
                    "translation_policy.reasoning_value_map: provider values "
                    + "must be non-empty strings"
                )
        if self.stream_options not in self._STREAM_OPTIONS:
            raise ValueError("translation_policy.stream_options: unsupported policy")

    def map_reasoning_effort(self, requested: str) -> str:
        mapped = self.reasoning_value_map.get(requested)
        if mapped is None:
            raise TranslationError(
                "reasoning_effort value has no evidenced mapping for preset "
                + f"{self.preset_id!r}"
            )
        return mapped


# ── Request build ─────────────────────────────────────────────────────────────


def _wire_message(message: AdapterMessage, policy: TranslationPolicy) -> dict[str, object]:
    role = message.role
    if role == "developer" and policy.developer_role == "reject":
        raise TranslationError(
            "preset does not evidence the 'developer' role; the message was "
            + "refused rather than silently rewritten"
        )
    wire: dict[str, object] = {"role": role, "content": message.content}
    if message.name is not None:
        wire["name"] = message.name
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return wire


def _wire_tool_choice(tool_choice: object, policy: TranslationPolicy) -> object:
    if tool_choice is None:
        return None
    if policy.tool_choice_policy == "pass":
        return tool_choice
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            if policy.tool_choice_policy == "omit_auto":
                return None  # documented omission: the de-facto default
            return tool_choice
        raise TranslationError(
            f"tool_choice {tool_choice!r} has no evidenced mapping for preset "
            + f"{policy.preset_id!r}"
        )
    raise TranslationError(
        "forced tool_choice has no evidenced mapping for preset "
        + f"{policy.preset_id!r}"
    )


def _wire_response_format(
    response_format: Mapping[str, object] | None, policy: TranslationPolicy
) -> Mapping[str, object] | None:
    if response_format is None:
        return None
    if policy.response_format_policy == "json_object_only":
        format_type = response_format.get("type")
        if format_type not in ("text", "json_object"):
            raise TranslationError(
                "response_format type has no evidenced mapping for preset "
                + f"{policy.preset_id!r}"
            )
    return response_format


def build_chat_completion_request(
    call: AdapterCall,
    policy: TranslationPolicy,
    *,
    wire_model: str | None = None,
) -> dict[str, object]:
    """Build one provider request document from an admitted dispatch.

    ``wire_model`` lets a resource binding carry the provider's exact model
    string when the registry's safe-id grammar cannot represent it; the
    default is the resource's physical model name. The request carries ONLY
    the features the preset's policy evidences; anything else is a
    :class:`TranslationError` before any byte is sent.
    """
    model = wire_model if wire_model is not None else call.model.model
    if not model:
        raise TranslationError("the dispatch carries no physical model name")
    wire: dict[str, object] = {
        "model": model,
        "messages": [
            _wire_message(message, policy) for message in call.messages
        ],
        "stream": call.stream,
    }
    if call.tools:
        wire["tools"] = list(call.tools)
    tool_choice = _wire_tool_choice(call.tool_choice, policy)
    if tool_choice is not None:
        wire["tool_choice"] = tool_choice
    response_format = _wire_response_format(call.response_format, policy)
    if response_format is not None:
        wire["response_format"] = dict(response_format)
    if call.reasoning_effort is not None:
        mapped_effort = policy.map_reasoning_effort(call.reasoning_effort)
        if policy.reasoning_policy == "reasoning_effort":
            wire["reasoning_effort"] = mapped_effort
        elif policy.reasoning_policy == "openrouter_reasoning":
            wire["reasoning"] = {"effort": mapped_effort}
        elif policy.reasoning_policy == "thinking_deepseek":
            wire["thinking"] = {
                "type": "enabled",
                "reasoning_effort": mapped_effort,
            }
        elif policy.reasoning_policy == "thinking_zai":
            wire["thinking"] = {"type": "enabled"}
            wire["reasoning_effort"] = mapped_effort
    if call.max_output_tokens is not None:
        wire[policy.max_tokens_field] = call.max_output_tokens
    for name, value in call.generation_params.items():
        if name in RESERVED_REQUEST_KEYS:
            raise TranslationError(
                f"generation parameter {name!r} collides with a structural "
                + "request field; refused rather than overwritten"
            )
        if name in policy.unevidenced_generation_params:
            raise TranslationError(
                f"generation parameter {name!r} is not evidenced for preset "
                + f"{policy.preset_id!r}; refused rather than silently forwarded"
            )
        if name in policy.omitted_generation_params:
            continue  # documented omission (provider removed/no-op semantics)
        wire[name] = value
    if call.stream and policy.stream_options == "request_include_usage":
        wire["stream_options"] = {"include_usage": True}
    return wire


# ── JSON narrowing helpers ────────────────────────────────────────────────────


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    return None


def _as_list(value: object) -> list[object] | None:
    if isinstance(value, list):
        return cast("list[object]", value)
    return None


def _as_str(value: object) -> str | None:
    if value.__class__ is str:
        return cast("str", value)
    return None


def safe_diagnostic_token(value: object, *, max_len: int = _MAX_ERROR_TOKEN_LENGTH) -> str | None:
    """A bounded, vocabulary-checked provider-supplied token (never free text).

    Used for every provider-controlled string that may enter a diagnostic
    note: error codes and backend version strings. Free text, overlong
    values and unsafe characters yield ``None`` — callers substitute a
    structural note instead of copying the provider's bytes.
    """
    token: str | None
    if isinstance(value, str):
        token = value
    elif isinstance(value, int) and not isinstance(value, bool):
        token = str(value)
    else:
        return None
    if not token or len(token) > max_len:
        return None
    if not set(token) <= _SAFE_TOKEN_CHARACTERS:
        return None
    return token


# ── Usage extraction ──────────────────────────────────────────────────────────


def extract_usage(raw_usage: object) -> UsageTokens | None:
    """Extract provider-reported usage; ``None`` means unavailable.

    ``prompt_tokens``/``completion_tokens`` must be non-negative integers
    when the object is present at all — a present-but-malformed usage
    object is protocol drift (fail closed), while an absent one is honest
    unavailability, never a fabricated zero.
    """
    if raw_usage is None:
        return None
    usage = _as_mapping(raw_usage)
    if usage is None:
        raise TranslationError("provider usage is not an object")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int):
        raise TranslationError(
            "provider usage field 'prompt_tokens' is not a non-negative integer"
        )
    if isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int):
        raise TranslationError(
            "provider usage field 'completion_tokens' is not a non-negative integer"
        )
    if prompt_tokens < 0 or completion_tokens < 0:
        raise TranslationError("provider usage counts must be non-negative")
    return UsageTokens(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


# ── Finish-reason normalization ───────────────────────────────────────────────


def normalize_finish_reason(raw: object) -> str:
    """Map a provider finish reason onto the closed normalized vocabulary.

    Only the evidenced OpenAI-compatible values map; anything else
    (``content_filter``, provider-specific tokens such as ``sensitive``,
    ``aborted`` or ``error``) is protocol drift — the failure is explicit
    and the response is never silently re-labeled as a normal stop.
    """
    finish = _as_str(raw)
    if finish is None:
        raise TranslationError("provider finish_reason is not a string")
    mapped = _FINISH_MAP.get(finish)
    if mapped is None:
        raise TranslationError(
            f"provider finish_reason {finish!r} has no evidenced normalized "
            + "mapping"
        )
    return mapped


# ── Non-streaming response parse ──────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedCompletion:
    """One parsed ``chat.completion`` response (normalized, provider-free)."""

    message: AdapterMessage
    finish_reason: str
    usage: UsageTokens | None


def _wire_tool_call_to_normalized(raw_call: object) -> AdapterToolCall:
    call = _as_mapping(raw_call)
    if call is None:
        raise TranslationError("provider tool_call is not an object")
    call_id = _as_str(call.get("id"))
    if not call_id:
        raise TranslationError("provider tool_call has no id")
    if call.get("type") != "function":
        raise TranslationError("provider tool_call is not a function call")
    function = _as_mapping(call.get("function"))
    if function is None:
        raise TranslationError("provider tool_call has no function object")
    name = _as_str(function.get("name"))
    if not name:
        raise TranslationError("provider tool_call has no function name")
    arguments = _as_str(function.get("arguments"))
    if not arguments:
        # A tool call without argument text is the empty JSON object (the
        # OpenAI client convention); the normalized contract requires
        # non-empty arguments text.
        arguments = EMPTY_ARGUMENTS
    return AdapterToolCall(id=call_id, name=name, arguments=arguments)


def parse_chat_completion_response(
    document: object, policy: TranslationPolicy
) -> ParsedCompletion:
    """Strictly parse one ``chat.completion`` object; drift fails closed.

    The policy is accepted for seam symmetry (every translation entry
    point takes the preset's policy) even though the non-streaming
    response schema is provider-independent today.
    """
    _ = policy
    body = _as_mapping(document)
    if body is None:
        raise TranslationError("provider response is not a JSON object")
    choices = _as_list(body.get("choices"))
    if not choices:
        raise TranslationError("provider response carries no choices array")
    first = _as_mapping(choices[0])
    if first is None:
        raise TranslationError("provider choice is not an object")
    message = _as_mapping(first.get("message"))
    if message is None:
        raise TranslationError("provider choice carries no message object")
    role = _as_str(message.get("role"))
    if role != "assistant":
        raise TranslationError("provider message role is not 'assistant'")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise TranslationError("provider message content is not a string or null")
    raw_calls = _as_list(message.get("tool_calls"))
    tool_calls: tuple[AdapterToolCall, ...] = ()
    if raw_calls is not None:
        tool_calls = tuple(
            _wire_tool_call_to_normalized(raw_call) for raw_call in raw_calls
        )
    if content is None and not tool_calls:
        raise TranslationError(
            "provider message carries neither content nor tool_calls"
        )
    if "finish_reason" not in first:
        raise TranslationError("provider response carries no finish_reason")
    finish_reason = normalize_finish_reason(first.get("finish_reason"))
    usage = extract_usage(body.get("usage"))
    return ParsedCompletion(
        message=AdapterMessage(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
        ),
        finish_reason=finish_reason,
        usage=usage,
    )


# ── SSE stream parsing ────────────────────────────────────────────────────────


class SseStreamParser:
    """Incremental bounded ``text/event-stream`` parser.

    Feeds transport bytes in, yields parsed JSON frames out. Framing
    follows the SSE rules the evidenced providers rely on: events are
    separated by blank lines, ``data:`` lines carry the payload, comment
    and other field lines are ignored (OpenRouter documents SSE comments
    that must be ignored), and the ``data: [DONE]`` sentinel ends the
    stream. Non-UTF-8 bytes, oversized frames and malformed JSON payloads
    are protocol drift (:class:`TranslationError`, fail closed).
    """

    _decoder: IncrementalDecoder
    _buffer: list[str]
    _data_lines: list[str]
    _total_bytes: int
    _frame_bytes: int
    _max_total_bytes: int
    _max_frame_bytes: int
    finished: bool

    def __init__(
        self,
        *,
        max_total_bytes: int,
        max_frame_bytes: int = MAX_SSE_FRAME_BYTES,
    ) -> None:
        self._decoder = getincrementaldecoder("utf-8")()
        self._buffer = []
        self._data_lines = []
        self._total_bytes = 0
        self._frame_bytes = 0
        self._max_total_bytes = max_total_bytes
        self._max_frame_bytes = max_frame_bytes
        self.finished = False

    def feed(self, data: bytes) -> tuple[dict[str, object], ...]:
        """Consume transport bytes; return the complete frames they yielded."""
        self._total_bytes += len(data)
        if self._total_bytes > self._max_total_bytes:
            raise TranslationError("provider stream exceeds the bounded size")
        text = self._decoder.decode(data).replace("\r\n", "\n").replace("\r", "\n")
        self._buffer.append(text)
        combined = "".join(self._buffer)
        frames: list[dict[str, object]] = []
        start = 0
        while True:
            index = combined.find("\n", start)
            if index < 0:
                break
            frame = self._line(combined[start:index])
            if frame is not None:
                frames.append(frame)
            start = index + 1
        self._buffer = [combined[start:]]
        return tuple(frames)

    def close(self) -> tuple[dict[str, object], ...]:
        """Flush the decoder tail; a truncated final line is drift."""
        tail = self._decoder.decode(b"", final=True)
        tail = tail.replace("\r\n", "\n").replace("\r", "\n")
        remainder = "".join(self._buffer) + tail
        self._buffer = []
        if remainder and "\n" not in remainder and remainder.strip():
            raise TranslationError("provider stream ended mid-event")
        frames: list[dict[str, object]] = []
        for line in remainder.split("\n"):
            frame = self._line(line)
            if frame is not None:
                frames.append(frame)
        return tuple(frames)

    def _line(self, line: str) -> dict[str, object] | None:
        if self.finished:
            return None  # the [DONE] sentinel ends the stream; ignore the rest
        if line == "":
            return self._dispatch_event()
        if line.startswith(":"):
            return None  # SSE comment (OpenRouter documents these)
        if line.startswith("data:"):
            payload = line[len("data:") :]
            if payload.startswith(" "):
                payload = payload[1:]
            self._frame_bytes += len(payload)
            if self._frame_bytes > self._max_frame_bytes:
                raise TranslationError("provider stream event exceeds the bounded size")
            self._data_lines.append(payload)
        # ``event:``, ``id:``, ``retry:`` and unknown fields carry no
        # evidenced semantics for this surface; tolerated and ignored.
        return None

    def _dispatch_event(self) -> dict[str, object] | None:
        self._frame_bytes = 0
        data_lines = self._data_lines
        self._data_lines = []
        if not data_lines:
            return None
        payload = "\n".join(data_lines)
        if payload.strip() == "[DONE]":
            self.finished = True
            return None
        try:
            frame: object = cast("object", json.loads(payload))
        except ValueError as exc:
            raise TranslationError("provider stream event is not valid JSON") from exc
        frame_mapping = _as_mapping(frame)
        if frame_mapping is None:
            raise TranslationError("provider stream event is not a JSON object")
        return dict(frame_mapping)


# ── Stream frame interpretation ───────────────────────────────────────────────


class ToolCallAccumulator:
    """Accumulates provider tool-call deltas into COMPLETE calls.

    OpenAI-style providers stream a tool call as fragments
    (``delta.tool_calls`` entries carrying an index, the id/name on the
    first fragment and ``function.arguments`` increments). The normalized
    adapter vocabulary emits only COMPLETE tool calls, so fragments are
    accumulated here and flushed once, in index order, at stream end.
    Conflicting ids or names for one index, an absent-then-present index
    mix, or a call that ends without an id or a name are drift.
    """

    _calls: dict[int, dict[str, str]]
    _order: list[int]
    _saw_explicit_index: bool
    _saw_implicit_index: bool

    def __init__(self) -> None:
        self._calls = {}
        self._order = []
        self._saw_explicit_index = False
        self._saw_implicit_index = False

    def add_fragment(self, raw_fragment: object) -> None:
        fragment = _as_mapping(raw_fragment)
        if fragment is None:
            raise TranslationError("provider tool_call delta is not an object")
        raw_index = fragment.get("index")
        if raw_index is None:
            index = 0
            self._saw_implicit_index = True
        else:
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise TranslationError("provider tool_call index is not an integer")
            if raw_index < 0:
                raise TranslationError("provider tool_call index is negative")
            self._saw_explicit_index = True
            index = raw_index
        if index in self._calls:
            call = self._calls[index]
        else:
            call = {"id": "", "name": "", "arguments": ""}
            self._calls[index] = call
            self._order.append(index)
        call_id = _as_str(fragment.get("id"))
        if call_id:
            if call["id"] and call["id"] != call_id:
                raise TranslationError(
                    "provider tool_call fragments disagree on the call id"
                )
            call["id"] = call_id
        function = _as_mapping(fragment.get("function"))
        if function is not None:
            name = _as_str(function.get("name"))
            if name:
                if call["name"] and call["name"] != name:
                    raise TranslationError(
                        "provider tool_call fragments disagree on the function name"
                    )
                call["name"] = name
            arguments = function.get("arguments")
            if arguments is not None:
                arguments_text = _as_str(arguments)
                if arguments_text is None:
                    raise TranslationError(
                        "provider tool_call arguments fragment is not a string"
                    )
                call["arguments"] += arguments_text

    def complete(self) -> tuple[AdapterToolCall, ...]:
        if self._saw_explicit_index and self._saw_implicit_index:
            raise TranslationError(
                "provider tool_call fragments mix indexed and unindexed "
                + "deltas; call boundaries are ambiguous"
            )
        calls: list[AdapterToolCall] = []
        for index in sorted(self._order):
            call = self._calls[index]
            if not call["id"]:
                raise TranslationError("provider tool_call ended without an id")
            if not call["name"]:
                raise TranslationError("provider tool_call ended without a name")
            arguments = call["arguments"] if call["arguments"] else EMPTY_ARGUMENTS
            calls.append(
                AdapterToolCall(id=call["id"], name=call["name"], arguments=arguments)
            )
        return tuple(calls)


@dataclass(frozen=True)
class StreamFrameView:
    """One parsed SSE frame's meaning for the adapter."""

    text_delta: str | None = None
    tool_fragments: tuple[object, ...] = ()
    finish_reason: str | None = None
    usage: UsageTokens | None = None


def interpret_stream_frame(frame: Mapping[str, object]) -> StreamFrameView:
    """Extract one frame's deltas, finish reason and usage (tolerant).

    Shape differences the evidence documents are all tolerated here:
    usage may ride on a final frame with an EMPTY choices array (OpenAI),
    on a final frame whose choices repeat the finish reason (OpenRouter),
    or on the last content frame (DeepSeek). Content beyond the first
    choice is ignored (the surface executes exactly one choice, ``n`` is
    not accepted at ingress).
    """
    text_delta: str | None = None
    tool_fragments: list[object] = []
    finish_reason: str | None = None
    usage = extract_usage(frame.get("usage"))
    choices = _as_list(frame.get("choices"))
    if choices:
        first = _as_mapping(choices[0])
        if first is not None:
            delta = _as_mapping(first.get("delta"))
            if delta is not None:
                raw_content = delta.get("content")
                if raw_content is not None:
                    content = _as_str(raw_content)
                    if content is None:
                        raise TranslationError(
                            "provider stream content delta is not a string"
                        )
                    text_delta = content
                raw_tool_calls = _as_list(delta.get("tool_calls"))
                if raw_tool_calls is not None:
                    tool_fragments = list(raw_tool_calls)
            raw_finish = first.get("finish_reason")
            if raw_finish is not None:
                finish_reason = normalize_finish_reason(raw_finish)
    return StreamFrameView(
        text_delta=text_delta,
        tool_fragments=tuple(tool_fragments),
        finish_reason=finish_reason,
        usage=usage,
    )


# ── Provider error notes ──────────────────────────────────────────────────────


def provider_error_note(status: int, document: object) -> str:
    """A safe structural note for a provider error response.

    Extracts at most a bounded, vocabulary-checked error CODE token from
    the evidenced envelopes (the OpenAI ``error.code``/``error.type``
    shape or the Z.ai ``{code, message}`` shape). Provider message TEXT is
    never copied — it is unbounded upstream content that may echo request
    material — and credentials never appear anywhere in a note.
    """
    token: str | None = None
    body = _as_mapping(document)
    if body is not None:
        error = _as_mapping(body.get("error"))
        if error is not None:
            token = safe_diagnostic_token(error.get("code")) or safe_diagnostic_token(
                error.get("type")
            )
        if token is None:
            token = safe_diagnostic_token(body.get("code"))
    if token is not None:
        return f"provider returned HTTP {status} (provider error code: {token})"
    return f"provider returned HTTP {status}"


__all__ = [
    "EMPTY_ARGUMENTS",
    "MAX_SSE_FRAME_BYTES",
    "REQUEST_EFFORTS",
    "RESERVED_REQUEST_KEYS",
    "ParsedCompletion",
    "SseStreamParser",
    "StreamFrameView",
    "ToolCallAccumulator",
    "TranslationError",
    "TranslationPolicy",
    "build_chat_completion_request",
    "extract_usage",
    "interpret_stream_frame",
    "normalize_finish_reason",
    "parse_chat_completion_response",
    "provider_error_note",
    "safe_diagnostic_token",
]
