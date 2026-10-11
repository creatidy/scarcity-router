"""Durable-store tests (M09): schema versioning, migrations, retention,
permissioned storage and crash/restart semantics (issue #94, D-041/D-044).

Deterministic: injected timestamps, temporary directories, no network.
"""

from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

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
            before = canonical_store_path(data_dir).read_bytes()
            for migrate in (True, False):
                with self.subTest(migrate=migrate):
                    with self.assertRaisesRegex(ServerStoreError, "future store"):
                        _ = ServerStore.open(data_dir, migrate=migrate)
                    self.assertEqual(before, canonical_store_path(data_dir).read_bytes())

    def test_future_store_refuses_before_journal_or_permission_changes(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            store.save_configuration_document({"schema_version": 2}, at=T0)
            store.set_provider_secret(provider_id="synthetic", secret="SYNTHETIC-SECRET", at=T0)
            store.close()
            path = canonical_store_path(data_dir)
            connection = sqlite3.connect(path)
            try:
                _ = connection.execute("PRAGMA journal_mode=WAL")
                _ = connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    (STORE_SCHEMA_VERSION + 1, T0),
                )
                connection.commit()
                before = path.read_bytes()
                directory_mode = stat.S_IMODE(data_dir.stat().st_mode)
                file_mode = stat.S_IMODE(path.stat().st_mode)
                with self.assertRaisesRegex(ServerStoreError, "future store") as caught:
                    _ = ServerStore.open(data_dir)
                self.assertNotIn("SYNTHETIC-SECRET", str(caught.exception))
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(directory_mode, stat.S_IMODE(data_dir.stat().st_mode))
                self.assertEqual(file_mode, stat.S_IMODE(path.stat().st_mode))
                self.assertEqual(("wal",), connection.execute("PRAGMA journal_mode").fetchone())
                self.assertEqual(
                    ("SYNTHETIC-SECRET",),
                    connection.execute("SELECT secret FROM provider_secrets").fetchone(),
                )
            finally:
                connection.close()

    def test_refused_last_connection_preserves_committed_uncheckpointed_wal(self) -> None:
        writer = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
if sys.argv[2] == 'future':
    c.execute("INSERT INTO schema_migrations VALUES (3, 'synthetic')")
else:
    c.execute('DELETE FROM schema_migrations WHERE version=1')
c.commit()
os._exit(0)
"""
        for damage in ("future", "gap"):
            for migrate in (True, False):
                with self.subTest(damage=damage, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                    data_dir = Path(parent) / "server"
                    store = ServerStore.open(data_dir)
                    path = store.path
                    store.save_configuration_document({"schema_version": 2}, at=T0)
                    store.set_provider_secret(provider_id="synthetic", secret="SYNTHETIC-SECRET", at=T0)
                    store.append_audit_record(payload={"pin": "synthetic-pin"}, recorded_at=T0,
                                             max_records=100, max_age_seconds=1000)
                    store.close()
                    _ = subprocess.run([sys.executable, "-c", writer, str(path), damage],
                                       env={"PATH": os.defpath, "HOME": parent, "TMPDIR": parent},
                                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    wal = Path(str(path) + "-wal")
                    self.assertGreater(wal.stat().st_size, 0)
                    os.chmod(data_dir, 0o750)
                    os.chmod(path, 0o640)
                    before = path.read_bytes(), wal.read_bytes()
                    reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                    try:
                        before_data = tuple(reader.iterdump())
                        self.assertEqual(("wal",), reader.execute("PRAGMA journal_mode").fetchone())
                    finally:
                        reader.close()
                    self.assertTrue(before == (path.read_bytes(), wal.read_bytes()), "inspection altered fixture")
                    with self.assertRaises(ServerStoreError):
                        _ = ServerStore.open(data_dir, migrate=migrate)
                    self.assertTrue(wal.exists(), "refusal removed committed WAL")
                    self.assertTrue(before == (path.read_bytes(), wal.read_bytes()), "refusal checkpointed WAL")
                    self.assertEqual((0o750, 0o640), (
                        stat.S_IMODE(data_dir.stat().st_mode), stat.S_IMODE(path.stat().st_mode),
                    ))
                    reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                    try:
                        self.assertTrue(before_data == tuple(reader.iterdump()), "retained state changed")
                        self.assertEqual(("wal",), reader.execute("PRAGMA journal_mode").fetchone())
                    finally:
                        reader.close()

    def test_existing_relative_path_with_uri_metacharacters_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server #%?"
            store = ServerStore.open(data_dir)
            path = store.path
            store.save_configuration_document({"schema_version": 2}, at=T0)
            store.close()
            relative = Path(os.path.relpath(path))
            reopened = ServerStore(relative)
            self.addCleanup(reopened.close)
            self.assertEqual(STORE_SCHEMA_VERSION, reopened.schema_version())
            self.assertEqual({"schema_version": 2}, reopened.load_configuration_document())

    def test_gapped_migration_history_refuses_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            store.close()
            path = canonical_store_path(data_dir)
            with sqlite3.connect(path) as connection:
                _ = connection.execute("DELETE FROM schema_migrations WHERE version=1")
            before = path.read_bytes()
            with self.assertRaisesRegex(ServerStoreError, "history is invalid"):
                _ = ServerStore.open(data_dir)
            self.assertEqual(before, path.read_bytes())

    def test_invalid_metadata_refuses_before_journal_or_permission_effects(self) -> None:
        for damage in ("gap", "missing_column", "view", "empty", "missing"):
            for migrate in (True, False):
                with self.subTest(damage=damage, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                    data_dir = Path(parent) / "server"
                    store = ServerStore.open(data_dir)
                    store.save_configuration_document({"schema_version": 2}, at=T0)
                    store.set_provider_secret(provider_id="synthetic", secret="SYNTHETIC-SECRET", at=T0)
                    store.append_audit_record(payload={"pin": "sr-pin:resource/zai/model/variant"}, recorded_at=T0,
                                             max_records=100, max_age_seconds=1000)
                    path = store.path
                    store.close()
                    connection = sqlite3.connect(path)
                    try:
                        self.assertEqual(("wal",), connection.execute("PRAGMA journal_mode=WAL").fetchone())
                        if damage == "gap":
                            _ = connection.execute("DELETE FROM schema_migrations WHERE version=1")
                        elif damage == "empty":
                            _ = connection.execute("DELETE FROM schema_migrations")
                        else:
                            _ = connection.execute("DROP TABLE schema_migrations")
                            if damage == "missing_column":
                                _ = connection.execute("CREATE TABLE schema_migrations(wrong_column INTEGER)")
                            elif damage == "view":
                                _ = connection.execute("CREATE VIEW schema_migrations AS SELECT 1 AS version")
                        connection.commit()
                    finally:
                        connection.close()
                    os.chmod(data_dir, 0o750)
                    os.chmod(path, 0o640)
                    before_bytes = path.read_bytes()
                    connection = sqlite3.connect(path)
                    try:
                        before_data = tuple(connection.iterdump())
                    finally:
                        connection.close()
                    refused = False
                    try:
                        opened = ServerStore.open(data_dir, migrate=migrate)
                    except ServerStoreError as exc:
                        refused = True
                        self.assertNotIn("SYNTHETIC-SECRET", str(exc))
                    else:
                        opened.close()
                    with self.subTest(property="typed_refusal"):
                        self.assertTrue(refused, "invalid metadata was returned as a usable store")
                    with self.subTest(property="permissions"):
                        self.assertEqual((0o750, 0o640), (
                            stat.S_IMODE(data_dir.stat().st_mode), stat.S_IMODE(path.stat().st_mode),
                        ))
                    with self.subTest(property="bytes"):
                        self.assertTrue(before_bytes == path.read_bytes(), "database bytes changed")
                    connection = sqlite3.connect(path)
                    try:
                        with self.subTest(property="logical_state"):
                            self.assertTrue(before_data == tuple(connection.iterdump()), "retained state changed")
                        with self.subTest(property="journal"):
                            self.assertEqual(("wal",), connection.execute("PRAGMA journal_mode").fetchone())
                    finally:
                        connection.close()

    def test_malformed_migration_version_refuses_opaquely_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir, migrate=False)
            path = store.path
            store.close()
            with sqlite3.connect(path) as connection:
                _ = connection.execute("CREATE TABLE schema_migrations(version TEXT, applied_at TEXT)")
                _ = connection.execute("INSERT INTO schema_migrations VALUES ('SYNTHETIC-SECRET', 'synthetic')")
            before = path.read_bytes()
            with self.assertRaisesRegex(ServerStoreError, "metadata is invalid") as caught:
                _ = ServerStore.open(data_dir)
            self.assertNotIn("SYNTHETIC-SECRET", str(caught.exception))
            self.assertEqual(before, path.read_bytes())

    def test_closed_metadata_reader_is_not_an_empty_store(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            store.close()
            for reader in (store.schema_version, store.applied_migration_versions):
                with self.subTest(reader=reader.__name__):
                    with self.assertRaisesRegex(ServerStoreError, "invalid or unreadable"):
                        _ = reader()

    def test_metadata_read_error_is_opaque_not_zero_or_empty(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = ServerStore.open(Path(parent) / "server")
            self.addCleanup(store.close)
            with mock.patch("scarcity_router.server_store._fetch_rows",
                            side_effect=sqlite3.OperationalError("SYNTHETIC-SECRET")):
                for reader in (store.schema_version, store.applied_migration_versions):
                    with self.subTest(reader=reader.__name__):
                        with self.assertRaisesRegex(ServerStoreError, "invalid or unreadable") as caught:
                            _ = reader()
                        self.assertNotIn("SYNTHETIC-SECRET", str(caught.exception))

    def test_supported_zero_old_and_current_stores_still_tighten_owned_permissions(self) -> None:
        for version in (0, 1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as parent:
                data_dir = Path(parent) / "server"
                store = ServerStore.open(data_dir, migrate=version == 2)
                path = store.path
                store.close()
                if version == 1:
                    connection = sqlite3.connect(path)
                    try:
                        for statement in MIGRATIONS[0][1]:
                            _ = connection.execute(statement)
                        _ = connection.execute("INSERT INTO schema_migrations VALUES (1, ?)", (T0,))
                        connection.commit()
                    finally:
                        connection.close()
                os.chmod(data_dir, 0o750)
                os.chmod(path, 0o640)
                reopened = ServerStore.open(data_dir, migrate=False)
                try:
                    self.assertEqual(version, reopened.schema_version())
                    self.assertEqual((0o700, 0o600), (
                        stat.S_IMODE(data_dir.stat().st_mode), stat.S_IMODE(path.stat().st_mode),
                    ))
                    self.assertEqual(STORE_SCHEMA_VERSION, reopened.migrate())
                finally:
                    reopened.close()

    def test_corrupt_database_refuses_opaquely_before_permission_effects(self) -> None:
        for migrate in (True, False):
            with self.subTest(migrate=migrate), tempfile.TemporaryDirectory() as parent:
                data_dir = Path(parent) / "server"
                store = ServerStore.open(data_dir)
                path = store.path
                store.close()
                before = b"not a database SYNTHETIC-SECRET"
                _ = path.write_bytes(before)
                os.chmod(data_dir, 0o750)
                os.chmod(path, 0o640)
                with self.assertRaisesRegex(ServerStoreError, "invalid or unreadable") as caught:
                    _ = ServerStore.open(data_dir, migrate=migrate)
                self.assertNotIn("SYNTHETIC-SECRET", str(caught.exception))
                self.assertTrue(before == path.read_bytes(), "corrupt bytes were altered")
                self.assertEqual((0o750, 0o640), (
                    stat.S_IMODE(data_dir.stat().st_mode), stat.S_IMODE(path.stat().st_mode),
                ))

    def test_failed_upgrade_rolls_back_the_entire_pending_set(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            broken = (MIGRATIONS[0], (2, ("CREATE TABLE partial(value TEXT)", "INVALID SQL")))
            with mock.patch("scarcity_router.server_store.MIGRATIONS", broken):
                with self.assertRaisesRegex(ServerStoreError, "original state retained"):
                    _ = ServerStore.open(data_dir)
            with sqlite3.connect(canonical_store_path(data_dir)) as connection:
                self.assertEqual([], connection.execute("SELECT name FROM sqlite_master").fetchall())
            store = ServerStore.open(data_dir)
            self.addCleanup(store.close)
            self.assertEqual((1, 2), store.applied_migration_versions())

    def test_failed_v1_upgrade_retains_configuration_secrets_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            raw = ServerStore.open(data_dir, migrate=False)
            path = raw.path
            raw.close()
            with sqlite3.connect(path) as connection:
                for statement in MIGRATIONS[0][1]:
                    _ = connection.execute(statement)
                _ = connection.execute("INSERT INTO schema_migrations VALUES (1, ?)", (T0,))
                _ = connection.execute("INSERT INTO server_configuration VALUES (1, ?, ?)", ('{"schema_version":1}', T0))
                _ = connection.execute("INSERT INTO provider_secrets VALUES ('synthetic', 'SYNTHETIC-SECRET', ?)", (T0,))
                _ = connection.execute("INSERT INTO audit_records(recorded_at,payload) VALUES (?, ?)", (T0, '{"pin":"sr-pin:resource/zai/model/variant"}'))
            before = path.read_bytes()
            broken = (MIGRATIONS[0], (2, ("DROP TABLE worker_pairings", "INVALID SQL")))
            with mock.patch("scarcity_router.server_store.MIGRATIONS", broken):
                with self.assertRaises(ServerStoreError):
                    _ = ServerStore.open(data_dir)
            self.assertEqual(before, path.read_bytes())
            store = ServerStore.open(data_dir)
            self.addCleanup(store.close)
            self.assertEqual({"schema_version": 1}, store.load_configuration_document())
            self.assertEqual("SYNTHETIC-SECRET", store.get_provider_secret("synthetic"))
            self.assertEqual(1, store.audit_record_count())

    def test_concurrent_open_serializes_migration_bookkeeping(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            def open_and_close(_index: int) -> tuple[int, ...]:
                store = ServerStore.open(data_dir)
                try:
                    return store.applied_migration_versions()
                finally:
                    store.close()
            with ThreadPoolExecutor(max_workers=2) as executor:
                self.assertEqual([(1, 2), (1, 2)], list(executor.map(open_and_close, range(2))))

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
        "audit_records",
    )

    _RETIRED_TABLES: tuple[str, ...] = ("worker_pairings",)

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
                for table in self._RETIRED_TABLES:
                    rows = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        + "AND name = ?",
                        (table,),
                    ).fetchall()
                    self.assertEqual(0, len(rows), table)
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


class RetiredPairingSurfaceTests(unittest.TestCase):
    """The M09-side duplicate pairing surface is gone (M05 is the ONE system).

    Since schema version 2 the store holds no worker pairing tables and
    exposes no pairing API; worker identities live in the M05 worker
    identity store beside this file.
    """

    def test_pairing_api_is_absent_from_the_store(self) -> None:
        for name in (
            "create_pairing",
            "list_pairings",
            "redeem_pairing",
            "revoke_pairing",
            "record_worker_connection",
            "active_worker_token_hashes",
        ):
            self.assertFalse(
                hasattr(ServerStore, name),
                f"ServerStore.{name} must stay retired",
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
