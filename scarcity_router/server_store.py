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
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .gateway_validation import v_safe_id, v_str

STORE_SCHEMA_VERSION = 2

STORE_FILE_NAME = "server-state.sqlite3"
DATA_DIR_NAME = "scarcity-router"
SERVER_DATA_DIR_NAME = "server"

# Each migration is (version, statements). Versions are applied in order
# inside one transaction each; the recorded version advances only when the
# whole migration succeeded (crash-safe by construction).
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


class ServerStoreError(ValueError):
    """Raised for store-level failures (unreadable file, bad migration state)."""


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

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _ = os.chmod(parent, 0o700)
        try:
            self._connection = sqlite3.connect(
                str(path), check_same_thread=False, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise ServerStoreError(f"server store is not usable: {exc}") from None
        try:
            os.chmod(path, 0o600)
            _ = self._connection.execute("PRAGMA journal_mode=DELETE")
            _ = self._connection.execute("PRAGMA synchronous=FULL")
            _ = self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.row_factory = sqlite3.Row
        except (sqlite3.Error, OSError) as exc:
            self._connection.close()
            raise ServerStoreError(f"server store is not usable: {exc}") from None

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
        if migrate:
            _ = store.migrate()
        return store

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @property
    def path(self) -> Path:
        return self._path

    def schema_version(self) -> int:
        """The applied schema version (0 for an un-migrated store)."""
        with self._lock:
            try:
                row = _fetch_row(self._connection,
                    "SELECT MAX(version) AS version FROM schema_migrations"
                )
            except sqlite3.Error:
                return 0
            if row is None:
                return 0
            raw = cast("int | None", row["version"])
            return int(raw) if raw is not None else 0

    def applied_migration_versions(self) -> tuple[int, ...]:
        """The ordered migration versions recorded in the store."""
        with self._lock:
            try:
                rows = _fetch_rows(self._connection,
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            except sqlite3.Error:
                return ()
        return tuple(int(cast(int, row["version"])) for row in rows)

    def migrate(self) -> int:
        """Apply pending migrations; returns the resulting schema version."""
        with self._lock:
            applied = self.schema_version()
            for version, statements in MIGRATIONS:
                if version <= applied:
                    continue
                self._apply_migration(version, statements)
            return self.schema_version()

    def _apply_migration(self, version: int, statements: tuple[str, ...]) -> None:
        try:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                _ = self._connection.execute(statement)
            _ = self._connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, self._now_for_migrations()),
            )
            _ = self._connection.execute("COMMIT")
        except sqlite3.Error as exc:
            _ = self._connection.execute("ROLLBACK")
            raise ServerStoreError(
                f"server store migration to version {version} failed: {exc}"
            ) from None

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
