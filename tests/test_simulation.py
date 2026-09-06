"""M2e simulation tests (D-027).

These tests pin the frozen simulation semantics: baseline and simulated
decisions are produced by the SAME authoritative ``select_model`` core,
overrides never mutate baseline inputs, a percentage override must match
exactly one existing ``ok`` window with a known percentage pair, and the
policy / replenishment / evaluated_at replacement semantics behave exactly
as frozen. Deterministic and self-contained: no network, subprocess, clock
or credential access.

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
from unittest import mock

from scarcity_router import (
    CapacityDiagnostic,
    CapacityScopeRef,
    CapacitySnapshot,
    CapacityWindow,
    ModelCatalog,
    ReplenishmentState,
    ReservationRule,
    SelectionContractValidationError,
    SelectionDecision,
    SelectorPolicy,
    SimulationOverrides,
    SimulationResult,
    TaskRequirement,
    UserPolicy,
    apply_capacity_overrides,
    neutral_selector_policy,
    simulate_selection,
)
from scarcity_router.selection_types import TaskProfileCatalog
from scarcity_router.simulation import CapacityPercentageOverride
from scarcity_router.simulation import select_model as select_model_core

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
        "dict[str, object]", json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    )
    definitions: list[dict[str, object]] = []
    for item in cast("list[object]", document["task_profiles"]):
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
DEEP_CODING = PROFILES.resolve("deep_coding")


def _snap(provider: str, five: int | None, weekly: int | None) -> CapacitySnapshot:
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
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=RETRIEVED_AT,
        status="ok",
        windows=tuple(windows),
        diagnostics=(),
    )


def _snapshots() -> list[CapacitySnapshot]:
    return [_snap("openai", 40, 40), _snap("zai", 80, 80)]


def _simulate(
    overrides: SimulationOverrides,
    *,
    snapshots: list[CapacitySnapshot] | None = None,
    policy: SelectorPolicy | None = None,
    requirement: TaskRequirement | None = None,
    states: tuple[ReplenishmentState, ...] = (),
) -> SimulationResult:
    return simulate_selection(
        catalog=CATALOG,
        requirement=DEEP_CODING if requirement is None else requirement,
        policy=policy if policy is not None else neutral_selector_policy(),
        snapshots=_snapshots() if snapshots is None else snapshots,
        evaluated_at=FIXED_AT,
        overrides=overrides,
        replenishment_states=states,
    )


class SameSelectorTests(unittest.TestCase):
    """Simulation must run the authoritative selector, never a copy."""

    def test_baseline_and_simulated_use_select_model(self) -> None:
        with mock.patch(
            "scarcity_router.simulation.select_model", wraps=select_model_core
        ) as spy:
            result = _simulate(SimulationOverrides())
        # Exactly two calls: the baseline and the simulated decision.
        self.assertEqual(2, spy.call_count)
        self.assertIsInstance(result.baseline, SelectionDecision)
        self.assertIsInstance(result.simulated, SelectionDecision)
        # With no overrides the simulated decision equals the baseline.
        self.assertEqual(result.baseline.to_dict(), result.simulated.to_dict())


class NoMutationTests(unittest.TestCase):
    """Simulation never mutates baseline inputs."""

    def test_baseline_snapshots_and_policy_unchanged(self) -> None:
        snapshots = _snapshots()
        policy = neutral_selector_policy()
        before_snapshots = [s.to_dict() for s in snapshots]
        before_policy = policy.to_dict()
        overrides = SimulationOverrides(
            capacity_percentages=(
                CapacityPercentageOverride(
                    provider="zai",
                    scope_id="coding_plan",
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=2,
                ),
            ),
        )
        _ = _simulate(overrides, snapshots=snapshots, policy=policy)
        self.assertEqual(before_snapshots, [s.to_dict() for s in snapshots])
        self.assertEqual(before_policy, policy.to_dict())


class CapacityOverrideTests(unittest.TestCase):
    """The famous 98/2 case and the typed override-target failures."""

    def test_98_2_weekly_override_changes_decision(self) -> None:
        result = _simulate(
            SimulationOverrides(
                capacity_percentages=(
                    CapacityPercentageOverride(
                        provider="zai",
                        scope_id="coding_plan",
                        resource="tokens",
                        kind="weekly",
                        remaining_percent=2,
                    ),
                ),
            )
        )
        assert result.baseline.selected is not None
        assert result.simulated.selected is not None
        self.assertEqual("glm-5.3", result.baseline.selected.identity.model)
        self.assertEqual(
            "gpt-5.6-sol", result.simulated.selected.identity.model
        )

    def test_override_preserves_everything_but_percentages(self) -> None:
        snapshots = _snapshots()
        overridden = apply_capacity_overrides(
            snapshots,
            (
                CapacityPercentageOverride(
                    provider="zai",
                    scope_id="coding_plan",
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=7,
                ),
            ),
        )
        self.assertEqual(2, len(overridden))
        baseline_zai = snapshots[1]
        simulated_zai = overridden[1]
        self.assertEqual(baseline_zai.provider, simulated_zai.provider)
        self.assertEqual(baseline_zai.status, simulated_zai.status)
        self.assertEqual(baseline_zai.source, simulated_zai.source)
        self.assertEqual(baseline_zai.retrieved_at, simulated_zai.retrieved_at)
        self.assertEqual(baseline_zai.plan, simulated_zai.plan)
        self.assertEqual(baseline_zai.diagnostics, simulated_zai.diagnostics)
        baseline_weekly = [
            w for w in baseline_zai.windows if w.kind == "weekly"
        ][0]
        simulated_weekly = [
            w for w in simulated_zai.windows if w.kind == "weekly"
        ][0]
        self.assertEqual(7, simulated_weekly.remaining_percent)
        self.assertEqual(93, simulated_weekly.used_percent)
        self.assertEqual(baseline_weekly.scope_id, simulated_weekly.scope_id)
        self.assertEqual(
            baseline_weekly.duration_seconds, simulated_weekly.duration_seconds
        )
        self.assertEqual(baseline_weekly.window_id, simulated_weekly.window_id)
        # The baseline object itself is untouched.
        self.assertEqual(80, baseline_weekly.remaining_percent)

    def test_ambiguous_override_rejected(self) -> None:
        # Two weekly coding_plan windows without a disambiguating window_id.
        zai = CapacitySnapshot(
            schema_version=3,
            provider="zai",
            source="synthetic_test",
            retrieved_at=RETRIEVED_AT,
            status="ok",
            windows=(
                CapacityWindow(
                    resource="tokens",
                    kind="weekly",
                    scope_id="coding_plan",
                    duration_seconds=604_800,
                    used_percent=20,
                    remaining_percent=80,
                    window_id="weekly-a",
                ),
                CapacityWindow(
                    resource="tokens",
                    kind="weekly",
                    scope_id="coding_plan",
                    duration_seconds=604_800,
                    used_percent=30,
                    remaining_percent=70,
                    window_id="weekly-b",
                ),
            ),
            diagnostics=(),
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = apply_capacity_overrides(
                [_snap("openai", 40, 40), zai],
                (
                    CapacityPercentageOverride(
                        provider="zai",
                        scope_id="coding_plan",
                        resource="tokens",
                        kind="weekly",
                        remaining_percent=2,
                    ),
                ),
            )

    def test_disambiguated_override_matches_exactly_one_window(self) -> None:
        zai = CapacitySnapshot(
            schema_version=3,
            provider="zai",
            source="synthetic_test",
            retrieved_at=RETRIEVED_AT,
            status="ok",
            windows=(
                CapacityWindow(
                    resource="tokens",
                    kind="weekly",
                    scope_id="coding_plan",
                    duration_seconds=604_800,
                    used_percent=20,
                    remaining_percent=80,
                    window_id="weekly-a",
                ),
                CapacityWindow(
                    resource="tokens",
                    kind="weekly",
                    scope_id="coding_plan",
                    duration_seconds=604_800,
                    used_percent=30,
                    remaining_percent=70,
                    window_id="weekly-b",
                ),
            ),
            diagnostics=(),
        )
        overridden = apply_capacity_overrides(
            [zai],
            (
                CapacityPercentageOverride(
                    provider="zai",
                    scope_id="coding_plan",
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=5,
                    window_id="weekly-b",
                ),
            ),
        )
        weekly_b = [
            w for w in overridden[0].windows if w.window_id == "weekly-b"
        ][0]
        self.assertEqual(5, weekly_b.remaining_percent)
        weekly_a = [
            w for w in overridden[0].windows if w.window_id == "weekly-a"
        ][0]
        self.assertEqual(80, weekly_a.remaining_percent)

    def test_missing_override_target_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = apply_capacity_overrides(
                _snapshots(),
                (
                    CapacityPercentageOverride(
                        provider="zai",
                        scope_id="other_plan",
                        resource="tokens",
                        kind="weekly",
                        remaining_percent=2,
                    ),
                ),
            )

    def test_missing_provider_override_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = apply_capacity_overrides(
                [_snap("zai", 80, 80)],
                (
                    CapacityPercentageOverride(
                        provider="openai",
                        scope_id="codex",
                        resource="tokens",
                        kind="weekly",
                        remaining_percent=2,
                    ),
                ),
            )

    def test_non_ok_snapshot_override_rejected(self) -> None:
        bad = CapacitySnapshot(
            schema_version=3,
            provider="zai",
            source="synthetic_test",
            retrieved_at=RETRIEVED_AT,
            status="unknown",
            windows=(),
            diagnostics=(CapacityDiagnostic(code="telemetry_unknown"),),
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = apply_capacity_overrides(
                [_snap("openai", 40, 40), bad],
                (
                    CapacityPercentageOverride(
                        provider="zai",
                        scope_id="coding_plan",
                        resource="tokens",
                        kind="weekly",
                        remaining_percent=2,
                    ),
                ),
            )

    def test_unknown_percentage_target_rejected(self) -> None:
        zai = CapacitySnapshot(
            schema_version=3,
            provider="zai",
            source="synthetic_test",
            retrieved_at=RETRIEVED_AT,
            status="ok",
            windows=(
                CapacityWindow(
                    resource="time",
                    kind="unknown",
                    scope_id="coding_plan",
                ),
            ),
            diagnostics=(),
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = apply_capacity_overrides(
                [zai],
                (
                    CapacityPercentageOverride(
                        provider="zai",
                        scope_id="coding_plan",
                        resource="time",
                        kind="weekly",
                        remaining_percent=2,
                    ),
                ),
            )

    def test_same_window_twice_rejected(self) -> None:
        override = CapacityPercentageOverride(
            provider="zai",
            scope_id="coding_plan",
            resource="tokens",
            kind="weekly",
            remaining_percent=2,
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = apply_capacity_overrides(
                [_snap("zai", 80, 80)],
                (override, override),
            )

    def test_override_remaining_bounds(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = CapacityPercentageOverride(
                provider="zai",
                scope_id="coding_plan",
                resource="tokens",
                kind="weekly",
                remaining_percent=cast("int", _ill(101)),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = CapacityPercentageOverride(
                provider="zai",
                scope_id="coding_plan",
                resource="tokens",
                kind="weekly",
                remaining_percent=cast("int", _ill("50")),
            )


class ReplacementTests(unittest.TestCase):
    """Policy, replenishment and evaluated_at replacement semantics."""

    def _zai_blackout_policy(self) -> SelectorPolicy:
        from scarcity_router import AvailabilityTarget, WeeklyBlackoutRule

        return SelectorPolicy(
            mode="balanced",
            resource_policy=UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="advisory",
                reservations=(),
                blackouts=(
                    WeeklyBlackoutRule(
                        rule_id="hold-zai-mornings",
                        target=AvailabilityTarget(provider="zai"),
                        timezone="UTC",
                        weekdays=("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
                        start_local="01:00",
                        end_local="02:00",
                        reason_code="peak_hold",
                    ),
                ),
            ),
        )

    def test_blackout_time_override(self) -> None:
        policy = self._zai_blackout_policy()
        simulated_at = datetime(2026, 9, 7, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(0, simulated_at.weekday())  # a Monday
        result = _simulate(
            SimulationOverrides(evaluated_at=simulated_at), policy=policy
        )
        assert result.baseline.selected is not None
        assert result.simulated.selected is not None
        # Monday 12:00 UTC is outside the blackout; Monday 01:30 is inside.
        self.assertEqual("glm-5.3", result.baseline.selected.identity.model)
        self.assertEqual(
            "gpt-5.6-sol", result.simulated.selected.identity.model
        )
        excluded = {
            (c.identity.provider, c.identity.model): c
            for c in result.simulated.excluded
        }
        glm = excluded[("zai", "glm-5.3")]
        self.assertEqual("policy_blackout", glm.exclusion_stage)

    def test_policy_replacement(self) -> None:
        simulated_policy = SelectorPolicy(
            mode="balanced",
            resource_policy=UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="advisory",
                reservations=(
                    ReservationRule(
                        rule_id="protect-zai-weekly",
                        scope=CapacityScopeRef(
                            provider="zai", scope_id="coding_plan"
                        ),
                        resource="tokens",
                        kind="weekly",
                        when_remaining_below=90,
                        minimum_task_level="L3",
                    ),
                ),
                blackouts=(),
            ),
        )
        result = _simulate(
            SimulationOverrides(selector_policy=simulated_policy),
            requirement=PROFILES.resolve("routine_coding"),
        )
        assert result.baseline.selected is not None
        assert result.simulated.selected is not None
        # L1 routine work is below the reservation minimum: Z.ai blocked in
        # the simulated run only; the baseline policy is untouched.
        self.assertEqual("glm-5.3-flash", result.baseline.selected.identity.model)
        self.assertEqual(
            "gpt-5.6-luna", result.simulated.selected.identity.model
        )
        excluded = {
            (c.identity.provider, c.identity.model): c
            for c in result.simulated.excluded
        }
        flash = excluded[("zai", "glm-5.3-flash")]
        self.assertEqual("reservation", flash.exclusion_stage)

    def test_replenishment_replacement(self) -> None:
        recoverable_policy = SelectorPolicy(
            mode="balanced",
            resource_policy=UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="recoverable",
                reservations=(),
                blackouts=(),
            ),
        )
        exhausted = [_snap("openai", 98, 0), _snap("zai", 80, 80)]
        state = ReplenishmentState(
            provider="openai",
            kind="rate_limit_reset",
            available_count=3,
            details_known=True,
            earliest_expiry=None,
            retrieved_at=RETRIEVED_AT,
        )
        # Baseline has no observations and the explicit empty tuple
        # simulates no observations: nothing is recoverable.
        without_states = _simulate(
            SimulationOverrides(replenishment_states=()),
            snapshots=exhausted,
            policy=recoverable_policy,
            requirement=PROFILES.resolve("scientific_review"),
        )
        self.assertEqual((), without_states.baseline.recoverable_candidates)
        self.assertEqual((), without_states.simulated.recoverable_candidates)
        # Supplying the reset state makes Sol recoverable (never eligible).
        replaced = _simulate(
            SimulationOverrides(replenishment_states=(state,)),
            snapshots=exhausted,
            policy=recoverable_policy,
            requirement=PROFILES.resolve("scientific_review"),
        )
        self.assertIsNone(replaced.simulated.selected)
        self.assertEqual(1, len(replaced.simulated.recoverable_candidates))
        self.assertEqual(
            "gpt-5.6-sol",
            replaced.simulated.recoverable_candidates[0].identity.model,
        )
        # Current capacity facts are unchanged by replenishment visibility.
        sol = [
            c
            for c in replaced.simulated.excluded
            if c.identity.model == "gpt-5.6-sol"
        ][0]
        assert sol.scarcity_assessment is not None
        self.assertEqual(10000, sol.scarcity_assessment.penalty_units)


class SerializationTests(unittest.TestCase):
    """Deterministic overrides serialization and strict shapes."""

    def test_overrides_round_trip(self) -> None:
        overrides = SimulationOverrides(
            capacity_percentages=(
                CapacityPercentageOverride(
                    provider="zai",
                    scope_id="coding_plan",
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=2,
                ),
            ),
            evaluated_at=datetime(2026, 9, 7, 1, 30, tzinfo=timezone.utc),
        )
        restored = SimulationOverrides.from_dict(overrides.to_dict())
        self.assertEqual(overrides, restored)

    def test_overrides_strict_shapes(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = SimulationOverrides.from_dict(_ill({"unknown_key": True}))
        # A naive (offset-less) instant is invalid.
        with self.assertRaises(SelectionContractValidationError):
            _ = SimulationOverrides.from_dict(
                _ill({"evaluated_at": "2026-09-07T01:30:00"})
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = SimulationOverrides.from_dict(
                _ill({"capacity_percentages": [{"provider": "zai"}]})
            )


if __name__ == "__main__":
    _ = unittest.main()
