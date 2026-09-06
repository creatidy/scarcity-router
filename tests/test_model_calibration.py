"""M2c calibration acceptance tests (D-025).

These tests pin the accepted curated content of the frozen M2a/M2b contracts:
the four-model catalog artifact, its provenance, the calibrated task profiles
in ``model-policy.json`` and the pure profile-expansion mechanism. They use a
TEST-ONLY capability-sufficiency helper (every specified minimum must pass
against ``effective_rating``; unknown ratings fail) to prove the calibration's
capability-only eligible sets. No production selector, scorer or scarcity
logic is implemented here.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scarcity_router.errors import SelectionContractValidationError
from scarcity_router.selection_types import (
    CAPABILITY_DIMENSIONS,
    CapabilityAssessment,
    CapabilityAssessments,
    CapabilityMinima,
    ModelCatalog,
    ModelCatalogEntry,
    TaskProfileCatalog,
    TaskProfileDefinition,
    TaskRequirement,
)

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"
POLICY_PATH = REPO / "model-policy.json"

ASSESSED_ON = "2026-09-06"

LUNA = ("openai", "gpt-5.6-luna", "max")
SOL = ("openai", "gpt-5.6-sol", "high")
GLM53 = ("zai", "glm-5.3", "max")
FLASH = ("zai", "glm-5.3-flash", "max")
ALL_MODELS = frozenset({LUNA, SOL, GLM53, FLASH})

# The accepted initial calibration (D-025). Ratings describe routing
# suitability in the owner's workflow; they are not benchmark percentiles.
ACCEPTED_RATINGS: dict[tuple[str, str, str], dict[str, int]] = {
    LUNA: {
        "reasoning": 4,
        "coding": 4,
        "scientific_methodological": 3,
        "writing_editorial": 5,
        "tool_use": 5,
        "translation_multilingual": 4,
    },
    SOL: {
        "reasoning": 5,
        "coding": 5,
        "scientific_methodological": 5,
        "writing_editorial": 5,
        "tool_use": 5,
        "translation_multilingual": 5,
    },
    GLM53: {
        "reasoning": 5,
        "coding": 5,
        "scientific_methodological": 4,
        "writing_editorial": 4,
        "tool_use": 5,
        "translation_multilingual": 4,
    },
    FLASH: {
        "reasoning": 4,
        "coding": 4,
        "scientific_methodological": 3,
        "writing_editorial": 4,
        "tool_use": 5,
        "translation_multilingual": 4,
    },
}

ACCEPTED_HARD_PROPERTIES: dict[tuple[str, str, str], dict[str, object]] = {
    LUNA: {
        "input_context_tokens": 1_050_000,
        "output_tokens": 128_000,
        "supports_tool_use": True,
        "supports_vision": True,
        "supports_reasoning_mode": True,
    },
    SOL: {
        "input_context_tokens": 1_050_000,
        "output_tokens": 128_000,
        "supports_tool_use": True,
        "supports_vision": True,
        "supports_reasoning_mode": True,
    },
    # GLM-5.3 vision=false is an evidenced negative fact (first-party
    # "text-only inputs" documentation), and the GLM output allowance is
    # first-party documented at 128K; both serialize explicitly. `None`
    # stays reserved for genuinely unknown properties.
    GLM53: {
        "input_context_tokens": 1_000_000,
        "output_tokens": 128_000,
        "supports_tool_use": True,
        "supports_vision": False,
        "supports_reasoning_mode": True,
    },
    FLASH: {
        "input_context_tokens": 1_000_000,
        "output_tokens": 128_000,
        "supports_tool_use": True,
        "supports_vision": True,
        "supports_reasoning_mode": True,
    },
}

ACCEPTED_BINDINGS: dict[tuple[str, str, str], set[tuple[str, str]]] = {
    LUNA: {("openai", "codex")},
    SOL: {("openai", "codex")},
    GLM53: {("zai", "coding_plan")},
    FLASH: {("zai", "coding_plan")},
}

ACCEPTED_MODEL_VERSION_DATES: dict[tuple[str, str, str], str] = {
    LUNA: "2026-07-09",
    SOL: "2026-07-09",
    GLM53: "2026-08-14",
    FLASH: "2026-08-26",
}

# Capability-only eligible sets (ignoring scarcity, capacity and policy):
# which calibrated models satisfy every profile minimum.
ACCEPTED_ELIGIBLE_SETS: dict[str, frozenset[tuple[str, str, str]]] = {
    "mechanical": frozenset(ALL_MODELS),
    "routine_coding": frozenset(ALL_MODELS),
    "deep_coding": frozenset({SOL, GLM53}),
    "scientific_review": frozenset({SOL}),
    "editorial": frozenset({LUNA, SOL}),
    "general_reasoning": frozenset(ALL_MODELS),
    "orchestration": frozenset({LUNA, SOL}),
    "translation": frozenset({SOL}),
}

FORMAL_PROFILE_IDS = frozenset(ACCEPTED_ELIGIBLE_SETS)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"{name} must be a JSON object")
    return cast(dict[str, object], value)


def _load_catalog() -> ModelCatalog:
    value = cast(object, json.loads(_load_catalog_text()))
    return ModelCatalog.from_dict(value)


def _load_catalog_text() -> str:
    return CATALOG_PATH.read_text(encoding="utf-8")


def _load_policy() -> dict[str, object]:
    value = cast(object, json.loads(POLICY_PATH.read_text(encoding="utf-8")))
    return _mapping(value, "model-policy.json")


def _entry_by_identity(
    catalog: ModelCatalog,
) -> dict[tuple[str, str, str], ModelCatalogEntry]:
    return {
        (e.identity.provider, e.identity.model, e.identity.variant): e
        for e in catalog.entries
    }


def _assessments(capabilities: CapabilityAssessments) -> dict[str, CapabilityAssessment]:
    return {
        "reasoning": capabilities.reasoning,
        "coding": capabilities.coding,
        "scientific_methodological": capabilities.scientific_methodological,
        "writing_editorial": capabilities.writing_editorial,
        "tool_use": capabilities.tool_use,
        "translation_multilingual": capabilities.translation_multilingual,
    }


def _policy_profile_entries() -> dict[str, dict[str, object]]:
    policy = _load_policy()
    profiles = cast("list[object]", policy["task_profiles"])
    return {
        cast(str, _mapping(raw, "task_profile")["id"]): _mapping(raw, "task_profile")
        for raw in profiles
    }


def _build_profile_catalog_from_policy() -> TaskProfileCatalog:
    definitions: list[TaskProfileDefinition] = []
    for profile_id, entry in _policy_profile_entries().items():
        definitions.append(
            TaskProfileDefinition(
                profile_id=profile_id,
                requirement=TaskRequirement.from_dict(
                    entry["calibrated_requirement"]
                ),
            )
        )
    return TaskProfileCatalog(definitions=tuple(definitions))


def _minima(minimum: CapabilityMinima) -> dict[str, int | None]:
    return {
        "reasoning": minimum.reasoning,
        "coding": minimum.coding,
        "scientific_methodological": minimum.scientific_methodological,
        "writing_editorial": minimum.writing_editorial,
        "tool_use": minimum.tool_use,
        "translation_multilingual": minimum.translation_multilingual,
    }


def _capability_eligible(
    minimum: CapabilityMinima, capabilities: CapabilityAssessments
) -> bool:
    """TEST-ONLY sufficiency check: every specified minimum must pass.

    Uses ``effective_rating``; an unknown rating fails. No averaging, no
    scoring, no production logic.
    """
    required_by_dimension = _minima(minimum)
    ratings_by_dimension = _assessments(capabilities)
    for dimension in CAPABILITY_DIMENSIONS:
        required = required_by_dimension[dimension]
        if required is None:
            continue
        rating = ratings_by_dimension[dimension].effective_rating
        if rating is None or rating < required:
            return False
    return True


_CATALOG = _load_catalog()
_BY_IDENTITY = _entry_by_identity(_CATALOG)
_PROFILE_CATALOG = _build_profile_catalog_from_policy()
_PROFILES = _policy_profile_entries()


class ModelCatalogCalibration(unittest.TestCase):
    def test_catalog_parses_with_accepted_version_and_dates(self) -> None:
        self.assertEqual(_CATALOG.catalog_version, 1)
        self.assertEqual(_CATALOG.updated_on, ASSESSED_ON)
        for entry in _CATALOG.entries:
            self.assertEqual(entry.last_reviewed_on, ASSESSED_ON)
            self.assertEqual(
                entry.model_version_date,
                ACCEPTED_MODEL_VERSION_DATES[
                    (entry.identity.provider, entry.identity.model, entry.identity.variant)
                ],
            )
            self.assertIsNone(entry.model_version)

    def test_exactly_four_accepted_identities(self) -> None:
        self.assertEqual(len(_CATALOG.entries), 4)
        self.assertEqual(set(_BY_IDENTITY), set(ALL_MODELS))

    def test_no_additional_model_slipped_into_catalog(self) -> None:
        for identity, entry in _BY_IDENTITY.items():
            serialized = json.dumps(entry.to_dict()).lower()
            for forbidden in ("astra", "terra", "claude", "gemini", "kimi", "deepseek", "ollama"):
                self.assertNotIn(forbidden, serialized, identity)

    def test_exact_rating_matrix(self) -> None:
        for identity, expected in ACCEPTED_RATINGS.items():
            entry = _BY_IDENTITY[identity]
            for dimension, assessment in _assessments(entry.capabilities).items():
                self.assertEqual(
                    assessment.effective_rating,
                    expected[dimension],
                    f"{identity}.{dimension}",
                )

    def test_exact_hard_property_matrix(self) -> None:
        for identity, expected in ACCEPTED_HARD_PROPERTIES.items():
            props = _BY_IDENTITY[identity].hard_properties
            self.assertEqual(
                props.input_context_tokens, expected["input_context_tokens"], identity
            )
            self.assertEqual(props.output_tokens, expected["output_tokens"], identity)
            self.assertEqual(
                props.supports_tool_use, expected["supports_tool_use"], identity
            )
            self.assertEqual(props.supports_vision, expected["supports_vision"], identity)
            self.assertEqual(
                props.supports_reasoning_mode,
                expected["supports_reasoning_mode"],
                identity,
            )
            self.assertIsNone(props.privacy_tags)

    def test_evidenced_hard_properties_serialize_explicitly(self) -> None:
        # GLM-5.3's vision=false is a KNOWN negative fact from first-party
        # "text-only inputs" documentation: it must serialize explicitly,
        # never be omitted (None remains reserved for genuinely unknown
        # properties). The GLM output allowances are first-party documented.
        glm = _BY_IDENTITY[GLM53]
        self.assertFalse(glm.hard_properties.supports_vision)
        self.assertEqual(glm.hard_properties.output_tokens, 128_000)
        serialized_glm = glm.hard_properties.to_dict()
        self.assertEqual(serialized_glm.get("supports_vision"), False)
        self.assertEqual(serialized_glm.get("output_tokens"), 128_000)
        flash = _BY_IDENTITY[FLASH]
        self.assertTrue(flash.hard_properties.supports_vision)
        self.assertEqual(flash.hard_properties.output_tokens, 128_000)
        serialized_flash = flash.hard_properties.to_dict()
        self.assertEqual(serialized_flash.get("output_tokens"), 128_000)
        # Known properties on all entries remain explicit.
        for identity in (LUNA, SOL, GLM53, FLASH):
            props = _BY_IDENTITY[identity].hard_properties
            self.assertTrue(props.supports_tool_use)
            self.assertTrue(props.supports_reasoning_mode)

    def test_exact_capacity_binding_matrix(self) -> None:
        for identity, expected in ACCEPTED_BINDINGS.items():
            entry = _BY_IDENTITY[identity]
            self.assertIsNotNone(entry.capacity_bindings)
            bindings = entry.capacity_bindings
            assert bindings is not None
            self.assertTrue(bindings)
            actual = {(b.provider, b.scope_id) for b in bindings}
            self.assertEqual(actual, expected, f"{identity} bindings")
            for binding in bindings:
                self.assertEqual(binding.provider, entry.identity.provider)

    def test_no_additional_openai_scope_recorded(self) -> None:
        for identity in (LUNA, SOL):
            bindings = _BY_IDENTITY[identity].capacity_bindings
            assert bindings is not None
            scope_ids = {b.scope_id for b in bindings}
            self.assertEqual(scope_ids, {"codex"})

    def test_no_human_override_anywhere(self) -> None:
        for entry in _CATALOG.entries:
            for dimension, assessment in _assessments(entry.capabilities).items():
                self.assertIsNone(assessment.human_override, dimension)
                self.assertEqual(assessment.effective_rating, assessment.rating, dimension)

    def test_complete_provenance_for_every_rating(self) -> None:
        for entry in _CATALOG.entries:
            for dimension, assessment in _assessments(entry.capabilities).items():
                self.assertIsNotNone(assessment.rating, dimension)
                self.assertIn(assessment.rating, range(1, 6))
                self.assertTrue(assessment.evidence, dimension)
                self.assertIn(assessment.confidence, ("low", "medium", "high"))
                self.assertEqual(assessment.assessed_on, ASSESSED_ON, dimension)
                self.assertTrue(assessment.rationale)
                for ref in assessment.evidence:
                    self.assertIn(
                        ref.source,
                        ("official_docs", "artificial_analysis", "owner_observation"),
                    )
                    self.assertTrue(ref.identifier)

    def test_no_external_metric_stored_as_internal_rating(self) -> None:
        # Internal ratings are the frozen ordinal 1..5 only; benchmark index
        # values (dozens on AA's scale) can never appear as a rating, and
        # evidence identifiers are references, not ratings.
        for entry in _CATALOG.entries:
            for assessment in _assessments(entry.capabilities).values():
                self.assertIsInstance(assessment.rating, int)
                self.assertIn(assessment.rating, range(1, 6))
                for ref in assessment.evidence:
                    self.assertIsInstance(ref.identifier, str)
                    self.assertNotIn("intelligence-index", ref.identifier.lower())

    def test_deterministic_json_round_trip(self) -> None:
        text = _load_catalog_text()
        canonical = json.dumps(_CATALOG.to_dict(), indent=2, ensure_ascii=False) + "\n"
        self.assertEqual(text, canonical)
        reparsed = ModelCatalog.from_dict(cast(object, json.loads(canonical)))
        self.assertEqual(reparsed, _CATALOG)
        self.assertEqual(
            [e.identity.to_dict() for e in reparsed.entries],
            [
                {"provider": p, "model": m, "variant": v}
                for p, m, v in sorted(ALL_MODELS)
            ],
        )


class TaskProfileCalibration(unittest.TestCase):
    def test_policy_version_incremented_and_calibrated(self) -> None:
        policy = _load_policy()
        self.assertEqual(policy["schema_version"], 1)
        self.assertEqual(policy["policy_version"], 4)
        self.assertEqual(policy["updated_at"], ASSESSED_ON)
        task_policy = _mapping(policy["task_profile_policy"], "task_profile_policy")
        self.assertTrue(task_policy["numeric_minima_included"])
        self.assertEqual(task_policy["numeric_minima_status"], "calibrated_m2c")

    def test_exactly_eight_formal_profiles_aligned_with_vocabulary(self) -> None:
        self.assertEqual(len(_PROFILES), 8)
        self.assertEqual(set(_PROFILES), set(FORMAL_PROFILE_IDS))

    def test_exact_task_requirement_expansion(self) -> None:
        expected_requirements = {
            "mechanical": TaskRequirement.from_dict(
                {
                    "task_level": "L0",
                    "capability_minima": {
                        "tool_use": 2,
                        "writing_editorial": 2,
                    },
                    "hard_constraints": {},
                }
            ),
            "routine_coding": TaskRequirement.from_dict(
                {
                    "task_level": "L1",
                    "capability_minima": {
                        "reasoning": 2,
                        "coding": 3,
                        "tool_use": 3,
                    },
                    "hard_constraints": {"requires_tool_use": True},
                }
            ),
            "deep_coding": TaskRequirement.from_dict(
                {
                    "task_level": "L3",
                    "capability_minima": {
                        "reasoning": 4,
                        "coding": 5,
                        "tool_use": 4,
                    },
                    "hard_constraints": {
                        "requires_tool_use": True,
                        "requires_reasoning_mode": True,
                    },
                }
            ),
            "scientific_review": TaskRequirement.from_dict(
                {
                    "task_level": "L4",
                    "capability_minima": {
                        "reasoning": 4,
                        "scientific_methodological": 5,
                        "writing_editorial": 4,
                    },
                    "hard_constraints": {"requires_reasoning_mode": True},
                }
            ),
            "editorial": TaskRequirement.from_dict(
                {
                    "task_level": "L2",
                    "capability_minima": {
                        "reasoning": 3,
                        "writing_editorial": 5,
                    },
                    "hard_constraints": {},
                }
            ),
            "general_reasoning": TaskRequirement.from_dict(
                {
                    "task_level": "L2",
                    "capability_minima": {
                        "reasoning": 4,
                        "writing_editorial": 3,
                    },
                    "hard_constraints": {},
                }
            ),
            "orchestration": TaskRequirement.from_dict(
                {
                    "task_level": "L3",
                    "capability_minima": {
                        "reasoning": 4,
                        "writing_editorial": 5,
                        "tool_use": 5,
                    },
                    "hard_constraints": {
                        "requires_tool_use": True,
                        "requires_reasoning_mode": True,
                    },
                }
            ),
            "translation": TaskRequirement.from_dict(
                {
                    "task_level": "L4",
                    "capability_minima": {
                        "writing_editorial": 4,
                        "translation_multilingual": 5,
                    },
                    "hard_constraints": {},
                }
            ),
        }
        self.assertEqual(set(expected_requirements), set(FORMAL_PROFILE_IDS))
        for profile_id, expected in expected_requirements.items():
            self.assertEqual(_PROFILE_CATALOG.resolve(profile_id), expected, profile_id)

    def test_capability_only_expected_eligible_sets(self) -> None:
        for profile_id in FORMAL_PROFILE_IDS:
            requirement = _PROFILE_CATALOG.resolve(profile_id)
            eligible = {
                identity
                for identity, entry in _BY_IDENTITY.items()
                if _capability_eligible(
                    requirement.capability_minima, entry.capabilities
                )
            }
            self.assertEqual(
                eligible, set(ACCEPTED_ELIGIBLE_SETS[profile_id]), profile_id
            )

    def test_no_model_provider_or_class_names_in_profile_requirements(self) -> None:
        forbidden_terms = (
            "gpt",
            "glm",
            "luna",
            "sol",
            "flash",
            "openai",
            "zai",
            "codex",
            "coding_plan",
            "execution_generalist",
            "orchestration_synthesis",
            "deep_technical_reasoner",
            "scientific_methodological_specialist",
            "translation_editorial_specialist",
        )
        for profile_id, entry in _PROFILES.items():
            serialized = json.dumps(entry["calibrated_requirement"]).lower()
            for term in forbidden_terms:
                self.assertNotIn(term, serialized, f"{profile_id}: {term}")

    def test_calibration_is_the_sole_numeric_definition(self) -> None:
        for profile_id, entry in _PROFILES.items():
            numeric_locations: set[str] = set()
            for key, value in entry.items():
                if key == "calibrated_requirement" or not isinstance(value, dict):
                    continue
                mapping = cast("dict[str, object]", value)
                if any(
                    isinstance(v, int) and not isinstance(v, bool)
                    for v in mapping.values()
                ):
                    numeric_locations.add(key)
            self.assertEqual(numeric_locations, set(), f"{profile_id}: {numeric_locations}")
            self.assertIn("calibrated_requirement", entry)
            self.assertIn("indicative_capability_needs", entry)
            self.assertIn("intent", entry)

    def test_unknown_profile_is_typed_failure(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = _PROFILE_CATALOG.resolve("nonexistent_profile")
        with self.assertRaises(SelectionContractValidationError):
            _ = _PROFILE_CATALOG.resolve("")

    def test_duplicate_profile_ids_are_rejected(self) -> None:
        definition = _PROFILE_CATALOG.definitions[0]
        with self.assertRaises(SelectionContractValidationError):
            _ = TaskProfileCatalog(definitions=(definition, definition))

    def test_profile_catalog_serialization_is_deterministic(self) -> None:
        serialized = json.dumps(_PROFILE_CATALOG.to_dict())
        reparsed = TaskProfileCatalog.from_dict(cast(object, json.loads(serialized)))
        self.assertEqual(reparsed, _PROFILE_CATALOG)
        ids = [d.profile_id for d in reparsed.definitions]
        self.assertEqual(ids, sorted(ids))

    def test_resolution_is_pure_stored_requirement(self) -> None:
        definition = _PROFILE_CATALOG.definitions[0]
        self.assertIs(definition.to_requirement(), definition.requirement)
        self.assertEqual(
            TaskProfileDefinition.from_dict(definition.to_dict()), definition
        )


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
