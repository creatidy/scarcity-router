"""ModelInventory contract tests (D-053, issue #117).

The inventory document is the ONLY carrier of runtime discovery results
across the worker protocol: typed, bounded, credential-free, fail-closed.
These tests pin the exact serialization shape and every rejection the
contract promises (bounds, closed vocabularies, unsafe identifiers,
unknown keys, malformed JSON, duplicate keys).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scarcity_router.model_inventory import (  # noqa: E402
    MAX_MODELS_PER_SOURCE,
    MAX_SOURCES_PER_REPORT,
    MODEL_INVENTORY_SCHEMA_VERSION,
    ModelInventoryError,
    ModelInventoryReport,
    DiscoveredModel,
    SourceInventory,
)

OBSERVED = "2026-09-24T12:00:00.000Z"


def _model(slug: str = "gpt-6-sol", efforts: tuple[str, ...] = ("low", "high")) -> DiscoveredModel:
    return DiscoveredModel(slug, efforts, "GPT-6 Sol")


def _source(**overrides: object) -> SourceInventory:
    kwargs: dict[str, object] = {
        "source_id": "personal-openai",
        "adapter_id": "codex:personal-openai",
        "kind": "codex_subscription",
        "observed_at": OBSERVED,
        "auth_state": "authenticated",
        "runtime_name": "codex",
        "runtime_version": "0.155.0-alpha.16.3",
        "models": (_model(),),
    }
    kwargs.update(overrides)
    # Keys above are fixed literals; dict[str, object] is just the merge vehicle.
    return SourceInventory(**kwargs)  # pyright: ignore[reportArgumentType] - fixed-literal test helper


def _report(**overrides: object) -> ModelInventoryReport:
    kwargs: dict[str, object] = {
        "worker_id": "worker-1",
        "sources": (_source(),),
    }
    kwargs.update(overrides)
    # Keys above are fixed literals; dict[str, object] is just the merge vehicle.
    return ModelInventoryReport(**kwargs)  # pyright: ignore[reportArgumentType] - fixed-literal test helper


class DiscoveredModelTests(unittest.TestCase):
    def test_serialization_shape_is_exactly_the_contract(self) -> None:
        self.assertEqual(
            {
                "slug": "gpt-6-sol",
                "reasoning_efforts": ["low", "high"],
                "display_name": "GPT-6 Sol",
            },
            _model().to_dict(),
        )
        # display_name stays absent when empty (no raw provider payload).
        self.assertEqual(
            {"slug": "m", "reasoning_efforts": ["low"]},
            DiscoveredModel("m", ("low",)).to_dict(),
        )

    def test_runtime_effort_names_are_not_forced_into_a_static_vocabulary(
        self,
    ) -> None:
        # The provider may introduce new effort names without a router
        # release: any safe identifier is a legal runtime effort name.
        model = _model(efforts=("minimal", "xhigh", "ultra", "some-future-effort"))
        self.assertEqual(
            ("minimal", "xhigh", "ultra", "some-future-effort"),
            model.reasoning_efforts,
        )

    def test_rejects_unsafe_slug_and_duplicate_efforts(self) -> None:
        with self.assertRaises(ModelInventoryError):
            _ = _model(slug="NOT A SLUG")
        with self.assertRaises(ModelInventoryError):
            _ = _model(efforts=("low", "low"))
        with self.assertRaises(ModelInventoryError):
            _ = _model(efforts=("low",) * 9)  # beyond MAX_EFFORTS_PER_MODEL


class SourceInventoryTests(unittest.TestCase):
    def test_roundtrip_preserves_the_exact_document(self) -> None:
        document = _source().to_dict()
        self.assertEqual(
            document,
            SourceInventory.from_dict(document).to_dict(),
        )

    def test_closed_vocabulary_rejections(self) -> None:
        with self.assertRaises(ModelInventoryError):
            _ = _source(kind="generic_discovery")
        with self.assertRaises(ModelInventoryError):
            _ = _source(auth_state="probably_fine")
        with self.assertRaises(ModelInventoryError):
            _ = _source(observed_at="2026-09-24T12:00:00Z")  # missing millis
        # The codex:<source_id> adapter-instance naming convention is
        # enforced where instances are built (worker side), not in the
        # generic contract; the report-level uniqueness guarantee is here.
        self.assertEqual("codex:personal-openai", _source().adapter_id)

    def test_model_bounds_and_duplicates_fail_closed(self) -> None:
        with self.assertRaises(ModelInventoryError):
            _ = _source(models=tuple(_model(f"m{i}") for i in range(MAX_MODELS_PER_SOURCE + 1)))
        with self.assertRaises(ModelInventoryError):
            _ = _source(models=(_model("same"), _model("same")))
        # Models are canonically ordered for stable audit.
        ordered = SourceInventory(
            source_id="s",
            adapter_id="codex:s",
            kind="codex_subscription",
            observed_at=OBSERVED,
            auth_state="authenticated",
            runtime_name="codex",
            runtime_version="1",
            models=(_model("z-slug"), _model("a-slug")),
        )
        self.assertEqual(("a-slug", "z-slug"), tuple(m.slug for m in ordered.models))


class ReportTests(unittest.TestCase):
    def test_roundtrip_and_exact_shape(self) -> None:
        report = _report()
        self.assertEqual(MODEL_INVENTORY_SCHEMA_VERSION, report.schema_version)
        self.assertEqual(report.to_dict(), ModelInventoryReport.from_dict(report.to_dict()).to_dict())

    def test_future_schema_version_fails_closed(self) -> None:
        document = _report().to_dict()
        document["schema_version"] = MODEL_INVENTORY_SCHEMA_VERSION + 1
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport.from_dict(document)

    def test_unknown_keys_are_rejected_never_ignored(self) -> None:
        document = _report().to_dict()
        document["raw_provider_payload"] = {"anything": "no"}
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport.from_dict(document)
        source_document = _source().to_dict()
        source_document["account_email"] = "leak@example.invalid"
        with self.assertRaises(ModelInventoryError):
            _ = SourceInventory.from_dict(source_document)

    def test_bounded_source_count_and_duplicates(self) -> None:
        with self.assertRaises(ModelInventoryError):
            _ = _report(
                sources=tuple(
                    _source(source_id=f"s{i}", adapter_id=f"codex:s{i}")
                    for i in range(MAX_SOURCES_PER_REPORT + 1)
                )
            )
        with self.assertRaises(ModelInventoryError):
            _ = _report(sources=(_source(), _source()))
        # Two sources may not share one adapter instance id either.
        with self.assertRaises(ModelInventoryError):
            _ = _report(
                sources=(
                    _source(source_id="a"),
                    _source(source_id="b", adapter_id="codex:personal-openai"),
                )
            )

    def test_parse_from_strict_json_rejects_duplicate_keys(self) -> None:
        import json

        document = json.dumps(_report().to_dict())
        self.assertEqual(
            MODEL_INVENTORY_SCHEMA_VERSION,
            ModelInventoryReport.parse(document).schema_version,
        )
        duplicated = document[:-1] + ', "worker_id": "x"}'
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport.parse(duplicated)
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport.parse("not json at all")


class TrackRegistryArtifactTests(unittest.TestCase):
    """The reviewed registry artifact loads and classifies as reviewed."""

    def test_repository_artifact_loads(self) -> None:
        from scarcity_router.model_tracks import load_track_registry

        registry = load_track_registry()
        track_ids = {track.track_id() for track in registry.tracks}
        self.assertIn("openai/sol", track_ids)
        self.assertIn("openai/luna", track_ids)
        self.assertIn("openai/astra", track_ids)
        self.assertIn("openai/daybreak", track_ids)

    def test_live_evidence_slugs_classify_into_tracks(self) -> None:
        from scarcity_router.model_tracks import load_track_registry

        registry = load_track_registry()
        sol = registry.classify("openai", "gpt-6-sol")
        assert sol is not None
        self.assertEqual("openai/sol", sol.track_id())
        # A future generation classifies into the SAME track (the whole point).
        nxt = registry.classify("openai", "gpt-6.1-sol")
        assert nxt is not None
        self.assertEqual("openai/sol", nxt.track_id())
        luna = registry.classify("openai", "gpt-6-luna")
        assert luna is not None
        self.assertEqual("openai/luna", luna.track_id())
        astra = registry.classify("openai", "gpt-6-astra")
        assert astra is not None
        self.assertEqual("openai/astra", astra.track_id())

    def test_unlisted_and_unknown_models_stay_unclassified(self) -> None:
        from scarcity_router.model_tracks import load_track_registry

        registry = load_track_registry()
        self.assertIsNone(registry.classify("openai", "gpt-5.5"))
        self.assertIsNone(registry.classify("openai", "some-new-unclassified-model"))
        self.assertIsNone(registry.classify("zai", "gpt-6-sol"))

    def test_daybreak_is_restricted_never_standard(self) -> None:
        from scarcity_router.model_tracks import load_track_registry

        daybreak = load_track_registry().classify("openai", "gpt-daybreak-blue-latest")
        assert daybreak is not None
        self.assertEqual("restricted", daybreak.classification)
        self.assertIsNone(daybreak.floor)

    def test_exact_generation_slug_still_classifies_with_5x_generation(self) -> None:
        # Tracks span generations: the existing gpt-5.6 slugs belong to the
        # same families (their EXACT catalog ratings take precedence over
        # floors elsewhere; classification here stays structural).
        from scarcity_router.model_tracks import load_track_registry

        registry = load_track_registry()
        legacy = registry.classify("openai", "gpt-5.6-sol")
        assert legacy is not None
        self.assertEqual("openai/sol", legacy.track_id())

    def test_ambiguous_pattern_match_fails_closed(self) -> None:
        from scarcity_router.model_tracks import (
            ModelTrack,
            TrackFloor,
            TrackRegistry,
            TrackRegistryError,
        )

        floor = TrackFloor(
            ratings={dim: 3 for dim in (
                "reasoning",
                "coding",
                "scientific_methodological",
                "writing_editorial",
                "tool_use",
                "translation_multilingual",
            )},
            assessed_on="2026-09-24",
            confidence="low",
            decision="test",
            rationale="test floor",
        )
        overlapping = TrackRegistry(
            (
                ModelTrack("p", "a", "A", "^gpt-a(-x)?$", "standard", floor),
                ModelTrack("p", "b", "B", "^gpt-a-x$", "standard", floor),
            )
        )
        with self.assertRaises(TrackRegistryError):
            _ = overlapping.classify("p", "gpt-a-x")


if __name__ == "__main__":
    _ = unittest.main()
