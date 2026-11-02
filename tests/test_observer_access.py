"""Observer role isolation and versioned HTTP projection tests."""

from __future__ import annotations

import json
import http.client
import os
import sqlite3
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast, override
from unittest import TestCase
from unittest.mock import patch

from scarcity_router.control_api import ControlPlane, ProducerEvent, ProducerEventSnapshot, ProducerReplay
from scarcity_router.gateway_contracts import hash_client_key
from scarcity_router.capacity import CapacityWindow
from scarcity_router.model_inventory import DiscoveredModel, ModelInventoryReport, SourceInventory
from scarcity_router.resource_state import QuotaFact, ResourceHealth, ResourceIdentity, ResourceRegistration, ResourceStateSnapshot
from scarcity_router.routing_core import ClientAuthorization
from scarcity_router.server_config import ResourceConfig, ServerConfiguration, SourceConfig
from scarcity_router.server_store import MIGRATIONS, STORE_FILE_NAME, ServerStore, ServerStoreError
from scarcity_router.worker_identity_store import WorkerIdentityError
from tests.server_fixtures import ServerHarness


class ObserverAccessTests(ServerHarness):
    plane: ControlPlane
    observer_id: str = ""
    observer_key: str = ""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()
        issued = self.plane.service_issue_client_key({"label": "synthetic observer", "role": "observer"})
        self.observer_id = cast(str, issued["client_id"])
        self.observer_key = cast(str, issued["api_key"])

    def observer(self, method: str, path: str, payload: object | None = None) -> tuple[int, object]:
        status, body, _ = self.exchange(method, path, payload, headers={"Authorization": f"Bearer {self.observer_key}"}, with_session=False)
        return status, body

    def test_roles_are_hash_only_isolated_and_individually_revocable(self) -> None:
        records = {record.client_id: record for record in self.plane.store.list_client_keys()}
        self.assertEqual("observer", records[self.observer_id].role)
        self.assertEqual(hash_client_key(self.observer_key), records[self.observer_id].key_hash)
        self.assertNotEqual(self.observer_key, records[self.observer_id].key_hash)
        self.assertNotIn(self.observer_id, self.plane.store.active_client_key_hashes())
        self.assertIn(self.client_id, self.plane.store.active_client_key_hashes())
        self.assertIn(self.observer_id, self.plane.store.active_client_key_hashes(role="observer"))
        pairing = self.plane._worker_identity_store.begin_pairing(label="synthetic")  # pyright: ignore[reportPrivateUsage]
        worker_id, worker_credential = self.plane._worker_identity_store.redeem_pairing_code(pairing.pairing_code)  # pyright: ignore[reportPrivateUsage]
        self.plane._worker_identity_store.authenticate(worker_id, worker_credential)  # pyright: ignore[reportPrivateUsage]
        with self.assertRaises(WorkerIdentityError):
            self.plane._worker_identity_store.authenticate(worker_id, self.observer_key)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(200, self.observer("GET", "/observer/v1/snapshot")[0])
        denied: tuple[tuple[str, str, object], ...] = (
            ("GET", "/v1/models", None), ("POST", "/v1/chat/completions", {}),
            ("GET", "/control/clients", None), ("POST", "/control/workers", {}),
            ("GET", "/v1/status", None),
        )
        for method, path, payload in denied:
            with self.subTest(path=path):
                self.assertIn(self.observer(method, path, payload)[0], (401, 403))
        status, _, _ = self.exchange("GET", "/observer/v1/snapshot", headers={"Authorization": f"Bearer {self.client_key}"}, with_session=False)
        self.assertEqual(401, status)
        status, _, _ = self.exchange("GET", "/v1/models", headers={"Authorization": f"Bearer {self.client_key}"}, with_session=False)
        self.assertEqual(200, status)
        snapshot = cast("dict[str, object]", self.observer("GET", "/observer/v1/snapshot")[1])
        self.plane.service_revoke_client_key(self.observer_id)
        self.assertEqual(401, self.observer("GET", "/observer/v1/snapshot")[0])
        self.assertEqual(401, self.observer("POST", "/observer/v1/replay", {"cursor": snapshot["cursor"]})[0])
        self.assertEqual(200, self.exchange("GET", "/v1/models", headers={"Authorization": f"Bearer {self.client_key}"}, with_session=False)[0])

    def test_snapshot_replay_and_redaction_share_one_projection(self) -> None:
        status, raw = self.observer("GET", "/observer/v1/snapshot")
        self.assertEqual(200, status)
        initial = cast("dict[str, object]", raw)
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated", quota_pool_ids=("private-pool",))
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        status, raw = self.observer("POST", "/observer/v1/replay", {"cursor": initial["cursor"]})
        self.assertEqual(200, status)
        replay = cast("dict[str, object]", raw)
        events = cast("list[dict[str, object]]", replay["events"])
        self.assertEqual(1, len(events))
        status, raw = self.observer("GET", "/observer/v1/snapshot")
        self.assertEqual(200, status)
        current = cast("dict[str, object]", raw)
        self.assertEqual(current["snapshot"], events[-1]["snapshot"])
        self.assertNotIn("private-pool", json.dumps(current))
        self.assertNotIn("quota_pool_ids", json.dumps(replay))
        self.assertNotIn(self.observer_key, json.dumps(replay))

    def test_stored_private_fields_are_never_returned_for_snapshot_or_replay(self) -> None:
        cut = self.plane.producer_event_snapshot()
        document = cast("dict[str, object]", json.loads(cut.payload))
        document["unapproved"] = "SYNTHETIC-PRIVATE-MARKER"
        document["resources"] = [{
            "identity": {"resource_id": "local", "channel": "local_app_adapter", "provider": "ollama", "model": "local", "variant": None, "entitlement": "local_ungated", "quota_pool_ids": ["SYNTHETIC-PRIVATE-MARKER"]},
            "origin": "configured", "enabled": True, "worker_connected": None, "observed_at": None,
            "freshness": "never_observed", "health": None, "availability": "unknown", "quota_facts": [],
            "raw_provider_status": "SYNTHETIC-PRIVATE-MARKER",
        }]
        payload = json.dumps(document)
        snapshot = ProducerEventSnapshot(cut.cursor, payload)
        event = ProducerEvent(cut.cursor, payload)
        replay = ProducerReplay(cut.cursor, (event,), None, None, True)
        with patch.object(self.plane, "producer_event_snapshot", return_value=snapshot), patch.object(self.plane, "producer_replay", return_value=replay):
            for method, path, body in (("GET", "/observer/v1/snapshot", None), ("POST", "/observer/v1/replay", {"cursor": asdict(cut.cursor)})):
                status, response = self.observer(method, path, body)
                self.assertEqual(200, status)
                self.assertNotIn("SYNTHETIC-PRIVATE-MARKER", json.dumps(response))

    def test_gap_restart_version_and_malformed_requests(self) -> None:
        initial = cast("dict[str, object]", self.observer("GET", "/observer/v1/snapshot")[1])
        cursor = cast("dict[str, object]", initial["cursor"])
        for altered, expected in (({**cursor, "schema_version": 2}, "schema_mismatch"),
                                  ({**cursor, "epoch": "0" * 32}, "epoch_mismatch"),
                                  ({**cursor, "sequence": cast(int, cursor["sequence"]) + 1}, "cursor_future")):
            status, raw = self.observer("POST", "/observer/v1/replay", {"cursor": altered})
            self.assertEqual(200, status)
            response = cast("dict[str, object]", raw)
            self.assertEqual(expected, response["resync_reason"])
            self.assertIsNotNone(response["resync"])
        with patch("scarcity_router.server_store._PRODUCER_EVENT_LIMIT", 1):
            for name in ("one", "two"):
                identity = ResourceIdentity(name, "local_app_adapter", "ollama", "local", "local_ungated")
                resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
                self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        status, raw = self.observer("POST", "/observer/v1/replay", {"cursor": cursor})
        self.assertEqual(200, status)
        self.assertEqual("cursor_expired", cast("dict[str, object]", raw)["resync_reason"])
        self.assertEqual(405, self.observer("POST", "/observer/v1/snapshot", {})[0])
        self.assertEqual(405, self.observer("GET", "/observer/v1/replay")[0])
        self.assertEqual(400, self.observer("POST", "/observer/v1/replay", {"cursor": {"epoch": "bad"}})[0])
        self.assertEqual(400, self.observer("GET", "/observer/v1/snapshot?credential=ignored")[0])
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("POST", "/observer/v1/replay")
            connection.putheader("Authorization", f"Bearer {self.observer_key}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "1048577")
            connection.endheaders()
            reply = connection.getresponse()
            self.assertEqual(400, reply.status)
            _ = reply.read()
        finally:
            connection.close()

    def test_restart_requires_resync_and_preserves_observer_access(self) -> None:
        before = cast("dict[str, object]", self.observer("GET", "/observer/v1/snapshot")[1])
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.plane.close_worker_store()
        self.plane.store.close()
        self.plane = self.make_plane(self.data_dir)
        self._start_server()
        status, raw = self.observer("POST", "/observer/v1/replay", {"cursor": before["cursor"]})
        self.assertEqual(200, status)
        self.assertEqual("epoch_mismatch", cast("dict[str, object]", raw)["resync_reason"])
        self.assertIsNotNone(cast("dict[str, object]", raw)["resync"])

    def test_missing_history_sequence_requires_snapshot_resync(self) -> None:
        before = cast("dict[str, object]", self.observer("GET", "/observer/v1/snapshot")[1])
        cursor = cast("dict[str, object]", before["cursor"])
        for name in ("one", "two"):
            identity = ResourceIdentity(name, "local_app_adapter", "ollama", "local", "local_ungated")
            resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
            self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        with sqlite3.connect(self.plane.store.path) as connection:
            _ = connection.execute("DELETE FROM producer_events WHERE sequence = ?", (cast(int, cursor["sequence"]) + 1,))
        status, raw = self.observer("POST", "/observer/v1/replay", {"cursor": cursor})
        self.assertEqual(200, status)
        response = cast("dict[str, object]", raw)
        self.assertEqual("history_gap", response["resync_reason"])
        self.assertIsNotNone(response["resync"])

    def test_reads_do_not_trigger_provider_collection_or_refresh(self) -> None:
        with patch.object(self.plane, "prepare_execution_admission", side_effect=AssertionError("unexpected refresh")):
            status, raw = self.observer("GET", "/observer/v1/snapshot")
            self.assertEqual(200, status)
            cursor = cast("dict[str, object]", raw)["cursor"]
            self.assertEqual(200, self.observer("POST", "/observer/v1/replay", {"cursor": cursor})[0])

    def test_observed_usage_is_normalized_and_private_scope_is_excluded(self) -> None:
        now = "2026-09-16T12:00:00.000Z"
        self.plane._registry_clock = lambda: now  # pyright: ignore[reportPrivateUsage]
        identity = ResourceIdentity("local", "local_app_adapter", "ollama", "local", "local_ungated")
        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60))
        self.plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
        fact = QuotaFact(observation_class="direct_observation", window=CapacityWindow.from_dict({
            "resource": "tokens", "kind": "five_hour", "duration_seconds": 18000,
            "used_percent": 6, "remaining_percent": 94, "resets_at": "2026-09-16T13:00:00.000Z",
            "scope_id": "private-scope", "provider_metadata": {"window_id": "private-window"},
        }))
        self.plane.apply_resource_observation(ResourceStateSnapshot(
            schema_version=1, identity=identity, observed_at=now, health=ResourceHealth(status="ok"),
            quota_facts=(fact,),
        ))
        status, raw = self.observer("GET", "/observer/v1/snapshot")
        self.assertEqual(200, status)
        snapshot = cast("dict[str, object]", cast("dict[str, object]", raw)["snapshot"])
        resources = cast("list[dict[str, object]]", snapshot["resources"])
        quotas = cast("list[dict[str, object]]", resources[0]["quota_facts"])
        self.assertEqual("direct_observation", quotas[0]["observation_class"])
        self.assertEqual(6, cast("dict[str, object]", quotas[0]["window"])["used_percent"])
        self.assertNotIn("private-scope", json.dumps(raw))
        self.assertNotIn("private-window", json.dumps(raw))

    def test_restricted_discovery_is_exposed_without_source_auth_details(self) -> None:
        source = SourceConfig("owned", "codex_subscription", "Synthetic", "worker-1")
        report = ModelInventoryReport(worker_id="worker-1", sources=(SourceInventory(
            source_id="owned", adapter_id="codex:owned", kind="codex_subscription",
            observed_at="2026-09-16T12:00:00.000Z", auth_state="authenticated",
            runtime_name="codex", runtime_version="1",
            models=(DiscoveredModel("restricted-model", ("high",)),),
        ),))
        with patch.object(self.plane, "_worker_id_exists", return_value=True):
            self.plane._save_config(ServerConfiguration(sources=(source,)))  # pyright: ignore[reportPrivateUsage]
            self.plane._apply_source_inventory(report)  # pyright: ignore[reportPrivateUsage]
        status, raw = self.observer("GET", "/observer/v1/snapshot")
        self.assertEqual(200, status)
        snapshot = cast("dict[str, object]", cast("dict[str, object]", raw)["snapshot"])
        discoveries = cast("list[dict[str, object]]", snapshot["discoveries"])
        self.assertEqual("model_not_allowed", discoveries[0]["reason"])
        self.assertNotIn("authentication", json.dumps(snapshot))
        self.assertNotIn("runtime_version", json.dumps(snapshot))

    def test_faulted_producer_returns_safe_service_unavailable(self) -> None:
        with patch.object(self.plane, "producer_event_snapshot", side_effect=ServerStoreError("SYNTHETIC-PRIVATE")):
            status, raw = self.observer("GET", "/observer/v1/snapshot")
        self.assertEqual(503, status)
        self.assertNotIn("SYNTHETIC-PRIVATE", json.dumps(raw))

    def test_duplicate_authorization_headers_are_rejected(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("GET", "/observer/v1/snapshot")
            connection.putheader("Authorization", f"Bearer {self.observer_key}")
            connection.putheader("Authorization", f"Bearer {self.observer_key}")
            connection.endheaders()
            reply = connection.getresponse()
            self.assertEqual(401, reply.status)
            _ = reply.read()
        finally:
            connection.close()

    def test_observer_role_cannot_accept_inference_grant(self) -> None:
        status, _ = self.admin_post("/control/clients", {"label": "observer", "role": "observer", "authorization": {}})
        self.assertEqual(400, status)

    def test_observer_never_displays_preexisting_inference_grant(self) -> None:
        config = self.plane.configuration
        grants = dict(config.client_authorizations)
        grants["reserved-observer"] = ClientAuthorization()
        self.plane._save_config(replace(config, client_authorizations=grants))  # pyright: ignore[reportPrivateUsage]
        _ = self.plane.service_issue_client_key({"client_id": "reserved-observer", "label": "observer", "role": "observer"})
        record = next(item for item in self.plane.clients_view() if item["client_id"] == "reserved-observer")
        self.assertEqual("observer", record["role"])
        self.assertIsNone(record["authorization"])


class ObserverMigrationTests(TestCase):
    def test_v5_existing_keys_remain_inference_after_role_migration(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / STORE_FILE_NAME
            connection = sqlite3.connect(path)
            try:
                for version, statements in MIGRATIONS:
                    if version > 5:
                        break
                    for statement in statements:
                        _ = connection.execute(statement)
                    _ = connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, "2026-09-16T12:00:00.000Z"))
                _ = connection.execute("INSERT INTO client_keys(client_id, key_hash, label, created_at, revoked_at) VALUES (?, ?, ?, ?, NULL)",
                                       ("existing", "0" * 64, "existing client", "2026-09-16T12:00:00.000Z"))
                connection.commit()
            finally:
                connection.close()
            os.chmod(path, 0o600)
            store = ServerStore.open(Path(root))
            try:
                self.assertEqual("inference", store.list_client_keys()[0].role)
                self.assertEqual({"existing": "0" * 64}, store.active_client_key_hashes())
                self.assertEqual({}, store.active_client_key_hashes(role="observer"))
            finally:
                store.close()
