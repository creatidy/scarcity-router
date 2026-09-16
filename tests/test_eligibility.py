"""Contract and parser tests for the D-039 execution-eligibility surface."""

from __future__ import annotations

import unittest

from scarcity_router.eligibility import (
    ELIGIBILITY_SCHEMA_VERSION,
    ExecutionEligibility,
)
from scarcity_router.errors import CapacityValidationError
from scarcity_router.providers.openai_eligibility import (
    parse_codex_execution_eligibility,
)

RETRIEVED_AT = "2026-09-16T09:00:00.000Z"


def _eligibility(
    state: str = "eligible",
    codes: tuple[str, ...] = (),
) -> ExecutionEligibility:
    return ExecutionEligibility(
        schema_version=ELIGIBILITY_SCHEMA_VERSION,
        provider="openai",
        source="codex_app_server",
        retrieved_at=RETRIEVED_AT,
        state=state,
        reason_codes=codes,
    )


def _result(**overrides: object) -> dict[str, object]:
    """A valid current-generation rate-limits result (safe fixture values)."""
    envelope: dict[str, object] = {
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "limitId": "codex",
            "planType": "prolite",
            "spendControlReached": False,
            "credits": {"hasCredits": False, "unlimited": False, "balance": None},
            "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 1758000000},
            "secondary": {"usedPercent": 10, "windowDurationMins": 10_080, "resetsAt": 1758000000},
        },
    }
    envelope.update(overrides)
    return envelope


def _state_of(result: object) -> ExecutionEligibility:
    return parse_codex_execution_eligibility(result, retrieved_at=RETRIEVED_AT)


class ContractTests(unittest.TestCase):
    def test_serialization_round_trip_is_exact(self) -> None:
        report = _eligibility(
            "policy_blocked", ("purchased_credits_present", "upsell_present")
        )
        restored = ExecutionEligibility.from_dict(report.to_dict())
        self.assertEqual(report, restored)
        self.assertEqual(report, report.validate())

    def test_reason_codes_must_be_sorted_and_unique(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = _eligibility("unknown", ("upsell_present", "credits_state_unknown"))
        with self.assertRaises(CapacityValidationError):
            _ = _eligibility("allowance_unavailable", ("upsell_present", "upsell_present"))

    def test_eligible_carries_no_codes_and_failure_requires_codes(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = _eligibility("eligible", ("upsell_present",))
        with self.assertRaises(CapacityValidationError):
            _ = _eligibility("unknown", ())

    def test_closed_vocabularies_reject_unknown_members(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = _eligibility("blocked", ())
        with self.assertRaises(CapacityValidationError):
            _ = _eligibility("unknown", ("credits_present",))

    def test_rejects_wrong_schema_version_and_unknown_keys(self) -> None:
        payload = _eligibility().to_dict()
        payload["schema_version"] = 2
        with self.assertRaises(CapacityValidationError):
            _ = ExecutionEligibility.from_dict(payload)
        payload = _eligibility().to_dict()
        payload["balance"] = "12.50"
        with self.assertRaises(CapacityValidationError):
            _ = ExecutionEligibility.from_dict(payload)

    def test_canonical_timestamp_and_safe_ids_enforced(self) -> None:
        report = _eligibility()
        bad = report.to_dict()
        bad["retrieved_at"] = "2026-09-16T09:00:00Z"
        with self.assertRaises(CapacityValidationError):
            _ = ExecutionEligibility.from_dict(bad)


class ParserTests(unittest.TestCase):
    def test_clean_current_generation_result_is_eligible(self) -> None:
        self.assertEqual(_state_of(_result()), _eligibility())

    def test_purchased_credits_present_is_policy_blocked_despite_healthy_windows(
        self,
    ) -> None:
        result = _result(
            rateLimits={
                "limitId": "codex",
                "planType": "prolite",
                "spendControlReached": False,
                "credits": {"hasCredits": True, "unlimited": False, "balance": "12.50"},
                "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 1758000000},
                "secondary": {"usedPercent": 1, "windowDurationMins": 10_080, "resetsAt": 1758000000},
            }
        )
        report = _state_of(result)
        self.assertEqual(report.state, "policy_blocked")
        self.assertEqual(report.reason_codes, ("purchased_credits_present",))
        # The balance itself is never surfaced.
        self.assertNotIn("12.50", str(report.to_dict()))

    def test_missing_credits_is_unknown(self) -> None:
        result = _result()
        limits = result["rateLimits"]
        assert isinstance(limits, dict)
        del limits["credits"]
        report = _state_of(result)
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("credits_state_unknown",))

    def test_null_and_malformed_credits_are_unknown(self) -> None:
        null_credits = _result()
        limits = null_credits["rateLimits"]
        assert isinstance(limits, dict)
        limits["credits"] = None
        self.assertEqual(
            _state_of(null_credits).reason_codes, ("credits_state_unknown",)
        )
        malformed_credits = _result()
        limits = malformed_credits["rateLimits"]
        assert isinstance(limits, dict)
        limits["credits"] = {"hasCredits": "true", "unlimited": False}
        self.assertEqual(
            _state_of(malformed_credits).reason_codes, ("credits_state_unknown",)
        )

    def test_ordinary_usage_false_blocks_allowance(self) -> None:
        report = _state_of(_result(ordinaryUsageAllowed=False))
        self.assertEqual(report.state, "allowance_unavailable")
        self.assertEqual(report.reason_codes, ("ordinary_usage_not_allowed",))

    def test_ordinary_usage_null_is_unknown(self) -> None:
        report = _state_of(_result(ordinaryUsageAllowed=None))
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("ordinary_usage_unknown",))

    def test_legacy_generation_without_permission_member_is_unknown(self) -> None:
        result = _result()
        del result["ordinaryUsageAllowed"]
        report = _state_of(result)
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("ordinary_usage_unknown",))

    def test_spend_control_reached_blocks_allowance_and_unknown_fails_closed(
        self,
    ) -> None:
        reached = _result()
        limits = reached["rateLimits"]
        assert isinstance(limits, dict)
        limits["spendControlReached"] = True
        report = _state_of(reached)
        self.assertEqual(report.state, "allowance_unavailable")
        self.assertEqual(report.reason_codes, ("spend_control_reached",))

        unknown = _result()
        limits = unknown["rateLimits"]
        assert isinstance(limits, dict)
        limits["spendControlReached"] = None
        report = _state_of(unknown)
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("spend_control_state_unknown",))

    def test_reached_type_blocks_allowance(self) -> None:
        result = _result()
        limits = result["rateLimits"]
        assert isinstance(limits, dict)
        limits["rateLimitReachedType"] = "rate_limit_reached"
        report = _state_of(result)
        self.assertEqual(report.state, "allowance_unavailable")
        self.assertEqual(report.reason_codes, ("rate_limit_reached",))

    def test_exhausted_individual_limit_blocks_allowance(self) -> None:
        result = _result()
        limits = result["rateLimits"]
        assert isinstance(limits, dict)
        limits["individualLimit"] = {
            "limit": "10.00",
            "used": "10.00",
            "remainingPercent": 0,
            "resetsAt": 1758000000,
        }
        report = _state_of(result)
        self.assertEqual(report.state, "allowance_unavailable")
        self.assertEqual(report.reason_codes, ("individual_limit_exhausted",))

    def test_upsell_presence_blocks_allowance_without_inspecting_contents(
        self,
    ) -> None:
        report = _state_of(_result(rateLimitUpsell={"button": "buy"}))
        self.assertEqual(report.state, "allowance_unavailable")
        self.assertEqual(report.reason_codes, ("upsell_present",))
        self.assertNotIn("buy", str(report.to_dict()))

    def test_main_window_at_100_percent_is_allowance_exhaustion(self) -> None:
        result = _result()
        limits = result["rateLimits"]
        assert isinstance(limits, dict)
        limits["primary"] = {
            "usedPercent": 100,
            "windowDurationMins": 300,
            "resetsAt": 1758000000,
        }
        report = _state_of(result)
        self.assertEqual(report.state, "allowance_unavailable")
        self.assertEqual(report.reason_codes, ("included_window_exhausted",))

    def test_non_integer_ordinary_usage_is_telemetry_invalid(self) -> None:
        report = _state_of(_result(ordinaryUsageAllowed="yes"))
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("telemetry_invalid",))

    def test_structural_drift_is_telemetry_invalid(self) -> None:
        report = _state_of({"unexpected": {"structured": True}})
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("telemetry_invalid",))
        report = _state_of(_result(rateLimits="nope"))
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("telemetry_invalid",))
        report = _state_of(["not", "a", "mapping"])
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("telemetry_invalid",))

    def test_policy_blocked_dominates_unknown_mandatory_fields(self) -> None:
        result = _result()
        limits = result["rateLimits"]
        assert isinstance(limits, dict)
        limits["credits"] = {"hasCredits": True, "unlimited": False, "balance": None}
        limits["spendControlReached"] = None
        report = _state_of(result)
        self.assertEqual(report.state, "policy_blocked")
        self.assertEqual(
            report.reason_codes,
            ("purchased_credits_present", "spend_control_state_unknown"),
        )

    def test_telemetry_invalid_short_circuits_state_to_unknown(self) -> None:
        result = _result()
        limits = result["rateLimits"]
        assert isinstance(limits, dict)
        limits["credits"] = {"hasCredits": True, "unlimited": False, "balance": None}
        limits["rateLimitReachedType"] = "camelCaseDrift"
        report = _state_of(result)
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.reason_codes, ("telemetry_invalid",))

    def test_all_blockers_accumulate_reason_codes(self) -> None:
        result = _result()
        limits = result["rateLimits"]
        assert isinstance(limits, dict)
        limits["spendControlReached"] = True
        limits["rateLimitReachedType"] = "rate_limit_reached"
        report = _state_of(result)
        self.assertEqual(report.state, "allowance_unavailable")
        self.assertEqual(
            report.reason_codes,
            ("rate_limit_reached", "spend_control_reached"),
        )


if __name__ == "__main__":
    _ = unittest.main()
