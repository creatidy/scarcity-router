"""Contract tests for the portable model policy artifact."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import cast

POLICY_PATH = Path(__file__).resolve().parents[1] / "model-policy.json"
SNAKE_CASE = re.compile(r"^[a-z]+(?:_[a-z0-9]+)*$")


def _load_policy() -> dict[str, object]:
    with POLICY_PATH.open(encoding="utf-8") as handle:
        value = cast(object, json.load(handle))
    if not isinstance(value, dict):
        raise AssertionError("model-policy.json must contain a JSON object")
    return cast(dict[str, object], value)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"{name} must be a JSON object")
    return cast(dict[str, object], value)


def _objects(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"{name} must be a JSON array")
    result: list[dict[str, object]] = []
    for item in cast("list[object]", value):
        result.append(_mapping(item, name))
    return result


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError(f"{name} must be a JSON array")
    result: list[str] = []
    for item in cast("list[object]", value):
        if not isinstance(item, str):
            raise AssertionError(f"{name} must contain strings")
        result.append(item)
    return result


def _required_string(entry: dict[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        raise AssertionError(f"{key} must be a string")
    return value


class ModelPolicyContract(unittest.TestCase):
    def test_json_versions_and_round_trip(self) -> None:
        text = POLICY_PATH.read_text(encoding="utf-8")
        policy = cast(object, json.loads(text))
        self.assertIsInstance(policy, dict)
        parsed = cast(dict[str, object], policy)
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["policy_version"], 2)
        self.assertEqual(parsed["updated_at"], "2026-09-05")
        self.assertEqual(json.loads(json.dumps(parsed)), parsed)

    def test_versioning_semantics_are_documented(self) -> None:
        versioning = _mapping(_load_policy()["versioning"], "versioning")
        schema = _mapping(versioning["schema_version"], "schema_version")
        policy = _mapping(versioning["policy_version"], "policy_version")
        self.assertIn("incompatibly", _required_string(schema, "change_policy"))
        self.assertIn("policy content", _required_string(policy, "change_policy"))

    def test_capability_dimensions_are_exact_and_unique(self) -> None:
        dimensions = _objects(
            _load_policy()["capability_dimensions"], "capability_dimensions"
        )
        ids = [_required_string(entry, "id") for entry in dimensions]
        self.assertEqual(
            set(ids),
            {
                "reasoning",
                "coding",
                "scientific_methodological",
                "writing_editorial",
                "tool_use",
                "translation_multilingual",
            },
        )
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("translation_multilingual", ids)
        self.assertNotIn("orchestration", ids)
        for entry in dimensions:
            self.assertRegex(_required_string(entry, "id"), SNAKE_CASE)

    def test_translation_and_orchestration_dimension_policy(self) -> None:
        dimension_policy = _mapping(
            _load_policy()["capability_dimension_policy"],
            "capability_dimension_policy",
        )
        translation = _mapping(
            dimension_policy["translation_multilingual"],
            "translation_multilingual",
        )
        self.assertEqual(translation["status"], "first_class_capability_dimension")
        self.assertEqual(
            translation["not_inferred_solely_from"], "writing_editorial"
        )
        orchestration = _mapping(
            dimension_policy["orchestration_dimension"],
            "orchestration_dimension",
        )
        self.assertEqual(orchestration["status"], "deferred")
        self.assertFalse(orchestration["dedicated_dimension_included"])
        current = _mapping(
            orchestration["current_representation"],
            "orchestration.current_representation",
        )
        self.assertEqual(
            _strings(current["capability_dimensions"], "current dimensions"),
            ["reasoning", "tool_use", "writing_editorial"],
        )

    def test_classes_have_stable_ids_labels_and_required_fields(self) -> None:
        expected_labels = {
            "execution_generalist": "EXECUTION_GENERALIST",
            "orchestration_synthesis": "ORCHESTRATION_SYNTHESIS",
            "deep_technical_reasoner": "DEEP_TECHNICAL_REASONER",
            "scientific_methodological_specialist": "SCIENTIFIC_METHODOLOGICAL_SPECIALIST",
            "translation_editorial_specialist": "TRANSLATION_EDITORIAL_SPECIALIST",
        }
        classes = _objects(
            _load_policy()["model_capability_classes"],
            "model_capability_classes",
        )
        ids = [_required_string(entry, "id") for entry in classes]
        self.assertEqual(set(ids), set(expected_labels))
        self.assertEqual(len(ids), len(set(ids)))
        for entry in classes:
            class_id = _required_string(entry, "id")
            self.assertRegex(class_id, SNAKE_CASE)
            self.assertEqual(_required_string(entry, "label"), expected_labels[class_id])
            for key in (
                "intent",
                "typical_strengths",
                "typical_task_profiles",
                "task_level_guidance",
                "usage_guidance",
            ):
                self.assertIn(key, entry)

    def test_class_invariants_and_selector_sequence_are_explicit(self) -> None:
        invariants = _mapping(
            _load_policy()["policy_invariants"], "policy_invariants"
        )
        self.assertTrue(invariants["classes_are_descriptive_metadata"])
        self.assertTrue(invariants["classes_are_non_exclusive"])
        self.assertFalse(invariants["classes_are_rankings"])
        self.assertFalse(invariants["class_membership_establishes_task_eligibility"])
        self.assertTrue(
            invariants["task_profiles_and_explicit_requirements_are_selector_facing"]
        )
        self.assertTrue(invariants["capability_minima_must_be_satisfied"])
        self.assertTrue(invariants["hard_constraints_must_be_satisfied"])
        self.assertTrue(invariants["runtime_capacity_is_independent"])
        self.assertTrue(invariants["scarcity_and_reservation_policy_is_independent"])
        self.assertEqual(
            invariants["principle"],
            "Choose the least scarce model that satisfies the actual task requirements.",
        )
        self.assertEqual(
            _strings(invariants["selector_sequence"], "selector_sequence"),
            [
                "resolve_task_requirements",
                "filter_hard_constraints",
                "require_every_capability_minimum",
                "apply_runtime_capacity_eligibility",
                "apply_scarcity_and_reservation_policy",
                "rank_sufficient_eligible_candidates",
                "explain_the_decision",
            ],
        )
        shortcuts = _strings(invariants["forbidden_shortcuts"], "forbidden_shortcuts")
        self.assertIn(
            'if task == "deep_coding": choose_a_deep_technical_reasoner()',
            shortcuts,
        )

    def test_multi_agent_orchestration_policy_is_bounded(self) -> None:
        orchestration = _mapping(
            _load_policy()["multi_agent_orchestration_policy"],
            "multi_agent_orchestration_policy",
        )
        self.assertTrue(orchestration["bounded_by_default"])
        self.assertFalse(orchestration["implicit_unlimited_mode"])
        self.assertTrue(orchestration["human_escalation_on_budget_exhaustion"])
        self.assertTrue(orchestration["review_head_must_be_frozen"])
        self.assertTrue(orchestration["dependent_steps_are_serialized"])
        self.assertTrue(orchestration["final_verification_is_scoped"])
        self.assertEqual(
            _mapping(orchestration["default_budgets"], "default_budgets"),
            {
                "max_initial_review_rounds": 1,
                "max_remediation_rounds": 1,
                "max_final_verification_rounds": 1,
                "max_worker_retries": 1,
                "max_reviewer_retries": 1,
                "max_wall_clock_minutes": 120,
            },
        )
        self.assertEqual(
            _strings(orchestration["finding_classes"], "finding_classes"),
            ["merge_blocker", "defer"],
        )
        self.assertEqual(
            orchestration["stop_outcome"], "stop_and_escalate_to_human"
        )
        review_head = _mapping(
            orchestration["review_head_policy"], "review_head_policy"
        )
        self.assertFalse(review_head["worker_may_push_during_review"])
        self.assertTrue(review_head["branch_change_invalidates_review"])
        self.assertTrue(review_head["dependent_phases_must_not_run_concurrently"])
        self.assertEqual(
            _strings(
                orchestration["final_verification_scope"],
                "final_verification_scope",
            ),
            [
                "verify_previous_merge_blockers",
                "check_obvious_remediation_regressions",
            ],
        )
        self.assertEqual(
            _mapping(
                orchestration["final_verification_outcomes"],
                "final_verification_outcomes",
            ),
            {
                "ready_to_merge": "stop",
                "remaining_merge_blocker": "stop_and_escalate_to_human",
            },
        )

    def test_orchestration_class_requires_bounded_workflow_governance(self) -> None:
        orchestration_class = next(
            entry
            for entry in _objects(
                _load_policy()["model_capability_classes"],
                "model_capability_classes",
            )
            if entry["id"] == "orchestration_synthesis"
        )
        governance = _mapping(
            orchestration_class["orchestration_governance"],
            "orchestration_governance",
        )
        self.assertEqual(
            set(_strings(governance["requires"], "orchestration requirements")),
            {
                "explicit_execution_budget",
                "explicit_stop_conditions",
                "worker_reviewer_serialization",
                "bounded_independent_review",
                "human_escalation_when_budget_exhausted",
            },
        )
        self.assertTrue(governance["independence_does_not_expand_scope"])

    def test_classes_record_formal_profiles_and_workflow_roles(self) -> None:
        expected_profiles = {
            "execution_generalist": {
                "mechanical",
                "routine_coding",
                "general_reasoning",
            },
            "orchestration_synthesis": {
                "orchestration",
                "editorial",
                "general_reasoning",
            },
            "deep_technical_reasoner": {"deep_coding", "general_reasoning"},
            "scientific_methodological_specialist": {"scientific_review"},
            "translation_editorial_specialist": {"translation"},
        }
        expected_roles = {
            "execution_generalist": {
                "evidence_preparation_without_specialist_adjudication"
            },
            "orchestration_synthesis": {"complex_synthesis"},
            "deep_technical_reasoner": {
                "advanced_architecture",
                "advanced_debugging",
            },
            "scientific_methodological_specialist": {
                "methodological_review",
                "evidence_adjudication",
                "expert_consultation",
            },
            "translation_editorial_specialist": {
                "translation_review",
                "bilingual_editorial_sync",
            },
        }
        for entry in _objects(
            _load_policy()["model_capability_classes"], "model_capability_classes"
        ):
            class_id = _required_string(entry, "id")
            self.assertEqual(
                set(_strings(entry["typical_task_profiles"], "typical_task_profiles")),
                expected_profiles[class_id],
            )
            self.assertEqual(
                set(_strings(entry["typical_workflow_roles"], "typical_workflow_roles")),
                expected_roles[class_id],
            )

    def test_all_cross_section_references_preserve_profile_boundaries(self) -> None:
        policy = _load_policy()
        classes = _objects(policy["model_capability_classes"], "model_capability_classes")
        class_ids = {
            _required_string(entry, "id")
            for entry in classes
        }
        profiles = _objects(policy["task_profiles"], "task_profiles")
        profile_ids = {
            _required_string(entry, "id")
            for entry in profiles
        }

        for entry in classes:
            class_id = _required_string(entry, "id")
            task_profiles = _strings(
                entry["typical_task_profiles"], "typical_task_profiles"
            )
            self.assertTrue(
                set(task_profiles).issubset(profile_ids),
                class_id,
            )
            workflow_roles = _strings(
                entry["typical_workflow_roles"], "typical_workflow_roles"
            )
            self.assertTrue(
                set(workflow_roles).isdisjoint(profile_ids),
                class_id,
            )

        for profile in profiles:
            for affinity in _objects(profile["class_affinities"], "class_affinities"):
                self.assertIn(_required_string(affinity, "class_id"), class_ids)

        workflow = _mapping(
            policy["current_workflow_examples"], "current_workflow_examples"
        )
        for model in _objects(workflow["models"], "workflow models"):
            for field in ("primary_archetypes", "secondary_archetypes"):
                archetypes = _strings(model[field], field)
                self.assertTrue(set(archetypes).issubset(class_ids))

        relationship = _mapping(
            policy["task_level_relationship"], "task_level_relationship"
        )
        for example in _objects(relationship["examples"], "task level examples"):
            for guidance in _objects(example["class_guidance"], "class_guidance"):
                self.assertIn(_required_string(guidance, "class_id"), class_ids)

    def test_task_profiles_include_new_profiles_and_non_numeric_expansion(self) -> None:
        policy = _load_policy()
        profile_policy = _mapping(
            policy["task_profile_policy"], "task_profile_policy"
        )
        self.assertTrue(profile_policy["profiles_are_selector_facing"])
        self.assertTrue(profile_policy["class_affinities_are_hints_not_requirements"])
        self.assertFalse(profile_policy["numeric_minima_included"])
        self.assertEqual(
            profile_policy["numeric_minima_status"], "deferred_to_m2_calibration"
        )
        profiles = _objects(policy["task_profiles"], "task_profiles")
        ids = [_required_string(entry, "id") for entry in profiles]
        self.assertEqual(
            set(ids),
            {
                "mechanical",
                "routine_coding",
                "deep_coding",
                "scientific_review",
                "editorial",
                "general_reasoning",
                "orchestration",
                "translation",
            },
        )
        self.assertEqual(len(ids), len(set(ids)))
        for entry in profiles:
            self.assertTrue(entry["selector_facing"])
            self.assertIn("intent", entry)
            self.assertIn("indicative_capability_needs", entry)
            self.assertIn("hard_requirement_categories", entry)

        by_id = {
            _required_string(entry, "id"): entry for entry in profiles
        }
        orchestration_guidance = _mapping(
            by_id["orchestration"]["indicative_strength_guidance"],
            "orchestration.indicative_strength_guidance",
        )
        self.assertEqual(
            orchestration_guidance,
            {
                "reasoning": "strong",
                "tool_use": "strong",
                "writing_editorial": "useful",
            },
        )
        translation_guidance = _mapping(
            by_id["translation"]["indicative_strength_guidance"],
            "translation.indicative_strength_guidance",
        )
        self.assertEqual(
            translation_guidance,
            {
                "translation_multilingual": "strong",
                "writing_editorial": "strong",
                "reasoning": "sufficient for contextual ambiguity",
            },
        )

    def test_profile_affinities_are_likely_hints_and_resolve(self) -> None:
        expected = {
            "mechanical": {("execution_generalist", "likely")},
            "routine_coding": {("execution_generalist", "likely")},
            "deep_coding": {
                ("deep_technical_reasoner", "likely"),
                ("execution_generalist", "alternative"),
            },
            "scientific_review": {
                ("scientific_methodological_specialist", "likely")
            },
            "editorial": {("orchestration_synthesis", "likely")},
            "general_reasoning": {("execution_generalist", "likely")},
            "orchestration": {("orchestration_synthesis", "likely")},
            "translation": {("translation_editorial_specialist", "likely")},
        }
        classes = {
            _required_string(entry, "id")
            for entry in _objects(
                _load_policy()["model_capability_classes"],
                "model_capability_classes",
            )
        }
        for entry in _objects(_load_policy()["task_profiles"], "task_profiles"):
            profile_id = _required_string(entry, "id")
            affinities = _objects(entry["class_affinities"], "class_affinities")
            actual = {
                (
                    _required_string(affinity, "class_id"),
                    _required_string(affinity, "relationship"),
                )
                for affinity in affinities
            }
            self.assertEqual(actual, expected[profile_id])
            self.assertTrue(actual)
            for class_id, relationship in actual:
                self.assertIn(class_id, classes)
                self.assertIn(relationship, {"likely", "alternative"})
        deep = next(
            entry
            for entry in _objects(_load_policy()["task_profiles"], "task_profiles")
            if entry["id"] == "deep_coding"
        )
        deep_affinities = _objects(deep["class_affinities"], "deep affinities")
        alternative = next(
            affinity
            for affinity in deep_affinities
            if affinity["relationship"] == "alternative"
        )
        self.assertIn(
            "actual capability vector",
            _required_string(alternative, "condition"),
        )

    def test_task_level_examples_resolve_and_are_explanatory(self) -> None:
        policy = _load_policy()
        relationship = _mapping(
            policy["task_level_relationship"], "task_level_relationship"
        )
        self.assertEqual(relationship["task_level_describes"], "difficulty_and_consequence")
        self.assertEqual(
            relationship["model_class_describes"],
            "the_kind_of_capability_a_model_is_especially_suited_to_provide",
        )
        self.assertTrue(relationship["examples_are_explanatory_defaults"])
        self.assertTrue(relationship["examples_are_not_selector_shortcuts"])
        examples = _objects(relationship["examples"], "task level examples")
        by_task = {
            _required_string(example, "task"): example for example in examples
        }
        expected = {
            "routine coding": ("L1", {"execution_generalist"}),
            "difficult debugging": (
                "L3",
                {"execution_generalist", "deep_technical_reasoner"},
            ),
            "restructuring a long technical document": (
                "L3",
                {"orchestration_synthesis"},
            ),
            "scientific evidence adjudication": (
                "L4",
                {"scientific_methodological_specialist"},
            ),
            "publication-quality technical translation": (
                "L3",
                {"translation_editorial_specialist"},
            ),
        }
        class_ids = {
            _required_string(entry, "id")
            for entry in _objects(policy["model_capability_classes"], "classes")
        }
        for task, (level, expected_classes) in expected.items():
            example = by_task[task]
            self.assertEqual(example["task_level"], level)
            guidance = _objects(example["class_guidance"], "class_guidance")
            actual_classes = {
                _required_string(item, "class_id") for item in guidance
            }
            self.assertEqual(actual_classes, expected_classes)
            self.assertTrue(actual_classes.issubset(class_ids))

    def test_current_workflow_examples_are_dated_and_resolve(self) -> None:
        policy = _load_policy()
        workflow = _mapping(
            policy["current_workflow_examples"], "current_workflow_examples"
        )
        self.assertEqual(workflow["assessment_date"], "2026-09-03")
        self.assertTrue(workflow["dated_assessment"])
        self.assertFalse(workflow["part_of_class_definitions"])
        self.assertFalse(workflow["numeric_capability_ratings_included"])
        class_ids = {
            _required_string(entry, "id")
            for entry in _objects(policy["model_capability_classes"], "classes")
        }
        models = _objects(workflow["models"], "workflow models")
        self.assertEqual(
            {
                _required_string(entry, "model_name") for entry in models
            },
            {
                "GLM-5.3-Flash Max",
                "GPT-5.6 Luna Max",
                "GLM-5.3 Max",
                "GPT-5.6 Sol High",
            },
        )
        self.assertFalse(any("Qwen" in _required_string(entry, "model_name") for entry in models))
        for entry in models:
            primary = _strings(entry["primary_archetypes"], "primary_archetypes")
            secondary = _strings(entry["secondary_archetypes"], "secondary_archetypes")
            self.assertTrue(set(primary).issubset(class_ids))
            self.assertTrue(set(secondary).issubset(class_ids))

    def test_exact_current_workflow_mapping(self) -> None:
        workflow = _mapping(
            _load_policy()["current_workflow_examples"], "current_workflow_examples"
        )
        models = {
            _required_string(entry, "model_name"): entry
            for entry in _objects(workflow["models"], "workflow models")
        }
        self.assertEqual(
            _strings(models["GLM-5.3-Flash Max"]["primary_archetypes"], "primary"),
            ["execution_generalist"],
        )
        self.assertEqual(
            _strings(models["GPT-5.6 Luna Max"]["primary_archetypes"], "primary"),
            ["orchestration_synthesis"],
        )
        self.assertEqual(
            _strings(models["GPT-5.6 Luna Max"]["secondary_archetypes"], "secondary"),
            ["execution_generalist"],
        )
        self.assertEqual(
            _strings(models["GLM-5.3 Max"]["primary_archetypes"], "primary"),
            ["deep_technical_reasoner"],
        )
        self.assertEqual(
            _strings(models["GLM-5.3 Max"]["secondary_archetypes"], "secondary"),
            ["execution_generalist"],
        )
        self.assertEqual(
            _strings(models["GPT-5.6 Sol High"]["primary_archetypes"], "primary"),
            [
                "scientific_methodological_specialist",
                "translation_editorial_specialist",
            ],
        )
        self.assertEqual(
            _strings(models["GPT-5.6 Sol High"]["secondary_archetypes"], "secondary"),
            ["orchestration_synthesis"],
        )


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
