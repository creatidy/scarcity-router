"""Responses surface 1: bounded stateless wire mapping, never routing (D-080).

The source-shaped subset is documented in execution-surface.md. Unsupported
state, modalities, phases, hosted tools and reasoning replay refuse before the
shared semantic coordinator. This module performs no provider or filesystem I/O.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast, final

from .gateway_adapters import (
    CHUNK_FINISH, CHUNK_REASONING_DELTA, CHUNK_TEXT_DELTA, CHUNK_TOOL_CALL, CHUNK_USAGE,
    AdapterStreamChunk, AdapterToolCall, CompletionOutcome, FINISH_CANCELLED, FINISH_LENGTH, FINISH_STOP, FINISH_TOOL_CALLS,
    MAX_STREAM_DELTA_CHARS,
)
from .gateway_contracts import GatewayError
from .gateway_openai import ChatCompletionRequest, MAX_MESSAGES, MAX_TOOLS, parse_chat_completion_request
from .gateway_validation import v_str_object_mapping

RESPONSES_SURFACE_VERSION = 1
MAX_RESPONSE_CHARS = 16_777_216

_REQUEST_KEYS = frozenset({
    "model", "input", "store", "stream", "stream_options", "tools", "tool_choice",
    "text", "reasoning", "max_output_tokens", "temperature", "top_p", "metadata",
    "parallel_tool_calls", "background", "truncation", "execution_requirements",
})
_ROLES = frozenset({"system", "developer", "user", "assistant"})
# Official SDK3.37.0 has max and xhigh as distinct values; the shared Chat
# compatibility vocabulary also has ultra, which this wire does not invent.
_WIRE_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})


def _invalid(param: str, message: str = "invalid or unsupported Responses parameter") -> GatewayError:
    return GatewayError.invalid_request(message, code="unsupported_parameter", param=param)


def _object(
    value: object, allowed: set[str] | frozenset[str], required: set[str], param: str,
) -> dict[str, object]:
    try:
        document = v_str_object_mapping(value, param)
    except ValueError:
        raise _invalid(param) from None
    if set(document) - allowed or not required <= set(document):
        raise _invalid(param)
    return dict(document)


def _text(value: object, param: str, *, limit: int = MAX_RESPONSE_CHARS, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise _invalid(param)
    return value


def _content(value: object, role: str) -> str:
    if isinstance(value, str):
        return _text(value, "input", empty=True)
    if not isinstance(value, list):
        raise _invalid("input", "only single text parts are supported; multipart input is not flattened")
    parts = cast(list[object], value)
    if len(parts) != 1:
        raise _invalid("input", "only single text parts are supported; multipart input is not flattened")
    part = _object(parts[0], {"type", "text", "annotations", "logprobs"}, {"type", "text"}, "input")
    kind = part["type"]
    if kind != "input_text" and not (role == "assistant" and kind == "output_text"):
        raise _invalid("input")
    if part.get("annotations") not in (None, []) or part.get("logprobs") not in (None, []):
        raise _invalid("input", "annotated or probabilistic history is not representable")
    return _text(part["text"], "input", empty=True)


def _messages(value: object) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    if isinstance(value, str):
        return [{"role": "user", "content": _text(value, "input")}], ()
    if not isinstance(value, list):
        raise _invalid("input")
    items = cast(list[object], value)
    if not items or len(items) > MAX_MESSAGES:
        raise _invalid("input")
    messages: list[dict[str, object]] = []
    identifiers: list[str] = []
    calls: set[str] = set()
    answered: set[str] = set()
    for raw in items:
        item = _object(raw, {
            "type", "role", "content", "id", "phase", "status", "call_id",
            "name", "arguments", "output", "async", "caller", "namespace",
        }, set(), "input")
        kind = item.get("type", "message")
        if kind == "message":
            item = _object(item, {"type", "role", "content", "id", "phase", "status"}, {"role", "content"}, "input")
            role = _text(item["role"], "input", limit=32)
            if role not in _ROLES or item.get("phase") is not None or item.get("status", "completed") != "completed":
                raise _invalid("input", "roles, phase and complete-history semantics must be representable")
            if calls != answered:
                raise _invalid("input", "a prior function call has no result; history is not repaired")
            messages.append({"role": role, "content": _content(item["content"], role)})
        elif kind == "function_call":
            item = _object(item, {
                "type", "id", "call_id", "name", "arguments", "status", "async", "caller", "namespace",
            }, {"type", "call_id", "name", "arguments"}, "input")
            if item.get("status") not in (None, "completed") or item.get("async") not in (None, False):
                raise _invalid("input")
            if "async" in item and item["async"] is not None and type(item["async"]) is not bool:
                raise _invalid("input")
            if item.get("caller") is not None or item.get("namespace") is not None:
                raise _invalid("input")
            call_id = _text(item["call_id"], "input", limit=256)
            if call_id in calls:
                raise _invalid("input", "duplicate function call identity")
            calls.add(call_id)
            if not messages or messages[-1]["role"] != "assistant":
                messages.append({"role": "assistant", "content": None, "tool_calls": []})
            previous = messages[-1]
            tool_calls = cast(list[dict[str, object]], previous.setdefault("tool_calls", []))
            tool_calls.append({"id": call_id, "type": "function", "function": {
                "name": _text(item["name"], "input", limit=256),
                "arguments": _text(item["arguments"], "input", limit=1_048_576),
            }})
        elif kind == "function_call_output":
            item = _object(item, {"type", "id", "call_id", "output"}, {"type", "call_id", "output"}, "input")
            call_id = _text(item["call_id"], "input", limit=256)
            if call_id not in calls or call_id in answered:
                raise _invalid("input", "orphaned or repeated function result; call identity is never guessed")
            answered.add(call_id)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": _text(item["output"], "input", empty=True)})
        else:
            raise _invalid("input", "state references, reasoning replay and hosted items are not supported")
        if item.get("id") is not None:
            identity = _text(item["id"], "input", limit=256)
            if identity in identifiers:
                raise _invalid("input", "duplicate concrete input item identity")
            identifiers.append(identity)
    if not messages or calls != answered:
        raise _invalid("input", "complete function history is required; missing results are not invented")
    return messages, tuple(identifiers)


@dataclass(frozen=True)
class ResponsesRequest:
    """Validated wire facts plus the existing canonical semantic request."""

    canonical: ChatCompletionRequest
    tools: tuple[Mapping[str, object], ...]
    text_format: Mapping[str, object]
    tool_choice: object
    parallel_tool_calls: bool
    input_item_ids: tuple[str, ...]
    include_obfuscation: bool


def _parse(value: object) -> tuple[ResponsesRequest, dict[str, object]]:
    document = _object(value, _REQUEST_KEYS, {"model", "input", "store"}, "request")
    try:
        # The internal API has the same JSON/UTF-8 boundary as HTTP. Validate,
        # do not sanitize or persist a second representation of the input.
        _ = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid("request", "Responses input must be finite, representable UTF-8 JSON") from None
    if document["store"] is not False:
        raise _invalid("store", "store=false is required; response storage and lookup are not supported")
    if document.get("background", False) is not False or document.get("truncation", "disabled") != "disabled":
        raise _invalid("request", "background execution and truncation are not supported")
    stream = document.get("stream", False)
    if type(stream) is not bool:
        raise _invalid("stream")
    include_obfuscation = True
    if "stream_options" in document:
        if stream is not True:
            raise _invalid("stream_options")
        options = _object(document["stream_options"], {"include_obfuscation"}, set(), "stream_options")
        flag = options.get("include_obfuscation", True)
        if type(flag) is not bool:
            raise _invalid("stream_options")
        include_obfuscation = flag
    messages, identifiers = _messages(document["input"])
    canonical: dict[str, object] = {"model": document["model"], "messages": messages, "stream": stream}
    tools: list[Mapping[str, object]] = []
    if "tools" in document:
        raw_tools = document["tools"]
        if not isinstance(raw_tools, list):
            raise _invalid("tools")
        tool_values = cast(list[object], raw_tools)
        if len(tool_values) > MAX_TOOLS:
            raise _invalid("tools")
        converted: list[dict[str, object]] = []
        for raw in tool_values:
            tool = _object(raw, {"type", "name", "parameters", "strict", "description"}, {"type", "name", "parameters", "strict"}, "tools")
            if tool["type"] != "function" or type(tool["strict"]) is not bool:
                raise _invalid("tools", "only explicit synchronous client function schemas are supported")
            if not isinstance(tool["parameters"], Mapping):
                raise _invalid("tools")
            description = tool.get("description")
            if description is not None and not isinstance(description, str):
                raise _invalid("tools")
            tools.append(dict(tool))
            converted.append({"type": "function", "function": {key: item for key, item in tool.items() if key != "type"}})
        canonical["tools"] = converted
    parallel = document.get("parallel_tool_calls", True)
    if type(parallel) is not bool:
        raise _invalid("parallel_tool_calls")
    # The wire default permits parallel calls; it does not require a serial
    # backend to enforce an otherwise absent generation control. Explicit
    # controls remain explicit so unsupported sources refuse before inference.
    if "parallel_tool_calls" in document:
        canonical["parallel_tool_calls"] = parallel
    choice = document.get("tool_choice", "auto")
    wire_choice: object
    if isinstance(choice, Mapping):
        forced = _object(cast(Mapping[object, object], choice), {"type", "name"}, {"type", "name"}, "tool_choice")
        if forced["type"] != "function" or not any(tool["name"] == forced["name"] for tool in tools):
            raise _invalid("tool_choice")
        canonical["tool_choice"] = {"type": "function", "function": {"name": forced["name"]}}
        wire_choice = dict(forced)
    elif not isinstance(choice, str) or choice not in ("auto", "none", "required"):
        raise _invalid("tool_choice")
    else:
        if choice == "required" and not tools:
            raise _invalid("tool_choice")
        wire_choice = choice
        if tools or "tool_choice" in document:
            canonical["tool_choice"] = choice
    text_format: dict[str, object] = {"type": "text"}
    if "text" in document:
        text = _object(document["text"], {"format"}, {"format"}, "text")
        text_format = _object(text["format"], {"type", "name", "schema", "strict", "description"}, {"type"}, "text")
        if text_format["type"] == "json_schema":
            text_format = _object(text_format, {"type", "name", "schema", "strict", "description"}, {"type", "name", "schema"}, "text")
            name = _text(text_format["name"], "text", limit=64)
            if re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
                raise _invalid("text")
            try:
                _ = v_str_object_mapping(text_format["schema"], "text")
            except ValueError:
                raise _invalid("text") from None
            strict = text_format.get("strict")
            if strict is not None and type(strict) is not bool:
                raise _invalid("text")
            if "description" in text_format:
                _ = _text(text_format["description"], "text", limit=65_536, empty=True)
            canonical["response_format"] = {"type": "json_schema", "json_schema": {key: item for key, item in text_format.items() if key != "type"}}
        elif text_format["type"] in ("text", "json_object") and set(text_format) == {"type"}:
            canonical["response_format"] = dict(text_format)
        else:
            raise _invalid("text")
    if "reasoning" in document:
        reasoning = _object(document["reasoning"], {"effort"}, {"effort"}, "reasoning")
        effort = reasoning["effort"]
        if not isinstance(effort, str) or effort not in _WIRE_EFFORTS:
            raise _invalid("reasoning")
        canonical["reasoning_effort"] = effort
    if "max_output_tokens" in document:
        canonical["max_completion_tokens"] = document["max_output_tokens"]
    for key in ("temperature", "top_p", "execution_requirements"):
        if key in document:
            canonical[key] = document[key]
    if "metadata" in document:
        metadata = document["metadata"]
        if not isinstance(metadata, Mapping):
            raise _invalid("metadata")
        metadata_values = cast(Mapping[object, object], metadata)
        if len(metadata_values) > 16:
            raise _invalid("metadata")
        for key, item in metadata_values.items():
            _ = _text(key, "metadata", limit=64)
            _ = _text(item, "metadata", limit=512)
        canonical["metadata"] = dict(cast(Mapping[str, object], metadata))
    try:
        parsed = parse_chat_completion_request(canonical)
    except (GatewayError, ValueError) as exc:
        # The other wire's dynamic parameter paths must not reflect input keys.
        code = exc.code if isinstance(exc, GatewayError) else "unsupported_parameter"
        raise GatewayError.invalid_request("invalid or unsupported Responses request", code=code, param="request") from None
    return ResponsesRequest(parsed, tuple(tools), dict(text_format), copy.deepcopy(wire_choice), parallel, identifiers, include_obfuscation), canonical


def parse_responses_request(value: object) -> ResponsesRequest:
    return _parse(value)[0]


def prepare_executable_response(
    response: object, *, expected_request: object, request: dict[str, object],
    require_bound: bool = False,
) -> dict[str, object]:
    """Reuse D-079 identity/demand checks without manual wire-field copying.

    Initial preparation omits bound model/context/effort like the existing Chat
    helper. Final validation uses require_bound=True and never repairs loss.
    Original Responses history/IDs/format fields are not rewritten into Chat.
    """
    from .executable_client import prepare_executable_completion

    probe = dict(request)
    unbound = "model" not in probe
    if unbound:
        probe["model"] = "unbound-responses"
    _parsed, canonical = _parse(probe)
    if unbound:
        del canonical["model"]
    prepared = prepare_executable_completion(
        response, expected_request=expected_request, completion=canonical,
        require_bound=require_bound,
    )
    out = copy.deepcopy(request)
    out["model"] = prepared["model"]
    out["execution_requirements"] = copy.deepcopy(prepared["execution_requirements"])
    if "reasoning_effort" in prepared:
        out["reasoning"] = {"effort": prepared["reasoning_effort"]}
    if "max_completion_tokens" in prepared:
        out["max_output_tokens"] = prepared["max_completion_tokens"]
    _ = parse_responses_request(out)
    return out


def _check_call(request: ResponsesRequest, call: AdapterToolCall) -> None:
    names = {cast(str, tool["name"]) for tool in request.tools}
    choice = request.tool_choice
    forced = cast(Mapping[str, object], choice).get("name") if isinstance(choice, Mapping) else None
    if call.name not in names or choice == "none" or (forced is not None and forced != call.name):
        raise GatewayError.api("unexpected Responses function identity", code="unsupported_response", http_status=502)


def _output_text(value: str) -> None:
    try:
        _ = value.encode("utf-8")
    except UnicodeError:
        raise GatewayError.api("unrepresentable Responses output", code="unsupported_response", http_status=502) from None


def response_payload(
    request: ResponsesRequest, outcome: CompletionOutcome,
    *, output: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Render actual semantic output, with unknown detailed usage left null."""
    if outcome.message.role != "assistant":
        raise GatewayError.api("unsupported Responses output role", code="unsupported_response", http_status=502)
    if len(outcome.message.tool_calls) > MAX_TOOLS or len({call.id for call in outcome.message.tool_calls}) != len(outcome.message.tool_calls):
        raise GatewayError.api("unsupported Responses function output", code="unsupported_response", http_status=502)
    calls = outcome.message.tool_calls
    complete = outcome.finish_reason not in (FINISH_CANCELLED, FINISH_LENGTH)
    required = request.tool_choice == "required" or isinstance(request.tool_choice, Mapping)
    if (
        (not request.parallel_tool_calls and len(calls) > 1)
        or (outcome.finish_reason == FINISH_TOOL_CALLS and not calls)
        or (outcome.finish_reason == FINISH_STOP and bool(calls))
        or (complete and required and not calls)
    ):
        raise GatewayError.api("inconsistent Responses function completion", code="unsupported_response", http_status=502)
    for call in calls:
        _check_call(request, call)
        for value in (call.id, call.name, call.arguments):
            _output_text(value)
    for value in (outcome.message.content, outcome.message.reasoning):
        if value is not None:
            _output_text(value)
    status = "cancelled" if outcome.finish_reason == FINISH_CANCELLED else "incomplete" if outcome.finish_reason == FINISH_LENGTH else "completed"
    item_status = "completed" if status == "completed" else "incomplete"
    if output is None:
        output = []
        size = len(outcome.message.content or "") + len(outcome.message.reasoning or "")
        size += sum(len(call.id) + len(call.name) + len(call.arguments) for call in outcome.message.tool_calls)
        if size > MAX_RESPONSE_CHARS:
            raise GatewayError.api("Responses output exceeds the bounded surface limit", code="unsupported_response", http_status=502)
        if outcome.message.reasoning is not None:
            output.append({"type": "reasoning", "id": f"rs-{outcome.request_id}", "status": item_status, "summary": [],
                           "content": [{"type": "reasoning_text", "text": outcome.message.reasoning}]})
        if outcome.message.content is not None:
            output.append({"type": "message", "id": f"msg-{outcome.request_id}", "role": "assistant", "status": item_status,
                           "content": [{"type": "output_text", "text": outcome.message.content, "annotations": [], "logprobs": []}]})
        for index, call in enumerate(outcome.message.tool_calls):
            output.append({"type": "function_call", "id": f"fc-{outcome.request_id}-{index}", "call_id": call.id,
                           "name": call.name, "arguments": call.arguments, "status": item_status})
    return _response_fields(request, outcome.request_id, outcome.created, status, output, outcome)


def _response_fields(
    request: ResponsesRequest, response_id: str, created: int, status: str,
    output: list[dict[str, object]], outcome: CompletionOutcome | None = None,
) -> dict[str, object]:
    canonical = request.canonical
    return {
        "id": response_id, "object": "response", "created_at": created,
        "status": status, "error": None,
        "incomplete_details": {"reason": None} if status == "incomplete" else None,
        "model": canonical.model if outcome is None else outcome.model_echo,
        "output": output, "store": False, "background": False,
        "previous_response_id": None, "instructions": None, "truncation": "disabled",
        "parallel_tool_calls": request.parallel_tool_calls,
        "tools": [dict(tool) for tool in request.tools], "tool_choice": copy.deepcopy(request.tool_choice),
        "text": {"format": dict(request.text_format)},
        "reasoning": None if canonical.reasoning_effort is None else {"effort": canonical.reasoning_effort},
        "max_output_tokens": canonical.capabilities.requested_output_tokens,
        "temperature": (canonical.generation_params or {}).get("temperature"),
        "top_p": (canonical.generation_params or {}).get("top_p"),
        "metadata": None if canonical.metadata is None else dict(canonical.metadata),
        "usage": None,
        "x_scarcity_router": {
            "responses_surface_version": RESPONSES_SURFACE_VERSION,
            "request_id": response_id, "decision_id": None if outcome is None else outcome.decision_id,
            "finish_reason": None if outcome is None else outcome.finish_reason,
            "usage_accounting": None if outcome is None else outcome.usage.to_dict(),
        },
    }


@final
class ResponsesStreamEvents:
    """Bounded wire-item accumulation, not an execution/session coordinator.

    Only normalized adapter facts are consumed. Success is emitted after the
    actual CompletionOutcome, never merely because a source sent a finish chunk.
    """

    def __init__(self, request: ResponsesRequest, *, response_id: str, created: int) -> None:
        self.request = request
        self.response_id = response_id
        self.created = created
        self.output: list[dict[str, object]] = []
        self._parts: dict[str, int] = {}
        self._text: dict[str, list[str]] = {"text": [], "reasoning": []}
        self._tools: list[AdapterToolCall] = []
        self._usage: dict[str, int] | None = None
        self._usage_updates = 0
        self._sequence = 0
        self._size = 0
        self._chunks = 0
        self._started = False
        self._done = False
        self._finish_reason: str | None = None

    def _error(self) -> GatewayError:
        return GatewayError.api("unsupported or inconsistent Responses stream", code="unsupported_response", http_status=502)

    def _event(self, kind: str, **fields: object) -> dict[str, object]:
        event: dict[str, object] = {"type": kind, "sequence_number": self._sequence, **copy.deepcopy(fields)}
        self._sequence += 1
        if self.request.include_obfuscation and kind in {
            "response.output_text.delta", "response.reasoning_text.delta", "response.function_call_arguments.delta",
        }:
            event["obfuscation"] = secrets.token_hex(secrets.randbelow(32) + 1)
        return event

    def _begin(self) -> list[dict[str, object]]:
        if self._started:
            return []
        self._started = True
        response = _response_fields(self.request, self.response_id, self.created, "in_progress", [])
        return [self._event("response.created", response=response), self._event("response.in_progress", response=response)]

    def _reserve(self, characters: int) -> None:
        self._size += characters
        self._chunks += 1
        if self._size > MAX_RESPONSE_CHARS or self._chunks > 1_000_000:
            raise self._error()

    def _part(self, kind: str, events: list[dict[str, object]]) -> int:
        index = self._parts.get(kind)
        if index is not None:
            return index
        index = len(self.output)
        self._parts[kind] = index
        item: dict[str, object] = {
            "type": "message" if kind == "text" else "reasoning",
            "id": f"{'msg' if kind == 'text' else 'rs'}-{self.response_id}",
            "status": "in_progress", "content": [],
        }
        if kind == "text":
            item["role"] = "assistant"
        else:
            item["summary"] = []
        self.output.append(item)
        events.append(self._event("response.output_item.added", output_index=index, item=item))
        part: dict[str, object] = {"type": "output_text" if kind == "text" else "reasoning_text", "text": ""}
        if kind == "text":
            part.update({"annotations": [], "logprobs": []})
        item["content"] = [part]
        events.append(self._event("response.content_part.added", output_index=index, item_id=item["id"], content_index=0, part=part))
        return index

    def _delta(self, kind: str, text: str, events: list[dict[str, object]]) -> None:
        _output_text(text)
        self._reserve(len(text))
        index = self._part(kind, events)
        part_kind = "output_text" if kind == "text" else "reasoning_text"
        self._text[kind].append(text)
        fields: dict[str, object] = {
            "output_index": index, "item_id": self.output[index]["id"], "content_index": 0, "delta": text,
        }
        if kind == "text":
            fields["logprobs"] = []
        events.append(self._event(f"response.{part_kind}.delta", **fields))

    def push(self, chunk: AdapterStreamChunk) -> tuple[dict[str, object], ...]:
        # Loopback adapters flush accumulated complete calls after the finish
        # frame. Only compatible tool facts and usage may follow that marker.
        if self._done or (
            self._finish_reason is not None and chunk.kind != CHUNK_USAGE
            and not (chunk.kind == CHUNK_TOOL_CALL and self._finish_reason in (FINISH_TOOL_CALLS, FINISH_LENGTH, FINISH_CANCELLED))
        ):
            raise self._error()
        events = self._begin()
        if chunk.kind in (CHUNK_TEXT_DELTA, CHUNK_REASONING_DELTA):
            assert chunk.text is not None
            kind = "text" if chunk.kind == CHUNK_TEXT_DELTA else "reasoning"
            self._delta(kind, chunk.text, events)
        elif chunk.kind == CHUNK_TOOL_CALL:
            assert chunk.tool_call is not None
            call = chunk.tool_call
            _check_call(self.request, call)
            for value in (call.id, call.name, call.arguments):
                _output_text(value)
            if len(self._tools) >= MAX_TOOLS or any(prior.id == call.id for prior in self._tools):
                raise self._error()
            if self._tools and not self.request.parallel_tool_calls:
                raise self._error()
            self._reserve(len(call.id) + len(call.name) + len(call.arguments))
            self._tools.append(call)
            index = len(self.output)
            item: dict[str, object] = {"type": "function_call", "id": f"fc-{self.response_id}-{len(self._tools) - 1}",
                    "call_id": call.id, "name": call.name, "arguments": "", "status": "in_progress"}
            self.output.append(item)
            events.append(self._event("response.output_item.added", output_index=index, item=item))
            item["arguments"] = call.arguments
            events.append(self._event("response.function_call_arguments.delta", output_index=index, item_id=item["id"], delta=call.arguments))
        elif chunk.kind == CHUNK_FINISH:
            self._finish_reason = chunk.finish_reason
        elif chunk.kind == CHUNK_USAGE:
            assert chunk.usage is not None
            self._reserve(0)
            self._usage = chunk.usage.to_dict()
            self._usage_updates += 1
        return tuple(events)

    def finish(self, outcome: CompletionOutcome | None) -> tuple[dict[str, object], ...]:
        if outcome is None or self._done:
            return ()
        if outcome.request_id != self.response_id or outcome.model_echo != self.request.canonical.model:
            raise self._error()
        if self._finish_reason is not None and self._finish_reason != outcome.finish_reason:
            raise self._error()
        # Reconcile only an exact observed prefix, never replace conflicting
        # output or fabricate missing calls/arguments from tool declarations.
        self._finish_reason = None
        events = self._begin()
        for kind, final in (("reasoning", outcome.message.reasoning), ("text", outcome.message.content)):
            observed = "".join(self._text[kind])
            if final is not None:
                if not final.startswith(observed):
                    raise self._error()
                if not final and kind not in self._parts:
                    _ = self._part(kind, events)
                elif len(final) > len(observed):
                    for start in range(len(observed), len(final), MAX_STREAM_DELTA_CHARS):
                        self._delta(kind, final[start:start + MAX_STREAM_DELTA_CHARS], events)
        final_tools = outcome.message.tool_calls
        if tuple(self._tools) != final_tools[:len(self._tools)]:
            raise self._error()
        for call in final_tools[len(self._tools):]:
            events.extend(self.push(AdapterStreamChunk(CHUNK_TOOL_CALL, tool_call=call)))
        for kind, index in self._parts.items():
            item = self.output[index]
            part = cast(list[dict[str, object]], item["content"])[0]
            part["text"] = "".join(self._text[kind])
        response = response_payload(self.request, outcome, output=self.output)
        response["created_at"] = self.created
        item_status = "completed" if response["status"] == "completed" else "incomplete"
        for index, item in enumerate(self.output):
            item["status"] = item_status
            if item["type"] == "function_call":
                if item_status == "completed":
                    events.append(self._event("response.function_call_arguments.done", output_index=index, item_id=item["id"], arguments=item["arguments"]))
            else:
                part = cast(list[dict[str, object]], item["content"])[0]
                kind = "output_text" if item["type"] == "message" else "reasoning_text"
                fields = {"output_index": index, "item_id": item["id"], "content_index": 0, "text": part["text"]}
                if kind == "output_text":
                    fields["logprobs"] = []
                events.append(self._event(f"response.{kind}.done", **fields))
                events.append(self._event("response.content_part.done", output_index=index, item_id=item["id"], content_index=0, part=part))
            events.append(self._event("response.output_item.done", output_index=index, item=item))
        extension = cast(dict[str, object], response["x_scarcity_router"])
        extension["execution_created"] = outcome.created
        if self._usage is not None:
            extension["observed_stream_usage"] = dict(self._usage)
            extension["usage_update_count"] = self._usage_updates
        if outcome.finish_reason == FINISH_CANCELLED:
            events.extend(self.fail(GatewayError.api("execution cancelled; settlement is not inferred", code="request_cancelled")))
        else:
            terminal = "response.completed" if response["status"] == "completed" else "response.incomplete"
            events.append(self._event(terminal, response=response))
            self._done = True
        return tuple(events)

    def fail(self, error: GatewayError) -> tuple[dict[str, object], ...]:
        if self._done:
            return ()
        self._done = True
        return (self._event("error", code=error.code, message=error.message, param=error.param),)
