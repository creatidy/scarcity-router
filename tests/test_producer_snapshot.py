"""Internal producer projection tests; no network consumer contract."""

from __future__ import annotations

import threading
from dataclasses import replace
from unittest.mock import patch

from scarcity_router.control_api import ProducerSnapshot
from scarcity_router.errors import CapacityValidationError
from scarcity_router.model_inventory import DiscoveredModel, ModelInventoryReport, SourceInventory
from scarcity_router.resource_state import ResourceIdentity, ResourceRegistration, ResourceStateSnapshot, ResourceHealth, WorkerStateReport
from scarcity_router.server_config import ResourceConfig, ServerConfiguration, SourceConfig
from tests.server_fixtures import ServerHarness


NOW = "2026-09-16T12:00:00.000Z"
LATER = "2026-09-16T13:00:00.000Z"


class ProducerSnapshotTests(ServerHarness):
    def test_discovery_disabled_freshness_and_disconnect(self) -> None:
        clock = [NOW]
        self.plane._registry_clock = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("disabled", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
        source = SourceConfig("owned", "codex_subscription", "Owned", "worker-1", allowed_models=("allowed",))
        inventory = SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription", observed_at=NOW,
            auth_state="authenticated", runtime_name="codex", runtime_version="1",
            models=(DiscoveredModel("allowed", ("high",)), DiscoveredModel("restricted", ("high",))),
        )
        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(resources=(resource,), sources=(source,)))  # pyright: ignore[reportPrivateUsage]
            self.plane._apply_source_inventory(ModelInventoryReport(worker_id="worker-1", sources=(inventory,)))  # pyright: ignore[reportPrivateUsage]
        registry = self.plane.current_application().registry.registry_snapshot(now=NOW)
        self.assertEqual(1, len(registry.entries))
        derived = registry.entries[0].identity
        self.plane.apply_resource_observation(ResourceStateSnapshot(
            schema_version=1, identity=derived, observed_at=NOW, health=ResourceHealth(status="ok"),
        ))
        with patch.object(self.plane.worker_endpoint, "connected_worker_ids", return_value=("worker-1",)):
            snapshot = self.plane.producer_snapshot()
        by_id = {item.resource_id: item for item in snapshot.resources}
        self.assertEqual("disabled", by_id["disabled"].availability)
        self.assertEqual("not_evaluated", by_id["disabled"].freshness)
        self.assertEqual("source", by_id[derived.resource_id].origin)
        self.assertTrue(by_id[derived.resource_id].worker_connected)
        self.assertEqual("fresh", by_id[derived.resource_id].freshness)
        self.assertEqual("unknown", by_id[derived.resource_id].availability)
        discoveries = {item.model: item for item in snapshot.discoveries}
        self.assertEqual("routable", discoveries["allowed"].state)
        self.assertEqual("discovered", discoveries["restricted"].state)
        self.assertEqual("model_not_allowed", discoveries["restricted"].reason)
        self.assertEqual("unknown", snapshot.sources[0].inventory_freshness)
        disconnected = self.plane.producer_snapshot()
        derived_state = next(item for item in disconnected.resources if item.resource_id == derived.resource_id)
        self.assertFalse(derived_state.worker_connected)
        self.assertEqual("unavailable", derived_state.availability)
        self.assertEqual(snapshot.discoveries, disconnected.discoveries)
        clock[0] = LATER
        with patch.object(self.plane.worker_endpoint, "connected_worker_ids", return_value=("worker-1",)):
            stale = self.plane.producer_snapshot()
        stale_resource = next(item for item in stale.resources if item.resource_id == derived.resource_id)
        self.assertEqual("stale", stale_resource.freshness)
        self.assertEqual("unknown", stale_resource.availability)

    def test_snapshot_waits_for_configuration_publication(self) -> None:
        identity = ResourceIdentity("disabled", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        snapshots: list[ProducerSnapshot] = []
        sync = self.plane._source_registry.sync_configuration  # pyright: ignore[reportPrivateUsage]

        def paused_sync(sources: tuple[SourceConfig, ...]) -> None:
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test timed out")
            sync(sources)

        def save() -> None:
            self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]

        def read() -> None:
            snapshots.append(self.plane.producer_snapshot())
            finished.set()

        with patch.object(self.plane._source_registry, "sync_configuration", side_effect=paused_sync):  # pyright: ignore[reportPrivateUsage]
            writer = threading.Thread(target=save)
            writer.start()
            self.assertTrue(entered.wait(timeout=5))
            reader = threading.Thread(target=read)
            reader.start()
            try:
                self.assertFalse(finished.wait(timeout=0.05))
            finally:
                release.set()
                writer.join(timeout=5)
                reader.join(timeout=5)
        self.assertTrue(finished.is_set())
        self.assertEqual(("disabled",), tuple(item.resource_id for item in snapshots[0].resources))

    def test_rejected_future_observation_cannot_poison_rebuild(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60))
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        future = ResourceStateSnapshot(
            schema_version=1, identity=identity, observed_at=LATER, health=ResourceHealth(status="ok"),
        )
        with self.assertRaises(CapacityValidationError):
            self.plane.apply_resource_observation(future)
        self.plane._rebuild_application()  # pyright: ignore[reportPrivateUsage]
        state = self.plane.producer_snapshot().resources[0]
        self.assertIsNone(state.observed_at)
        self.assertEqual("never_observed", state.freshness)

    def test_reassignment_after_endpoint_check_rejects_old_worker_report(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("worker-resource", "worker_bridged", "openai", "model", "subscription_included")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), worker_id="worker-1")
        observation = ResourceStateSnapshot(schema_version=1, identity=identity, observed_at=NOW,
                                            health=ResourceHealth(status="ok"))
        report = WorkerStateReport(schema_version=1, worker_id="worker-1", reported_at=NOW, resources=(observation,))
        checked = threading.Event()
        proceed = threading.Event()
        result: list[str] = []

        def old_worker() -> None:
            owner = self.plane._configured_worker_owner(identity.resource_id)  # pyright: ignore[reportPrivateUsage]
            if owner != "worker-1":
                result.append("wrong initial owner")
                checked.set()
                return
            checked.set()
            if not proceed.wait(timeout=5):
                result.append("timed out")
                return
            try:
                self.plane.apply_worker_report(report)
            except ValueError:
                result.append("rejected")
            else:
                result.append("accepted")

        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
            self.plane.apply_worker_report(report)
            thread = threading.Thread(target=old_worker)
            thread.start()
            try:
                self.assertTrue(checked.wait(timeout=5))
                self.plane._save_config(ServerConfiguration(resources=(replace(resource, worker_id="worker-2"),)))  # pyright: ignore[reportPrivateUsage]
            finally:
                proceed.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["rejected"], result)
        self.assertEqual("never_observed", self.plane.producer_snapshot().resources[0].freshness)

    def test_missing_retirement_and_rediscovery(self) -> None:
        source = SourceConfig("owned", "codex_subscription", "Owned", "worker-1", allowed_models=("allowed",))
        initial = SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription", observed_at=NOW,
            auth_state="authenticated", runtime_name="codex", runtime_version="1",
            models=(DiscoveredModel("allowed", ("high",)),),
        )
        missing_inventory = SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription", observed_at=NOW,
            auth_state="authenticated", runtime_name="codex", runtime_version="1", models=(),
        )
        def apply(inventory: SourceInventory) -> None:
            self.plane._apply_source_inventory(ModelInventoryReport(worker_id="worker-1", sources=(inventory,)))  # pyright: ignore[reportPrivateUsage]

        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(sources=(source,)))  # pyright: ignore[reportPrivateUsage]
            apply(initial)
            for _ in range(2):
                apply(missing_inventory)
                missing = self.plane.producer_snapshot()
                self.assertEqual("retained_missing", missing.discoveries[0].lifecycle)
                self.assertEqual("not_in_latest_inventory", missing.discoveries[0].state)
                self.assertIsNone(missing.discoveries[0].observed_at)
                self.assertEqual(1, len(missing.resources))
            apply(missing_inventory)
            retired = self.plane.producer_snapshot()
            self.assertEqual("retired", retired.discoveries[0].lifecycle)
            self.assertEqual((), retired.resources)
            apply(initial)
            rediscovered = self.plane.producer_snapshot()
            self.assertEqual("routable", rediscovered.discoveries[0].state)
            self.assertIsNone(rediscovered.discoveries[0].lifecycle)
            self.assertEqual(1, len(rediscovered.resources))

    def test_inventory_from_former_owner_is_rejected_after_reassignment(self) -> None:
        source = SourceConfig("owned", "codex_subscription", "Owned", "worker-1", allowed_models=("allowed",))
        inventory = SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription", observed_at=NOW,
            auth_state="authenticated", runtime_name="codex", runtime_version="1",
            models=(DiscoveredModel("allowed", ("high",)),),
        )
        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(sources=(source,)))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual("worker-1", self.plane._configured_worker_owner("owned:allowed"))  # pyright: ignore[reportPrivateUsage]
            self.plane._save_config(ServerConfiguration(sources=(replace(source, worker_id="worker-2"),)))  # pyright: ignore[reportPrivateUsage]
            with self.assertRaises(ValueError):
                self.plane._apply_source_inventory(ModelInventoryReport(worker_id="worker-1", sources=(inventory,)))  # pyright: ignore[reportPrivateUsage]
        snapshot = self.plane.producer_snapshot()
        self.assertEqual((), snapshot.resources)
        self.assertEqual((), snapshot.discoveries)
        self.assertEqual("never_observed", snapshot.sources[0].inventory_freshness)

    def test_restart_retains_configuration_but_not_discovery(self) -> None:
        source = SourceConfig("owned", "codex_subscription", "Owned", "worker-1", allowed_models=("allowed",))
        inventory = SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription", observed_at=NOW,
            auth_state="authenticated", runtime_name="codex", runtime_version="1",
            models=(DiscoveredModel("allowed", ("high",)),),
        )
        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(sources=(source,)))  # pyright: ignore[reportPrivateUsage]
            self.plane._apply_source_inventory(ModelInventoryReport(worker_id="worker-1", sources=(inventory,)))  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(self.plane.producer_snapshot().discoveries)
        restarted = self.make_plane(self.data_dir)
        try:
            self.assertEqual((source,), restarted.configuration.sources)
            self.assertEqual((), restarted.producer_snapshot().discoveries)
            self.assertEqual((), restarted.producer_snapshot().resources)
            self.assertEqual("never_observed", restarted.producer_snapshot().sources[0].inventory_freshness)
        finally:
            restarted.close_worker_store()
            restarted.store.close()
