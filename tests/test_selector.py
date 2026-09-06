"""M2e deterministic selector tests (D-027).

These tests pin the frozen ``balanced`` selection semantics over the real
M2c catalog and synthetic v3 capacity: the required selection scenarios,
the exact ranking order (scarcity before margin, margin before preference,
preference before stable identity, input-order independence), every hard
constraint individually with tri-state semantics, the monotone
``tighten_requirement`` rules, structured no-solution results and the
explicit input validation. Deterministic and self-contained: no network,
subprocess, clock or credential access.

Deliberately ill-typed values in negative tests pass through ``cast()`` to
the declared parameter type: the static annotation cannot express "wrong
type on purpose", and the runtime validator must be the one to reject them.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scarcity_router import (
    CapabilityAssessment,
    CapabilityAssessments,
    CapacityDiagnostic,
    CapacityScopeRef,
    CapacitySnapshot,
    CapacityWindow,
    EvidenceRef,
    HardConstraintFailure,
    HardConstraints,
    HumanOverride,
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
    ModelRef,
    ReplenishmentState,
    ReservationRule,
    SelectionContractValidationError,
    SelectionDecision,
    SelectorPolicy,
    TaskRequirement,
    UserPolicy,
    assess_scarcity,
    capability_margin,
    evaluate_capability_sufficiency,
    evaluate_hard_constraints,
    neutral_selector_policy,
    select_model,
    tighten_requirement,
)
from scarcity_router.policy import AvailabilityTarget, WeeklyBlackoutRule
from scarcity_router.selector import CandidateEvaluation
from scarcity_router.selection_types import CapabilityMinima, TaskProfileCatalog

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"
POLICY_PATH = REPO / "model-policy.json"

FIXED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
RETRIEVED_AT = "2026-09-06T12:00:00.000Z"


def _ill(value: object) -> object:
    """Mark a deliberately wrong-typed value for a negative-validation test."""
    return value


def _load_catalog() -> ModelCatalog:
    return ModelCatalog.from_dict(
        cast(object, json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
    )


def _load_profiles() -> TaskProfileCatalog:
    document = cast(
        "dict[str, object]",
        json.loads(POLICY_PATH.read_text(encoding="utf-8")),
    )
    raw_profiles = cast("list[object]", document["task_profiles"])
    definitions: list[dict[str, object]] = []
    for item in raw_profiles:
        entry = cast("dict[str, object]", item)
        definitions.append(
            {
                "profile_id": entry["id"],
                "requirement": entry["calibrated_requirement"],
            }
        )
    return TaskProfileCatalog.from_dict({"definitions": definitions})


CATALOG = _load_catalog()
PROFILES = _load_profiles()
BY_MODEL = {
    (entry.identity.provider, entry.identity.model): entry for entry in CATALOG.entries
}
LUNA = BY_MODEL[("openai", "gpt-5.6-luna")]
SOL = BY_MODEL[("openai", "gpt-5.6-sol")]
GLM53 = BY_MODEL[("zai", "glm-5.3")]
FLASH = BY_MODEL[("zai", "glm-5.3-flash")]


def _snap(
    provider: str,
    five: int | None = 50,
    weekly: int | None = 50,
    *,
    status: str = "ok",
) -> CapacitySnapshot:
    """Synthetic v3 snapshot with the evidenced provider scope identity."""
    scope = "codex" if provider == "openai" else "coding_plan"
    windows: list[CapacityWindow] = []
    if five is not None:
        windows.append(
            CapacityWindow(
                resource="tokens",
                kind="five_hour",
                scope_id=scope,
                duration_seconds=18_000,
                used_percent=100 - five,
                remaining_percent=five,
                window_id=f"{provider}-five",
            )
        )
    if weekly is not None:
        windows.append(
            CapacityWindow(
                resource="tokens",
                kind="weekly",
                scope_id=scope,
                duration_seconds=604_800,
                used_percent=100 - weekly,
                remaining_percent=weekly,
                window_id=f"{provider}-weekly",
            )
        )
    diagnostics: tuple[CapacityDiagnostic, ...] = ()
    if status != "ok":
        diagnostics = (CapacityDiagnostic(code="telemetry_unknown"),)
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=RETRIEVED_AT,
        status=status,
        windows=tuple(windows),
        diagnostics=diagnostics,
    )


def _select(
    requirement: TaskRequirement,
    snapshots: list[CapacitySnapshot],
    *,
    policy: SelectorPolicy | None = None,
    catalog: ModelCatalog | None = None,
    states: tuple[ReplenishmentState, ...] = (),
) -> SelectionDecision:
    return select_model(
        catalog=CATALOG if catalog is None else catalog,
        requirement=requirement,
        policy=policy if policy is not None else neutral_selector_policy(),
        snapshots=snapshots,
        evaluated_at=FIXED_AT,
        replenishment_states=states,
    )


def _excluded_by_identity(
    decision: SelectionDecision,
) -> dict[tuple[str, str, str], CandidateEvaluation]:
    return {
        (c.identity.provider, c.identity.model, c.identity.variant): c
        for c in decision.excluded
    }


def _unknown_capabilities() -> CapabilityAssessments:
    unknown = CapabilityAssessment(rating=None)
    return CapabilityAssessments(
        reasoning=unknown,
        coding=unknown,
        scientific_methodological=unknown,
        writing_editorial=unknown,
        tool_use=unknown,
        translation_multilingual=unknown,
    )


def _synthetic_entry(
    provider: str,
    model: str,
    variant: str,
    *,
    hard_properties: ModelHardProperties | None = None,
    capabilities: CapabilityAssessments | None = None,
    bindings: tuple[CapacityScopeRef, ...] | None = None,
) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        identity=ModelIdentity(provider=provider, model=model, variant=variant),
        display_name=f"Synthetic {model}",
        hard_properties=(
            ModelHardProperties() if hard_properties is None else hard_properties
        ),
        capabilities=(
            _unknown_capabilities() if capabilities is None else capabilities
        ),
        capacity_bindings=(
            (CapacityScopeRef(provider="openai", scope_id="codex"),)
            if bindings is None
            else bindings
        ),
    )


class ScenarioTests(unittest.TestCase):
    """The required M2e selector scenarios over the real M2c catalog."""

    def test_scenario_1_routine_coding_least_scarce(self) -> None:
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
        )
        assert decision.selected is not None
        self.assertEqual(
            ("zai", "glm-5.3-flash", "max"),
            (
                decision.selected.identity.provider,
                decision.selected.identity.model,
                decision.selected.identity.variant,
            ),
        )
        self.assertEqual(5, decision.selected.capability_margin)
        self.assertEqual(("selected_balanced",), decision.reason_codes)
        # Exact ranking: Z.ai less scarce, then smaller adequate margin.
        self.assertEqual(
            ["glm-5.3", "gpt-5.6-luna", "gpt-5.6-sol"],
            [c.identity.model for c in decision.alternatives],
        )

    def test_scenario_2_deep_coding_uses_glm_when_zai_less_scarce(self) -> None:
        decision = _select(
            PROFILES.resolve("deep_coding"),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
        )
        assert decision.selected is not None
        self.assertEqual("glm-5.3", decision.selected.identity.model)

    def test_scenario_3_deep_coding_uses_sol_when_openai_less_scarce(self) -> None:
        decision = _select(
            PROFILES.resolve("deep_coding"),
            [_snap("openai", 80, 80), _snap("zai", 40, 40)],
        )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-sol", decision.selected.identity.model)

    def test_scenario_4_famous_zai_98_2_case(self) -> None:
        decision = _select(
            PROFILES.resolve("deep_coding"),
            [_snap("openai", 40, 40), _snap("zai", 98, 2)],
        )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-sol", decision.selected.identity.model)
        # GLM-5.3 stays eligible (2% is nonzero) with the critical 9604
        # penalty and ranks behind Sol's 3600.
        self.assertEqual(
            ["glm-5.3"], [c.identity.model for c in decision.alternatives]
        )
        glm = decision.alternatives[0]
        assert glm.scarcity_assessment is not None
        self.assertEqual("known", glm.scarcity_assessment.state)
        self.assertEqual("critical", glm.scarcity_assessment.label)
        self.assertEqual(9604, glm.scarcity_assessment.penalty_units)
        excluded = _excluded_by_identity(decision)
        luna = excluded[("openai", "gpt-5.6-luna", "max")]
        flash = excluded[("zai", "glm-5.3-flash", "max")]
        self.assertEqual("capability", luna.exclusion_stage)
        self.assertEqual("capability", flash.exclusion_stage)

    def test_scenario_5_scientific_review_selects_sol_only(self) -> None:
        decision = _select(
            PROFILES.resolve("scientific_review"),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
        )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-sol", decision.selected.identity.model)
        self.assertEqual((), decision.alternatives)
        self.assertEqual(3, len(decision.excluded))
        self.assertTrue(
            all(c.exclusion_stage == "capability" for c in decision.excluded)
        )

    def test_scenario_6_translation_selects_sol_only_without_relaxation(self) -> None:
        requirement = PROFILES.resolve("translation")
        decision = _select(
            requirement, [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-sol", decision.selected.identity.model)
        self.assertEqual(requirement, decision.requirement)

    def test_scenario_7_editorial_prefers_smaller_margin_luna(self) -> None:
        decision = _select(
            PROFILES.resolve("editorial"),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
        )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-luna", decision.selected.identity.model)
        self.assertEqual(1, decision.selected.capability_margin)
        assert decision.alternatives
        self.assertEqual(2, decision.alternatives[0].capability_margin)

    def test_scenario_8_orchestration_prefers_smaller_margin_luna(self) -> None:
        decision = _select(
            PROFILES.resolve("orchestration"),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
        )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-luna", decision.selected.identity.model)

    def test_scenario_9_shared_quota_equality(self) -> None:
        snapshots = [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        self.assertEqual(
            assess_scarcity(LUNA, snapshots), assess_scarcity(SOL, snapshots)
        )
        self.assertEqual(
            assess_scarcity(GLM53, snapshots), assess_scarcity(FLASH, snapshots)
        )
        self.assertNotEqual(
            assess_scarcity(LUNA, snapshots), assess_scarcity(GLM53, snapshots)
        )

    def test_scenario_10_known_capacity_ranks_ahead_of_degraded_unknown(self) -> None:
        # Luna known 1% (critical, penalty 9801) vs degraded unknown Z.ai.
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [_snap("openai", 1, 1), _snap("zai", None, None, status="unknown")],
        )
        assert decision.selected is not None
        self.assertFalse(decision.degraded)
        self.assertEqual("gpt-5.6-luna", decision.selected.identity.model)
        assert decision.selected.scarcity_assessment is not None
        self.assertEqual(9801, decision.selected.scarcity_assessment.penalty_units)
        assert decision.alternatives
        self.assertTrue(any(c.degraded for c in decision.alternatives))

    def test_scenario_11_only_unknown_sufficient_candidate_degraded_selects(
        self,
    ) -> None:
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [
                _snap("openai", None, None, status="unknown"),
                _snap("zai", None, None, status="unknown"),
            ],
        )
        assert decision.selected is not None
        self.assertTrue(decision.degraded)
        self.assertTrue(decision.selected.degraded)
        self.assertEqual(
            ("selected_balanced", "selected_degraded_capacity"),
            decision.reason_codes,
        )
        self.assertEqual("gpt-5.6-luna", decision.selected.identity.model)
        # No fake percentage exists for the degraded selection.
        assert decision.selected.scarcity_assessment is not None
        self.assertIsNone(
            decision.selected.scarcity_assessment.effective_remaining_percent
        )

    def test_scenario_12_only_unknown_sufficient_candidate_strict_blocks(self) -> None:
        policy = SelectorPolicy(
            mode="balanced",
            resource_policy=UserPolicy(
                policy_version=1,
                unknown_capacity_mode="strict",
                replenishment_mode="advisory",
                reservations=(),
                blackouts=(),
            ),
        )
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [
                _snap("openai", None, None, status="unknown"),
                _snap("zai", None, None, status="unknown"),
            ],
            policy=policy,
        )
        self.assertIsNone(decision.selected)
        self.assertEqual(("no_eligible_candidate",), decision.reason_codes)
        self.assertEqual(4, len(decision.excluded))
        self.assertTrue(
            all(
                c.exclusion_stage == "capacity"
                and c.reason_codes == ("capacity_unknown_blocked",)
                for c in decision.excluded
            )
        )

    def _reservation_policy(self) -> SelectorPolicy:
        return SelectorPolicy(
            mode="balanced",
            resource_policy=UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="advisory",
                reservations=(
                    ReservationRule(
                        rule_id="protect-zai-weekly",
                        scope=CapacityScopeRef(provider="zai", scope_id="coding_plan"),
                        resource="tokens",
                        kind="weekly",
                        when_remaining_below=20,
                        minimum_task_level="L3",
                    ),
                ),
                blackouts=(),
            ),
        )

    def test_scenario_13_reservation_blocks_routine(self) -> None:
        policy = self._reservation_policy()
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [_snap("openai", 40, 40), _snap("zai", 98, 2)],
            policy=policy,
        )
        excluded = _excluded_by_identity(decision)
        for key in (("zai", "glm-5.3", "max"), ("zai", "glm-5.3-flash", "max")):
            candidate = excluded[key]
            self.assertEqual("reservation", candidate.exclusion_stage)
            self.assertEqual(("reservation_blocked",), candidate.reason_codes)
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-luna", decision.selected.identity.model)

    def test_scenario_14_reservation_permits_high_task(self) -> None:
        policy = self._reservation_policy()
        # Z.ai weekly 15 triggers the rule (< 20) but L3 is permitted; OpenAI
        # is nearly exhausted so the permitted GLM-5.3 wins normally.
        decision = _select(
            PROFILES.resolve("deep_coding"),
            [_snap("openai", 2, 2), _snap("zai", 98, 15)],
            policy=policy,
        )
        assert decision.selected is not None
        self.assertEqual("glm-5.3", decision.selected.identity.model)
        decisions = decision.selected.reservation_decisions
        self.assertEqual(1, len(decisions))
        self.assertEqual("known", decisions[0].state)
        assert decisions[0].triggered is True and decisions[0].blocked is False
        self.assertIn(
            "reservation_permitted_by_task_level", decisions[0].reason_codes
        )

    def test_scenario_15_reservation_unknown_fails_closed(self) -> None:
        policy = self._reservation_policy()
        # The reservation targets zai/coding_plan weekly, but the snapshot
        # only carries a five-hour window: the trigger state is unknown and
        # the candidate fails closed.
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [_snap("openai", 40, 40), _snap("zai", 98, None)],
            policy=policy,
        )
        excluded = _excluded_by_identity(decision)
        for key in (("zai", "glm-5.3", "max"), ("zai", "glm-5.3-flash", "max")):
            candidate = excluded[key]
            self.assertEqual("reservation", candidate.exclusion_stage)
            self.assertEqual(("reservation_unknown",), candidate.reason_codes)
            decision_unknown = candidate.reservation_decisions[0]
            self.assertEqual("unknown", decision_unknown.state)
            self.assertIn(
                "reservation_window_missing", decision_unknown.reason_codes
            )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-luna", decision.selected.identity.model)

    def test_scenario_16_blackout_hard_policy_exclusion(self) -> None:
        policy = SelectorPolicy(
            mode="balanced",
            resource_policy=UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="advisory",
                reservations=(),
                blackouts=(
                    WeeklyBlackoutRule(
                        rule_id="hold-zai",
                        target=AvailabilityTarget(provider="zai"),
                        timezone="UTC",
                        weekdays=("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
                        start_local="00:00",
                        end_local="23:59",
                        reason_code="peak_hold",
                    ),
                ),
            ),
        )
        snapshots = [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        snapshot_payloads = [s.to_dict() for s in snapshots]
        decision = _select(
            PROFILES.resolve("routine_coding"), snapshots, policy=policy
        )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-luna", decision.selected.identity.model)
        excluded = _excluded_by_identity(decision)
        for key in (("zai", "glm-5.3", "max"), ("zai", "glm-5.3-flash", "max")):
            candidate = excluded[key]
            self.assertEqual("policy_blackout", candidate.exclusion_stage)
            self.assertEqual(("policy_blocked",), candidate.reason_codes)
            assert candidate.blackout_decision is not None
            self.assertEqual("hold-zai", candidate.blackout_decision.rule_id)
        # Capacity telemetry is never rewritten by policy.
        self.assertEqual(snapshot_payloads, [s.to_dict() for s in snapshots])

    def test_scenario_17_recoverable_exhausted_sol(self) -> None:
        policy = SelectorPolicy(
            mode="balanced",
            resource_policy=UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="recoverable",
                reservations=(),
                blackouts=(),
            ),
        )
        states = (
            ReplenishmentState(
                provider="openai",
                kind="rate_limit_reset",
                available_count=2,
                details_known=True,
                earliest_expiry=None,
                retrieved_at=RETRIEVED_AT,
            ),
        )
        decision = _select(
            PROFILES.resolve("scientific_review"),
            [_snap("openai", 98, 0), _snap("zai", 80, 80)],
            policy=policy,
            states=states,
        )
        self.assertIsNone(decision.selected)
        self.assertEqual(("no_eligible_candidate",), decision.reason_codes)
        self.assertEqual(1, len(decision.recoverable_candidates))
        recoverable = decision.recoverable_candidates[0]
        self.assertEqual("gpt-5.6-sol", recoverable.identity.model)
        self.assertEqual("capacity", recoverable.exclusion_stage)
        self.assertIn("replenishment_recoverable", recoverable.reason_codes)
        evaluations = recoverable.replenishment_evaluations
        self.assertEqual(1, len(evaluations))
        self.assertTrue(evaluations[0].recoverable)
        self.assertTrue(evaluations[0].human_action_required)

    def test_scenario_18_vision_hard_requirement_excludes_glm(self) -> None:
        explicit = TaskRequirement(
            task_level="L3",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(requires_vision=True),
        )
        requirement = tighten_requirement(PROFILES.resolve("deep_coding"), explicit)
        decision = _select(
            requirement, [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        )
        assert decision.selected is not None
        self.assertEqual("gpt-5.6-sol", decision.selected.identity.model)
        excluded = _excluded_by_identity(decision)
        glm = excluded[("zai", "glm-5.3", "max")]
        self.assertEqual("hard_constraint", glm.exclusion_stage)
        self.assertEqual(("hard_constraint_failed",), glm.reason_codes)
        failures = glm.hard_constraint_failures
        self.assertEqual(1, len(failures))
        self.assertEqual("requires_vision", failures[0].constraint)
        self.assertEqual("unsupported", failures[0].reason)

    def test_scenario_19_output_over_128k_no_solution(self) -> None:
        explicit = TaskRequirement(
            task_level="L3",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(minimum_output_tokens=128_001),
        )
        requirement = tighten_requirement(PROFILES.resolve("deep_coding"), explicit)
        decision = _select(
            requirement, [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        )
        self.assertIsNone(decision.selected)
        self.assertEqual((), decision.alternatives)
        self.assertEqual(4, len(decision.excluded))
        self.assertEqual(("no_eligible_candidate",), decision.reason_codes)
        self.assertTrue(
            all(c.exclusion_stage == "hard_constraint" for c in decision.excluded)
        )
        self.assertEqual(3, len(decision.closest_candidates))

    def test_scenario_20_required_provider_creates_no_solution(self) -> None:
        explicit = TaskRequirement(
            task_level="L4",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(required_provider="zai"),
        )
        requirement = tighten_requirement(
            PROFILES.resolve("scientific_review"), explicit
        )
        decision = _select(
            requirement, [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        )
        self.assertIsNone(decision.selected)
        excluded = _excluded_by_identity(decision)
        self.assertEqual(
            "hard_constraint",
            excluded[("openai", "gpt-5.6-sol", "high")].exclusion_stage,
        )
        self.assertEqual(
            "capability", excluded[("zai", "glm-5.3", "max")].exclusion_stage
        )

    def test_scenario_21_required_model_selects_glm(self) -> None:
        explicit = TaskRequirement(
            task_level="L3",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(
                required_model=ModelRef(provider="zai", model="glm-5.3")
            ),
        )
        requirement = tighten_requirement(PROFILES.resolve("deep_coding"), explicit)
        decision = _select(
            requirement, [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        )
        assert decision.selected is not None
        self.assertEqual("glm-5.3", decision.selected.identity.model)

    def test_scenario_22_privacy_constraint_fails_honestly(self) -> None:
        explicit = TaskRequirement(
            task_level="L1",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(privacy_constraint="no_training"),
        )
        requirement = tighten_requirement(PROFILES.resolve("routine_coding"), explicit)
        decision = _select(
            requirement, [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        )
        self.assertIsNone(decision.selected)
        self.assertEqual(4, len(decision.excluded))
        for candidate in decision.excluded:
            self.assertEqual("hard_constraint", candidate.exclusion_stage)
            failure = candidate.hard_constraint_failures[0]
            self.assertEqual("privacy_constraint", failure.constraint)
            self.assertEqual("privacy_unknown", failure.reason)

    def test_scenario_23_unknown_required_capability_fails(self) -> None:
        entry = _synthetic_entry(
            "openai",
            "synthetic-unknown",
            "max",
            capabilities=_unknown_capabilities(),
        )
        catalog = ModelCatalog(
            catalog_version=1, updated_on="2026-09-06", entries=(entry,)
        )
        requirement = TaskRequirement(
            task_level="L1",
            capability_minima=CapabilityMinima(coding=3),
            hard_constraints=HardConstraints(),
        )
        decision = select_model(
            catalog=catalog,
            requirement=requirement,
            policy=neutral_selector_policy(),
            snapshots=[_snap("openai", 50, 50)],
            evaluated_at=FIXED_AT,
        )
        self.assertIsNone(decision.selected)
        failures = decision.excluded[0].capability_failures
        self.assertEqual(1, len(failures))
        self.assertEqual("coding", failures[0].dimension)
        self.assertEqual("unknown", failures[0].reason)

    def test_scenario_24_human_override_effective_rating(self) -> None:
        def rated_capabilities(
            coding: int, override: HumanOverride | None
        ) -> CapabilityAssessments:
            coding_assessment = CapabilityAssessment(
                rating=coding,
                evidence=(
                    EvidenceRef(
                        source="owner_observation",
                        identifier="synthetic_override_test_evidence",
                        date="2026-09-06",
                    ),
                ),
                confidence="medium",
                assessed_on="2026-09-06",
                rationale="Synthetic override-test rating with provenance.",
                human_override=override,
            )
            unknown = CapabilityAssessment(rating=None)
            return CapabilityAssessments(
                reasoning=unknown,
                coding=coding_assessment,
                scientific_methodological=unknown,
                writing_editorial=unknown,
                tool_use=unknown,
                translation_multilingual=unknown,
            )

        override = HumanOverride(
            rating=5,
            decided_on="2026-09-06",
            rationale="Owner-verified deep-coding capability on this synthetic model.",
        )
        without = _synthetic_entry(
            "openai",
            "synthetic-override",
            "max",
            capabilities=rated_capabilities(2, None),
        )
        with_override = _synthetic_entry(
            "openai",
            "synthetic-override",
            "max",
            capabilities=rated_capabilities(2, override),
        )
        requirement = TaskRequirement(
            task_level="L1",
            capability_minima=CapabilityMinima(coding=3),
            hard_constraints=HardConstraints(),
        )
        snapshots = [_snap("openai", 50, 50)]

        # Without the override the model is insufficient; with it, eligible.
        excluded_decision = select_model(
            catalog=ModelCatalog(
                catalog_version=1, updated_on="2026-09-06", entries=(without,)
            ),
            requirement=requirement,
            policy=neutral_selector_policy(),
            snapshots=snapshots,
            evaluated_at=FIXED_AT,
        )
        self.assertIsNone(excluded_decision.selected)
        self.assertEqual(
            "below_minimum",
            excluded_decision.excluded[0].capability_failures[0].reason,
        )
        self.assertEqual(
            2, excluded_decision.excluded[0].capability_failures[0].actual_rating
        )

        selected_decision = select_model(
            catalog=ModelCatalog(
                catalog_version=1, updated_on="2026-09-06", entries=(with_override,)
            ),
            requirement=requirement,
            policy=neutral_selector_policy(),
            snapshots=snapshots,
            evaluated_at=FIXED_AT,
        )
        assert selected_decision.selected is not None
        self.assertEqual(2, selected_decision.selected.capability_margin)

        # The original assessment and evidence are preserved, never mutated.
        self.assertEqual(2, with_override.capabilities.coding.rating)
        assert with_override.capabilities.coding.human_override is not None
        self.assertEqual(5, with_override.capabilities.coding.human_override.rating)
        # effective_rating drives both sufficiency and margin.
        self.assertEqual(5, with_override.capabilities.coding.effective_rating)
        self.assertEqual(
            2,
            capability_margin(
                requirement.capability_minima, with_override.capabilities
            ),
        )
        failures = evaluate_capability_sufficiency(
            requirement.capability_minima, without.capabilities
        )
        self.assertEqual(1, len(failures))


class RankingTests(unittest.TestCase):
    """The exact ``balanced`` ranking-order tests (D-027)."""

    def test_scarcity_before_margin(self) -> None:
        # Full order under routine_coding: penalty dominates margin, so GLM
        # (margin 7, penalty 400) ranks before Luna (margin 5, penalty 3600).
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
        )
        assert decision.selected is not None
        order = [decision.selected] + list(decision.alternatives)
        self.assertEqual(
            ["glm-5.3-flash", "glm-5.3", "gpt-5.6-luna", "gpt-5.6-sol"],
            [c.identity.model for c in order],
        )

    def test_margin_before_preference(self) -> None:
        policy = SelectorPolicy(
            mode="balanced",
            resource_policy=neutral_selector_policy().resource_policy,
            preference_order=(
                ModelIdentity(provider="zai", model="glm-5.3", variant="max"),
            ),
        )
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
            policy=policy,
        )
        assert decision.selected is not None
        # GLM is first preference but has a larger margin than Flash.
        self.assertEqual("glm-5.3-flash", decision.selected.identity.model)
        self.assertEqual("glm-5.3", decision.alternatives[0].identity.model)

    def test_preference_cannot_beat_scarcity(self) -> None:
        policy = SelectorPolicy(
            mode="balanced",
            resource_policy=neutral_selector_policy().resource_policy,
            preference_order=(
                ModelIdentity(provider="openai", model="gpt-5.6-luna", variant="max"),
            ),
        )
        decision = _select(
            PROFILES.resolve("routine_coding"),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
            policy=policy,
        )
        assert decision.selected is not None
        # Preferred Luna (penalty 3600) cannot beat less scarce Flash (400).
        self.assertEqual("glm-5.3-flash", decision.selected.identity.model)

    def test_preference_resolves_true_tie(self) -> None:
        # Identical synthetic entries and an empty-minima requirement give a
        # complete tie: without preference stable identity picks alpha, with
        # preference the explicit order picks beta.
        alpha = _synthetic_entry("openai", "alpha", "max")
        beta = _synthetic_entry("openai", "beta", "max")
        requirement = TaskRequirement(
            task_level="L1",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(),
        )
        snapshots = [_snap("openai", 50, 50)]

        plain = select_model(
            catalog=ModelCatalog(
                catalog_version=1, updated_on="2026-09-06", entries=(alpha, beta)
            ),
            requirement=requirement,
            policy=neutral_selector_policy(),
            snapshots=snapshots,
            evaluated_at=FIXED_AT,
        )
        assert plain.selected is not None
        self.assertEqual("alpha", plain.selected.identity.model)

        preferred = select_model(
            catalog=ModelCatalog(
                catalog_version=1, updated_on="2026-09-06", entries=(alpha, beta)
            ),
            requirement=requirement,
            policy=SelectorPolicy(
                mode="balanced",
                resource_policy=neutral_selector_policy().resource_policy,
                preference_order=(
                    ModelIdentity(provider="openai", model="beta", variant="max"),
                ),
            ),
            snapshots=snapshots,
            evaluated_at=FIXED_AT,
        )
        assert preferred.selected is not None
        self.assertEqual("beta", preferred.selected.identity.model)

    def test_stable_identity_final_tie_independent_of_catalog_order(self) -> None:
        alpha = _synthetic_entry("openai", "alpha", "max")
        beta = _synthetic_entry("openai", "beta", "max")
        requirement = TaskRequirement(
            task_level="L1",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(),
        )
        snapshots = [_snap("openai", 50, 50)]
        first = select_model(
            catalog=ModelCatalog(
                catalog_version=1, updated_on="2026-09-06", entries=(alpha, beta)
            ),
            requirement=requirement,
            policy=neutral_selector_policy(),
            snapshots=snapshots,
            evaluated_at=FIXED_AT,
        )
        second = select_model(
            catalog=ModelCatalog(
                catalog_version=1, updated_on="2026-09-06", entries=(beta, alpha)
            ),
            requirement=requirement,
            policy=neutral_selector_policy(),
            snapshots=snapshots,
            evaluated_at=FIXED_AT,
        )
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_input_order_independence(self) -> None:
        requirement = PROFILES.resolve("routine_coding")
        policy = neutral_selector_policy()
        base = _select(requirement, [_snap("openai", 40, 40), _snap("zai", 80, 80)])
        reversed_snapshots = _select(
            requirement, [_snap("zai", 80, 80), _snap("openai", 40, 40)]
        )
        self.assertEqual(base.to_dict(), reversed_snapshots.to_dict())
        reversed_catalog = ModelCatalog(
            catalog_version=1,
            updated_on=CATALOG.updated_on,
            entries=tuple(reversed(CATALOG.entries)),
        )
        reversed_entries = select_model(
            catalog=reversed_catalog,
            requirement=requirement,
            policy=policy,
            snapshots=[_snap("openai", 40, 40), _snap("zai", 80, 80)],
            evaluated_at=FIXED_AT,
        )
        self.assertEqual(base.to_dict(), reversed_entries.to_dict())


class HardConstraintTests(unittest.TestCase):
    """Every hard constraint individually, with tri-state semantics."""

    def _eval(
        self,
        hard: HardConstraints,
        entry: ModelCatalogEntry,
    ) -> tuple[HardConstraintFailure, ...]:
        return evaluate_hard_constraints(hard, entry)

    def test_minimum_input_context_tokens(self) -> None:
        hard = HardConstraints(minimum_input_context_tokens=500_000)
        unknown = _synthetic_entry(
            "openai", "syn-a", "max", hard_properties=ModelHardProperties()
        )
        insufficient = _synthetic_entry(
            "openai",
            "syn-b",
            "max",
            hard_properties=ModelHardProperties(input_context_tokens=100_000),
        )
        sufficient = _synthetic_entry(
            "openai",
            "syn-c",
            "max",
            hard_properties=ModelHardProperties(input_context_tokens=1_000_000),
        )
        self.assertEqual(
            ("unknown",), tuple(f.reason for f in self._eval(hard, unknown))
        )
        self.assertEqual(
            ("insufficient",), tuple(f.reason for f in self._eval(hard, insufficient))
        )
        self.assertEqual((), self._eval(hard, sufficient))

    def test_minimum_output_tokens(self) -> None:
        hard = HardConstraints(minimum_output_tokens=64_000)
        unknown = _synthetic_entry(
            "openai", "syn-a", "max", hard_properties=ModelHardProperties()
        )
        insufficient = _synthetic_entry(
            "openai",
            "syn-b",
            "max",
            hard_properties=ModelHardProperties(output_tokens=32_000),
        )
        self.assertEqual(
            ("unknown",), tuple(f.reason for f in self._eval(hard, unknown))
        )
        self.assertEqual(
            ("insufficient",), tuple(f.reason for f in self._eval(hard, insufficient))
        )

    def test_requires_tool_use_tri_state(self) -> None:
        hard = HardConstraints(requires_tool_use=True)
        unknown = _synthetic_entry(
            "openai", "syn-a", "max", hard_properties=ModelHardProperties()
        )
        unsupported = _synthetic_entry(
            "openai",
            "syn-b",
            "max",
            hard_properties=ModelHardProperties(supports_tool_use=False),
        )
        supported = _synthetic_entry(
            "openai",
            "syn-c",
            "max",
            hard_properties=ModelHardProperties(supports_tool_use=True),
        )
        not_required = HardConstraints(requires_tool_use=False)
        self.assertEqual(
            ("unknown",), tuple(f.reason for f in self._eval(hard, unknown))
        )
        self.assertEqual(
            ("unsupported",), tuple(f.reason for f in self._eval(hard, unsupported))
        )
        self.assertEqual((), self._eval(hard, supported))
        # A False requirement passes regardless — never "must not support".
        self.assertEqual((), self._eval(not_required, unsupported))
        self.assertEqual((), self._eval(not_required, unknown))

    def test_requires_vision_tri_state_real_catalog(self) -> None:
        hard = HardConstraints(requires_vision=True)
        # GLM-5.3 is evidenced known false; unknown differs from false.
        self.assertEqual(
            ("unsupported",), tuple(f.reason for f in self._eval(hard, GLM53))
        )
        unknown = _synthetic_entry(
            "openai", "syn-a", "max", hard_properties=ModelHardProperties()
        )
        self.assertEqual(
            ("unknown",), tuple(f.reason for f in self._eval(hard, unknown))
        )
        self.assertEqual((), self._eval(hard, FLASH))

    def test_requires_reasoning_mode_tri_state(self) -> None:
        hard = HardConstraints(requires_reasoning_mode=True)
        unknown = _synthetic_entry(
            "openai", "syn-a", "max", hard_properties=ModelHardProperties()
        )
        unsupported = _synthetic_entry(
            "openai",
            "syn-b",
            "max",
            hard_properties=ModelHardProperties(supports_reasoning_mode=False),
        )
        self.assertEqual(
            ("unknown",), tuple(f.reason for f in self._eval(hard, unknown))
        )
        self.assertEqual(
            ("unsupported",), tuple(f.reason for f in self._eval(hard, unsupported))
        )

    def test_required_provider(self) -> None:
        hard = HardConstraints(required_provider="openai")
        self.assertEqual(
            ("mismatch",), tuple(f.reason for f in self._eval(hard, GLM53))
        )
        self.assertEqual((), self._eval(hard, LUNA))

    def test_required_model(self) -> None:
        hard = HardConstraints(
            required_model=ModelRef(provider="zai", model="glm-5.3")
        )
        self.assertEqual(
            ("mismatch",), tuple(f.reason for f in self._eval(hard, FLASH))
        )
        self.assertEqual((), self._eval(hard, GLM53))

    def test_required_variant(self) -> None:
        hard = HardConstraints(required_variant="high")
        self.assertEqual(
            ("mismatch",), tuple(f.reason for f in self._eval(hard, LUNA))
        )
        self.assertEqual((), self._eval(hard, SOL))

    def test_privacy_constraint_exact_tag(self) -> None:
        hard = HardConstraints(privacy_constraint="no_training")
        unknown = _synthetic_entry(
            "openai", "syn-a", "max", hard_properties=ModelHardProperties()
        )
        empty = _synthetic_entry(
            "openai",
            "syn-b",
            "max",
            hard_properties=ModelHardProperties(privacy_tags=()),
        )
        matching = _synthetic_entry(
            "openai",
            "syn-c",
            "max",
            hard_properties=ModelHardProperties(privacy_tags=("no_training",)),
        )
        other = _synthetic_entry(
            "openai",
            "syn-d",
            "max",
            hard_properties=ModelHardProperties(privacy_tags=("regional",)),
        )
        self.assertEqual(
            ("privacy_unknown",), tuple(f.reason for f in self._eval(hard, unknown))
        )
        self.assertEqual(
            ("privacy_unsatisfied",),
            tuple(f.reason for f in self._eval(hard, empty)),
        )
        self.assertEqual(
            ("privacy_unsatisfied",),
            tuple(f.reason for f in self._eval(hard, other)),
        )
        self.assertEqual((), self._eval(hard, matching))
        # Current catalog privacy characteristics are unfrozen everywhere.
        self.assertEqual(
            ("privacy_unknown",), tuple(f.reason for f in self._eval(hard, LUNA))
        )


class TightenTests(unittest.TestCase):
    """The monotone ``tighten_requirement`` rules (D-027)."""

    BASE: TaskRequirement = PROFILES.resolve("deep_coding")

    def _explicit(
        self,
        *,
        task_level: str = "L3",
        minima: CapabilityMinima | None = None,
        hard: HardConstraints | None = None,
    ) -> TaskRequirement:
        return TaskRequirement(
            task_level=task_level,
            capability_minima=minima if minima is not None else CapabilityMinima(),
            hard_constraints=hard if hard is not None else HardConstraints(),
        )

    def test_higher_task_level_accepted_lower_rejected(self) -> None:
        raised = tighten_requirement(self.BASE, self._explicit(task_level="L4"))
        self.assertEqual("L4", raised.task_level)
        same = tighten_requirement(self.BASE, self._explicit(task_level="L3"))
        self.assertEqual("L3", same.task_level)
        with self.assertRaises(SelectionContractValidationError):
            _ = tighten_requirement(self.BASE, self._explicit(task_level="L2"))

    def test_capability_minimum_monotone(self) -> None:
        raised = tighten_requirement(
            self.BASE, self._explicit(minima=CapabilityMinima(coding=5, reasoning=5))
        )
        self.assertEqual(5, raised.capability_minima.reasoning)
        self.assertEqual(5, raised.capability_minima.coding)
        self.assertEqual(4, raised.capability_minima.tool_use)
        added = tighten_requirement(
            self.BASE, self._explicit(minima=CapabilityMinima(tool_use=5))
        )
        self.assertEqual(5, added.capability_minima.tool_use)
        lowered = self._explicit(minima=CapabilityMinima(coding=4))
        with self.assertRaises(SelectionContractValidationError):
            _ = tighten_requirement(self.BASE, lowered)

    def test_numeric_hard_minimum_monotone(self) -> None:
        raised = tighten_requirement(
            self.BASE,
            self._explicit(hard=HardConstraints(minimum_output_tokens=100_000)),
        )
        self.assertEqual(100_000, raised.hard_constraints.minimum_output_tokens)
        # Lowering an existing base minimum is an attempted relaxation.
        lowered = self._explicit(hard=HardConstraints(minimum_output_tokens=1_000))
        with self.assertRaises(SelectionContractValidationError):
            _ = tighten_requirement(raised, lowered)
        added = tighten_requirement(
            self.BASE,
            self._explicit(hard=HardConstraints(minimum_input_context_tokens=200_000)),
        )
        self.assertEqual(
            200_000, added.hard_constraints.minimum_input_context_tokens
        )

    def test_booleans_combine_monotonically_with_or(self) -> None:
        # Base requires tool use; explicit False cannot loosen it.
        no_op = tighten_requirement(
            self.BASE, self._explicit(hard=HardConstraints(requires_tool_use=False))
        )
        self.assertTrue(no_op.hard_constraints.requires_tool_use)
        added = tighten_requirement(
            self.BASE, self._explicit(hard=HardConstraints(requires_vision=True))
        )
        self.assertTrue(added.hard_constraints.requires_vision)
        self.assertTrue(added.hard_constraints.requires_tool_use)
        self.assertTrue(added.hard_constraints.requires_reasoning_mode)

    def test_identity_and_privacy_constraints(self) -> None:
        same = tighten_requirement(
            self.BASE,
            self._explicit(hard=HardConstraints(required_provider="openai")),
        )
        self.assertEqual("openai", same.hard_constraints.required_provider)
        new = tighten_requirement(
            self.BASE,
            self._explicit(hard=HardConstraints(required_variant="high")),
        )
        self.assertEqual("high", new.hard_constraints.required_variant)
        repeated = tighten_requirement(
            self.BASE,
            self._explicit(
                hard=HardConstraints(
                    required_model=ModelRef(provider="openai", model="gpt-5.6-sol")
                )
            ),
        )
        assert repeated.hard_constraints.required_model is not None
        self.assertEqual(
            ("openai", "gpt-5.6-sol"),
            (
                repeated.hard_constraints.required_model.provider,
                repeated.hard_constraints.required_model.model,
            ),
        )
        # A conflict requires the base to already carry the constraint:
        # tighten once to add it, then tighten again with a different value.
        for first, conflicting in (
            (
                HardConstraints(required_provider="openai"),
                HardConstraints(required_provider="zai"),
            ),
            (
                HardConstraints(required_variant="high"),
                HardConstraints(required_variant="low"),
            ),
            (
                HardConstraints(privacy_constraint="no_training"),
                HardConstraints(privacy_constraint="regional"),
            ),
            (
                HardConstraints(
                    required_model=ModelRef(provider="openai", model="gpt-5.6-sol")
                ),
                HardConstraints(
                    required_model=ModelRef(provider="zai", model="glm-5.3")
                ),
            ),
        ):
            with_base = tighten_requirement(self.BASE, self._explicit(hard=first))
            with self.assertRaises(SelectionContractValidationError):
                _ = tighten_requirement(with_base, self._explicit(hard=conflicting))

    def test_result_round_trips_through_task_requirement(self) -> None:
        merged = tighten_requirement(
            self.BASE,
            self._explicit(
                task_level="L4",
                minima=CapabilityMinima(reasoning=5),
                hard=HardConstraints(minimum_output_tokens=100_000),
            ),
        )
        restored = TaskRequirement.from_dict(merged.to_dict())
        self.assertEqual(merged, restored)


class NoSolutionTests(unittest.TestCase):
    """Structured no-solution results (D-027)."""

    def test_structured_no_solution(self) -> None:
        requirement = TaskRequirement(
            task_level="L3",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(minimum_output_tokens=999_999),
        )
        decision = _select(
            requirement, [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        )
        self.assertIsNone(decision.selected)
        self.assertEqual((), decision.alternatives)
        self.assertTrue(decision.excluded)
        self.assertEqual(decision.closest_candidates, _closest_of(decision))
        self.assertIn("no_eligible_candidate", decision.reason_codes)
        # No requirement relaxation and no capability bypass ever happen.
        self.assertEqual(requirement, decision.requirement)

    def test_closest_candidates_stage_progress_only(self) -> None:
        # All candidates fail the same hard constraint: the closest are
        # those hard exclusions, capped at 3.
        explicit = TaskRequirement(
            task_level="L1",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(minimum_output_tokens=999_999),
        )
        hard_decision = _select(
            explicit, [_snap("openai", 40, 40), _snap("zai", 80, 80)]
        )
        self.assertTrue(
            all(
                c.exclusion_stage == "hard_constraint"
                for c in hard_decision.closest_candidates
            )
        )
        self.assertEqual(3, len(hard_decision.closest_candidates))
        # Mixed stages: capability exclusions (a later stage) are closer
        # than hard ones; hard-excluded candidates never appear in closest.
        provider_scoped = TaskRequirement(
            task_level="L4",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(required_provider="zai"),
        )
        mixed = _select(
            tighten_requirement(
                PROFILES.resolve("scientific_review"), provider_scoped
            ),
            [_snap("openai", 40, 40), _snap("zai", 80, 80)],
        )
        self.assertIsNone(mixed.selected)
        self.assertEqual(
            [("zai", "glm-5.3", "max"), ("zai", "glm-5.3-flash", "max")],
            [
                (c.identity.provider, c.identity.model, c.identity.variant)
                for c in mixed.closest_candidates
            ],
        )


def _closest_of(
    decision: SelectionDecision,
) -> tuple[CandidateEvaluation, ...]:
    from scarcity_router.selector import EXCLUSION_STAGES

    progress = {stage: i for i, stage in enumerate(EXCLUSION_STAGES)}
    excluded = decision.excluded
    if not excluded:
        return ()
    max_progress = max(progress[c.exclusion_stage or ""] for c in excluded)
    closest = tuple(
        c for c in excluded if progress[c.exclusion_stage or ""] == max_progress
    )
    return closest[:3]


class ValidationTests(unittest.TestCase):
    """Deterministic input validation before ranking (D-027)."""

    def test_duplicate_provider_snapshots_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _select(
                PROFILES.resolve("routine_coding"),
                [_snap("openai", 40, 40), _snap("openai", 80, 80)],
            )

    def test_duplicate_replenishment_state_rejected(self) -> None:
        state = ReplenishmentState(
            provider="openai",
            kind="rate_limit_reset",
            available_count=1,
            details_known=True,
            earliest_expiry=None,
            retrieved_at=RETRIEVED_AT,
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = _select(
                PROFILES.resolve("routine_coding"),
                [_snap("openai", 40, 40), _snap("zai", 80, 80)],
                states=(state, state),
            )

    def test_naive_evaluated_at_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = select_model(
                catalog=CATALOG,
                requirement=PROFILES.resolve("routine_coding"),
                policy=neutral_selector_policy(),
                snapshots=[_snap("openai", 40, 40)],
                evaluated_at=cast("datetime", _ill(datetime(2026, 9, 6, 12, 0))),
            )

    def test_selector_policy_rejects_unknown_mode_and_duplicates(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = SelectorPolicy(
                mode=cast("str", _ill("quality-first")),
                resource_policy=neutral_selector_policy().resource_policy,
            )
        identity = ModelIdentity(provider="openai", model="alpha", variant="max")
        with self.assertRaises(SelectionContractValidationError):
            _ = SelectorPolicy(
                mode="balanced",
                resource_policy=neutral_selector_policy().resource_policy,
                preference_order=(identity, identity),
            )

    def test_selector_policy_round_trip(self) -> None:
        policy = neutral_selector_policy()
        restored = SelectorPolicy.from_dict(policy.to_dict())
        self.assertEqual(policy, restored)
        with self.assertRaises(SelectionContractValidationError):
            _ = SelectorPolicy.from_dict(
                _ill({"mode": "balanced", "unexpected": True})
            )


if __name__ == "__main__":
    _ = unittest.main()
