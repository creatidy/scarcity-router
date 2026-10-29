"""D-082 controlled behavior tests, NOT real-source positive acceptance.

Public Z.ai/model.dev acquisition observations are recorded separately in the
canonical issue. No network, model inference or fabricated source freshness here.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from model_intelligence.contract import Applicability, NATIVE_PAYLOAD_SCHEMA, PublicEvidence, SourceNotice
from model_intelligence.evidence import ModelClaim
from model_intelligence.zai import SCOPE as ZAI_SCOPE

from scarcity_router.machine_api import selection_envelope
from scarcity_router.resource_state import ResourceRegistryEntry
from scarcity_router.mi_consumer import MIConsumer, ModelBinding, canonical
from scarcity_router.routing_core import PinnedTarget, RequestBinding, admit_pinned_target, route_request
from scarcity_router.selection_types import CapabilityMinima, HardConstraints, TaskRequirement
from tests import test_mi_consumer as fixture
from tests import test_routing_core as routes


class ActiveMIAdmissionTests(unittest.TestCase):
    def decision(self, public: PublicEvidence, *, requirement: TaskRequirement | None = None, at: datetime = fixture.T):
        consumer = fixture.consumer(public)
        expected = fixture.encoded(public)[1]
        return fixture.MIConsumerTests().decision(None, at=at, admission=consumer.admission(expected, evaluated_at=at), requirement=requirement)

    def test_actual_selector_hard_boundary_changes_only_with_explicit_opt_in(self) -> None:
        requirement = TaskRequirement("L0", CapabilityMinima(), hard_constraints=HardConstraints(minimum_input_context_tokens=8000))
        baseline = fixture.MIConsumerTests().decision(None, requirement=requirement)
        explanation = fixture.MIConsumerTests().decision(fixture.consumer(fixture.public()), requirement=requirement)
        active = self.decision(fixture.public(), requirement=requirement)
        self.assertIsNotNone(baseline.selected)
        self.assertIsNotNone(explanation.selected)
        self.assertIsNone(active.selected)
        self.assertEqual("hard_constraint", active.excluded[0].exclusion_stage)
        self.assertEqual("4096", active.excluded[0].hard_constraint_failures[0].actual_value)
        self.assertIsNotNone(active.mi_use)
        _ = selection_envelope(active)

    def test_supported_fact_can_satisfy_request_without_expanding_bootstrap(self) -> None:
        requirement = TaskRequirement("L0", CapabilityMinima(), hard_constraints=HardConstraints(minimum_input_context_tokens=4096))
        decision = self.decision(fixture.public(), requirement=requirement)
        self.assertIsNotNone(decision.selected)
        assert decision.mi_use is not None
        self.assertIn(b"d082-admission/1", decision.mi_use.context)
        _ = selection_envelope(decision)

    def test_public_limit_never_increases_bootstrap_or_fills_unknown(self) -> None:
        public = fixture.public()
        claim = public.evidence.claims[0]
        assert isinstance(claim, ModelClaim)
        value = replace(public, evidence=replace(public.evidence, claims=(replace(claim, payload=replace(fixture.LIMIT, amount=Decimal(1_000_000_000))),)))
        consumer = fixture.consumer(value)
        admission = consumer.admission(fixture.encoded(value)[1], evaluated_at=fixture.T)
        projected, blocked = admission.prepare(fixture.CATALOG, fixture.T)
        self.assertEqual(fixture.ENTRY.hard_properties, projected.entries[0].hard_properties)
        self.assertFalse(blocked)
        unknown = replace(fixture.CATALOG, entries=(replace(fixture.ENTRY, hard_properties=replace(fixture.ENTRY.hard_properties, input_context_tokens=None)),))
        projected, blocked = admission.prepare(unknown, fixture.T)
        self.assertIsNone(projected.entries[0].hard_properties.input_context_tokens)
        self.assertIn(fixture.ENTRY.identity, blocked)

    def test_unknown_stale_conflict_future_and_critical_withdrawal_cannot_authorize(self) -> None:
        initial = fixture.public()
        observation = initial.evidence.observations[0]
        claim = initial.evidence.claims[0]
        assert isinstance(claim, ModelClaim)
        variants = (
            replace(initial, evidence=replace(initial.evidence, observations=(replace(observation, fresh_until=None),))),
            replace(initial, evidence=replace(initial.evidence, observations=(replace(observation, fresh_until=fixture.T),))),
            replace(initial, evidence=replace(initial.evidence, claims=(replace(claim, effective_from=fixture.T + fixture.DAY),))),
            replace(initial, evidence=initial.evidence.extend(claims=(replace(claim, claim_id="conflict", payload=replace(fixture.LIMIT, amount=Decimal(1))),))),
            replace(initial, notices=(SourceNotice("critical", observation.observation_id, frozenset({"statement:owned-claim"}), fixture.T, "revocation", True),)),
        )
        for value in variants:
            with self.subTest(public=value):
                decision = self.decision(value)
                self.assertIsNone(decision.selected)
                self.assertEqual("policy_blocked", decision.excluded[0].reason_codes[0])
                _ = selection_envelope(decision)

    def test_exact_reference_refresh_replay_and_no_automatic_reapproval(self) -> None:
        original = fixture.public()
        consumer = fixture.consumer(original)
        original_ref = fixture.encoded(original)[1]
        frozen = consumer.admission(original_ref, evaluated_at=fixture.T)
        before = fixture.MIConsumerTests().decision(None, admission=frozen)
        assert before.mi_use is not None
        envelope = canonical(selection_envelope(before))
        claim = original.evidence.claims[0]
        assert isinstance(claim, ModelClaim)
        updated = replace(original, evidence=original.evidence.extend(claims=(replace(claim, claim_id="new", revises=claim.claim_id, payload=replace(fixture.LIMIT, amount=Decimal(2048))),)))
        consumer.adopt(*fixture.encoded(updated))
        untrusted = fixture.MIConsumerTests().decision(None, admission=consumer.admission(original_ref, evaluated_at=fixture.T))
        self.assertIsNone(untrusted.selected)
        self.assertIsNotNone(fixture.MIConsumerTests().decision(None, admission=frozen).selected)
        self.assertEqual(envelope, canonical(selection_envelope(before)))
        with self.assertRaisesRegex(ValueError, "mi_evaluation_instant_mismatch"):
            _ = fixture.MIConsumerTests().decision(None, admission=frozen, at=fixture.T + fixture.DAY)
        expired = self.decision(original, at=fixture.T + fixture.DAY)
        self.assertIsNone(expired.selected)
        _ = selection_envelope(expired)

    def test_current_zai_scope_is_not_repaired_by_alias_or_fresh_retrieval(self) -> None:
        # Actual imported producer scope; controlled schema-negative fixture only.
        self.assertIsNone(ZAI_SCOPE.channel)
        self.assertIsNotNone(ZAI_SCOPE.configuration)
        binding = replace(fixture.BINDING, subject=replace(fixture.SUBJECT, channel=None, configuration=None))
        self.assertFalse(binding.matches(fixture.ENTRY, ZAI_SCOPE))

    def world(self, *, amount: int = 4096, at: datetime = routes.EVAL_AT, channel: str = "local_app_adapter", meaning: str = "input-allowance"):
        request = routes.request_fixture(request=RequestBinding(minimum_input_context_tokens=8000), cells=(
            routes.compatibility_fixture(channel, "openai", "gpt-5.6-luna", "reasoning_controls"),
        ))
        entry = request.catalog.entries[0]
        subject = replace(fixture.SUBJECT, provider_id=entry.identity.provider, provider_model_id=entry.identity.model,
            channel=channel, configuration=(("reasoning_effort", entry.reasoning_effort),))
        scope = Applicability("controlled", channel, subject.configuration, fixture.SCOPE.source_ids)
        original = fixture.public()
        observation = replace(original.evidence.observations[0], retrieved_at=at-timedelta(days=1), observed_at=at-timedelta(days=1), fresh_until=at+timedelta(days=1))
        old_claim = original.evidence.claims[0]
        assert isinstance(old_claim, ModelClaim)
        claim = replace(old_claim, subject=subject, payload=replace(fixture.LIMIT, amount=Decimal(amount), meaning=meaning), effective_from=at-timedelta(days=1))
        public = replace(original, applicability=scope, evidence=replace(original.evidence, observations=(observation,), claims=(claim,)))
        from model_intelligence.contract import encode_public_evidence

        data, ref = encode_public_evidence(public, produced_at=at-timedelta(seconds=1), payload_schema=NATIVE_PAYLOAD_SCHEMA)
        consumer = MIConsumer(expected_scope=scope, bindings=(ModelBinding(entry.identity, subject, fixture.LIMIT.dimension, meaning, fixture.LIMIT.basis or ""),), max_bytes=fixture.BOUND)
        consumer.adopt(data, ref)
        return request, consumer.admission(ref, evaluated_at=at)

    def test_actual_route_and_exact_pin_admission_enforce_public_ceiling(self) -> None:
        request, admission = self.world()
        pin = PinnedTarget("openai-sub", request.catalog.entries[0].identity)
        request = replace(request, candidate_identities=(pin.model,))
        baseline = route_request(request)
        self.assertIsNotNone(baseline.target)
        assert baseline.target is not None
        self.assertEqual(pin.model, baseline.target.model)
        self.assertTrue(admit_pinned_target(request, pinned_target=pin).approved)
        active = replace(request, mi_admission=admission)
        decision = route_request(active)
        self.assertFalse(admit_pinned_target(active, pinned_target=pin).approved)
        self.assertIsNone(decision.target)
        assert decision.mi_use is not None
        _ = decision.mi_use.envelope(decision.to_dict())

    def test_admission_supported_channel_effort_auth_and_expiry(self) -> None:
        request, admission = self.world(amount=16000)
        pin = PinnedTarget("openai-sub", request.catalog.entries[0].identity)
        active = replace(request, mi_admission=admission)
        approved = admit_pinned_target(active, pinned_target=pin)
        self.assertTrue(approved.approved)
        assert approved.mi_use is not None
        _ = approved.mi_use.envelope(approved.to_dict())
        other_channel = admit_pinned_target(active, pinned_target=replace(pin, resource_id="openai-payg"))
        self.assertFalse(other_channel.approved)
        unknown_effort = admit_pinned_target(replace(active, compatibility_cells=()), pinned_target=pin)
        self.assertFalse(unknown_effort.approved)
        unauthorized = admit_pinned_target(replace(active, admin_constraints=replace(active.admin_constraints, blocked_resource_ids=(pin.resource_id,))), pinned_target=pin)
        self.assertFalse(unauthorized.approved)
        with self.assertRaisesRegex(ValueError, "mi_evaluation_instant_mismatch"):
            _ = admit_pinned_target(replace(active, evaluated_at=request.evaluated_at+timedelta(days=1)), pinned_target=pin)
        self.assertIsNotNone(approved.target)

    def test_runtime_resource_bounds_are_not_lost_when_mi_is_available(self) -> None:
        request, admission = self.world(amount=16000)
        unknown_entries = tuple(replace(entry, hard_properties=replace(
            entry.hard_properties, input_context_tokens=None,
        ), capacity_binding_resources=("openai-sub",)) for entry in request.catalog.entries)
        registry_entries: list[ResourceRegistryEntry] = []
        for resource in request.registry_snapshot.entries:
            if resource.identity.resource_id == "openai-sub":
                identity = replace(resource.identity, variant=unknown_entries[0].identity.variant)
                assert resource.observation is not None
                resource = replace(resource, identity=identity, observation=replace(resource.observation, identity=identity))
            registry_entries.append(resource)
        request = replace(request, catalog=replace(
            request.catalog, catalog_version=None, updated_on=None, entries=unknown_entries,
        ), registry_snapshot=replace(request.registry_snapshot, entries=tuple(registry_entries)), mi_admission=admission)
        pin = PinnedTarget("openai-sub", request.catalog.entries[0].identity)
        decision = route_request(request)
        self.assertIsNotNone(decision.target)
        self.assertTrue(admit_pinned_target(request, pinned_target=pin).approved)

    def test_missing_mi_publication_preserves_independent_local_evidence(self) -> None:
        request, admission = self.world(amount=16000)
        missing = replace(admission, captured=(None, admission.captured[1]))
        request = replace(request, mi_admission=missing)
        decision = route_request(request)
        self.assertIsNotNone(decision.target)
        pin = PinnedTarget("openai-sub", request.catalog.entries[0].identity)
        self.assertTrue(admit_pinned_target(request, pinned_target=pin).approved)

    def test_output_allowance_also_intersects_existing_admission(self) -> None:
        request, admission = self.world(meaning="output-allowance")
        request = replace(request, request=RequestBinding(maximum_output_tokens=8000))
        pin = PinnedTarget("openai-sub", request.catalog.entries[0].identity)
        self.assertTrue(admit_pinned_target(request, pinned_target=pin).approved)
        self.assertFalse(admit_pinned_target(replace(request, mi_admission=admission), pinned_target=pin).approved)


if __name__ == "__main__":
    _ = unittest.main()
