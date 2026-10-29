"""Exact administrator grants for dynamically discovered resources."""

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from scarcity_router.execution_sources import RETIRE_AFTER_MISSES, SourceRegistry, add_configured_resources
from scarcity_router.capacity import CapacitySnapshot, CapacityWindow
from scarcity_router.model_inventory import DiscoveredModel, ModelInventoryReport, SourceInventory
from scarcity_router.machine_api import selection_envelope
from scarcity_router.selection_types import (
    CapabilityMinima, HardConstraints, ModelCatalog, ModelRef, TaskRequirement,
)
from scarcity_router.selector import capability_margin, neutral_selector_policy, select_model
from scarcity_router.server_config import SourceConfig, SourceContextEvidence
from scarcity_router.resource_state import ExecutionCapabilities, ResourceIdentity, ResourceRegistration
from scarcity_router.errors import SelectionContractValidationError
from scarcity_router.selection_app import (
    DEFAULT_CATALOG_PATH, DEFAULT_MODEL_POLICY_PATH, load_configured_artifacts,
)


def config(*, allowed_models: tuple[str, ...] = (),
           allowed_tracks: tuple[str, ...] = ()) -> SourceConfig:
    return SourceConfig("personal-openai", "codex_subscription", "Personal",
                        "worker-1", allowed_models=allowed_models,
                        allowed_tracks=allowed_tracks)


def inventory(slug: str, efforts: tuple[str, ...] = ("high",)) -> ModelInventoryReport:
    return ModelInventoryReport(worker_id="worker-1", sources=(SourceInventory(
        source_id="personal-openai", adapter_id="codex:personal-openai",
        kind="codex_subscription", observed_at="2026-09-24T12:00:00.000Z",
        auth_state="authenticated", runtime_name="codex", runtime_version="0.155.0",
        models=(DiscoveredModel(slug, efforts),),
    ),))


class DynamicSourcePolicyTests(unittest.TestCase):
    def test_context_evidence_is_exact_runtime_scoped_and_versioned(self) -> None:
        evidence = SourceContextEvidence("new-model", "high", "0.155.0", 4096, "local:test-attestation", "2026-10-01T00:00:00.000Z", "2026-09-01T00:00:00.000Z")
        source = SourceConfig("personal-openai", "codex_subscription", "Personal", "worker-1",
                              allowed_models=("new-model",), context_evidence=(evidence,))
        self.assertEqual(source, SourceConfig.from_dict(source.to_dict()))
        registry = SourceRegistry()
        registry.sync_configuration((source,))
        _ = registry.apply_inventory(inventory("new-model", ("high", "max")))
        registrations = {item.identity.variant: item for item in registry.derived_registrations()}
        capabilities = registrations["high"].capabilities
        self.assertEqual(4096, capabilities.context_limit_tokens)
        self.assertIsNone(registrations["max"].capabilities.context_limit_tokens)
        self.assertEqual("local:test-attestation", capabilities.context_evidence_ref)
        self.assertEqual(evidence.valid_until, capabilities.context_evidence_valid_until)
        changed = replace(source, context_evidence=(replace(evidence, runtime_version="1.0.0"),))
        registry.sync_configuration((changed,))
        self.assertIsNone(registry.derived_registrations()[0].capabilities.context_limit_tokens)
    def test_configured_local_resource_is_not_an_inferred_model_catalog(self) -> None:
        registration = ResourceRegistration(
            identity=ResourceIdentity(resource_id="local-resource", channel="local_app_adapter",
                                      provider="ollama", model="synthetic-local", entitlement="local_ungated"),
            freshness_ttl_seconds=60, poll_interval_seconds=30,
            capabilities=ExecutionCapabilities(),
        )
        catalog = add_configured_resources(ModelCatalog(None, None, ()), (registration,))
        self.assertEqual("ollama", catalog.entries[0].identity.provider)
        self.assertEqual("unconfigured", catalog.entries[0].identity.variant)
        self.assertIsNone(catalog.entries[0].reasoning_effort)
        self.assertIsNone(catalog.entries[0].capabilities.coding.rating)
        self.assertIsNone(catalog.entries[0].capacity_bindings)

    def test_default_composition_is_empty_and_legacy_overrides_are_refused(self) -> None:
        catalog, profiles, _version = load_configured_artifacts(
            DEFAULT_CATALOG_PATH, DEFAULT_MODEL_POLICY_PATH
        )
        self.assertIsNone(catalog.catalog_version)
        self.assertEqual((), catalog.entries)
        self.assertEqual((), profiles.definitions)
        with self.assertRaisesRegex(SelectionContractValidationError, "no longer supported"):
            _ = load_configured_artifacts(Path("old-catalog.json"), DEFAULT_MODEL_POLICY_PATH)

    def test_explicit_capacity_scope_applies_exhaustion_without_inference(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((SourceConfig(
            "personal-openai", "codex_subscription", "Personal", "worker-1",
            allowed_models=("new-model",), capacity_scope_ids=("codex",),
        ),))
        _ = registry.apply_inventory(inventory("new-model"))
        catalog = registry.derived_catalog_entries(ModelCatalog(None, None, ()))
        serialized = catalog.to_dict()
        self.assertEqual(catalog, ModelCatalog.from_dict(serialized))
        entries = serialized["entries"]
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        entries[0]["capacity_binding_resources"] = ["unrelated-resource"]
        with self.assertRaises(SelectionContractValidationError):
            _ = ModelCatalog.from_dict(serialized)
        snapshot = CapacitySnapshot(
            schema_version=3, provider="openai", source="synthetic", retrieved_at="2026-09-24T12:00:00.000Z",
            status="ok", windows=(
                CapacityWindow(resource="tokens", kind="five_hour", scope_id="codex", duration_seconds=18000,
                               used_percent=100, remaining_percent=0),
                CapacityWindow(resource="tokens", kind="weekly", scope_id="codex", duration_seconds=604800,
                               used_percent=100, remaining_percent=0),
            ), diagnostics=(),
        )
        decision = select_model(
            catalog=catalog, requirement=TaskRequirement("L0", CapabilityMinima(), HardConstraints()),
            policy=neutral_selector_policy(), snapshots=(snapshot,),
            evaluated_at=datetime(2026, 9, 24, 12, tzinfo=timezone.utc),
        )
        self.assertIsNone(decision.selected)
        self.assertEqual("capacity", decision.excluded[0].exclusion_stage)

    def test_different_source_scope_bindings_remain_unknown(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((
            SourceConfig("personal-openai", "codex_subscription", "One", "worker-1",
                         allowed_models=("new-model",), capacity_scope_ids=("codex",)),
            SourceConfig("other-openai", "codex_subscription", "Two", "worker-2",
                         allowed_models=("new-model",), capacity_scope_ids=("other-scope",)),
        ))
        _ = registry.apply_inventory(inventory("new-model"))
        _ = registry.apply_inventory(ModelInventoryReport(worker_id="worker-2", sources=(SourceInventory(
            source_id="other-openai", adapter_id="codex:other-openai", kind="codex_subscription",
            observed_at="2026-09-24T12:00:00.000Z", auth_state="authenticated",
            runtime_name="codex", runtime_version="1", models=(DiscoveredModel("new-model", ("high",)),),
        ),)))
        catalog = registry.derived_catalog_entries(ModelCatalog(None, None, ()))
        self.assertEqual(1, len(catalog.entries))
        self.assertIsNone(catalog.entries[0].capacity_bindings)

    def test_runtime_inventory_provenance_is_distinct_from_historical_catalog(self) -> None:
        inventory_view = ModelCatalog(None, None, ())
        self.assertEqual(inventory_view, ModelCatalog.from_dict(inventory_view.to_dict()))
        decision = select_model(
            catalog=inventory_view,
            requirement=TaskRequirement("L0", CapabilityMinima(), HardConstraints()),
            policy=neutral_selector_policy(), snapshots=(),
            evaluated_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        )
        envelope = selection_envelope(decision)
        self.assertEqual(2, envelope["schema_version"])
        self.assertEqual("runtime_inventory", decision.to_dict()["selection_input_kind"])
        self.assertIsNone(decision.catalog_version)
        self.assertIsNone(decision.selected)

    def test_exact_grant_adopts_only_reported_efforts_without_quality_claims(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((config(allowed_models=("new-model",)),))
        decisions = registry.apply_inventory(inventory("new-model"))
        self.assertEqual("routable", decisions[0].state)
        entries = registry.derived_catalog_entries(ModelCatalog(1, "2026-09-24", ())).entries
        self.assertIsNone(registry.derived_registrations()[0].capabilities.context_limit_tokens)
        self.assertIsNone(registry.derived_registrations()[0].capabilities.output_limit_tokens)
        self.assertEqual(1, len(entries))
        self.assertEqual("high", entries[0].identity.variant)
        self.assertIsNone(entries[0].capabilities.reasoning.rating)
        self.assertIsNone(entries[0].hard_properties.input_context_tokens)
        self.assertIsNone(entries[0].capacity_bindings)

    def test_unknown_quality_does_not_refuse_an_unconstrained_exact_model(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((config(allowed_models=("new-model",)),))
        _ = registry.apply_inventory(inventory("new-model"))
        catalog = registry.derived_catalog_entries(ModelCatalog(None, None, ()))
        requirement = TaskRequirement(
            "L0", CapabilityMinima(),
            HardConstraints(required_provider="openai", required_model=ModelRef("openai", "new-model"),
                            required_variant="high"),
        )
        decision = select_model(
            catalog=catalog, requirement=requirement, policy=neutral_selector_policy(),
            snapshots=(), evaluated_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(decision.selected)
        assert decision.selected is not None
        self.assertEqual("new-model", decision.selected.identity.model)
        self.assertNotIn("capability_margin", decision.selected.to_dict())
        with self.assertRaisesRegex(SelectionContractValidationError, "scoring was removed"):
            _ = capability_margin(CapabilityMinima(), catalog.entries[0].capabilities)
        self.assertTrue(decision.degraded)

        wrong_effort = TaskRequirement(
            "L0", CapabilityMinima(),
            HardConstraints(required_model=ModelRef("openai", "new-model"),
                            required_variant="low"),
        )
        mismatched = select_model(
            catalog=catalog, requirement=wrong_effort, policy=neutral_selector_policy(),
            snapshots=(), evaluated_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        )
        self.assertIsNone(mismatched.selected)
        self.assertEqual("hard_constraint", mismatched.excluded[0].exclusion_stage)

        quality_requirement = TaskRequirement(
            "L0", CapabilityMinima(coding=1), requirement.hard_constraints,
        )
        refused = select_model(
            catalog=catalog, requirement=quality_requirement, policy=neutral_selector_policy(),
            snapshots=(), evaluated_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        )
        self.assertIsNone(refused.selected)
        self.assertEqual("capability", refused.excluded[0].exclusion_stage)

    def test_legacy_track_and_missing_grant_fail_closed(self) -> None:
        for source in (config(), config(allowed_tracks=("openai/sol",),
                                       allowed_models=("gpt-6-sol",))):
            with self.subTest(source=source):
                registry = SourceRegistry()
                registry.sync_configuration((source,))
                decisions = registry.apply_inventory(inventory("gpt-6-sol"))
                self.assertNotEqual("routable", decisions[0].state)
                self.assertEqual((), registry.derived_registrations())

    def test_removing_grant_revokes_existing_resource(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((config(allowed_models=("gpt-6-sol",)),))
        _ = registry.apply_inventory(inventory("gpt-6-sol"))
        self.assertTrue(registry.derived_registrations())
        registry.sync_configuration((config(),))
        self.assertEqual((), registry.derived_registrations())
        view = registry.source_view()[0]
        self.assertEqual(0, view["routable"])
        self.assertEqual(
            [{"slug": "gpt-6-sol", "state": "discovered", "reason": "model_not_allowed",
              "reasoning_efforts": ["high"]}],
            view["models"],
        )

    def test_inventory_absence_and_reappearance_preserve_lifecycle(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((config(allowed_models=("new-model",)),))
        _ = registry.apply_inventory(inventory("new-model"))
        registration = registry.derived_registrations()[0]
        empty = ModelInventoryReport(worker_id="worker-1", sources=(SourceInventory(
            source_id="personal-openai", adapter_id="codex:personal-openai",
            kind="codex_subscription", observed_at="2026-09-24T12:00:00.000Z",
            auth_state="authenticated", runtime_name="codex", runtime_version="1", models=(),
        ),))
        for _index in range(RETIRE_AFTER_MISSES):
            _ = registry.apply_inventory(empty)
        self.assertEqual((), registry.derived_registrations())
        _ = registry.apply_inventory(inventory("new-model"))
        self.assertEqual(registration, registry.derived_registrations()[0])

    def test_untrusted_worker_inventory_cannot_adopt(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((config(allowed_models=("new-model",)),))
        report = inventory("new-model")
        untrusted = ModelInventoryReport(worker_id="another-worker", sources=report.sources)
        self.assertEqual((), registry.apply_inventory(untrusted))
        self.assertEqual((), registry.derived_registrations())

    def test_reassigning_source_worker_discards_old_inventory(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((config(allowed_models=("new-model",)),))
        _ = registry.apply_inventory(inventory("new-model"))
        self.assertTrue(registry.derived_registrations())
        registry.sync_configuration((SourceConfig(
            "personal-openai", "codex_subscription", "Personal", "worker-2",
            allowed_models=("new-model",),
        ),))
        self.assertEqual((), registry.derived_registrations())
        self.assertEqual((), registry.apply_inventory(inventory("new-model")))
        mismatched = SourceInventory(
            source_id="personal-openai", adapter_id="codex:other", kind="codex_subscription",
            observed_at="2026-09-24T12:00:00.000Z", auth_state="authenticated",
            runtime_name="codex", runtime_version="1", models=(DiscoveredModel("new-model", ("high",)),),
        )
        self.assertEqual((), registry.apply_inventory(ModelInventoryReport(
            worker_id="worker-2", sources=(mismatched,),
        )))
        self.assertEqual((), registry.derived_registrations())

    def test_plan_lane_never_claims_a_discovered_physical_model(self) -> None:
        registry = SourceRegistry()
        registry.sync_configuration((SourceConfig(
            "zai-plan", "zcode_subscription", "Plan", "worker-1",
            allowed_models=("synthetic-model", "plan-managed"),
        ),))
        report = ModelInventoryReport(worker_id="worker-1", sources=(SourceInventory(
            source_id="zai-plan", adapter_id="zcode:zai-plan", kind="zcode_subscription",
            observed_at="2026-09-24T12:00:00.000Z", auth_state="unverified",
            runtime_name="zcode", runtime_version="1",
            models=(DiscoveredModel("synthetic-model", ()), DiscoveredModel("plan-managed", ("high",))),
        ),))
        decisions = registry.apply_inventory(report)
        self.assertTrue(all(item.reason == "plan_lane_unverified" for item in decisions))
        self.assertEqual((), registry.derived_registrations())


if __name__ == "__main__":
    _ = unittest.main()
