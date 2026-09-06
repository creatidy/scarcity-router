"""M2d scarcity primitive tests (D-026).

These tests pin the frozen scarcity parameters: the continuous integer
penalty ``(100 - remaining_percent)^2`` (scale 10000), the exact explanatory
label boundaries, the normalized ``ScarcityAssessment`` contract and the
candidate capacity-applicability aggregation over explicit
``capacity_bindings`` — including the required M2c-catalog scenarios
(shared OpenAI scope, unrelated additional bucket, 98/2 weekly-critical
case). Deterministic and self-contained: no network, subprocess, clock or
credential access.

Deliberately ill-typed values in negative tests pass through ``cast()`` to
the declared parameter type: the static annotation cannot express "wrong
type on purpose", and the runtime validator must be the one to reject them.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scarcity_router import (
    CapabilityAssessment,
    CapabilityAssessments,
    CapacityDiagnostic,
    CapacityScopeRef,
    CapacitySnapshot,
    CapacityWindow,
    GoverningWindowEvidence,
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
    SCARCITY_LABELS,
    SCARCITY_PENALTY_SCALE,
    SCARCITY_REASON_CODES,
    SCARCITY_STATES,
    ScarcityAssessment,
    SelectionContractValidationError,
    assess_scarcity,
    scarcity_label,
    scarcity_penalty_units,
)

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"

TS = "2026-09-06T12:00:00.000Z"
RESET = "2026-09-12T09:00:00.000Z"


def _ill(value: object) -> object:
    """Mark a deliberately wrong-typed value for a negative-validation test.

    Returns ``object`` so the subsequent ``cast()`` to the declared parameter
    type is statically legal; the runtime validator must reject the value.
    """
    return value


def _load_catalog() -> dict[tuple[str, str], ModelCatalogEntry]:
    catalog = ModelCatalog.from_dict(
        cast(object, json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
    )
    return {(e.identity.provider, e.identity.model): e for e in catalog.entries}


CATALOG = _load_catalog()
LUNA = CATALOG[("openai", "gpt-5.6-luna")]
SOL = CATALOG[("openai", "gpt-5.6-sol")]
GLM53 = CATALOG[("zai", "glm-5.3")]


def _unknown_capabilities() -> CapabilityAssessments:
    """The cheap all-unknown capability vector (no provenance required)."""
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
    bindings: tuple[CapacityScopeRef, ...] | None,
) -> ModelCatalogEntry:
    """A minimal catalog entry for binding/applicability tests.

    Unknown capability ratings require no provenance, so the entry carries
    no curated content; capacity behavior is the only subject here.
    """
    return ModelCatalogEntry(
        identity=ModelIdentity(provider=provider, model=model, variant=variant),
        display_name="Synthetic Test Model",
        hard_properties=ModelHardProperties(),
        capabilities=_unknown_capabilities(),
        capacity_bindings=bindings,
    )


def _window(
    resource: str,
    kind: str,
    scope_id: str,
    remaining: int,
    *,
    resets_at: str | None = RESET,
    window_id: str | None = None,
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
        resets_at=resets_at,
        window_id=window_id,
    )


_STATUS_DIAGNOSTIC: dict[str, str] = {
    "unavailable": "source_unavailable",
    "auth_required": "auth_required",
    "unsupported": "unsupported_source",
    "schema_changed": "schema_changed",
    "unknown": "telemetry_unknown",
}


def _snapshot(
    provider: str,
    windows: tuple[CapacityWindow, ...] = (),
    *,
    status: str = "ok",
) -> CapacitySnapshot:
    diagnostics: tuple[CapacityDiagnostic, ...] = ()
    if status != "ok":
        diagnostics = (CapacityDiagnostic(code=_STATUS_DIAGNOSTIC[status]),)
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="test_source",
        retrieved_at=TS,
        status=status,
        windows=windows,
        diagnostics=diagnostics,
    )


# ── Continuous penalty ────────────────────────────────────────────────────────


class ScarcityPenaltyTests(unittest.TestCase):
    def test_frozen_values(self) -> None:
        cases = {
            100: 0,
            99: 1,
            80: 400,
            50: 2500,
            20: 6400,
            2: 9604,
            1: 9801,
            0: 10000,
        }
        for remaining, expected in cases.items():
            self.assertEqual(
                scarcity_penalty_units(remaining), expected, f"remaining {remaining}"
            )

    def test_scale_constant(self) -> None:
        self.assertEqual(SCARCITY_PENALTY_SCALE, 10_000)
        self.assertEqual(scarcity_penalty_units(0), SCARCITY_PENALTY_SCALE)

    def test_result_is_always_integer(self) -> None:
        for remaining in range(0, 101):
            self.assertIsInstance(scarcity_penalty_units(remaining), int)

    def test_rejects_out_of_range(self) -> None:
        for bad in (-1, 101, 1000):
            with self.assertRaises(SelectionContractValidationError):
                _ = scarcity_penalty_units(cast(int, bad))

    def test_rejects_bool(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = scarcity_penalty_units(cast(int, True))
        with self.assertRaises(SelectionContractValidationError):
            _ = scarcity_penalty_units(cast(int, False))

    def test_rejects_non_integer(self) -> None:
        for bad in (50.0, "50", None, [50]):
            with self.assertRaises(SelectionContractValidationError):
                _ = scarcity_penalty_units(cast(int, bad))


# ── Explanatory labels ────────────────────────────────────────────────────────


class ScarcityLabelTests(unittest.TestCase):
    def test_exact_boundaries(self) -> None:
        cases = {
            100: "plentiful",
            80: "plentiful",
            79: "normal",
            50: "normal",
            49: "scarce",
            20: "scarce",
            19: "critical",
            1: "critical",
            0: "unavailable",
        }
        for remaining, expected in cases.items():
            self.assertEqual(
                scarcity_label(remaining), expected, f"remaining {remaining}"
            )

    def test_vocabulary_is_exact(self) -> None:
        self.assertEqual(
            SCARCITY_LABELS,
            frozenset({
                "plentiful",
                "normal",
                "scarce",
                "critical",
                "unavailable",
                "unknown",
            }),
        )

    def test_unknown_is_never_produced_from_a_number(self) -> None:
        for remaining in range(0, 101):
            self.assertNotEqual(scarcity_label(remaining), "unknown")

    def test_rejects_invalid_percentages(self) -> None:
        for bad in (-1, 101, True, 1.5, "80", None):
            with self.assertRaises(SelectionContractValidationError):
                _ = scarcity_label(cast(int, bad))


# ── Assessment contract ───────────────────────────────────────────────────────


class GoverningEvidenceContractTests(unittest.TestCase):
    def test_round_trip_without_window_id(self) -> None:
        evidence = GoverningWindowEvidence(
            scope=CapacityScopeRef(provider="zai", scope_id="coding_plan"),
            resource="tokens",
            kind="weekly",
            remaining_percent=2,
        )
        self.assertEqual(
            evidence.to_dict(),
            {
                "scope": {"provider": "zai", "scope_id": "coding_plan"},
                "resource": "tokens",
                "kind": "weekly",
                "remaining_percent": 2,
            },
        )
        self.assertEqual(GoverningWindowEvidence.from_dict(evidence.to_dict()), evidence)

    def test_round_trip_with_window_id(self) -> None:
        evidence = GoverningWindowEvidence(
            scope=CapacityScopeRef(provider="openai", scope_id="codex"),
            resource="tokens",
            kind="five_hour",
            remaining_percent=40,
            window_id="primary",
        )
        restored = GoverningWindowEvidence.from_dict(evidence.to_dict())
        self.assertEqual(restored, evidence)
        self.assertEqual(restored.window_id, "primary")

    def test_rejects_unknown_keys_and_bad_values(self) -> None:
        payload = {
            "scope": {"provider": "openai", "scope_id": "codex"},
            "resource": "tokens",
            "kind": "weekly",
            "remaining_percent": 2,
            "extra": 1,
        }
        with self.assertRaises(SelectionContractValidationError):
            _ = GoverningWindowEvidence.from_dict(payload)
        with self.assertRaises(SelectionContractValidationError):
            _ = GoverningWindowEvidence.from_dict({
                "scope": {"provider": "openai", "scope_id": "codex"},
                "resource": "credits",  # not a normalized resource
                "kind": "weekly",
                "remaining_percent": 2,
            })
        with self.assertRaises(SelectionContractValidationError):
            _ = GoverningWindowEvidence(
                scope=CapacityScopeRef(provider="openai", scope_id="codex"),
                resource="tokens",
                kind="weekly",
                remaining_percent=101,
            )


def _numeric_assessment(
    state: str,
    effective: int,
    scopes: tuple[CapacityScopeRef, ...],
    reason_codes: tuple[str, ...],
    governing: object,
) -> ScarcityAssessment:
    """Direct-construction helper for numeric-state invariant tests.

    ``governing`` may be a deliberately ill-typed value; the cast keeps the
    helper statically legal so the runtime validator is the one to reject it.
    """
    return ScarcityAssessment(
        state=state,
        label=scarcity_label(effective),
        penalty_units=scarcity_penalty_units(effective),
        effective_remaining_percent=effective,
        applicable_scopes=scopes,
        governing_window=cast("GoverningWindowEvidence | None", governing),
        reason_codes=reason_codes,
    )


class ScarcityAssessmentContractTests(unittest.TestCase):
    def test_known_round_trip_is_exact(self) -> None:
        assessment = ScarcityAssessment(
            state="known",
            label="critical",
            penalty_units=9604,
            effective_remaining_percent=2,
            applicable_scopes=(CapacityScopeRef(provider="zai", scope_id="coding_plan"),),
            governing_window=GoverningWindowEvidence(
                scope=CapacityScopeRef(provider="zai", scope_id="coding_plan"),
                resource="tokens",
                kind="weekly",
                remaining_percent=2,
                window_id="weekly",
            ),
            reason_codes=(),
        )
        payload = assessment.to_dict()
        self.assertEqual(
            payload,
            {
                "state": "known",
                "label": "critical",
                "penalty_units": 9604,
                "effective_remaining_percent": 2,
                "applicable_scopes": [
                    {"provider": "zai", "scope_id": "coding_plan"}
                ],
                "governing_window": {
                    "scope": {"provider": "zai", "scope_id": "coding_plan"},
                    "resource": "tokens",
                    "kind": "weekly",
                    "remaining_percent": 2,
                    "window_id": "weekly",
                },
                "reason_codes": [],
            },
        )
        self.assertEqual(ScarcityAssessment.from_dict(payload), assessment)

    def test_unknown_omits_numeric_fields(self) -> None:
        # Known applicability + unknown telemetry: the binding is preserved,
        # only the numeric/governing fields are omitted.
        assessment = ScarcityAssessment(
            state="unknown",
            label="unknown",
            penalty_units=None,
            effective_remaining_percent=None,
            applicable_scopes=(
                CapacityScopeRef(provider="zai", scope_id="coding_plan"),
            ),
            governing_window=None,
            reason_codes=("missing_provider_snapshot",),
        )
        payload = assessment.to_dict()
        self.assertNotIn("penalty_units", payload)
        self.assertNotIn("effective_remaining_percent", payload)
        self.assertNotIn("governing_window", payload)
        self.assertEqual(
            payload["applicable_scopes"],
            [{"provider": "zai", "scope_id": "coding_plan"}],
        )
        self.assertEqual(ScarcityAssessment.from_dict(payload), assessment)

    def test_canonical_ordering(self) -> None:
        zai_scope = CapacityScopeRef(provider="zai", scope_id="coding_plan")
        first = ScarcityAssessment(
            state="unknown",
            label="unknown",
            penalty_units=None,
            effective_remaining_percent=None,
            applicable_scopes=(zai_scope,),
            governing_window=None,
            reason_codes=("missing_scope_window", "missing_provider_snapshot"),
        )
        second = ScarcityAssessment(
            state="unknown",
            label="unknown",
            penalty_units=None,
            effective_remaining_percent=None,
            applicable_scopes=(zai_scope,),
            governing_window=None,
            reason_codes=("missing_provider_snapshot", "missing_scope_window"),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first.reason_codes, ("missing_provider_snapshot", "missing_scope_window")
        )

    def test_unknown_state_rejects_numeric_penalty(self) -> None:
        zai_scope = CapacityScopeRef(provider="zai", scope_id="coding_plan")
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment(
                state="unknown",
                label="unknown",
                penalty_units=10000,
                effective_remaining_percent=None,
                applicable_scopes=(zai_scope,),
                governing_window=None,
                reason_codes=("missing_provider_snapshot",),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment(
                state="unknown",
                label="unknown",
                penalty_units=None,
                effective_remaining_percent=50,
                applicable_scopes=(zai_scope,),
                governing_window=None,
                reason_codes=("missing_provider_snapshot",),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment(
                state="unknown",
                label="unknown",
                penalty_units=None,
                effective_remaining_percent=None,
                applicable_scopes=(zai_scope,),
                governing_window=GoverningWindowEvidence(
                    scope=CapacityScopeRef(provider="openai", scope_id="codex"),
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=50,
                ),
                reason_codes=("missing_provider_snapshot",),
            )

    def test_unknown_state_requires_reason_codes(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment(
                state="unknown",
                label="unknown",
                penalty_units=None,
                effective_remaining_percent=None,
                applicable_scopes=(
                    CapacityScopeRef(provider="zai", scope_id="coding_plan"),
                ),
                governing_window=None,
                reason_codes=(),
            )

    def test_numeric_state_requires_consistent_penalty_and_label(self) -> None:
        scope = CapacityScopeRef(provider="openai", scope_id="codex")
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment(
                state="known",
                label="normal",
                penalty_units=100,  # not (100 - 50)^2
                effective_remaining_percent=50,
                applicable_scopes=(scope,),
                governing_window=GoverningWindowEvidence(
                    scope=scope,
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=50,
                ),
                reason_codes=(),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment(
                state="known",
                label="scarce",  # 50 -> normal
                penalty_units=2500,
                effective_remaining_percent=50,
                applicable_scopes=(scope,),
                governing_window=GoverningWindowEvidence(
                    scope=scope,
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=50,
                ),
                reason_codes=(),
            )

    def test_known_state_rejects_zero_remaining(self) -> None:
        scope = CapacityScopeRef(provider="openai", scope_id="codex")
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment(
                state="known",
                label="unavailable",
                penalty_units=10000,
                effective_remaining_percent=0,
                applicable_scopes=(scope,),
                governing_window=GoverningWindowEvidence(
                    scope=scope,
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=0,
                ),
                reason_codes=(),
            )

    def test_unavailable_state_requires_exhaustion_code(self) -> None:
        scope = CapacityScopeRef(provider="openai", scope_id="codex")
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment(
                state="unavailable",
                label="unavailable",
                penalty_units=10000,
                effective_remaining_percent=0,
                applicable_scopes=(scope,),
                governing_window=GoverningWindowEvidence(
                    scope=scope,
                    resource="tokens",
                    kind="weekly",
                    remaining_percent=0,
                ),
                reason_codes=(),
            )
        valid = ScarcityAssessment(
            state="unavailable",
            label="unavailable",
            penalty_units=10000,
            effective_remaining_percent=0,
            applicable_scopes=(scope,),
            governing_window=GoverningWindowEvidence(
                scope=scope,
                resource="tokens",
                kind="weekly",
                remaining_percent=0,
            ),
            reason_codes=("capacity_exhausted",),
        )
        self.assertEqual(ScarcityAssessment.from_dict(valid.to_dict()), valid)

    def test_from_dict_rejects_unknown_keys_and_bad_states(self) -> None:
        base = {
            "state": "unknown",
            "label": "unknown",
            "applicable_scopes": [
                {"provider": "zai", "scope_id": "coding_plan"}
            ],
            "reason_codes": ["missing_provider_snapshot"],
        }
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment.from_dict({**base, "unexpected": 1})
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment.from_dict({**base, "state": "maybe"})
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment.from_dict({**base, "label": "plentiful"})
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment.from_dict({
                key: value for key, value in base.items() if key != "reason_codes"
            })

    def test_numeric_state_rejects_ill_typed_governing_window(self) -> None:
        scope = CapacityScopeRef(provider="openai", scope_id="codex")
        evidence = GoverningWindowEvidence(
            scope=scope,
            resource="tokens",
            kind="weekly",
            remaining_percent=50,
        )
        # The well-typed baseline constructs (and round-trips) fine.
        baseline = _numeric_assessment("known", 50, (scope,), (), evidence)
        self.assertEqual(
            ScarcityAssessment.from_dict(baseline.to_dict()), baseline
        )
        for bad in (
            {"scope": {"provider": "openai", "scope_id": "codex"}},
            "tokens weekly",
            object(),
        ):
            with self.subTest(governing=type(bad).__name__):
                with self.assertRaises(SelectionContractValidationError):
                    _ = _numeric_assessment("known", 50, (scope,), (), bad)

    def test_known_state_rejects_contradictory_reason_codes(self) -> None:
        scope = CapacityScopeRef(provider="openai", scope_id="codex")
        evidence = GoverningWindowEvidence(
            scope=scope,
            resource="tokens",
            kind="weekly",
            remaining_percent=50,
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = _numeric_assessment(
                "known", 50, (scope,), ("capacity_exhausted",), evidence
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = _numeric_assessment(
                "known", 50, (scope,), ("missing_provider_snapshot",), evidence
            )

    def test_known_state_rejects_empty_applicable_scopes(self) -> None:
        evidence = GoverningWindowEvidence(
            scope=CapacityScopeRef(provider="openai", scope_id="codex"),
            resource="tokens",
            kind="weekly",
            remaining_percent=50,
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = _numeric_assessment("known", 50, (), (), evidence)

    def test_governing_scope_must_belong_to_applicable_scopes(self) -> None:
        zai_scope = CapacityScopeRef(provider="zai", scope_id="coding_plan")
        openai_scope = CapacityScopeRef(provider="openai", scope_id="codex")
        foreign = GoverningWindowEvidence(
            scope=zai_scope,
            resource="tokens",
            kind="weekly",
            remaining_percent=50,
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = _numeric_assessment("known", 50, (openai_scope,), (), foreign)
        zero = GoverningWindowEvidence(
            scope=zai_scope,
            resource="tokens",
            kind="weekly",
            remaining_percent=0,
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = _numeric_assessment(
                "unavailable", 0, (openai_scope,), ("capacity_exhausted",), zero
            )

    def test_unavailable_with_incompleteness_reason_remains_valid(self) -> None:
        # Explicit exhaustion may correctly dominate another unknown bound
        # scope: capacity_exhausted plus an incompleteness code is valid.
        scopes = (
            CapacityScopeRef(provider="openai", scope_id="codex"),
            CapacityScopeRef(provider="openai", scope_id="extra-scope"),
        )
        evidence = GoverningWindowEvidence(
            scope=scopes[0],
            resource="tokens",
            kind="weekly",
            remaining_percent=0,
        )
        assessment = _numeric_assessment(
            "unavailable",
            0,
            scopes,
            ("capacity_exhausted", "missing_scope_window"),
            evidence,
        )
        self.assertEqual(ScarcityAssessment.from_dict(assessment.to_dict()), assessment)

    def test_frozen_vocabularies(self) -> None:
        self.assertEqual(SCARCITY_STATES, frozenset({"known", "unknown", "unavailable"}))
        self.assertEqual(
            SCARCITY_REASON_CODES,
            frozenset({
                "capacity_bindings_unknown",
                "missing_provider_snapshot",
                "provider_snapshot_not_ok",
                "missing_scope_window",
                "window_percentage_unknown",
                "capacity_exhausted",
            }),
        )


class UnknownApplicabilityInvariantTests(unittest.TestCase):
    """The unknown state's two mutually exclusive applicability classes."""

    ZAI_SCOPE: CapacityScopeRef = CapacityScopeRef(
        provider="zai", scope_id="coding_plan"
    )
    OPENAI_SCOPE: CapacityScopeRef = CapacityScopeRef(
        provider="openai", scope_id="codex"
    )

    def _unknown(
        self,
        scopes: tuple[CapacityScopeRef, ...],
        reason_codes: tuple[str, ...],
    ) -> ScarcityAssessment:
        return ScarcityAssessment(
            state="unknown",
            label="unknown",
            penalty_units=None,
            effective_remaining_percent=None,
            applicable_scopes=scopes,
            governing_window=None,
            reason_codes=reason_codes,
        )

    def test_unknown_bindings_state_is_valid_and_round_trips(self) -> None:
        assessment = self._unknown((), ("capacity_bindings_unknown",))
        self.assertEqual(assessment.applicable_scopes, ())
        self.assertEqual(
            ScarcityAssessment.from_dict(assessment.to_dict()), assessment
        )

    def test_telemetry_unknown_with_empty_scopes_fails(self) -> None:
        for code in (
            "missing_provider_snapshot",
            "provider_snapshot_not_ok",
            "missing_scope_window",
            "window_percentage_unknown",
        ):
            with self.subTest(reason=code):
                with self.assertRaises(SelectionContractValidationError):
                    _ = self._unknown((), (code,))

    def test_bindings_unknown_with_scopes_fails(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = self._unknown(
                (self.OPENAI_SCOPE,), ("capacity_bindings_unknown",)
            )

    def test_mixed_applicability_semantics_fail(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = self._unknown(
                (self.OPENAI_SCOPE,),
                ("capacity_bindings_unknown", "missing_provider_snapshot"),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = self._unknown(
                (),
                ("capacity_bindings_unknown", "missing_scope_window"),
            )

    def test_multiple_telemetry_causes_with_known_scopes_round_trip(self) -> None:
        assessment = self._unknown(
            (self.OPENAI_SCOPE, self.ZAI_SCOPE),
            ("missing_provider_snapshot", "window_percentage_unknown"),
        )
        self.assertEqual(
            assessment.applicable_scopes,
            (self.OPENAI_SCOPE, self.ZAI_SCOPE),
        )
        self.assertEqual(
            ScarcityAssessment.from_dict(assessment.to_dict()), assessment
        )

    def test_from_dict_rejects_contradictory_serialized_state(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment.from_dict({
                "state": "unknown",
                "label": "unknown",
                "applicable_scopes": [],
                "reason_codes": ["missing_provider_snapshot"],
            })
        with self.assertRaises(SelectionContractValidationError):
            _ = ScarcityAssessment.from_dict({
                "state": "unknown",
                "label": "unknown",
                "applicable_scopes": [
                    {"provider": "openai", "scope_id": "codex"}
                ],
                "reason_codes": ["capacity_bindings_unknown"],
            })


# ── Candidate capacity applicability ──────────────────────────────────────────


class AssessScarcityTests(unittest.TestCase):
    def test_most_restrictive_window_governs_98_over_2(self) -> None:
        snapshot = _snapshot(
            "zai",
            (
                _window("tokens", "five_hour", "coding_plan", 98, window_id="short"),
                _window("tokens", "weekly", "coding_plan", 2, window_id="weekly"),
            ),
        )
        assessment = assess_scarcity(GLM53, [snapshot])
        self.assertEqual(assessment.state, "known")
        self.assertEqual(assessment.effective_remaining_percent, 2)
        self.assertEqual(assessment.penalty_units, 9604)
        self.assertEqual(assessment.label, "critical")
        self.assertEqual(assessment.reason_codes, ())
        assert assessment.governing_window is not None
        self.assertEqual(assessment.governing_window.kind, "weekly")
        self.assertEqual(assessment.governing_window.window_id, "weekly")
        self.assertEqual(
            assessment.applicable_scopes,
            (CapacityScopeRef(provider="zai", scope_id="coding_plan"),),
        )

    def test_multiple_scopes_most_restrictive_across_all(self) -> None:
        entry = _synthetic_entry(
            "openai",
            "test-model",
            "max",
            (
                CapacityScopeRef(provider="openai", scope_id="codex"),
                CapacityScopeRef(provider="openai", scope_id="extra-scope"),
            ),
        )
        snapshot = _snapshot(
            "openai",
            (
                _window("tokens", "weekly", "codex", 70),
                _window("tokens", "weekly", "extra-scope", 30),
            ),
        )
        assessment = assess_scarcity(entry, [snapshot])
        self.assertEqual(assessment.effective_remaining_percent, 30)
        self.assertEqual(assessment.penalty_units, 4900)
        assert assessment.governing_window is not None
        self.assertEqual(assessment.governing_window.scope.scope_id, "extra-scope")
        self.assertEqual(
            assessment.applicable_scopes,
            (
                CapacityScopeRef(provider="openai", scope_id="codex"),
                CapacityScopeRef(provider="openai", scope_id="extra-scope"),
            ),
        )

    def test_unrelated_zero_bucket_is_ignored(self) -> None:
        base = _snapshot(
            "openai",
            (
                _window("tokens", "five_hour", "codex", 90),
                _window("tokens", "weekly", "codex", 40),
            ),
        )
        with_unrelated = _snapshot(
            "openai",
            (
                _window("tokens", "five_hour", "codex", 90),
                _window("tokens", "weekly", "codex", 40),
                _window("tokens", "weekly", "reserve-bucket", 0),
            ),
        )
        self.assertEqual(assess_scarcity(LUNA, [base]), assess_scarcity(LUNA, [with_unrelated]))
        self.assertEqual(assess_scarcity(SOL, [base]), assess_scarcity(SOL, [with_unrelated]))

    def test_time_window_participates_without_token_special_case(self) -> None:
        snapshot = _snapshot(
            "zai",
            (
                _window("tokens", "weekly", "coding_plan", 90),
                _window("time", "five_hour", "coding_plan", 10),
            ),
        )
        assessment = assess_scarcity(GLM53, [snapshot])
        self.assertEqual(assessment.effective_remaining_percent, 10)
        self.assertEqual(assessment.penalty_units, 8100)
        self.assertEqual(assessment.label, "critical")
        assert assessment.governing_window is not None
        self.assertEqual(assessment.governing_window.resource, "time")

    def test_unknown_kind_window_with_pair_participates(self) -> None:
        snapshot = _snapshot(
            "zai",
            (
                _window("tokens", "weekly", "coding_plan", 95),
                _window("tokens", "unknown", "coding_plan", 15),
            ),
        )
        assessment = assess_scarcity(GLM53, [snapshot])
        self.assertEqual(assessment.effective_remaining_percent, 15)
        self.assertEqual(assessment.penalty_units, 7225)

    def test_unknown_bindings_yield_unknown_assessment(self) -> None:
        entry = _synthetic_entry("zai", "test-model", "max", None)
        snapshot = _snapshot(
            "zai", (_window("tokens", "weekly", "coding_plan", 80),)
        )
        assessment = assess_scarcity(entry, [snapshot])
        self.assertEqual(assessment.state, "unknown")
        self.assertEqual(assessment.label, "unknown")
        self.assertIsNone(assessment.penalty_units)
        self.assertIsNone(assessment.effective_remaining_percent)
        self.assertIsNone(assessment.governing_window)
        self.assertEqual(assessment.reason_codes, ("capacity_bindings_unknown",))
        self.assertEqual(assessment.applicable_scopes, ())

    def test_missing_provider_snapshot_is_unknown(self) -> None:
        snapshot = _snapshot(
            "openai",
            (_window("tokens", "weekly", "codex", 80),),
        )
        assessment = assess_scarcity(GLM53, [snapshot])
        self.assertEqual(assessment.state, "unknown")
        self.assertEqual(assessment.reason_codes, ("missing_provider_snapshot",))
        # The failure is in the telemetry, not the applicability: the known
        # binding is preserved.
        self.assertEqual(
            assessment.applicable_scopes,
            (CapacityScopeRef(provider="zai", scope_id="coding_plan"),),
        )

    def test_missing_bound_scope_is_unknown(self) -> None:
        snapshot = _snapshot(
            "zai", (_window("tokens", "weekly", "other-scope", 80),)
        )
        assessment = assess_scarcity(GLM53, [snapshot])
        self.assertEqual(assessment.state, "unknown")
        self.assertEqual(assessment.reason_codes, ("missing_scope_window",))
        self.assertEqual(
            assessment.applicable_scopes,
            (CapacityScopeRef(provider="zai", scope_id="coding_plan"),),
        )

    def test_applicable_percentage_unknown_is_unknown(self) -> None:
        snapshot = _snapshot(
            "zai",
            (
                _window("tokens", "weekly", "coding_plan", 80),
                _window("tokens", "five_hour", "coding_plan", 0, with_pair=False),
            ),
        )
        assessment = assess_scarcity(GLM53, [snapshot])
        self.assertEqual(assessment.state, "unknown")
        self.assertEqual(assessment.reason_codes, ("window_percentage_unknown",))
        self.assertIsNone(assessment.penalty_units)
        self.assertEqual(
            assessment.applicable_scopes,
            (CapacityScopeRef(provider="zai", scope_id="coding_plan"),),
        )

    def test_non_ok_provider_telemetry_is_unknown_not_unavailable(self) -> None:
        for status in ("unknown", "auth_required", "schema_changed", "unavailable"):
            with self.subTest(status=status):
                snapshot = _snapshot("zai", status=status)
                assessment = assess_scarcity(GLM53, [snapshot])
                self.assertEqual(assessment.state, "unknown")
                self.assertEqual(assessment.label, "unknown")
                self.assertIsNone(assessment.penalty_units)
                self.assertEqual(
                    assessment.reason_codes, ("provider_snapshot_not_ok",)
                )
                self.assertEqual(
                    assessment.applicable_scopes,
                    (CapacityScopeRef(provider="zai", scope_id="coding_plan"),),
                )

    def test_multiple_bindings_preserved_on_unknown_telemetry(self) -> None:
        entry = _synthetic_entry(
            "openai",
            "test-model",
            "max",
            (
                CapacityScopeRef(provider="openai", scope_id="extra-scope"),
                CapacityScopeRef(provider="openai", scope_id="codex"),
            ),
        )
        # Missing provider snapshot: ALL bound scopes are preserved,
        # canonically ordered, independent of construction order.
        missing = assess_scarcity(entry, [])
        self.assertEqual(missing.state, "unknown")
        self.assertEqual(missing.reason_codes, ("missing_provider_snapshot",))
        self.assertEqual(
            missing.applicable_scopes,
            (
                CapacityScopeRef(provider="openai", scope_id="codex"),
                CapacityScopeRef(provider="openai", scope_id="extra-scope"),
            ),
        )
        # One incomplete scope still preserves every binding.
        partial = _snapshot(
            "openai", (_window("tokens", "weekly", "codex", 80),)
        )
        incomplete = assess_scarcity(entry, [partial])
        self.assertEqual(incomplete.state, "unknown")
        self.assertEqual(incomplete.reason_codes, ("missing_scope_window",))
        self.assertEqual(
            incomplete.applicable_scopes,
            (
                CapacityScopeRef(provider="openai", scope_id="codex"),
                CapacityScopeRef(provider="openai", scope_id="extra-scope"),
            ),
        )

    def test_exhaustion_dominates_unknown_scope(self) -> None:
        # One model cannot bind across providers (M2b contract), so the
        # "another unknown bound scope" case is a second same-provider
        # scope with no matching window.
        entry = _synthetic_entry(
            "openai",
            "test-model",
            "max",
            (
                CapacityScopeRef(provider="openai", scope_id="codex"),
                CapacityScopeRef(provider="openai", scope_id="extra-scope"),
            ),
        )
        openai_zero = _snapshot(
            "openai", (_window("tokens", "weekly", "codex", 0),)
        )
        assessment = assess_scarcity(entry, [openai_zero])
        self.assertEqual(assessment.state, "unavailable")
        self.assertEqual(assessment.label, "unavailable")
        self.assertEqual(assessment.penalty_units, 10000)
        self.assertEqual(assessment.effective_remaining_percent, 0)
        self.assertEqual(
            assessment.reason_codes,
            ("capacity_exhausted", "missing_scope_window"),
        )
        assert assessment.governing_window is not None
        self.assertEqual(assessment.governing_window.remaining_percent, 0)

    def test_exhaustion_of_only_binding_is_unavailable(self) -> None:
        openai_zero = _snapshot(
            "openai", (_window("tokens", "weekly", "codex", 0),)
        )
        assessment = assess_scarcity(LUNA, [openai_zero])
        self.assertEqual(assessment.state, "unavailable")
        self.assertEqual(assessment.reason_codes, ("capacity_exhausted",))

    def test_exhaustion_dominates_unknown_percentage_window(self) -> None:
        snapshot = _snapshot(
            "zai",
            (
                _window("tokens", "weekly", "coding_plan", 0),
                _window("tokens", "five_hour", "coding_plan", 0, with_pair=False),
            ),
        )
        assessment = assess_scarcity(GLM53, [snapshot])
        self.assertEqual(assessment.state, "unavailable")
        self.assertEqual(assessment.penalty_units, 10000)
        self.assertEqual(
            assessment.reason_codes,
            ("capacity_exhausted", "window_percentage_unknown"),
        )

    def test_reset_timestamp_never_changes_penalty_or_label(self) -> None:
        windows_a = (
            _window("tokens", "five_hour", "codex", 70, resets_at=RESET),
            _window("tokens", "weekly", "codex", 40, resets_at=RESET),
        )
        windows_b = (
            _window("tokens", "five_hour", "codex", 70, resets_at="2030-01-01T00:00:00.000Z"),
            _window("tokens", "weekly", "codex", 40, resets_at="2030-01-01T00:00:00.000Z"),
        )
        self.assertEqual(
            assess_scarcity(LUNA, [_snapshot("openai", windows_a)]),
            assess_scarcity(LUNA, [_snapshot("openai", windows_b)]),
        )

    def test_duplicate_provider_snapshots_fail_typed_validation(self) -> None:
        first = _snapshot("openai", (_window("tokens", "weekly", "codex", 40),))
        second = _snapshot("openai", (_window("tokens", "weekly", "codex", 20),))
        with self.assertRaises(SelectionContractValidationError):
            _ = assess_scarcity(LUNA, [first, second])

    def test_rejects_wrong_input_types(self) -> None:
        snapshot = _snapshot(
            "zai", (_window("tokens", "weekly", "coding_plan", 80),)
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = assess_scarcity(
                cast(ModelCatalogEntry, _ill(snapshot)), [snapshot]
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = assess_scarcity(
                GLM53, [cast(CapacitySnapshot, _ill(GLM53))]
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = assess_scarcity(
                GLM53, [cast(CapacitySnapshot, _ill("not-a-snapshot"))]
            )

    def test_governing_tie_is_order_independent(self) -> None:
        entry = _synthetic_entry(
            "openai",
            "test-model",
            "max",
            (
                CapacityScopeRef(provider="openai", scope_id="codex"),
                CapacityScopeRef(provider="openai", scope_id="extra-scope"),
            ),
        )
        one = _snapshot(
            "openai",
            (
                _window("tokens", "weekly", "codex", 30, window_id="codex-weekly"),
                _window("tokens", "weekly", "extra-scope", 30, window_id="extra-weekly"),
            ),
        )
        two = _snapshot(
            "openai",
            (
                _window("tokens", "weekly", "extra-scope", 30, window_id="extra-weekly"),
                _window("tokens", "weekly", "codex", 30, window_id="codex-weekly"),
            ),
        )
        self.assertEqual(assess_scarcity(entry, [one]), assess_scarcity(entry, [two]))
        assessment = assess_scarcity(entry, [one])
        assert assessment.governing_window is not None
        # Canonical key (provider, scope_id, ...): "codex" sorts before
        # "extra-scope", so the codex window governs both times.
        self.assertEqual(assessment.governing_window.scope.scope_id, "codex")
        self.assertEqual(assessment.governing_window.window_id, "codex-weekly")

    def test_governing_tie_within_one_scope_uses_window_id(self) -> None:
        first = _snapshot(
            "zai",
            (
                _window("tokens", "weekly", "coding_plan", 30, window_id="b-window"),
                _window("tokens", "weekly", "coding_plan", 30, window_id="a-window"),
            ),
        )
        second = _snapshot(
            "zai",
            (
                _window("tokens", "weekly", "coding_plan", 30, window_id="a-window"),
                _window("tokens", "weekly", "coding_plan", 30, window_id="b-window"),
            ),
        )
        self.assertEqual(assess_scarcity(GLM53, [first]), assess_scarcity(GLM53, [second]))
        assessment = assess_scarcity(GLM53, [first])
        assert assessment.governing_window is not None
        self.assertEqual(assessment.governing_window.window_id, "a-window")

    def test_empty_bindings_scope_evidence_is_ignored_for_unknown_binding(self) -> None:
        # A snapshot with healthy windows must not turn unknown bindings
        # into a known assessment.
        snapshot = _snapshot(
            "openai", (_window("tokens", "weekly", "codex", 95),)
        )
        entry = _synthetic_entry("openai", "test-model", "max", None)
        assessment = assess_scarcity(entry, [snapshot])
        self.assertEqual(assessment.state, "unknown")
        self.assertEqual(assessment.reason_codes, ("capacity_bindings_unknown",))


# ── Integration scenarios (resource-state level only; no selection) ──────────


class ScenarioTests(unittest.TestCase):
    def test_scenario_a_abundant_zai_vs_scarcer_openai(self) -> None:
        openai = _snapshot(
            "openai",
            (
                _window("tokens", "five_hour", "codex", 98),
                _window("tokens", "weekly", "codex", 40),
            ),
        )
        zai = _snapshot(
            "zai",
            (
                _window("tokens", "five_hour", "coding_plan", 98),
                _window("tokens", "weekly", "coding_plan", 80),
            ),
        )
        openai_assessment = assess_scarcity(LUNA, [openai])
        zai_assessment = assess_scarcity(GLM53, [zai])
        self.assertEqual(openai_assessment.state, "known")
        self.assertEqual(zai_assessment.state, "known")
        # Resource-state result only: Z.ai is less scarce than OpenAI.
        self.assertLess(
            cast(int, zai_assessment.penalty_units),
            cast(int, openai_assessment.penalty_units),
        )
        # No model is selected here; M2e owns selection.

    def test_scenario_b_famous_98_2_case(self) -> None:
        zai = _snapshot(
            "zai",
            (
                _window("tokens", "five_hour", "coding_plan", 98),
                _window("tokens", "weekly", "coding_plan", 2),
            ),
        )
        assessment = assess_scarcity(GLM53, [zai])
        self.assertEqual(assessment.effective_remaining_percent, 2)
        self.assertEqual(assessment.penalty_units, 9604)
        self.assertEqual(assessment.label, "critical")

    def test_scenario_c_shared_openai_scope_same_raw_scarcity(self) -> None:
        openai = _snapshot(
            "openai",
            (
                _window("tokens", "five_hour", "codex", 65),
                _window("tokens", "weekly", "codex", 15),
            ),
        )
        luna = assess_scarcity(LUNA, [openai])
        sol = assess_scarcity(SOL, [openai])
        self.assertEqual(luna, sol)
        self.assertEqual(
            luna.governing_window,
            GoverningWindowEvidence(
                scope=CapacityScopeRef(provider="openai", scope_id="codex"),
                resource="tokens",
                kind="weekly",
                remaining_percent=15,
            ),
        )

    def test_scenario_d_unrelated_additional_openai_bucket(self) -> None:
        base = _snapshot(
            "openai",
            (
                _window("tokens", "five_hour", "codex", 50),
                _window("tokens", "weekly", "codex", 45),
            ),
        )
        extended = _snapshot(
            "openai",
            (
                _window("tokens", "five_hour", "codex", 50),
                _window("tokens", "weekly", "codex", 45),
                _window("tokens", "weekly", "additional-bucket", 0),
            ),
        )
        for entry in (LUNA, SOL):
            before = assess_scarcity(entry, [base])
            after = assess_scarcity(entry, [extended])
            self.assertEqual(before, after)
            self.assertEqual(before.effective_remaining_percent, 45)

    def test_scenario_f_exhausted_openai_stays_unavailable_with_reset_credit(self) -> None:
        # The policy-side half of this scenario (recoverable replenishment)
        # lives in tests/test_resource_policy.py; here the reset presence is
        # represented only by a different resets_at, which must not matter.
        exhausted = _snapshot(
            "openai", (_window("tokens", "weekly", "codex", 0),)
        )
        assessment = assess_scarcity(LUNA, [exhausted])
        self.assertEqual(assessment.state, "unavailable")
        self.assertEqual(assessment.penalty_units, 10000)


if __name__ == "__main__":
    _ = unittest.main()
