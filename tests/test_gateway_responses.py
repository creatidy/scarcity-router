"""Original source-shaped Responses fixtures; no SDK, account or provider call."""

from __future__ import annotations

import copy
import http.client
import json
import socket
import threading
import unittest
from dataclasses import replace
from typing import cast
from unittest import mock

from scarcity_router.gateway_adapters import (
    CHUNK_FINISH, CHUNK_REASONING_DELTA, CHUNK_TEXT_DELTA, CHUNK_TOOL_CALL,
    AdapterCall, AdapterMessage, AdapterResult, AdapterStreamChunk, AdapterToolCall, CompletionOutcome, ExecutionContext,
)
from scarcity_router.gateway_audit import AuditRecord, BoundedAuditTrail
from scarcity_router.gateway_contracts import ClientKeyDirectory, GatewayError, UsageAccounting, UsageTokens
from scarcity_router.gateway_responses import ResponsesStreamEvents, parse_responses_request, prepare_executable_response, response_payload
from scarcity_router.providers.openai_http_core import build_chat_completion_request
from scarcity_router.providers.openai_http_presets import OPENAI_API_PRESET
from scarcity_router.resource_state import ResourceIdentity
from scarcity_router.routing_core import RequestBinding
from scarcity_router.selection_types import CapabilityMinima, HardConstraints, ModelIdentity, TaskRequirement
from scarcity_router.worker_local_adapters import LoopbackOllamaAdapter
from scarcity_router.worker_codex_adapter import CodexIneligible, build_dynamic_tools
from tests.gateway_fixtures import CLIENT_ID, CLIENT_KEY, ScriptedAdapter, ambiguous_failure_behavior, audit_records, echo_behavior, tool_call_behavior
from tests.openai_http_fixtures import ScriptedProviderServer, completion_frame
from tests.test_gateway_server import ServerHarness, as_dict, as_list
from tests.test_e2e_codex_acceptance import CodexComposedTlsWorld, chatgpt_scenario
from tests.m10_codex_fixtures import PIN_MODEL, PROMPT, RESOURCE_ID, trace_events, trace_methods

CANARY = "SYNTHETIC-RESPONSES-NOT-A-CREDENTIAL"
FUNCTION: dict[str, object] = {"type": "function", "name": "read", "parameters": {"type": "object"}, "strict": False}


def document(**changes: object) -> dict[str, object]:
    return {"model": "deep-coding", "input": "hello", "store": False, **changes}


def outcome(**changes: object) -> CompletionOutcome:
    base = CompletionOutcome(
        request_id="resp-test", created=10, model_echo="deep-coding",
        message=AdapterMessage("assistant", content="answer"), finish_reason="stop",
        usage=UsageAccounting("unavailable"),
    )
    return replace(base, **changes)


class ResponsesCodecTests(unittest.TestCase):
    def test_user_shorthand_and_controls_preserve_existing_semantic_request(self) -> None:
        request = parse_responses_request(document(
            max_output_tokens=120, reasoning={"effort": "high"}, temperature=0.4,
            text={"format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}, "strict": True}},
            metadata={"trace": "synthetic"},
        ))
        self.assertEqual("user", request.canonical.messages[0].role)
        self.assertEqual("hello", request.canonical.messages[0].content)
        self.assertEqual(120, request.canonical.capabilities.requested_output_tokens)
        self.assertEqual("high", request.canonical.reasoning_effort)
        assert request.canonical.response_format is not None
        self.assertEqual("json_schema", request.canonical.response_format["type"])
        self.assertEqual({"temperature": 0.4}, request.canonical.generation_params)
        self.assertEqual({"trace": "synthetic"}, request.canonical.metadata)

    def test_functions_results_and_opaque_continuation_ids_are_not_guessed(self) -> None:
        history: list[object] = [
            {"role": "developer", "content": "keep roles"},
            {"role": "user", "content": "work"},
            {"type": "message", "id": "msg-old", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "checking", "annotations": [], "logprobs": []}]},
            {"type": "function_call", "id": "fc-old", "call_id": "srct-opaque", "name": "read", "arguments": '{"path":"x"}'},
            {"type": "function_call_output", "id": "out-old", "call_id": "srct-opaque", "output": "result"},
        ]
        data = document(tools=[FUNCTION], input=history)
        before = copy.deepcopy(data)
        request = parse_responses_request(data)
        self.assertEqual(before, data)
        self.assertEqual(["developer", "user", "assistant", "tool"], [item.role for item in request.canonical.messages])
        self.assertEqual("srct-opaque", request.canonical.messages[-2].tool_calls[0].id)
        self.assertEqual("srct-opaque", request.canonical.messages[-1].tool_call_id)
        self.assertEqual(("msg-old", "fc-old", "out-old"), request.input_item_ids)
        self.assertEqual('{"path":"x"}', request.canonical.messages[-2].tool_calls[0].arguments)

    def test_unknown_and_unrepresentable_fields_refuse_without_echo(self) -> None:
        cases: tuple[dict[str, object], ...] = (
            document(store=True), document(store=None), document(stream=1),
            document(previous_response_id=CANARY), document(conversation=CANARY),
            document(instructions=CANARY), document(background=True), document(truncation="auto"),
            document(reasoning={"effort": "high", "summary": "auto"}),
            document(reasoning={"effort": "ultra"}), document(reasoning={"effort": "max", "mode": "pro"}),
            document(tools=[{"type": "web_search"}]), document(tools=[{**FUNCTION, "namespace": CANARY}]),
            document(input=[{"type": "item_reference", "id": CANARY}]),
            document(input=[{"type": "reasoning", "encrypted_content": CANARY}]),
            document(input=[{"role": "assistant", "content": "x", "phase": "final_answer"}]),
            document(input=[{"role": "user", "content": [{"type": "input_text", "text": "a"}, {"type": "input_text", "text": "b"}]}]),
            document(input=[{"role": "user", "content": [{"type": "input_image", "image_url": CANARY}]}]),
            document(input=[{"type": "function_call_output", "call_id": CANARY, "output": "orphan"}]),
            document(input=[{"type": "function_call", "call_id": CANARY, "name": "read", "arguments": "{}"}]),
            document(metadata={CANARY: 42}), document(**{CANARY: True}),
        )
        for data in cases:
            with self.subTest(keys=tuple(data)):
                with self.assertRaises(GatewayError) as caught:
                    _ = parse_responses_request(data)
                self.assertEqual(400, caught.exception.http_status)
                self.assertNotIn(CANARY, json.dumps(caught.exception.to_payload()))

    def test_store_false_is_required_not_an_implicit_retention_change(self) -> None:
        data = document()
        del data["store"]
        with self.assertRaises(GatewayError):
            _ = parse_responses_request(data)

    def test_nonfinite_and_unencodable_json_refuses_before_semantic_dispatch(self) -> None:
        for data in (document(input="\ud800"), document(temperature=float("nan")), document(tools=[{**FUNCTION, "parameters": {"description": "\ud800"}}])):
            with self.subTest(), self.assertRaises(GatewayError):
                _ = parse_responses_request(data)

    def test_forced_tool_and_parallel_control_are_preserved(self) -> None:
        request = parse_responses_request(document(tools=[FUNCTION], tool_choice={"type": "function", "name": "read"}, parallel_tool_calls=False))
        self.assertEqual({"type": "function", "function": {"name": "read"}}, request.canonical.tool_choice)
        self.assertEqual(False, (request.canonical.generation_params or {})["parallel_tool_calls"])
        self.assertEqual([FUNCTION], [dict(item) for item in request.tools])

    def test_native_strict_function_schema_refuses_without_weakening_http(self) -> None:
        request = parse_responses_request(document(tools=[{**FUNCTION, "strict": True}]))
        with self.assertRaises(CodexIneligible):
            _ = build_dynamic_tools(request.canonical.tools)
        resource = ResourceIdentity(resource_id="http-source", channel="server_direct_http", provider="openai", model="local", entitlement="payg_metered")
        call = AdapterCall(resource=resource, model=ModelIdentity(provider="openai", model="local", variant="high"),
                           messages=(AdapterMessage("user", content="hello"),), tools=request.canonical.tools)
        wire = build_chat_completion_request(call, OPENAI_API_PRESET.policy)
        self.assertIs(True, as_dict(as_dict(as_list(wire["tools"])[0])["function"])["strict"])
        for strict in (False, None):
            function = dict(as_dict(as_dict(as_list(wire["tools"])[0])["function"]))
            if strict is None:
                del function["strict"]
            else:
                function["strict"] = strict
            mapped = build_dynamic_tools([{"type": "function", "function": function}])
            self.assertEqual(frozenset({"read"}), mapped.names)
            self.assertEqual(FUNCTION["parameters"], mapped.declarations[0]["parameters"])

    def test_impossible_tool_choices_refuse_before_execution(self) -> None:
        for declarations in ([], [FUNCTION]):
            choices: list[object] = [{"type": "function", "name": CANARY}]
            if not declarations:
                choices.append("required")
            for choice in choices:
                with self.subTest(tools=declarations, choice=choice):
                    with self.assertRaises(GatewayError) as caught:
                        _ = parse_responses_request(document(tools=declarations, tool_choice=choice))
                    self.assertEqual(400, caught.exception.http_status)
                    self.assertEqual("tool_choice", caught.exception.param)
                    self.assertNotIn(CANARY, json.dumps(caught.exception.to_payload()))

    def test_function_description_requires_string_null_or_omission(self) -> None:
        for description in ({"unexpected": CANARY}, [CANARY], 1, 0.5, True, False):
            with self.subTest(description=description):
                with self.assertRaises(GatewayError) as caught:
                    _ = parse_responses_request(document(tools=[{**FUNCTION, "description": description}]))
                self.assertEqual(400, caught.exception.http_status)
                self.assertEqual("tools", caught.exception.param)
                self.assertNotIn(CANARY, json.dumps(caught.exception.to_payload()))
        for function in (FUNCTION, {**FUNCTION, "description": None}, {**FUNCTION, "description": "synthetic function"}):
            request = parse_responses_request(document(tools=[function]))
            self.assertEqual(function, dict(request.tools[0]))
            canonical = as_dict(request.canonical.tools[0]["function"])
            self.assertEqual("description" in function, "description" in canonical)
            self.assertEqual(function.get("description"), canonical.get("description"))

    def test_wire_effort_spellings_are_not_collapsed_or_guessed(self) -> None:
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                request = parse_responses_request(document(reasoning={"effort": effort}))
                self.assertEqual(effort, request.canonical.reasoning_effort)

    def test_explicit_text_and_json_object_formats_are_not_lost(self) -> None:
        for kind in ("text", "json_object"):
            with self.subTest(kind=kind):
                request = parse_responses_request(document(text={"format": {"type": kind}}))
                self.assertEqual({"type": kind}, request.canonical.response_format)

    def test_malformed_json_schema_envelopes_refuse_locally(self) -> None:
        cases: tuple[dict[str, object], ...] = (
            {"type": "json_schema"}, {"type": "json_schema", "name": "answer"},
            {"type": "json_schema", "schema": {}},
            {"type": "json_schema", "name": "bad name", "schema": {}},
            {"type": "json_schema", "name": "x" * 65, "schema": {}},
            {"type": "json_schema", "name": "answer", "schema": []},
            {"type": "json_schema", "name": "answer", "schema": True},
            {"type": "json_schema", "name": "answer", "schema": {}, "strict": 1},
            {"type": "json_schema", "name": "answer", "schema": {}, "strict": "false"},
            {"type": "json_schema", "name": "answer", "schema": {}, "description": [CANARY]},
        )
        for shape in cases:
            with self.subTest(), self.assertRaises(GatewayError):
                _ = parse_responses_request(document(text={"format": shape}))

    def test_large_final_suffixes_are_framed_without_losing_text_or_reasoning(self) -> None:
        limit = 1_048_576
        for kind in ("text", "reasoning"):
            for prefix in ("", "known-prefix"):
                with self.subTest(kind=kind, prefix=prefix):
                    request = parse_responses_request(document(stream=True, stream_options={"include_obfuscation": False}))
                    stream = ResponsesStreamEvents(request, response_id="resp-test", created=1)
                    emitted: list[dict[str, object]] = []
                    if prefix:
                        emitted.extend(stream.push(AdapterStreamChunk(CHUNK_TEXT_DELTA if kind == "text" else CHUNK_REASONING_DELTA, text=prefix)))
                    final_text = prefix + ("x" if kind == "text" else "\U00010400") * (limit + 1)
                    message = AdapterMessage("assistant", content=final_text) if kind == "text" else AdapterMessage("assistant", reasoning=final_text)
                    emitted.extend(stream.finish(outcome(message=message)))
                    event_kind = "response.output_text.delta" if kind == "text" else "response.reasoning_text.delta"
                    deltas = [cast(str, item["delta"]) for item in emitted if item["type"] == event_kind]
                    self.assertEqual(final_text, "".join(deltas))
                    self.assertTrue(all(len(delta) <= limit for delta in deltas))
                    self.assertEqual(list(range(len(emitted))), [item["sequence_number"] for item in emitted])
                    self.assertEqual("response.completed", emitted[-1]["type"])

    def test_duplicate_or_incomplete_history_is_not_repaired(self) -> None:
        call: dict[str, object] = {"type": "function_call", "call_id": "x", "name": "read", "arguments": "{}"}
        result: dict[str, object] = {"type": "function_call_output", "call_id": "x", "output": "done"}
        for history in ([call, call, result], [call, result, result], [call, {"role": "user", "content": "interrupt"}, result], [{**call, "status": "incomplete"}, result]):
            with self.subTest(), self.assertRaises(GatewayError):
                _ = parse_responses_request(document(input=history))

    def test_undeclared_or_inconsistent_output_calls_do_not_gain_client_authority(self) -> None:
        request = parse_responses_request(document(tools=[FUNCTION], tool_choice="none"))
        call = AdapterToolCall("call-x", "read", "{}")
        with self.assertRaises(GatewayError):
            _ = response_payload(request, outcome(message=AdapterMessage("assistant", tool_calls=(call,)), finish_reason="tool_calls"))
        required = parse_responses_request(document(tools=[FUNCTION], tool_choice="required"))
        with self.assertRaises(GatewayError):
            _ = response_payload(required, outcome())

    def test_no_fake_usage_phase_or_reasoning_summary(self) -> None:
        request = parse_responses_request(document())
        result = outcome(message=AdapterMessage("assistant", content="answer", reasoning="opaque, not a summary"),
                         usage=UsageAccounting("provider_reported", provider_reported_usage=UsageTokens(10, 4)))
        payload = response_payload(request, result)
        self.assertEqual("response", payload["object"])
        self.assertIsNone(payload["usage"])
        items = as_list(payload["output"])
        self.assertEqual("reasoning", as_dict(items[0])["type"])
        self.assertEqual([], as_dict(items[0])["summary"])
        self.assertNotIn("phase", as_dict(items[1]))
        accounting = as_dict(as_dict(payload["x_scarcity_router"])["usage_accounting"])
        self.assertEqual({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}, accounting["provider_reported_usage"])

    def test_length_is_incomplete_without_guessing_a_limit_cause(self) -> None:
        payload = response_payload(parse_responses_request(document()), outcome(finish_reason="length"))
        self.assertEqual("incomplete", payload["status"])
        self.assertEqual({"reason": None}, payload["incomplete_details"])

    def test_mixed_accounting_is_preserved_not_summed_into_paid_or_zero_usage(self) -> None:
        usage = UsageAccounting("mixed", provider_reported_usage=UsageTokens(2, 3), estimated_usage=UsageTokens(7, 11))
        payload = response_payload(parse_responses_request(document()), outcome(usage=usage))
        self.assertIsNone(payload["usage"])
        self.assertEqual(usage.to_dict(), as_dict(payload["x_scarcity_router"])["usage_accounting"])

    def test_stream_snapshots_sequence_and_terminal_follow_actual_outcome(self) -> None:
        request = parse_responses_request(document(stream=True, stream_options={"include_obfuscation": False}))
        events = ResponsesStreamEvents(request, response_id="resp-test", created=3)
        first = events.push(AdapterStreamChunk(CHUNK_REASONING_DELTA, text="opaque"))
        text = events.push(AdapterStreamChunk(CHUNK_TEXT_DELTA, text="ans"))
        _ = events.push(AdapterStreamChunk(CHUNK_FINISH, finish_reason="stop"))
        self.assertNotIn("response.completed", [item["type"] for item in first + text])
        self.assertEqual((), events.finish(None))
        final = events.finish(outcome(message=AdapterMessage("assistant", content="answer", reasoning="opaque")))
        all_events = first + text + final
        self.assertEqual(list(range(len(all_events))), [item["sequence_number"] for item in all_events])
        self.assertEqual("response.completed", final[-1]["type"])
        self.assertEqual(3, as_dict(final[-1]["response"])["created_at"])
        # Added snapshots were not mutated into complete output retroactively.
        added = next(item for item in text if item["type"] == "response.output_item.added")
        self.assertEqual([], as_dict(added["item"])["content"])

    def test_conflicting_or_cancelled_stream_never_completes_successfully(self) -> None:
        request = parse_responses_request(document(stream=True))
        events = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        _ = events.push(AdapterStreamChunk(CHUNK_TEXT_DELTA, text="a"))
        with self.assertRaises(GatewayError):
            _ = events.finish(outcome(message=AdapterMessage("assistant", content="different")))
        failed = events.fail(GatewayError.api("source failed", code="backend_failure"))
        self.assertEqual("error", failed[-1]["type"])
        self.assertEqual((), events.finish(outcome()))
        cancelled = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        terminal = cancelled.finish(outcome(finish_reason="cancelled"))
        self.assertEqual("error", terminal[-1]["type"])
        self.assertNotIn("response.completed", [item["type"] for item in terminal])

    def test_tool_stream_preserves_call_id_and_no_incomplete_arguments_done(self) -> None:
        request = parse_responses_request(document(stream=True, tools=[FUNCTION]))
        call = AdapterToolCall("srct-opaque", "read", "{}")
        events = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        initial = events.push(AdapterStreamChunk(CHUNK_TOOL_CALL, tool_call=call))
        final = events.finish(outcome(message=AdapterMessage("assistant", tool_calls=(call,)), finish_reason="length"))
        self.assertEqual("srct-opaque", as_dict(next(item for item in initial if item["type"] == "response.output_item.added")["item"])["call_id"])
        self.assertNotIn("response.function_call_arguments.done", [item["type"] for item in final])
        self.assertEqual("response.incomplete", final[-1]["type"])

    def test_actual_loopback_adapter_delayed_tools_preserve_terminal_framing(self) -> None:
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        fragments: list[dict[str, object]] = [
            {"index": 0, "id": "original-call", "type": "function", "function": {"name": "read", "arguments": '{"q":'}},
            {"index": 0, "type": "function", "function": {"arguments": '"synthetic"}'}},
        ]
        frames: list[dict[str, object]] = []
        for fragment in fragments:
            frame = completion_frame()
            as_dict(as_list(frame["choices"])[0])["delta"] = {"tool_calls": [fragment]}
            frames.append(frame)
        frames.append(completion_frame(finish_reason="tool_calls"))
        provider.enqueue_stream(frames)
        resource = ResourceIdentity(resource_id="ollama-local", channel="worker_bridged", provider="ollama", model="local", entitlement="local_ungated")
        adapter = LoopbackOllamaAdapter(resource=resource, port=provider.port)
        request = parse_responses_request(document(stream=True, tools=[FUNCTION]))
        stream = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        emitted: list[dict[str, object]] = []
        chunks: list[str] = []

        def emit(chunk: AdapterStreamChunk) -> None:
            chunks.append(chunk.kind)
            emitted.extend(stream.push(chunk))

        result = adapter.invoke(
            AdapterCall(resource=resource, model=ModelIdentity(provider="openai", model="local", variant="max"),
                        messages=(AdapterMessage("user", content="hello"),), stream=True,
                        tools=request.canonical.tools, tool_choice=request.canonical.tool_choice),
            cancel_event=threading.Event(), deadline="2030-01-01T00:00:00.000Z", emit=emit,
        )
        self.assertEqual("completed", result.status)
        self.assertEqual([CHUNK_FINISH, CHUNK_TOOL_CALL], chunks)
        assert result.message is not None and result.finish_reason is not None
        emitted.extend(stream.finish(outcome(message=result.message, finish_reason=result.finish_reason)))
        self.assertEqual("response.completed", emitted[-1]["type"])
        self.assertEqual(list(range(len(emitted))), [item["sequence_number"] for item in emitted])
        call = as_dict(as_list(as_dict(emitted[-1]["response"])["output"])[0])
        self.assertEqual(("original-call", "read", '{"q":"synthetic"}'), (call["call_id"], call["name"], call["arguments"]))
        done = next(item for item in emitted if item["type"] == "response.function_call_arguments.done")
        self.assertEqual(call["arguments"], done["arguments"])
        self.assertEqual(1, provider.request_count)

    def test_delayed_tools_do_not_allow_late_text_duplicate_finish_or_false_success(self) -> None:
        request = parse_responses_request(document(stream=True, tools=[FUNCTION]))
        call = AdapterToolCall("original-call", "read", "{}")
        for reason in ("tool_calls", "length", "cancelled"):
            stream = ResponsesStreamEvents(request, response_id="resp-test", created=1)
            _ = stream.push(AdapterStreamChunk(CHUNK_FINISH, finish_reason=reason))
            _ = stream.push(AdapterStreamChunk(CHUNK_TOOL_CALL, tool_call=call))
            for chunk in (AdapterStreamChunk(CHUNK_TEXT_DELTA, text="late"), AdapterStreamChunk(CHUNK_REASONING_DELTA, text="late"),
                          AdapterStreamChunk(CHUNK_FINISH, finish_reason=reason)):
                with self.subTest(reason=reason, kind=chunk.kind), self.assertRaises(GatewayError):
                    _ = stream.push(chunk)
            final = stream.finish(outcome(message=AdapterMessage("assistant", tool_calls=(call,)), finish_reason=reason))
            terminal = "response.completed" if reason == "tool_calls" else "response.incomplete" if reason == "length" else "error"
            self.assertEqual(terminal, final[-1]["type"])
            if reason != "tool_calls":
                self.assertNotIn("response.function_call_arguments.done", [item["type"] for item in final])
        invalid = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        _ = invalid.push(AdapterStreamChunk(CHUNK_FINISH, finish_reason="stop"))
        with self.assertRaises(GatewayError):
            _ = invalid.push(AdapterStreamChunk(CHUNK_TOOL_CALL, tool_call=call))

    def test_chunkless_empty_output_and_obfuscation_options_are_explicit(self) -> None:
        request = parse_responses_request(document(stream=True))
        stream = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        events = stream.finish(outcome(message=AdapterMessage("assistant", content="")))
        self.assertEqual("response.completed", events[-1]["type"])
        other = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        delta = next(item for item in other.push(AdapterStreamChunk(CHUNK_TEXT_DELTA, text="a")) if item["type"] == "response.output_text.delta")
        self.assertIsInstance(delta["obfuscation"], str)

    def test_finish_conflict_duplicate_identity_and_output_bounds_do_not_complete(self) -> None:
        request = parse_responses_request(document(stream=True, tools=[FUNCTION]))
        stream = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        _ = stream.push(AdapterStreamChunk(CHUNK_FINISH, finish_reason="stop"))
        with self.assertRaises(GatewayError):
            _ = stream.finish(outcome(finish_reason="length"))
        call = AdapterToolCall("call-x", "read", "{}")
        other = ResponsesStreamEvents(request, response_id="resp-test", created=1)
        _ = other.push(AdapterStreamChunk(CHUNK_TOOL_CALL, tool_call=call))
        with self.assertRaises(GatewayError):
            _ = other.push(AdapterStreamChunk(CHUNK_TOOL_CALL, tool_call=call))
        with mock.patch("scarcity_router.gateway_responses.MAX_RESPONSE_CHARS", 1), self.assertRaises(GatewayError):
            _ = response_payload(request, outcome())


class ResponsesHTTPTests(ServerHarness):
    def post_responses(self, port: int, data: dict[str, object], *, key: str = CLIENT_KEY) -> http.client.HTTPResponse:
        connection = self.client(port)
        connection.request("POST", "/v1/responses", json.dumps(data), {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        return connection.getresponse()

    def test_authenticated_nonstream_uses_existing_core_once(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        response = self.post_responses(port, document())
        payload = as_dict(cast(object, json.loads(response.read())))
        self.assertEqual(200, response.status)
        self.assertEqual("response", payload["object"])
        self.assertEqual("completed", payload["status"])
        self.assertEqual(1, adapter.dispatch_count)
        self.assertEqual("hello", adapter.dispatches[0].messages[0].content)
        self.assertEqual(1, len(audit_records(self.application)))

    def test_unsupported_fields_refuse_before_adapter_with_no_sdk_retry(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        response = self.post_responses(port, document(previous_response_id=CANARY))
        self.assertEqual(400, response.status)
        self.assertEqual("false", response.getheader("x-should-retry"))
        self.assertNotIn(CANARY.encode(), response.read())
        self.assertEqual([], adapter.dispatches)

    def test_malformed_structured_requirement_never_dispatches(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        shapes: tuple[dict[str, object], ...] = (
            {"type": "json_schema"}, {"type": "json_schema", "name": "answer", "schema": []},
            {"type": "json_schema", "name": "answer", "schema": {}, "strict": "yes"},
        )
        for shape in shapes:
            with self.subTest(shape=shape):
                response = self.post_responses(port, document(text={"format": shape}))
                self.assertEqual(400, response.status, response.read())
                self.assertEqual([], adapter.dispatches)

    def test_impossible_tool_choices_never_dispatch_json_or_sse(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        for stream in (False, True):
            for tools, choice in (([], "required"), ([], {"type": "function", "name": CANARY}),
                                  ([FUNCTION], {"type": "function", "name": CANARY})):
                with self.subTest(stream=stream, tools=tools, choice=choice):
                    response = self.post_responses(port, document(stream=stream, tools=tools, tool_choice=choice))
                    wire = response.read()
                    self.assertEqual(400, response.status, wire)
                    self.assertNotIn(CANARY.encode(), wire)
                    self.assertEqual([], adapter.dispatches)

    def test_function_description_validation_precedes_json_or_sse_dispatch(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        for stream in (False, True):
            for description in ({"unexpected": CANARY}, [CANARY], 1, 0.5, True, False):
                with self.subTest(stream=stream, description=description):
                    response = self.post_responses(port, document(stream=stream, tools=[{**FUNCTION, "description": description}]))
                    wire = response.read()
                    self.assertEqual(400, response.status)
                    self.assertNotIn(CANARY.encode(), wire)
                    self.assertEqual([], adapter.dispatches)
        for stream in (False, True):
            for function in (FUNCTION, {**FUNCTION, "description": None}, {**FUNCTION, "description": "synthetic function"}):
                with self.subTest(stream=stream, function=function):
                    response = self.post_responses(port, document(stream=stream, tools=[function]))
                    _ = response.read()
                    self.assertEqual(200, response.status)
                    canonical = as_dict(adapter.dispatches[-1].tools[0]["function"])
                    self.assertEqual("description" in function, "description" in canonical)
                    self.assertEqual(function.get("description"), canonical.get("description"))
        self.assertEqual(6, len(adapter.dispatches))

    def test_other_configured_credential_is_not_retained_or_sent_to_a_model(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        other = "SYNTHETIC-OTHER-CLIENT-KEY-NOT-REAL"
        self.application.client_key_directory = ClientKeyDirectory.from_secrets({CLIENT_ID: CLIENT_KEY, "client-b": other})
        for data in (document(metadata={"trace": other}), document(metadata={other: "value"}),
                     document(tools=[{**FUNCTION, "description": other}])):
            with self.subTest():
                response = self.post_responses(port, data)
                wire = response.read()
                self.assertEqual(400, response.status)
                self.assertNotIn(other.encode(), wire)
                self.assertEqual([], adapter.dispatches)
        public = self.post_responses(port, document(metadata={"trace": "unconfigured-public-label"}))
        self.assertEqual(200, public.status)
        _ = public.read()
        self.assertEqual(1, adapter.dispatch_count)

    def test_stream_uses_typed_events_not_chat_frames_or_done(self) -> None:
        port = self.make_server()
        response = self.post_responses(port, document(stream=True))
        wire = response.read().decode()
        self.assertEqual(200, response.status)
        self.assertIn("event: response.created", wire)
        self.assertIn('"type":"response.completed"', wire)
        self.assertNotIn("chat.completion", wire)
        self.assertNotIn("[DONE]", wire)

    def test_full_function_round_trip_preserves_actual_call_lineage(self) -> None:
        first = tool_call_behavior(tool_name="read", arguments="{}")

        def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
            if call.messages[-1].role == "tool":
                return echo_behavior(content="accepted result")(call, context)
            return first(call, context)

        adapter = ScriptedAdapter(behavior=behavior)
        port = self.make_server(adapters=[adapter])
        response = self.post_responses(port, document(tools=[FUNCTION]))
        payload = as_dict(cast(object, json.loads(response.read())))
        self.assertEqual(200, response.status)
        outputs = as_list(payload["output"])
        function = next(as_dict(item) for item in outputs if as_dict(item)["type"] == "function_call")
        history: list[object] = [{"role": "user", "content": "hello"}, *outputs,
                                 {"type": "function_call_output", "call_id": function["call_id"], "output": "result"}]
        resumed = self.post_responses(port, document(tools=[FUNCTION], input=history))
        self.assertEqual(200, resumed.status)
        _ = resumed.read()
        self.assertEqual(2, adapter.dispatch_count)
        self.assertEqual(function["call_id"], adapter.dispatches[-1].messages[-1].tool_call_id)
        self.assertEqual("result", adapter.dispatches[-1].messages[-1].content)

    def test_174_exact_context_survives_codec_and_wrong_alias_refuses_before_effects(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        requirement = TaskRequirement(task_level="L2", capability_minima=CapabilityMinima(reasoning=3, coding=3), hard_constraints=HardConstraints())
        expected: dict[str, object] = {
            "schema_version": 1, "model": "deep-coding", "requirement": requirement.to_dict(),
            "binding": RequestBinding().to_dict(),
        }
        selected = self.application.select_executable(client_id=CLIENT_ID, document=expected)
        wire = prepare_executable_response(selected, expected_request=expected, request={"input": "hello", "store": False})
        self.assertEqual(wire, prepare_executable_response(selected, expected_request=expected, request=wire, require_bound=True))
        missing = dict(wire)
        del missing["reasoning"]
        with self.assertRaises(GatewayError):
            _ = prepare_executable_response(selected, expected_request=expected, request=missing, require_bound=True)
        response = self.post_responses(port, wire)
        reply = response.read()
        self.assertEqual(200, response.status, reply)
        self.assertEqual(1, adapter.dispatch_count)
        refused = self.post_responses(port, {**wire, "model": "deep-coding"})
        self.assertEqual(400, refused.status)
        _ = refused.read()
        self.assertEqual(1, adapter.dispatch_count)

    def test_mapping_failure_cancels_existing_lifecycle_without_false_disconnect_or_success(self) -> None:
        cancelled: list[bool] = []

        def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
            _ = call
            assert context.emit_chunk is not None
            try:
                context.emit_chunk(AdapterStreamChunk(CHUNK_TEXT_DELTA, text="prefix"))
                context.emit_chunk(AdapterStreamChunk(CHUNK_TOOL_CALL, tool_call=AdapterToolCall("bad", "not_declared", "{}")))
                raise AssertionError("unsupported output was accepted")
            finally:
                cancelled.append(context.cancelled)

        adapter = ScriptedAdapter(behavior=behavior)
        port = self.make_server(adapters=[adapter])
        response = self.post_responses(port, document(stream=True))
        wire = response.read().decode()
        self.assertEqual([True], cancelled)
        self.assertEqual(1, adapter.dispatch_count)
        self.assertIn('"type":"error"', wire)
        self.assertNotIn('"type":"response.completed"', wire)
        record = audit_records(self.application)[-1]
        self.assertEqual("failed", record.result_status)
        self.assertEqual(("response_mapping_failed",), record.reason_codes)

    def test_real_client_disconnect_reuses_current_cancellation_and_audit(self) -> None:
        release = threading.Event()
        audited = threading.Event()

        def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
            _ = call
            assert context.emit_chunk is not None
            context.emit_chunk(AdapterStreamChunk(CHUNK_TEXT_DELTA, text="before"))
            _ = release.wait(timeout=10)
            context.emit_chunk(AdapterStreamChunk(CHUNK_TEXT_DELTA, text="after"))
            raise AssertionError("disconnect not observed")

        port = self.make_server(adapters=[ScriptedAdapter(behavior=behavior)])
        trail = cast(BoundedAuditTrail, self.application.audit)
        append = trail.append

        def record(value: AuditRecord) -> None:
            append(value)
            audited.set()

        body = json.dumps(document(stream=True)).encode()
        with mock.patch.object(trail, "append", record), socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            request = (f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {CLIENT_KEY}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
            sock.sendall(request)
            received = b""
            while b"event: response.created" not in received:
                piece = sock.recv(4096)
                self.assertTrue(piece)
                received += piece
            sock.shutdown(socket.SHUT_WR)
            release.set()
            self.assertTrue(audited.wait(timeout=10))
        record_value = audit_records(self.application)[-1]
        self.assertEqual("cancelled", record_value.result_status)
        self.assertEqual(("client_disconnected",), record_value.reason_codes)

    def test_quote_backslash_bearer_is_not_reflected_in_metadata(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        key = CLIENT_KEY + '\\"quoted'
        self.application.client_key_directory = ClientKeyDirectory.from_secrets({CLIENT_ID: key})
        response = self.post_responses(port, document(metadata={"trace": f"prefix:{key}:suffix"}), key=key)
        self.assertEqual(400, response.status)
        self.assertNotIn(b"quoted", response.read())
        self.assertEqual([], adapter.dispatches)

    def test_ambiguous_loss_is_not_retried_or_reported_completed(self) -> None:
        adapter = ScriptedAdapter(behavior=ambiguous_failure_behavior())
        port = self.make_server(adapters=[adapter])
        response = self.post_responses(port, document())
        payload = as_dict(cast(object, json.loads(response.read())))
        self.assertEqual(500, response.status)
        self.assertEqual("ambiguous_execution_state", as_dict(payload["error"])["code"])
        self.assertEqual("false", response.getheader("x-should-retry"))
        self.assertEqual(1, adapter.dispatch_count)


class ResponsesNativeContinuationTests(CodexComposedTlsWorld):
    def _roundtrip(self, *, stream: bool) -> None:
        name = "synthetic_lookup"
        worker = self.start_codex_worker(chatgpt_scenario(turn={
            "dynamicTool": name, "deltas": ["Checking "], "postDeltas": ["Done."],
            "usage": {"last": {"inputTokens": 11, "outputTokens": 7}},
        }))
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            tool: dict[str, object] = {"type": "function", "name": name,
                                       "parameters": {"type": "object", "properties": {}}, "strict": False}

            def send(history: object) -> dict[str, object]:
                status, payload, _headers = self.exchange(
                    "POST", "/v1/responses", {"model": PIN_MODEL, "input": history, "tools": [tool],
                                              "store": False, "stream": stream},
                    headers={"Authorization": f"Bearer {self.client_key}"}, timeout=60,
                )
                self.assertEqual(200, status, payload)
                if not stream:
                    return as_dict(payload)
                frames = [as_dict(cast(object, json.loads(line[6:]))) for line in str(payload).splitlines() if line.startswith("data: ")]
                self.assertNotIn("[DONE]", str(payload))
                self.assertEqual("response.completed", frames[-1]["type"])
                return as_dict(frames[-1]["response"])

            first = send(PROMPT)
            outputs = as_list(first["output"])
            call = next(as_dict(item) for item in outputs if as_dict(item)["type"] == "function_call")
            self.assertTrue(cast(str, call["call_id"]).startswith("srct-"))
            history: list[object] = [{"role": "user", "content": PROMPT}, *outputs,
                                     {"type": "function_call_output", "call_id": call["call_id"], "output": "TOOL-RESULT"}]
            second = send(history)
            messages = [as_dict(item) for item in as_list(second["output"]) if as_dict(item)["type"] == "message"]
            self.assertEqual("Done.", as_dict(as_list(messages[-1]["content"])[0])["text"])
            methods = trace_methods(worker.trace_path)
            self.assertEqual(1, methods.count("thread/start"))
            self.assertEqual(1, methods.count("turn/start"))
            answers = trace_events(worker.trace_path, "tool_call_answer")
            self.assertEqual(1, len(answers))
            self.assertEqual("call-synthetic-1", answers[0]["callId"])
        finally:
            worker.stop()

    def test_actual_worker_codex_default_function_continuation(self) -> None:
        self._roundtrip(stream=False)

    def test_actual_worker_codex_default_streamed_function_continuation(self) -> None:
        self._roundtrip(stream=True)

    def test_explicit_unsupported_native_parallel_controls_do_not_start_a_turn(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            for permission in (False, True):
                with self.subTest(permission=permission):
                    status, payload, _headers = self.exchange(
                        "POST", "/v1/responses", {"model": PIN_MODEL, "input": PROMPT, "store": False,
                                                  "tools": [FUNCTION], "parallel_tool_calls": permission},
                        headers={"Authorization": f"Bearer {self.client_key}"}, timeout=60,
                    )
                    self.assertEqual(502, status, payload)
                    self.assertEqual("backend_failure", as_dict(as_dict(payload)["error"])["code"])
            self.assertEqual(0, trace_methods(worker.trace_path).count("turn/start"))
        finally:
            worker.stop()

    def test_impossible_tool_choices_do_not_start_a_native_turn(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            for stream in (False, True):
                for tools, choice in (([], "required"), ([], {"type": "function", "name": CANARY}),
                                      ([FUNCTION], {"type": "function", "name": CANARY})):
                    with self.subTest(stream=stream, tools=tools, choice=choice):
                        status, payload, _headers = self.exchange(
                            "POST", "/v1/responses", {"model": PIN_MODEL, "input": PROMPT, "store": False,
                                                      "tools": tools, "tool_choice": choice, "stream": stream},
                            headers={"Authorization": f"Bearer {self.client_key}"}, timeout=60,
                        )
                        self.assertEqual(400, status, payload)
            self.assertEqual(0, trace_methods(worker.trace_path).count("turn/start"))
        finally:
            worker.stop()

    def test_strict_function_demand_refuses_before_native_thread_or_turn(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            for stream in (False, True):
                bodies: tuple[tuple[str, dict[str, object]], ...] = (
                    ("/v1/responses", {"model": PIN_MODEL, "input": PROMPT, "store": False, "stream": stream,
                                       "tools": [{**FUNCTION, "strict": True}]}),
                    ("/v1/chat/completions", {"model": PIN_MODEL, "messages": [{"role": "user", "content": PROMPT}],
                                              "stream": stream, "tools": [{"type": "function", "function": {
                                                  key: value for key, value in {**FUNCTION, "strict": True}.items() if key != "type"}}]}),
                )
                for path, body in bodies:
                    with self.subTest(stream=stream, path=path):
                        status, payload, _headers = self.exchange(
                            "POST", path, body, headers={"Authorization": f"Bearer {self.client_key}"}, timeout=60,
                        )
                        self.assertEqual(502, status, payload)
                        self.assertEqual("backend_failure", as_dict(as_dict(payload)["error"])["code"])
            methods = trace_methods(worker.trace_path)
            self.assertNotIn("thread/start", methods)
            self.assertNotIn("turn/start", methods)
        finally:
            worker.stop()


if __name__ == "__main__":
    _ = unittest.main()
