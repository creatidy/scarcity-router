"""Parser tests for the Ollama local runtime adapter.

Every fixture under ``tests/fixtures/ollama-local/`` participates here. The
parsers under test are pure: they receive already decoded payloads and
perform no clock, filesystem, environment or network access.

All tests are deterministic and self-contained; the only file access is
reading the synthetic fixture inputs. These tests cover the local Ollama
contract only and deliberately do not duplicate the OpenAI/Codex or Z.ai
parser tests.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scarcity_router.providers.ollama import (
    parse_ollama_ps_response,
    parse_ollama_tags_response,
    parse_ollama_version_response,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ollama-local"


def _load(name: str) -> object:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return cast("object", json.load(handle))


class VersionProbe(unittest.TestCase):
    def test_ok_fixture_validates(self) -> None:
        self.assertTrue(parse_ollama_version_response(_load("version-ok.json")))

    def test_plain_envelope_validates(self) -> None:
        self.assertTrue(parse_ollama_version_response({"version": "0.0.0"}))

    def test_additive_keys_are_tolerated(self) -> None:
        payload: dict[str, object] = {
            "version": "0.0.0",
            "future_field": {"nested": [1, 2, 3]},
        }
        self.assertTrue(parse_ollama_version_response(payload))

    def test_malformed_fixture_is_drift(self) -> None:
        self.assertFalse(parse_ollama_version_response(_load("version-malformed.json")))

    def test_missing_version_key_is_drift(self) -> None:
        self.assertFalse(parse_ollama_version_response({"other": 1}))

    def test_non_string_version_is_drift(self) -> None:
        for bad in (1, None, True, ["0.0.0"], {"v": 1}):
            with self.subTest(bad=bad):
                self.assertFalse(parse_ollama_version_response({"version": bad}))

    def test_non_object_payload_is_drift(self) -> None:
        for bad in (None, "0.0.0", 1, [1, 2], True):
            with self.subTest(bad=bad):
                self.assertFalse(parse_ollama_version_response(bad))


class TagsListing(unittest.TestCase):
    def test_present_fixture_lists_names_in_order(self) -> None:
        listed = parse_ollama_tags_response(_load("tags-present.json"))
        self.assertEqual(
            listed, ("test-model:latest", "other-model:1b")
        )

    def test_missing_fixture_still_validates(self) -> None:
        listed = parse_ollama_tags_response(_load("tags-missing.json"))
        self.assertEqual(listed, ("other-model:1b",))

    def test_duplicate_names_are_drift(self) -> None:
        self.assertIsNone(parse_ollama_tags_response(_load("tags-duplicate-names.json")))

    def test_malformed_entries_are_drift(self) -> None:
        self.assertIsNone(parse_ollama_tags_response(_load("tags-malformed-entries.json")))

    def test_schema_changed_fixture_is_drift(self) -> None:
        self.assertIsNone(parse_ollama_tags_response(_load("tags-schema-changed.json")))

    def test_envelope_without_models_is_drift(self) -> None:
        self.assertIsNone(parse_ollama_tags_response({"items": []}))

    def test_models_not_a_list_is_drift(self) -> None:
        self.assertIsNone(parse_ollama_tags_response({"models": "test-model:latest"}))

    def test_empty_models_is_a_valid_empty_listing(self) -> None:
        self.assertEqual(parse_ollama_tags_response({"models": []}), ())

    def test_entry_name_must_be_non_empty_string(self) -> None:
        for bad in ("", None, 7, True, ["a"]):
            with self.subTest(bad=bad):
                payload = {"models": [{"name": bad}]}
                self.assertIsNone(parse_ollama_tags_response(payload))

    def test_additive_entry_keys_are_tolerated(self) -> None:
        payload = {"models": [{"name": "test-model:latest", "future": {"x": 1}}]}
        self.assertEqual(
            parse_ollama_tags_response(payload), ("test-model:latest",)
        )

    def test_details_context_length_is_never_read_as_context_fact(self) -> None:
        # tags details.context_length is model-file metadata; the tags
        # parser returns only names, so it can never leak into a context
        # field.
        listed = parse_ollama_tags_response(_load("tags-present.json"))
        self.assertEqual(listed, ("test-model:latest", "other-model:1b"))

    def test_normalization_is_deterministic(self) -> None:
        payload = _load("tags-present.json")
        self.assertEqual(
            parse_ollama_tags_response(payload),
            parse_ollama_tags_response(_load("tags-present.json")),
        )


class PsListing(unittest.TestCase):
    def test_loaded_fixture_yields_validated_context(self) -> None:
        loaded = parse_ollama_ps_response(_load("ps-loaded.json"))
        self.assertEqual(loaded, {"test-model:latest": 16384})

    def test_not_loaded_is_a_valid_empty_result(self) -> None:
        self.assertEqual(parse_ollama_ps_response(_load("ps-not-loaded.json")), {})

    def test_other_loaded_model_is_reported(self) -> None:
        loaded = parse_ollama_ps_response(_load("ps-other-loaded.json"))
        self.assertEqual(loaded, {"other-model:1b": 32768})

    def test_missing_context_length_is_drift(self) -> None:
        self.assertIsNone(
            parse_ollama_ps_response(_load("ps-missing-context-length.json"))
        )

    def test_schema_changed_fixture_is_drift(self) -> None:
        self.assertIsNone(parse_ollama_ps_response(_load("ps-schema-changed.json")))

    def test_context_length_must_be_positive_integer(self) -> None:
        for bad in (0, -1, "16384", 16.5, True, None):
            with self.subTest(bad=bad):
                payload = {
                    "models": [{"name": "test-model:latest", "context_length": bad}]
                }
                self.assertIsNone(parse_ollama_ps_response(payload))

    def test_duplicate_loaded_names_are_drift(self) -> None:
        entry: dict[str, object] = {
            "name": "test-model:latest",
            "context_length": 16384,
        }
        payload = {"models": [dict(entry), dict(entry)]}
        self.assertIsNone(parse_ollama_ps_response(payload))

    def test_normalization_is_deterministic(self) -> None:
        payload = _load("ps-loaded.json")
        self.assertEqual(
            parse_ollama_ps_response(payload),
            parse_ollama_ps_response(_load("ps-loaded.json")),
        )
