"""Exact-pinned MI producer -> real Router selector -> utilization conformance.

Owned synthetic public evidence only. No credentials, provider or model calls.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

from model_intelligence.contract import (
    Applicability, NATIVE_PAYLOAD_SCHEMA, PublicEvidence, SourceNotice,
    encode_public_evidence,
)
from model_intelligence.evidence import Evidence, Model, ModelClaim, NativeLimit, Observation
from model_intelligence.publication import CutReference, decode_cut, encode_cut
from model_intelligence.utilization import decode_decision_utilization

from scarcity_router.capacity import CapacitySnapshot, CapacityWindow
from scarcity_router.machine_api import selection_envelope
from scarcity_router.mi_consumer import MIAdmission, MIConsumer, ModelBinding, canonical, explained_catalog
from scarcity_router.selection_app import select_from_inputs
from scarcity_router.selection_types import (
    CapabilityAssessment, CapabilityAssessments, CapacityScopeRef, ModelCatalog,
    ModelCatalogEntry, ModelHardProperties, ModelIdentity, TaskProfileCatalog, TaskRequirement,
)
from scarcity_router.status import StatusCollectors
from scarcity_router.providers.openai_codex_acquisition import OpenAICodexObservation
from tests.observation import paired_observation

T = datetime(2026, 10, 26, 0, 0, 0, 123456, tzinfo=timezone.utc)
DAY = timedelta(days=1)
BOUND = 256 * 1024
UNKNOWN = CapabilityAssessment(None)
ENTRY = ModelCatalogEntry(
    identity=ModelIdentity("openai", "synthetic-model", "high"),
    display_name="Synthetic model", hard_properties=ModelHardProperties(
        supports_reasoning_mode=True, input_context_tokens=8192, output_tokens=8192,
    ),
    capabilities=CapabilityAssessments(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN),
    capacity_bindings=(CapacityScopeRef("openai", "codex"),), reasoning_effort="high",
)
CATALOG = ModelCatalog(1, "2026-10-26", (ENTRY,))
PROFILES = TaskProfileCatalog(())
POLICY_VERSION = 1
SUBJECT = Model(
    "owned-id", ENTRY.identity.provider, ENTRY.identity.model,
    revision="owned-revision", channel="owned-channel", interface_id="owned-api",
    interface_provider_id="owned-provider", interface_version="1",
    configuration=(("reasoning_effort", ENTRY.reasoning_effort), ("variant", ENTRY.identity.variant)),
)
SCOPE = Applicability("owned-scope", SUBJECT.channel, SUBJECT.configuration, frozenset({"owned-source"}))
BINDING = ModelBinding(ENTRY.identity, SUBJECT, "owned.input", "input-allowance", "owned-basis")
LIMIT = NativeLimit("owned.input", "input-allowance", Decimal(4096), "tokens", "owned-basis", "advertised")


def public() -> PublicEvidence:
    observation = Observation(
        "owned-observation", "owned-source", "official", "documentation",
        "https://example.invalid/synthetic", T - DAY, "public", "owned-synthetic",
        observed_at=T - DAY, fresh_until=T + DAY,
    )
    claim = ModelClaim(SUBJECT, LIMIT, claim_id="owned-claim", observation_id=observation.observation_id, effective_from=T - DAY)
    return PublicEvidence(SCOPE, Evidence((observation,), (claim,)), ((observation.observation_id, "owned-revision"),))


def encoded(value: PublicEvidence) -> tuple[bytes, CutReference]:
    return encode_public_evidence(value, produced_at=T - timedelta(seconds=1), payload_schema=NATIVE_PAYLOAD_SCHEMA)


def consumer(value: PublicEvidence | None = None) -> MIConsumer:
    result = MIConsumer(expected_scope=SCOPE, bindings=(BINDING,), max_bytes=BOUND)
    if value is not None:
        result.adopt(*encoded(value))
    return result


def collectors() -> StatusCollectors:
    def snapshot(provider: str, retrieved_at: str) -> CapacitySnapshot:
        return CapacitySnapshot(
            schema_version=3, provider=provider, source="synthetic_test", retrieved_at=retrieved_at,
            status="ok", diagnostics=(), windows=tuple(
                CapacityWindow(
                    resource="tokens", kind=kind, scope_id="codex" if provider == "openai" else "coding_plan",
                    remaining_percent=80, used_percent=20, duration_seconds=seconds,
                )
                for kind, seconds in (("five_hour", 18000), ("weekly", 604800))
            ),
        )
    def openai(*, retrieved_at: str) -> OpenAICodexObservation:
        return paired_observation(snapshot("openai", retrieved_at))

    def zai(*, retrieved_at: str) -> CapacitySnapshot:
        return snapshot("zai", retrieved_at)

    return StatusCollectors(openai=openai, zai=zai)


class MIConsumerTests(unittest.TestCase):
    def test_runtime_inventory_preserves_the_exact_mi_v1_receipt(self) -> None:
        owner = consumer(public())
        decision = select_from_inputs(
            catalog=replace(CATALOG, catalog_version=None, updated_on=None),
            profiles=PROFILES, profile_policy_version=POLICY_VERSION,
            profile_id=None,
            requirement=TaskRequirement.from_dict({"task_level": "L0", "capability_minima": {}, "hard_constraints": {}}),
            tightening=None, policy=None, collectors=collectors(), clock=lambda: T,
            mi_consumer=owner,
        )
        assert decision.mi_use is not None and decision.mi_use.cut is not None
        envelope = selection_envelope(decision)
        self.assertEqual(2, envelope["schema_version"])
        receipt = cast(dict[str, object], envelope["mi_receipt"])
        self.assertEqual(1, receipt["schema_version"])
        self.assertEqual(envelope["decision"], receipt["decision"])
        verified = decode_decision_utilization(
            decision.mi_use.utilization, response=canonical(receipt),
            expected_cut=decision.mi_use.cut.reference, evaluated_at=T,
            decision=decision.mi_use.decision, context=decision.mi_use.context, max_bytes=BOUND,
        )
        self.assertEqual(decision.mi_use.cut.reference, verified.cut)

    def decision(self, owner: MIConsumer | None, *, at: datetime = T, dependencies: StatusCollectors | None = None, admission: MIAdmission | None = None, requirement: TaskRequirement | None = None):
        return select_from_inputs(
            catalog=CATALOG, profiles=PROFILES, profile_policy_version=POLICY_VERSION,
            profile_id=None, requirement=requirement or TaskRequirement.from_dict({"task_level": "L0", "capability_minima": {}, "hard_constraints": {}}),
            tightening=None, policy=None, collectors=dependencies or collectors(), clock=lambda: at,
            mi_consumer=owner,
            mi_admission=admission,
        )

    def label(self, value: PublicEvidence, *, at: datetime = T) -> str:
        decision = self.decision(consumer(value), at=at)
        self.assertIsNotNone(decision.selected)
        assert decision.selected is not None
        return decision.selected.display_name

    def test_real_decision_consumes_public_fact_before_selector_and_emits_binding(self) -> None:
        owner = consumer(public())
        decision = self.decision(owner)
        assert decision.selected is not None and decision.mi_use is not None
        self.assertIn("public input 4096 tokens", decision.selected.display_name)
        receipt = decision.mi_use
        self.assertIsNotNone(receipt.cut)
        assert receipt.cut is not None
        verified = decode_decision_utilization(
            receipt.utilization, response=canonical(selection_envelope(decision)),
            expected_cut=receipt.cut.reference, evaluated_at=T,
            decision=receipt.decision, context=receipt.context, max_bytes=BOUND,
        )
        self.assertEqual(receipt.cut.reference, verified.cut)
        self.assertEqual(hashlib.sha256(receipt.decision).hexdigest(), verified.decision_sha256)
        context = cast(dict[str, object], json.loads(receipt.context))
        self.assertEqual("explanation-only/1", context["mode"])
        self.assertIn(b"4096 tokens", receipt.context)
        self.assertEqual(T, verified.evaluated_at)

    def test_mid_collection_refresh_cannot_relabel_old_decision(self) -> None:
        original = public()
        owner = consumer(original)
        old = owner.capture()[0]
        claim = original.evidence.claims[0]
        assert isinstance(claim, ModelClaim)
        update = replace(original, evidence=original.evidence.extend(claims=(
            replace(claim, claim_id="new", revises=claim.claim_id, payload=replace(LIMIT, amount=Decimal(8192)), effective_from=T),
        )))
        dependencies = collectors()

        def refresh(*, retrieved_at: str):
            owner.adopt(*encoded(update))
            return dependencies.openai(retrieved_at=retrieved_at)

        first = self.decision(owner, dependencies=replace(dependencies, openai=refresh))
        second = self.decision(owner)
        assert first.mi_use is not None and second.mi_use is not None
        assert first.selected is not None and second.selected is not None
        self.assertEqual(old, first.mi_use.cut)
        self.assertNotEqual(first.mi_use.cut, second.mi_use.cut)
        self.assertIn("4096", first.selected.display_name)
        self.assertIn("8192", second.selected.display_name)
        self.assertNotEqual(first.mi_use.reference, second.mi_use.reference)
        _ = selection_envelope(first)

    def test_default_and_empty_consumer_preserve_bootstrap(self) -> None:
        baseline = self.decision(None)
        empty = self.decision(consumer())
        self.assertEqual(baseline.to_dict(), empty.to_dict())
        self.assertNotIn("mi_utilization", selection_envelope(baseline))
        self.assertIsNone(baseline.mi_use)
        assert empty.mi_use is not None
        self.assertIsNone(empty.mi_use.cut)

    def test_public_evidence_never_changes_ratings_limits_capacity_or_identity(self) -> None:
        before = CATALOG.to_dict()
        owner = consumer(public())
        explained = explained_catalog(CATALOG, owner.capture(), T)
        changed = explained.entries[0]
        self.assertEqual(ENTRY, replace(changed, display_name=ENTRY.display_name))
        baseline, annotated = self.decision(None), self.decision(owner)
        assert baseline.selected is not None and annotated.selected is not None
        self.assertEqual(baseline.selected.identity, annotated.selected.identity)
        self.assertEqual(baseline.selected.scarcity_assessment, annotated.selected.scarcity_assessment)
        self.assertEqual(before, CATALOG.to_dict())

    def test_stale_unknown_and_future_have_distinct_dispositions(self) -> None:
        initial = public()
        observation = initial.evidence.observations[0]
        for changed, label in (
            (replace(observation, fresh_until=T), "stale"),
            (replace(observation, fresh_until=None), "freshness unknown"),
            (replace(observation, retrieved_at=T + DAY), "future"),
            (replace(observation, observed_at=T + DAY), "future"),
            (replace(observation, distribution="unknown"), "rights unknown"),
        ):
            with self.subTest(label=label):
                self.assertIn(f"MI {label}", self.label(replace(initial, evidence=replace(initial.evidence, observations=(changed,)))))
        self.assertIn("MI stale", self.label(initial, at=T + DAY))

    def test_explicit_conflict_has_no_latest_or_lexical_winner(self) -> None:
        initial = public()
        claim = initial.evidence.claims[0]
        assert isinstance(claim, ModelClaim)
        conflict = replace(claim, claim_id="z-disagreement", payload=replace(LIMIT, amount=Decimal(12)))
        self.assertIn("MI conflict", self.label(replace(initial, evidence=initial.evidence.extend(claims=(conflict,)))))

    def test_revocation_is_retained_even_when_notice_observation_stale(self) -> None:
        initial = public()
        for critical in (True, False, None):
            revoked = replace(initial, notices=(SourceNotice(
                "owned-notice", "owned-observation", frozenset({"statement:owned-claim"}), T, "revocation", critical,
            ),))
            self.assertIn("MI critical withdrawal" if critical is True else "MI withdrawn", self.label(revoked, at=T + DAY))
            self.assertIn("4096", self.label(replace(revoked, notices=(replace(revoked.notices[0], effective_from=T + DAY),))))

    def test_exact_subject_configuration_channel_and_effort_are_not_wildcards(self) -> None:
        initial = public()
        for subject in (
            replace(SUBJECT, channel="other"), replace(SUBJECT, revision="other"),
            replace(SUBJECT, physical_model_id="other"), replace(SUBJECT, interface_version="2"),
            replace(SUBJECT, provider_model_id="other"), replace(SUBJECT, configuration=None),
            replace(SUBJECT, configuration=()), replace(SUBJECT, configuration=(("reasoning_effort", None),)),
        ):
            claim = initial.evidence.claims[0]
            assert isinstance(claim, ModelClaim)
            changed = replace(initial, evidence=replace(initial.evidence, claims=(replace(claim, subject=subject),)))
            with self.subTest(subject=subject):
                self.assertIn("MI unknown", self.label(changed))

    def test_only_admissible_retractions_restore_withdrawn_public_facts(self) -> None:
        initial = public()
        original = initial.evidence.observations[0]
        report = replace(original, observation_id="restoration-report")
        revocation = SourceNotice(
            "withdrawn", original.observation_id, frozenset({"statement:owned-claim"}),
            T - timedelta(hours=2), "revocation", True,
        )
        retraction = SourceNotice(
            "restored", report.observation_id, revocation.targets,
            T - timedelta(hours=1), "retraction", None, revocation.notice_id,
        )

        def publication(observation: Observation) -> PublicEvidence:
            return replace(
                initial,
                evidence=initial.evidence.extend(observations=(observation,)),
                revisions=(*initial.revisions, (observation.observation_id, "owned-revision")),
                notices=(revocation, retraction),
            )

        self.assertIn("public input 4096 tokens", self.label(publication(report)))
        for inadmissible in (
            replace(report, retrieved_at=T + DAY),
            replace(report, observed_at=T + DAY),
            replace(report, published_at=T + DAY),
            replace(report, fresh_until=T),
            replace(report, fresh_until=None),
            replace(report, distribution="unknown"),
        ):
            with self.subTest(report=inadmissible):
                self.assertIn("MI critical withdrawal", self.label(publication(inadmissible)))

        # A pending correction does not rewrite earlier decisions or history.
        future = replace(publication(report), notices=(revocation, replace(retraction, effective_from=T + DAY)))
        owner = consumer(future)
        before = self.decision(owner)
        self.assertIn("MI critical withdrawal", self.label(future))
        assert before.mi_use is not None and before.mi_use.cut is not None
        self.assertEqual(future.notices, before.mi_use.cut.public.notices)
        _ = selection_envelope(before)

    def test_unsupported_native_semantics_never_convert_to_positive_fact(self) -> None:
        initial = public()
        claim = initial.evidence.claims[0]
        assert isinstance(claim, ModelClaim)
        for value in (
            replace(LIMIT, amount=Decimal("1.5")),
            replace(LIMIT, amount=Decimal("1e1000000")), replace(LIMIT, amount=Decimal(0)),
            replace(LIMIT, unit="characters"),
        ):
            self.assertIn("MI unsupported", self.label(replace(initial, evidence=replace(initial.evidence, claims=(replace(claim, payload=value),)))))
        self.assertIn("MI unsupported", self.label(replace(initial, evidence=replace(initial.evidence, claims=(replace(claim, payload=replace(LIMIT, meaning="total-context-window")),)))))
        for value in (replace(LIMIT, amount=None), replace(LIMIT, unit=None), replace(LIMIT, assertion="unknown"), replace(LIMIT, basis=None)):
            self.assertIn("MI unknown]", self.label(replace(initial, evidence=replace(initial.evidence, claims=(replace(claim, payload=value),)))))

    def test_future_only_current_future_successor_and_absent_are_distinct(self) -> None:
        initial = public()
        claim = initial.evidence.claims[0]
        assert isinstance(claim, ModelClaim)
        future_only = replace(initial, evidence=replace(initial.evidence, claims=(replace(claim, effective_from=T + DAY),)))
        self.assertIn("MI future]", self.label(future_only))
        self.assertNotIn("4096", self.label(future_only))
        absent = replace(initial, evidence=replace(initial.evidence, claims=()))
        self.assertIn("MI unknown]", self.label(absent))
        updated = replace(initial, evidence=initial.evidence.extend(claims=(
            replace(claim, claim_id="future", revises=claim.claim_id, effective_from=T + DAY, payload=replace(LIMIT, amount=Decimal(8192))),
        )))
        self.assertIn("public input 4096 tokens", self.label(updated))
        self.assertNotIn("8192", self.label(updated))

    def test_rejected_updates_leave_existing_cut_and_receipt_unchanged(self) -> None:
        initial = public()
        owner = consumer(initial)
        original = owner.capture()
        decision = self.decision(owner)
        before = canonical(selection_envelope(decision))
        data, ref = encoded(initial)
        changed_scope = replace(initial, applicability=replace(SCOPE, scope_id="other"))
        for invalid, expected in (
            (b"private-untrusted-contents", ref), (data + b" ", ref),
            (data, replace(ref, format_version=2)),
            (data, replace(ref, payload_schema="unsupported")),
            encoded(changed_scope),
            encoded(replace(initial, evidence=Evidence(initial.evidence.observations), revisions=initial.revisions)),
        ):
            with self.assertRaisesRegex(ValueError, "^mi_publication_rejected$"):
                owner.adopt(invalid, expected)
            self.assertEqual(original, owner.capture())
            self.assertEqual(before, canonical(selection_envelope(decision)))

    def test_members_sources_and_payload_are_validated_not_just_digest(self) -> None:
        data, ref = encoded(public())
        frame = decode_cut(data, ref, max_bytes=BOUND, supported_payload_schemas=frozenset({NATIVE_PAYLOAD_SCHEMA}))
        for members, sources in (((), frame.sources), (frame.members, ())):
            invalid, expected = encode_cut(
                frame.payload, payload_schema=NATIVE_PAYLOAD_SCHEMA, produced_at=T,
                scope=SCOPE.scope_id, members=members, sources=sources,
            )
            with self.assertRaisesRegex(ValueError, "mi_publication_rejected"):
                consumer().adopt(invalid, expected)

    def test_history_identity_rewrite_or_loss_is_rejected(self) -> None:
        initial = public()
        owner = consumer(initial)
        for changed in (
            replace(initial, revisions=(("owned-observation", "rewritten"),)),
            replace(initial, evidence=replace(initial.evidence, observations=(replace(initial.evidence.observations[0], fresh_until=T + 2 * DAY),))),
        ):
            with self.assertRaisesRegex(ValueError, "mi_publication_rejected"):
                owner.adopt(*encoded(changed))

    def test_bound_file_read_and_safe_missing_file_failure(self) -> None:
        owner = consumer(public())
        original = owner.capture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned.json"
            with self.assertRaisesRegex(ValueError, "^mi_publication_unavailable$"):
                owner.load(path, encoded(public())[1])
            _ = path.write_bytes(b"x" * (BOUND + 1))
            with self.assertRaisesRegex(ValueError, "^mi_publication_rejected$"):
                owner.load(path, encoded(public())[1])
        self.assertEqual(original, owner.capture())

    def test_utilization_refuses_changed_decision_context_manifest_and_envelope(self) -> None:
        decision = self.decision(consumer(public()))
        use = decision.mi_use
        assert use is not None
        with self.assertRaises(ValueError):
            _ = replace(use, context=use.context + b" ").envelope(decision.to_dict())
        with self.assertRaises(ValueError):
            _ = replace(use, utilization=use.utilization + b" ").envelope(decision.to_dict())
        with self.assertRaises(ValueError):
            _ = use.envelope({**decision.to_dict(), "catalog_version": 999})
        other = self.decision(consumer(public()), at=T + timedelta(microseconds=1))
        self.assertEqual(decision.to_dict(), other.to_dict())
        assert other.mi_use is not None
        self.assertNotEqual(use.reference, other.mi_use.reference)


if __name__ == "__main__":
    _ = unittest.main()
