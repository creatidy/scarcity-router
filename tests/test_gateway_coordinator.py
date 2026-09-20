"""Execution-coordinator lifecycle tests (M03, issue #88, D-043).

Covers the frozen lifecycle rules with the deterministic synthetic
adapter: non-streaming execution, the streaming lifecycle, exact pinned
target admission (including ``pinned_model_not_bound``), cancellation and
client-disconnect propagation, timeouts, concurrency exhaustion,
compatibility rejection (UNKNOWN/UNSUPPORTED fail closed, including the
coordinator's supplemental ``roles_history``/``tool_results`` admission
gate), dispatch failure without any cross-target reroute, ambiguous
execution-state safety (no blind retry), honest multi-call usage
accounting, audit completeness and the absence of prompt/response content
in the audit trail. Deterministic: no live providers, no wall clock.
"""

from __future__ import annotations

import json
import threading
import unittest
from typing import cast

from scarcity_router.gateway_adapters import (
    AdapterStreamChunk,
    ClientDisconnectedError,
)
from scarcity_router.gateway_audit import (
    RESULT_CANCELLED,
    RESULT_COMPLETED,
    RESULT_FAILED,
    RESULT_FAILED_AMBIGUOUS,
    RESULT_REJECTED,
    RESULT_TIMED_OUT,
)
from scarcity_router.gateway_contracts import GatewayError
from scarcity_router.gateway_coordinator import resolve_model_string
from scarcity_router.resource_state import ResourceRegistry
from tests.gateway_fixtures import (
    BlockingAdapter,
    CLIENT_ID,
    GatewayApplication,
    ScriptedAdapter,
    audit_records,
    T_EVAL,
    ambiguous_failure_behavior,
    build_cells,
    build_registry,
    canonical,
    echo_behavior,
    make_application,
    multi_call_behavior,
    parse_chat_request,
    permanent_failure_behavior,
    timeout_behavior,
    tool_call_behavior,
)

_USER_ONLY = {"role": "user", "content": "SECRET-CONTENT-MARKER-XYZ"}


class NonStreamingExecutionTests(unittest.TestCase):
    def test_round_trip_completes_with_openai_shaped_outcome(self) -> None:
        application = make_application()
        request = parse_chat_request(
            {"model": "deep-coding", "messages": [_USER_ONLY]}
        )
        outcome = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(outcome.request_id, "chatcmpl-0001")
        self.assertEqual(outcome.finish_reason, "stop")
        self.assertEqual(outcome.message.role, "assistant")
        self.assertEqual(outcome.model_echo, "deep-coding")
        self.assertIsNotNone(outcome.decision_id)
        self.assertEqual(outcome.usage.usage_source, "provider_reported")

    def test_reasoning_controls_are_carried_to_dispatch(self) -> None:
        application = make_application()
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [_USER_ONLY],
                "reasoning_effort": "high",
            }
        )
        _ = application.execute(client_id=CLIENT_ID, request=request)
        adapter = application.adapters.resolve("server_direct_http")
        assert adapter is not None
        scripted = cast(ScriptedAdapter, adapter)
        # reasoning controls tighten the merged requirement and gate the
        # matrix cell; the control value itself reaches the dispatch.
        self.assertEqual(scripted.dispatch_count, 1)
        self.assertEqual(scripted.dispatches[0].reasoning_effort, "high")

    def test_reasoning_controls_without_matrix_evidence_fail_closed(self) -> None:
        cells = build_cells(
            overrides={
                ("server_direct_http", "openai", "gpt-5.6-luna", "reasoning_controls"): "UNKNOWN",
                ("server_direct_http", "zai", "glm-5.3", "reasoning_controls"): "UNSUPPORTED",
            }
        )
        application = make_application(cells=cells)
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [_USER_ONLY],
                "reasoning_effort": "low",
            }
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(caught.exception.code, "no_eligible_target")
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_REJECTED)


class PinnedExecutionTests(unittest.TestCase):
    def _route_first(self, application: GatewayApplication) -> tuple[str, str]:
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "route me"}],
                "reasoning_effort": "high",
            }
        )
        _ = application.execute(client_id=CLIENT_ID, request=request)
        audit = audit_records(application)[-1]
        assert audit.selected_target is not None and audit.decision_id is not None
        target = audit.selected_target
        reference = (
            f"sr-pin:{target.resource_id}/{target.provider}"
            + f"/{target.model}/{target.variant}@{audit.decision_id}"
        )
        return reference, audit.decision_id

    def test_pinned_execution_dispatches_the_exact_target_without_reranking(self) -> None:
        application = make_application()
        reference, prior_decision_id = self._route_first(application)
        request = parse_chat_request({"model": reference, "messages": [_USER_ONLY]})
        outcome = application.execute(client_id=CLIENT_ID, request=request)
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_COMPLETED)
        # The audit decision id is the PRIOR recommendation's id (provenance).
        self.assertEqual(outcome.decision_id, prior_decision_id)
        self.assertEqual(audit.decision_id, prior_decision_id)
        # The executed target is the pinned one exactly.
        assert audit.selected_target is not None
        self.assertIn("openai-http", audit.selected_target.resource_id)

    def test_no_longer_bound_variant_is_rejected_not_substituted(self) -> None:
        application = make_application()
        # gpt-5.6-luna "max" is calibrated, but pin a variant the resource
        # does not bind: the explicit typed rejection, never a fallback to
        # another bound variant.
        request = parse_chat_request(
            {"model": "sr-pin:openai-http/openai/gpt-5.6-luna/low", "messages": [_USER_ONLY]}
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        error = caught.exception
        self.assertEqual(error.http_status, 400)
        self.assertEqual(error.code, "pinned_model_not_bound")
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_REJECTED)
        self.assertIsNone(audit.executed_target)
        adapter = application.adapters.resolve("server_direct_http")
        assert adapter is not None
        self.assertEqual(cast(ScriptedAdapter, adapter).dispatch_count, 0)

    def test_unknown_pin_target_is_not_found(self) -> None:
        application = make_application()
        request = parse_chat_request(
            {
                "model": "sr-pin:missing-resource/openai/gpt-5.6-luna/max",
                "messages": [_USER_ONLY],
            }
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.http_status, 404)
        self.assertEqual(caught.exception.code, "pin_target_not_found")

    def test_pin_of_a_stale_resource_fails_closed_at_admission(self) -> None:
        from datetime import timedelta

        stale_registry = build_registry()
        # Wind the evaluation clock past the freshness TTL.
        late = T_EVAL + timedelta(seconds=600)

        def late_clock():
            return late

        application = make_application(registry=stale_registry, clock=late_clock)
        request = parse_chat_request(
            {
                "model": "sr-pin:openai-http/openai/gpt-5.6-luna/max",
                "messages": [_USER_ONLY],
            }
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.http_status, 503)


class StreamingLifecycleTests(unittest.TestCase):
    def test_streaming_lifecycle_emits_normalized_chunks(self) -> None:
        application = make_application()
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [_USER_ONLY],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        )
        chunks: list[AdapterStreamChunk] = []

        def emit(chunk: AdapterStreamChunk) -> None:
            chunks.append(chunk)

        outcome = application.execute(
            client_id=CLIENT_ID, request=request, emit_chunk=emit
        )
        kinds = [chunk.kind for chunk in chunks]
        self.assertEqual(kinds, ["text_delta", "text_delta", "finish", "usage"])
        self.assertEqual(outcome.finish_reason, "stop")
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_COMPLETED)

    def test_client_disconnect_propagates_cancellation(self) -> None:
        application = make_application()
        request = parse_chat_request(
            {"model": "deep-coding", "messages": [_USER_ONLY], "stream": True}
        )

        def emit(chunk: AdapterStreamChunk) -> None:
            _ = chunk
            raise ClientDisconnectedError()

        with self.assertRaises(ClientDisconnectedError):
            _ = application.execute(
                client_id=CLIENT_ID, request=request, emit_chunk=emit
            )
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_CANCELLED)
        self.assertEqual(audit.reason_codes, ("client_disconnected",))
        # The backend was addressed exactly once; no retry happened.
        adapter = application.adapters.resolve("server_direct_http")
        assert adapter is not None
        self.assertEqual(cast(ScriptedAdapter, adapter).dispatch_count, 1)


class FailureSafetyTests(unittest.TestCase):
    def test_dispatch_failure_fails_closed_without_cross_target_reroute(self) -> None:
        failing = ScriptedAdapter(behavior=permanent_failure_behavior())
        worker = ScriptedAdapter(
            channel="worker_bridged",
            adapter_name="synthetic-worker",
            adapter_version="2.0.0",
        )
        application = make_application(
            registry=build_registry(with_worker=True),
            cells=build_cells(include_worker=True),
            adapters=[failing, worker],
        )
        request = parse_chat_request({"model": "deep-coding", "messages": [_USER_ONLY]})
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        error = caught.exception
        self.assertEqual(error.http_status, 502)
        self.assertEqual(error.code, "backend_failure")
        # Exactly one dispatch: the selected target failed and NOTHING was
        # retried on the qualified same-identity worker alternative.
        self.assertEqual(failing.dispatch_count, 1)
        self.assertEqual(worker.dispatch_count, 0)
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_FAILED)
        # The attempt went to the selected target only: selected and
        # executed targets agree, and the worker alternative was never
        # addressed.
        assert audit.selected_target is not None
        assert audit.executed_target is not None
        self.assertEqual(audit.executed_target.to_dict(), audit.selected_target.to_dict())
        self.assertEqual(audit.selected_target.resource_id, "openai-http")

    def test_ambiguous_execution_state_never_retries(self) -> None:
        adapter = ScriptedAdapter(behavior=ambiguous_failure_behavior())
        application = make_application(adapters=[adapter])
        request = parse_chat_request({"model": "deep-coding", "messages": [_USER_ONLY]})
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        error = caught.exception
        self.assertEqual(error.http_status, 500)
        self.assertEqual(error.code, "ambiguous_execution_state")
        self.assertEqual(adapter.dispatch_count, 1)
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_FAILED_AMBIGUOUS)

    def test_timeout_maps_to_the_execution_time_limit(self) -> None:
        adapter = ScriptedAdapter(behavior=timeout_behavior())
        application = make_application(adapters=[adapter])
        request = parse_chat_request({"model": "deep-coding", "messages": [_USER_ONLY]})
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        error = caught.exception
        self.assertEqual(error.http_status, 408)
        self.assertEqual(error.code, "execution_time_limit_exceeded")
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_TIMED_OUT)

    def test_missing_adapter_for_channel_is_an_explicit_503(self) -> None:
        application = make_application(adapters=[])
        request = parse_chat_request({"model": "deep-coding", "messages": [_USER_ONLY]})
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(caught.exception.code, "adapter_unavailable")


class AdmissionLimitTests(unittest.TestCase):
    def test_context_limit_rejects_before_dispatch(self) -> None:
        from scarcity_router.gateway_contracts import GatewayLimits

        application = make_application(limits=GatewayLimits(max_input_context_tokens=1))
        request = parse_chat_request(
            {"model": "deep-coding", "messages": [_USER_ONLY]}
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.code, "context_length_exceeded")
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_REJECTED)
        # Rejected at admission: registry state was never consulted.
        self.assertIsNone(audit.registry_revision)

    def test_output_limit_rejects_before_dispatch(self) -> None:
        from scarcity_router.gateway_contracts import GatewayLimits

        application = make_application(limits=GatewayLimits(max_output_tokens=8))
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [_USER_ONLY],
                "max_completion_tokens": 16,
            }
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.code, "output_limit_exceeded")

    def test_global_concurrency_exhaustion_is_an_immediate_429(self) -> None:
        from scarcity_router.gateway_contracts import GatewayLimits

        adapter = BlockingAdapter()
        application = make_application(
            adapters=[adapter],
            limits=GatewayLimits(max_concurrent_executions=1, max_concurrent_executions_per_client=1),
        )
        first = parse_chat_request(
            {"model": "deep-coding", "messages": [_USER_ONLY]}
        )
        second = parse_chat_request(
            {"model": "deep-coding", "messages": [_USER_ONLY]}
        )
        results: dict[str, object] = {}

        def run_first() -> None:
            results["first"] = application.execute(
                client_id=CLIENT_ID, request=first
            )

        thread = threading.Thread(target=run_first)
        thread.start()
        # Wait until the first execution is actually inside its dispatch so
        # the reservation is held before the second request arrives.
        self.assertTrue(adapter.started_event.wait(timeout=10))
        try:
            with self.assertRaises(GatewayError) as caught:
                _ = application.execute(client_id=CLIENT_ID, request=second)
            self.assertEqual(caught.exception.http_status, 429)
            self.assertEqual(caught.exception.code, "concurrency_limit_reached")
        finally:
            adapter.release_event.set()
            thread.join(timeout=10)
        self.assertIn("first", results)

    def test_reservation_is_released_after_completion(self) -> None:
        from scarcity_router.gateway_contracts import GatewayLimits

        application = make_application(
            limits=GatewayLimits(max_concurrent_executions=1, max_concurrent_executions_per_client=1)
        )
        for _ in range(3):
            request = parse_chat_request(
                {"model": "deep-coding", "messages": [_USER_ONLY]}
            )
            _ = application.execute(client_id=CLIENT_ID, request=request)
        # Three sequential executions under a limit of one: the reservation
        # must have been released after each completion, or these would 429.


class CompatibilityGateTests(unittest.TestCase):
    def test_unknown_capability_fails_closed_before_inference(self) -> None:
        cells = build_cells(
            overrides={
                ("server_direct_http", "openai", "gpt-5.6-luna", "tool_calls"): "UNKNOWN",
                ("server_direct_http", "zai", "glm-5.3", "tool_calls"): "UNKNOWN",
            }
        )
        application = make_application(cells=cells)
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [_USER_ONLY],
                "tools": [
                    {"type": "function", "function": {"name": "f", "parameters": {}}}
                ],
            }
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.code, "no_eligible_target")
        audit = audit_records(application)[-1]
        self.assertIsNone(audit.executed_target)

    def test_routed_request_lands_only_on_a_compatible_backend(self) -> None:
        cells = build_cells(
            overrides={
                ("server_direct_http", "openai", "gpt-5.6-luna", "tool_calls"): "UNSUPPORTED",
            }
        )
        application = make_application(cells=cells)
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [_USER_ONLY],
                "tools": [
                    {"type": "function", "function": {"name": "f", "parameters": {}}}
                ],
            }
        )
        _ = application.execute(client_id=CLIENT_ID, request=request)
        audit = audit_records(application)[-1]
        assert audit.selected_target is not None
        self.assertEqual(audit.selected_target.provider, "zai")

    def test_pinned_incompatible_target_rejects_before_inference(self) -> None:
        cells = build_cells(
            overrides={
                ("server_direct_http", "zai", "glm-5.3", "tool_calls"): "UNSUPPORTED",
            }
        )
        application = make_application(cells=cells)
        request = parse_chat_request(
            {
                "model": "sr-pin:zai-http/zai/glm-5.3/high",
                "messages": [_USER_ONLY],
                "tools": [
                    {"type": "function", "function": {"name": "f", "parameters": {}}}
                ],
            }
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(caught.exception.code, "compatibility_unsupported")

    def test_supplemental_roles_history_gate_fails_closed(self) -> None:
        # The routing core does not gate roles_history; the coordinator's
        # admission gate must: openai's cell is UNKNOWN, so a multi-message
        # conversation is rejected before inference even though openai
        # otherwise qualifies and ranks first.
        cells = build_cells(
            overrides={
                ("server_direct_http", "openai", "gpt-5.6-luna", "roles_history"): "UNKNOWN",
            }
        )
        application = make_application(cells=cells)
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "user"},
                ],
            }
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(caught.exception.code, "compatibility_unknown")
        self.assertEqual(caught.exception.param, "roles_history")

    def test_supplemental_tool_results_gate_fails_closed(self) -> None:
        cells = build_cells(
            overrides={
                ("server_direct_http", "zai", "glm-5.3", "tool_results"): "UNSUPPORTED",
            }
        )
        application = make_application(cells=cells)
        request = parse_chat_request(
            {
                "model": "zai-only",
                "messages": [
                    {"role": "user", "content": "user"},
                    {"role": "tool", "content": "result", "tool_call_id": "call-1"},
                ],
            }
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.code, "compatibility_unsupported")
        self.assertEqual(caught.exception.param, "tool_results")

    def test_single_message_does_not_require_roles_history(self) -> None:
        cells = build_cells(
            overrides={
                ("server_direct_http", "openai", "gpt-5.6-luna", "roles_history"): "MISS",
                ("server_direct_http", "zai", "glm-5.3", "roles_history"): "MISS",
            }
        )
        application = make_application(cells=cells)
        request = parse_chat_request(
            {"model": "deep-coding", "messages": [_USER_ONLY]}
        )
        _ = application.execute(client_id=CLIENT_ID, request=request)
        audit = audit_records(application)[-1]
        self.assertEqual(audit.result_status, RESULT_COMPLETED)


class ToolRoundTripTests(unittest.TestCase):
    """The representative client flow: a multi-turn tool round-trip."""

    def test_multi_turn_tool_round_trip(self) -> None:
        adapter = ScriptedAdapter(behavior=tool_call_behavior())
        application = make_application(adapters=[adapter])
        tools: list[dict[str, object]] = [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "list files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        # Turn 1: the model asks the CLIENT to run the tool.
        first = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "list the files"}],
                "tools": tools,
            }
        )
        outcome = application.execute(client_id=CLIENT_ID, request=first)
        self.assertEqual(outcome.finish_reason, "tool_calls")
        assert outcome.message.tool_calls
        self.assertEqual(outcome.message.tool_calls[0].name, "list_files")
        self.assertEqual(outcome.message.tool_calls[0].arguments, '{"path": "."}')
        # The router never executes client tools: they went back to the
        # client, and the backend received them only as conversation state.
        # Turn 2: the client posts the tool RESULT back; the model answers.
        history = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [
                    {"role": "user", "content": "list the files"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-synthetic-1",
                                "type": "function",
                                "function": {
                                    "name": "list_files",
                                    "arguments": '{"path": "."}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-synthetic-1",
                        "content": "a.txt\nb.txt",
                    },
                ],
                "tools": tools,
            }
        )
        final = application.execute(client_id=CLIENT_ID, request=history)
        self.assertEqual(final.finish_reason, "tool_calls")


class UsageAndAuditTests(unittest.TestCase):
    def test_multi_call_fan_out_is_represented_honestly(self) -> None:
        adapter = ScriptedAdapter(behavior=multi_call_behavior())
        application = make_application(adapters=[adapter])
        request = parse_chat_request({"model": "deep-coding", "messages": [_USER_ONLY]})
        outcome = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(outcome.usage.usage_source, "mixed")
        assert outcome.usage.provider_reported_usage is not None
        assert outcome.usage.estimated_usage is not None
        self.assertEqual(
            outcome.usage.provider_reported_usage.to_dict(),
            {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        )
        self.assertEqual(
            outcome.usage.estimated_usage.to_dict(),
            {"prompt_tokens": 10, "completion_tokens": 500, "total_tokens": 510},
        )
        audit = audit_records(application)[-1]
        self.assertEqual(audit.call_count, 2)
        self.assertIsNotNone(audit.provider_reported_usage)
        self.assertIsNotNone(audit.estimated_usage)

    def test_usage_without_any_report_stays_unavailable_not_zero_reported(self) -> None:
        adapter = ScriptedAdapter(
            behavior=echo_behavior(reported_usage=None)
        )
        application = make_application(adapters=[adapter])
        request = parse_chat_request({"model": "deep-coding", "messages": [_USER_ONLY]})
        outcome = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(outcome.usage.usage_source, "unavailable")

    def test_audit_record_is_complete_and_content_free(self) -> None:
        application = make_application()
        secret = "PROMPT-CONTENT-MUST-NOT-APPEAR-7f3a"
        request = parse_chat_request(
            {"model": "deep-coding", "messages": [{"role": "user", "content": secret}]}
        )
        _ = application.execute(client_id=CLIENT_ID, request=request)
        records = audit_records(application)
        self.assertEqual(len(records), 1)
        rendered = json.dumps(records[0].to_dict())
        self.assertNotIn(secret, rendered)
        record = records[0]
        self.assertIsNotNone(record.decision_id)
        self.assertIsNotNone(record.routing_profile)
        self.assertIsNotNone(record.registry_generated_at)
        self.assertIsNotNone(record.selected_target)
        self.assertIsNotNone(record.executed_target)
        self.assertEqual(record.result_status, RESULT_COMPLETED)
        self.assertEqual(record.adapter_name, "synthetic-http")
        self.assertEqual(record.started_at, canonical(T_EVAL))

    def test_rejected_request_audits_without_an_executed_target(self) -> None:
        application = make_application()
        request = parse_chat_request({"model": "does-not-exist", "messages": [_USER_ONLY]})
        with self.assertRaises(GatewayError):
            _ = application.execute(client_id=CLIENT_ID, request=request)
        audit = audit_records(application)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[-1].result_status, RESULT_REJECTED)
        self.assertEqual(audit[-1].reason_codes, ("model_not_found",))
        self.assertIsNone(audit[-1].executed_target)


class ModelResolutionTests(unittest.TestCase):
    def test_resolve_model_string_kinds(self) -> None:
        from tests.gateway_fixtures import build_aliases

        aliases = build_aliases()
        alias = resolve_model_string("zai-only", aliases)
        self.assertEqual(alias.kind, "alias")
        assert alias.profile is not None
        self.assertEqual(alias.profile.profile_id, "gateway-core")
        pin = resolve_model_string(
            "sr-pin:openai-http/openai/gpt-5.6-luna/max", aliases
        )
        self.assertEqual(pin.kind, "pin")
        assert pin.pinned_target is not None
        self.assertEqual(pin.pinned_target.resource_id, "openai-http")

    def test_empty_registry_yields_explicit_no_target(self) -> None:
        application = make_application(registry=ResourceRegistry())
        request = parse_chat_request({"model": "deep-coding", "messages": [_USER_ONLY]})
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.code, "no_eligible_target")


if __name__ == "__main__":
    _ = unittest.main()
