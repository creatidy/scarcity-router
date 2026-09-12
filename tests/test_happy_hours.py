"""D-035 happy-hour quota-preference tests.

Covers the ``WeeklyHappyHourRule`` schedule semantics (identical boundary
rules to blackouts plus optional inclusive campaign date bounds), the
``HappyHourDecision`` contract, ``UserPolicy`` integration and serialization
compatibility, the exact ranking integration (preference after the capacity
knowledge class, never an eligibility bypass), and the checked-in example
campaign rule. Deterministic and self-contained: no network, subprocess,
clock or credential access; every evaluation instant is constructed
explicitly.

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
    AvailabilityTarget,
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
    HappyHourDecision,
    ModelCatalog,
    ModelIdentity,
    SelectionContractValidationError,
    SelectionDecision,
    SelectorPolicy,
    TaskRequirement,
    UserPolicy,
    WeeklyBlackoutRule,
    WeeklyHappyHourRule,
    evaluate_happy_hours,
    neutral_selector_policy,
    select_model,
)

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"
EXAMPLE_POLICY_PATH = REPO / "examples" / "selector-policy.json"

RETRIEVED_AT = "2026-09-13T00:00:00.000Z"
SGT = ZoneInfo("Asia/Singapore")

FLASH = ModelIdentity(provider="zai", model="glm-5.3-flash", variant="max")
GLM53 = ModelIdentity(provider="zai", model="glm-5.3", variant="max")
# With no capability minima every OpenAI configuration is sufficient and
# shares one capacity scope, so the balanced tie-breaks select the lowest
# configured reasoning effort, then the first stable identity.
LUNA_MEDIUM = ModelIdentity(
    provider="openai", model="gpt-5.6-luna", variant="medium"
)


def _ill(value: object) -> object:
    """Mark a deliberately wrong-typed value for a negative-validation test."""
    return value


def _at(hour: int, minute: int = 0, day: int = 13) -> datetime:
    """An Asia/Singapore wall-clock instant (the policy converts zones)."""
    return datetime(2026, 9, day, hour, minute, tzinfo=SGT)


def _rule(
    *,
    rule_id: str = "night-campaign",
    target: AvailabilityTarget | None = None,
    weekdays: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
    start_local: str = "23:00",
    end_local: str = "09:00",
    start_date: str | None = "2026-09-01",
    end_date: str | None = "2026-09-30",
    reason_code: str = "campaign_zero_quota",
) -> WeeklyHappyHourRule:
    return WeeklyHappyHourRule(
        rule_id=rule_id,
        target=target if target is not None else AvailabilityTarget(provider="zai"),
        timezone="Asia/Singapore",
        weekdays=weekdays,
        start_local=start_local,
        end_local=end_local,
        reason_code=reason_code,
        start_date=start_date,
        end_date=end_date,
    )


def _snap(
    provider: str,
    five: int | None = 50,
    weekly: int | None = 50,
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
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=RETRIEVED_AT,
        status="ok",
        windows=tuple(windows),
        diagnostics=cast("tuple[CapacityDiagnostic, ...]", ()),
    )


def _catalog() -> ModelCatalog:
    return ModelCatalog.from_dict(
        cast(object, json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
    )


def _requirement() -> TaskRequirement:
    return TaskRequirement.from_dict(
        {
            "task_level": "L1",
            "capability_minima": {},
            "hard_constraints": {},
        }
    )


def _policy(
    *rules: WeeklyHappyHourRule,
    blackouts: tuple[WeeklyBlackoutRule, ...] = (),
) -> SelectorPolicy:
    neutral = neutral_selector_policy().resource_policy
    return SelectorPolicy(
        mode="balanced",
        resource_policy=UserPolicy(
            policy_version=neutral.policy_version,
            unknown_capacity_mode=neutral.unknown_capacity_mode,
            replenishment_mode=neutral.replenishment_mode,
            reservations=neutral.reservations,
            blackouts=blackouts,
            happy_hours=tuple(rules),
        ),
    )


def _select(
    policy: SelectorPolicy,
    snapshots: list[CapacitySnapshot],
    *,
    at: datetime | None = None,
) -> SelectionDecision:
    return select_model(
        catalog=_catalog(),
        requirement=_requirement(),
        policy=policy,
        snapshots=snapshots,
        evaluated_at=at if at is not None else _at(2),
    )


class WeeklyHappyHourRuleTests(unittest.TestCase):
    """Construction validation and schedule boundary semantics."""

    def test_weekdays_stored_canonically(self) -> None:
        rule = _rule(weekdays=("sun", "mon", "sat"))
        self.assertEqual(("mon", "sat", "sun"), rule.weekdays)

    def test_start_must_differ_from_end(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _rule(start_local="09:00", end_local="09:00")

    def test_invalid_times_weekdays_and_timezone_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _rule(start_local="24:00")
        with self.assertRaises(SelectionContractValidationError):
            _ = _rule(weekdays=())
        with self.assertRaises(SelectionContractValidationError):
            _ = _rule(weekdays=("mon", "mon"))
        with self.assertRaises(SelectionContractValidationError):
            _ = WeeklyHappyHourRule(
                rule_id="x",
                target=AvailabilityTarget(provider="zai"),
                timezone="Not/AZone",
                weekdays=("mon",),
                start_local="23:00",
                end_local="09:00",
                reason_code="r",
            )

    def test_date_bounds_must_be_ordered_and_valid(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _rule(start_date="2026-09-20", end_date="2026-09-10")
        with self.assertRaises(SelectionContractValidationError):
            _ = _rule(start_date="2026-13-01")
        with self.assertRaises(SelectionContractValidationError):
            _ = _rule(start_date="20260901")

    def test_cross_midnight_half_open_boundaries(self) -> None:
        rule = _rule(start_date=None, end_date=None)
        # Exactly at start is active; exactly at end is not; 08:59 is.
        self.assertTrue(rule.active_at(_at(23, 0)))
        self.assertTrue(rule.active_at(_at(8, 59)))
        self.assertFalse(rule.active_at(_at(9, 0)))
        self.assertFalse(rule.active_at(_at(22, 59)))

    def test_date_bounds_are_inclusive_on_both_ends(self) -> None:
        rule = _rule(start_date="2026-09-10", end_date="2026-09-20")
        self.assertTrue(rule.active_at(_at(2, day=10)))
        self.assertTrue(rule.active_at(_at(2, day=20)))
        self.assertFalse(rule.active_at(_at(2, day=9)))
        self.assertFalse(rule.active_at(_at(2, day=21)))

    def test_one_sided_date_bounds(self) -> None:
        self.assertTrue(
            _rule(start_date="2026-09-05", end_date=None).active_at(_at(2))
        )
        self.assertFalse(
            _rule(start_date="2026-09-14", end_date=None).active_at(_at(2))
        )
        self.assertFalse(
            _rule(start_date=None, end_date="2026-09-12").active_at(_at(2))
        )
        self.assertTrue(
            _rule(start_date=None, end_date="2026-09-13").active_at(_at(2))
        )

    def test_timezone_conversion_decides_local_membership(self) -> None:
        # 18:00 UTC on Sep 13 is 02:00 SGT on Sep 14: inside the nightly
        # window and inside the campaign dates.
        utc_instant = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
        self.assertTrue(_rule().active_at(utc_instant))

    def test_naive_instant_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _rule().active_at(datetime(2026, 9, 13, 2, 0))  # noqa: DTZ001

    def test_serialization_round_trip(self) -> None:
        rule = _rule()
        restored = WeeklyHappyHourRule.from_dict(rule.to_dict())
        self.assertEqual(rule, restored)
        self.assertEqual(
            {
                "rule_id": "night-campaign",
                "target": {"provider": "zai"},
                "timezone": "Asia/Singapore",
                "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start_local": "23:00",
                "end_local": "09:00",
                "reason_code": "campaign_zero_quota",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
            },
            rule.to_dict(),
        )

    def test_from_dict_rejects_unknown_keys_and_bad_shapes(self) -> None:
        document = _rule().to_dict()
        with self.assertRaises(SelectionContractValidationError):
            _ = WeeklyHappyHourRule.from_dict({**document, "extra": 1})
        with self.assertRaises(SelectionContractValidationError):
            _ = WeeklyHappyHourRule.from_dict(
                {**document, "weekdays": _ill("not-a-list")}
            )
        _ = document.pop("reason_code")
        with self.assertRaises(SelectionContractValidationError):
            _ = WeeklyHappyHourRule.from_dict(document)


class HappyHourDecisionTests(unittest.TestCase):
    """The decision contract: preferences name a rule, non-preferences do not."""

    def test_preferred_requires_rule_identity(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = HappyHourDecision(preferred=True)
        decision = HappyHourDecision(
            preferred=True, rule_id="r1", reason_code="campaign"
        )
        self.assertEqual(
            {
                "preferred": True,
                "rule_id": "r1",
                "reason_code": "campaign",
            },
            decision.to_dict(),
        )
        self.assertEqual(
            HappyHourDecision.from_dict(decision.to_dict()), decision
        )

    def test_not_preferred_names_no_rule(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = HappyHourDecision(preferred=False, rule_id="r1")
        self.assertEqual(
            {"preferred": False}, HappyHourDecision(preferred=False).to_dict()
        )


class EvaluateHappyHoursTests(unittest.TestCase):
    """Target matching and deterministic first-rule decision."""

    def test_provider_target_matches_every_provider_model(self) -> None:
        rule = _rule()
        self.assertTrue(evaluate_happy_hours((rule,), FLASH, _at(2)).preferred)
        self.assertTrue(evaluate_happy_hours((rule,), GLM53, _at(2)).preferred)

    def test_model_target_is_exact(self) -> None:
        rule = _rule(
            target=AvailabilityTarget(provider="zai", model="glm-5.3-flash")
        )
        self.assertTrue(evaluate_happy_hours((rule,), FLASH, _at(2)).preferred)
        not_preferred = evaluate_happy_hours((rule,), GLM53, _at(2))
        self.assertFalse(not_preferred.preferred)
        self.assertIsNone(not_preferred.rule_id)

    def test_first_matching_rule_in_given_order_decides(self) -> None:
        first = _rule(rule_id="a-rule", reason_code="first-reason")
        second = _rule(rule_id="b-rule", reason_code="second-reason")
        decision = evaluate_happy_hours((first, second), FLASH, _at(2))
        self.assertEqual("a-rule", decision.rule_id)
        self.assertEqual("first-reason", decision.reason_code)

    def test_outside_window_not_preferred(self) -> None:
        decision = evaluate_happy_hours((_rule(),), FLASH, _at(12))
        self.assertFalse(decision.preferred)


class UserPolicyHappyHourTests(unittest.TestCase):
    """Container integration and additive serialized compatibility."""

    def test_absent_member_loads_as_empty(self) -> None:
        document: dict[str, object] = {
            "policy_version": 1,
            "unknown_capacity_mode": "degraded",
            "replenishment_mode": "advisory",
            "reservations": [],
            "blackouts": [],
        }
        policy = UserPolicy.from_dict(cast(object, document))
        self.assertEqual((), policy.happy_hours)
        # Round-trips identically: an empty happy_hours set is never
        # serialized, so pre-D-035 documents are unchanged.
        self.assertEqual(document, policy.to_dict())

    def test_round_trip_with_happy_hours(self) -> None:
        policy = _policy(_rule(rule_id="b"), _rule(rule_id="a")).resource_policy
        restored = UserPolicy.from_dict(policy.to_dict())
        self.assertEqual(policy, restored)
        self.assertEqual(
            ["a", "b"], [rule.rule_id for rule in restored.happy_hours]
        )

    def test_rule_ids_unique_across_all_rule_kinds(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _policy(
                _rule(rule_id="same-id"),
                blackouts=(
                    WeeklyBlackoutRule(
                        rule_id="same-id",
                        target=AvailabilityTarget(provider="zai"),
                        timezone="Asia/Singapore",
                        weekdays=("mon",),
                        start_local="09:00",
                        end_local="17:00",
                        reason_code="r",
                    ),
                ),
            )

    def test_policy_level_helper(self) -> None:
        policy = _policy(_rule()).resource_policy
        decision = policy.evaluate_happy_hours(FLASH, _at(2))
        self.assertTrue(decision.preferred)
        self.assertFalse(policy.evaluate_happy_hours(FLASH, _at(12)).preferred)

    def test_member_must_be_a_list_of_rules(self) -> None:
        document: dict[str, object] = {
            "policy_version": 1,
            "unknown_capacity_mode": "degraded",
            "replenishment_mode": "advisory",
            "reservations": [],
            "blackouts": [],
            "happy_hours": _ill("not-a-list"),
        }
        with self.assertRaises(SelectionContractValidationError):
            _ = UserPolicy.from_dict(cast(object, document))
        document["happy_hours"] = [42]
        with self.assertRaises(SelectionContractValidationError):
            _ = UserPolicy.from_dict(cast(object, document))


FLASH_RULE = AvailabilityTarget(provider="zai", model="glm-5.3-flash")


class RankingIntegrationTests(unittest.TestCase):
    """The frozen balanced ranking with the D-035 preference group."""

    def test_happy_hour_beats_healthier_full_price_candidate(self) -> None:
        snapshots = [_snap("openai", 90, 90), _snap("zai", 20, 20)]
        # Outside the window, OpenAI (90%) is least scarce and wins.
        neutral = _select(neutral_selector_policy(), snapshots, at=_at(12))
        assert neutral.selected is not None
        self.assertEqual(LUNA_MEDIUM, neutral.selected.identity)
        # Inside the window, GLM-5.3-Flash is quota-preferred despite being
        # much scarcer — spending full-price OpenAI quota is what the
        # window exists to avoid.
        decision = _select(_policy(_rule(target=FLASH_RULE)), snapshots, at=_at(2))
        assert decision.selected is not None
        self.assertEqual(FLASH, decision.selected.identity)
        selected_decision = decision.selected.happy_hour_decision
        assert selected_decision is not None
        self.assertEqual("night-campaign", selected_decision.rule_id)
        # Alternatives stay in exact ranking order after the preferred one
        # and carry no preference themselves.
        self.assertEqual(LUNA_MEDIUM, decision.alternatives[0].identity)
        self.assertIsNone(decision.alternatives[0].happy_hour_decision)

    def test_serialized_decision_carries_the_preference(self) -> None:
        decision = _select(
            _policy(_rule(target=FLASH_RULE)),
            [_snap("openai"), _snap("zai")],
        )
        assert decision.selected is not None
        self.assertEqual(FLASH, decision.selected.identity)
        payload = decision.selected.to_dict()
        self.assertEqual(
            {
                "preferred": True,
                "rule_id": "night-campaign",
                "reason_code": "campaign_zero_quota",
            },
            payload["happy_hour_decision"],
        )

    def test_capability_and_hard_constraints_still_gate_inside_the_window(
        self,
    ) -> None:
        requirement = TaskRequirement.from_dict(
            {
                "task_level": "L1",
                "capability_minima": {},
                "hard_constraints": {"required_provider": "openai"},
            }
        )
        decision = select_model(
            catalog=_catalog(),
            requirement=requirement,
            policy=_policy(_rule(target=FLASH_RULE)),
            snapshots=[_snap("openai", 20, 20), _snap("zai", 95, 95)],
            evaluated_at=_at(2),
        )
        assert decision.selected is not None
        self.assertEqual(LUNA_MEDIUM, decision.selected.identity)
        excluded = {
            (c.identity.provider, c.identity.model): c for c in decision.excluded
        }
        flash = excluded[("zai", "glm-5.3-flash")]
        self.assertEqual("hard_constraint", flash.exclusion_stage)
        self.assertIsNone(flash.happy_hour_decision)

    def test_exhaustion_still_blocks_inside_the_window(self) -> None:
        decision = _select(
            _policy(_rule(target=FLASH_RULE)),
            [_snap("openai", 30, 30), _snap("zai", 0, 0)],
        )
        assert decision.selected is not None
        self.assertEqual(LUNA_MEDIUM, decision.selected.identity)
        excluded = {
            (c.identity.provider, c.identity.model): c for c in decision.excluded
        }
        flash = excluded[("zai", "glm-5.3-flash")]
        self.assertEqual("capacity", flash.exclusion_stage)
        self.assertIsNone(flash.happy_hour_decision)

    def test_known_capacity_still_ranks_ahead_of_unknown_preferred(self) -> None:
        # Only the OpenAI snapshot exists: Z.ai capacity is unknown, and the
        # capacity knowledge class still dominates the happy-hour group.
        decision = _select(
            _policy(_rule(target=FLASH_RULE)),
            [_snap("openai", 30, 30)],
        )
        assert decision.selected is not None
        self.assertEqual(LUNA_MEDIUM, decision.selected.identity)
        self.assertFalse(decision.degraded)

    def test_blackout_wins_over_overlapping_happy_hour(self) -> None:
        # An almost-all-day blackout overlaps the nightly happy hour at
        # 02:00 SGT: the hard exclusion wins and the preference can never
        # resurrect the candidate.
        blackout = WeeklyBlackoutRule(
            rule_id="all-day",
            target=AvailabilityTarget(provider="zai"),
            timezone="Asia/Singapore",
            weekdays=("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
            start_local="00:00",
            end_local="23:59",
            reason_code="preserve_zai_offpeak",
        )
        decision = _select(
            _policy(
                _rule(target=FLASH_RULE),
                blackouts=(blackout,),
            ),
            [_snap("openai", 30, 30), _snap("zai", 95, 95)],
        )
        assert decision.selected is not None
        self.assertEqual(LUNA_MEDIUM, decision.selected.identity)
        excluded = {
            (c.identity.provider, c.identity.model): c for c in decision.excluded
        }
        flash = excluded[("zai", "glm-5.3-flash")]
        self.assertEqual("policy_blackout", flash.exclusion_stage)
        self.assertIsNone(flash.happy_hour_decision)

    def test_ended_campaign_no_longer_prefers_and_is_noted(self) -> None:
        rule = _rule(target=FLASH_RULE, start_date="2026-09-01", end_date="2026-09-12")
        decision = _select(
            _policy(rule),
            [_snap("openai", 90, 90), _snap("zai", 20, 20)],
            at=_at(2),
        )
        assert decision.selected is not None
        self.assertEqual(LUNA_MEDIUM, decision.selected.identity)
        self.assertIsNone(decision.selected.happy_hour_decision)
        # The weekly window would cover 02:00 SGT; only the date bounds
        # exclude it, so the decision names the expired rule (explanation
        # only).
        self.assertEqual(("night-campaign",), decision.expired_happy_hour_rules)
        self.assertEqual(
            ["night-campaign"], decision.to_dict()["expired_happy_hour_rules"]
        )

    def test_active_campaign_carries_no_expiry_note(self) -> None:
        decision = _select(
            _policy(_rule(target=FLASH_RULE)),
            [_snap("openai", 90, 90), _snap("zai", 20, 20)],
            at=_at(2),
        )
        self.assertEqual((), decision.expired_happy_hour_rules)
        self.assertNotIn("expired_happy_hour_rules", decision.to_dict())
        # An inactive rule whose weekly window does not cover the instant
        # is ordinary schedule behavior, never an expiry note.
        outside = _select(
            _policy(_rule(target=FLASH_RULE, start_date=None, end_date=None)),
            [_snap("openai", 90, 90), _snap("zai", 20, 20)],
            at=_at(12),
        )
        self.assertEqual((), outside.expired_happy_hour_rules)


class DateExpiryTests(unittest.TestCase):
    """The explanation-only date-expiry signal for campaign rules."""

    def test_window_covering_but_dates_excluding_is_expired(self) -> None:
        rule = _rule(
            start_local="00:00",
            end_local="23:59",
            start_date="2026-09-01",
            end_date="2026-09-05",
        )
        # The weekly window covers all day, but Sep 14 is after end_date.
        self.assertTrue(rule.is_date_expired_at(_at(15)))
        # Inside the date bounds it is simply active.
        self.assertFalse(rule.is_date_expired_at(_at(15, day=3)))
        # Before start_date it is expired too.
        self.assertTrue(
            rule.is_date_expired_at(datetime(2026, 8, 25, 15, 0, tzinfo=SGT))
        )

    def test_window_not_covering_is_never_expired(self) -> None:
        rule = _rule(start_date="2026-09-01", end_date="2026-09-05")
        # 15:00 SGT is outside the nightly window: ordinary schedule
        # behavior, not a notable expiry.
        self.assertFalse(rule.is_date_expired_at(_at(15)))

    def test_no_date_bounds_never_expired(self) -> None:
        rule = _rule(start_date=None, end_date=None)
        self.assertFalse(rule.is_date_expired_at(_at(2)))
        self.assertFalse(rule.is_date_expired_at(_at(15)))


class ExamplePolicyTests(unittest.TestCase):
    """The checked-in owner example encodes the campaign window."""

    def _happy_hours(self) -> tuple[WeeklyHappyHourRule, ...]:
        policy = SelectorPolicy.from_dict(
            cast(object, json.loads(EXAMPLE_POLICY_PATH.read_text(encoding="utf-8")))
        )
        return policy.resource_policy.happy_hours

    def test_example_loads_and_carries_the_campaign_rule(self) -> None:
        happy_hours = self._happy_hours()
        self.assertEqual(1, len(happy_hours))
        rule = happy_hours[0]
        self.assertEqual("zai-flash-campaign-night-sgt", rule.rule_id)
        self.assertEqual(
            ("zai", "glm-5.3-flash"), (rule.target.provider, rule.target.model)
        )
        self.assertEqual("2026-09-03", rule.start_date)
        self.assertEqual("2026-09-20", rule.end_date)
        self.assertEqual(
            ("mon", "tue", "wed", "thu", "fri", "sat", "sun"), rule.weekdays
        )
        self.assertEqual(("23:00", "09:00"), (rule.start_local, rule.end_local))

    def test_campaign_window_and_period_boundaries(self) -> None:
        happy_hours = self._happy_hours()
        # 2026-09-13 is inside the campaign; 02:00 SGT is inside the window.
        self.assertTrue(evaluate_happy_hours(happy_hours, FLASH, _at(2)).preferred)
        self.assertFalse(
            evaluate_happy_hours(happy_hours, FLASH, _at(12)).preferred
        )
        # Exactly at the 09:00 SGT end boundary the window is over.
        self.assertFalse(evaluate_happy_hours(happy_hours, FLASH, _at(9)).preferred)
        # GLM-5.3 is never targeted by the campaign rule.
        self.assertFalse(
            evaluate_happy_hours(happy_hours, GLM53, _at(2)).preferred
        )
        # The end date is inclusive; the day after the campaign it is over
        # (2026-09-20 18:30 UTC is 2026-09-21 02:30 SGT).
        after = datetime(2026, 9, 20, 18, 30, tzinfo=timezone.utc)
        self.assertFalse(evaluate_happy_hours(happy_hours, FLASH, after).preferred)
        # Before the campaign start it had not begun (2026-09-01 18:30 UTC
        # is 2026-09-02 02:30 SGT).
        before = datetime(2026, 9, 1, 18, 30, tzinfo=timezone.utc)
        self.assertFalse(evaluate_happy_hours(happy_hours, FLASH, before).preferred)


if __name__ == "__main__":
    _ = unittest.main()
