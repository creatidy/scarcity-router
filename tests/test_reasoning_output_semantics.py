"""Reasoning output semantics (issue #158): preserve or fail closed.

The invariant under test: any provider reasoning output that an evidenced
preset can produce is either preserved through the normalized execution
path and rendered to the client with documented semantics, or the response
fails explicitly with a typed translation error. Silent loss is forbidden,
non-streaming AND streaming.

Layers covered (one semantic implementation, every transport):

- translation core: strict per-policy extraction (``reasoning_content``,
  ``reasoning`` + documented alias, drift on unevidenced/structured
  shapes), non-streaming and streaming;
- fragmented SSE transport: reasoning survives arbitrary byte splits
  including mid-UTF-8 codepoints;
- normalized seam: ``AdapterMessage.reasoning`` and the
  ``reasoning_delta`` chunk kind keep reasoning distinct from content;
- gateway rendering: one additive client-facing field
  (``choices[*].message.reasoning_content`` /
  ``choices[*].delta.reasoning_content``), present only when reasoning
  was preserved;
- worker protocol: the additive v4 ``reasoning`` message member
  (on top of the complete v3);
- worker-loopback translation: the shared core under the worker transport;
- composed server-direct HTTP: end-to-end preservation, drift fail-closed
  and privacy (reasoning never enters audit or error surfaces).

Everything is synthetic and deterministic; no live provider, no quota.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from typing import cast, override

from scarcity_router.gateway_adapters import (
    CHUNK_KINDS,
    CHUNK_REASONING_DELTA,
    CHUNK_TEXT_DELTA,
    AdapterMessage,
    AdapterStreamChunk,
    AdapterToolCall,
    CompletionOutcome,
)
from scarcity_router.gateway_contracts import UsageAccounting, UsageTokens
from scarcity_router.gateway_openai import chat_completion_payload, chunk_payload
from scarcity_router.providers.openai_http_core import (
    REASONING_OUTPUT_FIELDS,
    SseStreamParser,
    TranslationError,
    TranslationPolicy,
    build_chat_completion_request,
    interpret_stream_frame,
    parse_chat_completion_response,
)
from scarcity_router.providers.openai_http_presets import (
    DEEPSEEK_PRESET,
    GENERIC_PRESET,
    OLLAMA_PRESET,
    OPENAI_API_PRESET,
    OPENROUTER_PRESET,
    PRESETS,
    ZAI_CODING_PLAN_PRESET,
)
from scarcity_router.server_store import STORE_FILE_NAME
from scarcity_router.worker_local_translation import (
    LoopbackHTTPResponse,
    LoopbackStreamSession,
    OpenAICompatibleLoopbackTranslation,
)
from scarcity_router.worker_protocol import (
    SERVER_SUPPORTED_PROTOCOL_VERSIONS,
    WORKER_PROTOCOL_VERSION,
    WorkerProtocolError,
    chunk_from_dict,
    chunk_to_dict,
    message_from_dict,
    message_to_dict,
)
from tests.m10_fixtures import (
    PROMPT_MARKER,
    REASONING_MARKER,
    RESPONSE_MARKER,
    RealTimeServerHarness,
)
from tests.openai_http_fixtures import (
    ScriptedProviderServer,
    ScriptedResponse,
    completion_frame,
    make_call,
)
from tests.server_fixtures import FAKE_PROVIDER_SECRET
from tests.test_e2e_execution import (
    ZAI_RESOURCE_DOCUMENT,
    _server_observation,  # pyright: ignore[reportPrivateUsage] - shared e2e fixture helper
)

CONTENT_MARKER = "SYNTHETIC-CONTENT-MARKER-c1d4"


def _audit_payloads(data_dir: Path) -> list[str]:
    """Every persisted audit record payload (the privacy scan surface)."""
    connection = sqlite3.connect(data_dir / STORE_FILE_NAME)
    try:
        rows = cast(
            "list[tuple[object]]",
            connection.execute("SELECT payload FROM audit_records").fetchall(),
        )
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def deepseek_completion(
    *,
    content: str | None = CONTENT_MARKER,
    reasoning: object = REASONING_MARKER,
    tool_calls: list[dict[str, object]] | None = None,
    finish_reason: str = "stop",
) -> dict[str, object]:
    """One DeepSeek-shaped ``chat.completion`` document."""
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 7},
    }


def delta_frame(
    *,
    content: str | None = None,
    reasoning: object = None,
    reasoning_alias: object = None,
    finish_reason: str | None = None,
) -> dict[str, object]:
    delta: dict[str, object] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if reasoning_alias is not None:
        delta["reasoning"] = reasoning_alias
    return {
        "choices": [{"delta": delta, "finish_reason": finish_reason}],
    }


# ── Policy vocabulary ─────────────────────────────────────────────────────────


class PolicyVocabularyTests(unittest.TestCase):
    def test_preset_reasoning_output_policies_match_evidence(self) -> None:
        expected = {
            "openai-api": "none",
            "deepseek": "reasoning_content",
            "openrouter": "reasoning",
            "zai-coding-plan": "reasoning_content",
            "ollama": "none",
            "generic-openai": "none",
        }
        for preset_record in PRESETS:
            self.assertEqual(
                expected[preset_record.preset_id],
                preset_record.policy.reasoning_output_policy,
                preset_record.preset_id,
            )

    def test_unknown_reasoning_output_policy_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _ = TranslationPolicy(
                preset_id="synthetic",
                endpoint_path="/v1/chat/completions",
                max_tokens_field="max_tokens",
                developer_role="reject",
                tool_choice_policy="pass",
                response_format_policy="pass",
                reasoning_policy="reasoning_effort",
                reasoning_output_policy="auto_detect",
            )

    def test_known_reasoning_output_field_vocabulary_is_closed(self) -> None:
        self.assertEqual(
            frozenset({"reasoning_content", "reasoning", "reasoning_details"}),
            REASONING_OUTPUT_FIELDS,
        )


# ── Non-streaming translation core ────────────────────────────────────────────


class NonStreamingPreservationTests(unittest.TestCase):
    def test_plain_content_response_is_unchanged(self) -> None:
        parsed = parse_chat_completion_response(
            deepseek_completion(reasoning=None), DEEPSEEK_PRESET.policy
        )
        self.assertEqual(CONTENT_MARKER, parsed.message.content)
        self.assertIsNone(parsed.message.reasoning)
        self.assertEqual("stop", parsed.finish_reason)

    def test_deepseek_reasoning_content_is_preserved(self) -> None:
        parsed = parse_chat_completion_response(
            deepseek_completion(), DEEPSEEK_PRESET.policy
        )
        self.assertEqual(REASONING_MARKER, parsed.message.reasoning)
        self.assertEqual(CONTENT_MARKER, parsed.message.content)
        # Reasoning is distinct from content — never appended or merged.
        self.assertNotIn(REASONING_MARKER, parsed.message.content or "")

    def test_zai_reasoning_content_is_preserved(self) -> None:
        parsed = parse_chat_completion_response(
            deepseek_completion(), ZAI_CODING_PLAN_PRESET.policy
        )
        self.assertEqual(REASONING_MARKER, parsed.message.reasoning)

    def test_openrouter_reasoning_field_is_preserved(self) -> None:
        document = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": CONTENT_MARKER,
                        "reasoning": REASONING_MARKER,
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        parsed = parse_chat_completion_response(document, OPENROUTER_PRESET.policy)
        self.assertEqual(REASONING_MARKER, parsed.message.reasoning)

    def test_openrouter_documented_alias_is_accepted(self) -> None:
        parsed = parse_chat_completion_response(
            deepseek_completion(), OPENROUTER_PRESET.policy
        )
        self.assertEqual(REASONING_MARKER, parsed.message.reasoning)

    def test_openrouter_equal_alias_pair_is_reconciled(self) -> None:
        document = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": CONTENT_MARKER,
                        "reasoning": REASONING_MARKER,
                        "reasoning_content": REASONING_MARKER,
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        parsed = parse_chat_completion_response(document, OPENROUTER_PRESET.policy)
        self.assertEqual(REASONING_MARKER, parsed.message.reasoning)

    def test_openrouter_conflicting_alias_pair_fails_closed(self) -> None:
        document = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": CONTENT_MARKER,
                        "reasoning": REASONING_MARKER,
                        "reasoning_content": "different",
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        with self.assertRaises(TranslationError):
            _ = parse_chat_completion_response(document, OPENROUTER_PRESET.policy)

    def test_wrong_reasoning_type_is_never_stringified(self) -> None:
        for wrong in (
            {"steps": ["a"]},
            ["step"],
            7,
            True,
        ):
            with self.subTest(value=wrong):
                with self.assertRaises(TranslationError):
                    _ = parse_chat_completion_response(
                        deepseek_completion(reasoning=wrong),
                        DEEPSEEK_PRESET.policy,
                    )

    def test_structured_reasoning_details_fail_closed(self) -> None:
        structured = [{"type": "reasoning.text", "text": "hidden"}]
        for policy in (
            DEEPSEEK_PRESET.policy,
            OPENROUTER_PRESET.policy,
            OPENAI_API_PRESET.policy,
        ):
            with self.subTest(preset=policy.preset_id):
                document = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": CONTENT_MARKER,
                                "reasoning_details": structured,
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
                with self.assertRaises(TranslationError):
                    _ = parse_chat_completion_response(document, policy)

    def test_unevidenced_reasoning_field_on_none_policy_fails_closed(self) -> None:
        for policy in (OPENAI_API_PRESET.policy, OLLAMA_PRESET.policy, GENERIC_PRESET.policy):
            with self.subTest(preset=policy.preset_id):
                with self.assertRaises(TranslationError):
                    _ = parse_chat_completion_response(
                        deepseek_completion(), policy
                    )

    def test_unevidenced_null_reasoning_field_on_none_policy_fails_closed(self) -> None:
        # A null-valued known reasoning field still signals a response
        # schema the preset does not evidence — drift, never silence.
        document = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": CONTENT_MARKER,
                        "reasoning_content": None,
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        with self.assertRaises(TranslationError):
            _ = parse_chat_completion_response(document, OPENAI_API_PRESET.policy)

    def test_reasoning_only_response_is_representable(self) -> None:
        parsed = parse_chat_completion_response(
            deepseek_completion(content=None, finish_reason="length"),
            DEEPSEEK_PRESET.policy,
        )
        self.assertEqual(REASONING_MARKER, parsed.message.reasoning)
        self.assertIsNone(parsed.message.content)
        self.assertEqual("length", parsed.finish_reason)

    def test_reasoning_and_tool_calls_are_both_preserved(self) -> None:
        parsed = parse_chat_completion_response(
            deepseek_completion(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            ),
            DEEPSEEK_PRESET.policy,
        )
        self.assertEqual(REASONING_MARKER, parsed.message.reasoning)
        self.assertEqual(1, len(parsed.message.tool_calls))
        self.assertEqual("tool_calls", parsed.finish_reason)

    def test_empty_string_reasoning_is_absence(self) -> None:
        parsed = parse_chat_completion_response(
            deepseek_completion(reasoning=""), DEEPSEEK_PRESET.policy
        )
        self.assertIsNone(parsed.message.reasoning)
        self.assertEqual(CONTENT_MARKER, parsed.message.content)

    def test_drift_error_carries_field_names_never_values(self) -> None:
        try:
            _ = parse_chat_completion_response(
                deepseek_completion(), OPENAI_API_PRESET.policy
            )
        except TranslationError as exc:
            message = str(exc)
            self.assertIn("reasoning_content", message)
            self.assertNotIn(REASONING_MARKER, message)
        else:  # pragma: no cover - drift is asserted above
            self.fail("expected TranslationError")


# ── Streaming translation core ────────────────────────────────────────────────


class StreamingPreservationTests(unittest.TestCase):
    def test_deepseek_reasoning_delta_is_a_distinct_kind(self) -> None:
        view = interpret_stream_frame(delta_frame(reasoning="step "), DEEPSEEK_PRESET.policy)
        self.assertEqual("step ", view.reasoning_delta)
        self.assertIsNone(view.text_delta)

    def test_openrouter_reasoning_delta_is_preserved(self) -> None:
        view = interpret_stream_frame(
            delta_frame(reasoning_alias="thinking"), OPENROUTER_PRESET.policy
        )
        self.assertEqual("thinking", view.reasoning_delta)

    def test_openrouter_delta_alias_pair_reconciled_and_conflict_fails(self) -> None:
        equal = interpret_stream_frame(
            delta_frame(reasoning="a", reasoning_alias="a"), OPENROUTER_PRESET.policy
        )
        self.assertEqual("a", equal.reasoning_delta)
        with self.assertRaises(TranslationError):
            _ = interpret_stream_frame(
                delta_frame(reasoning="a", reasoning_alias="b"),
                OPENROUTER_PRESET.policy,
            )

    def test_reasoning_and_content_in_one_frame_both_arrive(self) -> None:
        view = interpret_stream_frame(
            delta_frame(content="answer", reasoning="why"),
            DEEPSEEK_PRESET.policy,
        )
        self.assertEqual("why", view.reasoning_delta)
        self.assertEqual("answer", view.text_delta)

    def test_reasoning_delta_with_finish_and_usage(self) -> None:
        frame = delta_frame(reasoning="tail", finish_reason="stop")
        frame["usage"] = {"prompt_tokens": 1, "completion_tokens": 2}
        view = interpret_stream_frame(frame, DEEPSEEK_PRESET.policy)
        self.assertEqual("tail", view.reasoning_delta)
        self.assertEqual("stop", view.finish_reason)
        assert view.usage is not None
        self.assertEqual(3, view.usage.total_tokens)

    def test_wrong_delta_reasoning_type_fails_closed(self) -> None:
        with self.assertRaises(TranslationError):
            _ = interpret_stream_frame(
                delta_frame(reasoning={"deep": True}), DEEPSEEK_PRESET.policy
            )

    def test_structured_delta_reasoning_fails_closed(self) -> None:
        frame = delta_frame()
        frame["choices"] = [
            {
                "delta": {
                    "reasoning_details": [{"type": "reasoning.summary", "summary": "s"}]
                },
                "finish_reason": None,
            }
        ]
        with self.assertRaises(TranslationError):
            _ = interpret_stream_frame(frame, OPENROUTER_PRESET.policy)

    def test_unevidenced_delta_on_none_policy_fails_closed(self) -> None:
        with self.assertRaises(TranslationError):
            _ = interpret_stream_frame(
                delta_frame(reasoning="hidden"), OPENAI_API_PRESET.policy
            )

    def test_plain_frames_remain_unchanged_under_none_policy(self) -> None:
        view = interpret_stream_frame(
            delta_frame(content="text"), OPENAI_API_PRESET.policy
        )
        self.assertEqual("text", view.text_delta)
        self.assertIsNone(view.reasoning_delta)


# ── Fragmented SSE transport ──────────────────────────────────────────────────


class SseFragmentationTests(unittest.TestCase):
    def _frames(self, chunks: list[bytes]) -> tuple[dict[str, object], ...]:
        parser = SseStreamParser(max_total_bytes=1_048_576)
        frames: list[dict[str, object]] = []
        for chunk in chunks:
            frames.extend(parser.feed(chunk))
        frames.extend(parser.close())
        return tuple(frames)

    def test_reasoning_split_at_arbitrary_byte_boundaries(self) -> None:
        event = (
            'data: {"choices":[{"delta":{"reasoning_content":"思考して step 1"}}]}\n\n'
        ).encode("utf-8")
        splits = [
            [event[i : i + 3] for i in range(0, len(event), 3)],
            [event[:1], event[1:7], event[7:]],
            [event],
        ]
        for parts in splits:
            with self.subTest(parts=len(parts)):
                frames = self._frames(parts)
                self.assertEqual(1, len(frames))
                view = interpret_stream_frame(frames[0], DEEPSEEK_PRESET.policy)
                self.assertEqual("思考して step 1", view.reasoning_delta)

    def test_reasoning_deterministically_precedes_content_across_frames(self) -> None:
        events = [
            'data: {"choices":[{"delta":{"reasoning_content":"a"}}]}\n\n',
            'data: {"choices":[{"delta":{"reasoning_content":"b"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"x"}}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        parser = SseStreamParser(max_total_bytes=1_048_576)
        order: list[str] = []
        for event in events:
            for frame in parser.feed(event.encode("utf-8")):
                view = interpret_stream_frame(frame, DEEPSEEK_PRESET.policy)
                if view.reasoning_delta is not None:
                    order.append(f"reasoning:{view.reasoning_delta}")
                if view.text_delta is not None:
                    order.append(f"text:{view.text_delta}")
                if view.finish_reason is not None:
                    order.append(f"finish:{view.finish_reason}")
        self.assertEqual(
            ["reasoning:a", "reasoning:b", "text:x", "finish:stop"], order
        )


# ── Normalized seam ───────────────────────────────────────────────────────────


class NormalizedSeamTests(unittest.TestCase):
    def test_reasoning_delta_chunk_kind_is_closed_vocabulary(self) -> None:
        self.assertIn(CHUNK_REASONING_DELTA, CHUNK_KINDS)

    def test_reasoning_chunk_carries_text_distinct_from_text_delta(self) -> None:
        chunk = AdapterStreamChunk(kind=CHUNK_REASONING_DELTA, text="why")
        self.assertEqual("why", chunk.text)
        self.assertNotEqual(CHUNK_TEXT_DELTA, chunk.kind)

    def test_text_is_invalid_outside_delta_kinds(self) -> None:
        with self.assertRaises(ValueError):
            _ = AdapterStreamChunk(kind="finish", text="nope")

    def test_normalized_message_round_trips_through_worker_protocol(self) -> None:
        message = AdapterMessage(
            role="assistant",
            content=CONTENT_MARKER,
            reasoning=REASONING_MARKER,
        )
        rebuilt = message_from_dict(message_to_dict(message))
        self.assertEqual(CONTENT_MARKER, rebuilt.content)
        self.assertEqual(REASONING_MARKER, rebuilt.reasoning)

    def test_worker_protocol_message_without_reasoning_unchanged(self) -> None:
        message = AdapterMessage(role="assistant", content=CONTENT_MARKER)
        document = message_to_dict(message)
        self.assertNotIn("reasoning", document)
        rebuilt = message_from_dict(document)
        self.assertIsNone(rebuilt.reasoning)

    def test_worker_protocol_version_bumped_for_the_member(self) -> None:
        # v3 is owned by merged D-062 (client-tool continuation); the
        # reasoning member is version 4 on top of the complete v3.
        self.assertEqual(4, WORKER_PROTOCOL_VERSION)
        self.assertEqual((4, 3, 2, 1), SERVER_SUPPORTED_PROTOCOL_VERSIONS)

    def test_reasoning_delta_chunk_round_trips_through_worker_protocol(self) -> None:
        chunk = AdapterStreamChunk(kind=CHUNK_REASONING_DELTA, text="step")
        rebuilt = chunk_from_dict(chunk_to_dict(chunk))
        self.assertEqual(CHUNK_REASONING_DELTA, rebuilt.kind)
        self.assertEqual("step", rebuilt.text)

    def test_request_history_carrying_reasoning_is_refused_not_dropped(self) -> None:
        call = make_call()
        history = list(call.messages) + [
            AdapterMessage(role="assistant", content="x", reasoning="hidden")
        ]
        from dataclasses import replace

        carrying = replace(call, messages=tuple(history))
        with self.assertRaises(TranslationError):
            _ = build_chat_completion_request(carrying, DEEPSEEK_PRESET.policy)


# ── Gateway rendering (public contract) ───────────────────────────────────────


def _outcome(message: AdapterMessage) -> CompletionOutcome:
    return CompletionOutcome(
        request_id="chatcmpl-test",
        created=1_700_000_000,
        model_echo="client-model",
        message=message,
        finish_reason="stop",
        usage=UsageAccounting(
            usage_source="provider_reported",
            provider_reported_usage=UsageTokens(prompt_tokens=1, completion_tokens=2),
        ),
    )


class GatewayRenderingTests(unittest.TestCase):
    def test_reasoning_renders_as_the_single_public_field(self) -> None:
        payload = chat_completion_payload(
            _outcome(
                AdapterMessage(
                    role="assistant",
                    content=CONTENT_MARKER,
                    reasoning=REASONING_MARKER,
                )
            )
        )
        choices = cast("list[object]", payload["choices"])
        message = cast("dict[str, object]", cast("dict[str, object]", choices[0])["message"])
        self.assertEqual(REASONING_MARKER, message["reasoning_content"])
        self.assertEqual(CONTENT_MARKER, message["content"])
        # One representation: the OpenRouter field name never leaks.
        self.assertNotIn("reasoning", message)

    def test_plain_message_renders_without_the_reasoning_key(self) -> None:
        payload = chat_completion_payload(
            _outcome(AdapterMessage(role="assistant", content=CONTENT_MARKER))
        )
        choices = cast("list[object]", payload["choices"])
        message = cast("dict[str, object]", cast("dict[str, object]", choices[0])["message"])
        self.assertNotIn("reasoning_content", message)

    def test_reasoning_delta_renders_as_delta_reasoning_content(self) -> None:
        payload = chunk_payload(
            request_id="chatcmpl-test",
            created=1_700_000_000,
            model_echo="client-model",
            chunk=AdapterStreamChunk(kind=CHUNK_REASONING_DELTA, text="why"),
            tool_call_index=0,
        )
        choices = cast("list[object]", payload["choices"])
        delta = cast("dict[str, object]", cast("dict[str, object]", choices[0])["delta"])
        self.assertEqual("why", delta["reasoning_content"])
        self.assertNotIn("content", delta)


# ── Worker-loopback translation (shared core, worker transport) ───────────────


class LoopbackTranslationTests(unittest.TestCase):
    def _translation(self) -> OpenAICompatibleLoopbackTranslation:
        return OpenAICompatibleLoopbackTranslation(DEEPSEEK_PRESET.policy)

    def test_non_streaming_response_preserves_reasoning(self) -> None:
        translation = self._translation()
        parsed = translation.parse_response(
            LoopbackHTTPResponse(
                status=200,
                body=json.dumps(deepseek_completion()).encode("utf-8"),
            )
        )
        message, finish_reason, _usage = parsed
        self.assertEqual(REASONING_MARKER, message.reasoning)
        self.assertEqual(CONTENT_MARKER, message.content)
        self.assertEqual("stop", finish_reason)

    def test_fragmented_stream_preserves_reasoning_and_order(self) -> None:
        translation = self._translation()
        session: LoopbackStreamSession = translation.open_stream_session()
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}',
            "",
            'data: {"choices":[{"delta":{"reasoning_content":"ink"}}]}',
            "",
            'data: {"choices":[{"delta":{"content":"out"}}]}',
            "",
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "",
            "data: [DONE]",
            "",
        ]
        order: list[tuple[str, str | None]] = []
        for line in lines:
            for chunk in session.feed_line(line):
                order.append(
                    (chunk.kind, chunk.text if chunk.text is not None else chunk.finish_reason)
                )
        for chunk in session.close():
            order.append(
                (chunk.kind, chunk.text if chunk.text is not None else chunk.finish_reason)
            )
        self.assertEqual(
            [
                (CHUNK_REASONING_DELTA, "th"),
                (CHUNK_REASONING_DELTA, "ink"),
                (CHUNK_TEXT_DELTA, "out"),
                ("finish", "stop"),
            ],
            order,
        )

    def test_drift_mid_stream_is_a_typed_worker_failure(self) -> None:
        translation = OpenAICompatibleLoopbackTranslation(OLLAMA_PRESET.policy)
        session = translation.open_stream_session()
        with self.assertRaises(WorkerProtocolError):
            # The data line buffers; the blank line dispatches the event.
            _ = session.feed_line(
                'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}'
            )
            _ = session.feed_line("")


# ── Privacy ───────────────────────────────────────────────────────────────────


class PrivacyTests(unittest.TestCase):
    def test_stream_drift_error_names_structure_not_content(self) -> None:
        try:
            _ = interpret_stream_frame(
                delta_frame(reasoning=REASONING_MARKER), OPENAI_API_PRESET.policy
            )
        except TranslationError as exc:
            self.assertNotIn(REASONING_MARKER, str(exc))
        else:  # pragma: no cover
            self.fail("expected TranslationError")

    def test_chunk_payload_leaks_no_reasoning_into_metadata(self) -> None:
        payload = chunk_payload(
            request_id="chatcmpl-test",
            created=1_700_000_000,
            model_echo="m",
            chunk=AdapterStreamChunk(kind=CHUNK_REASONING_DELTA, text=REASONING_MARKER),
            tool_call_index=0,
        )
        # The reasoning text appears ONLY in the delta field it belongs to.
        encoded = json.dumps(payload)
        self.assertIn(REASONING_MARKER, encoded)
        self.assertNotIn("x_scarcity_router", encoded)


# ── Worker protocol v4 gates (D-064 on top of the complete v3) ────────────────


class WorkerVersionGateTests(unittest.TestCase):
    """The no-silent-loss negotiation gate (D-064): a v4 worker holding a
    reasoning-bearing result under a negotiated version below 4 fails the
    attempt closed with a structural note; on v4 the reasoning member is
    serialized."""

    def test_reasoning_below_v4_fails_closed_not_dropped(self) -> None:
        from scarcity_router.worker_client import ensure_reasoning_representable

        message = AdapterMessage(
            role="assistant", content="answer", reasoning=REASONING_MARKER
        )
        with self.assertRaises(WorkerProtocolError) as caught:
            ensure_reasoning_representable(3, message)
        self.assertIn("below 4", str(caught.exception.message))
        # The structural note names no reasoning content.
        self.assertNotIn(REASONING_MARKER, caught.exception.message)

    def test_reasoning_on_v4_is_accepted(self) -> None:
        from scarcity_router.worker_client import ensure_reasoning_representable

        message = AdapterMessage(
            role="assistant", content="answer", reasoning=REASONING_MARKER
        )
        _ = ensure_reasoning_representable(4, message)

    def test_plain_result_below_v4_is_unaffected(self) -> None:
        from scarcity_router.worker_client import ensure_reasoning_representable

        _ = ensure_reasoning_representable(
            2, AdapterMessage(role="assistant", content="plain")
        )

    def test_worker_side_gate_is_wired_into_result_serialization(self) -> None:
        """The gate sits on the result-send path: a v3-negotiated session
        fails the reasoning-bearing result instead of serializing it."""
        from scarcity_router.gateway_adapters import AdapterResult
        from scarcity_router.worker_client import _ActiveSession  # pyright: ignore[reportPrivateUsage] - the wired path under test

        session = object.__new__(_ActiveSession)
        session._version = 3  # pyright: ignore[reportPrivateUsage] - minimal hand-built session
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = session._result_message(  # pyright: ignore[reportPrivateUsage] - the wired path under test
                "wa-1",
                AdapterResult(
                    status="completed",
                    calls=(),
                    message=AdapterMessage(
                        role="assistant",
                        content="answer",
                        reasoning=REASONING_MARKER,
                    ),
                    finish_reason="stop",
                ),
            )
        self.assertNotIn(REASONING_MARKER, caught.exception.message)


# ── Composed server-direct HTTP end to end ────────────────────────────────────


class ServerDirectReasoningEndToEndTests(RealTimeServerHarness):
    """Full composed-server dispatch against a scripted DeepSeek-shaped
    origin (the evidenced ``deepseek`` preset) and against a plain
    OpenAI-shaped origin (the ``openai-api`` preset, no evidenced
    reasoning output)."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()

    def _wire(self, *, adapter_id: str) -> ScriptedProviderServer:
        provider = ScriptedProviderServer()
        provider.start()
        self.addCleanup(provider.stop)
        document: dict[str, object] = {
            "provider_id": "zai-http",
            "adapter_id": adapter_id,
            "base_url": provider.origin,
            "secret": FAKE_PROVIDER_SECRET,
        }
        status, payload = self.admin_post("/control/providers", document)
        assert status == 200, payload
        status, payload = self.admin_post(
            "/control/resources", ZAI_RESOURCE_DOCUMENT
        )
        assert status == 200, payload
        self.plane.apply_resource_observation(
            _server_observation("zai-plan-1", "zai", "glm-5.3")
        )
        return provider

    def _exchange_completion(self) -> tuple[int, object]:
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": PROMPT_MARKER}],
            },
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        return status, payload

    def test_reasoning_is_preserved_to_the_client(self) -> None:
        provider = self._wire(adapter_id="deepseek")
        provider.enqueue(
            lambda _request: ScriptedResponse(
                200,
                {},
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": RESPONSE_MARKER,
                                    "reasoning_content": REASONING_MARKER,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 7},
                    }
                ).encode("utf-8"),
            )
        )
        status, payload = self._exchange_completion()
        self.assertEqual(200, status, payload)
        document = cast("dict[str, object]", payload)
        choices = cast("list[object]", document["choices"])
        message = cast(
            "dict[str, object]", cast("dict[str, object]", choices[0])["message"]
        )
        self.assertEqual(RESPONSE_MARKER, message["content"])
        self.assertEqual(REASONING_MARKER, message["reasoning_content"])
        # The exact evidenced endpoint path of the deepseek preset.
        self.assertEqual("/chat/completions", provider.last_request.path)
        # Privacy: the reasoning text never enters the audit store.
        for blob in _audit_payloads(self.data_dir):
            self.assertNotIn(REASONING_MARKER, blob)
            self.assertNotIn(RESPONSE_MARKER, blob)

    def test_reasoning_streams_as_delta_reasoning_content(self) -> None:
        provider = self._wire(adapter_id="deepseek")
        provider.enqueue_stream(
            [
                completion_frame(role="assistant"),
                {
                    "choices": [
                        {
                            "delta": {"reasoning_content": "thin"},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {"delta": {"reasoning_content": "king"}, "finish_reason": None}
                    ]
                },
                completion_frame(text=RESPONSE_MARKER),
                completion_frame(finish_reason="stop"),
            ]
        )
        connection = self.open_stream(
            "/v1/chat/completions",
            {
                "model": "sr-pin:zai-plan-1/zai/glm-5.3/max",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            bearer=self.client_key,
        )
        try:
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            payloads = self.read_sse_payloads(response)
        finally:
            connection.close()
        reasoning_deltas: list[str] = []
        text_deltas: list[str] = []
        order: list[str] = []
        for payload in payloads:
            choices = cast("list[object]", payload.get("choices", []))
            if not choices:
                continue
            delta = cast(
                "dict[str, object]", cast("dict[str, object]", choices[0])["delta"]
            )
            # Empty-string deltas are OpenAI role-chunk framing, not content.
            if delta.get("reasoning_content"):
                reasoning_deltas.append(cast("str", delta["reasoning_content"]))
                order.append("reasoning")
            if delta.get("content"):
                text_deltas.append(cast("str", delta["content"]))
                order.append("text")
        self.assertEqual([RESPONSE_MARKER], text_deltas)
        self.assertEqual(["thin", "king"], reasoning_deltas)
        # Deterministic order: both reasoning deltas precede the text.
        self.assertEqual(
            ["reasoning", "reasoning", "text"],
            [kind for kind in order if kind in ("reasoning", "text")],
        )

    def test_unevidenced_reasoning_fails_closed_over_http(self) -> None:
        provider = self._wire(adapter_id="openai-api")
        # The openai-api preset evidences no reasoning output, so a
        # reasoning-bearing response must fail explicitly — never return
        # the text with the reasoning silently discarded.
        provider.enqueue(
            lambda _request: ScriptedResponse(
                200,
                {},
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": RESPONSE_MARKER,
                                    "reasoning_content": REASONING_MARKER,
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ).encode("utf-8"),
            )
        )
        status, payload = self._exchange_completion()
        self.assertEqual(502, status, payload)
        encoded = json.dumps(payload)
        # Neither the reasoning nor the answer text surfaces: the response
        # failed closed and no partial content was silently kept.
        self.assertNotIn(REASONING_MARKER, encoded)
        self.assertIn("backend_failure", encoded)
        # The drift detail names structure, never content (privacy).
        self.assertNotIn(REASONING_MARKER, str(payload))

    def test_ordinary_client_responses_gain_no_reasoning_key(self) -> None:
        provider = self._wire(adapter_id="openai-api")
        provider.enqueue_completion(content=RESPONSE_MARKER)
        status, payload = self._exchange_completion()
        self.assertEqual(200, status, payload)
        document = cast("dict[str, object]", payload)
        choices = cast("list[object]", document["choices"])
        message = cast(
            "dict[str, object]", cast("dict[str, object]", choices[0])["message"]
        )
        self.assertNotIn("reasoning_content", message)
        self.assertEqual(RESPONSE_MARKER, message["content"])


# ── Tool-call interaction ─────────────────────────────────────────────────────


class ToolCallInteractionTests(unittest.TestCase):
    """Reasoning preservation never regresses client-owned tool semantics
    (issue #158): the accumulator's completeness guarantees hold with
    reasoning frames interleaved, and reasoning never influences
    tool-call parsing."""

    def _drive(
        self, events: list[str]
    ) -> tuple[list[str], tuple[AdapterToolCall, ...]]:
        from scarcity_router.providers.openai_http_core import ToolCallAccumulator

        parser = SseStreamParser(max_total_bytes=1_048_576)
        accumulator = ToolCallAccumulator()
        order: list[str] = []
        for event in events:
            for frame in parser.feed(event.encode("utf-8")):
                view = interpret_stream_frame(frame, DEEPSEEK_PRESET.policy)
                if view.reasoning_delta is not None:
                    order.append(f"reasoning:{view.reasoning_delta}")
                if view.text_delta is not None:
                    order.append(f"text:{view.text_delta}")
                for fragment in view.tool_fragments:
                    accumulator.add_fragment(fragment)
                if view.finish_reason is not None:
                    order.append(f"finish:{view.finish_reason}")
        for frame in parser.close():
            view = interpret_stream_frame(frame, DEEPSEEK_PRESET.policy)
            for fragment in view.tool_fragments:
                accumulator.add_fragment(fragment)
        return order, accumulator.complete()

    def test_reasoning_then_content_then_finish(self) -> None:
        order, _calls = self._drive(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"why"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"out"}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        self.assertEqual(
            ["reasoning:why", "text:out", "finish:stop"], order
        )

    def test_reasoning_then_tool_fragments_to_tool_calls_finish(self) -> None:
        order, calls = self._drive(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"need tool"}}]}\n\n',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"f","arguments":"{\\"a\\""}}]}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        self.assertEqual(["reasoning:need tool", "finish:tool_calls"], order)
        self.assertEqual(1, len(calls))
        self.assertEqual("f", calls[0].name)

    def test_reasoning_frames_between_tool_fragments(self) -> None:
        _order, calls = self._drive(
            [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"f"}}]}}]}\n\n',
                'data: {"choices":[{"delta":{"reasoning_content":"middle thought"}}]}\n\n',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]}}]}\n\n',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"c2","function":{"name":"g","arguments":"{}"}}]}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        self.assertEqual(2, len(calls))
        self.assertEqual(
            ("c1", "f", "{}"), (calls[0].id, calls[0].name, calls[0].arguments)
        )
        self.assertEqual(
            ("c2", "g", "{}"), (calls[1].id, calls[1].name, calls[1].arguments)
        )

    def test_usage_frame_after_tool_finish(self) -> None:
        order, calls = self._drive(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"r"}}]}\n\n',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"f","arguments":"{}"}}]}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        self.assertEqual(1, len(calls))
        self.assertEqual(["reasoning:r", "finish:tool_calls"], order)

    def test_reasoning_with_no_tool_call(self) -> None:
        order, calls = self._drive(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"only reasoning"}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        self.assertEqual(0, len(calls))
        self.assertEqual(["reasoning:only reasoning", "finish:stop"], order)

    def test_tool_only_response_with_no_reasoning(self) -> None:
        _order, calls = self._drive(
            [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"f","arguments":"{}"}}]}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        self.assertEqual(1, len(calls))
        self.assertEqual("f", calls[0].name)


if __name__ == "__main__":
    _ = unittest.main()
