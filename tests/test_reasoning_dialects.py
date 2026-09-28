"""Reasoning-dialect normalization (child #135 of program #132).

The bounded compatibility layer for the evidenced reasoning
representations observed verbatim in the 2026-09-26 ZCode capture:
``reasoning_effort`` (canonical), ``reasoning: {"effort": ...}``,
``thinking: {"type": "enabled"|"disabled"}`` and ``enable_thinking``.
Strictness is preserved — closed object shapes, agreeing effort forms,
enable/disable forms as consistency assertions only, no effort ever
guessed, D-054 max-only restrictions untouched.
"""

from __future__ import annotations

import json
import unittest
from typing import cast

from scarcity_router.gateway_contracts import GatewayError
from tests.gateway_fixtures import parse_chat_request
from tests.representative_client_fixture import representative_request
from tests.test_gateway_server import ServerHarness, as_dict

_USER = {"role": "user", "content": "hi"}


def _body(**extra: object) -> dict[str, object]:
    document: dict[str, object] = {"model": "deep-coding", "messages": [_USER]}
    document.update(extra)
    return document


class DialectNormalizationTests(unittest.TestCase):
    def test_exact_real_zcode_dialect_normalizes_to_max(self) -> None:
        """All four observed forms together: one canonical intent, max."""
        request = parse_chat_request(
            representative_request(model="deep-coding", include_output_limit=False)
        )
        self.assertEqual(request.reasoning_effort, "max")
        self.assertTrue(request.capabilities.requires_reasoning_controls)
        # The dialect fields are consumed by normalization — they are not
        # generation parameters and never reach an adapter.
        self.assertEqual(request.generation_params, {})

    def test_reasoning_effort_object_form_alone(self) -> None:
        request = parse_chat_request(_body(reasoning={"effort": "high"}))
        self.assertEqual(request.reasoning_effort, "high")
        self.assertTrue(request.capabilities.requires_reasoning_controls)

    def test_thinking_enabled_alone_with_effort_is_consistent(self) -> None:
        request = parse_chat_request(
            _body(reasoning_effort="medium", thinking={"type": "enabled"})
        )
        self.assertEqual(request.reasoning_effort, "medium")

    def test_enable_thinking_alone_with_effort_is_consistent(self) -> None:
        request = parse_chat_request(_body(reasoning_effort="low", enable_thinking=True))
        self.assertEqual(request.reasoning_effort, "low")

    def test_agreeing_duplicate_effort_forms_are_accepted(self) -> None:
        request = parse_chat_request(
            _body(reasoning_effort="max", reasoning={"effort": "max"})
        )
        self.assertEqual(request.reasoning_effort, "max")

    def test_conflicting_effort_forms_are_a_typed_conflict(self) -> None:
        with self.assertRaises(GatewayError) as caught:
            _ = parse_chat_request(
                _body(reasoning_effort="max", reasoning={"effort": "high"})
            )
        error = caught.exception
        self.assertEqual(error.http_status, 400)
        self.assertEqual(error.code, "conflicting_reasoning_parameters")

    def test_disabled_flag_plus_effort_is_a_conflict(self) -> None:
        for extra in (
            {"reasoning_effort": "max", "thinking": {"type": "disabled"}},
            {"reasoning_effort": "max", "enable_thinking": False},
            {"reasoning": {"effort": "high"}, "enable_thinking": False},
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(GatewayError) as caught:
                    _ = parse_chat_request(_body(**extra))
                self.assertEqual(caught.exception.code, "conflicting_reasoning_parameters")

    def test_contradictory_flags_are_a_conflict(self) -> None:
        for extra in (
            {"thinking": {"type": "disabled"}, "enable_thinking": True},
            {"thinking": {"type": "enabled"}, "enable_thinking": False},
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(GatewayError) as caught:
                    _ = parse_chat_request(_body(**extra))
                self.assertEqual(caught.exception.code, "conflicting_reasoning_parameters")

    def test_enabled_without_any_effort_fails_without_guessing(self) -> None:
        for extra in (
            {"enable_thinking": True},
            {"thinking": {"type": "enabled"}},
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(GatewayError) as caught:
                    _ = parse_chat_request(_body(**extra))
                self.assertEqual(caught.exception.code, "reasoning_effort_required")

    def test_empty_reasoning_object_asserts_nothing(self) -> None:
        """An empty object is no assertion (stream_options precedent):
        accepted, no effort, no reasoning requirement — nothing ignored."""
        request = parse_chat_request(_body(reasoning={}))
        self.assertIsNone(request.reasoning_effort)
        self.assertFalse(request.capabilities.requires_reasoning_controls)

    def test_disabled_only_means_no_reasoning_requested(self) -> None:
        for extra in (
            {"thinking": {"type": "disabled"}},
            {"enable_thinking": False},
            {"thinking": {"type": "disabled"}, "enable_thinking": False},
        ):
            with self.subTest(extra=extra):
                request = parse_chat_request(_body(**extra))
                self.assertIsNone(request.reasoning_effort)
                self.assertFalse(request.capabilities.requires_reasoning_controls)

    def test_unknown_nested_keys_remain_rejected(self) -> None:
        for extra, param in (
            ({"reasoning": {"effort": "max", "provider_specific": 1}}, "reasoning.provider_specific"),
            ({"thinking": {"type": "enabled", "budget_tokens": 5}}, "thinking.budget_tokens"),
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(GatewayError) as caught:
                    _ = parse_chat_request(_body(**extra))
                self.assertEqual(caught.exception.code, "unknown_parameter")
                self.assertEqual(caught.exception.param, param)

    def test_malformed_dialect_shapes_are_rejected(self) -> None:
        for extra in (
            {"reasoning": "max"},
            {"thinking": "enabled"},
            {"enable_thinking": "true"},
            {"reasoning": {"effort": "bogus"}},
            {"thinking": {"type": "sometimes"}},
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(GatewayError):
                    _ = parse_chat_request(_body(**extra))

    def test_echoed_reasoning_output_is_unknown_parameter(self) -> None:
        """D-062 (#158): reasoning output is response-only. A client that
        echoes `reasoning_content` back in message history receives the
        standard typed rejection — never a silent drop and never an
        unexplained forward to the backend."""
        echo: dict[str, object] = {
            "model": "deep-coding",
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "hidden chain",
                },
                {"role": "user", "content": "again"},
            ],
        }
        with self.assertRaises(GatewayError) as caught:
            _ = parse_chat_request(echo)
        self.assertEqual(caught.exception.code, "unknown_parameter")
        self.assertEqual(caught.exception.param, "messages[1].reasoning_content")

    def test_unknown_top_level_keys_stay_unknown_parameter(self) -> None:
        with self.assertRaises(GatewayError) as caught:
            _ = parse_chat_request(_body(bogus_dialect={"type": "enabled"}))
        self.assertEqual(caught.exception.code, "unknown_parameter")

    def test_normalized_effort_still_respects_the_model_boundary(self) -> None:
        """D-054 intact: normalization never relaxes the family boundary.

        A dialect-normalized effort the exact identity does not offer
        still fails at the model boundary (child B's typed rejection) —
        the dialect layer changes parsing, never eligibility.
        """
        from tests.gateway_fixtures import CLIENT_ID, audit_records, make_application

        application = make_application()
        body = representative_request(
            model="gpt-5.6-luna", include_output_limit=False
        )
        # Both effort forms agree on an effort luna lacks; the dialects
        # normalize to it and the model boundary rejects it.
        body["reasoning_effort"] = "ultra"
        body["reasoning"] = {"effort": "ultra"}
        request = parse_chat_request(body)
        self.assertEqual(request.reasoning_effort, "ultra")
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.code, "unsupported_reasoning_effort")
        self.assertEqual(audit_records(application)[-1].result_status, "rejected")

    def test_max_only_family_accepts_the_normalized_max(self) -> None:
        """The representative path end to end: dialects normalize to max
        and the exact max-only identity executes — no leak, no downgrade."""
        from tests.gateway_fixtures import CLIENT_ID, audit_records, make_application

        application = make_application()
        body = representative_request(
            model="gpt-5.6-luna", include_output_limit=False
        )
        request = parse_chat_request(body)
        self.assertEqual(request.reasoning_effort, "max")
        _ = application.execute(client_id=CLIENT_ID, request=request)
        audit = audit_records(application)[-1]
        self.assertEqual(audit.selected_target, audit.executed_target)
        selected = audit.selected_target
        assert selected is not None
        self.assertEqual(selected.model, "gpt-5.6-luna")
        self.assertEqual(selected.variant, "max")


class RepresentativeBoundaryTests(ServerHarness):
    """The conflict path stays a clean typed 400 at the public boundary."""

    def test_conflicting_dialects_are_a_clean_400_at_the_boundary(self) -> None:
        port = self.make_server()
        body = representative_request(model="deep-coding", include_output_limit=False)
        body["reasoning_effort"] = "high"  # contradicts reasoning.effort max
        response = self.post_chat(port, body)
        self.assertEqual(response.status, 400)
        payload = cast("dict[str, object]", json.loads(response.read()))
        error = as_dict(payload["error"])
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["code"], "conflicting_reasoning_parameters")


if __name__ == "__main__":
    import unittest

    _ = unittest.main()
