"""Execution-surface contract tests (M03, issue #88).

Covers the frozen building blocks: admission limits, the OpenAI-compatible
error vocabulary (never the machine-interface v1 codes), the client-key
directory, honest usage accounting, the D-043 audit record and its bounded
in-memory trail, the ``model``-field resolution (alias or exact pinned
reference) and the strict chat-completions request parser with its
capability inference. Deterministic and synthetic throughout.
"""

from __future__ import annotations

import datetime
import unittest
from dataclasses import replace
from typing import cast

from scarcity_router.gateway_audit import (
    RESULT_COMPLETED,
    RESULT_REJECTED,
    AuditRecord,
    BoundedAuditTrail,
    ExecutedTarget,
)
from scarcity_router.gateway_contracts import (
    ERROR_TYPES,
    ERROR_TYPE_INVALID_REQUEST,
    EXECUTION_SURFACE_VERSION,
    ClientKeyDirectory,
    GatewayError,
    GatewayLimits,
    UsageAccounting,
    UsageTokens,
    hash_client_key,
)
from scarcity_router.gateway_openai import (
    estimate_input_tokens,
    models_list_payload,
)
from scarcity_router.routing_core import ClientRoutingProfile
from tests.gateway_fixtures import (
    CLIENT_ID,
    CLIENT_KEY,
    audit_records,
    canonical,
    make_application,
    parse_chat_request,
)

EXEC_AT = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.timezone.utc)

_USER_ONLY: dict[str, object] = {"role": "user", "content": "hi"}


class ExecutionSurfaceVersionTests(unittest.TestCase):
    def test_surface_version_is_frozen_one(self) -> None:
        self.assertEqual(EXECUTION_SURFACE_VERSION, 1)


class GatewayLimitsTests(unittest.TestCase):
    def test_safe_defaults_are_bounded(self) -> None:
        limits = GatewayLimits()
        for value in limits.to_dict().values():
            self.assertGreaterEqual(value, 1)

    def test_every_limit_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            _ = GatewayLimits(max_concurrent_executions=0)
        with self.assertRaises(ValueError):
            _ = GatewayLimits(execution_time_limit_seconds=-1)

    def test_per_client_limit_cannot_exceed_global(self) -> None:
        with self.assertRaises(ValueError):
            _ = GatewayLimits(
                max_concurrent_executions=1, max_concurrent_executions_per_client=2
            )

    def test_round_trip_rejects_unknown_keys(self) -> None:
        limits = GatewayLimits(max_output_tokens=99)
        restored = GatewayLimits.from_dict(limits.to_dict())
        self.assertEqual(restored, limits)
        with self.assertRaises(ValueError):
            _ = GatewayLimits.from_dict({"unlimited": True})


class GatewayErrorTests(unittest.TestCase):
    def test_error_vocabulary_is_openai_compatible(self) -> None:
        error = GatewayError.invalid_request("bad", code="unknown_parameter", param="n")
        self.assertEqual(error.http_status, 400)
        self.assertEqual(error.error_type, ERROR_TYPE_INVALID_REQUEST)
        self.assertEqual(
            error.to_payload(),
            {
                "error": {
                    "message": "bad",
                    "type": "invalid_request_error",
                    "param": "n",
                    "code": "unknown_parameter",
                }
            },
        )

    def test_error_vocabulary_never_reuses_machine_interface_codes(self) -> None:
        """The closed machine-interface vocabulary must not appear here."""
        for error in (
            GatewayError.invalid_request("x"),
            GatewayError.authentication(),
            GatewayError.permission("x"),
            GatewayError.not_found("x"),
            GatewayError.rate_limit("x"),
            GatewayError.timeout("x"),
            GatewayError.api("x"),
        ):
            self.assertIn(error.error_type, ERROR_TYPES)
            self.assertNotEqual(error.error_type, "invalid_request")
            self.assertNotEqual(error.error_type, "internal_error")

    def test_api_errors_require_5xx(self) -> None:
        with self.assertRaises(ValueError):
            _ = GatewayError.api("x", http_status=404)

    def test_messages_are_bounded_safe_text(self) -> None:
        with self.assertRaises(ValueError):
            _ = GatewayError.invalid_request("")
        with self.assertRaises(ValueError):
            _ = GatewayError.invalid_request("x" * 501)


class ClientKeyDirectoryTests(unittest.TestCase):
    def test_authenticate_accepts_only_the_issued_key(self) -> None:
        directory = ClientKeyDirectory.from_secrets({CLIENT_ID: CLIENT_KEY})
        self.assertEqual(directory.authenticate(CLIENT_KEY), CLIENT_ID)
        self.assertIsNone(directory.authenticate("sk-sr-wrong"))
        self.assertIsNone(directory.authenticate(""))
        self.assertIsNone(directory.authenticate(None))

    def test_directory_stores_only_hashes(self) -> None:
        directory = ClientKeyDirectory.from_secrets({CLIENT_ID: CLIENT_KEY})
        serialized = repr(sorted(directory.client_ids))
        self.assertNotIn(CLIENT_KEY, serialized)

    def test_hash_is_sha256_hex(self) -> None:
        digest = hash_client_key(CLIENT_KEY)
        self.assertEqual(len(digest), 64)
        _ = int(digest, 16)  # must parse as hex
        self.assertEqual(digest, hash_client_key(CLIENT_KEY))

    def test_unknown_client_shape_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _ = ClientKeyDirectory({"Bad Client": hash_client_key("x")})
        with self.assertRaises(ValueError):
            _ = ClientKeyDirectory({"client-a": "tooshort"})


class UsageAccountingTests(unittest.TestCase):
    def test_sources_are_enforced_against_their_totals(self) -> None:
        reported = UsageTokens(3, 4)
        estimated = UsageTokens(0, 9)
        mixed = UsageAccounting(
            usage_source="mixed",
            provider_reported_usage=reported,
            estimated_usage=estimated,
        )
        self.assertEqual(mixed.provider_reported_usage, reported)
        with self.assertRaises(ValueError):
            _ = UsageAccounting(usage_source="provider_reported")
        with self.assertRaises(ValueError):
            _ = UsageAccounting(
                usage_source="unavailable", provider_reported_usage=reported
            )
        with self.assertRaises(ValueError):
            _ = UsageAccounting(usage_source="estimated")
        with self.assertRaises(ValueError):
            _ = UsageAccounting(usage_source="mixed", estimated_usage=estimated)

    def test_totals_add_and_serialize(self) -> None:
        tokens = UsageTokens(2, 3) + UsageTokens(4, 5)
        self.assertEqual(tokens.total_tokens, 14)
        self.assertEqual(
            tokens.to_dict(),
            {"prompt_tokens": 6, "completion_tokens": 8, "total_tokens": 14},
        )


def _target(resource_id: str = "openai-http") -> ExecutedTarget:
    return ExecutedTarget(
        resource_id=resource_id,
        provider="openai",
        model="gpt-5.6-luna",
        variant="medium",
    )


def _completed_record(**overrides: object) -> AuditRecord:
    record = AuditRecord(
        request_id="chatcmpl-0001",
        client_id=CLIENT_ID,
        decision_id="rd-" + "a" * 32,
        routing_profile="gateway-core",
        routing_policy_version=1,
        registry_revision=3,
        registry_generated_at=canonical(EXEC_AT),
        selected_target=_target(),
        executed_target=_target(),
        adapter_name="synthetic-http",
        adapter_version="1.2.3",
        started_at=canonical(EXEC_AT),
        ended_at=canonical(EXEC_AT),
        result_status=RESULT_COMPLETED,
        reason_codes=("completed",),
        provider_reported_usage=UsageTokens(1, 2),
        call_count=1,
    )
    if not overrides:
        return record
    return replace(record, **overrides)  # type: ignore[arg-type]


class AuditRecordTests(unittest.TestCase):
    def test_completed_record_carries_the_frozen_d043_field_set(self) -> None:
        record = _completed_record()
        payload = record.to_dict()
        for field in (
            "request_id",
            "decision_id",
            "client_id",
            "routing_profile",
            "routing_policy_version",
            "registry_revision",
            "registry_generated_at",
            "selected_target",
            "executed_target",
            "adapter_name",
            "adapter_version",
            "started_at",
            "ended_at",
            "result_status",
            "reason_codes",
            "provider_reported_usage",
            "call_count",
        ):
            self.assertIn(field, payload)

    def test_record_has_no_content_bearing_field(self) -> None:
        """No field of the audit record can carry prompt/response text."""
        allowed_text_fields = {
            "request_id",
            "client_id",
            "decision_id",
            "routing_profile",
            "registry_generated_at",
            "adapter_name",
            "adapter_version",
            "started_at",
            "ended_at",
            "result_status",
        }
        record = _completed_record()
        for key, value in record.to_dict().items():
            if isinstance(value, str):
                self.assertIn(
                    key,
                    allowed_text_fields,
                    f"free-text field {key!r} is not part of the frozen set",
                )

    def test_rejections_carry_no_executed_target(self) -> None:
        record = AuditRecord(
            request_id="chatcmpl-0002",
            client_id=CLIENT_ID,
            started_at=canonical(EXEC_AT),
            ended_at=canonical(EXEC_AT),
            result_status=RESULT_REJECTED,
            reason_codes=("model_not_found",),
        )
        self.assertIsNone(record.to_dict()["executed_target"])

    def test_honesty_invariants(self) -> None:
        with self.assertRaises(ValueError):
            # completed with zero calls is impossible
            _ = _completed_record(call_count=0)
        with self.assertRaises(ValueError):
            # an executed target without a selected target is impossible
            _ = AuditRecord(
                request_id="chatcmpl-0003",
                client_id=CLIENT_ID,
                started_at=canonical(EXEC_AT),
                ended_at=canonical(EXEC_AT),
                result_status=RESULT_COMPLETED,
                reason_codes=("completed",),
                executed_target=_target(),
                call_count=1,
            )
        with self.assertRaises(ValueError):
            # cancellation without an executed target is impossible
            _ = AuditRecord(
                request_id="chatcmpl-0004",
                client_id=CLIENT_ID,
                started_at=canonical(EXEC_AT),
                ended_at=canonical(EXEC_AT),
                result_status="cancelled",
                reason_codes=("client_disconnected",),
            )

    def test_reason_codes_are_safe_ids(self) -> None:
        with self.assertRaises(ValueError):
            _ = _completed_record(reason_codes=("has spaces",))


class BoundedAuditTrailTests(unittest.TestCase):
    def test_retention_is_bounded_by_count(self) -> None:
        trail = BoundedAuditTrail(max_records=3, max_age_seconds=10**9)
        for index in range(5):
            trail.append(
                AuditRecord(
                    request_id=f"chatcmpl-{index:04d}",
                    client_id=CLIENT_ID,
                    started_at=canonical(EXEC_AT),
                    ended_at=canonical(EXEC_AT),
                    result_status=RESULT_REJECTED,
                    reason_codes=("model_not_found",),
                )
            )
        self.assertEqual(len(trail), 3)
        ids = [record.request_id for record in trail.snapshot()]
        self.assertEqual(ids, ["chatcmpl-0002", "chatcmpl-0003", "chatcmpl-0004"])

    def test_retention_is_bounded_by_age(self) -> None:
        from datetime import timedelta

        trail = BoundedAuditTrail(max_records=100, max_age_seconds=60)
        for offset in (0, 120, 300):
            moment = EXEC_AT + timedelta(seconds=offset)
            trail.append(
                AuditRecord(
                    request_id=f"chatcmpl-{offset:04d}",
                    client_id=CLIENT_ID,
                    started_at=canonical(moment),
                    ended_at=canonical(moment),
                    result_status=RESULT_REJECTED,
                    reason_codes=("model_not_found",),
                )
            )
        ids = [record.request_id for record in trail.snapshot()]
        # Everything older than 60s relative to the newest record is pruned.
        self.assertEqual(ids, ["chatcmpl-0300"])


class ModelFieldResolutionTests(unittest.TestCase):
    def test_unknown_model_is_model_not_found(self) -> None:
        application = make_application()
        request = parse_chat_request(
            {"model": "gpt-5.6-luna", "messages": [_USER_ONLY]}
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        error = caught.exception
        self.assertEqual(error.http_status, 404)
        self.assertEqual(error.code, "model_not_found")

    def test_alias_resolution_goes_through_the_profile(self) -> None:
        application = make_application()
        request = parse_chat_request({"model": "zai-only", "messages": [_USER_ONLY]})
        _ = application.execute(client_id=CLIENT_ID, request=request)
        audit = audit_records(application)[-1]
        self.assertEqual(audit.routing_profile, "gateway-core")
        self.assertEqual(
            audit.selected_target,
            ExecutedTarget(
                resource_id="zai-http", provider="zai", model="glm-5.3", variant="high"
            ),
        )

    def test_pinned_reference_parses_exactly(self) -> None:
        from scarcity_router.gateway_coordinator import parse_pinned_reference
        from scarcity_router.selection_types import ModelIdentity

        pin = parse_pinned_reference(
            "sr-pin:openai-http/openai/gpt-5.6-luna/max@rd-abc123"
        )
        self.assertEqual(pin.resource_id, "openai-http")
        self.assertEqual(
            pin.model,
            ModelIdentity(provider="openai", model="gpt-5.6-luna", variant="max"),
        )
        self.assertEqual(pin.decision_id, "rd-abc123")

    def test_incomplete_or_malformed_pins_are_request_errors(self) -> None:
        from scarcity_router.gateway_coordinator import parse_pinned_reference

        for bad in (
            "sr-pin:openai-http/openai/gpt-5.6-luna",  # missing variant
            "sr-pin:openai-http/openai/gpt-5.6-luna/max/extra",
            "sr-pin:BAD-RESOURCE/openai/gpt-5.6-luna/max",
            "sr-pin:openai-http/openai/gpt-5.6-luna/max@not a decision",
        ):
            with self.assertRaises(GatewayError, msg=bad):
                _ = parse_pinned_reference(bad)

    def test_models_listing_lists_exactly_the_aliases(self) -> None:
        payload = models_list_payload(("deep-coding", "zai-only"))
        data = cast("list[dict[str, object]]", payload["data"])
        self.assertEqual(payload["object"], "list")
        ids = [entry["id"] for entry in data]
        self.assertEqual(ids, ["deep-coding", "zai-only"])


class ChatCompletionParserTests(unittest.TestCase):
    def test_minimal_request_defaults(self) -> None:
        request = parse_chat_request({"model": "deep-coding", "messages": [_USER_ONLY]})
        self.assertFalse(request.stream)
        self.assertFalse(request.capabilities.requires_tool_calls)
        # "hi" is 2 chars -> the conservative floor is ceil(2/4) == 1.
        self.assertEqual(request.capabilities.estimated_input_tokens, 1)
        self.assertIsNone(request.capabilities.requested_output_tokens)

    def test_capability_inference_from_structure(self) -> None:
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "list files"},
                    {"role": "tool", "content": "a.txt", "tool_call_id": "call-1"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "list_files", "parameters": {}},
                    }
                ],
                "tool_choice": "auto",
                "response_format": {"type": "json_object"},
                "stream": True,
                "reasoning_effort": "low",
                "max_completion_tokens": 64,
            }
        )
        caps = request.capabilities
        self.assertTrue(caps.requires_tool_calls)
        self.assertTrue(caps.requires_tool_results)
        self.assertTrue(caps.requires_roles_history)
        self.assertTrue(caps.requires_structured_output)
        self.assertTrue(caps.requires_streaming)
        self.assertTrue(caps.requires_reasoning_controls)
        self.assertEqual(caps.requested_output_tokens, 64)
        self.assertGreater(caps.estimated_input_tokens or 0, 0)

    def test_unknown_keys_are_rejected_with_their_name(self) -> None:
        for key in ("logprobs", "logit_bias", "n", "service_tier", "functions"):
            with self.assertRaises(GatewayError) as caught:
                _ = parse_chat_request(
                    {"model": "m", "messages": [_USER_ONLY], key: True}
                )
            self.assertEqual(caught.exception.param, key)

    def test_structural_rejections(self) -> None:
        cases: list[tuple[dict[str, object], str | None]] = [
            ({"model": "m", "messages": []}, "messages"),
            (
                {"model": "m", "messages": [{"role": "wizard", "content": "hi"}]},
                "messages",
            ),
            (
                {"model": "m", "messages": [{"role": "tool", "content": "result"}]},
                "messages[0].tool_call_id",
            ),
            (
                {
                    "model": "m",
                    "messages": [_USER_ONLY],
                    "max_tokens": 5,
                    "max_completion_tokens": 6,
                },
                "max_tokens",
            ),
            (
                {
                    "model": "m",
                    "messages": [_USER_ONLY],
                    "stream_options": {"include_usage": True},
                },
                "stream_options",
            ),
            (
                {
                    "model": "m",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "x"}]}
                    ],
                },
                "messages",
            ),
            (
                {
                    "model": "m",
                    "messages": [_USER_ONLY],
                    "tools": [{"type": "code_interpreter"}],
                },
                "tools",
            ),
            ({"messages": [_USER_ONLY]}, "model"),
        ]
        for document, param in cases:
            with self.assertRaises(GatewayError) as caught:
                _ = parse_chat_request(document)
            if param is not None:
                self.assertEqual(caught.exception.param, param)

    def test_context_estimate_is_a_conservative_floor(self) -> None:
        parsed = parse_chat_request(
            {"model": "m", "messages": [{"role": "user", "content": "a" * 40}]}
        )
        self.assertEqual(estimate_input_tokens(parsed.messages, ()), 10)  # ceil(40/4)


class AliasSafetyTests(unittest.TestCase):
    def test_alias_table_rejects_pin_prefix_and_unsafe_ids(self) -> None:
        from scarcity_router.errors import SelectionContractError
        from scarcity_router.gateway_coordinator import RoutingAliasTable

        profile = ClientRoutingProfile(profile_id="gateway-core")
        with self.assertRaises(ValueError):
            _ = RoutingAliasTable({"sr-pin:x": profile})
        with self.assertRaises((ValueError, SelectionContractError)):
            _ = RoutingAliasTable({"bad alias": profile})


if __name__ == "__main__":
    _ = unittest.main()
