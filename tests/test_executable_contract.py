"""Owned offline producer/client/gateway conformance, not installed harness proof.

Kernel Declaration v1 source shape: public Kernel73e3af45c911aa9ae8c26b97bf0ec9f452063f10,
core/intake.py and adapters/scarcity_router.py. No foreign implementation is copied.
"""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from datetime import timedelta
from typing import cast

from scarcity_router.executable_client import prepare_executable_completion
from scarcity_router.executable_contract import parse_executable_request
from scarcity_router.gateway_contracts import GatewayError
from scarcity_router.gateway_continuation import (
    ContinuationRegistry, PendingContinuation, canonical_json_text, message_fingerprint, tool_calls_fingerprint,
)
from scarcity_router.gateway_coordinator import parse_pinned_reference
from scarcity_router.kernel_requirements import QUALITY_PREFIX, interpret_kernel_declaration
from scarcity_router.resource_state import ResourceRegistry, ResourceRegistration, PromotionObservation
from scarcity_router.routing_core import ClientAuthorization
from scarcity_router.selection_types import CapabilityMinima, TaskProfileCatalog
from tests.gateway_fixtures import CLIENT_ID, CLIENT_KEY, ScriptedAdapter, T_EVAL, TTL, canonical, make_application, parse_chat_request
from tests.test_gateway_server import AUTH_HEADERS, ServerHarness, as_dict
from tests.test_gateway_continuation_coordinator import FakeContinuationAdapter
from scarcity_router.gateway_openai import chat_completion_payload


def route_document() -> dict[str, object]:
    application = make_application()
    requirement = application.profiles.resolve("gateway-core").to_dict()
    declaration = {
        "version": 1, "outcome": "owned synthetic result", "criteria": ["exact retained result"],
        "quality": [QUALITY_PREFIX + json.dumps({"requirement": requirement})],
        "interface": [], "context": [], "unknowns": [], "paths": [], "network": [],
        "effects": [], "recipes": ["offline conformance"], "provenance": ["owned synthetic fixture"],
    }
    interpreted = interpret_kernel_declaration(json.dumps(declaration).encode(), profiles=application.profiles)
    assert interpreted.requirement is not None and interpreted.request is not None and not interpreted.problems
    return {"schema_version": 1, "model": "deep-coding", "requirement": interpreted.requirement.to_dict(),
            "binding": interpreted.request.to_dict()}


COMPLETION: dict[str, object] = {"messages": [{"role": "user", "content": "owned synthetic request"}]}


class ExecutableContractTests(unittest.TestCase):
    def output_application(self, adapter: ScriptedAdapter):
        application = make_application(adapters=[adapter])
        entries = application.registry.registry_snapshot().entries
        application.registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
        for entry in entries:
            application.registry.register(ResourceRegistration(
                identity=entry.identity, freshness_ttl_seconds=TTL,
                capabilities=replace(entry.capabilities, output_limit_control=True, output_limit_tokens=1000),
            ))
            assert entry.observation is not None
            application.registry.apply_snapshot(entry.observation)
        return application

    def test_real_serialization_helper_admission_and_exact_dispatch(self) -> None:
        adapter = ScriptedAdapter()
        application = make_application(adapters=[adapter])
        document = route_document()
        response = cast(object, json.loads(json.dumps(application.select_executable(client_id=CLIENT_ID, document=document))))
        self.assertEqual(adapter.dispatch_count, 0)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        pin = parse_pinned_reference(cast(str, prepared["model"]))
        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(adapter.dispatch_count, 1)
        call = adapter.dispatches[0]
        self.assertEqual(call.resource.resource_id, pin.resource_id)
        self.assertEqual((call.model.provider, call.model.model, call.model.variant),
                         (pin.model.provider, pin.model.model, pin.model.variant))
        self.assertEqual(call.reasoning_effort, prepared["reasoning_effort"])
        self.assertNotIn("execution_requirements", dict(call.generation_params or {}))

    def test_exact_execution_does_not_call_competitive_ranking(self) -> None:
        from unittest.mock import patch
        application = make_application()
        document = route_document()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        with patch("scarcity_router.gateway_coordinator.route_request", side_effect=AssertionError("must not rerank")):
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))

    def test_logical_request_selects_and_serializes_configured_effort_not_variant_text(self) -> None:
        application = make_application()
        document = route_document()
        document["model"] = "gpt-5.6-luna"
        as_dict(document["binding"])["explicit_variant"] = "max"
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        self.assertEqual(parse_pinned_reference(cast(str, prepared["model"])).model.variant, "max")
        self.assertEqual(prepared["reasoning_effort"], "max")
        del as_dict(document["binding"])["explicit_variant"]
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        self.assertEqual(parse_pinned_reference(cast(str, prepared["model"])).model.model, "gpt-5.6-luna")

    def test_pin_grammar_preserves_opaque_variant_and_optional_decision(self) -> None:
        for reference in ("sr-pin:r/openai/gpt-5.6-luna/max", "sr-pin:r/openai/gpt-5.6-luna/max@rd-fixture"):
            pin = parse_pinned_reference(reference)
            self.assertEqual(pin.model.variant, "max")
            self.assertEqual(pin.decision_id, "rd-fixture" if "@" in reference else None)

    def test_opaque_variant_does_not_infer_effort_and_null_differs_from_none(self) -> None:
        for effort in (None, "none", "max"):
            adapter = ScriptedAdapter()
            application = make_application(adapters=[adapter])
            application.catalog = replace(application.catalog, entries=tuple(
                replace(entry, identity=replace(entry.identity, variant="opaque-config"), reasoning_effort=effort,
                        hard_properties=replace(entry.hard_properties, supports_reasoning_mode=effort is not None))
                if entry.identity.variant == "max" else entry for entry in application.catalog.entries))
            document = route_document()
            document["model"] = "gpt-5.6-luna"
            as_dict(document["binding"])["explicit_variant"] = "opaque-config"
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            self.assertEqual(parse_pinned_reference(cast(str, prepared["model"])).model.variant, "opaque-config")
            if effort is None:
                self.assertNotIn("reasoning_effort", prepared)
            else:
                self.assertEqual(prepared["reasoning_effort"], effort)
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(adapter.dispatches[0].reasoning_effort, effort)

    def test_versions_unknown_fields_and_missing_quality_refuse(self) -> None:
        for version in (True, 1.0, None, 2):
            document = route_document()
            document["schema_version"] = version
            with self.assertRaises(GatewayError):
                _ = parse_executable_request(document)
        for field in ("requirement", "binding", "model", "schema_version"):
            document = route_document()
            del document[field]
            with self.assertRaises(GatewayError):
                _ = parse_executable_request(document)
        document = route_document()
        document["client_authorization"] = {"allowed_providers": ["openai"]}
        with self.assertRaises(GatewayError):
            _ = parse_executable_request(document)

    def test_no_candidate_has_no_executable_binding(self) -> None:
        document = route_document()
        requirement = as_dict(document["requirement"])
        requirement["capability_minima"] = {"reasoning": 5, "coding": 5}
        application = make_application()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        self.assertIsNone(response["execution"])
        self.assertEqual(as_dict(response["route"])["status"], "no_solution")
        with self.assertRaises(GatewayError):
            _ = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)

    def test_retained_structural_controls_cannot_be_lost(self) -> None:
        application = make_application()
        for field in ("requires_streaming", "requires_tool_calls", "requires_structured_output"):
            document = route_document()
            as_dict(document["binding"])[field] = True
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            with self.subTest(field=field), self.assertRaises(GatewayError):
                _ = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)

    def test_unsupported_structural_feature_has_no_candidate(self) -> None:
        from tests.gateway_fixtures import build_cells
        cells = build_cells(overrides={
            ("server_direct_http", "openai", "gpt-5.6-luna", "structured_output"): "UNSUPPORTED",
            ("server_direct_http", "zai", "glm-5.3", "structured_output"): "UNKNOWN",
        })
        application = make_application(cells=cells)
        document = route_document()
        as_dict(document["binding"])["requires_structured_output"] = True
        self.assertIsNone(application.select_executable(client_id=CLIENT_ID, document=document)["execution"])

    def test_output_ceiling_is_preserved_and_not_a_quality_floor(self) -> None:
        adapter = ScriptedAdapter()
        application = self.output_application(adapter)
        document = route_document()
        as_dict(document["binding"])["maximum_output_tokens"] = 200
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        self.assertEqual(prepared["max_completion_tokens"], 200)
        context = as_dict(prepared["execution_requirements"])
        self.assertNotIn("minimum_output_tokens", as_dict(as_dict(context["requirement"])["hard_constraints"]))
        prepared["max_completion_tokens"] = 100
        prepared = prepare_executable_completion(response, expected_request=document, completion=prepared, require_bound=True)
        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(adapter.dispatches[0].max_output_tokens, 100)
        prepared["max_completion_tokens"] = 201
        with self.assertRaises(GatewayError):
            _ = parse_chat_request(prepared)

    def test_genuine_output_floor_still_refuses_contradictory_narrowing(self) -> None:
        adapter = ScriptedAdapter()
        application = self.output_application(adapter)
        document = route_document()
        as_dict(as_dict(document["requirement"])["hard_constraints"])["minimum_output_tokens"] = 150
        as_dict(document["binding"])["maximum_output_tokens"] = 200
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        prepared["max_completion_tokens"] = 100
        with self.assertRaises(GatewayError):
            _ = prepare_executable_completion(response, expected_request=document, completion=prepared, require_bound=True)
        self.assertEqual(adapter.dispatch_count, 0)

    def test_effort_control_unknown_missing_or_unsupported_selects_capable_alternative(self) -> None:
        from tests.gateway_fixtures import build_cells
        for value in ("UNKNOWN", "UNSUPPORTED", "missing"):
            adapter = ScriptedAdapter()
            cells = build_cells(overrides={
                ("server_direct_http", "openai", "gpt-5.6-luna", "reasoning_controls"): "UNKNOWN" if value == "missing" else value,
            })
            if value == "missing":
                cells = tuple(cell for cell in cells if not (cell.provider == "openai" and cell.feature == "reasoning_controls"))
            application = make_application(cells=cells, adapters=[adapter])
            document = route_document()
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            self.assertEqual(parse_pinned_reference(cast(str, prepared["model"])).model.provider, "zai")
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(adapter.dispatch_count, 1)

    def test_null_effort_lane_survives_unknown_control_mapping(self) -> None:
        from tests.gateway_fixtures import build_cells
        cells = tuple(replace(cell, value="UNKNOWN") if cell.feature == "reasoning_controls" else cell for cell in build_cells())
        application = make_application(cells=cells)
        application.catalog = replace(application.catalog, entries=tuple(
            replace(entry, reasoning_effort=None, hard_properties=replace(entry.hard_properties, supports_reasoning_mode=False))
            if entry.identity.variant == "max" else entry for entry in application.catalog.entries))
        document = route_document()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        self.assertNotIn("reasoning_effort", prepared)
        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))

    def test_explicit_pin_binding_accepts_new_audit_reference_without_substitution(self) -> None:
        for suffix in ("", "@rd-prior-audit"):
            adapter = ScriptedAdapter()
            application = make_application(adapters=[adapter])
            document = route_document()
            initial = application.select_executable(client_id=CLIENT_ID, document=document)
            model = cast(str, as_dict(initial["execution"])["model"]).split("@", 1)[0] + suffix
            expected_pin = parse_pinned_reference(model)
            document["model"] = model
            as_dict(document["binding"])["pinned_target"] = expected_pin.to_dict()
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            actual_pin = parse_pinned_reference(cast(str, prepared["model"]))
            self.assertEqual((actual_pin.resource_id, actual_pin.model), (expected_pin.resource_id, expected_pin.model))
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(adapter.dispatch_count, 1)

    def test_changed_pin_or_effort_refuses_before_adapter_dispatch(self) -> None:
        for field, value in (("model", "sr-pin:absent/openai/gpt-5.6-luna/max"), ("reasoning_effort", "low")):
            adapter = ScriptedAdapter()
            application = make_application(adapters=[adapter])
            document = route_document()
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            prepared[field] = value
            with self.subTest(field=field), self.assertRaises(GatewayError):
                _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(adapter.dispatch_count, 0)

    def test_helper_refuses_missing_weakened_or_replaced_artifacts(self) -> None:
        application = make_application()
        document = route_document()
        original = application.select_executable(client_id=CLIENT_ID, document=document)
        for mutation in ("pin", "context", "quality", "effort"):
            response = copy.deepcopy(original)
            execution = as_dict(response["execution"])
            context = as_dict(execution["execution_requirements"])
            if mutation == "pin":
                execution["model"] = "deep-coding"
            elif mutation == "context":
                del execution["execution_requirements"]
            elif mutation == "quality":
                as_dict(context["requirement"])["capability_minima"] = {}
            else:
                execution["reasoning_effort"] = "low"
            with self.subTest(mutation=mutation), self.assertRaises(GatewayError):
                _ = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)

    def test_final_consumer_boundary_detects_loss_without_repair_or_relabel(self) -> None:
        application = make_application()
        document = route_document()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        self.assertEqual(prepared, prepare_executable_completion(response, expected_request=document,
                         completion=prepared, require_bound=True))
        for lost in (("model",), ("execution_requirements",), ("reasoning_effort",),
                     ("model", "execution_requirements", "reasoning_effort")):
            outgoing = copy.deepcopy(prepared)
            for key in lost:
                del outgoing[key]
            with self.subTest(lost=lost), self.assertRaises(GatewayError):
                _ = prepare_executable_completion(response, expected_request=document,
                                                  completion=outgoing, require_bound=True)
            for key in lost:
                self.assertNotIn(key, outgoing)

    def test_retained_context_requires_exact_pin_not_logical_fallback(self) -> None:
        application = make_application()
        document = route_document()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        prepared["model"] = "deep-coding"
        with self.assertRaises(GatewayError) as caught:
            _ = parse_chat_request(prepared)
        self.assertEqual(caught.exception.code, "execution_pin_required")

    def test_calibration_change_cannot_weaken_cached_profile(self) -> None:
        adapter = ScriptedAdapter()
        application = make_application(adapters=[adapter])
        document = route_document()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        definition = application.profiles.definitions[0]
        application.profiles = TaskProfileCatalog(definitions=(replace(definition,
            requirement=replace(definition.requirement, capability_minima=CapabilityMinima(reasoning=4, coding=4))),))
        with self.assertRaises(GatewayError):
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(adapter.dispatch_count, 0)

    def test_current_restricted_client_cannot_execute_copied_pin(self) -> None:
        adapter = ScriptedAdapter()
        application = make_application(adapters=[adapter])
        document = route_document()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        application.client_authorizations = {"restricted-client": ClientAuthorization(allowed_providers=())}
        with self.assertRaises(GatewayError):
            _ = application.execute(client_id="restricted-client", request=parse_chat_request(prepared))
        self.assertEqual(adapter.dispatch_count, 0)

    def test_current_stale_or_missing_resource_refuses_without_substitution(self) -> None:
        for change in ("stale", "missing"):
            adapter = ScriptedAdapter()
            application = make_application(adapters=[adapter])
            document = route_document()
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            if change == "stale":
                application.clock = lambda: T_EVAL + timedelta(seconds=TTL + 1)
            else:
                application.registry = ResourceRegistry()
            with self.subTest(change=change), self.assertRaises(GatewayError):
                _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(adapter.dispatch_count, 0)

    def test_current_model_quality_is_rechecked_before_dispatch(self) -> None:
        adapter = ScriptedAdapter()
        application = make_application(adapters=[adapter])
        document = route_document()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        pin = parse_pinned_reference(cast(str, prepared["model"]))
        entries = tuple(replace(entry, capabilities=replace(entry.capabilities,
            reasoning=replace(entry.capabilities.reasoning, rating=1))) if entry.identity == pin.model else entry
            for entry in application.catalog.entries)
        application.catalog = replace(application.catalog, entries=entries)
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(caught.exception.code, "capability_failed")
        self.assertEqual(adapter.dispatch_count, 0)

    def test_refused_private_resource_names_are_not_inventory_output(self) -> None:
        application = make_application(client_authorizations={CLIENT_ID: ClientAuthorization(allowed_providers=())})
        response = application.select_executable(client_id=CLIENT_ID, document=route_document())
        self.assertIsNone(response["execution"])
        exclusions = cast(list[object], as_dict(response["route"])["target_exclusions"])
        self.assertTrue(exclusions)
        for exclusion in exclusions:
            self.assertEqual(as_dict(exclusion)["resource_id"], "restricted")

    def test_binding_failure_and_expired_private_promotion_cannot_bypass_projection(self) -> None:
        application = make_application(client_authorizations={CLIENT_ID: ClientAuthorization(allowed_providers=())})
        entry = application.registry.registry_snapshot().entries[0]
        private = replace(entry.identity, resource_id="private-uncalibrated", model="private-unassessed-model")
        application.registry.register(ResourceRegistration(identity=private, freshness_ttl_seconds=TTL))
        assert entry.observation is not None
        promotion = PromotionObservation(source="private-expired-promotion", observed_at=entry.observation.observed_at,
            channel=entry.identity.channel, provider=entry.identity.provider, model=entry.identity.model,
            valid_until=canonical(T_EVAL - timedelta(seconds=1)))
        application.registry.apply_snapshot(replace(entry.observation, promotions=(promotion,)))
        response = application.select_executable(client_id=CLIENT_ID, document=route_document())
        text = json.dumps(response)
        for sensitive in (private.resource_id, private.model, promotion.source):
            self.assertNotIn(sensitive, text)
        self.assertEqual(as_dict(response["route"])["expired_promotions"], [])

    def test_known_client_credential_cannot_be_reflected_as_a_requirement_tag(self) -> None:
        application = make_application()
        document = route_document()
        as_dict(as_dict(document["requirement"])["hard_constraints"])["privacy_constraint"] = CLIENT_KEY
        with self.assertRaises(GatewayError) as caught:
            _ = application.select_executable(client_id=CLIENT_ID, document=document)
        self.assertNotIn(CLIENT_KEY, caught.exception.message)
        self.assertEqual(caught.exception.code, "execution_requirements_invalid")

    def test_continuation_cannot_drop_or_replace_original_requirements(self) -> None:
        for mutation in ("missing", "changed"):
            adapter = ScriptedAdapter()
            continuations = ContinuationRegistry()
            application = make_application(adapters=[adapter], continuations=continuations)
            document = route_document()
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            token = "srct-" + "a" * 32
            prepared["messages"] = [*cast(list[object], prepared["messages"]),
                {"role": "assistant", "tool_calls": [{"id": token, "type": "function",
                  "function": {"name": "synthetic_lookup", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": token, "content": "owned result"}]
            parsed = parse_chat_request(prepared)
            assert parsed.execution_requirements is not None
            pin = parse_pinned_reference(parsed.model)
            record = PendingContinuation(
                continuation_token=token, attempt_id="owned-attempt", resource_id=pin.resource_id,
                channel="server_direct_http", call_id="owned-call", deadline=T_EVAL + timedelta(seconds=60),
                created_at=T_EVAL.isoformat(), client_id=CLIENT_ID, model_echo=parsed.model,
                reasoning_effort=parsed.reasoning_effort, tools_fingerprint=None,
                tool_choice_json=canonical_json_text(parsed.tool_choice),
                prefix_fingerprint=message_fingerprint(parsed.messages[:-2]),
                assistant_tool_calls_digest=tool_calls_fingerprint(parsed.messages[-2].tool_calls),
                execution_requirements_digest=parsed.execution_requirements.fingerprint(),
            )
            self.assertTrue(continuations.register(record))
            if mutation == "missing":
                del prepared["execution_requirements"]
            else:
                as_dict(as_dict(prepared["execution_requirements"])["requirement"])["task_level"] = "L3"
            with self.subTest(mutation=mutation), self.assertRaises(GatewayError) as caught:
                _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(caught.exception.code, "continuation_mismatch")
            self.assertEqual(adapter.dispatch_count, 0)

    def test_real_suspension_refuses_new_binding_output_ceiling_before_delivery(self) -> None:
        from tests.gateway_fixtures import build_registry, build_cells
        adapter = FakeContinuationAdapter()
        continuations = ContinuationRegistry()
        registry = build_registry(with_worker=True)
        original = registry.registry_snapshot().entries
        registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
        for entry in original:
            registry.register(ResourceRegistration(identity=entry.identity, freshness_ttl_seconds=TTL,
                capabilities=replace(entry.capabilities, reasoning_controls=True,
                                     output_limit_control=False, output_limit_tokens=128000)))
            assert entry.observation is not None
            registry.apply_snapshot(entry.observation)
        application = make_application(adapters=[adapter], registry=registry, continuations=continuations, cells=build_cells(include_worker=True),
            continuation_capability_source=lambda: frozenset({"openai-worker"}))
        document = route_document()
        document["model"] = "sr-pin:openai-worker/openai/gpt-5.6-luna/max"
        as_dict(document["binding"])["requires_tool_calls"] = True
        as_dict(document["binding"])["maximum_output_tokens"] = 128000
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        body: dict[str, object] = {"messages": [{"role": "user", "content": "owned request"}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]}
        prepared = prepare_executable_completion(response, expected_request=document, completion=body)
        first = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        choices = cast(list[object], chat_completion_payload(first)["choices"])
        message = as_dict(as_dict(choices[0])["message"])
        calls = cast(list[object], message["tool_calls"])
        token = as_dict(calls[0])["id"]
        prepared["messages"] = [*cast(list[object], prepared["messages"]), message,
                                {"role": "tool", "tool_call_id": token, "content": "owned tool result"}]
        prepared["max_completion_tokens"] = 100
        prepared = prepare_executable_completion(response, expected_request=document, completion=prepared, require_bound=True)
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(caught.exception.code, "continuation_mismatch")
        self.assertEqual(adapter.delivered, [])


class ExecutableHTTPTests(ServerHarness):
    def test_authenticated_route_to_completion_uses_same_listener(self) -> None:
        adapter = ScriptedAdapter()
        port = self.make_server(adapters=[adapter])
        document = route_document()
        connection = self.client(port)
        connection.request("POST", "/v1/route", body=json.dumps(document), headers=AUTH_HEADERS)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload: object = cast(object, json.loads(response.read()))
        self.assertEqual(adapter.dispatch_count, 0)
        prepared = prepare_executable_completion(payload, expected_request=document, completion=COMPLETION)
        completed = self.post_chat(port, prepared)
        self.assertEqual(completed.status, 200, completed.read())
        self.assertEqual(adapter.dispatch_count, 1)

    def test_route_authentication_and_closed_shape_refuse(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request("POST", "/v1/route", body="{}", headers={"Content-Type": "application/json"})
        self.assertEqual(connection.getresponse().status, 401)
        connection = self.client(port)
        connection.request("POST", "/v1/route", body="{}", headers=AUTH_HEADERS)
        rejected = connection.getresponse()
        self.assertEqual(rejected.status, 400)
        self.assertEqual(as_dict(as_dict(cast(object, json.loads(rejected.read())))["error"])["code"], "execution_requirements_invalid")
