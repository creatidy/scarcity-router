"""Durable-store tests (M09): schema versioning, migrations, retention,
permissioned storage and crash/restart semantics (issue #94, D-041/D-044).

Deterministic: injected timestamps, temporary directories, no network.
"""

from __future__ import annotations

import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from scarcity_router.server_store import (
    MIGRATIONS,
    STORE_SCHEMA_VERSION,
    ServerStore,
    ServerStoreError,
    canonical_store_path,
)

T0 = "2026-09-16T12:00:00.000Z"
T1 = "2026-09-16T12:01:00.000Z"


class StoreLifecycleTests(unittest.TestCase):
    def test_open_creates_owner_only_directory_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            self.addCleanup(store.close)
            dir_mode = stat.S_IMODE(data_dir.stat().st_mode)
            file_mode = stat.S_IMODE(store.path.stat().st_mode)
            self.assertEqual(0o700, dir_mode & 0o777)
            self.assertEqual(0o600, file_mode & 0o777)

    def test_schema_version_is_current_after_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            self.assertEqual(STORE_SCHEMA_VERSION, store.schema_version())
            self.assertEqual(STORE_SCHEMA_VERSION, store.require_current_schema())

    def test_migration_from_unmigrated_file_reaches_current_version(self) -> None:
        """The migration seam: a raw file migrates forward, exactly once."""
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            raw = ServerStore.open(data_dir, migrate=False)
            try:
                self.assertEqual(0, raw.schema_version())
            finally:
                raw.close()
            store = ServerStore.open(data_dir)
            self.addCleanup(store.close)
            self.assertEqual(STORE_SCHEMA_VERSION, store.schema_version())
            applied = self._migration_versions(store)
            self.assertEqual(list(range(1, STORE_SCHEMA_VERSION + 1)), applied)
            # Re-opening applies nothing new (idempotent).
            store.close()
            reopened = ServerStore.open(data_dir)
            self.addCleanup(reopened.close)
            self.assertEqual(STORE_SCHEMA_VERSION, reopened.schema_version())
            self.assertEqual(applied, self._migration_versions(reopened))

    def test_migration_list_covers_every_version_from_one(self) -> None:
        self.assertEqual(
            list(range(1, STORE_SCHEMA_VERSION + 1)),
            [version for version, _statements in MIGRATIONS],
        )

    def test_future_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            store.close()
            # Forge a future-schema marker with a raw sqlite3 connection
            # (test-side only; no private API of the store is touched).
            connection = sqlite3.connect(str(canonical_store_path(data_dir)))
            try:
                cursor = connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    + "VALUES (?, ?)",
                    (STORE_SCHEMA_VERSION + 1, T0),
                )
                _ = cursor
                connection.commit()
            finally:
                connection.close()
            reopened = ServerStore.open(data_dir)
            self.addCleanup(reopened.close)
            with self.assertRaises(ServerStoreError):
                _ = reopened.require_current_schema()

    def test_reopen_preserves_written_state(self) -> None:
        """Crash/restart semantics: committed state survives a reopen."""
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            store.save_configuration_document({"schema_version": 1, "k": 1}, at=T0)
            store.close()
            reopened = ServerStore.open(data_dir)
            self.addCleanup(reopened.close)
            self.assertEqual(
                {"schema_version": 1, "k": 1},
                reopened.load_configuration_document(),
            )

    _TABLES: tuple[str, ...] = (
        "server_configuration",
        "administrator_identity",
        "admin_sessions",
        "client_keys",
        "provider_secrets",
        "worker_pairings",
        "audit_records",
    )

    def _migration_versions(self, store: ServerStore) -> list[int]:
        return list(store.applied_migration_versions())

    def test_all_expected_tables_exist_after_migration(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            store.close()
            connection = sqlite3.connect(str(canonical_store_path(data_dir)))
            try:
                for table in self._TABLES:
                    rows = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        + "AND name = ?",
                        (table,),
                    ).fetchall()
                    self.assertEqual(1, len(rows), table)
            finally:
                connection.close()


class ConfigurationAndIdentityTests(unittest.TestCase):
    def test_configuration_round_trip_and_absence(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            self.assertIsNone(store.load_configuration_document())
            document = {"schema_version": 1, "aliases": {"a": {"profile_id": "p"}}}
            store.save_configuration_document(document, at=T0)
            self.assertEqual(document, store.load_configuration_document())
            self.assertEqual(T0, store.configuration_updated_at())

    def test_admin_identity_rotation_revokes_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            self.assertIsNone(store.admin_identity())
            store.set_admin_identity(
                password_salt_hex="00" * 16,
                password_hash_hex="11" * 32,
                pbkdf2_iterations=1000,
                at=T0,
            )
            store.create_session(
                session_hash="a" * 64, csrf_hash="b" * 64, created_at=T0, expires_at=T1
            )
            self.assertIsNotNone(store.session_csrf_hash("a" * 64, now=T0))
            store.set_admin_identity(
                password_salt_hex="22" * 16,
                password_hash_hex="33" * 32,
                pbkdf2_iterations=1000,
                at=T1,
            )
            self.assertIsNone(store.session_csrf_hash("a" * 64, now=T1))

    def test_session_expiry_deletes_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            store.create_session(
                session_hash="a" * 64, csrf_hash="b" * 64, created_at=T0, expires_at=T0
            )
            self.assertIsNone(store.session_csrf_hash("a" * 64, now=T1))
            store.create_session(
                session_hash="c" * 64, csrf_hash="d" * 64, created_at=T0, expires_at=T1
            )
            self.assertEqual("d" * 64, store.session_csrf_hash("c" * 64, now=T0))
            self.assertEqual(1, store.delete_expired_sessions(now=T1))


class ClientKeyAndSecretTests(unittest.TestCase):
    def test_client_key_lifecycle_hash_only(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            store.add_client_key(
                client_id="client-a", key_hash="a" * 64, label="cli", created_at=T0
            )
            self.assertEqual(
                {"client-a": "a" * 64}, store.active_client_key_hashes()
            )
            self.assertTrue(
                store.revoke_client_key(client_id="client-a", revoked_at=T1)
            )
            self.assertEqual({}, store.active_client_key_hashes())
            records = store.list_client_keys()
            self.assertEqual(1, len(records))
            self.assertEqual(T1, records[0].revoked_at)
            self.assertFalse(
                store.revoke_client_key(client_id="client-a", revoked_at=T1)
            )

    def test_duplicate_client_key_id_refused(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            store.add_client_key(
                client_id="client-a", key_hash="a" * 64, label="cli", created_at=T0
            )
            with self.assertRaises(ServerStoreError):
                store.add_client_key(
                    client_id="client-a", key_hash="b" * 64, label="x", created_at=T0
                )

    def test_provider_secret_presence_and_deletion(self) -> None:
        """The dispatch-only accessor is the sole reader of the value."""
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            self.assertFalse(store.has_provider_secret("zai-http"))
            store.set_provider_secret(
                provider_id="zai-http", secret="SYNTHETIC-SECRET", at=T0
            )
            self.assertTrue(store.has_provider_secret("zai-http"))
            self.assertEqual(
                "SYNTHETIC-SECRET", store.get_provider_secret("zai-http")
            )
            self.assertIsNone(store.get_provider_secret("other"))
            self.assertTrue(
                store.delete_provider_secret(provider_id="zai-http")
            )
            self.assertFalse(store.has_provider_secret("zai-http"))


class PairingTests(unittest.TestCase):
    def test_pairing_lifecycle_single_use_redemption(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            store.create_pairing(
                worker_id="worker-a",
                label="lab rig",
                code_hash="c" * 64,
                created_at=T0,
                expires_at=T1,
            )
            records = store.list_pairings()
            self.assertEqual(("worker-a",), (records[0].worker_id,))
            self.assertEqual("pending", records[0].status)
            worker_id = store.redeem_pairing(
                code_hash="c" * 64, token_hash="t" * 64, at=T0
            )
            self.assertEqual("worker-a", worker_id)
            # A code redeems exactly once.
            self.assertIsNone(
                store.redeem_pairing(code_hash="c" * 64, token_hash="x" * 64, at=T0)
            )
            self.assertEqual({"worker-a": "t" * 64}, store.active_worker_token_hashes())
            self.assertTrue(
                store.record_worker_connection(worker_id="worker-a", at=T1)
            )
            self.assertTrue(store.revoke_pairing(worker_id="worker-a", at=T1))
            self.assertEqual({}, store.active_worker_token_hashes())
            self.assertEqual("revoked", store.list_pairings()[0].status)

    def test_expired_code_is_never_redeemable(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            store.create_pairing(
                worker_id="worker-a",
                label="lab rig",
                code_hash="c" * 64,
                created_at=T0,
                expires_at=T1,
            )
            self.assertIsNone(
                store.redeem_pairing(code_hash="c" * 64, token_hash="t" * 64, at=T1)
            )
            self.assertEqual("revoked", store.list_pairings()[0].status)

    def test_duplicate_worker_id_refused(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            store.create_pairing(
                worker_id="worker-a",
                label="one",
                code_hash="c" * 64,
                created_at=T0,
                expires_at=T1,
            )
            with self.assertRaises(ServerStoreError):
                store.create_pairing(
                    worker_id="worker-a",
                    label="two",
                    code_hash="d" * 64,
                    created_at=T0,
                    expires_at=T1,
                )


class AuditRetentionTests(unittest.TestCase):
    def test_retention_is_bounded_by_count(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            for index in range(25):
                store.append_audit_record(
                    payload={"n": index},
                    recorded_at=T0,
                    max_records=10,
                    max_age_seconds=1_000_000,
                )
            self.assertEqual(10, store.audit_record_count())

    def test_retention_is_bounded_by_age(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            store.append_audit_record(
                payload={"n": "old"},
                recorded_at="2026-09-01T00:00:00.000Z",
                max_records=100,
                max_age_seconds=60,
            )
            store.append_audit_record(
                payload={"n": "new"},
                recorded_at=T0,
                max_records=100,
                max_age_seconds=60,
            )
            self.assertEqual(1, store.audit_record_count())

    def test_retention_bounds_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            with self.assertRaises(ServerStoreError):
                store.append_audit_record(
                    payload={}, recorded_at=T0, max_records=0, max_age_seconds=60
                )
            with self.assertRaises(ServerStoreError):
                store.append_audit_record(
                    payload={}, recorded_at=T0, max_records=10, max_age_seconds=0
                )


if __name__ == "__main__":
    _ = unittest.main()
