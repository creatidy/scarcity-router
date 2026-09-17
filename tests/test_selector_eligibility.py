"""Selector execution-eligibility gate tests (D-039).

The eligibility gate excludes every catalog entry of a provider whose report
is not ``eligible`` BEFORE any other stage, so the selector can pick the next
safe candidate. Absence of a report keeps the pre-D-039 behavior unchanged.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scarcity_router.capacity import CapacitySnapshot, CapacityWindow
from scarcity_router.eligibility import ELIGIBILITY_SCHEMA_VERSION, ExecutionEligibility
from scarcity_router.errors import SelectionContractValidationError
from scarcity_router.policy import ReplenishmentState
from scarcity_router.selector import (
    EXCLUSION_STAGES,
    CandidateEvaluation,
    SelectionDecision,
    neutral_selector_policy,
    select_model,
)
from scarcity_router.selection_types import ModelCatalog, TaskProfileCatalog

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "model-catalog.json"
POLICY_PATH = REPO_ROOT / "model-policy.json"

FIXED_AT = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
RETRIEVED_AT = "2026-09-16T12:00:00.000Z"


def _load_catalog() -> ModelCatalog:
    payload = cast(
        "dict[str, object]",
        json.loads(CATALOG_PATH.read_text(encoding="utf-8")),
    )
    return ModelCatalog.from_dict(payload)


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


def _snap(provider: str, five: int = 50, weekly: int = 50) -> CapacitySnapshot:
    scope = "codex" if provider == "openai" else "coding_plan"
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=RETRIEVED_AT,
        status="ok",
        windows=(
            CapacityWindow(
                resource="tokens",
                kind="five_hour",
                scope_id=scope,
                duration_seconds=18_000,
                used_percent=100 - five,
                remaining_percent=five,
                window_id=f"{provider}-five",
            ),
            CapacityWindow(
                resource="tokens",
                kind="weekly",
                scope_id=scope,
                duration_seconds=604_800,
                used_percent=100 - weekly,
                remaining_percent=weekly,
                window_id=f"{provider}-weekly",
            ),
        ),
        diagnostics=(),
    )


def _report(
    state: str,
    codes: tuple[str, ...],
) -> ExecutionEligibility:
    return ExecutionEligibility(
        schema_version=ELIGIBILITY_SCHEMA_VERSION,
        provider="openai",
        source="codex_app_server",
        retrieved_at=RETRIEVED_AT,
        state=state,
        reason_codes=codes,
    )


def _select(
    snapshots: list[CapacitySnapshot],
    eligibility: tuple[ExecutionEligibility, ...] = (),
) -> SelectionDecision:
    return select_model(
        catalog=CATALOG,
        requirement=PROFILES.resolve("routine_coding"),
        policy=neutral_selector_policy(),
        snapshots=snapshots,
        evaluated_at=FIXED_AT,
        eligibility_reports=eligibility,
    )


def _excluded(
    decision: SelectionDecision,
) -> dict[tuple[str, str, str], CandidateEvaluation]:
    return {
        (c.identity.provider, c.identity.model, c.identity.variant): c
        for c in decision.excluded
    }


class EligibilityGateTests(unittest.TestCase):
    def test_policy_blocked_excludes_openai_and_zai_is_selected(self) -> None:
        decision = _select(
            [_snap("openai"), _snap("zai")],
            (_report("policy_blocked", ("purchased_credits_present",)),),
        )
        assert decision.selected is not None
        self.assertEqual(decision.selected.identity.provider, "zai")
        excluded = _excluded(decision)
        openai_keys = [key for key in excluded if key[0] == "openai"]
        self.assertGreater(len(openai_keys), 0)
        for key in openai_keys:
            evaluation = excluded[key]
            self.assertEqual(evaluation.exclusion_stage, "execution")
            self.assertEqual(evaluation.reason_codes, ("execution_policy_blocked",))
            assert evaluation.execution_eligibility is not None
            self.assertEqual(
                evaluation.execution_eligibility.state, "policy_blocked"
            )

    def test_unverified_excludes_openai_fail_closed(self) -> None:
        decision = _select(
            [_snap("openai"), _snap("zai")],
            (_report("unknown", ("credits_state_unknown",)),),
        )
        assert decision.selected is not None
        self.assertEqual(decision.selected.identity.provider, "zai")
        evaluation = _excluded(decision)[("openai", "gpt-5.6-luna", "medium")]
        self.assertEqual(evaluation.exclusion_stage, "execution")
        self.assertEqual(evaluation.reason_codes, ("execution_unverified",))
        self.assertIsNotNone(evaluation.execution_eligibility)

    def test_allowance_unavailable_excludes_openai(self) -> None:
        decision = _select(
            [_snap("openai"), _snap("zai")],
            (_report("allowance_unavailable", ("included_window_exhausted",)),),
        )
        assert decision.selected is not None
        self.assertEqual(decision.selected.identity.provider, "zai")
        evaluation = _excluded(decision)[("openai", "gpt-5.6-luna", "medium")]
        self.assertEqual(
            evaluation.reason_codes, ("execution_allowance_unavailable",)
        )

    def test_eligible_report_preserves_baseline_selection(self) -> None:
        baseline = _select([_snap("openai"), _snap("zai", 98, 0)])
        gated = _select(
            [_snap("openai"), _snap("zai", 98, 0)],
            (_report("eligible", ()),),
        )
        assert baseline.selected is not None
        assert gated.selected is not None
        self.assertEqual(baseline.selected.identity, gated.selected.identity)
        for key, evaluation in _excluded(gated).items():
            self.assertNotEqual("execution", evaluation.exclusion_stage, key)

    def test_openai_still_selected_when_eligible_and_zai_lacks_capacity(self) -> None:
        decision = _select(
            [_snap("openai"), _snap("zai", 98, 0)],
            (_report("eligible", ()),),
        )
        assert decision.selected is not None
        self.assertEqual(decision.selected.identity.provider, "openai")

    def test_both_providers_excluded_yields_no_solution(self) -> None:
        decision = _select(
            [_snap("openai", 0, 0), _snap("zai", 0, 0)],
            (_report("allowance_unavailable", ("included_window_exhausted",)),),
        )
        self.assertIsNone(decision.selected)
        self.assertIn("no_eligible_candidate", decision.reason_codes)

    def test_execution_stage_sorts_first(self) -> None:
        self.assertEqual(EXCLUSION_STAGES[0], "execution")

    def test_duplicate_reports_are_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _select(
                [_snap("openai"), _snap("zai")],
                (
                    _report("eligible", ()),
                    replace(_report("eligible", ()), provider="openai"),
                ),
            )

    def test_no_reports_matches_documented_baseline(self) -> None:
        without_gate = _select([_snap("openai"), _snap("zai", 98, 0)])
        assert without_gate.selected is not None
        self.assertEqual(without_gate.selected.identity.provider, "openai")

    def test_replenishment_report_is_unaffected_by_gate(self) -> None:
        state = ReplenishmentState(
            provider="openai",
            kind="rate_limit_reset",
            available_count=1,
            details_known=True,
            earliest_expiry="2026-09-16T18:00:00.000Z",
            retrieved_at=RETRIEVED_AT,
        )
        decision = select_model(
            catalog=CATALOG,
            requirement=PROFILES.resolve("routine_coding"),
            policy=neutral_selector_policy(),
            snapshots=[_snap("openai"), _snap("zai")],
            evaluated_at=FIXED_AT,
            replenishment_states=(state,),
            eligibility_reports=(
                _report("allowance_unavailable", ("included_window_exhausted",)),
            ),
        )
        _ = state
        _ = state
        assert decision.selected is not None
        self.assertEqual(decision.selected.identity.provider, "zai")


if __name__ == "__main__":
    _ = unittest.main()
