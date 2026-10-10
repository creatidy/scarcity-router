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
from scarcity_router.routing_core import (
    ClientAuthorization, AdministratorConstraints, RouteDecision, RouteRequest,
    public_route_document, route_request,
)
from scarcity_router.gateway_adapters import AdapterRegistry
from scarcity_router.selection_types import CapabilityMinima, ModelIdentity, TaskProfileCatalog
from tests.gateway_fixtures import CLIENT_ID, CLIENT_KEY, ScriptedAdapter, T_EVAL, TTL, canonical, make_application, parse_chat_request
from tests.test_gateway_server import AUTH_HEADERS, ServerHarness, as_dict
from tests.test_gateway_continuation_coordinator import FakeContinuationAdapter
from scarcity_router.gateway_openai import chat_completion_payload, CHAT_COMPLETION_ALLOWED_KEYS
from pathlib import Path


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


def serialized_identities(value: object) -> set[tuple[str, str, str]]:
    """Inspect every nested public field, including policy/explanation provenance."""
    found: set[tuple[str, str, str]] = set()
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        provider, model, variant = fields.get("provider"), fields.get("model"), fields.get("variant")
        if isinstance(provider, str) and isinstance(model, str) and isinstance(variant, str):
            found.add((provider, model, variant))
        for child in fields.values():
            found.update(serialized_identities(child))
    elif isinstance(value, list):
        for child in cast(list[object], value):
            found.update(serialized_identities(child))
    return found


class ExecutableContractTests(unittest.TestCase):
    def test_denied_provider_or_all_resources_have_no_public_identities(self) -> None:
        for grant in (ClientAuthorization(allowed_providers=()),
                      ClientAuthorization(blocked_resource_ids=("openai-http", "zai-http"))):
            application = make_application(client_authorizations={CLIENT_ID: grant})
            application.policy = replace(application.policy, preference_order=tuple(entry.identity for entry in application.catalog.entries))
            response = application.select_executable(client_id=CLIENT_ID, document=route_document())
            self.assertIsNone(response["execution"])
            self.assertEqual(serialized_identities(response), set())
            self.assertEqual(as_dict(response["route"])["unroutable_identities"], [])
            text = json.dumps(response)
            for entry in application.catalog.entries:
                self.assertNotIn(entry.identity.model, text)

    def test_mixed_resources_filter_identity_and_preference_explanations_not_selection(self) -> None:
        from unittest.mock import patch
        for grant in (ClientAuthorization(allowed_providers=("zai",)),
                      ClientAuthorization(blocked_resource_ids=("openai-http",))):
            application = make_application(client_authorizations={CLIENT_ID: grant})
            application.policy = replace(application.policy, preference_order=tuple(entry.identity for entry in application.catalog.entries))
            observed: list[tuple[RouteRequest, RouteDecision, dict[str, object]]] = []
            def evaluate(request: RouteRequest) -> RouteDecision:
                decision = route_request(request)
                observed.append((request, decision, decision.to_dict()))
                return decision
            with patch("scarcity_router.gateway_coordinator.route_request", side_effect=evaluate):
                response = application.select_executable(client_id=CLIENT_ID, document=route_document())
            allowed = {("zai", "glm-5.3", "high")}
            self.assertEqual(serialized_identities(response), allowed)
            self.assertNotIn("gpt-5.6-luna", json.dumps(response))
            request, decision, original = observed[0]
            self.assertEqual(decision.to_dict(), original)
            self.assertTrue(decision.unroutable_identities)
            self.assertEqual(decision.selection.preference_order, application.policy.preference_order)
            public = as_dict(response["route"])
            self.assertEqual(public["decision_id"], decision.decision_id)
            self.assertEqual(public["target"], original["target"])
            self.assertEqual(as_dict(public["selection"])["selected"], as_dict(original["selection"])["selected"])
            self.assertEqual(public_route_document(decision, request), public)

    def test_variant_visibility_follows_resource_qualification_not_catalog_presence(self) -> None:
        application = make_application()
        entries = application.registry.registry_snapshot().entries
        registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
        for entry in entries:
            identity = replace(entry.identity, variant="medium") if entry.identity.provider == "openai" else entry.identity
            registry.register(ResourceRegistration(identity=identity, freshness_ttl_seconds=TTL, capabilities=entry.capabilities))
            assert entry.observation is not None
            registry.apply_snapshot(replace(entry.observation, identity=identity))
        application.registry = registry
        application.client_authorizations = {CLIENT_ID: ClientAuthorization(allowed_providers=("openai",))}
        application.policy = replace(application.policy, preference_order=(
            ModelIdentity(provider="openai", model="gpt-5.6-luna", variant="max"),
            ModelIdentity(provider="openai", model="gpt-5.6-luna", variant="medium")))
        response = application.select_executable(client_id=CLIENT_ID, document=route_document())
        self.assertEqual(serialized_identities(response), {("openai", "gpt-5.6-luna", "medium")})
        self.assertEqual(as_dict(response["route"])["unroutable_identities"], [])
        self.assertIsNotNone(response["execution"])

    def test_authorized_unavailable_variant_explanation_is_retained(self) -> None:
        application = make_application(client_authorizations={CLIENT_ID: ClientAuthorization(allowed_providers=("openai",))})
        application.clock = lambda: T_EVAL + timedelta(seconds=TTL + 1)
        response = application.select_executable(client_id=CLIENT_ID, document=route_document())
        self.assertIsNone(response["execution"])
        expected = {("openai", "gpt-5.6-luna", "max"), ("openai", "gpt-5.6-luna", "medium")}
        self.assertEqual(serialized_identities(response), expected)
        self.assertNotIn("glm-5.3", json.dumps(response))
        exclusions = cast(list[object], as_dict(response["route"])["target_exclusions"])
        self.assertTrue(any(as_dict(item)["resource_id"] == "openai-http" for item in exclusions))

    def test_public_identity_view_changes_with_same_published_authority(self) -> None:
        old = make_application()
        published = make_application(client_authorizations={CLIENT_ID: ClientAuthorization(allowed_providers=("zai",))})
        old.application_source = lambda: published
        response = old.select_executable(client_id=CLIENT_ID, document=route_document())
        self.assertEqual(serialized_identities(response), {("zai", "glm-5.3", "high")})
        published.client_authorizations = {CLIENT_ID: ClientAuthorization(allowed_providers=())}
        response = old.select_executable(client_id=CLIENT_ID, document=route_document())
        self.assertIsNone(response["execution"])
        self.assertEqual(serialized_identities(response), set())

    def test_empty_registry_has_no_public_catalog_or_preference_identities(self) -> None:
        application = make_application(registry=ResourceRegistry(clock=lambda: canonical(T_EVAL)))
        application.policy = replace(application.policy, preference_order=tuple(entry.identity for entry in application.catalog.entries))
        response = application.select_executable(client_id=CLIENT_ID, document=route_document())
        self.assertIsNone(response["execution"])
        self.assertEqual(serialized_identities(response), set())
        self.assertEqual(as_dict(response["route"])["unroutable_identities"], [])

    def test_retired_adapter_exact_admission_has_typed_refusal_and_no_dispatch(self) -> None:
        adapter = ScriptedAdapter()
        application = make_application(adapters=[adapter])
        document = route_document()
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        original_pin = parse_pinned_reference(cast(str, prepared["model"]))
        application.adapters = AdapterRegistry()
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(caught.exception.code, "adapter_unavailable")
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(parse_pinned_reference(cast(str, prepared["model"])), original_pin)
        self.assertEqual(adapter.dispatch_count, 0)

    def test_revoked_worker_continuation_exact_admission_is_typed_without_dispatch(self) -> None:
        from tests.gateway_fixtures import build_registry, build_cells
        adapter = ScriptedAdapter(channel="worker_bridged")
        original = build_registry(with_worker=True).registry_snapshot().entries
        registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
        for entry in original:
            registry.register(ResourceRegistration(identity=entry.identity, freshness_ttl_seconds=TTL,
                capabilities=replace(entry.capabilities, reasoning_controls=True)))
            assert entry.observation is not None
            registry.apply_snapshot(entry.observation)
        application = make_application(adapters=[adapter], registry=registry, cells=build_cells(include_worker=True),
            continuation_capability_source=lambda: frozenset({"openai-worker"}))
        document = route_document()
        document["model"] = "sr-pin:openai-worker/openai/gpt-5.6-luna/max"
        as_dict(document["binding"])["requires_tool_calls"] = True
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        completion: dict[str, object] = {**COMPLETION, "tools": [{"type": "function", "function": {"name": "lookup"}}]}
        prepared = prepare_executable_completion(response, expected_request=document, completion=completion)
        original_pin = parse_pinned_reference(cast(str, prepared["model"]))
        application.continuation_capability_source = lambda: frozenset()
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(caught.exception.code, "worker_continuation_unavailable")
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(parse_pinned_reference(cast(str, prepared["model"])), original_pin)
        self.assertEqual(adapter.dispatch_count, 0)

    def test_authoritative_request_allowlist_matches_actual_parser(self) -> None:
        document = (Path(__file__).resolve().parents[1] / "docs/execution-surface.md").read_text()
        requests = document.split("## Requests", 1)[1]
        allowlist = requests.split("```text", 1)[1].split("```", 1)[0]
        keys = {key.strip() for key in allowlist.split(",")}
        self.assertEqual(keys, CHAT_COMPLETION_ALLOWED_KEYS)

    def test_combined_availability_refusals_do_not_block_healthy_other_channel(self) -> None:
        from tests.gateway_fixtures import build_registry, build_cells, observation
        for failure in ("resource_stale", "resource_never_observed", "resource_unhealthy"):
            for revoke_continuation in (False, True):
                with self.subTest(failure=failure, revoke_continuation=revoke_continuation):
                    direct = ScriptedAdapter()
                    worker = ScriptedAdapter(channel="worker_bridged")
                    entries = build_registry(with_worker=True).registry_snapshot().entries
                    registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
                    for entry in entries:
                        registry.register(ResourceRegistration(identity=entry.identity, freshness_ttl_seconds=TTL,
                            capabilities=replace(entry.capabilities, reasoning_controls=True)))
                        assert entry.observation is not None
                        registry.apply_snapshot(entry.observation)
                    application = make_application(adapters=[direct, worker], registry=registry,
                        cells=build_cells(include_worker=True),
                        continuation_capability_source=lambda: frozenset({"openai-worker"}))
                    document = route_document()
                    document["model"] = "sr-pin:openai-worker/openai/gpt-5.6-luna/max"
                    as_dict(document["binding"])["requires_tool_calls"] = True
                    response = application.select_executable(client_id=CLIENT_ID, document=document)
                    completion: dict[str, object] = {**COMPLETION, "tools": [{"type": "function", "function": {"name": "lookup"}}]}
                    prepared = prepare_executable_completion(response, expected_request=document, completion=completion)
                    original_pin = parse_pinned_reference(cast(str, prepared["model"]))
                    current = ResourceRegistry(clock=lambda: canonical(T_EVAL))
                    for entry in registry.registry_snapshot().entries:
                        current.register(ResourceRegistration(identity=entry.identity, freshness_ttl_seconds=TTL,
                            capabilities=entry.capabilities))
                        assert entry.observation is not None
                        snapshot = entry.observation
                        if entry.identity.resource_id == "openai-worker":
                            if failure == "resource_never_observed":
                                continue
                            if failure == "resource_stale":
                                snapshot = replace(snapshot, observed_at=canonical(T_EVAL - timedelta(seconds=TTL + 1)))
                            else:
                                snapshot = observation(entry.identity, status="auth_required")
                        current.apply_snapshot(snapshot)
                    application.registry = current
                    application.adapters = AdapterRegistry()
                    application.adapters.register(direct)
                    if revoke_continuation:
                        application.continuation_capability_source = lambda: frozenset()
                    mixed = copy.deepcopy(document)
                    mixed["model"] = "deep-coding"
                    selected = application.select_executable(client_id=CLIENT_ID, document=mixed)
                    self.assertIsNotNone(selected["execution"])
                    route = as_dict(selected["route"])
                    self.assertEqual(as_dict(as_dict(route["target"])["resource"])["channel"], "server_direct_http")
                    exclusions = cast(list[object], route["target_exclusions"])
                    exclusion = next(as_dict(item) for item in exclusions if as_dict(item)["resource_id"] == "openai-worker")
                    codes = ["adapter_unavailable", failure]
                    if revoke_continuation:
                        codes.append("worker_continuation_unavailable")
                    self.assertEqual(exclusion["stage"], "availability")
                    self.assertEqual(exclusion["reason_codes"], sorted(codes))
                    with self.assertRaises(GatewayError) as caught:
                        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
                    self.assertEqual(caught.exception.code, "target_unavailable")
                    self.assertEqual(caught.exception.http_status, 503)
                    self.assertEqual(parse_pinned_reference(cast(str, prepared["model"])), original_pin)
                    self.assertEqual(direct.dispatch_count, 0)
                    self.assertEqual(worker.dispatch_count, 0)

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

    def test_older_application_respects_one_current_authority_snapshot(self) -> None:
        for kind in ("provider_revoked", "resource_blocked", "retired_registry", "retired_adapter"):
            application = make_application()
            current_registry = application.registry
            current_adapters = application.adapters
            administrator = AdministratorConstraints()
            if kind == "provider_revoked":
                administrator = AdministratorConstraints(allowed_providers=())
            elif kind == "resource_blocked":
                administrator = AdministratorConstraints(blocked_resource_ids=tuple(
                    entry.identity.resource_id for entry in current_registry.registry_snapshot().entries))
            elif kind == "retired_registry":
                current_registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
            else:
                current_adapters = AdapterRegistry()
            calls: list[str] = []
            def authority(client: str):
                calls.append(client)
                return current_registry, administrator, ClientAuthorization(), current_adapters
            application.authority_source = authority
            response = application.select_executable(client_id=CLIENT_ID, document=route_document())
            self.assertIsNone(response["execution"], kind)
            self.assertEqual(calls, [CLIENT_ID])
            if kind in ("provider_revoked", "resource_blocked"):
                text = json.dumps(response)
                for entry in current_registry.registry_snapshot().entries:
                    self.assertNotIn(entry.identity.resource_id, text)
            if kind == "retired_adapter":
                self.assertTrue(any("adapter_unavailable" in cast(list[str], as_dict(item)["reason_codes"])
                    for item in cast(list[object], as_dict(response["route"])["target_exclusions"])))

    def test_current_authority_read_failure_never_restores_old_ready_target(self) -> None:
        application = make_application()
        def unavailable(client: str):
            _ = client
            raise RuntimeError("owned unavailable source")
        application.authority_source = unavailable
        with self.assertRaises(GatewayError) as caught:
            _ = application.select_executable(client_id=CLIENT_ID, document=route_document())
        self.assertEqual(caught.exception.code, "state_unavailable")

    def test_old_application_exact_admission_uses_lower_current_capability_and_state(self) -> None:
        for change in ("lower_context", "stale"):
            adapter = ScriptedAdapter()
            application = make_application(adapters=[adapter])
            entries = application.registry.registry_snapshot().entries
            def view(context: int, stale: bool):
                registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
                for entry in entries:
                    registry.register(ResourceRegistration(identity=entry.identity, freshness_ttl_seconds=TTL,
                        capabilities=replace(entry.capabilities, context_limit_tokens=context)))
                    assert entry.observation is not None
                    registry.apply_snapshot(replace(entry.observation,
                        observed_at=canonical(T_EVAL - timedelta(seconds=TTL + 1))) if stale else entry.observation)
                return registry
            application.registry = view(3000, False)
            document = route_document()
            as_dict(document["binding"])["minimum_input_context_tokens"] = 1000
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            current_registry = view(500 if change == "lower_context" else 3000, change == "stale")
            application.authority_source = lambda client: (
                current_registry, application.admin_constraints, ClientAuthorization(), application.adapters)
            with self.subTest(change=change), self.assertRaises(GatewayError):
                _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(adapter.dispatch_count, 0)

    def test_explicit_and_profile_context_floors_intersect_gateway_allowance(self) -> None:
        for source in ("explicit", "profile"):
            application = make_application()
            application.limits = replace(application.limits, max_input_context_tokens=1000)
            document = route_document()
            if source == "explicit":
                as_dict(as_dict(document["requirement"])["hard_constraints"])["minimum_input_context_tokens"] = 2000
            else:
                definition = application.profiles.definitions[0]
                application.profiles = TaskProfileCatalog(definitions=(replace(definition, requirement=replace(
                    definition.requirement, hard_constraints=replace(definition.requirement.hard_constraints,
                                                                     minimum_input_context_tokens=2000))),))
            with self.subTest(source=source), self.assertRaises(GatewayError) as caught:
                _ = application.select_executable(client_id=CLIENT_ID, document=document)
            self.assertEqual(caught.exception.code, "context_length_exceeded")

    def test_retained_known_context_cannot_bypass_allowance_at_exact_admission(self) -> None:
        adapter = ScriptedAdapter()
        application = self.output_application(adapter)
        entries = application.registry.registry_snapshot().entries
        registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
        for entry in entries:
            registry.register(ResourceRegistration(identity=entry.identity, freshness_ttl_seconds=TTL,
                capabilities=replace(entry.capabilities, context_limit_tokens=3000)))
            assert entry.observation is not None
            registry.apply_snapshot(entry.observation)
        application.registry = registry
        document = route_document()
        as_dict(as_dict(document["requirement"])["hard_constraints"])["minimum_input_context_tokens"] = 2000
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        application.limits = replace(application.limits, max_input_context_tokens=1000)
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(caught.exception.code, "context_length_exceeded")
        self.assertEqual(adapter.dispatch_count, 0)

    def test_genuine_output_floor_intersects_allowance_without_synthesizing_cap(self) -> None:
        for source in ("explicit", "profile"):
            adapter = ScriptedAdapter()
            application = make_application(adapters=[adapter])
            application.limits = replace(application.limits, max_output_tokens=1000)
            document = route_document()
            if source == "explicit":
                as_dict(as_dict(document["requirement"])["hard_constraints"])["minimum_output_tokens"] = 2000
            else:
                definition = application.profiles.definitions[0]
                application.profiles = TaskProfileCatalog(definitions=(replace(definition, requirement=replace(
                    definition.requirement, hard_constraints=replace(definition.requirement.hard_constraints,
                                                                     minimum_output_tokens=2000))),))
            self.assertNotIn("maximum_output_tokens", as_dict(document["binding"]))
            with self.subTest(source=source), self.assertRaises(GatewayError) as caught:
                _ = application.select_executable(client_id=CLIENT_ID, document=document)
            self.assertEqual(caught.exception.code, "output_limit_exceeded")
            self.assertEqual(adapter.dispatch_count, 0)

    def test_output_floor_refuses_lowered_published_allowance_without_a_cap(self) -> None:
        adapter = ScriptedAdapter()
        application = make_application(adapters=[adapter])
        registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
        for entry in application.registry.registry_snapshot().entries:
            registry.register(ResourceRegistration(identity=entry.identity, freshness_ttl_seconds=TTL,
                capabilities=replace(entry.capabilities, output_limit_control=False, output_limit_tokens=3000)))
            assert entry.observation is not None
            registry.apply_snapshot(entry.observation)
        application.registry = registry
        document = route_document()
        as_dict(as_dict(document["requirement"])["hard_constraints"])["minimum_output_tokens"] = 2000
        response = application.select_executable(client_id=CLIENT_ID, document=document)
        prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
        self.assertNotIn("max_completion_tokens", prepared)
        self.assertNotIn("max_tokens", prepared)
        published = make_application(adapters=[adapter], registry=registry)
        published.limits = replace(published.limits, max_output_tokens=1000)
        application.application_source = lambda: published
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
        self.assertEqual(caught.exception.code, "output_limit_exceeded")
        self.assertEqual(adapter.dispatch_count, 0)

    def test_retained_null_effort_rejects_same_identity_configured_drift(self) -> None:
        from tests.gateway_fixtures import build_registry, build_cells
        for channel in ("server_direct_http", "worker_bridged"):
            adapter = ScriptedAdapter(channel=channel)
            application = make_application(adapters=[adapter], registry=build_registry(with_worker=True),
                                           cells=build_cells(include_worker=True))
            original_entry = next(entry for entry in application.registry.registry_snapshot().entries
                if entry.identity.channel == channel and entry.identity.provider == "openai")
            identity = replace(original_entry.identity, variant="opaque-config" if channel == "server_direct_http" else None)
            registry = ResourceRegistry(clock=lambda: canonical(T_EVAL))
            registry.register(ResourceRegistration(identity=identity, freshness_ttl_seconds=TTL,
                capabilities=replace(original_entry.capabilities, reasoning_controls=True)))
            assert original_entry.observation is not None
            registry.apply_snapshot(replace(original_entry.observation, identity=identity))
            application.registry = registry
            application.catalog = replace(application.catalog, entries=tuple(replace(entry,
                identity=replace(entry.identity, variant="opaque-config"), reasoning_effort=None,
                hard_properties=replace(entry.hard_properties, supports_reasoning_mode=False))
                if entry.identity.variant == "max" else entry for entry in application.catalog.entries))
            document = route_document()
            document["model"] = f"sr-pin:{identity.resource_id}/openai/gpt-5.6-luna/opaque-config"
            response = application.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            self.assertIsNone(as_dict(prepared["execution_requirements"])["reasoning_effort"])
            self.assertNotIn("reasoning_effort", prepared)
            current = make_application(adapters=[adapter], registry=registry, cells=build_cells(include_worker=True))
            current.catalog = replace(application.catalog, entries=tuple(replace(entry, reasoning_effort="high",
                hard_properties=replace(entry.hard_properties, supports_reasoning_mode=True))
                if entry.identity.variant == "opaque-config" else entry for entry in application.catalog.entries))
            application.application_source = lambda: current
            with self.subTest(channel=channel), self.assertRaises(GatewayError) as caught:
                _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(caught.exception.code, "execution_requirements_invalid")
            self.assertEqual(adapter.dispatch_count, 0)

    def test_published_replacement_artifacts_and_limits_reject_before_dispatch(self) -> None:
        for change in ("limits", "profile_version", "profile_content", "catalog", "compatibility"):
            adapter = ScriptedAdapter()
            original = make_application(adapters=[adapter])
            document = route_document()
            response = original.select_executable(client_id=CLIENT_ID, document=document)
            prepared = prepare_executable_completion(response, expected_request=document, completion=COMPLETION)
            replacement = make_application(adapters=[adapter])
            if change == "limits":
                replacement.limits = replace(replacement.limits, max_input_context_tokens=1)
            elif change == "profile_version":
                replacement.profile_policy_version = 2
            elif change == "profile_content":
                definition = replacement.profiles.definitions[0]
                replacement.profiles = TaskProfileCatalog(definitions=(replace(definition, requirement=replace(
                    definition.requirement, capability_minima=CapabilityMinima(reasoning=4, coding=4))),))
            elif change == "compatibility":
                replacement.compatibility_cells = tuple(replace(cell, value="UNSUPPORTED")
                    if cell.feature == "reasoning_controls" else cell for cell in replacement.compatibility_cells)
            else:
                pin = parse_pinned_reference(cast(str, prepared["model"]))
                replacement.catalog = replace(replacement.catalog, entries=tuple(replace(entry, capabilities=replace(
                    entry.capabilities, reasoning=replace(entry.capabilities.reasoning, rating=1)))
                    if entry.identity == pin.model else entry for entry in replacement.catalog.entries))
            original.application_source = lambda: replacement
            with self.subTest(change=change), self.assertRaises(GatewayError):
                _ = original.execute(client_id=CLIENT_ID, request=parse_chat_request(prepared))
            self.assertEqual(adapter.dispatch_count, 0)

    def test_published_selection_uses_new_profile_limits_and_aliases(self) -> None:
        original = make_application()
        replacement = make_application()
        replacement.profile_policy_version = 2
        original.application_source = lambda: replacement
        response = original.select_executable(client_id=CLIENT_ID, document=route_document())
        context = as_dict(as_dict(response["execution"])["execution_requirements"])
        self.assertEqual(context["profile_policy_version"], 2)
        replacement.limits = replace(replacement.limits, max_input_context_tokens=1000)
        document = route_document()
        as_dict(as_dict(document["requirement"])["hard_constraints"])["minimum_input_context_tokens"] = 2000
        with self.assertRaises(GatewayError) as caught:
            _ = original.select_executable(client_id=CLIENT_ID, document=document)
        self.assertEqual(caught.exception.code, "context_length_exceeded")

    def test_unavailable_published_application_does_not_use_old_artifacts(self) -> None:
        original = make_application()
        def unavailable():
            raise RuntimeError("owned unavailable publisher")
        original.application_source = unavailable
        with self.assertRaises(GatewayError) as caught:
            _ = original.select_executable(client_id=CLIENT_ID, document=route_document())
        self.assertEqual(caught.exception.code, "state_unavailable")

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
    def test_embedded_authenticated_bearer_is_not_retained_or_reflected(self) -> None:
        port = self.make_server()
        document = route_document()
        as_dict(as_dict(document["requirement"])["hard_constraints"])["privacy_constraint"] = CLIENT_KEY + ":tag"
        for authorization in ("Bearer " + CLIENT_KEY, "bearer " + CLIENT_KEY,
                              "bEaReR " + CLIENT_KEY, "Bearer   " + CLIENT_KEY,
                              "  bEaReR   " + CLIENT_KEY + "  "):
            headers = dict(AUTH_HEADERS)
            headers["Authorization"] = authorization
            connection = self.client(port)
            connection.request("POST", "/v1/route", body=json.dumps(document), headers=headers)
            response = connection.getresponse()
            text = response.read().decode()
            self.assertEqual(response.status, 400)
            self.assertNotIn(CLIENT_KEY, text)
            self.assertIn("execution_requirements_invalid", text)

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
