"""Server-side worker identity, pairing and credential service (M05, D-044).

The administrator identity is the sole trust root for worker pairing
(D-044): an administrator issues a short-lived one-time pairing code, the
worker redeems it over a verified TLS connection, and the worker receives a
per-device credential with rotation and revocation. There is no shared
fleet password, no manual worker-IP configuration and no certificate
ceremony.

This module is the bounded, versioned durable store for that worker
identity state, per D-041/D-044:

- **Storage mechanism (recorded choice):** stdlib ``sqlite3`` in one
  database file inside the server's data directory. The directory is
  created ``0o700`` and the database file is forced to ``0o600`` after
  creation (the directory's owner-only traversal is the primary
  protection; the file mode is the recorded fallback pattern). SQLite was
  chosen over a JSON-per-worker file layout because pairing-code
  redemption must be atomic under concurrent connections (a one-time code
  must be redeemable exactly once even when two connections race) and
  because D-041 already names a "SQLite-class single-file store" as the
  server's durable-state pattern. No external database service is
  introduced.
- **Only hashes at rest.** Pairing codes and worker credentials are stored
  exclusively as SHA-256 hashes with per-credential salted pepping (a
  random per-store salt, so identical credentials do not produce equal
  hashes across deployments). Raw code/credential values are returned
  exactly once, at issuance, and are never logged, exported or returned
  again.
- **Bounded.** Outstanding pairing codes are capped; expired codes and
  superseded credential hashes are pruned on a bounded schedule. There is
  no unbounded growth path.
- **Administration seam.** Issue/list/revoke/rotate are typed methods on
  :class:`WorkerIdentityStore` and :class:`WorkerAdminService`. M09 owns
  the control-API/UI surface on top; this module deliberately ships no
  HTTP, CLI or UI.

Revocation takes effect without redeployment: a revoked identity's next
connection is rejected, and live sessions are closed by the endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from .gateway_validation import v_int, v_safe_id, v_text

# Store schema version (server-internal, migrations explicit per D-041).
WORKER_IDENTITY_STORE_SCHEMA_VERSION = 1

# A pairing code is short-lived and single-use (D-044 "short-lived
# one-time code"). Defaults here; the administrator seam may narrow them,
# never extend them beyond the documented maxima.
DEFAULT_PAIRING_CODE_TTL_SECONDS = 600
MAX_PAIRING_CODE_TTL_SECONDS = 3600
MAX_OUTSTANDING_PAIRING_CODES = 16

_CREDENTIAL_LENGTH_BYTES = 32
_PAIRING_CODE_LENGTH_BYTES = 16

_LATEST_SCHEMA_ROW = "latest"

_MIN_ADMIN_PRUNE_INTERVAL_SECONDS = 60


class WorkerIdentityError(Exception):
    """A typed identity/pairing failure with a closed reason code.

    Reason codes align with the worker protocol's error vocabulary so the
    endpoint can translate them one-to-one.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


@dataclass(frozen=True)
class PairingCode:
    """One outstanding one-time pairing code (shown once, hashed at rest)."""

    pairing_code: str
    expires_at: str
    label: str | None


@dataclass(frozen=True)
class WorkerIdentityRecord:
    """The safe, secret-free projection of one worker identity.

    Credential material never appears here: only the worker id, its
    administrator-assigned label, lifecycle status and bookkeeping
    timestamps.
    """

    worker_id: str
    label: str | None
    status: str
    created_at: str
    credential_rotated_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "worker_id": self.worker_id,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.credential_rotated_at is not None:
            out["credential_rotated_at"] = self.credential_rotated_at
        return out


def _canonical(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_canonical(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


class WorkerIdentityStore:
    """Bounded durable store of pairing codes and per-device identities.

    Thread-safe (all mutations take one lock); the SQLite connection is
    opened once per store instance. The clock is injectable so expiry
    behavior is deterministic in tests.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
        pairing_code_ttl_seconds: int = DEFAULT_PAIRING_CODE_TTL_SECONDS,
    ) -> None:
        _ = v_int(pairing_code_ttl_seconds, "pairing_code_ttl_seconds", lo=1)
        if pairing_code_ttl_seconds > MAX_PAIRING_CODE_TTL_SECONDS:
            raise ValueError(
                f"pairing_code_ttl_seconds {pairing_code_ttl_seconds} exceeds the "
                + f"documented maximum {MAX_PAIRING_CODE_TTL_SECONDS}"
            )
        self._clock: Callable[[], datetime] = clock if clock is not None else _utcnow
        self._pairing_code_ttl: int = pairing_code_ttl_seconds
        self._lock: threading.Lock = threading.Lock()
        # The pepper salt is PERSISTED in store_meta on first init and read
        # back on every load: credential and pairing-code hashes must stay
        # verifiable across server restarts (a per-process salt would brick
        # every stored credential and void every outstanding code).
        self._salt: bytes = b""
        self._closed: bool = False
        self._path: str = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(self._path))
        if parent:
            ensure_private_tree(parent)
        self._connection: sqlite3.Connection = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False
        )
        try:
            # The database file may predate this process (umask-dependent
            # creation); force the recorded 0o600 mode either way.
            os.chmod(self._path, 0o600)
        except OSError:
            self._connection.close()
            raise
        _ = self._connection.execute("PRAGMA journal_mode=WAL")
        _ = self._connection.execute("PRAGMA synchronous=FULL")
        try:
            self._migrate()
        except BaseException:
            # Fail closed without leaking the connection (the corrupt
            # salt path raises from inside _migrate).
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _migrate(self) -> None:
        with self._lock:
            _ = self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = cast(
                "tuple[object] | None",
                self._connection.execute(
                    "SELECT value FROM store_meta WHERE key = 'schema_version'"
                ).fetchone(),
            )
            if row is None:
                _ = self._connection.execute(
                    "INSERT INTO store_meta (key, value) VALUES ('schema_version', ?)",
                    (str(WORKER_IDENTITY_STORE_SCHEMA_VERSION),),
                )
            else:
                version = int(cast("str", row[0]))
                if version != WORKER_IDENTITY_STORE_SCHEMA_VERSION:
                    raise WorkerIdentityError(
                        "internal_error",
                        f"worker identity store schema version {version} is not "
                        + f"{WORKER_IDENTITY_STORE_SCHEMA_VERSION}; an explicit "
                        + "migration is required",
                    )
            self._load_or_create_salt()
            _ = self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code_hash TEXT PRIMARY KEY,
                    label TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    redeemed_by TEXT,
                    redeemed_at TEXT
                )
                """
            )
            _ = self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_identities (
                    worker_id TEXT PRIMARY KEY,
                    label TEXT,
                    credential_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
                    created_at TEXT NOT NULL,
                    credential_rotated_at TEXT
                )
                """
            )

    def _load_or_create_salt(self) -> None:
        """Read the persisted pepper salt, creating it on first init.

        The salt lives in ``store_meta`` beside the schema version: a
        store reopened after a server restart must hash presented
        credentials with the SAME pepper the stored hashes used. A
        corrupt salt row fails closed (no store) rather than silently
        re-peppering with a fresh value that would invalidate every
        stored hash.
        """
        row = cast(
            "tuple[object] | None",
            self._connection.execute(
                "SELECT value FROM store_meta WHERE key = 'salt'"
            ).fetchone(),
        )
        if row is None:
            salt_hex = secrets.token_bytes(32).hex()
            _ = self._connection.execute(
                "INSERT INTO store_meta (key, value) VALUES ('salt', ?)",
                (salt_hex,),
            )
        else:
            salt_hex = str(row[0])
        try:
            self._salt = bytes.fromhex(salt_hex)
        except ValueError:
            raise WorkerIdentityError(
                "internal_error",
                "the worker identity store's pepper salt is corrupt; refusing "
                + "to open the store rather than invalidating every stored "
                + "hash",
            ) from None
        if len(self._salt) != 32:
            raise WorkerIdentityError(
                "internal_error",
                "the worker identity store's pepper salt has an unexpected "
                + "length; refusing to open the store",
            )

    # ── Credential hashing ───────────────────────────────────────────

    def _hash_secret(self, secret: str) -> str:
        peppered = hmac.new(self._salt, secret.encode("utf-8"), hashlib.sha256).digest()
        return hashlib.sha256(peppered).hexdigest()

    # ── Pairing lifecycle ────────────────────────────────────────────

    def begin_pairing(self, *, label: str | None = None) -> PairingCode:
        """Issue one short-lived one-time pairing code (administrator act).

        ``label`` is human-readable display text (which machine is this?),
        never an identifier: it is bounded free text so the M09
        administration surface can label a device the way the operator
        speaks about it. It is stored only beside the code's hash and is
        never used for authentication.
        """
        checked_label = (
            None
            if label is None
            else (v_text(label.strip(), "pairing.label", max_len=200) or None)
        )
        now = self._clock()
        expires_at = now + timedelta(seconds=self._pairing_code_ttl)
        pairing_code = secrets.token_urlsafe(_PAIRING_CODE_LENGTH_BYTES)
        while pairing_code.startswith("-"):
            # CLI-safe grammar: a code whose first character is '-' is
            # ambiguous as an argv value (argparse reads it as a flag),
            # so a leading '-' is redrawn. Mid-code '-' stays legal.
            pairing_code = secrets.token_urlsafe(_PAIRING_CODE_LENGTH_BYTES)
        with self._lock:
            self._prune_expired_locked(now)
            outstanding = cast(
                "tuple[object]",
                self._connection.execute(
                    "SELECT COUNT(*) FROM pairing_codes WHERE redeemed_by IS NULL"
                ).fetchone(),
            )
            count = int(cast("int", outstanding[0]))
            if count >= MAX_OUTSTANDING_PAIRING_CODES:
                raise WorkerIdentityError(
                    ERR_PAIRING_UNAVAILABLE,
                    "the pairing-code backlog is full; revoke or wait for "
                    + "outstanding codes to expire",
                )
            _ = self._connection.execute(
                """
                INSERT INTO pairing_codes (code_hash, label, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (self._hash_secret(pairing_code), checked_label, _canonical(now), _canonical(expires_at)),
            )
        return PairingCode(
            pairing_code=pairing_code, expires_at=_canonical(expires_at), label=checked_label
        )

    def redeem_pairing_code(self, pairing_code: str) -> tuple[str, str]:
        """Redeem one code exactly once; returns (worker_id, credential).

        Redemption is atomic: the conditional UPDATE claims the row for one
        caller only, so a replayed or raced code is rejected with
        ``pairing_code_used`` even under concurrent connections.
        """
        _ = v_text(pairing_code, "pairing_code", max_len=512)
        now = self._clock()
        code_hash = self._hash_secret(pairing_code)
        worker_id = f"w-{uuid.uuid4().hex[:16]}"
        credential = secrets.token_urlsafe(_CREDENTIAL_LENGTH_BYTES)
        credential_hash = self._hash_secret(credential)
        with self._lock:
            row = cast(
                "tuple[object, object] | None",
                self._connection.execute(
                    "SELECT expires_at, redeemed_by FROM pairing_codes WHERE code_hash = ?",
                    (code_hash,),
                ).fetchone(),
            )
            if row is None:
                raise WorkerIdentityError(ERR_PAIRING_INVALID, "the pairing code is not valid")
            expires_at = cast("str", row[0])
            if _parse_canonical(expires_at) < now:
                raise WorkerIdentityError(ERR_PAIRING_EXPIRED, "the pairing code has expired")
            # Atomic single-use claim: only the first UPDATE that flips the
            # unredeemed row wins.
            cursor = self._connection.execute(
                """
                UPDATE pairing_codes
                SET redeemed_by = ?, redeemed_at = ?
                WHERE code_hash = ? AND redeemed_by IS NULL
                """,
                ("claimed", _canonical(now), code_hash),
            )
            if cursor.rowcount != 1:
                raise WorkerIdentityError(ERR_PAIRING_USED, "the pairing code was already used")
            _ = self._connection.execute(
                """
                UPDATE pairing_codes SET redeemed_by = ? WHERE code_hash = ?
                """,
                (worker_id, code_hash),
            )
            _ = self._connection.execute(
                """
                INSERT INTO worker_identities (
                    worker_id, label, credential_hash, status, created_at
                ) VALUES (?, ?, ?, 'active', ?)
                """,
                (worker_id, self._label_for(code_hash), credential_hash, _canonical(now)),
            )
        return worker_id, credential

    def _label_for(self, code_hash: str) -> str | None:
        row = cast(
            "tuple[object] | None",
            self._connection.execute(
                "SELECT label FROM pairing_codes WHERE code_hash = ?", (code_hash,)
            ).fetchone(),
        )
        if row is None:
            return None
        value = row[0]
        return None if value is None else cast("str", value)

    def authenticate(self, worker_id: str, credential: str) -> None:
        """Verify one device identity or raise a typed auth failure.

        Unknown id, wrong credential and revoked identity are distinct
        outcomes so the endpoint can send the exact protocol error code;
        none of them reveals whether a *credential* was close.
        """
        _ = v_safe_id(worker_id, "worker_id")
        _ = v_text(credential, "credential", max_len=512)
        with self._lock:
            row = cast(
                "tuple[object, object] | None",
                self._connection.execute(
                    "SELECT credential_hash, status FROM worker_identities WHERE worker_id = ?",
                    (worker_id,),
                ).fetchone(),
            )
        if row is None:
            raise WorkerIdentityError(ERR_UNAUTHORIZED, "the worker identity is not recognized")
        stored_hash = cast("str", row[0])
        status = cast("str", row[1])
        if status != "active":
            raise WorkerIdentityError(ERR_CREDENTIAL_REVOKED, "the worker identity is revoked")
        if not hmac.compare_digest(self._hash_secret(credential), stored_hash):
            raise WorkerIdentityError(ERR_UNAUTHORIZED, "the worker credential is not valid")

    def is_active(self, worker_id: str) -> bool:
        """Whether the identity exists and is active (live-session checks)."""
        with self._lock:
            row = cast(
                "tuple[object] | None",
                self._connection.execute(
                    "SELECT status FROM worker_identities WHERE worker_id = ?", (worker_id,)
                ).fetchone(),
            )
        if row is None:
            return False
        return cast("str", row[0]) == "active"

    # ── Rotation and revocation ──────────────────────────────────────

    def rotate_credential(self, worker_id: str) -> str:
        """Issue a new credential for one active identity (admin path).

        Returns the new credential value exactly once (transient). The old
        credential stops working immediately; the next connection must use
        the new one. Rotation without redeployment.
        """
        _ = v_safe_id(worker_id, "worker_id")
        credential = secrets.token_urlsafe(_CREDENTIAL_LENGTH_BYTES)
        now = self._clock()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE worker_identities
                SET credential_hash = ?, credential_rotated_at = ?
                WHERE worker_id = ? AND status = 'active'
                """,
                (self._hash_secret(credential), _canonical(now), worker_id),
            )
            if cursor.rowcount != 1:
                raise WorkerIdentityError(
                    ERR_UNAUTHORIZED, "no active worker identity with that id"
                )
        return credential

    def revoke(self, worker_id: str) -> None:
        """Revoke one identity; its next connection is rejected."""
        _ = v_safe_id(worker_id, "worker_id")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE worker_identities SET status = 'revoked' WHERE worker_id = ?",
                (worker_id,),
            )
            if cursor.rowcount != 1:
                raise WorkerIdentityError(
                    ERR_UNAUTHORIZED, "no worker identity with that id"
                )

    # ── Administration reads ─────────────────────────────────────────

    def list_identities(self) -> tuple[WorkerIdentityRecord, ...]:
        """All worker identities in deterministic (worker_id) order."""
        with self._lock:
            rows = cast(
                "list[tuple[object, ...]]",
                self._connection.execute(
                    """
                    SELECT worker_id, label, status, created_at, credential_rotated_at
                    FROM worker_identities ORDER BY worker_id
                    """
                ).fetchall(),
            )
        records: list[WorkerIdentityRecord] = []
        for row in rows:
            rotated = row[4]
            records.append(
                WorkerIdentityRecord(
                    worker_id=cast("str", row[0]),
                    label=None if row[1] is None else cast("str", row[1]),
                    status=cast("str", row[2]),
                    created_at=cast("str", row[3]),
                    credential_rotated_at=None if rotated is None else cast("str", rotated),
                )
            )
        return tuple(records)

    def get_identity(self, worker_id: str) -> WorkerIdentityRecord | None:
        for record in self.list_identities():
            if record.worker_id == worker_id:
                return record
        return None

    # ── Bounded growth ───────────────────────────────────────────────

    def _prune_expired_locked(self, now: datetime) -> None:
        """Bound the pairing-code table: codes dead for a day are history."""
        _ = self._connection.execute(
            "DELETE FROM pairing_codes WHERE expires_at < ?",
            (_canonical(now - timedelta(days=1)),),
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Canonical file name of the worker identity store beside the server's
#: main store in the server data directory (one pairing system, one place).
WORKERS_STORE_FILE_NAME = "workers.sqlite3"


def default_worker_store_path(data_dir: str | os.PathLike[str]) -> str:
    """The worker identity store path inside a server data directory.

    The M04/M05/M09 integration keeps exactly ONE pairing system: the M05
    identity store, placed beside the server store in the same ``0o700``
    server data directory. The composition root, the control plane's
    default and the ``doctor`` command all resolve the location through
    this helper.
    """
    return os.path.join(os.fspath(data_dir), WORKERS_STORE_FILE_NAME)




def ensure_private_tree(path: str) -> None:
    """Create ``path`` and any missing ancestor as 0o700 (never loosen)."""
    current = os.path.abspath(path)
    missing: list[str] = []
    probe = current
    while probe and not os.path.isdir(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
        os.chmod(directory, 0o700)



class WorkerAdminService:
    """The typed administration seam over one identity store (M09 surface).

    M09's control API/UI composes this service; this module ships no HTTP,
    CLI or UI itself. Every operation is an administrator act (D-044).
    """

    def __init__(self, store: WorkerIdentityStore) -> None:
        self._store: WorkerIdentityStore = store

    def begin_pairing(self, *, label: str | None = None) -> PairingCode:
        return self._store.begin_pairing(label=label)

    def list_workers(self) -> tuple[WorkerIdentityRecord, ...]:
        return self._store.list_identities()

    def revoke_worker(self, worker_id: str) -> None:
        self._store.revoke(worker_id)

    def rotate_worker_credential(self, worker_id: str) -> str:
        """New credential value (transient; deliver to the device out of band)."""
        return self._store.rotate_credential(worker_id)


ERR_UNAUTHORIZED = "unauthorized"
"""Worker-identity failure reason codes.

These mirror the worker protocol's closed error vocabulary (the endpoint
translates typed failures one-to-one into protocol error messages); the
tests assert the equality so drift cannot happen silently.
"""
ERR_PAIRING_INVALID = "pairing_code_invalid"
ERR_PAIRING_EXPIRED = "pairing_code_expired"
ERR_PAIRING_USED = "pairing_code_used"
ERR_PAIRING_UNAVAILABLE = "pairing_unavailable"
ERR_CREDENTIAL_REVOKED = "credential_revoked"

__all__ = [
    "DEFAULT_PAIRING_CODE_TTL_SECONDS",
    "MAX_OUTSTANDING_PAIRING_CODES",
    "MAX_PAIRING_CODE_TTL_SECONDS",
    "WORKER_IDENTITY_STORE_SCHEMA_VERSION",
    "WORKERS_STORE_FILE_NAME",
    "PairingCode",
    "WorkerAdminService",
    "WorkerIdentityError",
    "WorkerIdentityRecord",
    "WorkerIdentityStore",
    "default_worker_store_path",
]
