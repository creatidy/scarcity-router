"""Server-side execution-source tests (D-053, issue #119).

Adoption policy, derived exact resources, track-floor catalog entries,
quota-pool semantics, ownership and the upgrade simulation (T0 → T1),
against the reviewed repository track artifact — no mocks of the policy
itself.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import cast, override

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scarcity_router.execution_sources import (  # noqa: E402
    RETIRE_AFTER_MISSES,
    SourceRegistry,
)
from scarcity_router.model_inventory import (  # noqa: E402
    DiscoveredModel,
    ModelInventoryReport,
    SourceInventory,
)
from scarcity_router.model_tracks import load_track_registry  # noqa: E402
from scarcity_router.selection_types import REASONING_EFFORTS  # noqa: E402
from scarcity_router.server_config import (  # noqa: E402
    CONFIG_SCHEMA_VERSION,
    ResourceConfig,
    ServerConfigError,
    ServerConfiguration,
    SourceConfig,
)

OBSERVED = "2026-09-24T12:00:00.000Z"


def _config(**overrides: object) -> SourceConfig:
    kwargs: dict[str, object] = {
        "source_id": "personal-openai",
        "kind": "codex_subscription",
        "label": "Personal ChatGPT Pro",
        "worker_id": "worker-1",
    }
    kwargs.update(overrides)
    # Keys above are fixed literals; dict[str, object] is just the merge vehicle.
    return SourceConfig(**kwargs)  # pyright: ignore[reportArgumentType] - fixed-literal test helper


def _inventory(
    source_id: str,
    worker_id: str,
    models: tuple[DiscoveredModel, ...],
    auth_state: str = "authenticated",
) -> ModelInventoryReport:
    return ModelInventoryReport(
        worker_id=worker_id,
        sources=(
            SourceInventory(
                source_id=source_id,
                adapter_id=f"codex:{source_id}",
                kind="codex_subscription",
                observed_at=OBSERVED,
                auth_state=auth_state,  # type: ignore[arg-type]
                runtime_name="codex",
                runtime_version="0.155.0",
                models=models,
            ),
        ),
    )


def _model(slug: str, efforts: tuple[str, ...] = ("low", "high")) -> DiscoveredModel:
    return DiscoveredModel(slug, efforts)


SIX_GEN = (
    _model("gpt-6-luna", ("low", "medium", "high")),
    _model("gpt-6-sol", ("low", "medium", "high", "xhigh", "max", "ultra")),
    _model("gpt-6-astra", ("low", "medium", "high", "xhigh", "max", "ultra")),
)


class AdoptionTests(unittest.TestCase):
    def __init__(self, method_name: str = "runTest") -> None:
        self.registry: SourceRegistry = SourceRegistry(
            track_registry=load_track_registry()
        )
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self.registry = SourceRegistry(track_registry=load_track_registry())
        self.registry.sync_configuration((_config(),))

    def _apply(self, models: tuple[DiscoveredModel, ...], **kw: object):
        return self.registry.apply_inventory(
            _inventory("personal-openai", "worker-1", models, **kw)  # pyright: ignore[reportArgumentType] - fixed-literal helper
        )

    def test_new_known_track_model_is_adopted_without_manual_configuration(self) -> None:
        decisions = self._apply((_model("gpt-6.1-sol", ("low", "high")),))
        self.assertEqual("routable", decisions[0].state)
        self.assertEqual("openai/sol", decisions[0].track_id)
        registrations = self.registry.derived_registrations()
        self.assertEqual(1, len(registrations))
        identity = registrations[0].identity
        self.assertEqual("personal-openai:gpt-6.1-sol", identity.resource_id)
        self.assertEqual("worker_bridged", identity.channel)
        self.assertEqual("openai", identity.provider)
        self.assertEqual("gpt-6.1-sol", identity.model)
        self.assertEqual("subscription_included", identity.entitlement)

    def test_unknown_model_is_discovered_but_never_routable(self) -> None:
        decisions = self._apply((_model("some-new-unclassified-model", ("high",)),))
        self.assertEqual("discovered", decisions[0].state)
        self.assertEqual("unclassified_track", decisions[0].reason)
        self.assertEqual((), self.registry.derived_registrations())
        # And it stays visible in the source view.
        view = self.registry.source_view()[0]
        self.assertEqual(1, view["errors"])

    def test_daybreak_is_restricted_never_materialized(self) -> None:
        decisions = self._apply((_model("gpt-daybreak-blue-latest", ("high",)),))
        self.assertEqual("restricted", decisions[0].state)
        self.assertEqual("restricted_track", decisions[0].reason)
        self.assertEqual((), self.registry.derived_registrations())
        view = self.registry.source_view()[0]
        self.assertEqual(1, view["restricted"])
        self.assertEqual(0, view["routable"])

    def test_adoption_requires_an_authenticated_source(self) -> None:
        decisions = self._apply(SIX_GEN, auth_state="auth_required")
        self.assertTrue(all(d.state == "classified" for d in decisions))
        self.assertEqual((), self.registry.derived_registrations())

    def test_auto_adopt_disabled_keeps_models_classified(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration((_config(auto_adopt=False),))
        decisions = registry.apply_inventory(
            _inventory("personal-openai", "worker-1", (_model("gpt-6-sol", ("high",)),))
        )
        self.assertEqual("classified", decisions[0].state)
        self.assertEqual("adoption_disabled", decisions[0].reason)

    def test_allowed_tracks_narrows_adoption(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration(
            (_config(allowed_tracks=("openai/sol",)),)
        )
        decisions = registry.apply_inventory(
            _inventory(
                "personal-openai",
                "worker-1",
                (_model("gpt-6-sol", ("high",)), _model("gpt-6-luna", ("high",))),
            )
        )
        states = {d.slug: d.state for d in decisions}
        self.assertEqual("routable", states["gpt-6-sol"])
        self.assertEqual("classified", states["gpt-6-luna"])
        self.assertEqual("track_not_allowed", next(
            d.reason for d in decisions if d.slug == "gpt-6-luna"
        ))

    def test_policy_effort_vocabulary_reached_only_through_reported_efforts(self) -> None:
        # gpt-6.1-sol has no exact catalog entries, so every (model,
        # effort) pair in the merged view is a derived floor entry — and
        # only efforts the runtime actually advertised appear.
        _ = self._apply((_model("gpt-6.1-sol", ("low", "ultra")),))
        entries = {
            (e.identity.model, e.identity.variant)
            for e in self.registry.derived_catalog_entries(_base_catalog()).entries
            if e.identity.model == "gpt-6.1-sol"
        }
        self.assertIn(("gpt-6.1-sol", "low"), entries)
        self.assertIn(("gpt-6.1-sol", "ultra"), entries)
        self.assertNotIn(("gpt-6.1-sol", "high"), entries)  # never advertised

    def test_effort_only_in_static_vocabulary_is_routable(self) -> None:
        # "ultra" is part of the calibrated policy vocabulary now.
        self.assertIn("ultra", REASONING_EFFORTS)


class LifecycleTests(unittest.TestCase):
    def __init__(self, method_name: str = "runTest") -> None:
        self.registry: SourceRegistry = SourceRegistry(
            track_registry=load_track_registry()
        )
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self.registry = SourceRegistry(track_registry=load_track_registry())
        self.registry.sync_configuration((_config(),))
        _ = self.registry.apply_inventory(
            _inventory("personal-openai", "worker-1", SIX_GEN)
        )

    def _apply_without(self, slug: str) -> None:
        models = tuple(m for m in SIX_GEN if m.slug != slug)
        _ = self.registry.apply_inventory(
            _inventory("personal-openai", "worker-1", models)
        )

    def test_disappearance_is_deterministic_unavailable_then_retired(self) -> None:
        rid = "personal-openai:gpt-6-sol"
        # Miss 1 and 2: still registered (bounded grace), but the NEXT
        # authenticated observation's derived set is recomputed from what
        # the runtime lists — the resource remains registered until the
        # miss counter retires it.
        self._apply_without("gpt-6-sol")
        self._apply_without("gpt-6-sol")
        rids = {r.identity.resource_id for r in self.registry.derived_registrations()}
        self.assertIn(rid, rids)
        self._apply_without("gpt-6-sol")  # third consecutive miss
        rids = {r.identity.resource_id for r in self.registry.derived_registrations()}
        self.assertNotIn(rid, rids)
        view = self.registry.source_view()[0]
        self.assertIn("gpt-6-sol", cast("list[str]", view["retired"]))

    def test_reappearing_model_re_materializes(self) -> None:
        for _ in range(RETIRE_AFTER_MISSES):
            self._apply_without("gpt-6-luna")
        rids = {r.identity.resource_id for r in self.registry.derived_registrations()}
        self.assertNotIn("personal-openai:gpt-6-luna", rids)
        # The provider restores it.
        _ = self.registry.apply_inventory(
            _inventory("personal-openai", "worker-1", SIX_GEN)
        )
        rids = {r.identity.resource_id for r in self.registry.derived_registrations()}
        self.assertIn("personal-openai:gpt-6-luna", rids)

    def test_retire_constant_is_bounded(self) -> None:
        self.assertEqual(3, RETIRE_AFTER_MISSES)


class QuotaPoolTests(unittest.TestCase):
    def test_two_sources_default_to_two_pools_and_two_resources(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration(
            (
                _config(source_id="personal-openai"),
                _config(source_id="second-openai", label="Second account"),
            )
        )
        _ = registry.apply_inventory(
            _inventory("personal-openai", "worker-1", (_model("gpt-6-sol", ("high",)),))
        )
        _ = registry.apply_inventory(
            _inventory("second-openai", "worker-1", (_model("gpt-6-sol", ("high",)),))
        )
        registrations = registry.derived_registrations()
        self.assertEqual(2, len(registrations))
        pools = {
            r.identity.resource_id: r.identity.quota_pool_ids for r in registrations
        }
        self.assertEqual(
            ("pool-second-openai",),
            pools["second-openai:gpt-6-sol"],
        )
        self.assertEqual(
            ("pool-personal-openai",),
            pools["personal-openai:gpt-6-sol"],
        )
        # Same physical model, two independent targets (D-042/D-053).
        self.assertEqual(
            {"gpt-6-sol", "gpt-6-sol"},
            {r.identity.model for r in registrations},
        )

    def test_explicit_shared_pool_is_the_only_sharing_path(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration(
            (
                _config(source_id="a", quota_pool_id="shared-family"),
                _config(source_id="b", quota_pool_id="shared-family"),
            )
        )
        _ = registry.apply_inventory(
            _inventory("a", "worker-1", (_model("gpt-6-sol", ("high",)),))
        )
        _ = registry.apply_inventory(
            _inventory("b", "worker-1", (_model("gpt-6-sol", ("high",)),))
        )
        pools = {
            r.identity.resource_id: r.identity.quota_pool_ids
            for r in registry.derived_registrations()
        }
        self.assertEqual(("shared-family",), pools["a:gpt-6-sol"])
        self.assertEqual(("shared-family",), pools["b:gpt-6-sol"])


class OwnershipTests(unittest.TestCase):
    def test_derived_resources_own_to_their_configured_worker(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration(
            (_config(source_id="personal-openai", worker_id="worker-1"),)
        )
        _ = registry.apply_inventory(
            _inventory("personal-openai", "worker-1", (_model("gpt-6-sol", ("high",)),))
        )
        self.assertEqual(
            "worker-1", registry.owner_of("personal-openai:gpt-6-sol")
        )
        self.assertIsNone(registry.owner_of("unknown-source:gpt-6-sol"))
        self.assertIsNone(registry.owner_of("zai-plan-1"))
        self.assertEqual(
            "codex:personal-openai", registry.adapter_of("personal-openai:gpt-6-sol")
        )


class ConfigurationTests(unittest.TestCase):
    def test_v2_document_round_trips_sources(self) -> None:
        config = ServerConfiguration(
            sources=(
                _config(allowed_tracks=("openai/sol", "openai/astra")),
                _config(source_id="second-openai", label="Second"),
            )
        )
        document = config.to_document()
        self.assertEqual(CONFIG_SCHEMA_VERSION, document["schema_version"])
        parsed = ServerConfiguration.from_document(document)
        self.assertEqual(2, len(parsed.sources))
        self.assertEqual(
            ("openai/sol", "openai/astra"), parsed.sources[0].allowed_tracks
        )

    def test_v1_document_still_parses_as_zero_sources(self) -> None:
        document: dict[str, object] = {
            "schema_version": 1,
            "resources": [],
        }
        parsed = ServerConfiguration.from_document(document)
        self.assertEqual((), parsed.sources)

    def test_v1_document_rejects_sources(self) -> None:
        document: dict[str, object] = {
            "schema_version": 1,
            "sources": [_config().to_dict()],
        }
        with self.assertRaises(ServerConfigError):
            _ = ServerConfiguration.from_document(document)

    def test_hand_made_derived_resource_ids_are_rejected(self) -> None:
        from scarcity_router.resource_state import (
            ResourceIdentity,
            ResourceRegistration,
        )

        registration = ResourceRegistration(
            identity=ResourceIdentity(
                resource_id="personal-openai:gpt-6-sol",
                channel="worker_bridged",
                provider="openai",
                model="gpt-6-sol",
                entitlement="subscription_included",
            ),
            freshness_ttl_seconds=600,
        )
        hand_made = ResourceConfig(
            registration=registration, enabled=True, worker_id="w"
        )
        with self.assertRaises(ServerConfigError):
            _ = ServerConfiguration(resources=(hand_made,))

    def test_unknown_kind_and_long_ids_fail_closed(self) -> None:
        with self.assertRaises(ServerConfigError):
            _ = _config(kind="generic_discovery")
        with self.assertRaises(ServerConfigError):
            _ = _config(source_id="way-too-long-source-identifier")


def _base_catalog():
    from scarcity_router.selection_app import DEFAULT_CATALOG_PATH, load_catalog

    return load_catalog(DEFAULT_CATALOG_PATH)


class DerivedCatalogTests(unittest.TestCase):
    def test_floor_entries_are_conservative_and_provenance_bearing(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration((_config(),))
        _ = registry.apply_inventory(
            _inventory(
                "personal-openai",
                "worker-1",
                (_model("gpt-6.1-sol", ("high", "ultra")),),
            )
        )
        merged = registry.derived_catalog_entries(_base_catalog())
        entry = next(
            e
            for e in merged.entries
            if e.identity.model == "gpt-6.1-sol" and e.identity.variant == "high"
        )
        # Capability inheritance is ONLY the track floor...
        classified = load_track_registry().classify("openai", "gpt-6.1-sol")
        assert classified is not None and classified.floor is not None
        floor = classified.floor
        self.assertEqual(floor.ratings["reasoning"], entry.capabilities.reasoning.rating)
        self.assertEqual(floor.ratings["tool_use"], entry.capabilities.tool_use.rating)
        # ...provenance points at the reviewed artifact...
        self.assertEqual(
            "model-tracks.json", entry.capabilities.reasoning.evidence[0].source
        )
        self.assertEqual("low", entry.capabilities.reasoning.confidence)
        # ...context/output carry the owner-reviewed family-continuity
        # assumption; vision and tool support stay honestly unknown...
        self.assertEqual(1050000, entry.hard_properties.input_context_tokens)
        self.assertIsNone(entry.hard_properties.supports_vision)
        self.assertIsNone(entry.hard_properties.supports_tool_use)
        # ...and capacity applicability is unknown, never optimistic.
        self.assertIsNone(entry.capacity_bindings)

    def test_exact_catalog_entries_always_win(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration((_config(),))
        _ = registry.apply_inventory(
            _inventory("personal-openai", "worker-1", (_model("gpt-6-sol", ("high",)),))
        )
        merged = registry.derived_catalog_entries(_base_catalog())
        sol_high = [
            e
            for e in merged.entries
            if e.identity.model == "gpt-6-sol" and e.identity.variant == "high"
        ]
        self.assertEqual(1, len(sol_high))
        # The reviewed D-053 catalog entry (owner-reviewed display name),
        # not a re-derived floor entry — exactly one entry for the pair.
        self.assertEqual("GPT-6 Sol High", sol_high[0].display_name)

    def test_registry_never_mutates_the_base_catalog(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration((_config(),))
        _ = registry.apply_inventory(
            _inventory("personal-openai", "worker-1", (_model("gpt-6.1-sol", ("high",)),))
        )
        base = _base_catalog()
        _ = registry.derived_catalog_entries(base)
        self.assertFalse(
            any(e.identity.model == "gpt-6.1-sol" for e in base.entries)
        )


class UpgradeSimulationTests(unittest.TestCase):
    """The central acceptance criterion: T0/T1 without user edits."""

    def test_new_generation_becomes_routable_without_configuration_change(self) -> None:
        registry = SourceRegistry(track_registry=load_track_registry())
        registry.sync_configuration((_config(),))
        # T0: the source exposes gpt-6-sol.
        _ = registry.apply_inventory(
            _inventory("personal-openai", "worker-1", (_model("gpt-6-sol", ("high",)),))
        )
        self.assertEqual(
            ("personal-openai:gpt-6-sol",),
            tuple(r.identity.resource_id for r in registry.derived_registrations()),
        )
        # T1: the provider adds gpt-6.1-sol. The SERVER CONFIGURATION IS
        # UNTOUCHED — the same registry, the same config object.
        _ = registry.apply_inventory(
            _inventory(
                "personal-openai",
                "worker-1",
                (_model("gpt-6-sol", ("high",)), _model("gpt-6.1-sol", ("high", "ultra"))),
            )
        )
        rids = {r.identity.resource_id for r in registry.derived_registrations()}
        self.assertIn("personal-openai:gpt-6-sol", rids)
        self.assertIn("personal-openai:gpt-6.1-sol", rids)
        # The new generation routes at floor level only.
        merged = registry.derived_catalog_entries(_base_catalog())
        new_entries = [
            e for e in merged.entries if e.identity.model == "gpt-6.1-sol"
        ]
        self.assertTrue(new_entries)
        self.assertTrue(
            all(
                rating is not None and rating <= 4
                for e in new_entries
                for rating in [e.capabilities.reasoning.rating]
            )
        )


if __name__ == "__main__":
    _ = unittest.main()
