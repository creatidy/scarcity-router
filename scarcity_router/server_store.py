"""The server component's durable store (M09, issue #94; D-041/D-044).

One embedded SQLite-class single-file store inside the server's data
directory holds exactly the durable state D-041 assigns to the server:
administrator configuration state, identities (administrator, inference
client keys), provider credentials (the bounded D-044 exception) and the
bounded audit trail. Worker pairing/identity state is NOT stored here:
since the M04/M05/M09 integration there is exactly ONE pairing system —
the M05 worker identity store (`worker_identity_store.py`, a separate
permissioned database beside this file) — and this store's superseded
duplicate worker-pairing tables were dropped by the explicit schema
version 2 migration. No external database, cache or message-queue
service is introduced; recommendation-only mode keeps today's
zero-durable-state behavior.

Discipline:

- **Server-internal schema.** The schema is not a public serialized
  contract. Every change lands as an explicit, tested migration recorded
  in :data:`MIGRATIONS`; the applied set lives in ``schema_migrations``
  and unknown newer files fail closed rather than being half-read.
- **Crash safety.** Writes run inside transactions with
  ``synchronous=FULL``; a crash leaves either the previous or the new
  state, never a partial one. Authentication secrets (password verifiers,
  session tokens, client keys, worker tokens, pairing codes) are stored
  only as SHA-256 hashes (or PBKDF2 verifiers); provider credentials are
  the sole plaintext secrets and live in one dedicated table that no
  export, diagnostic or listing path reads.
- **Permissioned file fallback.** The store directory is ``0o700`` and
  the database file is forced to ``0o600`` (D-044's recorded fallback;
  OS-native storage remains the preferred alternative where a deployment
  provides it). The ``0o700`` directory closes the creation-time window
  before ``chmod``.
- **No prompt/response content.** Nothing in this store ever holds a
  prompt, a response body or a raw provider payload; the audit table
  carries only the D-043 metadata set as serialized by
  :mod:`scarcity_router.gateway_audit`.

All timestamps are caller-supplied canonical instants so behavior stays
deterministic in tests. A single connection guarded by one lock serves
all threads (the server's durable writes are administrative and rare).
"""

from __future__ import annotations

import json
import os
import errno
import re
import shutil
import sqlite3
import stat
import time
import threading
import weakref
from contextlib import contextmanager
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from .gateway_validation import v_safe_id, v_str

STORE_SCHEMA_VERSION = 2

STORE_FILE_NAME = "server-state.sqlite3"
DATA_DIR_NAME = "scarcity-router"
SERVER_DATA_DIR_NAME = "server"


@dataclass(slots=True)
class _DatabaseDescriptorLease:
    """Keep a raw validation descriptor open while SQLite connections live."""

    descriptors: list[int]
    references: int

    @property
    def fd(self) -> int:
        return self.descriptors[0]


_DATABASE_DESCRIPTOR_LEASES_LOCK = threading.RLock()
_DATABASE_DESCRIPTOR_LEASES: dict[tuple[int, int], _DatabaseDescriptorLease] = {}


def _release_database_descriptor_lease(key: tuple[int, int]) -> None:
    """Close validation descriptors only after every store connection closes.

    POSIX record locks are process-owned. Closing any descriptor for an inode
    can release locks held by a different SQLite connection in this process,
    so the validation descriptor must share the managed connection lifetime.
    """
    with _DATABASE_DESCRIPTOR_LEASES_LOCK:
        lease = _DATABASE_DESCRIPTOR_LEASES.get(key)
        if lease is None:
            return
        lease.references -= 1
        if lease.references < 0:
            raise RuntimeError("server store descriptor lease underflow")
        if lease.references:
            return
        for fd in lease.descriptors:
            try:
                os.close(fd)
            except OSError:
                # The descriptor is no longer associated with a live SQLite
                # connection. A close error must not release another lease.
                pass
        del _DATABASE_DESCRIPTOR_LEASES[key]


def _close_connection_and_release_lease(
    connection: sqlite3.Connection, key: tuple[int, int]
) -> None:
    connection.close()
    _release_database_descriptor_lease(key)

# Each migration is (version, statements). Versions are applied in order
# inside one transaction for the pending set; the recorded versions advance
# only when the whole upgrade succeeded (crash-safe by construction).
#
# Version 2 (M04/M05/M09 integration): the superseded M09-side
# ``worker_pairings`` table is DROPPED. M09 built an administration-facing
# pairing store before M05's transport existed; the wave integrates on M05
# as the SOLE pairing system (its own store in ``worker_identity_store.py``
# beside this file), and no data conversion exists: that table held only
# M09-format code/token HASHES whose redemption path is retired, while M05
# identities hash with a per-store pepper salt that cannot reproduce them.
# Pending codes and revoked rows carry no convertible state. Every
# UNRELATED table (configuration, administrator identity, sessions, client
# keys, provider secrets, audit) is untouched by the migration.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE server_configuration (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                document TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE administrator_identity (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                password_salt_hex TEXT NOT NULL,
                password_hash_hex TEXT NOT NULL,
                pbkdf2_iterations INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE admin_sessions (
                session_hash TEXT PRIMARY KEY,
                csrf_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE client_keys (
                client_id TEXT PRIMARY KEY,
                key_hash TEXT NOT NULL,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """,
            """
            CREATE TABLE provider_secrets (
                provider_id TEXT PRIMARY KEY,
                secret TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE worker_pairings (
                worker_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('pending', 'paired', 'revoked')),
                code_hash TEXT,
                token_hash TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                last_connected_at TEXT
            )
            """,
            """
            CREATE TABLE audit_records (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
        ),
    ),
    # Version 2: retire the superseded duplicate pairing surface (the M05
    # worker identity store is the single pairing system). See the
    # migration-list comment above for why the rows are dropped, not
    # converted, and why no unrelated data is affected.
    (
        2,
        (
            "DROP TABLE IF EXISTS worker_pairings",
        ),
    ),
)

_AUDIT_HARD_CAP = 100_000
_RECOVERY_SLOT = ".store-recovery-candidate"
_RECOVERY_CLAIM = ".store-recovery.claim"
_RECOVERY_NEXT = ".store-recovery.claim-next"
_RECOVERY_GUARD = ".store-recovery.lock"
_RECOVERY_MAGIC = b"scarcity-router recovery claim\n"


class ServerStoreError(ValueError):
    """Raised for store-level failures (unreadable file, bad migration state)."""


class _HotRollbackRequired(ServerStoreError):
    """Private signal for SQLite's precise read-only rollback condition."""


def _ascii_lower(text: str) -> str:
    return text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def _schema_tokens(sql: str) -> tuple[str, ...]:
    """Compare the owned DDL, ignoring formatting but preserving literals."""
    tokens = cast("list[str]", re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/|\w+|[^\s]", sql, re.DOTALL))
    normalized: list[str] = []
    for token in tokens:
        if token.startswith(("--", "/*")):
            continue
        if token.startswith("'"):
            normalized.append(token)
            continue
        if len(token) >= 2 and token[0] in ('"', "`") and token[-1] == token[0]:
            delimiter = token[0]
            token = token[1:-1].replace(delimiter * 2, delimiter)
        elif token.startswith("[") and token.endswith("]"):
            token = token[1:-1]
        normalized.append(_ascii_lower(token))
    return tuple(normalized)


@dataclass(frozen=True)
class AdminIdentityRecord:
    """The stored administrator password verifier (never a password)."""

    password_salt_hex: str
    password_hash_hex: str
    pbkdf2_iterations: int
    created_at: str


@dataclass(frozen=True)
class ClientKeyRecord:
    """One inference-client key entry (hash only, never key material)."""

    client_id: str
    key_hash: str
    label: str
    created_at: str
    revoked_at: str | None


def default_server_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """The default server data directory (XDG ``data home`` based).

    ``$(XDG_DATA_HOME or ~/.local/share)/scarcity-router/server/`` — the
    same XDG discipline as the D-036 user configuration directory, kept
    separate from it: that directory holds the neutral user selector
    policy, this one holds the deployed server's durable state.
    """
    environment = os.environ if env is None else env
    raw = environment.get("XDG_DATA_HOME", "")
    if raw and os.path.isabs(raw):
        base = Path(raw)
    else:
        home = environment.get("HOME", "")
        base = (Path(home).expanduser() if home else Path.home()) / ".local" / "share"
    return base / DATA_DIR_NAME / SERVER_DATA_DIR_NAME


def canonical_store_path(data_dir: Path) -> Path:
    """The store file inside a server data directory."""
    return data_dir / STORE_FILE_NAME


class ServerStore:
    """The single durable store of one server component instance."""

    _path: Path
    _lock: threading.RLock
    _connection: sqlite3.Connection
    _parent_info: os.stat_result
    _lease_key: tuple[int, int] | None
    _database_fd: int | None
    _close_finalizer: weakref.finalize[..., ServerStore] | None

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._lease_key = None
        self._database_fd = None
        self._close_finalizer = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Directory aliases are supported; the final database entry is not
            # resolved, so a file link cannot redirect inspection or chmod.
            path = path.parent.resolve() / path.name
            parent_info = self._store_info(path.parent, directory=True)
            assert parent_info is not None
            self._parent_info = parent_info
            expected = self._store_info(path)
            _ = self._check_store_entry(path, expected)
            with self._recovery_guard(path, expected) as expected:
                _ = self._check_store_entry(path, expected)
                lease_key, fd, expected = self._acquire_database_descriptor(path, expected)
                self._lease_key = lease_key
                self._database_fd = fd
                _ = self._check_store_entry(path, expected)
                self._connection = self._open_store(path, expected, fd)
                self._close_finalizer = weakref.finalize(
                    self,
                    _close_connection_and_release_lease,
                    self._connection,
                    lease_key,
                )
        except OSError:
            self._cleanup_failed_open()
            raise ServerStoreError("server store filesystem access is unavailable; refusing access") from None
        except BaseException:
            self._cleanup_failed_open()
            raise

    def _store_info(self, path: Path, *, directory: bool = False) -> os.stat_result | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if directory:
                raise
            return None
        reparse = cast(int, getattr(info, "st_file_attributes", 0)) & cast(int, getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        owner = os.geteuid() if os.name != "nt" else path.parent.stat().st_uid
        if reparse or not valid_type or info.st_uid != owner or (not directory and info.st_nlink != 1):
            raise ServerStoreError("unsafe server store entry; administrator inspection required")
        return info

    def _check_store_entry(self, path: Path, expected: os.stat_result | None) -> os.stat_result | None:
        parent = self._store_info(path.parent, directory=True)
        if parent is None or not os.path.samestat(parent, self._parent_info):
            raise ServerStoreError("server store directory identity changed; refusing access")
        current = self._store_info(path)
        if (current is None) != (expected is None) or (
            current is not None and expected is not None and not os.path.samestat(current, expected)
        ):
            raise ServerStoreError("server store entry identity changed; refusing access")
        # SQLite can modify existing sidecars even during a read-only open.
        for suffix in ("-journal", "-wal", "-shm"):
            _ = self._store_info(path.with_name(path.name + suffix))
        return current

    def _acquire_database_descriptor(
        self, path: Path, expected: os.stat_result | None
    ) -> tuple[tuple[int, int], int, os.stat_result]:
        """Acquire a per-inode descriptor lease without closing live-store fds.

        POSIX record locks are process-owned; closing any descriptor for a
        database inode can release locks held by a different SQLite
        connection in this process. Reuse the existing descriptor for the
        inode and defer its close until the last ``ServerStore`` connection
        has closed.
        """
        with _DATABASE_DESCRIPTOR_LEASES_LOCK:
            if expected is not None:
                key = (expected.st_dev, expected.st_ino)
                lease = _DATABASE_DESCRIPTOR_LEASES.get(key)
                if lease is not None:
                    opened = os.fstat(lease.fd)
                    if (
                        not os.path.samestat(opened, expected)
                        or not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                    ):
                        raise ServerStoreError(
                            "server store entry identity changed; refusing access"
                        )
                    current = self._check_store_entry(path, expected)
                    if current is None or not os.path.samestat(opened, current):
                        raise ServerStoreError(
                            "server store entry identity changed; refusing access"
                        )
                    lease.references += 1
                    return key, lease.fd, opened

            flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            flags |= os.O_RDONLY if expected is not None else os.O_RDWR | os.O_CREAT | os.O_EXCL
            fd = os.open(path, flags, 0o600)
            registered = False
            opened: os.stat_result | None = None
            try:
                opened = os.fstat(fd)
                if expected is None:
                    expected = opened
                if (
                    not os.path.samestat(opened, expected)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                ):
                    raise ServerStoreError(
                        "server store entry identity changed; refusing access"
                    )
                current = self._check_store_entry(path, expected)
                if current is None or not os.path.samestat(opened, current):
                    raise ServerStoreError(
                        "server store entry identity changed; refusing access"
                    )

                key = (opened.st_dev, opened.st_ino)
                existing = _DATABASE_DESCRIPTOR_LEASES.get(key)
                if existing is not None:
                    existing_info = os.fstat(existing.fd)
                    if not os.path.samestat(existing_info, opened):
                        raise ServerStoreError(
                            "server store descriptor lease identity changed"
                        )
                    # A path replacement raced the initial stat. Do not close
                    # this descriptor while another managed SQLite connection
                    # may hold process-scoped locks on its inode.
                    existing.descriptors.append(fd)
                    existing.references += 1
                    registered = True
                    return key, existing.fd, opened

                _DATABASE_DESCRIPTOR_LEASES[key] = _DatabaseDescriptorLease(
                    descriptors=[fd], references=1
                )
                registered = True
                return key, fd, opened
            finally:
                if not registered:
                    if opened is None:
                        os.close(fd)
                    else:
                        # If validation failed after a raced replacement, keep
                        # the descriptor attached to any existing lease rather
                        # than closing an inode another store still uses.
                        existing = _DATABASE_DESCRIPTOR_LEASES.get(
                            (opened.st_dev, opened.st_ino)
                        )
                        if existing is None:
                            os.close(fd)
                        else:
                            existing.descriptors.append(fd)

    def _cleanup_failed_open(self) -> None:
        connection = getattr(self, "_connection", None)
        if isinstance(connection, sqlite3.Connection):
            try:
                connection.close()
            except sqlite3.Error:
                # Keep the descriptor lease pinned if SQLite did not close.
                return
        if self._lease_key is not None:
            _release_database_descriptor_lease(self._lease_key)
            self._lease_key = None
            self._database_fd = None

    def _database_lease_references(self) -> int:
        if self._lease_key is None:
            return 0
        with _DATABASE_DESCRIPTOR_LEASES_LOCK:
            lease = _DATABASE_DESCRIPTOR_LEASES.get(self._lease_key)
            return lease.references if lease is not None else 0

    def _open_store(self, path: Path, expected: os.stat_result, fd: int) -> sqlite3.Connection:
        parent = path.parent
        # sqlite3 opens filenames, not validated descriptors. The raw
        # validation descriptor is leased across all managed connections to
        # this inode because an independent close can release their POSIX locks.
        _ = self._check_store_entry(path, expected)
        # Closing the last writable WAL reader can checkpoint even on a
        # refusal. Inspect stores read-only first, including WAL.
        try:
            self._connection = sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True,
                check_same_thread=False, isolation_level=None,
            )
        except sqlite3.Error:
            raise ServerStoreError(
                "server store migration metadata is invalid or unreadable; refusing repair"
            ) from None
        self._connection.row_factory = sqlite3.Row
        try:
            _ = self._check_store_entry(path, expected)
            try:
                _ = self.applied_migration_versions()
            except _HotRollbackRequired:
                self._connection.close()
                self._inspect_interrupted_store(path)
        finally:
            self._connection.close()
        _ = self._check_store_entry(path, expected)
        try:
            self._connection = sqlite3.connect(
                str(path), check_same_thread=False, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise ServerStoreError(f"server store is not usable: {exc}") from None
        try:
            _ = self._check_store_entry(path, expected)
            self._connection.row_factory = sqlite3.Row
            # Inspect before changing permissions or persistent journal mode.
            # Supported old stores still migrate; invalid history and future
            # stores refuse even through the raw test seam.
            _ = self.applied_migration_versions()
            _ = self._check_store_entry(path, expected)
            _ = os.chmod(parent, 0o700)
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            else:
                os.chmod(path, 0o600)
            _ = self._check_store_entry(path, expected)
            _ = self._connection.execute("PRAGMA journal_mode=DELETE")
            _ = self._connection.execute("PRAGMA synchronous=FULL")
            _ = self._connection.execute("PRAGMA foreign_keys=ON")
        except ServerStoreError:
            self._connection.close()
            raise
        except (sqlite3.Error, OSError) as exc:
            self._connection.close()
            raise ServerStoreError(f"server store is not usable: {exc}") from None
        return self._connection

    # ── Lifecycle and schema ──────────────────────────────────────────────

    @classmethod
    def open(
        cls, data_dir: Path, *, migrate: bool = True
    ) -> "ServerStore":
        """Open (creating if needed) the store inside ``data_dir``.

        With ``migrate`` (the production default) pending migrations apply
        before the store is returned. With ``migrate=False`` a fresh or
        un-migrated file opens raw — the migration-test seam; every other
        use of an un-migrated store raises on first statement.
        """
        store = cls(canonical_store_path(data_dir))
        try:
            if migrate:
                _ = store.migrate()
                _ = store.require_current_schema()
        except ServerStoreError:
            store.close()
            raise
        return store

    def close(self) -> None:
        with self._lock:
            finalizer = self._close_finalizer
            if finalizer is not None and finalizer.alive:
                # The finalizer closes SQLite first, then releases the shared
                # raw descriptor lease. Never reverse that ownership order.
                _ = finalizer()

    @property
    def path(self) -> Path:
        return self._path

    def schema_version(self) -> int:
        """The applied schema version (0 for an un-migrated store)."""
        versions = self.applied_migration_versions()
        return versions[-1] if versions else 0

    def applied_migration_versions(self) -> tuple[int, ...]:
        """Read and validate history; only an empty new database has version 0."""
        with self._lock:
            began_read = False
            try:
                if not self._connection.in_transaction:
                    _ = self._connection.execute("BEGIN")
                    began_read = True
                objects = _fetch_rows(self._connection,
                    "SELECT name, type, tbl_name, sql FROM sqlite_schema"
                )
                metadata = tuple(row for row in objects if _ascii_lower(cast(str, row["name"])) == "schema_migrations")
                if not metadata and not objects:
                    return ()
                if len(metadata) != 1 or metadata[0]["type"] != "table" or type(cast(object, metadata[0]["sql"])) is not str:
                    raise ServerStoreError(
                        "server store migration metadata is invalid; refusing repair"
                    )
                if _schema_tokens(cast(str, metadata[0]["sql"])) != _schema_tokens(MIGRATIONS[0][1][0]):
                    raise ServerStoreError("server store migration metadata is invalid; refusing repair")
                columns = _fetch_rows(self._connection, "PRAGMA table_info(schema_migrations)")
                if tuple((_ascii_lower(cast(str, row["name"])), _ascii_lower(cast(str, row["type"])), row["notnull"], row["pk"]) for row in columns) != (
                    ("version", "integer", 0, 1), ("applied_at", "text", 1, 0),
                ):
                    raise ServerStoreError(
                        "server store migration metadata is invalid; refusing repair"
                    )
                rows = _fetch_rows(self._connection,
                    "SELECT version, applied_at FROM schema_migrations ORDER BY version"
                )
                versions: list[int] = []
                for row in rows:
                    raw = cast(object, row["version"])
                    if type(raw) is not int or raw < 1 or type(cast(object, row["applied_at"])) is not str:
                        raise ServerStoreError(
                            "server store migration metadata is invalid; refusing repair"
                        )
                    versions.append(raw)
                if versions and versions[-1] > STORE_SCHEMA_VERSION:
                    raise ServerStoreError(
                        "server store schema is newer than this program understands; refusing to read a future store"
                    )
                applied = tuple(versions)
                expected = tuple(version for version, _ in MIGRATIONS)
                if not applied or applied != expected[:len(applied)]:
                    raise ServerStoreError(
                        "server store migration history is invalid; refusing repair"
                    )
                self._validate_owned_schema(applied[-1], objects)
                return applied
            except sqlite3.Error as exc:
                if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY_ROLLBACK:
                    raise _HotRollbackRequired(
                        "server store requires validated interrupted-transaction recovery"
                    ) from None
                raise ServerStoreError(
                    "server store migration metadata is invalid or unreadable; refusing repair"
                ) from None
            finally:
                if began_read:
                    _ = self._connection.execute("ROLLBACK")

    def _validate_owned_schema(self, version: int, objects: list[sqlite3.Row]) -> None:
        # Only trusted migration SQL runs here, in memory. Actual definitions
        # are compared, never executed; triggers/views cannot authorize writes.
        reference = sqlite3.connect(":memory:")
        reference.row_factory = sqlite3.Row
        try:
            for number, statements in MIGRATIONS:
                if number > version:
                    break
                for statement in statements:
                    _ = reference.execute(statement)
            expected = _fetch_rows(reference,
                "SELECT name, type, tbl_name, sql FROM sqlite_schema"
            )

            def definition(row: sqlite3.Row) -> tuple[object, ...]:
                sql = cast(object, row["sql"])
                if sql is not None and not isinstance(sql, str):
                    raise ServerStoreError("server store owned schema is invalid; refusing repair")
                return (_ascii_lower(cast(str, row["tbl_name"])),
                        _schema_tokens(sql) if isinstance(sql, str) else None)

            # Generated names are admitted only when this reference creates
            # that exact object; a reserved-looking prefix grants no exemption.
            wanted = {(_ascii_lower(cast(str, row["name"])), cast(str, row["type"])):
                      definition(row) for row in expected}
            actual = {(_ascii_lower(cast(str, row["name"])), cast(str, row["type"])):
                      definition(row) for row in objects}
            if actual != wanted:
                raise ServerStoreError("server store owned schema is invalid; refusing repair")

            def compiled_rows(connection: sqlite3.Connection, pragma: str, name: str,
                              fields: tuple[str, ...], folded: frozenset[str]) -> tuple[tuple[object, ...], ...]:
                quoted = '"' + name.replace('"', '""') + '"'
                rows = _fetch_rows(connection, f"PRAGMA {pragma}({quoted})")
                return tuple(tuple(
                    _ascii_lower(cast(str, row[field]))
                    if field in folded and isinstance(cast(object, row[field]), str)
                    else cast(object, row[field]) for field in fields
                ) for row in rows)

            def compiled_table(connection: sqlite3.Connection, name: str) -> tuple[object, ...]:
                columns = compiled_rows(connection, "table_xinfo", name,
                                        ("cid", "name", "type", "notnull", "dflt_value", "pk", "hidden"),
                                        frozenset({"name", "type"}))
                foreign_keys = compiled_rows(connection, "foreign_key_list", name,
                                             ("id", "seq", "table", "from", "to", "on_update", "on_delete", "match"),
                                             frozenset({"table", "from", "to", "on_update", "on_delete", "match"}))
                quoted = '"' + name.replace('"', '""') + '"'
                indexes: list[tuple[object, ...]] = []
                for row in _fetch_rows(connection, f"PRAGMA index_list({quoted})"):
                    parts = compiled_rows(connection, "index_xinfo", cast(str, row["name"]),
                                          ("seqno", "cid", "name", "desc", "coll", "key"),
                                          frozenset({"name", "coll"}))
                    indexes.append((cast(object, row["unique"]), cast(object, row["origin"]),
                                    cast(object, row["partial"]), parts))
                return columns, foreign_keys, tuple(sorted(indexes, key=repr))

            # Quoted keywords can be type words rather than constraints. Use
            # SQLite's compiled structure, not lexical equivalence, for these.
            for row in expected:
                if row["type"] == "table":
                    name = cast(str, row["name"])
                    if compiled_table(self._connection, name) != compiled_table(reference, name):
                        raise ServerStoreError("server store owned schema is invalid; refusing repair")
        finally:
            reference.close()

    def _copy_database_from_lease(
        self, target: Path, expected: tuple[int, ...]
    ) -> None:
        """Copy the main file without opening or closing another DB fd."""
        fd = self._database_fd
        if fd is None:
            raise ServerStoreError("server store recovery descriptor is unavailable")
        info = os.fstat(fd)
        opened = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if opened != expected:
            raise ServerStoreError("server store changed during recovery inspection; retry")

        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(target_fd, "wb", closefd=False) as writer:
                # This descriptor is shared by the live store lease. Read it
                # in place; opening/closing another fd for the DB inode could
                # release process-owned POSIX locks held by SQLite.
                with _DATABASE_DESCRIPTOR_LEASES_LOCK:
                    position = os.lseek(fd, 0, os.SEEK_CUR)
                    try:
                        _ = os.lseek(fd, 0, os.SEEK_SET)
                        remaining = info.st_size
                        while remaining:
                            chunk = os.read(fd, min(1024 * 1024, remaining))
                            if not chunk:
                                raise ServerStoreError(
                                    "server store changed during recovery inspection; retry"
                                )
                            _ = writer.write(chunk)
                            remaining -= len(chunk)
                    finally:
                        _ = os.lseek(fd, position, os.SEEK_SET)
                writer.flush()
                os.fsync(writer.fileno())
        finally:
            os.close(target_fd)

    def _inspect_interrupted_store(self, path: Path) -> None:
        """Authorize normal SQLite rollback only from a validated private copy."""
        try:
            # A recovery copy is only safe when this opener owns the sole
            # ServerStore lease. Another live connection may be in a write
            # transaction; do not inspect or snapshot its database in place.
            if self._database_lease_references() != 1:
                raise ServerStoreError(
                    "server store recovery refused while another store connection is open"
                )
            parent_info = path.parent.lstat()
            if not stat.S_ISDIR(parent_info.st_mode):
                raise ServerStoreError("server store recovery requires an owned directory")
            with self._recovery_slot(path) as directory:
                if directory.stat().st_uid != parent_info.st_uid:
                    raise ServerStoreError("server store recovery ownership is invalid")

                def identity(source: Path) -> tuple[int, ...] | None:
                    try:
                        info = source.lstat()
                    except FileNotFoundError:
                        return None
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != parent_info.st_uid:
                        raise ServerStoreError("server store recovery file ownership is invalid")
                    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

                sources = (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"))
                before = {source: identity(source) for source in sources}
                if before[path] is None or before[sources[1]] is None:
                    raise ServerStoreError("server store changed during recovery inspection; retry")
                for source, stamp in before.items():
                    if stamp is None:
                        continue
                    target = directory / source.name
                    if source == path:
                        self._copy_database_from_lease(target, stamp)
                        continue
                    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as reader:
                        info = os.fstat(reader.fileno())
                        opened = (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                                  info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        if opened != stamp:
                            raise ServerStoreError("server store changed during recovery inspection; retry")
                        if source == sources[1]:
                            # Owned single-database writes never use a super-journal.
                            magic = bytes.fromhex("d9d505f920a163d7")
                            if reader.read(8) != magic or info.st_size <= 512:
                                raise ServerStoreError("server store recovery journal is invalid")
                            _ = reader.seek(-8, os.SEEK_END)
                            if reader.read(8) == magic:
                                raise ServerStoreError("server store recovery journal is unsupported")
                            _ = reader.seek(0)
                        output = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(output, "wb") as writer:
                            shutil.copyfileobj(reader, writer)
                if before != {source: identity(source) for source in sources}:
                    raise ServerStoreError("server store changed during recovery inspection; retry")
                candidate = directory / path.name
                self._connection = sqlite3.connect(str(candidate), isolation_level=None)
                self._connection.row_factory = sqlite3.Row
                try:
                    _ = self.applied_migration_versions()
                finally:
                    self._connection.close()
                if before != {source: identity(source) for source in sources}:
                    raise ServerStoreError("server store changed during recovery inspection; retry")
        except (sqlite3.Error, OSError):
            raise ServerStoreError("server store interrupted-transaction inspection failed safely") from None

    def _recovery_info(self, path: Path, *, directory: bool = False) -> os.stat_result:
        info = path.lstat()
        reparse = cast(int, getattr(info, "st_file_attributes", 0)) & cast(int, getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        mode = 0o700 if directory else 0o600
        if reparse or not valid_type or info.st_uid != path.parent.stat().st_uid or (not directory and info.st_nlink != 1):
            raise ServerStoreError("unsafe recovery artifact; administrator inspection required")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != mode:
            raise ServerStoreError("unsafe recovery artifact permissions; administrator inspection required")
        return info

    @staticmethod
    def _sync_recovery_directory(directory: Path) -> None:
        if os.name != "nt":
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @contextmanager
    def _recovery_guard(self, path: Path, expected: os.stat_result | None) -> Generator[os.stat_result | None, None, None]:
        guard = path.parent / _RECOVERY_GUARD
        fd: int | None = None
        locked = False
        try:
            parent = path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or cast(int, getattr(parent, "st_file_attributes", 0)) & cast(int, getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise ServerStoreError("unsafe recovery directory; administrator inspection required")
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                fd = os.open(guard, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _ = self._recovery_info(guard)
                fd = os.open(guard, flags)
            guard_info = self._recovery_info(guard)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode, opened.st_nlink) != (guard_info.st_dev, guard_info.st_ino, guard_info.st_uid, guard_info.st_mode, guard_info.st_nlink):
                raise ServerStoreError("recovery lock identity changed; retry")
            deadline = time.monotonic() + 5
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        _ = os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise ServerStoreError("recovery inspection is active; retry after it finishes") from None
                    time.sleep(0.01)
            # Another guarded opener may have created a previously absent file.
            if expected is None:
                expected = self._store_info(path)
            _ = self._check_store_entry(path, expected)
            _ = os.lseek(fd, 0, os.SEEK_SET)
            marker = os.read(fd, 2)
            if marker not in (b"", b"\0"):
                raise ServerStoreError("unknown recovery lock; administrator inspection required")
            if not marker:
                _ = os.lseek(fd, 0, os.SEEK_SET)
                _ = os.write(fd, b"\0")
                os.fsync(fd)
                self._sync_recovery_directory(path.parent)
            _ = self._check_store_entry(path, expected)
            self._reclaim_recovery_slot(path)
            if any(entry.name != _RECOVERY_SLOT for entry in path.parent.glob(".store-recovery-*")):
                raise ServerStoreError("unattested recovery artifact; administrator inspection required")
            yield expected
        except OSError:
            raise ServerStoreError("recovery lifecycle unavailable; source retained") from None
        finally:
            if fd is not None:
                try:
                    if locked:
                        if os.name == "nt":
                            import msvcrt
                            _ = os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _write_recovery_claim(self, path: Path, payload: bytes) -> None:
        stage = path.parent / _RECOVERY_NEXT
        claim = path.parent / _RECOVERY_CLAIM
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as writer:
            _ = writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        if os.name == "nt":
            import ctypes
            library = cast(object, ctypes.WinDLL("kernel32", use_last_error=True))
            move = cast(object, getattr(library, "MoveFileExW"))
            setattr(move, "argtypes", [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong])
            setattr(move, "restype", ctypes.c_int)
            if not cast(Callable[[str, str, int], int], move)(str(stage), str(claim), 1 | 8):
                raise ServerStoreError("recovery claim publication failed; source retained")
        else:
            os.replace(stage, claim)
        self._sync_recovery_directory(path.parent)

    def _reclaim_recovery_slot(self, path: Path) -> None:
        slot = path.parent / _RECOVERY_SLOT
        claim = path.parent / _RECOVERY_CLAIM
        stage = path.parent / _RECOVERY_NEXT
        planned = _RECOVERY_MAGIC + b"P\n"
        owned = _RECOVERY_MAGIC + b"O\n"
        if stage.exists() or stage.is_symlink():
            stage_info = self._recovery_info(stage)
            if stage_info.st_size > len(owned) + 32:
                raise ServerStoreError("unknown recovery claim; administrator inspection required")
            partial = stage.read_bytes()
            if not (planned.startswith(partial) or owned.startswith(partial) or (partial.startswith(owned) and len(partial) <= len(owned) + 32)):
                raise ServerStoreError("unknown recovery claim; administrator inspection required")
            stage.unlink()
        if not claim.exists() and not claim.is_symlink():
            if slot.exists() or slot.is_symlink():
                raise ServerStoreError("unattested recovery slot; administrator inspection required")
            return
        claim_info = self._recovery_info(claim)
        if claim_info.st_size > len(owned) + 32:
            raise ServerStoreError("unknown recovery claim; administrator inspection required")
        payload = claim.read_bytes()
        if payload != planned and not (payload.startswith(owned) and len(payload) == len(owned) + 32):
            raise ServerStoreError("unknown recovery claim; administrator inspection required")
        if slot.exists() or slot.is_symlink():
            info = self._recovery_info(slot, directory=True)
            if payload != planned and payload[len(owned):] != info.st_dev.to_bytes(16, "big") + info.st_ino.to_bytes(16, "big"):
                raise ServerStoreError("recovery slot identity changed; administrator inspection required")
            entries: list[Path] = []
            for entry in slot.iterdir():
                entries.append(entry)
                if len(entries) > 4:
                    raise ServerStoreError("unattested recovery contents; administrator inspection required")
            allowed = {path.name + suffix for suffix in ("", "-journal", "-wal", "-shm")}
            if (payload == planned and entries) or any(entry.name not in allowed for entry in entries):
                raise ServerStoreError("unattested recovery contents; administrator inspection required")
            for entry in entries:
                _ = self._recovery_info(entry)
            for entry in entries:
                entry.unlink()
            slot.rmdir()
            self._sync_recovery_directory(path.parent)
        claim.unlink()
        self._sync_recovery_directory(path.parent)

    @contextmanager
    def _recovery_slot(self, path: Path) -> Generator[Path, None, None]:
        slot = path.parent / _RECOVERY_SLOT
        self._write_recovery_claim(path, _RECOVERY_MAGIC + b"P\n")
        slot.mkdir(mode=0o700)
        info = self._recovery_info(slot, directory=True)
        self._sync_recovery_directory(path.parent)
        self._write_recovery_claim(path, _RECOVERY_MAGIC + b"O\n" + info.st_dev.to_bytes(16, "big") + info.st_ino.to_bytes(16, "big"))
        try:
            yield slot
        finally:
            self._reclaim_recovery_slot(path)

    def migrate(self) -> int:
        """Apply a bounded, ordered migration set atomically and idempotently."""
        with self._lock:
            try:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                applied = self.applied_migration_versions()
                for version, statements in MIGRATIONS[len(applied):]:
                    for statement in statements:
                        _ = self._connection.execute(statement)
                    _ = self._connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, self._now_for_migrations()),
                    )
                _ = self._connection.execute("COMMIT")
            except (sqlite3.Error, ServerStoreError) as exc:
                if self._connection.in_transaction:
                    _ = self._connection.execute("ROLLBACK")
                if isinstance(exc, ServerStoreError):
                    raise
                raise ServerStoreError(
                    "server store migration failed; original state retained"
                ) from None
            return self.schema_version()

    @staticmethod
    def _now_for_migrations() -> str:
        """The migration bookkeeping stamp (wall clock; diagnostic only)."""
        from datetime import datetime, timezone

        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def require_current_schema(self) -> int:
        """Fail closed unless the store is at the current schema version."""
        version = self.schema_version()
        if version > STORE_SCHEMA_VERSION:
            raise ServerStoreError(
                f"server store schema version {version} is newer than this "
                + f"program understands ({STORE_SCHEMA_VERSION}); refusing to "
                + "read a future store"
            )
        if version < STORE_SCHEMA_VERSION:
            raise ServerStoreError(
                f"server store schema version {version} is not migrated to "
                + f"{STORE_SCHEMA_VERSION}"
            )
        return version

    # ── Authoritative server configuration (single source of truth) ──────

    def load_configuration_document(self) -> dict[str, object] | None:
        """The authoritative configuration document, or ``None`` (first run)."""
        with self._lock:
            row = _fetch_row(self._connection,
                "SELECT document FROM server_configuration WHERE id = 1"
            )
        if row is None:
            return None
        try:
            document = cast(
                "dict[str, object]", json.loads(cast(str, row["document"]))
            )
        except json.JSONDecodeError as exc:
            raise ServerStoreError(
                "server store configuration document is corrupt"
            ) from exc
        return document

    def save_configuration_document(self, document: Mapping[str, object], at: str) -> None:
        """Atomically replace the authoritative configuration document."""
        encoded = json.dumps(dict(document), sort_keys=True, allow_nan=False)
        with self._lock:
            try:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                _ = self._connection.execute(
                    "INSERT INTO server_configuration (id, document, updated_at) "
                    + "VALUES (1, ?, ?) "
                    + "ON CONFLICT(id) DO UPDATE SET document = excluded.document, "
                    + "updated_at = excluded.updated_at",
                    (encoded, at),
                )
                _ = self._connection.execute("COMMIT")
            except sqlite3.Error as exc:
                _ = self._connection.execute("ROLLBACK")
                raise ServerStoreError(
                    f"server store configuration write failed: {exc}"
                ) from None

    def configuration_updated_at(self) -> str | None:
        with self._lock:
            row = _fetch_row(self._connection,
                "SELECT updated_at FROM server_configuration WHERE id = 1"
            )
        return cast(str, row["updated_at"]) if row is not None else None

    # ── Administrator identity and sessions ──────────────────────────────

    def admin_identity(self) -> AdminIdentityRecord | None:
        with self._lock:
            row = _fetch_row(self._connection,
                "SELECT password_salt_hex, password_hash_hex, pbkdf2_iterations, "
                + "created_at FROM administrator_identity WHERE id = 1"
            )
        if row is None:
            return None
        return AdminIdentityRecord(
            password_salt_hex=cast(str, row["password_salt_hex"]),
            password_hash_hex=cast(str, row["password_hash_hex"]),
            pbkdf2_iterations=int(cast(int, row["pbkdf2_iterations"])),
            created_at=cast(str, row["created_at"]),
        )

    def set_admin_identity(
        self,
        *,
        password_salt_hex: str,
        password_hash_hex: str,
        pbkdf2_iterations: int,
        at: str,
    ) -> None:
        _ = v_str(password_salt_hex, "store.password_salt_hex")
        _ = v_str(password_hash_hex, "store.password_hash_hex")
        if pbkdf2_iterations < 1:
            raise ServerStoreError("pbkdf2_iterations must be positive")
        with self._lock:
            try:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                _ = self._connection.execute(
                    "INSERT INTO administrator_identity (id, password_salt_hex, "
                    + "password_hash_hex, pbkdf2_iterations, created_at, updated_at) "
                    + "VALUES (1, ?, ?, ?, ?, ?) "
                    + "ON CONFLICT(id) DO UPDATE SET password_salt_hex = "
                    + "excluded.password_salt_hex, password_hash_hex = "
                    + "excluded.password_hash_hex, pbkdf2_iterations = "
                    + "excluded.pbkdf2_iterations, updated_at = excluded.updated_at",
                    (password_salt_hex, password_hash_hex, pbkdf2_iterations, at, at),
                )
                # Credential rotation revokes every existing session.
                _ = self._connection.execute("DELETE FROM admin_sessions")
                _ = self._connection.execute("COMMIT")
            except sqlite3.Error as exc:
                _ = self._connection.execute("ROLLBACK")
                raise ServerStoreError(
                    f"server store administrator write failed: {exc}"
                ) from None

    def create_session(
        self, *, session_hash: str, csrf_hash: str, created_at: str, expires_at: str
    ) -> None:
        with self._lock:
            try:
                _ = self._connection.execute(
                    "INSERT INTO admin_sessions (session_hash, csrf_hash, "
                    + "created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (session_hash, csrf_hash, created_at, expires_at),
                )
            except sqlite3.Error as exc:
                raise ServerStoreError(
                    f"server store session write failed: {exc}"
                ) from None

    def session_csrf_hash(self, session_hash: str, *, now: str) -> str | None:
        """The live session's CSRF verifier, or ``None`` (expired/unknown)."""
        with self._lock:
            row = _fetch_row(self._connection,
                "SELECT csrf_hash, expires_at FROM admin_sessions "
                + "WHERE session_hash = ?",
                (session_hash,),
            )
        if row is None:
            return None
        if cast(str, row["expires_at"]) <= now:
            self.delete_session(session_hash)
            return None
        return cast(str, row["csrf_hash"])

    def delete_session(self, session_hash: str) -> None:
        with self._lock:
            _ = self._connection.execute(
                "DELETE FROM admin_sessions WHERE session_hash = ?", (session_hash,)
            )

    def delete_expired_sessions(self, *, now: str) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?", (now,)
            )
            return int(cursor.rowcount)

    # ── Inference-client keys (hash-only storage) ─────────────────────────

    def list_client_keys(self) -> tuple[ClientKeyRecord, ...]:
        with self._lock:
            rows = _fetch_rows(self._connection,
                "SELECT client_id, key_hash, label, created_at, revoked_at "
                + "FROM client_keys ORDER BY client_id"
            )
        return tuple(
            ClientKeyRecord(
                client_id=cast(str, row["client_id"]),
                key_hash=cast(str, row["key_hash"]),
                label=cast(str, row["label"]),
                created_at=cast(str, row["created_at"]),
                revoked_at=(
                    cast("str | None", row["revoked_at"])
                    if row["revoked_at"] is not None
                    else None
                ),
            )
            for row in rows
        )

    def add_client_key(
        self, *, client_id: str, key_hash: str, label: str, created_at: str
    ) -> None:
        _ = v_safe_id(client_id, "store.client_id")
        _ = v_str(label, "store.client_label")
        with self._lock:
            try:
                _ = self._connection.execute(
                    "INSERT INTO client_keys (client_id, key_hash, label, "
                    + "created_at, revoked_at) VALUES (?, ?, ?, ?, NULL)",
                    (client_id, key_hash, label, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ServerStoreError(
                    f"client id {client_id!r} already exists"
                ) from exc
            except sqlite3.Error as exc:
                raise ServerStoreError(
                    f"server store client key write failed: {exc}"
                ) from None

    def revoke_client_key(self, *, client_id: str, revoked_at: str) -> bool:
        """Revoke one client key; ``False`` when the id does not exist."""
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE client_keys SET revoked_at = ? "
                + "WHERE client_id = ? AND revoked_at IS NULL",
                (revoked_at, client_id),
            )
            return int(cursor.rowcount) > 0

    def active_client_key_hashes(self) -> dict[str, str]:
        """Active client id -> SHA-256 key hash (feeds ClientKeyDirectory)."""
        with self._lock:
            rows = _fetch_rows(self._connection,
                "SELECT client_id, key_hash FROM client_keys "
                + "WHERE revoked_at IS NULL ORDER BY client_id"
            )
        return {cast(str, row["client_id"]): cast(str, row["key_hash"]) for row in rows}

    # ── Provider credentials (the bounded D-044 exception) ────────────────

    def set_provider_secret(self, *, provider_id: str, secret: str, at: str) -> None:
        _ = v_safe_id(provider_id, "store.provider_id")
        with self._lock:
            try:
                _ = self._connection.execute(
                    "INSERT INTO provider_secrets (provider_id, secret, updated_at) "
                    + "VALUES (?, ?, ?) "
                    + "ON CONFLICT(provider_id) DO UPDATE SET secret = "
                    + "excluded.secret, updated_at = excluded.updated_at",
                    (provider_id, secret, at),
                )
            except sqlite3.Error as exc:
                raise ServerStoreError(
                    "server store provider credential write failed"
                ) from exc

    def has_provider_secret(self, provider_id: str) -> bool:
        with self._lock:
            row = _fetch_row(self._connection,
                "SELECT 1 FROM provider_secrets WHERE provider_id = ?",
                (provider_id,),
            )
        return row is not None

    def get_provider_secret(self, provider_id: str) -> str | None:
        """Read one provider credential for dispatch only.

        This accessor exists for the execution adapters' credential seam
        (M04) and nowhere else: it is never called by exports, listings,
        diagnostics or any control/UI response path.
        """
        with self._lock:
            row = _fetch_row(self._connection,
                "SELECT secret FROM provider_secrets WHERE provider_id = ?",
                (provider_id,),
            )
        return cast(str, row["secret"]) if row is not None else None

    def delete_provider_secret(self, *, provider_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM provider_secrets WHERE provider_id = ?", (provider_id,)
            )
            return int(cursor.rowcount) > 0

    # ── Bounded audit trail (D-043 metadata only) ─────────────────────────

    def append_audit_record(
        self,
        *,
        payload: Mapping[str, object],
        recorded_at: str,
        max_records: int,
        max_age_seconds: int,
    ) -> None:
        """Append one audit record and enforce bounded retention."""
        if not 1 <= max_records <= _AUDIT_HARD_CAP:
            raise ServerStoreError(
                f"audit retention max_records must be within 1..{_AUDIT_HARD_CAP}"
            )
        if max_age_seconds < 1:
            raise ServerStoreError("audit retention max_age_seconds must be positive")
        encoded = json.dumps(dict(payload), sort_keys=True, allow_nan=False)
        cutoff = _utc_now_minus(recorded_at, max_age_seconds)
        with self._lock:
            try:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                _ = self._connection.execute(
                    "INSERT INTO audit_records (recorded_at, payload) VALUES (?, ?)",
                    (recorded_at, encoded),
                )
                if cutoff is not None:
                    _ = self._connection.execute(
                        "DELETE FROM audit_records WHERE recorded_at <= ?",
                        (cutoff,),
                    )
                # The count bound always holds, even without a parseable
                # cutoff (caller clock drift keeps retention bounded).
                _ = self._connection.execute(
                    "DELETE FROM audit_records WHERE seq NOT IN "
                    + "(SELECT seq FROM audit_records ORDER BY seq DESC LIMIT ?)",
                    (max_records,),
                )
                _ = self._connection.execute("COMMIT")
            except sqlite3.Error as exc:
                _ = self._connection.execute("ROLLBACK")
                raise ServerStoreError(
                    f"server store audit write failed: {exc}"
                ) from None

    def audit_record_count(self) -> int:
        with self._lock:
            row = _fetch_row(self._connection,
                "SELECT COUNT(*) AS n FROM audit_records"
            )
        return int(cast(int, row["n"])) if row is not None else 0


def _fetch_row(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...] = (),
) -> sqlite3.Row | None:
    """One row read with an explicit type (sqlite3 stubs type rows as Any)."""
    return cast("sqlite3.Row | None", connection.execute(sql, params).fetchone())


def _fetch_rows(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...] = (),
) -> list[sqlite3.Row]:
    """Row-list reads with an explicit type (same stub discipline)."""
    return cast("list[sqlite3.Row]", connection.execute(sql, params).fetchall())


def _utc_now_minus(canonical_now: str, seconds: int) -> str | None:
    """``canonical_now - seconds`` in canonical form, or ``None`` on drift."""
    from datetime import datetime, timedelta, timezone

    try:
        moment = datetime.fromisoformat(canonical_now.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return None
        shifted = moment - timedelta(seconds=seconds)
        return (
            shifted.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except ValueError:
        return None


__all__ = [
    "MIGRATIONS",
    "SERVER_DATA_DIR_NAME",
    "STORE_FILE_NAME",
    "STORE_SCHEMA_VERSION",
    "AdminIdentityRecord",
    "ClientKeyRecord",
    "ServerStore",
    "ServerStoreError",
    "canonical_store_path",
    "default_server_data_dir",
]
