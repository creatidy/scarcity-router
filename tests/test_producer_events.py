"""Internal event/snapshot, rollback and restart acceptance tests."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from typing import cast
from unittest.mock import patch

from scarcity_router.control_api import ProducerCursor, ProducerEvent, ProducerEventSnapshot
from scarcity_router.capacity import CapacityDiagnostic, CapacityWindow
from scarcity_router.errors import CapacityValidationError
from scarcity_router.execution_sources import SourceRegistry
from scarcity_router.gateway_contracts import GatewayError
from scarcity_router.model_inventory import DiscoveredModel, ModelInventoryReport, SourceInventory
from scarcity_router.resource_state import QuotaFact, ResourceHealth, ResourceIdentity, ResourceRegistration, ResourceStateSnapshot, WorkerStateReport
from scarcity_router.server_config import ResourceConfig, ServerConfiguration, SourceConfig
from scarcity_router.server_store import ServerStoreError
from scarcity_router.worker_endpoint import AttemptOutcome
from tests.server_fixtures import ServerHarness
from tests.worker_fixtures import MemoryTransport, ScriptedWorker
from scarcity_router.worker_protocol import ErrorMessage, ExecuteMessage, HelloAckMessage, PairResultMessage, StateReportAckMessage
from tests.test_worker_endpoint import _worker_call  # pyright: ignore[reportPrivateUsage]

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
        replay = self.plane.producer_replay(initial.cursor)
        self.assertIsNone(replay.resync)
        self.assertEqual((registered.cursor.sequence, observed.cursor.sequence),
                         tuple(event.cursor.sequence for event in replay.events))
        self.assertEqual(observed.payload, replay.events[-1].payload)
        for instant in (NEXT, "2026-09-16T12:00:02.000Z", "2026-09-16T12:00:03.000Z"):
            clock[0] = instant
            self.plane.apply_resource_observation(replace(observation, observed_at=instant))
        refreshed = self.plane.producer_event_snapshot()
        self.assertEqual(observed.cursor.sequence, refreshed.cursor.sequence)
        self.assertNotEqual(observed.cursor.provenance, refreshed.cursor.provenance)
        refreshed_doc = cast("dict[str, object]", json.loads(refreshed.payload))
        self.assertEqual("2026-09-16T12:00:03.000Z", cast("list[dict[str, object]]", refreshed_doc["resources"])[0]["observed_at"])
        unchanged = self.plane.producer_replay(observed.cursor)
        self.assertIsNone(unchanged.resync)
        self.assertFalse(unchanged.provenance_current)
        self.assertEqual((), unchanged.events)
        retained = self.plane.producer_replay(initial.cursor)
        self.assertEqual(2, len(retained.events))
        self.assertFalse(retained.provenance_current)
        self.assertIsNone(retained.resync)
        self.assertEqual((), self.plane.producer_replay(refreshed.cursor).events)
        self.assertTrue(self.plane.producer_replay(refreshed.cursor).provenance_current)
        clock[0] = FUTURE
        stale = self.plane.producer_event_snapshot()
        self.assertEqual(observed.cursor.sequence + 1, stale.cursor.sequence)
        document = cast("dict[str, object]", json.loads(stale.payload))
        resources = cast("list[dict[str, object]]", document["resources"])
        self.assertEqual("stale", resources[0]["freshness"])
        replay_after_stale = self.plane.producer_replay(refreshed.cursor)
        self.assertIsNone(replay_after_stale.resync)
        self.assertEqual(stale.payload, replay_after_stale.events[-1].payload)
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

    def test_revocation_remains_effective_when_event_journal_is_faulted(self) -> None:
        issued = self.plane.service_issue_client_key({"label": "synthetic"})
        client_id = cast(str, issued["client_id"])
        key = cast(str, issued["api_key"])
        admitted = self.plane.current_application()
        assert admitted.client_key_directory is not None
        self.assertEqual(client_id, admitted.client_key_directory.authenticate(key))
        status, _, _ = self.exchange("GET", "/v1/models", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(200, status)
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
        with patch.object(self.plane.store, "append_producer_event", side_effect=ServerStoreError("synthetic")):
            self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        self.plane.service_revoke_client_key(client_id)
        current = self.plane.current_application().client_key_directory
        assert current is not None
        self.assertIsNone(current.authenticate(key))
        status, _, _ = self.exchange("GET", "/v1/models", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(401, status)
        assert admitted.authority_source is not None
        with self.assertRaises(GatewayError):
            _ = admitted.authority_source(client_id)
        with self.assertRaises(ServerStoreError):
            _ = self.plane.producer_event_snapshot()

    def test_worker_storage_failure_closes_session_and_interrupts_attempt(self) -> None:
        for fault_before_report in (False, True):
            with self.subTest(already_faulted=fault_before_report):
                plane = self.make_plane(self.data_dir / ("prior" if fault_before_report else "during"))
                worker_side, server_side = MemoryTransport.pair()
                session = plane.worker_endpoint.attach_transport(server_side)
                thread = threading.Thread(target=session.run, daemon=True)
                thread.start()
                worker = ScriptedWorker(worker_side)
                try:
                    code = plane._worker_identity_store.begin_pairing(label="synthetic")  # pyright: ignore[reportPrivateUsage]
                    paired = worker.send_pair(code.pairing_code)
                    assert isinstance(paired, PairResultMessage)
                    identity = ResourceIdentity("worker-resource", "worker_bridged", "openai", "model", "subscription_included")
                    resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), worker_id=paired.worker_id)
                    plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
                    plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
                    pending = session.submit_execute(ExecuteMessage(
                        request_id="chatcmpl-synthetic", attempt_id="wa-synthetic",
                        adapter_id="synthetic", deadline="2030-01-01T00:00:00.000Z", call=_worker_call(),
                    ))
                    _ = worker.next_execute()
                    if fault_before_report:
                        extra = ResourceConfig(ResourceRegistration(
                            ResourceIdentity("other", "local_app_adapter", "ollama", "local", "local_ungated"),
                            freshness_ttl_seconds=60,
                        ), enabled=False)
                        with patch.object(plane.store, "append_producer_event", side_effect=ServerStoreError("synthetic")):
                            plane._save_config(ServerConfiguration(resources=(extra, resource)))  # pyright: ignore[reportPrivateUsage]
                    report = WorkerStateReport(schema_version=1, worker_id=paired.worker_id, reported_at=NOW, resources=(
                        ResourceStateSnapshot(schema_version=1, identity=identity, observed_at=NOW,
                                              health=ResourceHealth(status="ok")),
                    ))
                    with patch.object(plane.store, "append_producer_event", side_effect=ServerStoreError("synthetic")):
                        answer = worker.send_state_report(report.to_dict())
                    self.assertIsInstance(answer, ErrorMessage)
                    assert isinstance(answer, ErrorMessage)
                    self.assertTrue(answer.fatal)
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual((), plane.worker_endpoint.connected_worker_ids())
                    kind, outcome = pending.take(5)
                    self.assertEqual("outcome", kind)
                    self.assertEqual("interrupted", cast(AttemptOutcome, outcome).status)
                finally:
                    session.close(note="cleanup")
                    thread.join(timeout=5)
                    plane.close_worker_store()
                    plane.store.close()

    def test_invalid_cursor_fails_closed(self) -> None:
        for cursor in (ProducerCursor("bad", 0, "0" * 64), ProducerCursor("0" * 32, -1, "0" * 64),
                       ProducerCursor("0" * 32, 0, "bad")):
            with self.assertRaises(ServerStoreError):
                _ = self.plane.producer_replay(cursor)
        current = self.plane.producer_event_snapshot()
        future = self.plane.producer_replay(ProducerCursor(
            current.cursor.epoch, current.cursor.sequence + 1, current.cursor.provenance,
        ))
        self.assertEqual("cursor_future", future.reason)
        self.assertEqual(current, future.resync)
        incompatible = self.plane.producer_replay(replace(current.cursor, schema_version=2))
        self.assertEqual("schema_mismatch", incompatible.reason)
        self.assertEqual(current, incompatible.resync)

    def test_versioned_consumer_fixture_handles_duplicates_and_reordering(self) -> None:
        initial = self.plane.producer_event_snapshot()
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        self.plane._save_config(ServerConfiguration(resources=(replace(resource, enabled=True),)))  # pyright: ignore[reportPrivateUsage]
        replay = self.plane.producer_replay(initial.cursor)
        self.assertEqual(2, len(replay.events))
        self.assertEqual(replay.events, self.plane.producer_replay(initial.cursor).events)
        cursor = initial.cursor
        document = cast("dict[str, object]", json.loads(initial.payload))
        self.assertEqual(1, document["schema_version"])
        self.assertEqual("execution_gateway", document["mode"])

        def deliver(event: ProducerEvent) -> bool:
            nonlocal cursor, document
            if event.cursor.epoch != cursor.epoch:
                return False
            if event.cursor.sequence <= cursor.sequence:
                return True
            if event.cursor.sequence != cursor.sequence + 1:
                return False
            document = cast("dict[str, object]", json.loads(event.payload))
            cursor = event.cursor
            return True

        before_document = document.copy()
        reordered = replay.events[1]
        if not deliver(reordered):
            self.assertEqual(initial.cursor, cursor)
            self.assertEqual(before_document, document)
            recovered = self.plane.producer_replay(cursor)
        else:
            self.fail("out-of-order event was accepted")
        self.assertIsNone(recovered.resync)
        self.assertEqual(replay.events, recovered.events)
        self.assertTrue(deliver(recovered.events[0]))
        after_first, first_cursor = document.copy(), cursor
        self.assertTrue(deliver(recovered.events[0]))
        self.assertEqual(first_cursor, cursor)
        self.assertEqual(after_first, document)
        self.assertTrue(deliver(recovered.events[1]))
        after_recovery, recovered_cursor = document.copy(), cursor
        self.assertTrue(deliver(reordered))
        self.assertEqual(recovered_cursor, cursor)
        self.assertEqual(after_recovery, document)
        current = self.plane.producer_event_snapshot()
        self.assertEqual(current.cursor, cursor)
        self.assertEqual(json.loads(current.payload), document)

    def test_consumer_reconstructs_partial_failure_usage_and_source_inventory(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        identities = tuple(ResourceIdentity(resource_id, "local_app_adapter", "ollama", resource_id, "local_ungated")
                           for resource_id in ("healthy", "failed", "unknown", "disabled"))
        resources = tuple(ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60),
                                         enabled=identity.resource_id != "disabled") for identity in identities)
        source = SourceConfig("owned", "codex_subscription", "Owned", "worker-1", allowed_models=("allowed",))
        inventory = ModelInventoryReport(worker_id="worker-1", sources=(SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription", observed_at=NOW,
            auth_state="authenticated", runtime_name="codex", runtime_version="1",
            models=(DiscoveredModel("allowed", ("high",)),),
        ),))
        baseline = self.plane.producer_event_snapshot()
        fact = QuotaFact(observation_class="provider_telemetry", window=CapacityWindow.from_dict({
            "resource": "tokens", "kind": "five_hour", "duration_seconds": 18000,
            "used_percent": 6, "remaining_percent": 94, "resets_at": FUTURE,
        }))
        healthy = ResourceStateSnapshot(schema_version=1, identity=identities[0], observed_at=NOW,
                                        health=ResourceHealth(status="ok"), quota_facts=(fact,))
        failed = ResourceStateSnapshot(
            schema_version=1, identity=identities[1], observed_at=NOW,
            health=ResourceHealth(status="unavailable", diagnostics=(CapacityDiagnostic("source_unavailable"),)),
        )
        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(resources=resources, sources=(source,)))  # pyright: ignore[reportPrivateUsage]
            self.plane._apply_source_inventory(inventory)  # pyright: ignore[reportPrivateUsage]
        self.plane.apply_resource_observation(healthy)
        self.plane.apply_resource_observation(failed)
        replay = self.plane.producer_replay(baseline.cursor)
        self.assertIsNone(replay.resync)
        self.assertTrue(replay.provenance_current)
        self.assertGreaterEqual(len(replay.events), 4)
        for previous, event in zip((baseline.cursor.sequence, *(e.cursor.sequence for e in replay.events[:-1])), replay.events):
            self.assertEqual(previous + 1, event.cursor.sequence)
        document = cast("dict[str, object]", json.loads(replay.events[-1].payload))
        self.assertEqual(1, document["schema_version"])
        self.assertEqual("execution_gateway", document["mode"])
        entries = cast("list[dict[str, object]]", document["resources"])
        by_id = {cast("dict[str, object]", entry["identity"])["resource_id"]: entry for entry in entries}
        self.assertEqual("ok", by_id["healthy"]["health"])
        facts = cast("list[dict[str, object]]", by_id["healthy"]["quota_facts"])
        self.assertEqual("provider_telemetry", facts[0]["observation_class"])
        self.assertEqual(6, cast("dict[str, object]", facts[0]["window"])["used_percent"])
        self.assertEqual("unavailable", by_id["failed"]["health"])
        self.assertEqual("unavailable", by_id["failed"]["availability"])
        self.assertEqual("unknown", by_id["unknown"]["availability"])
        self.assertEqual("disabled", by_id["disabled"]["availability"])
        self.assertEqual("source", by_id["owned:allowed:high"]["origin"])
        self.assertEqual(False, by_id["owned:allowed:high"]["worker_connected"])
        sources = cast("list[dict[str, object]]", document["sources"])
        self.assertEqual("unknown", sources[0]["inventory_freshness"])
        discoveries = cast("list[dict[str, object]]", document["discoveries"])
        self.assertEqual("routable", discoveries[0]["state"])
        self.assertEqual(json.loads(self.plane.producer_event_snapshot().payload), document)

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

    def test_waiter_ignores_timestamp_only_refresh_until_semantic_change(self) -> None:
        clock = [NOW]
        self.plane._registry_clock = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60))
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        observation = ResourceStateSnapshot(schema_version=1, identity=identity, observed_at=NOW,
                                            health=ResourceHealth(status="ok"))
        self.plane.apply_resource_observation(observation)
        initial = self.plane.producer_event_snapshot()
        waiting, finished = threading.Event(), threading.Event()
        responses: list[object] = []
        condition = self.plane._producer_condition  # pyright: ignore[reportPrivateUsage]
        original_wait = condition.wait

        def signaled_wait(timeout: float | None = None) -> bool:
            waiting.set()
            return original_wait(timeout)

        def wait() -> None:
            responses.append(self.plane.wait_for_producer_events(initial.cursor, timeout_seconds=5))
            finished.set()

        with patch.object(condition, "wait", side_effect=signaled_wait):
            thread = threading.Thread(target=wait)
            thread.start()
            self.assertTrue(waiting.wait(timeout=5))
            clock[0] = NEXT
            self.plane.apply_resource_observation(replace(observation, observed_at=NEXT))
            self.assertFalse(finished.wait(timeout=0.05))
            self.plane.apply_resource_observation(replace(
                observation, observed_at=NEXT,
                health=ResourceHealth(status="unavailable", diagnostics=(CapacityDiagnostic("source_unavailable"),)),
            ))
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        replay = self.plane.producer_replay(initial.cursor)
        self.assertEqual(replay, responses[0])
        self.assertIsNone(replay.reason)
        self.assertTrue(replay.provenance_current)
        self.assertEqual(initial.cursor.sequence + 1, replay.cursor.sequence)

    def test_past_reset_is_not_a_repeated_wait_deadline(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60))
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        fact = QuotaFact(observation_class="provider_telemetry", window=CapacityWindow.from_dict({
            "resource": "tokens", "kind": "five_hour", "duration_seconds": 18000,
            "used_percent": 6, "remaining_percent": 94, "resets_at": "2026-09-16T11:59:00.000Z",
        }))
        self.plane.apply_resource_observation(ResourceStateSnapshot(
            schema_version=1, identity=identity, observed_at=NOW, health=ResourceHealth(status="ok"),
            quota_facts=(fact,),
        ))
        registry = self.plane.current_application().registry
        actual = registry.registry_snapshot()
        self.assertEqual("stale", actual.entries[0].freshness)
        # Exercise the deadline guard even if a future registry permits this state.
        synthetic = replace(actual, entries=(replace(actual.entries[0], freshness="fresh"),))
        with patch.object(registry, "registry_snapshot", return_value=synthetic):
            self.assertAlmostEqual(60.001, self.plane._next_producer_expiry() or 0, places=3)  # pyright: ignore[reportPrivateUsage]

    def test_boundary_crossing_during_wait_setup_is_not_missed(self) -> None:
        clock = [NOW]
        self.plane._registry_clock = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=1))
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        self.plane.apply_resource_observation(ResourceStateSnapshot(
            schema_version=1, identity=identity, observed_at=NOW, health=ResourceHealth(status="ok"),
        ))
        initial = self.plane.producer_event_snapshot()

        def cross() -> None:
            clock[0] = "2026-09-16T12:00:02.000Z"
            return None

        with patch.object(self.plane, "_next_producer_expiry", side_effect=cross):
            replay = self.plane.wait_for_producer_events(initial.cursor, timeout_seconds=5)
        self.assertEqual(1, len(replay.events))

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

    def test_binding_publication_is_serialized_with_reassignment(self) -> None:
        self.plane._registry_clock = lambda: NOW  # pyright: ignore[reportPrivateUsage]
        sessions: list[tuple[object, threading.Thread]] = []

        def pair() -> tuple[ScriptedWorker, PairResultMessage, object]:
            worker_side, server_side = MemoryTransport.pair()
            session = self.plane.worker_endpoint.attach_transport(server_side)
            thread = threading.Thread(target=session.run, daemon=True)
            thread.start()
            sessions.append((session, thread))
            worker = ScriptedWorker(worker_side)
            code = self.plane._worker_identity_store.begin_pairing(label="synthetic")  # pyright: ignore[reportPrivateUsage]
            paired = worker.send_pair(code.pairing_code)
            assert isinstance(paired, PairResultMessage)
            return worker, paired, session

        worker_a, pair_a, _ = pair()
        worker_b, pair_b, session_b = pair()
        identity = ResourceIdentity("worker-resource", "worker_bridged", "openai", "model", "subscription_included")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), worker_id=pair_a.worker_id)
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        observation = ResourceStateSnapshot(schema_version=1, identity=identity, observed_at=NOW,
                                            health=ResourceHealth(status="ok"))
        report_a = WorkerStateReport(schema_version=1, worker_id=pair_a.worker_id, reported_at=NOW,
                                     resources=(observation,))
        report_b = replace(report_a, worker_id=pair_b.worker_id)
        accepted, release, changed = threading.Event(), threading.Event(), threading.Event()
        answers: list[object] = []
        original = self.plane.apply_combined_worker_report

        def paused(report: WorkerStateReport, inventories: tuple[ModelInventoryReport, ...]) -> WorkerStateReport:
            result = original(report, inventories)
            accepted.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test timed out")
            return result

        def send_a() -> None:
            answers.append(worker_a.send_state_report(report_a.to_dict()))

        def reassign() -> None:
            self.plane._save_config(ServerConfiguration(  # pyright: ignore[reportPrivateUsage]
                resources=(replace(resource, worker_id=pair_b.worker_id),),
            ))
            changed.set()

        try:
            with patch.object(self.plane.worker_endpoint, "atomic_report_sink", side_effect=paused):
                report_thread = threading.Thread(target=send_a)
                report_thread.start()
                self.assertTrue(accepted.wait(timeout=5))
                config_thread = threading.Thread(target=reassign)
                config_thread.start()
                try:
                    self.assertFalse(changed.wait(timeout=0.05))
                finally:
                    release.set()
                    report_thread.join(timeout=5)
                    config_thread.join(timeout=5)
            self.assertTrue(changed.is_set())
            self.assertIsInstance(answers[0], StateReportAckMessage)
            self.assertIsInstance(worker_b.send_state_report(report_b.to_dict()), StateReportAckMessage)
            self.assertIs(session_b, self.plane.worker_endpoint.session_for_resource(identity.resource_id))
        finally:
            from scarcity_router.worker_endpoint import WorkerSession
            for session, thread in sessions:
                cast(WorkerSession, session).close(note="cleanup")
                thread.join(timeout=5)
