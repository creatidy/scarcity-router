"""Contract tests for the M2b selection-input core types.

These tests construct the task-requirement and model-catalog contracts
(``scarcity_router.selection_types``, D-024) directly and assert the frozen
invariants, including deterministic serialization, round-trips, the explicit
unknown states, repository consistency with ``model-policy.json`` and narrow
anti-inference source assertions. They do NOT populate any real rating,
minimum or catalog entry: no such values exist in M2b.

Deliberately ill-typed values in negative tests pass through ``cast()`` to
the declared parameter type: the static annotation cannot express "wrong type
on purpose", and the runtime validator must be the one to reject them.

Run with either:

    python -m unittest discover -s tests -v
    python -m unittest tests.test_selection_types -v

All tests are deterministic and self-contained; no network, subprocess, or
credential access occurs.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import cast

import scarcity_router
from scarcity_router import (
    CAPABILITY_DIMENSIONS,
    CONFIDENCE_VALUES,
    MAX_RATING,
    MIN_RATING,
    SUPPORTED_PROVIDERS,
    TASK_LEVELS,
    CapabilityAssessment,
    CapabilityAssessments,
    CapabilityMinima,
    CapacityScopeRef,
    CapacityValidationError,
    EvidenceRef,
    HardConstraints,
    HumanOverride,
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
    ModelRef,
    SelectionContractError,
    SelectionContractValidationError,
    TaskRequirement,
)
from scarcity_router import selection_types as st

POLICY_PATH = Path(__file__).resolve().parents[1] / "model-policy.json"
MODULE_PATH = Path(st.__file__).resolve()

UNKNOWN_ASSESSMENT = CapabilityAssessment(rating=None)


def _ill(value: object) -> object:
    """Mark a deliberately wrong-typed value for a negative-validation test.

    Returns ``object`` so the subsequent ``cast()`` to the declared parameter
    type is statically legal; the runtime validator must reject the value.
    """
    return value


def _clone(payload: dict[str, object]) -> dict[str, object]:
    """Deep-copy a payload (JSON round-trip keeps the exact JSON shape)."""
    return cast("dict[str, object]", json.loads(json.dumps(payload)))


def _unknown_vector() -> CapabilityAssessments:
    return CapabilityAssessments(
        reasoning=UNKNOWN_ASSESSMENT,
        coding=UNKNOWN_ASSESSMENT,
        scientific_methodological=UNKNOWN_ASSESSMENT,
        writing_editorial=UNKNOWN_ASSESSMENT,
        tool_use=UNKNOWN_ASSESSMENT,
        translation_multilingual=UNKNOWN_ASSESSMENT,
    )


def _load_policy() -> dict[str, object]:
    value = cast(object, json.loads(POLICY_PATH.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise AssertionError("model-policy.json must contain a JSON object")
    return cast("dict[str, object]", value)


# ── Identity primitives ───────────────────────────────────────────────────────


class IdentityPrimitives(unittest.TestCase):
    def test_safe_model_ref_round_trip(self) -> None:
        ref = ModelRef(provider="openai", model="example-model")
        self.assertEqual(
            ref.to_dict(), {"provider": "openai", "model": "example-model"}
        )
        self.assertEqual(ModelRef.from_dict(ref.to_dict()), ref)

    def test_safe_model_identity_round_trip(self) -> None:
        identity = ModelIdentity(provider="zai", model="example", variant="base")
        self.assertEqual(
            identity.to_dict(),
            {"provider": "zai", "model": "example", "variant": "base"},
        )
        self.assertEqual(ModelIdentity.from_dict(identity.to_dict()), identity)

    def test_safe_capacity_scope_ref_round_trip(self) -> None:
        scope = CapacityScopeRef(provider="zai", scope_id="coding_plan")
        self.assertEqual(
            scope.to_dict(), {"provider": "zai", "scope_id": "coding_plan"}
        )
        self.assertEqual(CapacityScopeRef.from_dict(scope.to_dict()), scope)
        # Exactly two fields: no window kind, no window id, no model identity.
        self.assertEqual(set(scope.to_dict().keys()), {"provider", "scope_id"})

    def test_unsafe_ids_rejected(self) -> None:
        for bad in ("", "UPPER", "has space", "-leading", ".leading", "a" * 65, "tab\tx"):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelRef(provider="openai", model=bad)
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelIdentity(provider="openai", model="m", variant=bad)
                with self.assertRaises(SelectionContractValidationError):
                    _ = CapacityScopeRef(provider="openai", scope_id=bad)

    def test_provider_restricted_to_supported_set(self) -> None:
        self.assertEqual(SUPPORTED_PROVIDERS, frozenset({"openai", "zai"}))
        for bad in ("anthropic", "ollama", "local", "OpenAI"):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelRef(provider=bad, model="m")
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelIdentity(provider=bad, model="m", variant="v")
                with self.assertRaises(SelectionContractValidationError):
                    _ = CapacityScopeRef(provider=bad, scope_id="s")
                with self.assertRaises(SelectionContractValidationError):
                    _ = HardConstraints(required_provider=cast(str, bad))
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelRef.from_dict({"provider": bad, "model": "m"})

    def test_from_dict_rejects_unknown_and_missing_keys(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelRef.from_dict({"provider": "openai", "model": "m", "extra": 1})
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelRef.from_dict({"provider": "openai"})
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelIdentity.from_dict(
                {"provider": "openai", "model": "m", "variant": "v", "slug": "x"}
            )


# ── CapabilityMinima ──────────────────────────────────────────────────────────


class CapabilityMinimaContract(unittest.TestCase):
    def test_valid_minima_bounds(self) -> None:
        low = CapabilityMinima(reasoning=MIN_RATING)
        high = CapabilityMinima(coding=MAX_RATING)
        self.assertEqual(low.to_dict(), {"reasoning": 1})
        self.assertEqual(high.to_dict(), {"coding": 5})

    def test_unspecified_minima_omitted(self) -> None:
        self.assertEqual(CapabilityMinima().to_dict(), {})
        partial = CapabilityMinima(tool_use=3, translation_multilingual=2)
        self.assertEqual(
            partial.to_dict(), {"tool_use": 3, "translation_multilingual": 2}
        )

    def test_out_of_range_rejected(self) -> None:
        for bad in (0, -1, 6, 100):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = CapabilityMinima(reasoning=bad)

    def test_bool_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityMinima(reasoning=cast("int | None", True))

    def test_non_integer_rejected(self) -> None:
        for bad in ("4", 4.0):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = CapabilityMinima(reasoning=cast("int | None", bad))

    def test_unknown_dimension_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityMinima.from_dict({"reasoning": 3, "orchestration": 4})

    def test_round_trip(self) -> None:
        minima = CapabilityMinima(coding=4, reasoning=4, tool_use=3)
        self.assertEqual(CapabilityMinima.from_dict(minima.to_dict()), minima)
        self.assertEqual(
            CapabilityMinima.from_dict(_clone(minima.to_dict())).to_dict(),
            minima.to_dict(),
        )


# ── HardConstraints ───────────────────────────────────────────────────────────


class HardConstraintsContract(unittest.TestCase):
    def test_valid_numeric_minima(self) -> None:
        hc = HardConstraints(
            minimum_input_context_tokens=1, minimum_output_tokens=131072
        )
        self.assertEqual(
            hc.to_dict(),
            {"minimum_input_context_tokens": 1, "minimum_output_tokens": 131072},
        )

    def test_zero_and_negative_numeric_rejected(self) -> None:
        for bad in (0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = HardConstraints(minimum_input_context_tokens=bad)
                with self.assertRaises(SelectionContractValidationError):
                    _ = HardConstraints(minimum_output_tokens=bad)
                with self.assertRaises(SelectionContractValidationError):
                    _ = HardConstraints.from_dict({"minimum_output_tokens": bad})

    def test_bool_numeric_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = HardConstraints(minimum_input_context_tokens=cast("int | None", True))
        with self.assertRaises(SelectionContractValidationError):
            _ = HardConstraints.from_dict({"minimum_output_tokens": False})

    def test_strict_bools_through_serialized_boundary(self) -> None:
        # Direct construction must be strict too.
        with self.assertRaises(SelectionContractValidationError):
            _ = HardConstraints(requires_tool_use=cast(bool, _ill(1)))
        for bad in (1, 0, "true", "false"):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = HardConstraints.from_dict({"requires_vision": bad})
        # A serialized true round-trips as a strict bool.
        hc = HardConstraints(requires_reasoning_mode=True)
        self.assertIs(
            HardConstraints.from_dict(hc.to_dict()).requires_reasoning_mode, True
        )

    def test_required_model_structured_object(self) -> None:
        hc = HardConstraints(
            required_model=ModelRef(provider="openai", model="example-model")
        )
        self.assertEqual(
            hc.to_dict(),
            {"required_model": {"provider": "openai", "model": "example-model"}},
        )
        self.assertEqual(HardConstraints.from_dict(hc.to_dict()), hc)
        # The stringly qualified form is not a valid serialized shape.
        with self.assertRaises(SelectionContractValidationError):
            _ = HardConstraints.from_dict({"required_model": "openai/example-model"})

    def test_provider_model_contradiction_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = HardConstraints(
                required_provider="openai",
                required_model=ModelRef(provider="zai", model="example"),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = HardConstraints.from_dict(
                {
                    "required_provider": "zai",
                    "required_model": {"provider": "openai", "model": "example"},
                }
            )

    def test_matching_providers_valid(self) -> None:
        hc = HardConstraints(
            required_provider="zai",
            required_model=ModelRef(provider="zai", model="example"),
        )
        self.assertEqual(HardConstraints.from_dict(hc.to_dict()), hc)
        # required_model alone (without required_provider) is fine.
        self.assertEqual(
            HardConstraints(
                required_model=ModelRef(provider="zai", model="example")
            ).to_dict(),
            {"required_model": {"provider": "zai", "model": "example"}},
        )

    def test_safe_variant_and_privacy_ids(self) -> None:
        hc = HardConstraints(
            required_variant="max", privacy_constraint="owner_local_only"
        )
        self.assertEqual(
            hc.to_dict(),
            {"required_variant": "max", "privacy_constraint": "owner_local_only"},
        )
        self.assertEqual(HardConstraints.from_dict(hc.to_dict()), hc)
        for bad in ("Has Space", "UPPER", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = HardConstraints(required_variant=bad)
                with self.assertRaises(SelectionContractValidationError):
                    _ = HardConstraints(privacy_constraint=bad)

    def test_deterministic_compact_serialization(self) -> None:
        self.assertEqual(HardConstraints().to_dict(), {})
        hc = HardConstraints(
            minimum_input_context_tokens=131072,
            requires_tool_use=True,
            required_model=ModelRef(provider="openai", model="example-model"),
        )
        serialized = hc.to_dict()
        self.assertEqual(
            list(serialized.keys()),
            ["minimum_input_context_tokens", "requires_tool_use", "required_model"],
        )
        # None values and false requires_* are omitted, never emitted.
        plain = HardConstraints(requires_vision=False, required_variant=None)
        self.assertEqual(plain.to_dict(), {})
        self.assertEqual(HardConstraints.from_dict(_clone(serialized)), hc)

    def test_full_round_trip(self) -> None:
        hc = HardConstraints(
            minimum_input_context_tokens=200000,
            minimum_output_tokens=8192,
            requires_tool_use=True,
            requires_vision=True,
            requires_reasoning_mode=False,
            required_provider="zai",
            required_model=ModelRef(provider="zai", model="example"),
            required_variant="max",
            privacy_constraint="no_third_party_sharing",
        )
        self.assertEqual(HardConstraints.from_dict(_clone(hc.to_dict())), hc)


# ── TaskRequirement ───────────────────────────────────────────────────────────


class TaskRequirementContract(unittest.TestCase):
    def test_levels_l0_to_l5_valid(self) -> None:
        self.assertEqual(TASK_LEVELS, ("L0", "L1", "L2", "L3", "L4", "L5"))
        for level in TASK_LEVELS:
            with self.subTest(level=level):
                tr = TaskRequirement(
                    task_level=level,
                    capability_minima=CapabilityMinima(),
                    hard_constraints=HardConstraints(),
                )
                self.assertEqual(tr.task_level, level)

    def test_invalid_level_rejected(self) -> None:
        for bad in ("L6", "l4", " L2", "L2 ", 4, None, ""):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = TaskRequirement(
                        task_level=cast(str, bad),
                        capability_minima=CapabilityMinima(),
                        hard_constraints=HardConstraints(),
                    )

    def test_round_trip(self) -> None:
        tr = TaskRequirement(
            task_level="L4",
            capability_minima=CapabilityMinima(coding=4, reasoning=4, tool_use=3),
            hard_constraints=HardConstraints(
                minimum_input_context_tokens=131072, requires_tool_use=True
            ),
        )
        self.assertEqual(TaskRequirement.from_dict(_clone(tr.to_dict())), tr)

    def test_empty_minima_and_constraints_valid(self) -> None:
        tr = TaskRequirement(
            task_level="L1",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(),
        )
        self.assertEqual(
            tr.to_dict(),
            {"task_level": "L1", "capability_minima": {}, "hard_constraints": {}},
        )
        self.assertEqual(TaskRequirement.from_dict(tr.to_dict()), tr)

    def test_no_level_to_capability_inference(self) -> None:
        # A high task level with empty minima must stay empty: the level is
        # never a source of capability minima.
        payload: dict[str, object] = {
            "task_level": "L5",
            "capability_minima": {},
            "hard_constraints": {},
        }
        tr = TaskRequirement.from_dict(payload)
        self.assertEqual(tr.capability_minima.to_dict(), {})
        self.assertEqual(tr.hard_constraints.to_dict(), {})

    def test_component_types_enforced(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = TaskRequirement(
                task_level="L2",
                capability_minima=cast(CapabilityMinima, _ill({"reasoning": 3})),
                hard_constraints=HardConstraints(),
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = TaskRequirement(
                task_level="L2",
                capability_minima=CapabilityMinima(),
                hard_constraints=cast(HardConstraints, _ill(None)),
            )


# ── ModelHardProperties ───────────────────────────────────────────────────────


class ModelHardPropertiesContract(unittest.TestCase):
    def test_all_unknown_allowed(self) -> None:
        props = ModelHardProperties()
        self.assertEqual(props.to_dict(), {})
        self.assertIsNone(props.supports_tool_use)
        self.assertEqual(ModelHardProperties.from_dict({}), props)

    def test_unknown_is_not_false(self) -> None:
        # None stays None through a round trip and is omitted, never emitted
        # as false.
        props = ModelHardProperties(supports_tool_use=None, supports_vision=True)
        self.assertEqual(props.to_dict(), {"supports_vision": True})
        rt = ModelHardProperties.from_dict(props.to_dict())
        self.assertIsNone(rt.supports_tool_use)
        self.assertIs(rt.supports_vision, True)

    def test_positive_allowances(self) -> None:
        props = ModelHardProperties(input_context_tokens=1, output_tokens=1)
        self.assertEqual(
            props.to_dict(), {"input_context_tokens": 1, "output_tokens": 1}
        )
        for bad in (0, -5):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelHardProperties(input_context_tokens=bad)
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelHardProperties(output_tokens=bad)

    def test_strict_bools(self) -> None:
        for bad in (1, 0, "yes"):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelHardProperties(
                        supports_tool_use=cast("bool | None", _ill(bad))
                    )
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelHardProperties.from_dict({"supports_vision": bad})

    def test_duplicate_privacy_tags_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelHardProperties(privacy_tags=("owner_only", "owner_only"))

    def test_privacy_tags_validation_and_canonical_serialization(self) -> None:
        props = ModelHardProperties(privacy_tags=("second_tag", "first_tag"))
        # Deterministic: canonical sorted order independent of construction.
        self.assertEqual(
            props.to_dict(), {"privacy_tags": ["first_tag", "second_tag"]}
        )
        self.assertEqual(ModelHardProperties.from_dict(props.to_dict()), props)
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelHardProperties(privacy_tags=("Has Space",))
        # None (not established) and an explicit empty set are distinct.
        none_props = ModelHardProperties()
        empty_props = ModelHardProperties(privacy_tags=())
        self.assertIsNone(none_props.privacy_tags)
        self.assertEqual(empty_props.to_dict(), {"privacy_tags": []})
        self.assertEqual(
            ModelHardProperties.from_dict(empty_props.to_dict()), empty_props
        )

    def test_full_round_trip(self) -> None:
        props = ModelHardProperties(
            input_context_tokens=400000,
            output_tokens=128000,
            supports_tool_use=True,
            supports_vision=False,
            supports_reasoning_mode=None,
            privacy_tags=("owner_only",),
        )
        self.assertEqual(ModelHardProperties.from_dict(_clone(props.to_dict())), props)


# ── EvidenceRef / HumanOverride ───────────────────────────────────────────────


class EvidenceRefContract(unittest.TestCase):
    def test_valid_refs(self) -> None:
        ref = EvidenceRef(
            source="official_docs",
            identifier="https://example.com/docs/model-card",
            version="2026-09",
            date="2026-09-01",
        )
        self.assertEqual(
            ref.to_dict(),
            {
                "source": "official_docs",
                "identifier": "https://example.com/docs/model-card",
                "version": "2026-09",
                "date": "2026-09-01",
            },
        )
        self.assertEqual(EvidenceRef.from_dict(ref.to_dict()), ref)
        # DOI-like and benchmark-style identifiers are allowed.
        doi = EvidenceRef(source="benchmark", identifier="doi:10.1000/example.2026")
        self.assertEqual(EvidenceRef.from_dict(doi.to_dict()), doi)

    def test_source_grammar(self) -> None:
        for good in (
            "official_docs",
            "benchmark",
            "owner_observation",
            "artificial_analysis",
        ):
            _ = EvidenceRef(source=good, identifier="x")
        for bad in ("Official Docs", "has space", "", "a" * 65):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = EvidenceRef(source=bad, identifier="x")

    def test_identifier_bounds(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = EvidenceRef(source="benchmark", identifier="")
        with self.assertRaises(SelectionContractValidationError):
            _ = EvidenceRef(source="benchmark", identifier="bad\nidentifier")
        with self.assertRaises(SelectionContractValidationError):
            _ = EvidenceRef(source="benchmark", identifier=" pad")
        with self.assertRaises(SelectionContractValidationError):
            _ = EvidenceRef(source="benchmark", identifier="x" * 513)

    def test_date_validated(self) -> None:
        for bad in ("2026-13-01", "20260906", "09/06/2026", "2026-09"):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = EvidenceRef(source="benchmark", identifier="x", date=bad)


class HumanOverrideContract(unittest.TestCase):
    def test_valid_override(self) -> None:
        override = HumanOverride(
            rating=3, decided_on="2026-09-06", rationale="reserve headroom"
        )
        self.assertEqual(
            override.to_dict(),
            {"rating": 3, "decided_on": "2026-09-06", "rationale": "reserve headroom"},
        )
        self.assertEqual(HumanOverride.from_dict(override.to_dict()), override)

    def test_rating_bounds(self) -> None:
        for bad in (0, 6, -1, True, "4", 4.0):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = HumanOverride(
                        rating=cast(int, bad), decided_on="2026-09-06", rationale="r"
                    )

    def test_date_and_rationale_validated(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = HumanOverride(rating=4, decided_on="2026-9-6", rationale="r")
        for bad in ("", "  ", "line1\nline2"):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = HumanOverride(rating=4, decided_on="2026-09-06", rationale=bad)

    def test_no_identity_fields(self) -> None:
        override = HumanOverride(rating=2, decided_on="2026-09-06", rationale="r")
        self.assertEqual(
            set(override.to_dict().keys()), {"rating", "decided_on", "rationale"}
        )
        with self.assertRaises(SelectionContractValidationError):
            _ = HumanOverride.from_dict(
                {
                    "rating": 2,
                    "decided_on": "2026-09-06",
                    "rationale": "r",
                    "user": "someone",
                }
            )


# ── CapabilityAssessment ──────────────────────────────────────────────────────


class CapabilityAssessmentContract(unittest.TestCase):
    def test_unknown_rating_representation(self) -> None:
        unk = CapabilityAssessment(rating=None)
        self.assertEqual(unk.to_dict(), {"rating": None})
        self.assertIsNone(unk.effective_rating)
        self.assertEqual(CapabilityAssessment.from_dict({"rating": None}), unk)

    def test_unknown_rating_rejects_partial_provenance(self) -> None:
        ev = EvidenceRef(source="benchmark", identifier="x")
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(rating=None, evidence=(ev,))
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(rating=None, confidence="high")
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(rating=None, assessed_on="2026-09-06")
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(rating=None, rationale="not yet assessed")
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(
                rating=None,
                human_override=HumanOverride(
                    rating=3, decided_on="2026-09-06", rationale="r"
                ),
            )

    def test_zero_and_out_of_range_invalid(self) -> None:
        for bad in (0, 6, -2, True, "4", 4.0):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = CapabilityAssessment(rating=cast("int | None", bad))

    def test_known_rating_requires_complete_provenance(self) -> None:
        ev = EvidenceRef(
            source="owner_observation", identifier="workflow note", date="2026-09-06"
        )
        complete = CapabilityAssessment(
            rating=4,
            evidence=(ev,),
            confidence="high",
            assessed_on="2026-09-06",
            rationale="owner workflow experience",
        )
        self.assertEqual(CapabilityAssessment.from_dict(complete.to_dict()), complete)
        # A naked rating is invalid, in construction and through the boundary.
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(rating=4)
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment.from_dict({"rating": 4})
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(
                rating=4,
                evidence=(),
                confidence="low",
                assessed_on="2026-09-06",
                rationale="r",
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(
                rating=4,
                evidence=(ev,),
                confidence=None,
                assessed_on="2026-09-06",
                rationale="r",
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(
                rating=4,
                evidence=(ev,),
                confidence="low",
                assessed_on=None,
                rationale="r",
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(
                rating=4,
                evidence=(ev,),
                confidence="low",
                assessed_on="2026-09-06",
                rationale=None,
            )

    def test_confidence_allowlist(self) -> None:
        ev = (EvidenceRef(source="benchmark", identifier="x"),)
        for good in sorted(CONFIDENCE_VALUES):
            with self.subTest(confidence=good):
                _ = CapabilityAssessment(
                    rating=2,
                    evidence=ev,
                    confidence=good,
                    assessed_on="2026-09-06",
                    rationale="r",
                )
        for bad in ("unknown", "HIGH", "medium-high", 3):
            with self.subTest(confidence=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = CapabilityAssessment(
                        rating=2,
                        evidence=ev,
                        confidence=cast("str | None", bad),
                        assessed_on="2026-09-06",
                        rationale="r",
                    )

    def test_invalid_dates_rejected(self) -> None:
        ev = (EvidenceRef(source="benchmark", identifier="x"),)
        for bad in ("2026-13-01", "20260906", "yesterday"):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = CapabilityAssessment(
                        rating=2,
                        evidence=ev,
                        confidence="low",
                        assessed_on=bad,
                        rationale="r",
                    )

    def test_override_preserves_base_rating_and_evidence(self) -> None:
        ev = EvidenceRef(source="owner_observation", identifier="workflow note")
        assessed = CapabilityAssessment(
            rating=4,
            evidence=(ev,),
            confidence="medium",
            assessed_on="2026-09-06",
            rationale="initial curation",
            human_override=HumanOverride(
                rating=3, decided_on="2026-09-06", rationale="reserve"
            ),
        )
        # The base rating and evidence are never mutated.
        self.assertEqual(assessed.rating, 4)
        self.assertEqual(assessed.evidence, (ev,))
        serialized = assessed.to_dict()
        self.assertEqual(serialized["rating"], 4)
        self.assertEqual(
            serialized["human_override"],
            {"rating": 3, "decided_on": "2026-09-06", "rationale": "reserve"},
        )
        self.assertEqual(len(cast("list[object]", serialized["evidence"])), 1)
        rt = CapabilityAssessment.from_dict(_clone(serialized))
        self.assertEqual(rt, assessed)
        self.assertEqual(rt.effective_rating, 3)

    def test_effective_rating(self) -> None:
        ev = (EvidenceRef(source="benchmark", identifier="x"),)
        plain = CapabilityAssessment(
            rating=5,
            evidence=ev,
            confidence="low",
            assessed_on="2026-09-06",
            rationale="r",
        )
        self.assertEqual(plain.effective_rating, 5)
        overridden = CapabilityAssessment(
            rating=5,
            evidence=ev,
            confidence="low",
            assessed_on="2026-09-06",
            rationale="r",
            human_override=HumanOverride(
                rating=1, decided_on="2026-09-06", rationale="r"
            ),
        )
        self.assertEqual(overridden.effective_rating, 1)
        self.assertIsNone(CapabilityAssessment(rating=None).effective_rating)

    def test_override_requires_known_base(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessment(
                rating=None,
                human_override=HumanOverride(
                    rating=4, decided_on="2026-09-06", rationale="r"
                ),
            )


# ── CapabilityAssessments vector ──────────────────────────────────────────────


class CapabilityVectorContract(unittest.TestCase):
    def test_all_six_dimensions_required(self) -> None:
        self.assertEqual(len(CAPABILITY_DIMENSIONS), 6)
        # Five positional arguments cannot construct the fixed six-dimension
        # vector (translation_multilingual would be missing).
        five: list[CapabilityAssessment] = [UNKNOWN_ASSESSMENT] * 5
        with self.assertRaises(TypeError):
            _ = CapabilityAssessments(*five)
        payload = {dim: {"rating": None} for dim in CAPABILITY_DIMENSIONS}
        del payload["tool_use"]
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessments.from_dict(cast("dict[str, object]", payload))

    def test_extra_dimension_rejected(self) -> None:
        payload = {dim: {"rating": None} for dim in CAPABILITY_DIMENSIONS}
        payload["orchestration"] = {"rating": None}
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessments.from_dict(cast("dict[str, object]", payload))

    def test_unknown_rating_remains_explicit_null(self) -> None:
        vector = _unknown_vector()
        serialized = vector.to_dict()
        self.assertEqual(set(serialized.keys()), set(CAPABILITY_DIMENSIONS))
        for dim in CAPABILITY_DIMENSIONS:
            self.assertEqual(serialized[dim], {"rating": None})
        self.assertEqual(CapabilityAssessments.from_dict(serialized), vector)

    def test_round_trip_mixed(self) -> None:
        ev = (EvidenceRef(source="benchmark", identifier="x"),)
        known = CapabilityAssessment(
            rating=3,
            evidence=ev,
            confidence="medium",
            assessed_on="2026-09-06",
            rationale="r",
        )
        vector = CapabilityAssessments(
            reasoning=known,
            coding=UNKNOWN_ASSESSMENT,
            scientific_methodological=known,
            writing_editorial=UNKNOWN_ASSESSMENT,
            tool_use=known,
            translation_multilingual=UNKNOWN_ASSESSMENT,
        )
        self.assertEqual(
            CapabilityAssessments.from_dict(_clone(vector.to_dict())), vector
        )

    def test_dimension_elements_enforced(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = CapabilityAssessments(
                reasoning=cast(CapabilityAssessment, _ill(None)),
                coding=UNKNOWN_ASSESSMENT,
                scientific_methodological=UNKNOWN_ASSESSMENT,
                writing_editorial=UNKNOWN_ASSESSMENT,
                tool_use=UNKNOWN_ASSESSMENT,
                translation_multilingual=UNKNOWN_ASSESSMENT,
            )


# ── Capacity bindings and ModelCatalogEntry ───────────────────────────────────


def _entry(
    provider: str = "openai",
    model: str = "example",
    variant: str = "base",
    bindings: tuple[CapacityScopeRef, ...] | None = None,
) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        identity=ModelIdentity(provider=provider, model=model, variant=variant),
        display_name="Example Model",
        hard_properties=ModelHardProperties(),
        capabilities=_unknown_vector(),
        capacity_bindings=bindings,
    )


class CapacityBindingsContract(unittest.TestCase):
    def test_none_means_unknown_applicability(self) -> None:
        entry = _entry(bindings=None)
        serialized = entry.to_dict()
        self.assertIsNone(serialized["capacity_bindings"])
        self.assertIsNone(ModelCatalogEntry.from_dict(serialized).capacity_bindings)

    def test_empty_binding_set_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _entry(bindings=())
        empty = _entry(bindings=None).to_dict()
        empty["capacity_bindings"] = []
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelCatalogEntry.from_dict(_clone(empty))

    def test_known_bindings_valid(self) -> None:
        single = _entry(
            provider="zai",
            bindings=(CapacityScopeRef(provider="zai", scope_id="coding_plan"),),
        )
        self.assertEqual(
            single.to_dict()["capacity_bindings"],
            [{"provider": "zai", "scope_id": "coding_plan"}],
        )
        multiple = _entry(
            provider="openai",
            bindings=(
                CapacityScopeRef(provider="openai", scope_id="codex"),
                CapacityScopeRef(provider="openai", scope_id="extra_bucket"),
            ),
        )
        serialized = multiple.to_dict()
        self.assertEqual(
            cast("list[object]", serialized["capacity_bindings"]),
            [
                {"provider": "openai", "scope_id": "codex"},
                {"provider": "openai", "scope_id": "extra_bucket"},
            ],
        )
        self.assertEqual(ModelCatalogEntry.from_dict(_clone(serialized)), multiple)

    def test_duplicate_binding_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _entry(
                provider="zai",
                bindings=(
                    CapacityScopeRef(provider="zai", scope_id="coding_plan"),
                    CapacityScopeRef(provider="zai", scope_id="coding_plan"),
                ),
            )

    def test_cross_provider_binding_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _entry(
                provider="openai",
                bindings=(CapacityScopeRef(provider="zai", scope_id="coding_plan"),),
            )
        crossed = _entry(provider="openai").to_dict()
        crossed["capacity_bindings"] = [{"provider": "zai", "scope_id": "coding_plan"}]
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelCatalogEntry.from_dict(_clone(crossed))

    def test_bindings_canonical_order(self) -> None:
        entry = _entry(
            provider="openai",
            bindings=(
                CapacityScopeRef(provider="openai", scope_id="zeta"),
                CapacityScopeRef(provider="openai", scope_id="alpha"),
            ),
        )
        serialized = entry.to_dict()
        bindings = cast("list[dict[str, object]]", serialized["capacity_bindings"])
        self.assertEqual(
            [b["scope_id"] for b in bindings], ["alpha", "zeta"]
        )


class ModelCatalogEntryContract(unittest.TestCase):
    def test_minimal_unknown_entry_round_trip(self) -> None:
        entry = _entry()
        self.assertEqual(ModelCatalogEntry.from_dict(_clone(entry.to_dict())), entry)
        serialized = entry.to_dict()
        # Unknown capability and unknown applicability both survive.
        self.assertEqual(
            cast("dict[str, object]", serialized["capabilities"])["reasoning"],
            {"rating": None},
        )
        self.assertIsNone(serialized["capacity_bindings"])

    def test_display_name_validation(self) -> None:
        for bad in ("", " ", "bad\nname", "x" * 129):
            with self.subTest(bad=bad):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelCatalogEntry(
                        identity=ModelIdentity(provider="openai", model="m", variant="v"),
                        display_name=bad,
                        hard_properties=ModelHardProperties(),
                        capabilities=_unknown_vector(),
                        capacity_bindings=None,
                    )

    def test_metadata_validation(self) -> None:
        for bad_date in ("2026-13-01", "20260906"):
            with self.subTest(bad_date=bad_date):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelCatalogEntry(
                        identity=ModelIdentity(provider="openai", model="m", variant="v"),
                        display_name="M",
                        hard_properties=ModelHardProperties(),
                        capabilities=_unknown_vector(),
                        capacity_bindings=None,
                        model_version_date=bad_date,
                    )
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelCatalogEntry(
                        identity=ModelIdentity(provider="openai", model="m", variant="v"),
                        display_name="M",
                        hard_properties=ModelHardProperties(),
                        capabilities=_unknown_vector(),
                        capacity_bindings=None,
                        last_reviewed_on=bad_date,
                    )
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelCatalogEntry(
                identity=ModelIdentity(provider="openai", model="m", variant="v"),
                display_name="M",
                hard_properties=ModelHardProperties(),
                capabilities=_unknown_vector(),
                capacity_bindings=None,
                model_version="",
            )

    def test_binding_element_types_enforced(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _entry(
                bindings=cast(
                    "tuple[CapacityScopeRef, ...]", ("openai:codex",)
                )
            )


# ── ModelCatalog container ────────────────────────────────────────────────────


class ModelCatalogContract(unittest.TestCase):
    def test_empty_catalog_structurally_valid(self) -> None:
        catalog = ModelCatalog(catalog_version=1, updated_on="2026-09-06", entries=())
        self.assertEqual(
            catalog.to_dict(),
            {"catalog_version": 1, "updated_on": "2026-09-06", "entries": []},
        )
        self.assertEqual(ModelCatalog.from_dict(catalog.to_dict()), catalog)

    def test_duplicate_identity_rejected(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelCatalog(
                catalog_version=1,
                updated_on="2026-09-06",
                entries=(_entry(), _entry()),
            )

    def test_deterministic_entry_order(self) -> None:
        a = _entry(provider="openai", model="a-model")
        z = _entry(provider="openai", model="z-model")
        zai_entry = _entry(provider="zai", model="a-model")
        forward = ModelCatalog(1, "2026-09-06", (zai_entry, a, z))
        backward = ModelCatalog(1, "2026-09-06", (z, a, zai_entry))
        self.assertEqual(forward.to_dict(), backward.to_dict())
        order = [
            cast("dict[str, object]", e)["identity"]
            for e in cast("list[object]", backward.to_dict()["entries"])
        ]
        self.assertEqual(
            order,
            [
                {"provider": "openai", "model": "a-model", "variant": "base"},
                {"provider": "openai", "model": "z-model", "variant": "base"},
                {"provider": "zai", "model": "a-model", "variant": "base"},
            ],
        )

    def test_round_trip(self) -> None:
        catalog = ModelCatalog(
            catalog_version=3,
            updated_on="2026-09-06",
            entries=(
                _entry(provider="openai", model="a-model"),
                _entry(
                    provider="zai",
                    model="b-model",
                    bindings=(
                        CapacityScopeRef(provider="zai", scope_id="coding_plan"),
                    ),
                ),
            ),
        )
        self.assertEqual(ModelCatalog.from_dict(_clone(catalog.to_dict())), catalog)

    def test_unknown_capability_and_applicability_survive(self) -> None:
        catalog = ModelCatalog(1, "2026-09-06", (_entry(bindings=None),))
        rt = ModelCatalog.from_dict(_clone(catalog.to_dict()))
        entry = rt.entries[0]
        self.assertEqual(entry.capabilities.reasoning.to_dict(), {"rating": None})
        self.assertIsNone(entry.capacity_bindings)

    def test_container_validation(self) -> None:
        for bad_version in (0, -1, True):
            with self.subTest(bad_version=bad_version):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelCatalog(
                        catalog_version=cast(int, bad_version),
                        updated_on="2026-09-06",
                        entries=(),
                    )
        for bad_date in ("2026-13-01", "20260906"):
            with self.subTest(bad_date=bad_date):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelCatalog(catalog_version=1, updated_on=bad_date, entries=())
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelCatalog.from_dict(
                {"catalog_version": 1, "updated_on": "2026-09-06"}
            )
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelCatalog.from_dict(
                {
                    "catalog_version": 1,
                    "updated_on": "2026-09-06",
                    "entries": {},
                    "extra": 1,
                }
            )


# ── Error family separation ───────────────────────────────────────────────────


class ErrorFamilyContract(unittest.TestCase):
    def test_hierarchy_and_separation(self) -> None:
        # The negative relationships are the contract: selection-contract
        # errors are a family of their own, never capacity errors.
        self.assertFalse(
            issubclass(SelectionContractValidationError, CapacityValidationError)
        )
        self.assertFalse(issubclass(SelectionContractError, CapacityValidationError))
        # Behavioral check: selection validation raises the selection family.
        with self.assertRaises(SelectionContractError):
            _ = CapabilityMinima(reasoning=0)

    def test_exported_from_package(self) -> None:
        exported = set(scarcity_router.__all__)
        for name in (
            "ModelRef",
            "ModelIdentity",
            "CapacityScopeRef",
            "CapabilityMinima",
            "HardConstraints",
            "TaskRequirement",
            "ModelHardProperties",
            "EvidenceRef",
            "HumanOverride",
            "CapabilityAssessment",
            "CapabilityAssessments",
            "ModelCatalogEntry",
            "ModelCatalog",
            "SelectionContractError",
            "SelectionContractValidationError",
        ):
            self.assertIn(name, exported)


# ── Repository policy consistency ─────────────────────────────────────────────


class ModelPolicyConsistency(unittest.TestCase):
    """The frozen vocabularies must still agree with model-policy.json.

    If the policy artifact changes later, these tests force an explicit
    contract update in code (and a decision record entry). Numeric minima are
    deliberately NOT compared because none exist yet.
    """

    def test_capability_dimensions_match_policy(self) -> None:
        policy = _load_policy()
        dims = policy["capability_dimensions"]
        self.assertIsInstance(dims, list)
        ids = [
            cast("dict[str, object]", d)["id"] for d in cast("list[object]", dims)
        ]
        self.assertEqual(list(CAPABILITY_DIMENSIONS), ids)

    def test_task_levels_match_policy(self) -> None:
        policy = _load_policy()
        levels = policy["task_levels"]
        self.assertIsInstance(levels, list)
        ids = [
            cast("dict[str, object]", level)["id"]
            for level in cast("list[object]", levels)
        ]
        self.assertEqual(list(TASK_LEVELS), ids)

    def test_policy_still_defers_numeric_minima(self) -> None:
        policy = _load_policy()
        profile_policy = cast("dict[str, object]", policy["task_profile_policy"])
        self.assertIs(profile_policy["numeric_minima_included"], False)
        self.assertEqual(
            profile_policy["numeric_minima_status"], "deferred_to_m2_calibration"
        )


# ── Anti-inference contract assertions ────────────────────────────────────────


class AntiInferenceContract(unittest.TestCase):
    """The M2b module must not pre-freeze what M2c has not calibrated."""

    def test_no_profile_resolver_or_selector_in_production(self) -> None:
        for forbidden in (
            "expand_profile",
            "resolve_profile",
            "profile_to_requirement",
            "select",
            "score",
            "sufficiency",
            "rank",
        ):
            self.assertFalse(
                hasattr(st, forbidden),
                f"selection_types must not define {forbidden!r} in M2b",
            )

    def test_module_source_is_pure(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        # No I/O, environment, network or process access.
        for token in (
            "import os",
            "import sys",
            "import socket",
            "import subprocess",
            "import pathlib",
            "import importlib",
            "import json",
            "import urllib",
            "import requests",
            "os.environ",
            "open(",
        ):
            self.assertNotIn(
                token, source, f"forbidden token {token!r} in selection_types"
            )
        # No capacity window/slug identity leakage into the selection contract.
        for token in ("window_id", "normalModelSlug", "model-policy", "model_policy"):
            self.assertNotIn(
                token, source, f"forbidden token {token!r} in selection_types"
            )
        # No concrete model names or model-class → eligibility mapping.
        model_name = re.compile(r"\bgpt\b|\bluna\b|\bsol\b|\bglm\b", re.IGNORECASE)
        self.assertIsNone(model_name.search(source))


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
