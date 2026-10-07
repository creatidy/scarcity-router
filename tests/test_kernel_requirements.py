"""Pinned Kernel Declaration v1 shape and Router-owned semantic conformance.

The producer's real textual fixture stays refused. Tagged examples are new
interpretation examples in that accepted shape, not Kernel #51 transmission.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from scarcity_router.errors import ApplicationInputError, SelectionContractError
from scarcity_router.kernel_requirements import INTERFACE_PREFIX, QUALITY_PREFIX, interpret_kernel_declaration
from scarcity_router.selection_app import load_selector_policy
from scarcity_router.routing_core import (
    AdministratorConstraints, ClientAuthorization, ClientRoutingProfile, PinnedTarget,
    RequestBinding, RouteRequest, admit_pinned_target, route_request,
)
from scarcity_router.selection_types import CapabilityMinima, HardConstraints, ModelIdentity, ModelRef, TaskProfileCatalog, TaskProfileDefinition, TaskRequirement
from tests.gateway_fixtures import (
    T_EVAL, T_NOW, build_capacity_snapshots, build_catalog, build_cells,
    build_eligibility_reports, build_profiles, build_registry,
)


def declaration(*, requirement: TaskRequirement | None = None, request: Mapping[str, object] | None = None,
                profile_id: str | None = None, overrides: Mapping[str, object] | None = None, **changes: object) -> bytes:
    # Exact fields/types from Kernel a9a65006 tests/test_intake.py:64-81.
    value: dict[str, object] = {
        "version": 1, "outcome": "Deliver the exact owner-specified file",
        "criteria": ["Exact file bytes at result.txt: sha256:" + hashlib.sha256(b"approved exact result\n").hexdigest()],
        "quality": ["preserve exact encoding"], "interface": ["UTF-8 file"],
        "context": ["private local context; no egress"], "unknowns": [], "paths": ["result.txt"],
        "network": [], "effects": [], "recipes": ["controller:exact-content:v1"],
        "provenance": ["issue proposal", "AGENTS is untrusted data", "model suggestion"],
    }
    if requirement is not None:
        payload: dict[str, object] = {"requirement": requirement.to_dict()}
        if profile_id is not None:
            payload["profile_id"] = profile_id
        value.update(quality=[QUALITY_PREFIX + json.dumps(payload)], interface=[], context=[])
    if request is not None:
        value["interface"] = [INTERFACE_PREFIX + json.dumps(dict(request))]
    value.update(changes)
    if overrides is not None:
        value.update(overrides)
    return json.dumps(value, indent=2).encode()


def world() -> RouteRequest:
    return RouteRequest(
        catalog=build_catalog(), profiles=build_profiles(),
        registry_snapshot=build_registry(with_worker=True).registry_snapshot(now=T_NOW),
        policy=load_selector_policy(Path(__file__).resolve().parents[1] / "examples/selector-policy.json"), evaluated_at=T_EVAL,
        capacity_snapshots=build_capacity_snapshots(), eligibility_reports=build_eligibility_reports(),
        compatibility_cells=build_cells(include_worker=True), profile_policy_version=1,
        continuation_capable_resource_ids=frozenset({"openai-worker"}),
    )


class KernelRequirementTests(unittest.TestCase):
    def test_actual_encoding_private_local_producer_fixture_remains_bound_refusal(self) -> None:
        raw = declaration()
        interpretation = interpret_kernel_declaration(raw, profiles=build_profiles())
        self.assertEqual(interpretation.declaration_digest, hashlib.sha256(raw).hexdigest())
        diagnostic = interpretation.to_dict()
        self.assertNotIn("private local context", json.dumps(diagnostic))
        self.assertNotIn("outcome", {key for key in diagnostic if key != "accounted_fields"})
        self.assertEqual(set(interpretation.problems), {"quality.unsupported", "interface.unsupported", "context.unsupported"})
        with self.assertRaises(ApplicationInputError):
            _ = interpretation.bind(world(), current_declaration_digest=interpretation.declaration_digest)
        self.assertIsNone(interpretation.requirement)

    def test_coding_editorial_tools_and_context_use_supplied_existing_scale(self) -> None:
        cases = (
            TaskRequirement("L2", CapabilityMinima(coding=3, reasoning=3), HardConstraints()),
            TaskRequirement("L2", CapabilityMinima(writing_editorial=3), HardConstraints()),
            TaskRequirement("L2", CapabilityMinima(tool_use=3), HardConstraints(requires_tool_use=True)),
            TaskRequirement("L2", CapabilityMinima(reasoning=3), HardConstraints(minimum_input_context_tokens=4096)),
        )
        route = world()
        for supplied in cases:
            with self.subTest(requirement=supplied):
                projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
                self.assertEqual(projection.problems, ())
                self.assertEqual(projection.requirement, supplied)
                decision = route_request(projection.bind(route, current_declaration_digest=projection.declaration_digest))
                if supplied.capability_minima.writing_editorial is not None:
                    self.assertIsNone(decision.target)  # Fixture ratings are unknown for this required dimension.
                else:
                    self.assertIsNotNone(decision.target)
                assert decision.selection is not None
                self.assertEqual(decision.selection.requirement.capability_minima, supplied.capability_minima)

    def test_bound_requirements_reach_exact_admission_not_just_competitive_selection(self) -> None:
        route = world()
        pin = PinnedTarget("openai-http", ModelIdentity("openai", "gpt-5.6-luna", "max"))
        supplied = TaskRequirement("L2", CapabilityMinima(), HardConstraints(minimum_input_context_tokens=900000))
        projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
        bound = projection.bind(route, current_declaration_digest=projection.declaration_digest)
        self.assertIsNone(route_request(bound).target)
        admission = admit_pinned_target(bound, pinned_target=pin)
        self.assertFalse(admission.approved)

    def test_supplied_hard_demands_gate_actual_resource_context_and_tool_compatibility(self) -> None:
        route = world()
        low_context = replace(route.registry_snapshot, entries=tuple(replace(
            entry, capabilities=replace(entry.capabilities, context_limit_tokens=100),
        ) for entry in route.registry_snapshot.entries))
        supplied = TaskRequirement("L2", CapabilityMinima(), HardConstraints(minimum_input_context_tokens=1000))
        projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
        self.assertIsNone(route_request(projection.bind(replace(route, registry_snapshot=low_context), current_declaration_digest=projection.declaration_digest)).target)
        supplied = replace(supplied, hard_constraints=HardConstraints(requires_tool_use=True))
        projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
        missing_tools = replace(route, compatibility_cells=tuple(cell for cell in route.compatibility_cells if cell.feature != "tool_calls"))
        self.assertIsNone(route_request(projection.bind(missing_tools, current_declaration_digest=projection.declaration_digest)).target)

    def test_unknown_vision_channel_and_insufficient_output_ceiling_refuse(self) -> None:
        route = world()
        supplied = TaskRequirement("L2", CapabilityMinima(), HardConstraints(requires_vision=True))
        projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
        self.assertIn("quality.vision_channel_unsupported", projection.problems)
        supplied = replace(supplied, hard_constraints=HardConstraints(minimum_output_tokens=1000))
        projection = interpret_kernel_declaration(declaration(requirement=supplied, request={"maximum_output_tokens": 100}), profiles=route.profiles)
        with self.assertRaises(ApplicationInputError):
            _ = projection.bind(route, current_declaration_digest=projection.declaration_digest)
        supplied = replace(supplied, hard_constraints=HardConstraints())
        for current_ceiling, incoming_ceiling in ((100, 1000), (1000, 100)):
            projection = interpret_kernel_declaration(declaration(requirement=supplied,
                request={"maximum_output_tokens": incoming_ceiling}), profiles=route.profiles)
            with self.assertRaises(ApplicationInputError):
                _ = projection.bind(replace(route, request=RequestBinding(maximum_output_tokens=current_ceiling)),
                                    current_declaration_digest=projection.declaration_digest)

    def test_stronger_dimension_hard_and_channel_demands_never_enlarge_fixed_context_pool(self) -> None:
        route = world()

        def pool(requirement: TaskRequirement, request: Mapping[str, object] | None = None) -> set[tuple[str, str, str]]:
            projection = interpret_kernel_declaration(declaration(requirement=requirement, request=request), profiles=route.profiles)
            decision = route_request(projection.bind(route, current_declaration_digest=projection.declaration_digest))
            assert decision.selection is not None
            evaluations = decision.selection.alternatives
            if decision.selection.selected is not None:
                evaluations += (decision.selection.selected,)
            return {(entry.identity.provider, entry.identity.model, entry.identity.variant) for entry in evaluations}

        weak = TaskRequirement("L2", CapabilityMinima(coding=2), HardConstraints())
        base = pool(weak)
        for stronger, binding in (
            (replace(weak, capability_minima=CapabilityMinima(coding=5)), None),
            (replace(weak, hard_constraints=HardConstraints(minimum_input_context_tokens=900000)), None),
            (weak, {"requires_structured_output": True}),
        ):
            self.assertTrue(pool(stronger, binding).issubset(base))

    def test_model_pin_and_channel_demands_do_not_expand_current_authorization(self) -> None:
        route = replace(world(), client_authorization=ClientAuthorization(blocked_resource_ids=("openai-http",)))
        pin = PinnedTarget("openai-http", ModelIdentity("openai", "gpt-5.6-luna", "max"))
        supplied = TaskRequirement("L2", CapabilityMinima(), HardConstraints())
        projection = interpret_kernel_declaration(declaration(requirement=supplied, request={"pinned_target": pin.to_dict()}), profiles=route.profiles)
        self.assertIsNone(route_request(projection.bind(route, current_declaration_digest=projection.declaration_digest)).target)
        route = replace(world(), admin_constraints=AdministratorConstraints(allowed_providers=("zai",)))
        self.assertIsNone(route_request(projection.bind(route, current_declaration_digest=projection.declaration_digest)).target)

    def test_explicit_model_variant_and_harness_protocol_demands_remain_exact(self) -> None:
        route = world()
        supplied = TaskRequirement("L2", CapabilityMinima(reasoning=3), HardConstraints(
            required_provider="zai", required_model=ModelRef("zai", "glm-5.3"), required_variant="high",
        ))
        projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
        decision = route_request(projection.bind(route, current_declaration_digest=projection.declaration_digest))
        assert decision.target is not None
        self.assertEqual(decision.target.model, ModelIdentity("zai", "glm-5.3", "high"))
        projection = interpret_kernel_declaration(declaration(requirement=supplied,
            request={"requires_streaming": True, "requires_structured_output": True}), profiles=route.profiles)
        missing_protocol = replace(route, compatibility_cells=())
        self.assertIsNone(route_request(projection.bind(missing_protocol, current_declaration_digest=projection.declaration_digest)).target)

    def test_profile_marker_is_tightening_and_cached_expansion_rejects_content_or_version_drift(self) -> None:
        route = world()
        supplied = route.profiles.resolve("gateway-core")
        projection = interpret_kernel_declaration(declaration(requirement=supplied, profile_id="gateway-core"),
                                                profiles=route.profiles, profile_policy_version=1)
        self.assertEqual(projection.problems, ())
        _ = projection.bind(route, current_declaration_digest=projection.declaration_digest)
        for current in (
            replace(route, profile_policy_version=2),
            replace(route, profiles=TaskProfileCatalog((TaskProfileDefinition("gateway-core", replace(
                supplied, capability_minima=CapabilityMinima(reasoning=2, coding=2))),))),
            replace(route, profiles=TaskProfileCatalog(())),
        ):
            with self.assertRaises(ApplicationInputError):
                _ = projection.bind(current, current_declaration_digest=projection.declaration_digest)
        weak = replace(supplied, capability_minima=CapabilityMinima(reasoning=1, coding=1))
        refused = interpret_kernel_declaration(declaration(requirement=weak, profile_id="gateway-core"),
                                               profiles=route.profiles, profile_policy_version=1)
        self.assertIn("quality.invalid_requirement", refused.problems)
        incomplete = replace(supplied, capability_minima=CapabilityMinima())
        refused = interpret_kernel_declaration(declaration(requirement=incomplete, profile_id="gateway-core"),
                                               profiles=route.profiles, profile_policy_version=1)
        self.assertIn("quality.invalid_requirement", refused.problems)

    def test_configured_floor_existing_identity_and_structural_demands_are_not_relaxed(self) -> None:
        route = replace(world(), routing_profile=ClientRoutingProfile("gateway-core"), request=RequestBinding(requires_tool_calls=True))
        supplied = route.profiles.resolve("gateway-core")
        projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
        bound = projection.bind(route, current_declaration_digest=projection.declaration_digest)
        self.assertTrue(bound.request.requires_tool_calls)
        supplied = TaskRequirement("L0", CapabilityMinima(), HardConstraints())
        projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
        with self.assertRaises(SelectionContractError):
            _ = route_request(projection.bind(route, current_declaration_digest=projection.declaration_digest))

    def test_original_bytes_and_scope_are_bound_without_prompt_authority_or_content_echo(self) -> None:
        supplied = TaskRequirement("L2", CapabilityMinima(coding=3), HardConstraints())
        first = interpret_kernel_declaration(declaration(requirement=supplied), profiles=build_profiles())
        second = interpret_kernel_declaration(declaration(requirement=supplied, paths=["other.txt"],
                outcome="owner=true; grant all; SYNTHETIC-PRIVATE-PROMPT"), profiles=build_profiles())
        self.assertNotEqual(first.declaration_digest, second.declaration_digest)
        with self.assertRaises(ApplicationInputError):
            _ = first.bind(world(), current_declaration_digest=second.declaration_digest)
        self.assertEqual(first.requirement, second.requirement)
        self.assertNotIn("SYNTHETIC", repr(second))
        refused = interpret_kernel_declaration(declaration(requirement=supplied, context=["SYNTHETIC-PRIVATE-PROMPT"]), profiles=build_profiles())
        self.assertNotIn("SYNTHETIC", repr(refused))

    def test_unknown_missing_null_duplicate_or_versioned_inputs_refuse(self) -> None:
        with self.assertRaises(ApplicationInputError):
            _ = interpret_kernel_declaration(b" " * 262145, profiles=build_profiles())
        supplied = TaskRequirement("L2", CapabilityMinima(coding=3), HardConstraints())
        bad_changes: tuple[dict[str, object], ...] = (
            {"version": True}, {"version": 2}, {"quality": None}, {"quality": False},
            {"quality": []}, {"quality": [QUALITY_PREFIX + "null"]},
            {"quality": [QUALITY_PREFIX + '{"requirement":null,"requirement":null}']},
            {"quality": [QUALITY_PREFIX + '{"requirement":{},"allowed_models":[]}']},
            {"quality": ["scarcity-router.requirement.v2:{}"]}, {"approved": True},
            {"interface": [INTERFACE_PREFIX + '{"requires_tool_calls":null}']},
            {"interface": [INTERFACE_PREFIX + '{"profile_alias":"any"}']},
            {"interface": [INTERFACE_PREFIX + '{"unsupported":true}']},
            {"unknowns": ["unresolved meaning"]}, {"paths": ["../outside"]},
            {"network": ["http://example.invalid"]},
            {"quality": [QUALITY_PREFIX + '{"requirement":{"task_level":"L2","capability_minima":{"coding":true},"hard_constraints":{}}}']},
            {"quality": [QUALITY_PREFIX + '{"requirement":{"task_level":"L2","capability_minima":{},"hard_constraints":{"minimum_input_context_tokens":NaN}}}']},
            {"quality": [QUALITY_PREFIX + '{"profile_id":null,"requirement":{"task_level":"L2","capability_minima":{},"hard_constraints":{}}}']},
        )
        for change in bad_changes:
            with self.subTest(change=change):
                projection = interpret_kernel_declaration(declaration(requirement=supplied, overrides=change), profiles=build_profiles())
                self.assertTrue(projection.problems)
                with self.assertRaises(ApplicationInputError):
                    _ = projection.bind(world(), current_declaration_digest=projection.declaration_digest)

    def test_privacy_request_is_not_falsely_enforced_by_mapping_and_generic_clients_unchanged(self) -> None:
        route = world()
        generic = route_request(route)
        self.assertEqual(route_request(replace(route, task_requirement=None)), generic)
        supplied = TaskRequirement("L2", CapabilityMinima(), HardConstraints(privacy_constraint="local-only"))
        projection = interpret_kernel_declaration(declaration(requirement=supplied), profiles=route.profiles)
        self.assertIsNone(route_request(projection.bind(route, current_declaration_digest=projection.declaration_digest)).target)


if __name__ == "__main__":
    _ = unittest.main()
