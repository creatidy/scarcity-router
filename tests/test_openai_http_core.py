"""Unit tests for the OpenAI-compatible wire translation core (M04).

Pure, transport-free: these tests exercise the ONE semantic implementation
shared by the server-direct HTTP adapter (M04) and the future
worker-bridged path (M05). Deterministic; no sockets, no providers.
"""

from __future__ import annotations

import unittest
from typing import cast

from scarcity_router.gateway_adapters import (
    AdapterMessage,
    AdapterToolCall,
)
from scarcity_router.gateway_contracts import UsageTokens
from scarcity_router.providers.openai_http_core import (
    EMPTY_ARGUMENTS,
    SseStreamParser,
    ToolCallAccumulator,
    TranslationError,
    build_chat_completion_request,
    extract_usage,
    interpret_stream_frame,
    normalize_finish_reason,
    parse_chat_completion_response,
    provider_error_note,
)
from tests.openai_http_fixtures import make_call, preset


def good_message() -> dict[str, object]:
    return {"role": "assistant", "content": "hi"}


def wire_object(document: dict[str, object], key: str) -> object:
    return document[key]


def wire_messages(wire: dict[str, object]) -> list[dict[str, object]]:
    value = cast("list[object]", wire["messages"])
    return [cast("dict[str, object]", item) for item in value]


class RequestBuildTest(unittest.TestCase):
    """The preset translation policies, knob by knob."""

    def test_openai_preset_uses_modern_output_field_and_developer_role(self) -> None:
        call = make_call(
            max_output_tokens=512,
            reasoning_effort="high",
            messages=(
                AdapterMessage(role="developer", content="policy"),
                AdapterMessage(role="user", content="hello"),
            ),
            generation_params={"temperature": 0.5, "seed": 7},
        )
        wire = build_chat_completion_request(call, preset("openai-api").policy)
        self.assertEqual(wire["model"], "gpt-5.6-luna")
        self.assertEqual(wire["max_completion_tokens"], 512)
        self.assertEqual(wire["reasoning_effort"], "high")
        self.assertEqual(wire_messages(wire)[0]["role"], "developer")
        self.assertEqual(wire["seed"], 7)
        streaming = build_chat_completion_request(
            make_call(stream=True), preset("openai-api").policy
        )
        self.assertEqual(streaming["stream_options"], {"include_usage": True})

    def test_deepseek_preset_maps_reasoning_through_documented_values(self) -> None:
        for requested, expected in (
            ("minimal", "low"),
            ("low", "low"),
            ("medium", "high"),
            ("high", "high"),
        ):
            call = make_call(
                provider="deepseek",
                model="deepseek-chat",
                reasoning_effort=requested,
                max_output_tokens=256,
            )
            wire = build_chat_completion_request(call, preset("deepseek").policy)
            self.assertEqual(wire["max_tokens"], 256)
            self.assertEqual(
                wire["thinking"],
                {"type": "enabled", "reasoning_effort": expected},
            )
            self.assertNotIn("reasoning_effort", wire)

    def test_deepseek_preset_refuses_unevidenced_parameters(self) -> None:
        for parameter in ("stop", "seed", "parallel_tool_calls"):
            call = make_call(provider="deepseek", generation_params={parameter: 1})
            with self.assertRaises(TranslationError):
                _ = build_chat_completion_request(call, preset("deepseek").policy)

    def test_deepseek_preset_omits_documented_removed_penalties(self) -> None:
        call = make_call(
            provider="deepseek",
            generation_params={"frequency_penalty": 0.3, "temperature": 0.2},
        )
        wire = build_chat_completion_request(call, preset("deepseek").policy)
        self.assertNotIn("frequency_penalty", wire)
        self.assertNotIn("presence_penalty", wire)
        self.assertEqual(wire["temperature"], 0.2)

    def test_deepseek_preset_refuses_json_schema_response_format(self) -> None:
        call = make_call(
            provider="deepseek",
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": {}},
            },
        )
        with self.assertRaises(TranslationError):
            _ = build_chat_completion_request(call, preset("deepseek").policy)
        allowed = make_call(
            provider="deepseek", response_format={"type": "json_object"}
        )
        wire = build_chat_completion_request(allowed, preset("deepseek").policy)
        self.assertEqual(wire["response_format"], {"type": "json_object"})

    def test_deepseek_preset_refuses_developer_role(self) -> None:
        call = make_call(
            provider="deepseek",
            messages=(AdapterMessage(role="developer", content="policy"),),
        )
        with self.assertRaises(TranslationError):
            _ = build_chat_completion_request(call, preset("deepseek").policy)

    def test_openrouter_preset_maps_reasoning_object_and_omits_stream_options(
        self,
    ) -> None:
        call = make_call(
            provider="openrouter",
            model="z-ai-glm-5-3",
            reasoning_effort="low",
            max_output_tokens=256,
        )
        wire = build_chat_completion_request(call, preset("openrouter").policy)
        self.assertEqual(wire["reasoning"], {"effort": "low"})
        self.assertEqual(wire["max_tokens"], 256)
        # stream_options is not a documented OpenRouter request parameter:
        # omitted (usage arrives regardless), never guessed.
        self.assertNotIn("stream_options", wire)
        _ = wire

    def test_zai_preset_maps_thinking_and_restricts_tool_choice(self) -> None:
        call = make_call(
            resource_id="zai-http",
            provider="zai",
            model="glm-5.3",
            reasoning_effort="high",
        )
        wire = build_chat_completion_request(call, preset("zai-coding-plan").policy)
        self.assertEqual(wire["thinking"], {"type": "enabled"})
        self.assertEqual(wire["reasoning_effort"], "high")
        self.assertNotIn("stream_options", wire)
        forced = make_call(
            resource_id="zai-http",
            provider="zai",
            tool_choice={"type": "function", "function": {"name": "f"}},
            tools=({"type": "function", "function": {"name": "f"}},),
        )
        with self.assertRaises(TranslationError):
            _ = build_chat_completion_request(
                forced, preset("zai-coding-plan").policy
            )
        auto = make_call(resource_id="zai-http", provider="zai", tool_choice="auto")
        wire = build_chat_completion_request(auto, preset("zai-coding-plan").policy)
        self.assertEqual(wire["tool_choice"], "auto")

    def test_ollama_preset_omits_tool_choice_auto_and_refuses_forced(self) -> None:
        auto = make_call(
            resource_id="ollama-local",
            provider="ollama",
            model="llama3-8b",
            tool_choice="auto",
            tools=({"type": "function", "function": {"name": "f"}},),
        )
        wire = build_chat_completion_request(auto, preset("ollama").policy)
        self.assertNotIn("tool_choice", wire)
        self.assertIn("tools", wire)
        forced = make_call(
            resource_id="ollama-local",
            provider="ollama",
            tool_choice="required",
        )
        with self.assertRaises(TranslationError):
            _ = build_chat_completion_request(forced, preset("ollama").policy)

    def test_ollama_preset_refuses_developer_role_and_json_schema(self) -> None:
        with self.assertRaises(TranslationError):
            _ = build_chat_completion_request(
                make_call(
                    resource_id="ollama-local",
                    provider="ollama",
                    messages=(AdapterMessage(role="developer", content="x"),),
                ),
                preset("ollama").policy,
            )
        with self.assertRaises(TranslationError):
            _ = build_chat_completion_request(
                make_call(
                    resource_id="ollama-local",
                    provider="ollama",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "out", "schema": {}},
                    },
                ),
                preset("ollama").policy,
            )

    def test_wire_model_override_wins_over_identity(self) -> None:
        call = make_call(model="gpt-5-6-luna")  # safe-id registry name
        wire = build_chat_completion_request(
            call, preset("openai-api").policy, wire_model="gpt-5.6-luna"
        )
        self.assertEqual(wire["model"], "gpt-5.6-luna")

    def test_tool_messages_and_history_tool_calls_are_carried_verbatim(self) -> None:
        call = make_call(
            messages=(
                AdapterMessage(role="user", content="list files"),
                AdapterMessage(
                    role="assistant",
                    content=None,
                    tool_calls=(
                        AdapterToolCall(
                            id="call-1", name="list_files", arguments='{"p": "."}'
                        ),
                    ),
                ),
                AdapterMessage(role="tool", content="a.txt", tool_call_id="call-1"),
            )
        )
        wire = build_chat_completion_request(call, preset("openai-api").policy)
        messages = wire_messages(wire)
        self.assertEqual(len(messages), 3)
        tool_calls = cast("list[object]", messages[1]["tool_calls"])
        first_call = cast("dict[str, object]", tool_calls[0])
        function = cast("dict[str, object]", first_call["function"])
        self.assertEqual(function["name"], "list_files")
        self.assertEqual(messages[2]["tool_call_id"], "call-1")

    def test_non_streaming_request_omits_stream_options(self) -> None:
        wire = build_chat_completion_request(make_call(), preset("openai-api").policy)
        self.assertIs(wire["stream"], False)
        self.assertNotIn("stream_options", wire)

    def test_generation_params_cannot_overwrite_structural_fields(self) -> None:
        # F5 defense in depth: a structural key inside generation_params
        # must never open an overwrite path onto fields the core builds.
        from scarcity_router.providers.openai_http_core import (
            RESERVED_REQUEST_KEYS,
        )

        for name in ("model", "messages", "stream", "reasoning", "thinking",
                     "max_tokens", "tool_choice"):
            with self.subTest(name=name):
                call = make_call(generation_params={name: "injected"})
                with self.assertRaises(TranslationError):
                    _ = build_chat_completion_request(
                        call, preset("openai-api").policy
                    )
        self.assertIn("response_format", RESERVED_REQUEST_KEYS)
        self.assertIn("stream_options", RESERVED_REQUEST_KEYS)


class ResponseParseTest(unittest.TestCase):
    """Strict chat.completion parsing; drift fails closed."""

    def _completion(self, **overrides: object) -> dict[str, object]:
        choice: dict[str, object] = {
            "message": good_message(),
            "finish_reason": "stop",
        }
        choice.update(overrides)  # type: ignore[arg-type]
        return {"id": "x", "choices": [choice]}

    def test_parses_completion_with_usage(self) -> None:
        document = self._completion()
        document["usage"] = {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        }
        parsed = parse_chat_completion_response(document, preset("openai-api").policy)
        self.assertEqual(parsed.message.content, "hi")
        self.assertEqual(parsed.finish_reason, "stop")
        assert parsed.usage is not None
        self.assertEqual(parsed.usage.total_tokens, 5)

    def test_reasoning_content_is_tolerated_and_ignored(self) -> None:
        document = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": "hidden chain",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        parsed = parse_chat_completion_response(document, preset("deepseek").policy)
        self.assertEqual(parsed.message.content, "answer")

    def test_tool_call_response_normalizes_empty_arguments(self) -> None:
        document = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "f", "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        parsed = parse_chat_completion_response(document, preset("openai-api").policy)
        assert parsed.message.tool_calls
        self.assertEqual(parsed.message.tool_calls[0].arguments, EMPTY_ARGUMENTS)
        self.assertEqual(parsed.finish_reason, "tool_calls")

    def test_drift_cases_fail_closed(self) -> None:
        drift_documents: tuple[object, ...] = (
            "not-an-object",
            {"choices": []},
            {"choices": [{"message": good_message()}]},  # no finish_reason
            {"choices": [{"message": good_message(), "finish_reason": "content_filter"}]},
            {"choices": [{"message": good_message(), "finish_reason": "sensitive"}]},
            {
                "choices": [
                    {
                        "message": {"role": "tool", "content": "x"},
                        "finish_reason": "stop",
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant"}, "finish_reason": "stop"}]},
            {
                "choices": [{"message": good_message(), "finish_reason": "stop"}],
                "usage": "17",  # usage present but not an object: drift
            },
            {
                "choices": [{"message": good_message(), "finish_reason": "stop"}],
                "usage": {"prompt_tokens": None, "completion_tokens": 1},
            },
        )
        for document in drift_documents:
            with self.assertRaises(TranslationError):
                _ = parse_chat_completion_response(
                    document, preset("openai-api").policy
                )

    def test_malformed_usage_object_is_drift_but_absent_is_unavailable(self) -> None:
        with self.assertRaises(TranslationError):
            _ = parse_chat_completion_response(
                {
                    "choices": [
                        {
                            "message": good_message(),
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": "many", "completion_tokens": 1},
                },
                preset("openai-api").policy,
            )
        parsed = parse_chat_completion_response(
            {"choices": [{"message": good_message(), "finish_reason": "stop"}]},
            preset("openai-api").policy,
        )
        self.assertIsNone(parsed.usage)

    def test_non_openai_finish_reasons_have_no_mapping(self) -> None:
        for raw in ("aborted", "model_context_window_exceeded", "error", 42, None):
            with self.assertRaises(TranslationError):
                _ = normalize_finish_reason(raw)
        for raw, normalized in (
            ("stop", "stop"),
            ("length", "length"),
            ("tool_calls", "tool_calls"),
        ):
            self.assertEqual(normalize_finish_reason(raw), normalized)

    def test_extract_usage(self) -> None:
        self.assertIsNone(extract_usage(None))
        usage = extract_usage({"prompt_tokens": 1, "completion_tokens": 0})
        assert usage is not None
        self.assertIsInstance(usage, UsageTokens)
        with self.assertRaises(TranslationError):
            _ = extract_usage({"prompt_tokens": -1, "completion_tokens": 0})
        with self.assertRaises(TranslationError):
            _ = extract_usage({"prompt_tokens": 1.5, "completion_tokens": 0})


class SseParserTest(unittest.TestCase):
    """Incremental bounded SSE framing."""

    def _parse_all(self, chunks: list[bytes]) -> list[dict[str, object]]:
        parser = SseStreamParser(max_total_bytes=1024 * 1024)
        frames: list[dict[str, object]] = []
        for chunk in chunks:
            frames.extend(parser.feed(chunk))
        frames.extend(parser.close())
        return frames

    def test_frames_across_arbitrary_chunk_boundaries(self) -> None:
        text = (
            'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"y"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        encoded = text.encode("utf-8")
        for split in (1, 2, 7, 33, len(encoded)):
            frames = self._parse_all(
                [encoded[i : i + split] for i in range(0, len(encoded), split)]
            )
            self.assertEqual(len(frames), 2)

    def test_done_sentinel_and_comments(self) -> None:
        frames = self._parse_all(
            [
                b": keep-alive comment\n\n",
                b'data: {"a": 1}\n\n',
                b"data: [DONE]\n\n",
                b'data: {"after": true}\n\n',
            ]
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], {"a": 1})

    def test_crlf_and_multibyte_boundaries(self) -> None:
        frame_line = 'data: {"text":"café"}\n\n'.encode("utf-8")
        # Split inside the multi-byte é so decoding must carry state.
        split = frame_line.index("é".encode("utf-8")) + 1
        frames = self._parse_all([frame_line[:split], frame_line[split:]])
        self.assertEqual(frames[0]["text"], "café")

    def test_malformed_json_is_drift(self) -> None:
        parser = SseStreamParser(max_total_bytes=1024 * 1024)
        with self.assertRaises(TranslationError):
            _ = parser.feed(b"data: {not json}\n\n")

    def test_oversized_stream_is_drift(self) -> None:
        parser = SseStreamParser(max_total_bytes=16)
        with self.assertRaises(TranslationError):
            _ = parser.feed(b'data: {"long": "' + b"x" * 64 + b'"}\n\n')

    def test_truncated_final_line_is_drift(self) -> None:
        parser = SseStreamParser(max_total_bytes=1024 * 1024)
        _ = parser.feed(b'data: {"a": 1}\n\ndata: {"b"')
        with self.assertRaises(TranslationError):
            _ = parser.close()
        clean = SseStreamParser(max_total_bytes=1024 * 1024)
        _ = clean.feed(b'data: {"a": 1}\n\n')
        _ = clean.close()  # complete events: closing is clean


class StreamInterpretationTest(unittest.TestCase):
    """Frame interpretation: provider shape differences tolerated."""

    def test_openai_style_usage_chunk_with_empty_choices(self) -> None:
        view = interpret_stream_frame(
            {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        )
        self.assertIsNone(view.text_delta)
        assert view.usage is not None
        self.assertEqual(view.usage.total_tokens, 3)

    def test_openrouter_style_final_chunk_with_repeated_finish(self) -> None:
        frame: dict[str, object] = {
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 0},
        }
        view = interpret_stream_frame(frame)
        self.assertEqual(view.finish_reason, "stop")
        assert view.usage is not None

    def test_tool_call_fragments_collected(self) -> None:
        view = interpret_stream_frame(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {
                                        "name": "f",
                                        "arguments": '{"',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
        self.assertEqual(len(view.tool_fragments), 1)


class ToolCallAccumulatorTest(unittest.TestCase):
    def test_accumulates_fragments_into_complete_calls(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add_fragment(
            {"index": 0, "id": "call-1", "function": {"name": "f"}}
        )
        accumulator.add_fragment(
            {"index": 0, "function": {"arguments": '{"a": '}}
        )
        accumulator.add_fragment({"index": 0, "function": {"arguments": "1}"}})
        accumulator.add_fragment(
            {"index": 1, "id": "call-2", "function": {"name": "g", "arguments": "{}"}}
        )
        calls = accumulator.complete()
        self.assertEqual(
            [(c.id, c.name, c.arguments) for c in calls],
            [("call-1", "f", '{"a": 1}'), ("call-2", "g", "{}")],
        )

    def test_missing_id_or_name_is_drift(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add_fragment({"index": 0, "function": {"name": "f"}})
        with self.assertRaises(TranslationError):
            _ = accumulator.complete()
        accumulator = ToolCallAccumulator()
        accumulator.add_fragment({"index": 0, "id": "call-1"})
        with self.assertRaises(TranslationError):
            _ = accumulator.complete()

    def test_conflicting_ids_are_drift(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add_fragment({"index": 0, "id": "a"})
        with self.assertRaises(TranslationError):
            accumulator.add_fragment({"index": 0, "id": "b"})

    def test_mixed_indexed_and_unindexed_fragments_are_drift(self) -> None:
        # An absent index cannot be assigned once explicit indexes exist:
        # call boundaries are ambiguous, so the mix is drift (conservative).
        accumulator = ToolCallAccumulator()
        accumulator.add_fragment(
            {"index": 0, "id": "call-1", "function": {"name": "f"}}
        )
        accumulator.add_fragment({"function": {"arguments": "{}"}})
        with self.assertRaises(TranslationError):
            _ = accumulator.complete()

    def test_all_unindexed_single_call_still_completes(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add_fragment(
            {"id": "call-1", "function": {"name": "f", "arguments": "{}"}}
        )
        calls = accumulator.complete()
        self.assertEqual(
            [(c.id, c.name, c.arguments) for c in calls],
            [("call-1", "f", "{}")],
        )


class ErrorNoteTest(unittest.TestCase):
    def test_extracts_only_bounded_safe_tokens(self) -> None:
        note = provider_error_note(
            429,
            {
                "error": {
                    "message": "rate limited for prompt material",
                    "code": "rate_limit_exceeded",
                }
            },
        )
        self.assertIn("429", note)
        self.assertIn("rate_limit_exceeded", note)
        self.assertNotIn("rate limited for prompt material", note)

    def test_zai_shape_and_opaque_bodies(self) -> None:
        note = provider_error_note(400, {"code": 1210, "message": "invalid"})
        self.assertIn("1210", note)
        self.assertNotIn("invalid\n", note)
        self.assertEqual(provider_error_note(500, None), "provider returned HTTP 500")
        self.assertEqual(
            provider_error_note(503, {"error": {"code": "x" * 80}}),
            "provider returned HTTP 503",
        )


class AdapterCallImportGuard(unittest.TestCase):
    """The core stays import-clean of the coordinator and of any transport
    (the M05 worker-bridged seam rule: the worker can reuse it verbatim)."""

    def test_core_module_imports_no_coordinator_or_transport(self) -> None:
        import scarcity_router.providers.openai_http_core as core

        with open(core.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("gateway_coordinator", source)
        self.assertNotIn("import http", source)
        self.assertNotIn("ssl", source)
        self.assertNotIn("urllib", source)


if __name__ == "__main__":
    _ = unittest.main()
