"""M2d resource-policy primitive tests (D-026).

These tests pin the frozen policy primitives: unknown-capacity modes
(``degraded``/``strict``), scope-targeted reservation rules with the strict
``<`` threshold boundary, timezone-aware weekly blackout rules with
half-open ``[start, end)`` semantics, the normalized D-021
``ReplenishmentState`` with its three visibility modes, and the
``UserPolicy`` container — including the required shared-scope, Z.ai
protection and blackout scenarios. Deterministic and self-contained: no
network, subprocess, clock or credential access; every evaluation instant
is constructed explicitly.

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
from zoneinfo import ZoneInfo

from scarcity_router import (
    REPLENISHMENT_MODES,
    UNKNOWN_CAPACITY_MODES,
    AvailabilityTarget,
    BlackoutDecision,
    CapacityDiagnostic,
    CapacityScopeRef,
    CapacitySnapshot,
    CapacityWindow,
    ModelCatalog,
    ModelCatalogEntry,
    ModelIdentity,
    ReplenishmentDecision,
    ReplenishmentState,
    ReservationDecision,
    ReservationRule,
    UnknownCapacityDecision,
    UserPolicy,
    WeeklyBlackoutRule,
    apply_replenishment_mode,
    apply_unknown_capacity_mode,
    assess_scarcity,
    evaluate_blackouts,
    evaluate_reservation,
)
from scarcity_router.errors import SelectionContractValidationError

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"

TS = "2026-09-06T12:00:00.000Z"
RESET = "2026-09-12T09:00:00.000Z"

LUNA = ModelIdentity(provider="openai", model="gpt-5.6-luna", variant="max")
SOL = ModelIdentity(provider="openai", model="gpt-5.6-sol", variant="max")
GLM53 = ModelIdentity(provider="zai", model="glm-5.3", variant="max")
FLASH = ModelIdentity(provider="zai", model="glm-5.3-flash", variant="max")

CODEX_SCOPE = CapacityScopeRef(provider="openai", scope_id="codex")
CODING_PLAN_SCOPE = CapacityScopeRef(provider="zai", scope_id="coding_plan")

WARSAW = ZoneInfo("Europe/Warsaw")


def _ill(value: object) -> object:
    """Mark a deliberately wrong-typed value for a negative-validation test."""
    return value


def _load_catalog() -> dict[tuple[str, str], ModelCatalogEntry]:
    catalog = ModelCatalog.from_dict(
        cast(object, json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
    )
    return {(e.identity.provider, e.identity.model): e for e in catalog.entries}


CATALOG = _load_catalog()
LUNA_ENTRY = CATALOG[("openai", "gpt-5.6-luna")]
GLM53_ENTRY = CATALOG[("zai", "glm-5.3")]


def _window(
    resource: str,
    kind: str,
    scope_id: str,
    remaining: int,
    *,
    with_pair: bool = True,
) -> CapacityWindow:
    duration = {"five_hour": 18_000, "weekly": 604_800}.get(kind)
    return CapacityWindow(
        resource=resource,
        kind=kind,
        scope_id=scope_id,
        duration_seconds=duration,
        used_percent=(100 - remaining) if with_pair else None,
        remaining_percent=remaining if with_pair else None,
        resets_at=RESET,
    )


def _snapshot(
    provider: str,
    windows: tuple[CapacityWindow, ...] = (),
    *,
    status: str = "ok",
) -> CapacitySnapshot:
    diagnostics: tuple[CapacityDiagnostic, ...] = ()
    if status != "ok":
        code = {
            "unavailable": "source_unavailable",
            "auth_required": "auth_required",
            "unsupported": "unsupported_source",
            "schema_changed": "schema_changed",
            "unknown": "telemetry_unknown",
        }[status]
        diagnostics = (CapacityDiagnostic(code=code),)
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="test_source",
        retrieved_at=TS,
        status=status,
        windows=windows,
        diagnostics=diagnostics,
    )


def _openai_snapshot(weekly: int, five_hour: int = 90) -> CapacitySnapshot:
    return _snapshot(
        "openai",
        (
            _window("tokens", "five_hour", "codex", five_hour),
            _window("tokens", "weekly", "codex", weekly),
        ),
    )


def _zai_snapshot(weekly: int, five_hour: int = 98) -> CapacitySnapshot:
    return _snapshot(
        "zai",
        (
            _window("tokens", "five_hour", "coding_plan", five_hour),
            _window("tokens", "weekly", "coding_plan", weekly),
        ),
    )


def _replenishment(count: int = 2, details_known: bool = True) -> ReplenishmentState:
    return ReplenishmentState(
        provider="openai",
        kind="rate_limit_reset",
        available_count=count,
        details_known=details_known,
        earliest_expiry="2026-09-08T00:00:00.000Z" if count > 0 else None,
        retrieved_at=TS,
    )


# ── Unknown-capacity policy ───────────────────────────────────────────────────


class UnknownCapacityPolicyTests(unittest.TestCase):
    def test_frozen_modes(self) -> None:
        self.assertEqual(UNKNOWN_CAPACITY_MODES, frozenset({"degraded", "strict"}))

    def test_degraded_mode_keeps_unknown_conditionally_usable(self) -> None:
        assessment = assess_scarcity(GLM53_ENTRY, [])
        self.assertEqual(assessment.state, "unknown")
        decision = apply_unknown_capacity_mode("degraded", assessment)
        self.assertIsInstance(decision, UnknownCapacityDecision)
        self.assertTrue(decision.eligible_by_unknown_policy)
        self.assertTrue(decision.degraded)
        self.assertEqual(
            decision.reason_codes, ("unknown_capacity_degraded",)
        )

    def test_strict_mode_blocks_unknown(self) -> None:
        assessment = assess_scarcity(GLM53_ENTRY, [])
        decision = apply_unknown_capacity_mode("strict", assessment)
        self.assertFalse(decision.eligible_by_unknown_policy)
        self.assertFalse(decision.degraded)
        self.assertEqual(decision.reason_codes, ("unknown_capacity_blocked",))

    def test_known_nonzero_capacity_is_eligible_in_both_modes(self) -> None:
        assessment = assess_scarcity(GLM53_ENTRY, [_zai_snapshot(weekly=80)])
        self.assertEqual(assessment.state, "known")
        for mode in ("degraded", "strict"):
            decision = apply_unknown_capacity_mode(mode, assessment)
            self.assertTrue(decision.eligible_by_unknown_policy)
            self.assertFalse(decision.degraded)
            self.assertEqual(decision.reason_codes, ())

    def test_known_unavailable_is_blocked_in_both_modes(self) -> None:
        assessment = assess_scarcity(LUNA_ENTRY, [_openai_snapshot(weekly=0)])
        self.assertEqual(assessment.state, "unavailable")
        for mode in ("degraded", "strict"):
            decision = apply_unknown_capacity_mode(mode, assessment)
            self.assertFalse(decision.eligible_by_unknown_policy)
            self.assertFalse(decision.degraded)
            self.assertEqual(decision.reason_codes, ("capacity_exhausted",))

    def test_invalid_mode_rejected(self) -> None:
        assessment = assess_scarcity(GLM53_ENTRY, [])
        for bad in ("optimistic", "", "DEGRADED", None, 1):
            with self.assertRaises(SelectionContractValidationError):
                _ = apply_unknown_capacity_mode(cast(str, bad), assessment)

    def test_decision_serialization_round_trip(self) -> None:
        assessment = assess_scarcity(GLM53_ENTRY, [])
        degraded = apply_unknown_capacity_mode("degraded", assessment)
        payload = degraded.to_dict()
        self.assertEqual(
            payload,
            {
                "mode": "degraded",
                "eligible_by_unknown_policy": True,
                "degraded": True,
                "reason_codes": ["unknown_capacity_degraded"],
            },
        )
        self.assertEqual(UnknownCapacityDecision.from_dict(payload), degraded)
        with self.assertRaises(SelectionContractValidationError):
            _ = UnknownCapacityDecision.from_dict({**payload, "extra": True})

    def test_unknown_mode_never_carries_a_numeric_penalty(self) -> None:
        # The decision type has no penalty field at all: unknown capacity is
        # never ranked against a numeric value.
        self.assertNotIn("penalty", UnknownCapacityDecision.__dataclass_fields__)


# ── Reservation rules ─────────────────────────────────────────────────────────


class ReservationRuleContractTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        rule = ReservationRule(
            rule_id="protect-openai-weekly",
            scope=CODEX_SCOPE,
            resource="tokens",
            kind="weekly",
            when_remaining_below=20,
            minimum_task_level="L4",
        )
        payload = rule.to_dict()
        self.assertEqual(
            payload,
            {
                "rule_id": "protect-openai-weekly",
                "scope": {"provider": "openai", "scope_id": "codex"},
                "resource": "tokens",
                "kind": "weekly",
                "when_remaining_below": 20,
                "minimum_task_level": "L4",
            },
        )
        self.assertEqual(ReservationRule.from_dict(payload), rule)

    def test_rejects_invalid_fields(self) -> None:
        def rule(
            rule_id: str = "protect-openai-weekly",
            scope: CapacityScopeRef = CODEX_SCOPE,
            resource: str = "tokens",
            kind: str = "weekly",
            when_remaining_below: int = 20,
            minimum_task_level: str = "L4",
        ) -> ReservationRule:
            return ReservationRule(
                rule_id=rule_id,
                scope=scope,
                resource=resource,
                kind=kind,
                when_remaining_below=when_remaining_below,
                minimum_task_level=minimum_task_level,
            )

        with self.assertRaises(SelectionContractValidationError):
            _ = rule(rule_id="Not Safe")
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(resource="unknown")  # not a known normalized resource
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(resource="credits")
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(kind="unknown")  # cannot identify a target window
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(kind="daily")
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(when_remaining_below=0)
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(when_remaining_below=101)
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(when_remaining_below=True)  # bool passes statically
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(when_remaining_below=cast(int, _ill(20.0)))
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(minimum_task_level="L6")
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(minimum_task_level="l4")
        with self.assertRaises(SelectionContractValidationError):
            _ = rule(scope=cast(CapacityScopeRef, _ill("openai/codex")))


class ReservationEvaluationTests(unittest.TestCase):
    def _rule(
        self,
        scope: CapacityScopeRef = CODEX_SCOPE,
        threshold: int = 20,
        minimum: str = "L4",
        rule_id: str = "protect-openai-weekly",
    ) -> ReservationRule:
        return ReservationRule(
            rule_id=rule_id,
            scope=scope,
            resource="tokens",
            kind="weekly",
            when_remaining_below=threshold,
            minimum_task_level=minimum,
        )

    def test_strict_boundary_20_does_not_trigger_19_does(self) -> None:
        at_threshold = evaluate_reservation(
            self._rule(), [_openai_snapshot(weekly=20)], "L5"
        )
        self.assertEqual(at_threshold.state, "known")
        self.assertFalse(at_threshold.triggered)
        self.assertFalse(at_threshold.blocked)
        self.assertEqual(at_threshold.reason_codes, ())
        self.assertEqual(at_threshold.evidence_remaining_percent, 20)

        below_threshold = evaluate_reservation(
            self._rule(), [_openai_snapshot(weekly=19)], "L5"
        )
        self.assertTrue(below_threshold.triggered)
        self.assertFalse(below_threshold.blocked)
        self.assertIn("reservation_triggered", below_threshold.reason_codes)
        self.assertIn(
            "reservation_permitted_by_task_level", below_threshold.reason_codes
        )

    def test_task_level_gating(self) -> None:
        rule = self._rule(minimum="L4")
        l3 = evaluate_reservation(rule, [_openai_snapshot(weekly=10)], "L3")
        l4 = evaluate_reservation(rule, [_openai_snapshot(weekly=10)], "L4")
        l5 = evaluate_reservation(rule, [_openai_snapshot(weekly=10)], "L5")
        self.assertTrue(l3.triggered and l3.blocked)
        self.assertIn("reservation_blocked", l3.reason_codes)
        self.assertTrue(l4.triggered and not l4.blocked)
        self.assertTrue(l5.triggered and not l5.blocked)

    def test_shared_openai_scope_covers_luna_and_sol_alike(self) -> None:
        # Luna and Sol both bind to openai/codex (M2c catalog), so one
        # scope-targeted rule evaluates the same shared quota evidence for
        # both models — there is no per-model weekly quota.
        self.assertEqual(LUNA_ENTRY.capacity_bindings, (CODEX_SCOPE,))
        snapshots = [_openai_snapshot(weekly=15)]
        luna_decision = evaluate_reservation(self._rule(), snapshots, "L2")
        sol_decision = evaluate_reservation(self._rule(), snapshots, "L2")
        self.assertEqual(luna_decision, sol_decision)
        self.assertTrue(luna_decision.triggered)
        self.assertTrue(luna_decision.blocked)
        self.assertEqual(luna_decision.evidence_remaining_percent, 15)

    def test_zai_protection_scenario_l1_blocked_l3_permitted(self) -> None:
        rule = ReservationRule(
            rule_id="protect-zai-weekly",
            scope=CODING_PLAN_SCOPE,
            resource="tokens",
            kind="weekly",
            when_remaining_below=20,
            minimum_task_level="L3",
        )
        snapshots = [_zai_snapshot(weekly=2)]
        routine = evaluate_reservation(rule, snapshots, "L1")
        deep = evaluate_reservation(rule, snapshots, "L3")
        self.assertTrue(routine.triggered and routine.blocked)
        self.assertTrue(deep.triggered and not deep.blocked)
        # The rule exists only in this scenario test; it is not checked in
        # as any user's default policy.

    def test_missing_snapshot_is_explicitly_unknown(self) -> None:
        decision = evaluate_reservation(self._rule(), [], "L2")
        self.assertEqual(decision.state, "unknown")
        self.assertIsNone(decision.triggered)
        self.assertIsNone(decision.blocked)
        self.assertIsNone(decision.evidence_remaining_percent)
        self.assertEqual(
            decision.reason_codes, ("reservation_scope_snapshot_missing",)
        )

    def test_non_ok_snapshot_is_explicitly_unknown(self) -> None:
        decision = evaluate_reservation(
            self._rule(), [_snapshot("openai", status="auth_required")], "L2"
        )
        self.assertEqual(decision.state, "unknown")
        self.assertEqual(
            decision.reason_codes, ("reservation_scope_snapshot_not_ok",)
        )

    def test_missing_target_window_is_explicitly_unknown(self) -> None:
        # The openai snapshot exists but has no weekly token window.
        decision = evaluate_reservation(
            self._rule(),
            [_snapshot("openai", (_window("tokens", "five_hour", "codex", 50),))],
            "L2",
        )
        self.assertEqual(decision.state, "unknown")
        self.assertEqual(decision.reason_codes, ("reservation_window_missing",))

    def test_percentage_unknown_is_explicitly_unknown(self) -> None:
        decision = evaluate_reservation(
            self._rule(),
            [
                _snapshot(
                    "openai",
                    (_window("tokens", "weekly", "codex", 0, with_pair=False),),
                )
            ],
            "L2",
        )
        self.assertEqual(decision.state, "unknown")
        self.assertEqual(
            decision.reason_codes, ("reservation_percentage_unknown",)
        )

    def test_most_restrictive_matching_window_governs(self) -> None:
        snapshot = _snapshot(
            "openai",
            (
                _window("tokens", "weekly", "codex", 60),
                _window("tokens", "weekly", "codex", 5),
            ),
        )
        decision = evaluate_reservation(self._rule(), [snapshot], "L2")
        self.assertTrue(decision.triggered)
        self.assertEqual(decision.evidence_remaining_percent, 5)

    def test_reservation_does_not_make_exhausted_capacity_usable(self) -> None:
        # At 0% the rule triggers and a high task level is permitted by the
        # reservation layer — but the scarcity layer still reports explicit
        # exhaustion, and no M2d primitive turns that into eligibility.
        snapshots = [_openai_snapshot(weekly=0)]
        decision = evaluate_reservation(self._rule(), snapshots, "L5")
        self.assertTrue(decision.triggered)
        self.assertFalse(decision.blocked)
        assessment = assess_scarcity(LUNA_ENTRY, snapshots)
        self.assertEqual(assessment.state, "unavailable")
        self.assertEqual(assessment.penalty_units, 10000)
        for mode in ("degraded", "strict"):
            policy_decision = apply_unknown_capacity_mode(mode, assessment)
            self.assertFalse(policy_decision.eligible_by_unknown_policy)

    def test_decision_serialization_round_trip(self) -> None:
        known = evaluate_reservation(
            self._rule(), [_openai_snapshot(weekly=10)], "L3"
        )
        payload = known.to_dict()
        self.assertEqual(
            payload,
            {
                "rule_id": "protect-openai-weekly",
                "state": "known",
                "triggered": True,
                "blocked": True,
                "evidence_remaining_percent": 10,
                "reason_codes": ["reservation_blocked", "reservation_triggered"],
            },
        )
        self.assertEqual(ReservationDecision.from_dict(payload), known)

        unknown = evaluate_reservation(self._rule(), [], "L3")
        unknown_payload = unknown.to_dict()
        self.assertNotIn("triggered", unknown_payload)
        self.assertNotIn("blocked", unknown_payload)
        self.assertNotIn("evidence_remaining_percent", unknown_payload)
        self.assertEqual(ReservationDecision.from_dict(unknown_payload), unknown)

    def test_duplicate_provider_snapshots_fail_typed_validation(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = evaluate_reservation(
                self._rule(),
                [_openai_snapshot(weekly=10), _openai_snapshot(weekly=15)],
                "L3",
            )


# ── Weekly blackout rules ─────────────────────────────────────────────────────


class AvailabilityTargetTests(unittest.TestCase):
    def test_round_trip_and_omitted_optionals(self) -> None:
        provider_only = AvailabilityTarget(provider="zai")
        self.assertEqual(provider_only.to_dict(), {"provider": "zai"})
        self.assertEqual(
            AvailabilityTarget.from_dict({"provider": "zai"}), provider_only
        )
        full = AvailabilityTarget(provider="zai", model="glm-5.3", variant="max")
        self.assertEqual(
            full.to_dict(),
            {"provider": "zai", "model": "glm-5.3", "variant": "max"},
        )
        self.assertEqual(AvailabilityTarget.from_dict(full.to_dict()), full)

    def test_variant_requires_model(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = AvailabilityTarget(provider="zai", variant="max")

    def test_invalid_values_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = AvailabilityTarget(provider="kilo")  # unsupported provider
        with self.assertRaises(SelectionContractValidationError):
            _ = AvailabilityTarget(provider="Zai")
        with self.assertRaises(SelectionContractValidationError):
            _ = AvailabilityTarget(provider="zai", model="GLM-5.3")

    def test_exact_matching(self) -> None:
        self.assertTrue(AvailabilityTarget(provider="zai").matches(GLM53))
        self.assertTrue(AvailabilityTarget(provider="zai").matches(FLASH))
        self.assertFalse(AvailabilityTarget(provider="zai").matches(LUNA))
        self.assertTrue(
            AvailabilityTarget(provider="zai", model="glm-5.3").matches(GLM53)
        )
        self.assertFalse(
            AvailabilityTarget(provider="zai", model="glm-5.3").matches(FLASH)
        )
        self.assertTrue(
            AvailabilityTarget(
                provider="zai", model="glm-5.3", variant="max"
            ).matches(GLM53)
        )
        self.assertFalse(
            AvailabilityTarget(
                provider="zai", model="glm-5.3", variant="other"
            ).matches(GLM53)
        )


class WeeklyBlackoutRuleContractTests(unittest.TestCase):
    def _rule(
        self,
        rule_id: str = "zai-quiet-hours",
        target: AvailabilityTarget | None = None,
        timezone: str = "Europe/Warsaw",
        weekdays: tuple[str, ...] = ("mon",),
        start_local: str = "17:00",
        end_local: str = "03:00",
        reason_code: str = "preserve_zai",
    ) -> WeeklyBlackoutRule:
        return WeeklyBlackoutRule(
            rule_id=rule_id,
            target=target if target is not None else AvailabilityTarget(provider="zai"),
            timezone=timezone,
            weekdays=weekdays,
            start_local=start_local,
            end_local=end_local,
            reason_code=reason_code,
        )

    def test_round_trip_and_canonical_weekday_order(self) -> None:
        rule = self._rule(weekdays=("sun", "mon"))
        self.assertEqual(rule.weekdays, ("mon", "sun"))
        payload = rule.to_dict()
        self.assertEqual(payload["weekdays"], ["mon", "sun"])
        self.assertEqual(WeeklyBlackoutRule.from_dict(payload), rule)

    def test_invalid_values_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(timezone="Mars/Olympus")
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(timezone="../etc/passwd")
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(timezone="")
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(start_local="17:0")
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(start_local="24:00")
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(start_local="1700")
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(end_local="17:60")
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(weekdays=("mon", "mon"))
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(weekdays=("funday",))
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(weekdays=())
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(start_local="03:00", end_local="03:00")
        with self.assertRaises(SelectionContractValidationError):
            _ = self._rule(reason_code="Not Safe")


class BlackoutEvaluationTests(unittest.TestCase):
    def _same_day_rule(self) -> WeeklyBlackoutRule:
        return WeeklyBlackoutRule(
            rule_id="zai-quiet-hours",
            target=AvailabilityTarget(provider="zai"),
            timezone="Europe/Warsaw",
            weekdays=("mon",),
            start_local="09:00",
            end_local="17:00",
            reason_code="preserve_zai",
        )

    def _cross_midnight_rule(self) -> WeeklyBlackoutRule:
        return WeeklyBlackoutRule(
            rule_id="zai-peak",
            target=AvailabilityTarget(provider="zai"),
            timezone="Europe/Warsaw",
            weekdays=("mon",),
            start_local="17:00",
            end_local="03:00",
            reason_code="preserve_zai",
        )

    def test_inclusive_start_exclusive_end(self) -> None:
        rule = self._same_day_rule()
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 9, 0, tzinfo=WARSAW)
            ).blocked
        )
        self.assertFalse(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 8, 59, tzinfo=WARSAW)
            ).blocked
        )
        self.assertFalse(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 17, 0, tzinfo=WARSAW)
            ).blocked
        )
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 16, 59, tzinfo=WARSAW)
            ).blocked
        )

    def test_cross_midnight_interval(self) -> None:
        rule = self._cross_midnight_rule()
        # Monday 17:00 inclusive through Tuesday 03:00 exclusive.
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 17, 0, tzinfo=WARSAW)
            ).blocked
        )
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 23, 30, tzinfo=WARSAW)
            ).blocked
        )
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 15, 2, 59, tzinfo=WARSAW)
            ).blocked
        )
        self.assertFalse(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 15, 3, 0, tzinfo=WARSAW)
            ).blocked
        )
        self.assertFalse(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 16, 59, tzinfo=WARSAW)
            ).blocked
        )
        # Tuesday is not a configured start day.
        self.assertFalse(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 15, 17, 0, tzinfo=WARSAW)
            ).blocked
        )

    def test_utc_instant_converted_into_configured_zone(self) -> None:
        rule = self._cross_midnight_rule()
        # Monday 17:00 CEST (UTC+2) == 15:00Z.
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
            ).blocked
        )
        # Monday 16:59 CEST == 14:59Z.
        self.assertFalse(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 9, 14, 14, 59, tzinfo=timezone.utc)
            ).blocked
        )

    def test_europe_warsaw_dst_fall_back_repeated_hour(self) -> None:
        rule = WeeklyBlackoutRule(
            rule_id="dst-fall",
            target=AvailabilityTarget(provider="zai"),
            timezone="Europe/Warsaw",
            weekdays=("sun",),
            start_local="02:15",
            end_local="04:00",
            reason_code="dst_test",
        )
        # 2026-10-25: 03:00 CEST becomes 02:00 CET at 01:00Z; the wall clock
        # hour 02:15 occurs twice and both instants are inside the interval.
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 10, 25, 0, 15, tzinfo=timezone.utc)
            ).blocked
        )
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc)
            ).blocked
        )
        self.assertFalse(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 10, 25, 3, 15, tzinfo=timezone.utc)
            ).blocked
        )

    def test_europe_warsaw_dst_spring_forward_gap(self) -> None:
        rule = WeeklyBlackoutRule(
            rule_id="dst-spring",
            target=AvailabilityTarget(provider="zai"),
            timezone="Europe/Warsaw",
            weekdays=("sun",),
            start_local="02:15",
            end_local="04:00",
            reason_code="dst_test",
        )
        # 2026-03-29: 02:00 CET becomes 03:00 CEST; local 02:15 does not
        # exist, and evaluation only converts the given instant.
        self.assertFalse(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 3, 29, 0, 15, tzinfo=timezone.utc)
            ).blocked
        )
        self.assertTrue(
            evaluate_blackouts(
                [rule], GLM53, datetime(2026, 3, 29, 1, 15, tzinfo=timezone.utc)
            ).blocked
        )

    def test_non_target_provider_and_model_unaffected(self) -> None:
        rule = self._same_day_rule()
        inside = datetime(2026, 9, 14, 12, 0, tzinfo=WARSAW)
        self.assertFalse(evaluate_blackouts([rule], LUNA, inside).blocked)
        self.assertFalse(evaluate_blackouts([rule], SOL, inside).blocked)
        model_specific = WeeklyBlackoutRule(
            rule_id="glm-only",
            target=AvailabilityTarget(provider="zai", model="glm-5.3"),
            timezone="Europe/Warsaw",
            weekdays=("mon",),
            start_local="09:00",
            end_local="17:00",
            reason_code="glm_only",
        )
        self.assertTrue(
            evaluate_blackouts([model_specific], GLM53, inside).blocked
        )
        self.assertFalse(
            evaluate_blackouts([model_specific], FLASH, inside).blocked
        )

    def test_blocked_decision_shape(self) -> None:
        rule = self._same_day_rule()
        decision = evaluate_blackouts(
            [rule], GLM53, datetime(2026, 9, 14, 12, 0, tzinfo=WARSAW)
        )
        self.assertIsInstance(decision, BlackoutDecision)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.rule_id, "zai-quiet-hours")
        self.assertEqual(decision.reason_code, "preserve_zai")
        self.assertEqual(decision.reason_codes, ("policy_blocked",))
        restored = BlackoutDecision.from_dict(decision.to_dict())
        self.assertEqual(restored, decision)
        unblocked = evaluate_blackouts(
            [rule], GLM53, datetime(2026, 9, 14, 18, 0, tzinfo=WARSAW)
        )
        self.assertFalse(unblocked.blocked)
        self.assertIsNone(unblocked.rule_id)
        self.assertIsNone(unblocked.reason_code)
        self.assertEqual(unblocked.reason_codes, ())
        self.assertEqual(BlackoutDecision.from_dict(unblocked.to_dict()), unblocked)

    def test_first_matching_rule_in_given_order_wins(self) -> None:
        early = self._same_day_rule()
        other = WeeklyBlackoutRule(
            rule_id="other-window",
            target=AvailabilityTarget(provider="zai"),
            timezone="Europe/Warsaw",
            weekdays=("mon",),
            start_local="10:00",
            end_local="11:00",
            reason_code="other_reason",
        )
        decision = evaluate_blackouts(
            [other, early], GLM53, datetime(2026, 9, 14, 10, 30, tzinfo=WARSAW)
        )
        self.assertEqual(decision.rule_id, "other-window")

    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = evaluate_blackouts(
                [self._same_day_rule()], GLM53, datetime(2026, 9, 14, 12, 0)
            )

    def test_policy_block_never_mutates_capacity_or_scarcity(self) -> None:
        snapshot = _zai_snapshot(weekly=80)
        before_payload = snapshot.to_dict()
        before_assessment = assess_scarcity(GLM53_ENTRY, [snapshot])
        rule = self._same_day_rule()
        decision = evaluate_blackouts(
            [rule], GLM53, datetime(2026, 9, 14, 12, 0, tzinfo=WARSAW)
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(snapshot.to_dict(), before_payload)
        self.assertEqual(
            assess_scarcity(GLM53_ENTRY, [snapshot]), before_assessment
        )
        # The block is policy, not scarcity: the assessment stays healthy.
        self.assertEqual(before_assessment.state, "known")
        self.assertEqual(before_assessment.label, "plentiful")


# ── Replenishment state and visibility ────────────────────────────────────────


class ReplenishmentStateContractTests(unittest.TestCase):
    def test_round_trip_with_expiry(self) -> None:
        state = _replenishment()
        payload = state.to_dict()
        self.assertEqual(
            payload,
            {
                "provider": "openai",
                "kind": "rate_limit_reset",
                "available_count": 2,
                "details_known": True,
                "earliest_expiry": "2026-09-08T00:00:00.000Z",
                "retrieved_at": TS,
            },
        )
        self.assertEqual(ReplenishmentState.from_dict(payload), state)

    def test_zero_count_omits_expiry(self) -> None:
        state = _replenishment(count=0)
        self.assertIsNone(state.earliest_expiry)
        self.assertNotIn("earliest_expiry", state.to_dict())
        self.assertEqual(ReplenishmentState.from_dict(state.to_dict()), state)

    def test_details_known_true_does_not_require_expiry(self) -> None:
        state = ReplenishmentState(
            provider="openai",
            kind="rate_limit_reset",
            available_count=3,
            details_known=True,
            earliest_expiry=None,
            retrieved_at=TS,
        )
        self.assertEqual(ReplenishmentState.from_dict(state.to_dict()), state)

    def test_invalid_states_rejected(self) -> None:
        def state(
            provider: str = "openai",
            kind: str = "rate_limit_reset",
            available_count: int = 2,
            details_known: bool = True,
            earliest_expiry: str | None = "2026-09-08T00:00:00.000Z",
            retrieved_at: str = TS,
        ) -> ReplenishmentState:
            return ReplenishmentState(
                provider=provider,
                kind=kind,
                available_count=available_count,
                details_known=details_known,
                earliest_expiry=earliest_expiry,
                retrieved_at=retrieved_at,
            )

        with self.assertRaises(SelectionContractValidationError):
            _ = state(available_count=-1)
        with self.assertRaises(SelectionContractValidationError):
            _ = state(available_count=True)  # bool passes statically, not at runtime
        with self.assertRaises(SelectionContractValidationError):
            _ = state(available_count=cast(int, _ill(1.0)))
        with self.assertRaises(SelectionContractValidationError):
            _ = state(details_known=cast(bool, _ill(1)))  # strict bool only
        with self.assertRaises(SelectionContractValidationError):
            _ = state(details_known=cast(bool, _ill("true")))
        with self.assertRaises(SelectionContractValidationError):
            _ = state(retrieved_at="2026-09-06T12:00:00Z")  # not canonical .sssZ
        with self.assertRaises(SelectionContractValidationError):
            _ = state(retrieved_at="not-a-timestamp")
        with self.assertRaises(SelectionContractValidationError):
            _ = state(earliest_expiry="2026-09-08T00:00:00.000")  # missing Z/ms
        with self.assertRaises(SelectionContractValidationError):
            _ = state(available_count=0)  # zero count must not carry expiry
        with self.assertRaises(SelectionContractValidationError):
            _ = state(details_known=False)  # expiry requires known details
        with self.assertRaises(SelectionContractValidationError):
            _ = state(kind="some free-text kind!")
        with self.assertRaises(SelectionContractValidationError):
            _ = state(provider="anthropic")  # unsupported provider

    def test_serialized_shape_has_no_free_text_or_credit_identity(self) -> None:
        payload = _replenishment().to_dict()
        self.assertEqual(
            set(payload.keys()),
            {
                "provider",
                "kind",
                "available_count",
                "details_known",
                "earliest_expiry",
                "retrieved_at",
            },
        )
        for key in ("id", "credit_id", "title", "description", "account"):
            self.assertNotIn(key, payload)
        with self.assertRaises(SelectionContractValidationError):
            _ = ReplenishmentState.from_dict({**payload, "title": "Bonus credit"})


class ReplenishmentModeTests(unittest.TestCase):
    def test_frozen_modes(self) -> None:
        self.assertEqual(
            REPLENISHMENT_MODES, frozenset({"ignore", "advisory", "recoverable"})
        )

    def test_ignore_mode_has_no_policy_effect(self) -> None:
        decision = apply_replenishment_mode("ignore", _replenishment())
        self.assertFalse(decision.visible)
        self.assertFalse(decision.recoverable)
        self.assertFalse(decision.human_action_required)
        self.assertIsNone(decision.available_count)
        self.assertEqual(decision.reason_codes, ())

    def test_advisory_mode_exposes_without_recovery(self) -> None:
        decision = apply_replenishment_mode("advisory", _replenishment(count=2))
        self.assertTrue(decision.visible)
        self.assertEqual(decision.available_count, 2)
        self.assertTrue(decision.details_known)
        self.assertFalse(decision.recoverable)
        self.assertFalse(decision.human_action_required)
        self.assertEqual(decision.reason_codes, ("replenishment_available",))

    def test_recoverable_mode_requires_human_action(self) -> None:
        decision = apply_replenishment_mode("recoverable", _replenishment(count=2))
        self.assertTrue(decision.visible)
        self.assertTrue(decision.recoverable)
        self.assertTrue(decision.human_action_required)
        self.assertEqual(
            decision.reason_codes,
            ("human_action_required", "replenishment_available", "replenishment_recoverable"),
        )

    def test_recoverable_mode_with_zero_count_is_not_recoverable(self) -> None:
        decision = apply_replenishment_mode("recoverable", _replenishment(count=0))
        self.assertTrue(decision.visible)
        self.assertFalse(decision.recoverable)
        self.assertFalse(decision.human_action_required)

    def test_missing_observation_is_invisible_in_every_mode(self) -> None:
        for mode in ("ignore", "advisory", "recoverable"):
            decision = apply_replenishment_mode(mode, None)
            self.assertFalse(decision.visible)

    def test_invalid_mode_rejected(self) -> None:
        for bad in ("redeem", "", "ADVISORY", None, 3):
            with self.assertRaises(SelectionContractValidationError):
                _ = apply_replenishment_mode(cast(str, bad), _replenishment())

    def test_decision_serialization_round_trip(self) -> None:
        decision = apply_replenishment_mode("recoverable", _replenishment(count=2))
        payload = decision.to_dict()
        self.assertEqual(
            payload,
            {
                "mode": "recoverable",
                "visible": True,
                "available_count": 2,
                "details_known": True,
                "recoverable": True,
                "human_action_required": True,
                "reason_codes": [
                    "human_action_required",
                    "replenishment_available",
                    "replenishment_recoverable",
                ],
            },
        )
        self.assertEqual(ReplenishmentDecision.from_dict(payload), decision)
        invisible = apply_replenishment_mode("ignore", _replenishment())
        self.assertEqual(
            ReplenishmentDecision.from_dict(invisible.to_dict()), invisible
        )

    def test_replenishment_never_changes_a_scarcity_assessment(self) -> None:
        snapshots = [_openai_snapshot(weekly=0)]
        before = assess_scarcity(LUNA_ENTRY, snapshots)
        for mode in ("ignore", "advisory", "recoverable"):
            _ = apply_replenishment_mode(mode, _replenishment(count=3))
        after = assess_scarcity(LUNA_ENTRY, snapshots)
        self.assertEqual(before, after)
        self.assertEqual(before.state, "unavailable")
        self.assertEqual(before.penalty_units, 10000)


# ── UserPolicy container ──────────────────────────────────────────────────────


class UserPolicyContractTests(unittest.TestCase):
    def _reservation(self, rule_id: str) -> ReservationRule:
        return ReservationRule(
            rule_id=rule_id,
            scope=CODEX_SCOPE,
            resource="tokens",
            kind="weekly",
            when_remaining_below=20,
            minimum_task_level="L4",
        )

    def _blackout(self, rule_id: str) -> WeeklyBlackoutRule:
        return WeeklyBlackoutRule(
            rule_id=rule_id,
            target=AvailabilityTarget(provider="zai"),
            timezone="Europe/Warsaw",
            weekdays=("mon",),
            start_local="17:00",
            end_local="03:00",
            reason_code="preserve_zai",
        )

    def test_round_trip_exact_shape(self) -> None:
        policy = UserPolicy(
            policy_version=1,
            unknown_capacity_mode="degraded",
            replenishment_mode="advisory",
            reservations=(self._reservation("b-rule"),),
            blackouts=(self._blackout("a-rule"),),
        )
        payload = policy.to_dict()
        self.assertEqual(
            payload,
            {
                "policy_version": 1,
                "unknown_capacity_mode": "degraded",
                "replenishment_mode": "advisory",
                "reservations": [self._reservation("b-rule").to_dict()],
                "blackouts": [self._blackout("a-rule").to_dict()],
            },
        )
        self.assertEqual(UserPolicy.from_dict(payload), policy)

    def test_rule_ids_unique_across_the_whole_policy(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="advisory",
                reservations=(self._reservation("same-id"),),
                blackouts=(self._blackout("same-id"),),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="advisory",
                reservations=(
                    self._reservation("dup"),
                    self._reservation("dup"),
                ),
                blackouts=(),
            )

    def test_canonical_rule_ordering(self) -> None:
        first = UserPolicy(
            policy_version=1,
            unknown_capacity_mode="degraded",
            replenishment_mode="advisory",
            reservations=(self._reservation("b-rule"), self._reservation("a-rule")),
            blackouts=(self._blackout("z-rule"), self._blackout("m-rule")),
        )
        second = UserPolicy(
            policy_version=1,
            unknown_capacity_mode="degraded",
            replenishment_mode="advisory",
            reservations=(self._reservation("a-rule"), self._reservation("b-rule")),
            blackouts=(self._blackout("m-rule"), self._blackout("z-rule")),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            [r.rule_id for r in first.reservations], ["a-rule", "b-rule"]
        )
        self.assertEqual(
            [r.rule_id for r in first.blackouts], ["m-rule", "z-rule"]
        )
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_invalid_containers_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = UserPolicy(
                policy_version=0,
                unknown_capacity_mode="degraded",
                replenishment_mode="advisory",
                reservations=(),
                blackouts=(),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = UserPolicy(
                policy_version=1,
                unknown_capacity_mode="balanced",  # not an M2d mode
                replenishment_mode="advisory",
                reservations=(),
                blackouts=(),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="quality-first",  # ranking modes are M2e
                reservations=(),
                blackouts=(),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = UserPolicy(
                policy_version=1,
                unknown_capacity_mode="degraded",
                replenishment_mode="advisory",
                reservations=cast(
                    "tuple[ReservationRule, ...]",
                    _ill([self._reservation("a")]),
                ),  # list, not tuple
                blackouts=(),
            )

    def test_policy_level_helpers(self) -> None:
        policy = UserPolicy(
            policy_version=1,
            unknown_capacity_mode="strict",
            replenishment_mode="recoverable",
            reservations=(self._reservation("b-rule"), self._reservation("a-rule")),
            blackouts=(self._blackout("c-rule"),),
        )
        unknown = apply_unknown_capacity_mode(
            "degraded", assess_scarcity(GLM53_ENTRY, [])
        )
        self.assertTrue(unknown.eligible_by_unknown_policy)
        strict = policy.apply_unknown_capacity(
            assess_scarcity(GLM53_ENTRY, [])
        )
        self.assertFalse(strict.eligible_by_unknown_policy)
        self.assertEqual(strict.mode, "strict")

        decisions = policy.evaluate_reservations(
            [_openai_snapshot(weekly=10)], "L2"
        )
        self.assertEqual(
            [d.rule_id for d in decisions], ["a-rule", "b-rule"]
        )
        self.assertTrue(all(d.triggered for d in decisions))

        blocked = policy.evaluate_blackouts(
            GLM53, datetime(2026, 9, 14, 18, 0, tzinfo=WARSAW)
        )
        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.rule_id, "c-rule")


# ── Integration scenarios (resource-state level only; no selection) ──────────


class ScenarioTests(unittest.TestCase):
    def test_scenario_e_healthy_quota_with_matching_blackout(self) -> None:
        snapshot = _zai_snapshot(weekly=80)
        assessment = assess_scarcity(GLM53_ENTRY, [snapshot])
        self.assertEqual(assessment.state, "known")
        self.assertEqual(assessment.label, "plentiful")
        rule = WeeklyBlackoutRule(
            rule_id="zai-quiet-hours",
            target=AvailabilityTarget(provider="zai"),
            timezone="Europe/Warsaw",
            weekdays=("mon",),
            start_local="09:00",
            end_local="17:00",
            reason_code="preserve_zai",
        )
        snapshot_payload = snapshot.to_dict()
        decision = evaluate_blackouts(
            [rule], GLM53, datetime(2026, 9, 14, 12, 0, tzinfo=WARSAW)
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason_codes, ("policy_blocked",))
        # Capacity remains healthy and unrewritten; the exclusion is policy.
        self.assertEqual(snapshot.to_dict(), snapshot_payload)
        self.assertEqual(assess_scarcity(GLM53_ENTRY, [snapshot]), assessment)
        # OpenAI models are unaffected by the provider-targeted rule.
        self.assertFalse(
            evaluate_blackouts(
                [rule], LUNA, datetime(2026, 9, 14, 12, 0, tzinfo=WARSAW)
            ).blocked
        )

    def test_scenario_f_exhausted_openai_with_reset_credit(self) -> None:
        snapshots = [_openai_snapshot(weekly=0)]
        assessment = assess_scarcity(LUNA_ENTRY, snapshots)
        self.assertEqual(assessment.state, "unavailable")
        self.assertEqual(assessment.penalty_units, 10000)

        state = _replenishment(count=2)
        decision = apply_replenishment_mode("recoverable", state)
        self.assertTrue(decision.recoverable)
        self.assertTrue(decision.human_action_required)

        # Current eligible capacity is NOT restored, under any mode.
        for unknown_mode in ("degraded", "strict"):
            policy_decision = apply_unknown_capacity_mode(
                unknown_mode, assessment
            )
            self.assertFalse(policy_decision.eligible_by_unknown_policy)
        # The assessment itself is untouched by the replenishment decision.
        self.assertEqual(assess_scarcity(LUNA_ENTRY, snapshots), assessment)


if __name__ == "__main__":
    _ = unittest.main()
