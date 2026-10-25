"""Durable-store tests (M09): schema versioning, migrations, retention,
permissioned storage and crash/restart semantics (issue #94, D-041/D-044).

Deterministic: injected timestamps, temporary directories, no network.
"""

from __future__ import annotations

import gc
import hashlib
import sqlite3
import os
import json
import re
import shutil
import subprocess
import sys
import stat
import tempfile
import unittest
import weakref
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import BinaryIO, cast
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


def _store_connection(store: ServerStore) -> sqlite3.Connection:
    """Private transaction seam needed for inode-lock regression tests."""
    return cast(sqlite3.Connection, getattr(store, "_connection"))


class DatabaseEntryTests(unittest.TestCase):
    def test_linked_cold_sidecars_refuse_before_sqlite_or_recovery_access(self) -> None:
        for journal in ("DELETE", "WAL"):
            for production in (False, True):
                for suffix in ("-journal", "-wal", "-shm"):
                    for link in ("symlink", "hardlink"):
                        for content in (b"", b"synthetic sidecar fixture"):
                            with self.subTest(journal=journal, production=production, suffix=suffix,
                                              link=link, empty=not content), tempfile.TemporaryDirectory() as root:
                                directory = Path(root) / "server"
                                store = ServerStore.open(directory)
                                path = store.path
                                store.save_configuration_document({"fixture": "retained"}, T0)
                                store.close()
                                connection = sqlite3.connect(path)
                                try:
                                    mode = cast("tuple[str]", connection.execute("PRAGMA journal_mode=" + journal).fetchone())
                                    self.assertEqual((journal.lower(),), mode)
                                    if journal == "WAL":
                                        self.assertEqual((0, 0, 0), connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
                                finally:
                                    connection.close()
                                sidecars = tuple(Path(str(path) + s) for s in ("-journal", "-wal", "-shm"))
                                self.assertTrue(all(not p.exists() for p in sidecars))
                                target = Path(root) / "fixture-target"
                                _ = target.write_bytes(content)
                                os.chmod(target, 0o640)
                                entry = Path(str(path) + suffix)
                                if link == "symlink":
                                    entry.symlink_to(target)
                                else:
                                    os.link(target, entry)
                                os.chmod(directory, 0o750)
                                os.chmod(path, 0o640)
                                before = path.read_bytes(), target.read_bytes(), target.stat(), entry.lstat()
                                connect = sqlite3.connect
                                with mock.patch("scarcity_router.server_store.sqlite3.connect", wraps=connect) as opened, \
                                        mock.patch.object(ServerStore, "_reclaim_recovery_slot") as reclaimed:
                                    with self.assertRaisesRegex(ServerStoreError, "unsafe"):
                                        if production:
                                            _ = ServerStore.open(directory)
                                        else:
                                            _ = ServerStore(path)
                                opened.assert_not_called()
                                reclaimed.assert_not_called()
                                self.assertEqual(before, (path.read_bytes(), target.read_bytes(), target.stat(), entry.lstat()))
                                self.assertEqual((0o750, 0o640, 0o640), (
                                    stat.S_IMODE(directory.stat().st_mode), stat.S_IMODE(path.stat().st_mode),
                                    stat.S_IMODE(target.stat().st_mode),
                                ))

    def test_nonregular_or_foreign_owned_sidecars_refuse_before_access(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            for kind in ("directory", "foreign"):
                with self.subTest(suffix=suffix, kind=kind), tempfile.TemporaryDirectory() as root:
                    store = ServerStore.open(Path(root) / "server")
                    path = store.path
                    store.close()
                    entry = Path(str(path) + suffix)
                    if kind == "directory":
                        entry.mkdir()
                    else:
                        _ = entry.write_bytes(b"synthetic sidecar fixture")
                    original = Path.lstat
                    def observed(entry_path: Path) -> os.stat_result:
                        info = original(entry_path)
                        if kind == "foreign" and entry_path == entry:
                            fields = list(info)
                            fields[4] = info.st_uid + 1
                            return os.stat_result(fields)
                        return info
                    before = path.read_bytes()
                    with mock.patch.object(Path, "lstat", autospec=True, side_effect=observed), \
                            mock.patch("scarcity_router.server_store.sqlite3.connect") as opened, \
                            mock.patch.object(ServerStore, "_reclaim_recovery_slot") as reclaimed:
                        with self.assertRaisesRegex(ServerStoreError, "unsafe"):
                            _ = ServerStore.open(path.parent)
                    opened.assert_not_called()
                    reclaimed.assert_not_called()
                    self.assertEqual(before, path.read_bytes())

    def test_linked_database_refuses_before_sqlite_or_recovery_access(self) -> None:
        for version in (0, 1, 2):
            for migrate in (False, True):
                for link in ("symlink", "hardlink"):
                    with self.subTest(version=version, migrate=migrate, link=link), tempfile.TemporaryDirectory() as root:
                        target_dir = Path(root) / "target"
                        if version:
                            target = OwnedSchemaRecoveryTests().seed(str(target_dir), version)
                        else:
                            store = ServerStore.open(target_dir, migrate=False)
                            target = store.path
                            store.close()
                        directory = Path(root) / "entry"
                        directory.mkdir(mode=0o750)
                        os.chmod(directory, 0o750)
                        entry = canonical_store_path(directory)
                        if link == "symlink":
                            entry.symlink_to(target)
                        else:
                            os.link(target, entry)
                        os.chmod(target, 0o640)
                        files = (target, Path(str(target) + "-journal"), Path(str(target) + "-wal"))
                        before = tuple(p.read_bytes() if p.exists() else None for p in files)
                        reader = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
                        try:
                            records = tuple(reader.iterdump())
                            journal_mode = cast("tuple[str]", reader.execute("PRAGMA journal_mode").fetchone())
                        finally:
                            reader.close()
                        connect = sqlite3.connect
                        with mock.patch("scarcity_router.server_store.sqlite3.connect", wraps=connect) as opened, \
                                mock.patch("scarcity_router.server_store._fetch_rows") as metadata, \
                                mock.patch.object(ServerStore, "_reclaim_recovery_slot") as reclaimed:
                            refused = False
                            try:
                                store = ServerStore.open(directory, migrate=migrate)
                            except ServerStoreError:
                                refused = True
                            else:
                                store.close()
                        with self.subTest(property="refusal"):
                            self.assertTrue(refused, "unsafe database entry was accepted")
                        with self.subTest(property="no_read"):
                            self.assertEqual(0, opened.call_count, "unsafe database entry reached SQLite")
                            metadata.assert_not_called()
                            reclaimed.assert_not_called()
                        with self.subTest(property="unchanged"):
                            self.assertTrue(before == tuple(p.read_bytes() if p.exists() else None for p in files), "synthetic target changed")
                            self.assertEqual(0o640, stat.S_IMODE(target.stat().st_mode))
                            self.assertEqual(0o750, stat.S_IMODE(directory.stat().st_mode))
                            reader = connect(target.as_uri() + "?mode=ro", uri=True)
                            try:
                                self.assertTrue(records == tuple(reader.iterdump()), "synthetic records changed")
                                self.assertTrue(journal_mode == reader.execute("PRAGMA journal_mode").fetchone(), "journal mode changed")
                            finally:
                                reader.close()

    def test_linked_hot_and_wal_database_refuses_without_target_access(self) -> None:
        for version in (1, 2):
            for migrate in (False, True):
                for link in ("symlink", "hardlink"):
                    for journal in ("hot", "wal"):
                        with self.subTest(version=version, migrate=migrate, link=link, journal=journal), tempfile.TemporaryDirectory() as root:
                            fixture = OwnedSchemaRecoveryTests()
                            target = fixture.seed(str(Path(root) / "target"), version)
                            if journal == "hot":
                                _ = fixture.crash(target)
                            else:
                                writer = "import os, sqlite3, sys; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL'); c.execute(\"UPDATE client_keys SET label='synthetic-committed'\"); c.commit(); os._exit(0)"
                                _ = subprocess.run([sys.executable, "-c", writer, str(target)],
                                                   env={"PATH": os.defpath, "HOME": root, "TMPDIR": root},
                                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                            directory = Path(root) / "entry"
                            directory.mkdir(mode=0o750)
                            os.chmod(directory, 0o750)
                            entry = canonical_store_path(directory)
                            if link == "symlink":
                                entry.symlink_to(target)
                            else:
                                os.link(target, entry)
                            os.chmod(target, 0o640)
                            files = (target, Path(str(target) + "-journal"), Path(str(target) + "-wal"), Path(str(target) + "-shm"))
                            before = tuple(p.read_bytes() if p.exists() else None for p in files)
                            with mock.patch("scarcity_router.server_store.sqlite3.connect") as opened, \
                                    mock.patch("scarcity_router.server_store._fetch_rows") as metadata, \
                                    mock.patch.object(ServerStore, "_reclaim_recovery_slot") as reclaimed:
                                with self.assertRaises(ServerStoreError):
                                    _ = ServerStore.open(directory, migrate=migrate)
                            opened.assert_not_called()
                            metadata.assert_not_called()
                            reclaimed.assert_not_called()
                            self.assertTrue(before == tuple(p.read_bytes() if p.exists() else None for p in files), "synthetic journal target changed")
                            self.assertEqual((0o750, 0o640), (stat.S_IMODE(directory.stat().st_mode), stat.S_IMODE(target.stat().st_mode)))

    def test_replaced_database_refuses_before_sqlite_access(self) -> None:
        for replacement in ("regular", "symlink", "hardlink"):
            for migrate in (False, True):
                with self.subTest(replacement=replacement, migrate=migrate), tempfile.TemporaryDirectory() as root:
                    fixture = OwnedSchemaRecoveryTests()
                    path = fixture.seed(str(Path(root) / "source"), 2)
                    target = fixture.seed(str(Path(root) / "target"), 2)
                    os.chmod(path.parent, 0o750)
                    os.chmod(target, 0o640)
                    before = path.read_bytes(), target.read_bytes()
                    retained = path.with_name("previous.sqlite3")
                    original_open = os.open
                    def replacing_open(entry: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777) -> int:
                        fd = original_open(entry, flags, mode)
                        if os.fspath(entry) == str(path):
                            _ = path.rename(retained)
                            if replacement == "regular":
                                _ = shutil.copyfile(target, path)
                            elif replacement == "symlink":
                                path.symlink_to(target)
                            else:
                                os.link(target, path)
                        return fd
                    with mock.patch("scarcity_router.server_store.os.open", side_effect=replacing_open), \
                            mock.patch("scarcity_router.server_store.sqlite3.connect") as opened, \
                            mock.patch("scarcity_router.server_store._fetch_rows") as metadata:
                        with self.assertRaises(ServerStoreError):
                            _ = ServerStore.open(path.parent, migrate=migrate)
                    opened.assert_not_called()
                    metadata.assert_not_called()
                    self.assertTrue(before == (retained.read_bytes(), target.read_bytes()), "replacement changed synthetic files")
                    self.assertEqual((0o750, 0o640), (stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(target.stat().st_mode)))

    def test_nonowned_directory_or_database_refuses_before_sqlite_or_cleanup(self) -> None:
        for foreign in ("directory", "database"):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory() as root:
                path = OwnedSchemaRecoveryTests().seed(root, 2)
                original = Path.lstat
                def foreign_owner(entry: Path) -> os.stat_result:
                    info = original(entry)
                    if entry == (path.parent if foreign == "directory" else path):
                        fields = list(info)
                        fields[4] = info.st_uid + 1
                        return os.stat_result(fields)
                    return info
                before = path.read_bytes()
                with mock.patch.object(Path, "lstat", autospec=True, side_effect=foreign_owner), \
                        mock.patch("scarcity_router.server_store.sqlite3.connect") as opened, \
                        mock.patch.object(ServerStore, "_reclaim_recovery_slot") as reclaimed:
                    with self.assertRaisesRegex(ServerStoreError, "unsafe"):
                        _ = ServerStore(path)
                opened.assert_not_called()
                reclaimed.assert_not_called()
                self.assertTrue(before == path.read_bytes(), "foreign-owned synthetic file changed")

    def test_nonregular_and_reparse_database_refuse_before_access(self) -> None:
        for kind in ("directory", "reparse", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / "server"
                directory.mkdir(mode=0o700)
                path = canonical_store_path(directory)
                if kind == "directory":
                    path.mkdir()
                elif kind == "fifo":
                    if os.name == "nt":
                        continue
                    os.mkfifo(path)
                else:
                    store = ServerStore.open(directory)
                    store.close()
                original = Path.lstat
                def reparse_info(entry: Path) -> os.stat_result:
                    info = original(entry)
                    if kind == "reparse" and entry == path:
                        return cast(os.stat_result, mock.Mock(st_mode=info.st_mode, st_uid=info.st_uid,
                                                            st_nlink=info.st_nlink, st_file_attributes=0x400))
                    return info
                with mock.patch.object(Path, "lstat", autospec=True, side_effect=reparse_info), \
                        mock.patch("scarcity_router.server_store.sqlite3.connect") as opened, \
                        mock.patch.object(ServerStore, "_reclaim_recovery_slot") as reclaimed:
                    with self.assertRaises(ServerStoreError):
                        _ = ServerStore.open(directory)
                opened.assert_not_called()
                reclaimed.assert_not_called()

    def test_parent_alias_and_explicit_relative_unicode_file_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "named user data"
            directory.mkdir(mode=0o750)
            alias = Path(root) / "alias"
            alias.symlink_to(directory, target_is_directory=True)
            store = ServerStore.open(alias)
            store.close()
            filename = directory / "explicit-\u00e9 #%?.sqlite3"
            _ = canonical_store_path(directory).rename(filename)
            relative = Path(os.path.relpath(alias / filename.name))
            direct = ServerStore(relative)
            try:
                self.assertEqual(relative, direct.path)
                self.assertEqual(2, direct.schema_version())
            finally:
                direct.close()
            self.assertEqual((0o700, 0o600), (stat.S_IMODE(directory.stat().st_mode), stat.S_IMODE(filename.stat().st_mode)))


class DatabaseDescriptorLeaseTests(unittest.TestCase):
    _EXTERNAL_WRITER: str = """
import sqlite3, sys, time
connection = sqlite3.connect(sys.argv[1], timeout=0)
connection.execute("PRAGMA busy_timeout=0")
print("READY", flush=True)
deadline = time.monotonic() + 10
blocked = False
while True:
    try:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO provider_secrets VALUES (?, ?, ?)",
                ("external", "external-secret", sys.argv[2]),
            )
            print("PENDING", flush=True)
        connection.commit()
        print("COMMITTED", flush=True)
        break
    except sqlite3.OperationalError as error:
        if (error.sqlite_errorcode & 0xFF) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            print("ERROR", flush=True)
            raise
        if not blocked:
            print("BLOCKED", flush=True)
            blocked = True
        if time.monotonic() >= deadline:
            print("TIMEOUT", flush=True)
            sys.exit(2)
        time.sleep(0.01)
connection.close()
"""

    def _start_external_writer(self, path: Path) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-c", self._EXTERNAL_WRITER, str(path), T1],
            cwd=path.parent,
            env={"PATH": os.defpath, "HOME": str(path.parent), "TMPDIR": str(path.parent)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _wait_for_external_line(
        self, writer: subprocess.Popen[str], expected: str
    ) -> None:
        assert writer.stdout is not None
        lines: list[str] = []
        while True:
            line = cast(str, writer.stdout.readline())
            if not line:
                break
            value = line.strip()
            lines.append(value)
            if value == expected:
                return
            if value in ("COMMITTED", "TIMEOUT", "ERROR"):
                break
        self.fail(f"external writer output did not reach {expected}: {lines}")

    @staticmethod
    def _stop_external_writer(writer: subprocess.Popen[str]) -> None:
        if writer.poll() is None:
            writer.kill()
            _ = writer.wait(timeout=10)
        for stream in (writer.stdout, writer.stderr):
            if stream is not None:
                stream.close()

    def _finish_external_writer(
        self, writer: subprocess.Popen[str], first: ServerStore
    ) -> None:
        assert writer.stdout is not None
        _ = _store_connection(first).execute("COMMIT")
        self._wait_for_external_line(writer, "COMMITTED")
        self.assertEqual(0, writer.wait(timeout=10))
        assert writer.stderr is not None
        self.assertEqual("", cast(str, writer.stderr.read()))

    def _assert_both_provider_secrets_survived(self, directory: Path) -> None:
        reopened = ServerStore.open(directory)
        try:
            self.assertEqual("first-secret", reopened.get_provider_secret("first"))
            self.assertEqual("external-secret", reopened.get_provider_secret("external"))
        finally:
            reopened.close()

    def test_successful_second_open_and_close_preserve_active_read_lock(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "server"
            first = ServerStore.open(directory)
            writer: subprocess.Popen[str] | None = None
            try:
                first.set_provider_secret(provider_id="first", secret="first-secret", at=T0)
                _ = _store_connection(first).execute("BEGIN")
                count = cast(sqlite3.Row | None, _store_connection(first).execute(
                    "SELECT COUNT(*) FROM provider_secrets"
                ).fetchone())
                assert count is not None
                self.assertEqual(1, count[0])

                # The already-current production open must not release the
                # first connection's active rollback-journal read lock.
                second = ServerStore.open(directory)
                second.close()

                writer = self._start_external_writer(first.path)
                assert writer.stdout is not None
                self.assertEqual("READY", cast(str, writer.stdout.readline()).strip())
                self._wait_for_external_line(writer, "BLOCKED")
                self._finish_external_writer(writer, first)
            finally:
                if writer is not None:
                    self._stop_external_writer(writer)
                first.close()

            self._assert_both_provider_secrets_survived(directory)

    def test_failed_second_open_keeps_write_lock_until_first_commit(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "server"
            fixture = OwnedSchemaRecoveryTests()
            path = fixture.seed(parent, 1)
            before = fixture.records(path)
            first = ServerStore.open(directory, migrate=False)
            writer: subprocess.Popen[str] | None = None
            try:
                _ = _store_connection(first).execute("BEGIN IMMEDIATE")
                _ = _store_connection(first).execute(
                    "INSERT INTO provider_secrets VALUES (?, ?, ?)",
                    ("first", "first-secret", T0),
                )

                second_open_failed = False
                try:
                    second = ServerStore.open(directory)
                except ServerStoreError:
                    second_open_failed = True
                else:
                    second.close()
                self.assertTrue(second_open_failed, "the write-locked reopen should refuse safely")

                writer = self._start_external_writer(first.path)
                assert writer.stdout is not None
                self.assertEqual("READY", cast(str, writer.stdout.readline()).strip())
                self._wait_for_external_line(writer, "BLOCKED")
                self._finish_external_writer(writer, first)
            finally:
                if writer is not None:
                    self._stop_external_writer(writer)
                first.close()

            self._assert_both_provider_secrets_survived(directory)
            after = fixture.records(path)
            self.assertEqual(before[0], after[0])
            self.assertCountEqual(
                [*before[1], ("first", "first-secret", T0), ("external", "external-secret", T1)],
                after[1],
            )
            self.assertEqual(before[2:], after[2:])

    def test_guard_exit_failure_finalizes_constructor_lease_exactly_once(self) -> None:
        guard = cast(
            Callable[[ServerStore, Path, os.stat_result | None], AbstractContextManager[os.stat_result | None]],
            getattr(ServerStore, "_recovery_guard"),
        )
        cleanup = cast(Callable[[ServerStore], None], getattr(ServerStore, "_cleanup_failed_open"))
        for failure in (RuntimeError, OSError, KeyboardInterrupt):
            with self.subTest(failure=failure.__name__), tempfile.TemporaryDirectory() as parent:
                fixture = OwnedSchemaRecoveryTests()
                path = fixture.seed(parent, 2)
                before = fixture.records(path)
                first = ServerStore.open(path.parent)
                failed: list[weakref.ReferenceType[ServerStore]] = []
                before_cleanup: list[bool] = []
                after_cleanup: list[bool] = []

                @contextmanager
                def interrupted_guard(
                    store: ServerStore, path: Path, expected: os.stat_result | None,
                ) -> Generator[os.stat_result | None, None, None]:
                    with guard(store, path, expected) as checked:
                        yield checked
                    raise failure("synthetic guard teardown")

                def record_cleanup(store: ServerStore) -> None:
                    finalizer = cast("weakref.finalize[..., object] | None", getattr(store, "_close_finalizer"))
                    assert finalizer is not None
                    failed.append(weakref.ref(store))
                    before_cleanup.append(finalizer.alive)
                    cleanup(store)
                    after_cleanup.append(finalizer.alive)

                writer: subprocess.Popen[str] | None = None
                try:
                    connection = _store_connection(first)
                    _ = connection.execute("BEGIN IMMEDIATE")
                    _ = connection.execute(
                        "INSERT INTO provider_secrets VALUES (?, ?, ?)",
                        ("first", "first-secret", T0),
                    )
                    expected_error = ServerStoreError if failure is OSError else failure
                    message = "filesystem access is unavailable" if failure is OSError else "synthetic guard teardown"
                    with mock.patch.object(ServerStore, "_recovery_guard", interrupted_guard), \
                            mock.patch.object(ServerStore, "_cleanup_failed_open", record_cleanup):
                        with self.assertRaisesRegex(expected_error, message):
                            _ = ServerStore.open(path.parent)
                    _ = gc.collect()
                    self.assertEqual([True], before_cleanup)
                    self.assertEqual(1, len(failed))
                    self.assertIsNone(failed[0]())
                    self.assertTrue(connection.in_transaction)
                    writer = self._start_external_writer(path)
                    self._wait_for_external_line(writer, "READY")
                    self._wait_for_external_line(writer, "BLOCKED")
                    self.assertEqual([False], after_cleanup)
                    references = cast(Callable[[], int], getattr(first, "_database_lease_references"))
                    self.assertEqual(1, references())
                    self._finish_external_writer(writer, first)
                finally:
                    if writer is not None:
                        self._stop_external_writer(writer)
                    first.close()

                self._assert_both_provider_secrets_survived(path.parent)
                after = fixture.records(path)
                self.assertEqual(before[0], after[0])
                self.assertCountEqual(
                    [*before[1], ("first", "first-secret", T0), ("external", "external-secret", T1)],
                    after[1],
                )
                self.assertEqual(before[2:], after[2:])

    def test_failed_wal_second_open_preserves_writer_and_committed_records(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "server"
            first = ServerStore.open(directory)
            writer: subprocess.Popen[str] | None = None
            try:
                first.set_provider_secret(provider_id="committed", secret="retained-secret", at=T0)
                connection = _store_connection(first)
                self.assertEqual(("wal",), tuple(cast(sqlite3.Row, connection.execute("PRAGMA journal_mode=WAL").fetchone())))
                _ = connection.execute("BEGIN IMMEDIATE")
                _ = connection.execute(
                    "INSERT INTO provider_secrets VALUES (?, ?, ?)",
                    ("first", "first-secret", T0),
                )

                # A live WAL transaction prevents the production opener from
                # switching to DELETE; refusal must neither checkpoint nor
                # release the first connection's write lock.
                with self.assertRaises(ServerStoreError):
                    _ = ServerStore.open(directory)
                self.assertTrue(connection.in_transaction)
                self.assertEqual(("wal",), tuple(cast(sqlite3.Row, connection.execute("PRAGMA journal_mode").fetchone())))
                writer = self._start_external_writer(first.path)
                self._wait_for_external_line(writer, "READY")
                self._wait_for_external_line(writer, "BLOCKED")
                self._finish_external_writer(writer, first)
            finally:
                if writer is not None:
                    self._stop_external_writer(writer)
                first.close()

            self._assert_both_provider_secrets_survived(directory)
            reopened = ServerStore.open(directory)
            try:
                self.assertEqual("retained-secret", reopened.get_provider_secret("committed"))
            finally:
                reopened.close()

    def test_wal_reader_survives_failed_open_and_external_writer_commit(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "server"
            first = ServerStore.open(directory)
            writer: subprocess.Popen[str] | None = None
            try:
                first.set_provider_secret(provider_id="first", secret="first-secret", at=T0)
                connection = _store_connection(first)
                self.assertEqual(("wal",), tuple(cast(sqlite3.Row, connection.execute("PRAGMA journal_mode=WAL").fetchone())))
                _ = connection.execute("BEGIN")
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM provider_secrets").fetchone()[0])
                with self.assertRaises(ServerStoreError):
                    _ = ServerStore.open(directory)

                # Unlike rollback mode, a WAL reader must allow an external
                # commit while preserving its original snapshot until COMMIT.
                writer = self._start_external_writer(first.path)
                self._wait_for_external_line(writer, "READY")
                self._wait_for_external_line(writer, "COMMITTED")
                self.assertEqual(0, writer.wait(timeout=10))
                assert writer.stderr is not None
                self.assertEqual("", cast(str, writer.stderr.read()))
                self.assertTrue(connection.in_transaction)
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM provider_secrets").fetchone()[0])
                _ = connection.execute("COMMIT")
                self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM provider_secrets").fetchone()[0])
            finally:
                if writer is not None:
                    self._stop_external_writer(writer)
                first.close()

            self._assert_both_provider_secrets_survived(directory)


class RecoveryLifecycleTests(unittest.TestCase):
    _CHILD: str = """
import os, sys
from pathlib import Path
from scarcity_router import server_store as s
copier = s.shutil.copyfileobj
def interrupted(reader, writer):
    copier(reader, writer)
    writer.flush()
    os.fsync(writer.fileno())
    if sys.argv[2] == 'hold':
        print('READY', flush=True)
        sys.stdin.readline()
    os._exit(0)
s.shutil.copyfileobj = interrupted
s.ServerStore.open(Path(sys.argv[1]))
"""

    def _child(self, path: Path, mode: str = "exit") -> subprocess.Popen[str]:
        return subprocess.Popen([sys.executable, "-c", self._CHILD, str(path.parent), mode],
                                env={"PATH": os.defpath, "HOME": str(path.parent), "TMPDIR": str(path.parent), "PYTHONPATH": str(Path.cwd())},
                                text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def test_delimiter_and_unicode_names_refuse_before_original_effects(self) -> None:
        for damage in ("bracket", "quote", "kelvin", "control"):
            for migrate in (True, False):
                with self.subTest(damage=damage, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                    path = OwnedSchemaRecoveryTests().seed(parent, 2)
                    c = sqlite3.connect(path)
                    try:
                        if damage in ("bracket", "quote"):
                            _ = c.execute("DROP TABLE server_configuration")
                            name = '"[id]"' if damage == "bracket" else '"i""d"'
                            _ = c.execute(f"CREATE TABLE server_configuration({name} INTEGER PRIMARY KEY CHECK ({name}=1), document TEXT NOT NULL, updated_at TEXT NOT NULL)")
                        elif damage == "kelvin":
                            _ = c.execute('ALTER TABLE client_keys RENAME TO "client_\u212aeys"')
                        else:
                            _ = c.execute('ALTER TABLE schema_migrations RENAME TO "\u017fchema_migrations"')
                        c.commit()
                        self.assertEqual(("wal",), c.execute("PRAGMA journal_mode=WAL").fetchone())
                    finally:
                        c.close()
                    os.chmod(path.parent, 0o750)
                    os.chmod(path, 0o640)
                    before = path.read_bytes()
                    with self.assertRaises(ServerStoreError):
                        _ = ServerStore.open(path.parent, migrate=migrate)
                    self.assertTrue(before == path.read_bytes(), "semantic alias changed original")
                    self.assertEqual((0o750, 0o640), (stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode)))
                    c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                    try:
                        self.assertEqual(("wal",), c.execute("PRAGMA journal_mode").fetchone())
                    finally:
                        c.close()

    def test_repeated_killed_copies_are_bounded_and_reclaimed_on_cold_future_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            fixture = OwnedSchemaRecoveryTests()
            path = fixture.seed(parent, 2)
            _ = fixture.crash(path)
            for _attempt in range(3):
                child = self._child(path)
                try:
                    self.assertEqual(0, child.wait(timeout=10))
                finally:
                    _ = child.communicate(timeout=10)
                self.assertEqual(1, len(list(path.parent.glob(".store-recovery-*"))))
            c = sqlite3.connect(path)
            try:
                _ = c.execute("INSERT INTO schema_migrations VALUES (3, ?)", (T0,))
                c.commit()
            finally:
                c.close()
            os.chmod(path.parent, 0o750)
            os.chmod(path, 0o640)
            before = path.read_bytes()
            with self.assertRaises(ServerStoreError):
                _ = ServerStore.open(path.parent)
            self.assertTrue(before == path.read_bytes(), "future original changed")
            self.assertEqual([], list(path.parent.glob(".store-recovery-*")))
            self.assertFalse((path.parent / ".store-recovery.claim").exists())
            self.assertEqual((0o750, 0o640), (stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode)))

    def test_active_inspector_not_deleted_and_kill_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            fixture = OwnedSchemaRecoveryTests()
            path = fixture.seed(parent, 2)
            _ = fixture.crash(path)
            child = self._child(path, "hold")
            try:
                assert child.stdout is not None
                self.assertEqual("READY", cast(str, child.stdout.readline()).strip())
                slot = path.parent / ".store-recovery-candidate"
                inode = slot.stat().st_ino
                with self.assertRaisesRegex(ServerStoreError, "active"):
                    _ = ServerStore.open(path.parent)
                self.assertEqual(inode, slot.stat().st_ino)
            finally:
                child.kill()
                _ = child.communicate(timeout=10)
            store = ServerStore.open(path.parent)
            store.close()
            self.assertFalse(slot.exists())

    def test_interrupted_claim_directory_and_cleanup_states_recover_without_copy(self) -> None:
        magic = b"scarcity-router recovery claim\n"
        for state in ("stage", "planned", "directory", "owned", "cleaned"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as parent:
                path = OwnedSchemaRecoveryTests().seed(parent, 2)
                claim = path.parent / ".store-recovery.claim"
                slot = path.parent / ".store-recovery-candidate"
                if state == "stage":
                    stage = path.parent / ".store-recovery.claim-next"
                    _ = stage.write_bytes(magic[:5])
                    os.chmod(stage, 0o600)
                else:
                    payload = magic + b"P\n"
                    if state in ("directory", "owned", "cleaned"):
                        slot.mkdir(mode=0o700)
                    if state in ("owned", "cleaned"):
                        info = slot.stat()
                        payload = magic + b"O\n" + info.st_dev.to_bytes(16, "big") + info.st_ino.to_bytes(16, "big")
                    _ = claim.write_bytes(payload)
                    os.chmod(claim, 0o600)
                    if state == "owned":
                        candidate = slot / path.name
                        _ = candidate.write_bytes(b"SYNTHETIC-SECRET")
                        os.chmod(candidate, 0o600)
                    if state == "cleaned":
                        slot.rmdir()
                with mock.patch.object(ServerStore, "_inspect_interrupted_store") as inspection:
                    store = ServerStore.open(path.parent)
                store.close()
                inspection.assert_not_called()
                self.assertFalse(slot.exists())
                self.assertFalse(claim.exists())

    def test_unknown_symlink_type_mode_and_unattested_artifacts_are_not_adopted(self) -> None:
        for damage in ("guard_symlink", "guard_mode", "claim_symlink", "slot", "foreign", "claim", "lock_type"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as parent:
                path = OwnedSchemaRecoveryTests().seed(parent, 2)
                guard = path.parent / ".store-recovery.lock"
                claim = path.parent / ".store-recovery.claim"
                target = Path(parent) / "foreign"
                _ = target.write_bytes(b"SYNTHETIC-SECRET")
                if damage == "guard_symlink":
                    guard.unlink()
                    guard.symlink_to(target)
                elif damage == "guard_mode":
                    if os.name == "nt":
                        continue
                    os.chmod(guard, 0o644)
                elif damage == "claim_symlink":
                    claim.symlink_to(target)
                elif damage in ("slot", "foreign"):
                    (path.parent / (".store-recovery-candidate" if damage == "slot" else ".store-recovery-foreign")).mkdir(mode=0o700)
                elif damage == "claim":
                    _ = claim.write_bytes(b"unattested")
                    os.chmod(claim, 0o600)
                else:
                    guard.unlink()
                    guard.mkdir(mode=0o700)
                before = path.read_bytes(), target.read_bytes()
                with self.assertRaises(ServerStoreError):
                    _ = ServerStore.open(path.parent)
                self.assertTrue(before == (path.read_bytes(), target.read_bytes()), "foreign source changed")

    @unittest.skipUnless(os.name == "nt", "native Windows lock/publication requires Windows")
    def test_windows_native_killed_copy_restart(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            fixture = OwnedSchemaRecoveryTests()
            path = fixture.seed(parent, 2)
            _ = fixture.crash(path)
            child = self._child(path)
            try:
                self.assertEqual(0, child.wait(timeout=10))
            finally:
                _ = child.communicate(timeout=10)
            store = ServerStore.open(path.parent)
            store.close()
            self.assertEqual([], list(path.parent.glob(".store-recovery-*")))

    def test_foreign_lock_owner_refuses_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            path = OwnedSchemaRecoveryTests().seed(parent, 2)
            original = Path.lstat
            def foreign_owner(entry: Path) -> os.stat_result:
                info = original(entry)
                if entry.name == ".store-recovery.lock":
                    fields = list(info)
                    fields[4] = info.st_uid + 1
                    return os.stat_result(fields)
                return info
            before = path.read_bytes()
            with mock.patch.object(Path, "lstat", autospec=True, side_effect=foreign_owner):
                with self.assertRaisesRegex(ServerStoreError, "unsafe"):
                    _ = ServerStore.open(path.parent)
            self.assertTrue(before == path.read_bytes(), "foreign lock changed source")


class StoreTransitionTests(unittest.TestCase):
    _UPGRADE: str = """
import hashlib, json, os, sqlite3, sys
from pathlib import Path
path = Path(sys.argv[1])
c = sqlite3.connect(path, timeout=0, isolation_level=None)
try:
    c.execute('BEGIN IMMEDIATE')
    c.execute("INSERT INTO provider_secrets VALUES ('upgrade', 'committed', 'fixture')")
    c.execute("INSERT INTO schema_migrations VALUES (3, 'fixture')")
    c.execute('COMMIT')
except sqlite3.OperationalError as error:
    if (error.sqlite_errorcode & 255) != sqlite3.SQLITE_BUSY:
        raise
    if c.in_transaction:
        c.rollback()
    c.close()
    print(json.dumps({'committed': False}), flush=True)
    sys.exit(0)
if sys.argv[2] == 'hot':
    c.execute('PRAGMA cache_size=1')
    c.execute('BEGIN IMMEDIATE')
    c.execute("UPDATE audit_records SET payload=?", ('unfinished' * 1024,))
files = [Path(str(path) + suffix) for suffix in ('', '-wal', '-journal')]
print(json.dumps({'committed': True, 'digests': [
    hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in files
]}), flush=True)
os._exit(0)
"""

    def test_external_upgrade_after_preflight_preserves_refused_state_and_commits(self) -> None:
        self._assert_external_upgrade_transition(after_validation=1)

    def test_external_upgrade_after_writable_validation_preserves_refused_state_and_commits(self) -> None:
        self._assert_external_upgrade_transition(after_validation=2)

    def _assert_external_upgrade_transition(self, *, after_validation: int) -> None:
        inspect = ServerStore.applied_migration_versions
        for journal in ("wal", "hot"):
            for version in (1, 2):
                for migrate in (False, True):
                    with self.subTest(journal=journal, version=version, migrate=migrate), tempfile.TemporaryDirectory() as root:
                        fixture = OwnedSchemaRecoveryTests()
                        path = fixture.seed(root, version)
                        before = fixture.records(path)
                        if journal == "wal":
                            connection = sqlite3.connect(path)
                            try:
                                self.assertEqual(("wal",), connection.execute("PRAGMA journal_mode=WAL").fetchone())
                            finally:
                                connection.close()
                        transition: dict[str, object] = {}
                        validations = 0

                        def after_preflight(store: ServerStore) -> tuple[int, ...]:
                            nonlocal validations
                            result = inspect(store)
                            validations += 1
                            if validations == after_validation:
                                # A synchronous process completion is the barrier:
                                # no writable-open step runs before the upgrade ends.
                                child = subprocess.run(
                                    [sys.executable, "-c", self._UPGRADE, str(path), journal],
                                    env={"PATH": os.defpath, "HOME": root, "TMPDIR": root},
                                    capture_output=True, text=True, check=True, timeout=10,
                                )
                                transition.update(cast("dict[str, object]", json.loads(child.stdout)))
                            return result

                        refused = False
                        with mock.patch.object(ServerStore, "applied_migration_versions", after_preflight):
                            try:
                                store = ServerStore.open(path.parent, migrate=migrate)
                            except ServerStoreError:
                                refused = True
                            else:
                                store.close()
                        self.assertIn("committed", transition)
                        if transition["committed"]:
                            self.assertTrue(refused, "a committed future schema must refuse")
                            files = tuple(Path(str(path) + suffix) for suffix in ("", "-wal", "-journal"))
                            actual = [hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in files]
                            self.assertEqual(transition["digests"], actual, "refusal changed the external upgrade's recovery artifacts")
                        # Only after byte-preservation assertions may this test's
                        # newer SQLite client perform recovery/checkpointing.
                        connection = sqlite3.connect(path)
                        try:
                            self.assertIsNotNone(cast("tuple[int] | None", connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()))
                        finally:
                            connection.close()
                        after = fixture.records(path)
                        self.assertEqual(before[0], after[0])
                        expected_secrets = [*before[1]]
                        if transition["committed"]:
                            expected_secrets.append(("upgrade", "committed", "fixture"))
                        self.assertCountEqual(expected_secrets, after[1])
                        self.assertEqual(before[2:], after[2:])

    def test_hot_recovery_excludes_external_upgrade_and_revalidates_after_unlock(self) -> None:
        inspect = ServerStore.applied_migration_versions
        lock = cast(
            Callable[[ServerStore, Path], AbstractContextManager[None]],
            getattr(ServerStore, "_interrupted_store_lock"),
        )
        with tempfile.TemporaryDirectory() as root:
            fixture = OwnedSchemaRecoveryTests()
            path = fixture.seed(root, 2)
            before = fixture.records(path)
            _ = fixture.crash(path)
            attempts: list[dict[str, object]] = []

            def upgrade() -> dict[str, object]:
                child = subprocess.run(
                    [sys.executable, "-c", self._UPGRADE, str(path), "hot"],
                    env={"PATH": os.defpath, "HOME": root, "TMPDIR": root},
                    capture_output=True, text=True, check=True, timeout=10,
                )
                return cast("dict[str, object]", json.loads(child.stdout))

            def after_validation(store: ServerStore) -> tuple[int, ...]:
                result = inspect(store)
                attempts.append(upgrade())
                self.assertFalse(attempts[-1]["committed"], "hot recovery lost source exclusion")
                return result

            @contextmanager
            def after_unlock(store: ServerStore, entry: Path) -> Generator[None, None, None]:
                with lock(store, entry):
                    yield
                attempts.append(upgrade())
                self.assertTrue(attempts[-1]["committed"], "recovery source lock leaked")

            with mock.patch.object(ServerStore, "applied_migration_versions", after_validation), \
                    mock.patch.object(ServerStore, "_interrupted_store_lock", after_unlock):
                with self.assertRaises(ServerStoreError):
                    _ = ServerStore.open(path.parent)
            self.assertEqual([False, False, True], [attempt["committed"] for attempt in attempts])
            files = tuple(Path(str(path) + suffix) for suffix in ("", "-wal", "-journal"))
            self.assertEqual(attempts[-1]["digests"], [
                hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in files
            ])
            connection = sqlite3.connect(path)
            try:
                self.assertIsNotNone(cast("tuple[int] | None", connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()))
            finally:
                connection.close()
            after = fixture.records(path)
            self.assertEqual(before[0], after[0])
            self.assertCountEqual([*before[1], ("upgrade", "committed", "fixture")], after[1])
            self.assertEqual(before[2:], after[2:])

    def test_interrupted_hot_inspection_releases_locks_and_retains_source(self) -> None:
        inspect = ServerStore.applied_migration_versions
        for failure in (RuntimeError, KeyboardInterrupt):
            with self.subTest(failure=failure.__name__), tempfile.TemporaryDirectory() as root:
                fixture = OwnedSchemaRecoveryTests()
                path = fixture.seed(root, 2)
                records = fixture.records(path)
                journal = fixture.crash(path)
                before = path.read_bytes(), journal.read_bytes()

                def interrupted(store: ServerStore) -> tuple[int, ...]:
                    _ = inspect(store)
                    raise failure("synthetic interruption after candidate validation")

                with mock.patch.object(ServerStore, "applied_migration_versions", interrupted):
                    with self.assertRaises(failure):
                        _ = ServerStore.open(path.parent)
                self.assertEqual(before, (path.read_bytes(), journal.read_bytes()))
                store = ServerStore.open(path.parent)
                store.close()
                self.assertEqual(records, fixture.records(path))


class OwnedSchemaRecoveryTests(unittest.TestCase):
    def seed(self, parent: str, version: int) -> Path:
        return self._seed(parent, version)

    def records(self, path: Path) -> tuple[list[tuple[object, ...]], ...]:
        return self._records(path)

    def crash(self, path: Path) -> Path:
        return self._crash(path)

    def test_prepared_reserved_prefix_objects_refuse_cold_and_hot_without_source_effects(self) -> None:
        fixtures = {
            "trigger": "CREATE TRIGGER fixture_extra AFTER INSERT ON schema_migrations BEGIN DELETE FROM audit_records; END",
            "index": "CREATE INDEX fixture_extra ON audit_records(recorded_at)",
            "view": "CREATE VIEW fixture_extra AS SELECT recorded_at FROM audit_records",
            "table": "CREATE TABLE fixture_extra(value TEXT)",
        }
        for kind, ddl in fixtures.items():
            for version in (1, 2):
                for hot in (True, False):
                    for migrate in (True, False):
                        with self.subTest(kind=kind, version=version, hot=hot, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                            path = self._seed(parent, version)
                            c = sqlite3.connect(path)
                            try:
                                _ = c.execute(ddl)
                                _ = c.execute("PRAGMA writable_schema=ON")
                                _ = c.execute("UPDATE sqlite_schema SET name='sqlite_fixture_extra', tbl_name=replace(tbl_name,'fixture_extra','sqlite_fixture_extra'), sql=replace(sql,'fixture_extra','sqlite_fixture_extra') WHERE name='fixture_extra'")
                                _ = c.execute("PRAGMA writable_schema=OFF")
                                c.commit()
                            finally:
                                c.close()
                            c = sqlite3.connect(path)
                            try:
                                self.assertEqual((kind,), c.execute("SELECT type FROM sqlite_schema WHERE name='sqlite_fixture_extra'").fetchone())
                                self.assertEqual((30,), c.execute("SELECT COUNT(*) FROM audit_records").fetchone())
                            finally:
                                c.close()
                            before_records = self._records(path)
                            if hot:
                                _ = self._crash(path)
                            os.chmod(path.parent, 0o750)
                            os.chmod(path, 0o640)
                            files = (path, Path(str(path) + "-wal"), Path(str(path) + "-journal"))
                            before = tuple(p.read_bytes() if p.exists() else None for p in files)
                            with self.assertRaisesRegex(ServerStoreError, "owned schema is invalid"):
                                _ = ServerStore.open(path.parent, migrate=migrate)
                            self.assertTrue(before == tuple(p.read_bytes() if p.exists() else None for p in files), "reserved object changed source")
                            self.assertEqual((0o750, 0o640), (stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode)))
                            self.assertEqual([], list(path.parent.glob(".store-recovery-*")))
                            if not hot:
                                self.assertTrue(before_records == self._records(path), "reserved object lost retained records")

    def test_reference_generated_internal_objects_accept_known_quoted_case(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as parent:
                directory = Path(parent) / "server"
                raw = ServerStore.open(directory, migrate=False)
                path = raw.path
                raw.close()
                c = sqlite3.connect(path)
                try:
                    for number, statements in MIGRATIONS[:version]:
                        for statement in statements:
                            ddl = re.sub(r"CREATE TABLE (\w+)", lambda match: 'CREATE TABLE "' + match[1].upper() + '"', statement)
                            _ = c.execute(ddl)
                        _ = c.execute("INSERT INTO schema_migrations VALUES (?, ?)", (number, T0))
                    self.assertEqual((1,), c.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name='sqlite_sequence' AND type='table'").fetchone())
                    self.assertGreater(cast("tuple[int]", c.execute("SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND sql IS NULL").fetchone())[0], 0)
                    c.commit()
                finally:
                    c.close()
                store = ServerStore.open(directory)
                try:
                    self.assertEqual(2, store.schema_version())
                finally:
                    store.close()

    def test_native_quoted_constraint_keywords_refuse_cold_and_hot_before_effects(self) -> None:
        for version in (1, 2):
            for kind in ("primary", "notnull"):
                for hot in (True, False):
                    for migrate in (True, False):
                        with self.subTest(version=version, kind=kind, hot=hot, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                            path = self._seed(parent, version)
                            c = sqlite3.connect(path)
                            try:
                                original = cast("list[tuple[object, ...]]", c.execute("SELECT * FROM client_keys").fetchall())
                                _ = c.execute("DROP TABLE client_keys")
                                key = 'client_id TEXT "PRIMARY" "KEY"' if kind == "primary" else "client_id TEXT PRIMARY KEY"
                                verifier = 'key_hash TEXT "NOT" "NULL"' if kind == "notnull" else "key_hash TEXT NOT NULL"
                                _ = c.execute(f"CREATE TABLE client_keys({key}, {verifier}, label TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT)")
                                _ = c.executemany("INSERT INTO client_keys VALUES (?, ?, ?, ?, ?)", original)
                                if version == 1:
                                    _ = c.execute("INSERT INTO worker_pairings(worker_id,label,status,code_hash,created_at) VALUES ('synthetic','synthetic','pending','synthetic-hash',?)", (T0,))
                                columns = cast("list[tuple[object, ...]]", c.execute("PRAGMA table_info(client_keys)").fetchall())
                                self.assertEqual(0, columns[0][5] if kind == "primary" else columns[1][3])
                                c.commit()
                            finally:
                                c.close()
                            if hot:
                                _ = self._crash(path)
                            os.chmod(path.parent, 0o750)
                            os.chmod(path, 0o640)
                            files = (path, Path(str(path) + "-wal"), Path(str(path) + "-journal"))
                            before = tuple(p.read_bytes() if p.exists() else None for p in files)
                            with self.assertRaisesRegex(ServerStoreError, "owned schema is invalid"):
                                _ = ServerStore.open(path.parent, migrate=migrate)
                            for label, original_bytes, file_path in zip(("database", "WAL", "journal"), before, files, strict=True):
                                self.assertTrue(original_bytes == (file_path.read_bytes() if file_path.exists() else None), f"unsupported constraint changed {label}")
                            self.assertEqual((0o750, 0o640), (stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode)))
                            self.assertEqual([], list(path.parent.glob(".store-recovery-*")))
                            if not hot and version == 1:
                                c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                                try:
                                    self.assertEqual((1,), c.execute("SELECT COUNT(*) FROM worker_pairings").fetchone())
                                finally:
                                    c.close()

    def test_compiled_structure_accepts_quoted_identifiers_and_types(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as parent:
                directory = Path(parent) / "server"
                raw = ServerStore.open(directory, migrate=False)
                path = raw.path
                raw.close()
                c = sqlite3.connect(path)
                try:
                    for number, statements in MIGRATIONS[:version]:
                        for statement in statements:
                            ddl = re.sub(r"CREATE TABLE (\w+)", r'CREATE TABLE "\1"', statement)
                            ddl = re.sub(r"\b(client_id|key_hash|label|created_at|revoked_at|id|version|applied_at)\b", r'"\1"', ddl)
                            _ = c.execute(ddl.replace("TEXT", '"TEXT"'))
                        _ = c.execute("INSERT INTO schema_migrations VALUES (?, ?)", (number, T0))
                    c.commit()
                finally:
                    c.close()
                store = ServerStore.open(directory)
                try:
                    self.assertEqual(2, store.schema_version())
                finally:
                    store.close()

    def test_quoted_primary_key_refusal_preserves_committed_wal(self) -> None:
        writer = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
c.execute("UPDATE client_keys SET label='committed'")
c.commit()
os._exit(0)
"""
        for version in (1, 2):
            for migrate in (True, False):
                with self.subTest(version=version, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                    path = self._seed(parent, version)
                    c = sqlite3.connect(path)
                    try:
                        _ = c.execute("DROP TABLE client_keys")
                        _ = c.execute('CREATE TABLE client_keys(client_id TEXT "PRIMARY" "KEY", key_hash TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT)')
                        _ = c.execute("INSERT INTO client_keys VALUES ('synthetic','synthetic-hash','synthetic',?,NULL)", (T0,))
                        c.commit()
                    finally:
                        c.close()
                    _ = subprocess.run([sys.executable, "-c", writer, str(path)],
                                       env={"PATH": os.defpath, "HOME": parent, "TMPDIR": parent},
                                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    wal = Path(str(path) + "-wal")
                    self.assertGreater(wal.stat().st_size, 0)
                    os.chmod(path.parent, 0o750)
                    os.chmod(path, 0o640)
                    before = path.read_bytes(), wal.read_bytes()
                    with self.assertRaisesRegex(ServerStoreError, "owned schema is invalid"):
                        _ = ServerStore.open(path.parent, migrate=migrate)
                    self.assertTrue(before == (path.read_bytes(), wal.read_bytes()), "refusal changed committed WAL")
                    self.assertEqual((0o750, 0o640), (stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode)))

    _WRITER: str = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA synchronous=FULL')
c.execute('PRAGMA cache_size=1')
c.execute('BEGIN IMMEDIATE')
c.execute('UPDATE audit_records SET payload=?', ('unfinished' * 1024,))
c.execute("UPDATE server_configuration SET document='unfinished'")
c.execute("UPDATE provider_secrets SET secret='unfinished'")
os._exit(0)
"""

    def _seed(self, parent: str, version: int) -> Path:
        directory = Path(parent) / "server"
        raw = ServerStore.open(directory, migrate=False)
        path = raw.path
        raw.close()
        connection = sqlite3.connect(path)
        try:
            for number, statements in MIGRATIONS[:version]:
                for statement in statements:
                    _ = connection.execute(statement)
                _ = connection.execute("INSERT INTO schema_migrations VALUES (?, ?)", (number, T0))
            _ = connection.execute("INSERT INTO server_configuration VALUES (1, '{}', ?)", (T0,))
            _ = connection.execute("INSERT INTO provider_secrets VALUES ('synthetic', 'SYNTHETIC-SECRET', ?)", (T0,))
            _ = connection.execute("INSERT INTO client_keys VALUES ('synthetic', 'synthetic-hash', 'synthetic', ?, NULL)", (T0,))
            payload = json.dumps({"pin": "synthetic-pin", "padding": "x" * 8192})
            _ = connection.executemany("INSERT INTO audit_records(recorded_at,payload) VALUES (?, ?)", [(T0, payload)] * 30)
            connection.commit()
        finally:
            connection.close()
        return path

    def _crash(self, path: Path) -> Path:
        _ = subprocess.run([sys.executable, "-c", self._WRITER, str(path)],
                           env={"PATH": os.defpath, "HOME": str(path.parent), "TMPDIR": str(path.parent)},
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        journal = Path(str(path) + "-journal")
        self.assertTrue(journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7"), "journal is not genuinely hot")
        before = path.read_bytes(), journal.read_bytes()
        reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError) as caught:
                _ = reader.execute("SELECT version FROM schema_migrations")
            self.assertEqual(sqlite3.SQLITE_READONLY_ROLLBACK, caught.exception.sqlite_errorcode)
        finally:
            reader.close()
        self.assertTrue(before == (path.read_bytes(), journal.read_bytes()), "inspection recovered source")
        return journal

    def _records(self, path: Path) -> tuple[list[tuple[object, ...]], ...]:
        reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return tuple(cast("list[tuple[object, ...]]", reader.execute(sql).fetchall()) for sql in (
                "SELECT * FROM server_configuration", "SELECT * FROM provider_secrets",
                "SELECT * FROM client_keys", "SELECT * FROM audit_records ORDER BY seq",
            ))
        finally:
            reader.close()

    def test_supported_hot_rollback_recovers_unrelated_state_without_operator_step(self) -> None:
        for version in (1, 2):
            for migrate in (True, False):
                with self.subTest(version=version, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                    path = self._seed(parent, version)
                    before = self._records(path)
                    _ = self._crash(path)
                    store = ServerStore.open(path.parent, migrate=migrate)
                    try:
                        self.assertEqual(2 if migrate else version, store.schema_version())
                        self.assertEqual("SYNTHETIC-SECRET", store.get_provider_secret("synthetic"))
                        self.assertEqual(30, store.audit_record_count())
                    finally:
                        store.close()
                    self.assertTrue(before == self._records(path), "rollback lost unrelated records")
                    self.assertEqual([], list(path.parent.glob(".store-recovery-*")))
                    self.assertEqual((0o700, 0o600), (
                        stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode),
                    ))

    def test_unknown_or_invalid_hot_store_is_never_recovered_in_place(self) -> None:
        for damage in ("future", "gap", "trigger", "missing", "incompatible"):
            for migrate in (True, False):
                with self.subTest(damage=damage, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                    path = self._seed(parent, 1 if damage == "trigger" else 2)
                    connection = sqlite3.connect(path)
                    try:
                        if damage == "future":
                            _ = connection.execute("INSERT INTO schema_migrations VALUES (3, ?)", (T0,))
                        elif damage == "gap":
                            _ = connection.execute("DELETE FROM schema_migrations WHERE version=1")
                        elif damage == "trigger":
                            _ = connection.execute("CREATE TRIGGER erase_audit AFTER INSERT ON schema_migrations BEGIN DELETE FROM audit_records; END")
                        elif damage == "missing":
                            _ = connection.execute("DROP TABLE client_keys")
                        else:
                            _ = connection.execute("ALTER TABLE client_keys ADD COLUMN unauthorized TEXT")
                        connection.commit()
                    finally:
                        connection.close()
                    journal = self._crash(path)
                    os.chmod(path.parent, 0o750)
                    os.chmod(path, 0o640)
                    before = path.read_bytes(), journal.read_bytes()
                    with self.assertRaises(ServerStoreError) as caught:
                        _ = ServerStore.open(path.parent, migrate=migrate)
                    self.assertNotIn("SYNTHETIC-SECRET", str(caught.exception))
                    self.assertTrue(before == (path.read_bytes(), journal.read_bytes()), "invalid source recovered or changed")
                    self.assertEqual((0o750, 0o640), (
                        stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode),
                    ))
                    self.assertEqual([], list(path.parent.glob(".store-recovery-*")))

    def test_owned_schema_trigger_missing_columns_constraints_and_timestamp_refuse_before_effects(self) -> None:
        for damage in ("trigger", "missing", "column", "constraint", "timestamp"):
            for migrate in (True, False):
                with self.subTest(damage=damage, migrate=migrate), tempfile.TemporaryDirectory() as parent:
                    path = self._seed(parent, 1)
                    connection = sqlite3.connect(path)
                    try:
                        if damage == "trigger":
                            _ = connection.execute("CREATE TRIGGER erase_audit AFTER INSERT ON schema_migrations BEGIN DELETE FROM audit_records; END")
                        elif damage == "missing":
                            _ = connection.execute("DROP TABLE worker_pairings")
                        elif damage == "column":
                            _ = connection.execute("ALTER TABLE provider_secrets ADD COLUMN unauthorized TEXT")
                        elif damage == "constraint":
                            _ = connection.execute("DROP TABLE server_configuration")
                            _ = connection.execute("CREATE TABLE server_configuration(id INTEGER PRIMARY KEY, document TEXT NOT NULL, updated_at TEXT NOT NULL)")
                        else:
                            _ = connection.execute("UPDATE schema_migrations SET applied_at=?", (b"SYNTHETIC-SECRET",))
                        connection.commit()
                        self.assertEqual(("wal",), connection.execute("PRAGMA journal_mode=WAL").fetchone())
                    finally:
                        connection.close()
                    before_records = self._records(path)
                    os.chmod(path.parent, 0o750)
                    os.chmod(path, 0o640)
                    before = path.read_bytes()
                    with self.assertRaises(ServerStoreError) as caught:
                        _ = ServerStore.open(path.parent, migrate=migrate)
                    self.assertNotIn("SYNTHETIC-SECRET", str(caught.exception))
                    self.assertTrue(before == path.read_bytes(), "invalid owned schema changed")
                    self.assertTrue(before_records == self._records(path), "invalid schema lost unrelated records")
                    self.assertEqual((0o750, 0o640), (
                        stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode),
                    ))
                    reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                    try:
                        self.assertEqual(("wal",), reader.execute("PRAGMA journal_mode").fetchone())
                    finally:
                        reader.close()

    def test_schema_format_case_and_quoted_identifiers_are_not_new_versions(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "server"
            raw = ServerStore.open(directory, migrate=False)
            path = raw.path
            raw.close()
            connection = sqlite3.connect(path)
            try:
                for statement in MIGRATIONS[0][1]:
                    ddl = re.sub(r"CREATE TABLE (\w+)", r'create table "\1"', statement)
                    _ = connection.execute(ddl.replace("INTEGER", "integer").replace("TEXT", "text"))
                _ = connection.execute("INSERT INTO schema_migrations VALUES (1, ?)", (T0,))
                connection.commit()
            finally:
                connection.close()
            with mock.patch.object(ServerStore, "_recovery_slot") as temporary:
                store = ServerStore.open(directory)
            temporary.assert_not_called()
            self.addCleanup(store.close)
            self.assertEqual(2, store.schema_version())

    def test_plain_readonly_failure_does_not_authorize_candidate_or_original_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            path = self._seed(parent, 2)
            error = sqlite3.OperationalError("synthetic readonly failure")
            error.sqlite_errorcode = sqlite3.SQLITE_READONLY
            with mock.patch("scarcity_router.server_store._fetch_rows", side_effect=error), \
                    mock.patch.object(ServerStore, "_inspect_interrupted_store") as recovery:
                with self.assertRaises(ServerStoreError):
                    _ = ServerStore.open(path.parent)
            recovery.assert_not_called()

    def test_private_recovery_copies_are_permissioned_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            path = self._seed(parent, 2)
            _ = self._crash(path)
            copier = shutil.copyfileobj
            def checked_copy(reader: BinaryIO, writer: BinaryIO) -> None:
                self.assertEqual(0o700, stat.S_IMODE(next(path.parent.glob(".store-recovery-*")).stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE(os.fstat(writer.fileno()).st_mode))
                copier(reader, writer)
            with mock.patch("scarcity_router.server_store.shutil.copyfileobj", side_effect=checked_copy):
                store = ServerStore.open(path.parent)
            store.close()
            self.assertEqual([], list(path.parent.glob(".store-recovery-*")))

    def test_hot_recovery_rejects_symlink_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            path = self._seed(parent, 2)
            journal = self._crash(path)
            target = Path(parent) / "owned-other-journal"
            _ = journal.rename(target)
            journal.symlink_to(target)
            before = path.read_bytes(), target.read_bytes()
            with self.assertRaises(ServerStoreError):
                _ = ServerStore.open(path.parent)
            self.assertTrue(before == (path.read_bytes(), target.read_bytes()), "symlink target changed")
            self.assertTrue(journal.is_symlink())
            self.assertEqual([], list(path.parent.glob(".store-recovery-*")))

    def test_changed_source_refuses_before_original_recovery_and_cleans_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            path = self._seed(parent, 2)
            journal = self._crash(path)
            before = path.read_bytes(), journal.read_bytes()
            copier = shutil.copyfileobj
            changed = False
            def changing_copy(reader: BinaryIO, writer: BinaryIO) -> None:
                nonlocal changed
                copier(reader, writer)
                if not changed:
                    info = path.stat()
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
                    changed = True
            with mock.patch("scarcity_router.server_store.shutil.copyfileobj", side_effect=changing_copy):
                with self.assertRaisesRegex(ServerStoreError, "changed"):
                    _ = ServerStore.open(path.parent)
            self.assertTrue(before == (path.read_bytes(), journal.read_bytes()), "original recovered after source changed")
            self.assertEqual([], list(path.parent.glob(".store-recovery-*")))


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
