"""Internal event/snapshot, rollback and restart acceptance tests."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from typing import cast
from unittest.mock import patch

from scarcity_router.control_api import ProducerCursor, ProducerEventSnapshot
from scarcity_router.errors import CapacityValidationError
from scarcity_router.execution_sources import SourceRegistry
from scarcity_router.model_inventory import DiscoveredModel, ModelInventoryReport, SourceInventory
from scarcity_router.resource_state import ResourceHealth, ResourceIdentity, ResourceRegistration, ResourceStateSnapshot, WorkerStateReport
from scarcity_router.server_config import ResourceConfig, ServerConfiguration, SourceConfig
from scarcity_router.server_store import ServerStoreError
from tests.server_fixtures import ServerHarness
from tests.worker_fixtures import MemoryTransport, ScriptedWorker
from scarcity_router.worker_protocol import ErrorMessage, HelloAckMessage, PairResultMessage, StateReportAckMessage

NOW = "2026-09-16T12:00:00.000Z"
NEXT = "2026-09-16T12:00:01.000Z"
FUTURE = "2026-09-16T13:00:00.000Z"


class ProducerEventTests(ServerHarness):
    def test_semantic_deduplication_replay_and_retention_gap(self) -> None:
        clock = [NOW]
        self.plane._registry_clock = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60))
        initial = self.plane.producer_event_snapshot()
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        registered = self.plane.producer_event_snapshot()
        self.assertEqual(initial.cursor.sequence + 1, registered.cursor.sequence)
        observation = ResourceStateSnapshot(schema_version=1, identity=identity, observed_at=NOW,
                                            health=ResourceHealth(status="ok"))
        self.plane.apply_resource_observation(observation)
        observed = self.plane.producer_event_snapshot()
        clock[0] = NEXT
        self.plane.apply_resource_observation(replace(observation, observed_at=NEXT))
        self.assertEqual(observed, self.plane.producer_event_snapshot())
        replay = self.plane.producer_replay(initial.cursor)
        self.assertIsNone(replay.resync)
        self.assertEqual((registered.cursor.sequence, observed.cursor.sequence),
                         tuple(event.cursor.sequence for event in replay.events))
        self.assertEqual(observed.payload, replay.events[-1].payload)
        self.assertNotIn("observed_at", observed.payload)
        clock[0] = FUTURE
        stale = self.plane.producer_event_snapshot()
        self.assertEqual(observed.cursor.sequence + 1, stale.cursor.sequence)
        document = cast("dict[str, object]", json.loads(stale.payload))
        resources = cast("list[dict[str, object]]", document["resources"])
        self.assertEqual("stale", resources[0]["freshness"])
        with patch("scarcity_router.server_store._PRODUCER_EVENT_LIMIT", 1):
            self.plane._save_config(ServerConfiguration(resources=(replace(resource, enabled=False),)))  # pyright: ignore[reportPrivateUsage]
        gap = self.plane.producer_replay(initial.cursor)
        self.assertEqual((), gap.events)
        self.assertEqual("cursor_expired", gap.reason)
        self.assertEqual(self.plane.producer_event_snapshot(), gap.resync)

    def test_restart_requires_snapshot_not_reconstructed_observations(self) -> None:
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60))
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        self.plane.apply_resource_observation(ResourceStateSnapshot(
            schema_version=1, identity=identity, observed_at=NOW, health=ResourceHealth(status="ok"),
        ))
        before = self.plane.producer_event_snapshot()
        restarted = self.make_plane(self.data_dir)
        try:
            response = restarted.producer_replay(before.cursor)
            self.assertIsNotNone(response.resync)
            self.assertEqual("epoch_mismatch", response.reason)
            assert response.resync is not None
            self.assertNotEqual(before.cursor.epoch, response.resync.cursor.epoch)
            self.assertGreaterEqual(response.resync.cursor.sequence, before.cursor.sequence)
            self.assertEqual("never_observed", restarted.producer_snapshot().resources[0].freshness)
            with self.assertRaises(ServerStoreError):
                _ = self.plane.producer_event_snapshot()
        finally:
            restarted.close_worker_store()
            restarted.store.close()

    def test_inventory_and_resource_report_are_one_failure_atomic_event(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        source = SourceConfig("owned", "codex_subscription", "Owned", "worker-1", allowed_models=("allowed",))
        inventory = ModelInventoryReport(worker_id="worker-1", sources=(SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription", observed_at=NOW,
            auth_state="authenticated", runtime_name="codex", runtime_version="1",
            models=(DiscoveredModel("allowed", ("high",)),),
        ),))
        scratch = SourceRegistry()
        scratch.sync_configuration((source,))
        _ = scratch.apply_inventory(inventory)
        identity = scratch.derived_registrations()[0].identity
        observation = ResourceStateSnapshot(schema_version=1, identity=identity, observed_at=NOW,
                                            health=ResourceHealth(status="ok"))
        report = WorkerStateReport(schema_version=1, worker_id="worker-1", reported_at=NOW,
                                   resources=(observation,))
        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(sources=(source,)))  # pyright: ignore[reportPrivateUsage]
            before = self.plane.producer_event_snapshot()
            invalid = replace(report, reported_at=FUTURE, resources=(replace(observation, observed_at=FUTURE),))
            with self.assertRaises(CapacityValidationError):
                _ = self.plane.apply_combined_worker_report(invalid, (inventory,))
            self.assertEqual(before, self.plane.producer_event_snapshot())
            self.assertEqual((), self.plane.producer_snapshot().discoveries)
            _ = self.plane.apply_combined_worker_report(report, (inventory,))
            after = self.plane.producer_event_snapshot()
        self.assertEqual(before.cursor.sequence + 1, after.cursor.sequence)
        self.assertEqual(1, len(self.plane.producer_replay(before.cursor).events))
        self.assertEqual("fresh", self.plane.producer_snapshot().resources[0].freshness)

    def test_snapshot_is_serialized_with_mutation_and_durable_append(self) -> None:
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original_append = self.plane.store.append_producer_event
        snapshots: list[ProducerEventSnapshot] = []

        def paused(epoch: str, payload: str) -> int:
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test timed out")
            return original_append(epoch, payload)

        def write() -> None:
            self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]

        def read() -> None:
            snapshots.append(self.plane.producer_event_snapshot())
            finished.set()

        before = self.plane.producer_event_snapshot()
        with patch.object(self.plane.store, "append_producer_event", side_effect=paused):
            writer = threading.Thread(target=write)
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
        self.assertEqual(before.cursor.sequence + 1, snapshots[0].cursor.sequence)
        self.assertEqual(snapshots[0].payload, self.plane.producer_replay(before.cursor).events[-1].payload)

    def test_concurrent_refreshes_publish_a_contiguous_complete_history(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        identities = tuple(ResourceIdentity(resource_id, "local_app_adapter", "ollama", "local", "local_ungated")
                           for resource_id in ("one", "two"))
        resources = tuple(ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60))
                          for identity in identities)
        self.plane._save_config(ServerConfiguration(resources=resources))  # pyright: ignore[reportPrivateUsage]
        before = self.plane.producer_event_snapshot()
        barrier = threading.Barrier(3)
        failures: list[Exception] = []

        def refresh(identity: ResourceIdentity) -> None:
            try:
                _ = barrier.wait(timeout=5)
                self.plane.apply_resource_observation(ResourceStateSnapshot(
                    schema_version=1, identity=identity, observed_at=NOW, health=ResourceHealth(status="ok"),
                ))
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=refresh, args=(identity,)) for identity in identities]
        for thread in threads:
            thread.start()
        _ = barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        replay = self.plane.producer_replay(before.cursor)
        self.assertIsNone(replay.resync)
        self.assertEqual((before.cursor.sequence + 1, before.cursor.sequence + 2),
                         tuple(event.cursor.sequence for event in replay.events))
        self.assertEqual(self.plane.producer_event_snapshot().payload, replay.events[-1].payload)

    def test_failed_publication_invalidates_cursor_until_restart(self) -> None:
        before = self.plane.producer_event_snapshot()
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
        with patch.object(self.plane.store, "append_producer_event", side_effect=ServerStoreError("synthetic")):
            with self.assertRaises(ServerStoreError):
                self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        with self.assertRaises(ServerStoreError):
            _ = self.plane.producer_replay(before.cursor)
        restarted = self.make_plane(self.data_dir)
        try:
            self.assertIsNotNone(restarted.producer_replay(before.cursor).resync)
        finally:
            restarted.close_worker_store()
            restarted.store.close()

    def test_worker_report_rolls_back_if_event_commit_fails(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("worker-resource", "worker_bridged", "openai", "model", "subscription_included")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), worker_id="worker-1")
        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        before = self.plane.producer_event_snapshot()
        observation = ResourceStateSnapshot(schema_version=1, identity=identity, observed_at=NOW,
                                            health=ResourceHealth(status="ok"))
        report = WorkerStateReport(schema_version=1, worker_id="worker-1", reported_at=NOW,
                                   resources=(observation,))
        with patch.object(self.plane.store, "append_producer_event", side_effect=ServerStoreError("synthetic")):
            with self.assertRaises(ServerStoreError):
                self.plane.apply_worker_report(report)
        self.assertEqual("never_observed", self.plane.producer_snapshot().resources[0].freshness)
        self.assertIsNone(self.plane.current_application().registry.registry_snapshot().entries[0].observation)
        with self.assertRaises(ServerStoreError):
            _ = self.plane.producer_replay(before.cursor)

    def test_invalid_cursor_fails_closed(self) -> None:
        for cursor in (ProducerCursor("bad", 0), ProducerCursor("0" * 32, -1)):
            with self.assertRaises(ServerStoreError):
                _ = self.plane.producer_replay(cursor)
        current = self.plane.producer_event_snapshot()
        future = self.plane.producer_replay(ProducerCursor(current.cursor.epoch, current.cursor.sequence + 1))
        self.assertEqual("cursor_future", future.reason)
        self.assertEqual(current, future.resync)

    def test_new_epoch_between_position_check_and_replay_fences_old_producer(self) -> None:
        cursor = self.plane.producer_event_snapshot().cursor
        read = self.plane.store.read_producer_events

        def fenced_read(epoch: str, after: int, *, owner_epoch: str | None = None,
                        owner_sequence: int | None = None) -> tuple[int, tuple[tuple[int, str], ...], str | None]:
            _ = self.plane.store.start_producer_epoch()
            return read(epoch, after, owner_epoch=owner_epoch, owner_sequence=owner_sequence)

        with patch.object(self.plane.store, "read_producer_events", side_effect=fenced_read):
            with self.assertRaises(ServerStoreError):
                _ = self.plane.producer_replay(cursor)

    def test_waiter_receives_change_without_polling(self) -> None:
        initial = self.plane.producer_event_snapshot()
        ready = threading.Event()
        results: list[object] = []

        def wait() -> None:
            ready.set()
            results.append(self.plane.wait_for_producer_events(initial.cursor, timeout_seconds=5))

        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(ready.wait(timeout=5))
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        replay = results[0]
        self.assertEqual(self.plane.producer_replay(initial.cursor), replay)
        self.assertEqual((), self.plane.wait_for_producer_events(
            self.plane.producer_event_snapshot().cursor, timeout_seconds=0,
        ).events)

    def test_waiter_wakes_at_freshness_boundary_without_polling(self) -> None:
        clock = [NOW]
        self.plane._registry_clock = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=1))
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        self.plane.apply_resource_observation(ResourceStateSnapshot(
            schema_version=1, identity=identity, observed_at=NOW, health=ResourceHealth(status="ok"),
        ))
        before = self.plane.producer_event_snapshot()
        waits: list[float] = []

        def advance(*, timeout: float) -> bool:
            waits.append(timeout)
            clock[0] = "2026-09-16T12:00:02.000Z"
            return True

        with patch.object(self.plane._producer_condition, "wait", side_effect=advance):  # pyright: ignore[reportPrivateUsage]
            replay = self.plane.wait_for_producer_events(before.cursor, timeout_seconds=5)
        self.assertEqual(1, len(waits))
        self.assertAlmostEqual(1.001, waits[0], places=3)
        self.assertEqual(1, len(replay.events))
        document = cast("dict[str, object]", json.loads(replay.events[0].payload))
        self.assertEqual("stale", cast("list[dict[str, object]]", document["resources"])[0]["freshness"])

    def test_worker_connection_and_disconnect_are_replayable(self) -> None:
        worker_side, server_side = MemoryTransport.pair()
        session = self.plane.worker_endpoint.attach_transport(server_side)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        worker = ScriptedWorker(worker_side)
        code = self.plane._worker_identity_store.begin_pairing(label="synthetic")  # pyright: ignore[reportPrivateUsage]
        paired = worker.send_pair(code.pairing_code)
        self.assertIsInstance(paired, PairResultMessage)
        assert isinstance(paired, PairResultMessage)
        session.close(note="synthetic disconnect")
        thread.join(timeout=5)
        source = SourceConfig("owned", "codex_subscription", "Owned", paired.worker_id)
        self.plane._save_config(ServerConfiguration(sources=(source,)))  # pyright: ignore[reportPrivateUsage]
        disconnected = self.plane.producer_event_snapshot()

        worker_side, server_side = MemoryTransport.pair()
        session = self.plane.worker_endpoint.attach_transport(server_side)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        worker = ScriptedWorker(worker_side)
        try:
            self.assertIsInstance(worker.send_hello(paired.worker_id, paired.credential), HelloAckMessage)
            connected = self.plane.producer_event_snapshot()
            self.assertEqual(disconnected.cursor.sequence + 1, connected.cursor.sequence)
            sources = cast("list[dict[str, object]]", json.loads(connected.payload)["sources"])
            self.assertTrue(sources[0]["worker_connected"])
            session.close(note="synthetic disconnect")
            offline = self.plane.producer_event_snapshot()
            self.assertEqual(connected.cursor.sequence + 1, offline.cursor.sequence)
            self.assertEqual(2, len(self.plane.producer_replay(disconnected.cursor).events))
        finally:
            session.close(note="cleanup")
            thread.join(timeout=5)

    def test_worker_protocol_rejects_complete_report_without_partial_inventory(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        worker_side, server_side = MemoryTransport.pair()
        session = self.plane.worker_endpoint.attach_transport(server_side)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        worker = ScriptedWorker(worker_side)
        code = self.plane._worker_identity_store.begin_pairing(label="synthetic")  # pyright: ignore[reportPrivateUsage]
        paired = worker.send_pair(code.pairing_code)
        self.assertIsInstance(paired, PairResultMessage)
        assert isinstance(paired, PairResultMessage)
        source = SourceConfig("owned", "codex_subscription", "Owned", paired.worker_id, allowed_models=("allowed",))
        inventory = ModelInventoryReport(worker_id=paired.worker_id, sources=(SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription", observed_at=NOW,
            auth_state="authenticated", runtime_name="codex", runtime_version="1",
            models=(DiscoveredModel("allowed", ("high",)),),
        ),))
        scratch = SourceRegistry()
        scratch.sync_configuration((source,))
        _ = scratch.apply_inventory(inventory)
        identity = scratch.derived_registrations()[0].identity
        observation = ResourceStateSnapshot(schema_version=1, identity=identity, observed_at=NOW,
                                            health=ResourceHealth(status="ok"))
        report = WorkerStateReport(schema_version=1, worker_id=paired.worker_id, reported_at=NOW,
                                   resources=(observation,))
        try:
            self.plane._save_config(ServerConfiguration(sources=(source,)))  # pyright: ignore[reportPrivateUsage]
            before = self.plane.producer_event_snapshot()
            invalid = replace(report, reported_at=FUTURE, resources=(replace(observation, observed_at=FUTURE),))
            rejected = worker.send_state_report(invalid.to_dict(), inventories=(inventory.to_dict(),))
            self.assertIsInstance(rejected, ErrorMessage)
            self.assertEqual(before, self.plane.producer_event_snapshot())
            self.assertEqual((), self.plane.producer_snapshot().discoveries)
            accepted = worker.send_state_report(report.to_dict(), inventories=(inventory.to_dict(),))
            self.assertIsInstance(accepted, StateReportAckMessage)
            after = self.plane.producer_event_snapshot()
            self.assertEqual(before.cursor.sequence + 1, after.cursor.sequence)
            self.assertEqual("fresh", self.plane.producer_snapshot().resources[0].freshness)
        finally:
            session.close(note="cleanup")
            thread.join(timeout=5)
