"""Deterministic behavior tests for the dated delegated-model governance policy.

D-060 (#156): the standing Flash-only delegated rule for Z.ai work and its
dated campaign suspension (``model-policy.json`` ``delegated_model_policy``)
are evaluated by ``tools/model_governance.py`` with an injectable clock.
Every instant below is fixed, so no test depends on today's date. Boundary
instants reuse the D-059 convention: inclusive local calendar dates in
Asia/Singapore, so the last covered instant is 2026-10-07 23:59:59+08:00
and the standing rule is back in force from 2026-10-08 00:00:00+08:00.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast, override

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "model_governance.py"
POLICY_PATH = REPO_ROOT / "model-policy.json"

# Standing-phase instants (normal governance applies).
STANDING_EARLY = datetime.fromisoformat("2026-09-20T09:00:00+08:00")
STANDING_LAST_INSTANT = datetime.fromisoformat("2026-09-27T23:59:59+08:00")
# Override-phase instants (temporary campaign suspension active).
OVERRIDE_FIRST_INSTANT = datetime.fromisoformat("2026-09-28T00:00:00+08:00")
OVERRIDE_NOMINAL_PEAK = datetime.fromisoformat("2026-09-30T15:00:00+08:00")
OVERRIDE_LAST_INSTANT = datetime.fromisoformat("2026-10-07T23:59:59+08:00")
# Expiry boundary (D-059 convention: ordinary behavior resumes Oct 8 SGT).
STANDING_RESUMED_FIRST_INSTANT = datetime.fromisoformat(
    "2026-10-08T00:00:00+08:00"
)
STANDING_RESUMED_PEAK = datetime.fromisoformat("2026-10-08T14:00:00+08:00")

STANDING_RULE = "zai_delegated_flash_only"
CAMPAIGN_OVERRIDE = "zai_glm53_all_day_off_peak_2026"


class ModelRefT(Protocol):
    provider: str
    model: str

    def label(self) -> str: ...


class StandingRuleT(Protocol):
    rule_id: str
    statement: str
    restricted_models: tuple[ModelRefT, ...]
    substitute_model: ModelRefT


class TemporaryOverrideT(Protocol):
    override_id: str
    suspends: str


class DelegatedModelPolicyT(Protocol):
    calendar: str
    preference_default: ModelRefT
    standing_rules: tuple[StandingRuleT, ...]
    temporary_overrides: tuple[TemporaryOverrideT, ...]


class ModelVerdictT(Protocol):
    requested: ModelRefT
    allowed: bool
    restricted_by_rule_ids: tuple[str, ...]
    active_override_ids: tuple[str, ...]
    allowed_by_override_ids: tuple[str, ...]
    substitute: ModelRefT | None
    disclosure_required: bool
    notes: tuple[str, ...]

    def to_json(self) -> dict[str, object]: ...


class GovernanceEvaluationT(Protocol):
    evaluated_at: datetime
    local_date: date
    calendar: str
    standing_rule_ids_in_force: tuple[str, ...]
    suspended_rule_ids: tuple[str, ...]
    active_override_ids: tuple[str, ...]
    preference_default: ModelRefT

    def to_json(self) -> dict[str, object]: ...


class ModelRefCtor(Protocol):
    def __call__(self, *, provider: str, model: str) -> ModelRefT: ...


class ToolModule(Protocol):
    ModelRef: ModelRefCtor

    def load_policy_document(
        self, text: str, *, label: str
    ) -> dict[str, object]: ...

    def validate_delegated_model_policy(
        self, section: object
    ) -> DelegatedModelPolicyT: ...

    def evaluate_delegated_model_policy(
        self, policy: DelegatedModelPolicyT, *, instant: datetime
    ) -> GovernanceEvaluationT: ...

    def verdict_for(
        self,
        policy: DelegatedModelPolicyT,
        evaluation: GovernanceEvaluationT,
        requested: ModelRefT,
    ) -> ModelVerdictT: ...


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("model_governance", TOOL_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("model_governance tool module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MG = cast(ToolModule, cast(object, _load_tool()))


def _policy_from_artifact() -> DelegatedModelPolicyT:
    document = MG.load_policy_document(
        POLICY_PATH.read_text(encoding="utf-8"), label=str(POLICY_PATH)
    )
    section = document.get("delegated_model_policy")
    return MG.validate_delegated_model_policy(section)


def _evaluate(
    policy: DelegatedModelPolicyT, instant: datetime
) -> GovernanceEvaluationT:
    return MG.evaluate_delegated_model_policy(policy, instant=instant)


def _verdict(
    policy: DelegatedModelPolicyT, instant: datetime, model: str
) -> ModelVerdictT:
    evaluation = _evaluate(policy, instant)
    provider, _, name = model.partition("/")
    return MG.verdict_for(
        policy,
        evaluation,
        MG.ModelRef(provider=provider, model=name),
    )


class StandingPolicyTests(unittest.TestCase):
    """Before the relaxation (and after it expires): the Flash-only rule."""

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.policy: DelegatedModelPolicyT = cast(DelegatedModelPolicyT, object())

    @override
    def setUp(self) -> None:
        self.policy = _policy_from_artifact()

    def test_plain_glm53_follows_the_standing_rule_before_the_override(self) -> None:
        for instant in (STANDING_EARLY, STANDING_LAST_INSTANT):
            with self.subTest(instant=instant):
                verdict = _verdict(self.policy, instant, "zai/glm-5.3")
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.restricted_by_rule_ids, (STANDING_RULE,))
                self.assertEqual(verdict.allowed_by_override_ids, ())
                self.assertIsNotNone(verdict.substitute)
                assert verdict.substitute is not None
                self.assertEqual(verdict.substitute.model, "glm-5.3-flash")
                self.assertTrue(verdict.disclosure_required)

    def test_flash_remains_available_under_the_standing_policy(self) -> None:
        verdict = _verdict(self.policy, STANDING_EARLY, "zai/glm-5.3-flash")
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.restricted_by_rule_ids, ())
        self.assertFalse(verdict.disclosure_required)

    def test_no_overrides_are_active_in_the_standing_phase(self) -> None:
        evaluation = _evaluate(self.policy, STANDING_EARLY)
        self.assertEqual(evaluation.active_override_ids, ())
        self.assertEqual(evaluation.suspended_rule_ids, ())
        self.assertEqual(evaluation.standing_rule_ids_in_force, (STANDING_RULE,))
        self.assertEqual(evaluation.preference_default.model, "glm-5.3-flash")


class OverrideActiveTests(unittest.TestCase):
    """During the temporary campaign suspension (2026-09-28..2026-10-07)."""

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.policy: DelegatedModelPolicyT = cast(DelegatedModelPolicyT, object())

    @override
    def setUp(self) -> None:
        self.policy = _policy_from_artifact()

    def test_glm53_is_eligible_throughout_the_override_window(self) -> None:
        for instant in (
            OVERRIDE_FIRST_INSTANT,
            OVERRIDE_NOMINAL_PEAK,
            OVERRIDE_LAST_INSTANT,
        ):
            with self.subTest(instant=instant):
                verdict = _verdict(self.policy, instant, "zai/glm-5.3")
                self.assertTrue(verdict.allowed)
                self.assertEqual(
                    verdict.allowed_by_override_ids, (CAMPAIGN_OVERRIDE,)
                )
                self.assertEqual(verdict.restricted_by_rule_ids, ())
                self.assertIsNone(verdict.substitute)
                self.assertFalse(verdict.disclosure_required)

    def test_explicit_glm53_selection_is_preserved_not_substituted(self) -> None:
        # The #156 regression: an explicit owner/task selection of plain
        # GLM-5.3 must not be silently replaced by Flash while the override
        # is active.
        verdict = _verdict(self.policy, OVERRIDE_NOMINAL_PEAK, "zai/glm-5.3")
        self.assertTrue(verdict.allowed)
        self.assertIsNone(verdict.substitute)
        self.assertIn("eligibility and choice, not preference", verdict.notes[0])

    def test_the_old_flash_only_rule_no_longer_restricts_during_override(self) -> None:
        evaluation = _evaluate(self.policy, OVERRIDE_NOMINAL_PEAK)
        self.assertEqual(evaluation.active_override_ids, (CAMPAIGN_OVERRIDE,))
        self.assertEqual(evaluation.suspended_rule_ids, (STANDING_RULE,))
        self.assertEqual(evaluation.standing_rule_ids_in_force, ())
        self.assertEqual(evaluation.preference_default.model, "glm-5.3-flash")

    def test_flash_stays_eligible_and_remains_the_preference_default(self) -> None:
        # Allowing GLM-5.3 restores eligibility and choice — it does not
        # make the stronger model the default for every task.
        verdict = _verdict(self.policy, OVERRIDE_NOMINAL_PEAK, "zai/glm-5.3-flash")
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.allowed_by_override_ids, ())
        self.assertEqual(
            [note for note in verdict.notes if "not a preference default" in note],
            [],
        )
        glm_verdict = _verdict(self.policy, OVERRIDE_NOMINAL_PEAK, "zai/glm-5.3")
        self.assertTrue(
            any("not a preference default" in note for note in glm_verdict.notes)
        )


class ExpiryBoundaryTests(unittest.TestCase):
    """The standing rule returns automatically after the campaign boundary."""

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.policy: DelegatedModelPolicyT = cast(DelegatedModelPolicyT, object())

    @override
    def setUp(self) -> None:
        self.policy = _policy_from_artifact()

    def test_standing_rule_resumes_on_october_8_sgt(self) -> None:
        for instant in (
            STANDING_RESUMED_FIRST_INSTANT,
            STANDING_RESUMED_PEAK,
        ):
            with self.subTest(instant=instant):
                evaluation = _evaluate(self.policy, instant)
                self.assertEqual(evaluation.active_override_ids, ())
                self.assertEqual(evaluation.suspended_rule_ids, ())
                self.assertEqual(
                    evaluation.standing_rule_ids_in_force, (STANDING_RULE,)
                )
                verdict = _verdict(self.policy, instant, "zai/glm-5.3")
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.restricted_by_rule_ids, (STANDING_RULE,))

    def test_utc_instants_convert_to_the_sgt_calendar_date(self) -> None:
        # 2026-10-07T15:59:59Z is 2026-10-07T23:59:59+08:00 (last covered
        # instant); one second later is already October 8 in the policy
        # calendar. Mirrors the D-059 UTC-to-local boundary tests.
        last_covered = _verdict(
            self.policy,
            datetime.fromisoformat("2026-10-07T15:59:59+00:00"),
            "zai/glm-5.3",
        )
        self.assertTrue(last_covered.allowed)
        first_standing = _verdict(
            self.policy,
            datetime.fromisoformat("2026-10-07T16:00:00+00:00"),
            "zai/glm-5.3",
        )
        self.assertFalse(first_standing.allowed)

    def test_override_start_boundary_also_converts_from_utc(self) -> None:
        # 2026-09-27T16:00:00Z is 2026-09-28T00:00:00+08:00 — the first
        # covered instant of the governance relaxation.
        verdict = _verdict(
            self.policy,
            datetime.fromisoformat("2026-09-27T16:00:00+00:00"),
            "zai/glm-5.3",
        )
        self.assertTrue(verdict.allowed)


class AuditableSubstitutionTests(unittest.TestCase):
    """No hidden fallback: restriction and substitution stay explicit."""

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.policy: DelegatedModelPolicyT = cast(DelegatedModelPolicyT, object())

    @override
    def setUp(self) -> None:
        self.policy = _policy_from_artifact()

    def test_restriction_carries_rule_substitute_and_disclosure(self) -> None:
        verdict = _verdict(self.policy, STANDING_RESUMED_PEAK, "zai/glm-5.3")
        payload = verdict.to_json()
        self.assertEqual(payload["restricted_by_rule_ids"], [STANDING_RULE])
        self.assertEqual(payload["substitute"], "zai/glm-5.3-flash")
        self.assertTrue(payload["disclosure_required"])
        self.assertIn("never silently", verdict.notes[0])
        self.assertIn(STANDING_RULE, verdict.notes[0])

    def test_allowed_via_override_never_fabricates_a_substitution(self) -> None:
        verdict = _verdict(self.policy, OVERRIDE_NOMINAL_PEAK, "zai/glm-5.3")
        payload = verdict.to_json()
        self.assertTrue(payload["allowed"])
        self.assertIsNone(payload["substitute"])
        self.assertFalse(payload["disclosure_required"])

    def test_evaluation_json_names_the_phase(self) -> None:
        evaluation = _evaluate(self.policy, OVERRIDE_NOMINAL_PEAK)
        payload = evaluation.to_json()
        self.assertEqual(payload["local_date"], "2026-09-30")
        self.assertEqual(payload["calendar"], "Asia/Singapore")
        self.assertEqual(payload["active_override_ids"], [CAMPAIGN_OVERRIDE])
        self.assertEqual(payload["suspended_rule_ids"], [STANDING_RULE])


class ValidationTests(unittest.TestCase):
    """Malformed policy sections and ambiguous instants fail closed."""

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.base: dict[str, object] = cast(dict[str, object], object())

    @override
    def setUp(self) -> None:
        self.base = cast(
            "dict[str, object]",
            json.loads(
            json.dumps(
                {
                    "selector_facing": False,
                    "calendar": "Asia/Singapore",
                    "preference_default": {
                        "provider": "zai",
                        "model": "glm-5.3-flash",
                    },
                    "standing_rules": [
                        {
                            "rule_id": "zai_delegated_flash_only",
                            "statement": "Flash only.",
                            "restricted_models": [
                                {"provider": "zai", "model": "glm-5.3"}
                            ],
                            "substitute_model": {
                                "provider": "zai",
                                "model": "glm-5.3-flash",
                            },
                        }
                    ],
                    "temporary_overrides": [
                        {
                            "override_id": "override_a",
                            "suspends": "zai_delegated_flash_only",
                            "effective_from": "2026-09-28",
                            "effective_until": "2026-10-07",
                        }
                    ],
                }
            )
            ),
        )

    def _overrides(self) -> list[dict[str, object]]:
        return cast("list[dict[str, object]]", self.base["temporary_overrides"])

    def _rules(self) -> list[dict[str, object]]:
        return cast("list[dict[str, object]]", self.base["standing_rules"])

    def _assert_rejected(self, pattern: str) -> None:
        with self.assertRaisesRegex(ValueError, pattern):
            _ = MG.validate_delegated_model_policy(self.base)

    def test_valid_section_passes(self) -> None:
        policy = MG.validate_delegated_model_policy(self.base)
        self.assertEqual(policy.standing_rules[0].rule_id, STANDING_RULE)

    def test_unknown_suspends_reference_is_rejected(self) -> None:
        self._overrides()[0]["suspends"] = "no_such_rule"
        self._assert_rejected("unknown standing rule")

    def test_unordered_or_malformed_dates_are_rejected(self) -> None:
        self._overrides()[0]["effective_from"] = "2026-10-08"
        self._assert_rejected("effective_from")
        self._overrides()[0]["effective_from"] = "2026-10-7"
        self._assert_rejected("YYYY-MM-DD")

    def test_duplicate_ids_are_rejected(self) -> None:
        rules = self._rules()
        rules.append(dict(rules[0]))
        self._assert_rejected("duplicate standing rule id")

    def test_selector_facing_section_is_rejected(self) -> None:
        self.base["selector_facing"] = True
        self._assert_rejected("selector_facing")

    def test_unknown_calendar_is_rejected(self) -> None:
        self.base["calendar"] = "Mars/Olympus"
        self._assert_rejected("IANA zone")

    def test_missing_section_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            _ = MG.validate_delegated_model_policy(None)

    def test_naive_instant_is_rejected(self) -> None:
        policy = MG.validate_delegated_model_policy(self.base)
        with self.assertRaisesRegex(ValueError, "timezone-aware instant"):
            _ = MG.evaluate_delegated_model_policy(
                policy, instant=datetime.fromisoformat("2026-09-30T15:00:00")
            )


class CliTests(unittest.TestCase):
    """The operator surface: exit codes and machine-readable output."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL_PATH), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_allowed_model_exits_zero_with_override_provenance(self) -> None:
        result = self._run(
            "--at",
            "2026-09-30T15:00:00+08:00",
            "--model",
            "zai/glm-5.3",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = cast("dict[str, object]", json.loads(result.stdout))
        verdict = cast("dict[str, object]", payload["verdict"])
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["allowed_by_override_ids"], [CAMPAIGN_OVERRIDE])
        evaluation = cast("dict[str, object]", payload["evaluation"])
        self.assertEqual(evaluation["active_override_ids"], [CAMPAIGN_OVERRIDE])

    def test_restricted_model_exits_three_after_expiry(self) -> None:
        result = self._run(
            "--at",
            "2026-10-08T14:00:00+08:00",
            "--model",
            "zai/glm-5.3",
            "--json",
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        payload = cast("dict[str, object]", json.loads(result.stdout))
        verdict = cast("dict[str, object]", payload["verdict"])
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["restricted_by_rule_ids"], [STANDING_RULE])

    def test_naive_at_argument_exits_two_with_actionable_error(self) -> None:
        result = self._run("--at", "2026-10-08T14:00:00", "--model", "zai/glm-5.3")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("UTC offset", result.stderr)


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
