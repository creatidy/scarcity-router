"""ModelInventory contract tests (D-053, issue #117).

The inventory document is the ONLY carrier of runtime discovery results
across the worker protocol: typed, bounded, credential-free, fail-closed.
These tests pin the exact serialization shape and every rejection the
contract promises (bounds, closed vocabularies, unsafe identifiers,
unknown keys, malformed JSON, duplicate keys).
"""

from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path
from typing import cast

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
from scarcity_router.model_tracks import TrackFloor  # noqa: E402

OBSERVED = "2026-09-24T12:00:00.000Z"


class VersionMatrixTests(unittest.TestCase):
    def test_documented_family_versions_match_the_real_producers(self) -> None:
        from scarcity_router import get_version
        from scarcity_router.capacity import SCHEMA_VERSION
        from scarcity_router.eligibility import ELIGIBILITY_SCHEMA_VERSION
        from scarcity_router.gateway_audit import AUDIT_SCHEMA_VERSION
        from scarcity_router.gateway_contracts import EXECUTION_SURFACE_VERSION
        from scarcity_router.machine_api import ENVELOPE_SCHEMA_VERSION
        from scarcity_router.model_tracks import TRACK_REGISTRY_SCHEMA_VERSION
        from scarcity_router.resource_state import REGISTRY_SCHEMA_VERSION, RESOURCE_STATE_SCHEMA_VERSION, WORKER_REPORT_SCHEMA_VERSION
        from scarcity_router.routing_core import ROUTE_DECISION_SCHEMA_VERSION
        from scarcity_router.selection_app import MODEL_POLICY_SCHEMA_VERSION, load_catalog, load_model_policy
        from scarcity_router.server_config import CONFIG_SCHEMA_VERSION
        from scarcity_router.server_store import STORE_SCHEMA_VERSION
        from scarcity_router.worker_identity_store import WORKER_IDENTITY_STORE_SCHEMA_VERSION
        from scarcity_router.worker_local_store import WORKER_LOCAL_STORE_SCHEMA_VERSION
        from scarcity_router.worker_protocol import WORKER_PROTOCOL_VERSION
        from scarcity_router.worker_setup import WORKER_LOCAL_SETTINGS_SCHEMA_VERSION

        _profiles, policy_version = load_model_policy(REPO / "model-policy.json")
        versions = {
            "CapacitySnapshot.schema_version": SCHEMA_VERSION,
            "ExecutionEligibility.schema_version": ELIGIBILITY_SCHEMA_VERSION,
            "Machine envelope": ENVELOPE_SCHEMA_VERSION,
            "catalog_version": load_catalog(REPO / "model-catalog.json").catalog_version,
            "policy_version": policy_version,
            "Model policy schema": MODEL_POLICY_SCHEMA_VERSION,
            "Track registry schema": TRACK_REGISTRY_SCHEMA_VERSION,
            "Execution surface": EXECUTION_SURFACE_VERSION,
            "RouteDecision.schema_version": ROUTE_DECISION_SCHEMA_VERSION,
            "ResourceStateSnapshot.schema_version": RESOURCE_STATE_SCHEMA_VERSION,
            "RegistrySnapshot.schema_version": REGISTRY_SCHEMA_VERSION,
            "WorkerStateReport.schema_version": WORKER_REPORT_SCHEMA_VERSION,
            "ModelInventoryReport.schema_version": MODEL_INVENTORY_SCHEMA_VERSION,
            "Worker protocol": WORKER_PROTOCOL_VERSION,
            "Server configuration schema": CONFIG_SCHEMA_VERSION,
            "Server store schema": STORE_SCHEMA_VERSION,
            "Worker identity store schema": WORKER_IDENTITY_STORE_SCHEMA_VERSION,
            "Worker local store schema": WORKER_LOCAL_STORE_SCHEMA_VERSION,
            "Worker local settings schema": WORKER_LOCAL_SETTINGS_SCHEMA_VERSION,
            "AuditRecord.schema_version": AUDIT_SCHEMA_VERSION,
            "Package release": get_version(),
        }
        document = (REPO / "docs/machine-interfaces.md").read_text(encoding="utf-8")
        for name, version in versions.items():
            with self.subTest(family=name):
                self.assertIn(f"| `{name}` | `{version}` |", document)

    def test_catalog_content_version_does_not_migrate_or_recalibrate_entries(self) -> None:
        from scarcity_router.selection_app import load_catalog
        from scarcity_router.selection_types import ModelCatalog

        catalog = load_catalog(REPO / "model-catalog.json")
        document = catalog.to_dict()
        document["catalog_version"] = catalog.catalog_version + 1
        projected = ModelCatalog.from_dict(document)
        self.assertEqual(catalog.entries, projected.entries)
        self.assertEqual(document, projected.to_dict())

    def test_track_shape_refuses_future_aliases_and_missing_semantics(self) -> None:
        from scarcity_router.model_tracks import TrackRegistry

        document = cast("dict[str, object]", json.loads((REPO / "model-tracks.json").read_text(encoding="utf-8")))
        for version in (True, 1.0, "1", None, 2):
            with self.subTest(version=version):
                document["schema_version"] = version
                with self.assertRaises(ValueError):
                    _ = TrackRegistry.from_dict(document)
        document["schema_version"] = 1
        del document["tracks"]
        with self.assertRaises(ValueError):
            _ = TrackRegistry.from_dict(document)

    def test_public_evidence_frames_are_not_implicitly_calibrated_catalogs(self) -> None:
        from scarcity_router.errors import SelectionContractValidationError
        from scarcity_router.selection_app import load_catalog
        from scarcity_router.selection_types import ModelCatalog

        # Owned synthetic framing follows the inspected MI publication producer,
        # not its data or implementation. Router has no admitted payload mapping.
        bootstrap = load_catalog(REPO / "model-catalog.json")
        for schema in ("mi.public-evidence-working/1", "mi.public-evidence-working/2"):
            frame: dict[str, object] = {
                "format_version": 1,
                "payload_schema": schema,
                "produced_at": OBSERVED,
                "scope": "synthetic-public-scope",
                "members": [],
                "sources": [],
                "payload": "{}",
            }
            with self.subTest(schema=schema):
                with self.assertRaises(SelectionContractValidationError):
                    _ = ModelCatalog.from_dict(frame)
        self.assertEqual(bootstrap.to_dict(), load_catalog(REPO / "model-catalog.json").to_dict())


def _model(slug: str = "gpt-6-sol", efforts: tuple[str, ...] = ("low", "high")) -> DiscoveredModel:
    return DiscoveredModel(slug, efforts)


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
        # No free-text field exists: display_name was removed (Daybreak
        # blocker 6) — the wire cannot carry account-identifying text.
        self.assertEqual(
            {"slug": "gpt-6-sol", "reasoning_efforts": ["low", "high"]},
            _model().to_dict(),
        )
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

    def _test_floor(self) -> TrackFloor:
        return TrackFloor(
            ratings={dim: 3 for dim in (
                "reasoning",
                "coding",
                "scientific_methodological",
                "writing_editorial",
                "tool_use",
                "translation_multilingual",
            )},
            assessed_on="2026-09-26",
            confidence="low",
            decision="test",
            rationale="test floor",
        )

    def test_effort_restriction_designation_and_clamp(self) -> None:
        # D-054: max_only is an explicit owner-reviewed designation; the
        # floor-expansion clamp keeps only the max effort, and never
        # invents it when the runtime did not report one.
        from scarcity_router.model_tracks import ModelTrack

        unrestricted = ModelTrack(
            "openai", "sol", "GPT Sol", "^gpt-[0-9.]+-sol$", "standard", self._test_floor()
        )
        self.assertIsNone(unrestricted.effort_restriction)
        self.assertEqual(
            ("low", "high"),
            unrestricted.restrict_floor_efforts(("low", "high")),
        )

        light = ModelTrack(
            "openai", "luna", "GPT Luna", "^gpt-[0-9.]+-luna$", "standard",
            self._test_floor(), effort_restriction="max_only",
        )
        self.assertEqual(
            ("max",), light.restrict_floor_efforts(("low", "medium", "high", "max"))
        )
        self.assertEqual((), light.restrict_floor_efforts(("low", "high")))
        # "ultra" is a distinct effort above max: not part of the restriction.
        self.assertEqual((), light.restrict_floor_efforts(("low", "ultra")))
        # Unrestricted tracks pass the reported efforts through unchanged.
        self.assertEqual(
            ("max", "ultra"),
            unrestricted.restrict_floor_efforts(("max", "ultra")),
        )

    def test_effort_restriction_validation_fails_closed(self) -> None:
        from scarcity_router.model_tracks import ModelTrack, TrackRegistryError

        with self.assertRaises(TrackRegistryError):
            _ = ModelTrack(
                "openai", "luna", "GPT Luna", "^gpt-[0-9.]+-luna$", "standard",
                self._test_floor(), effort_restriction="low_only",
            )
        # A restricted track never routes, so restricting its effort is
        # meaningless and rejected.
        with self.assertRaises(TrackRegistryError):
            _ = ModelTrack(
                "openai", "daybreak", "Daybreak", "^gpt-daybreak.*$", "restricted",
                None, effort_restriction="max_only",
            )

    def test_track_registry_round_trips_effort_restriction(self) -> None:
        from scarcity_router.model_tracks import ModelTrack

        light = ModelTrack(
            "openai", "luna", "GPT Luna", "^gpt-[0-9.]+-luna$", "standard",
            self._test_floor(), notes="D-054 light family",
            effort_restriction="max_only",
        )
        rebuilt = ModelTrack.from_dict(light.to_dict())
        self.assertEqual(rebuilt.to_dict(), light.to_dict())
        self.assertEqual(rebuilt.effort_restriction, "max_only")
        self.assertEqual(rebuilt.notes, "D-054 light family")


if __name__ == "__main__":
    _ = unittest.main()


class SlugBoundTests(unittest.TestCase):
    """The derived-id bound is enforced at the inventory contract itself."""

    def test_over_long_slug_is_rejected_before_any_report(self) -> None:
        # 41-64 char safe-id slugs used to pass the inventory contract and
        # explode later in adoption (review finding 1); the bound is the
        # contract's, so the WORKER fails the listing closed and the
        # server can never be poisoned by one.
        with self.assertRaises(ModelInventoryError):
            _ = DiscoveredModel("a" * 41, ("low",))
        # Exactly at the bound is legal (35 + source 20 + effort 7 + 2
        # separators = 64, exactly the safe-id contract).
        self.assertEqual("b" * 35, DiscoveredModel("b" * 35, ("low",)).slug)

    def test_source_resource_id_rejects_unfit_combinations(self) -> None:
        from scarcity_router.model_inventory import source_resource_id

        with self.assertRaises(ValueError):
            _ = source_resource_id("a" * 20, "b" * 45)  # combined > 64 (safe-id)
        fitted = source_resource_id("s", "c" * 40)
        self.assertEqual("s:" + "c" * 40, fitted)
