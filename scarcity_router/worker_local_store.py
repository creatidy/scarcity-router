"""Worker-local bounded state store: per-device identity and config (M05).

The native worker keeps ONLY bounded local state per D-041/D-044: its
per-device pairing identity (worker id + credential), the configured
server origin and its local adapter allowlist. No provider application
credential is ever copied into this store or onto the server (Codex auth
remains provider-managed, D-018); those live in their own provider-managed
stores on the worker host, untouched by this program.

**Storage mechanism (recorded choice):** stdlib ``sqlite3`` in one
database file. The state directory is created ``0o700`` and the database
file is forced ``0o600`` after creation — the recorded permissioned-file
fallback pattern (D-044); SQLite gives atomic replacement of the identity
row (pairing and rotation must never leave a half-written credential on
disk). OS-native keychains are preferred where the repository has already
authorized them; no OS keychain authorization exists yet, so this is the
recorded fallback — swapping in an OS-native backend later changes only
this module.

**Windows and WSL are different devices** and deliberately do NOT share a
credential/configuration directory:

- Windows (``sys.platform == "win32"``): ``%LOCALAPPDATA%\\scarcity-router\\worker``
  (fallback ``%USERPROFILE%\\AppData\\Local\\...``).
- WSL/Linux/everything else: XDG data home
  ``$(XDG_DATA_HOME or ~/.local/share)/scarcity-router/worker``.

WSL is Linux, so it resolves the Linux path — a Windows directory is
never consulted from WSL and vice versa, even when both run on the same
physical machine (they are separate devices with separate identities).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import cast

from .gateway_validation import v_safe_id, v_text
from .worker_identity_store import ensure_private_tree

WORKER_LOCAL_STORE_SCHEMA_VERSION = 1

STATE_DIR_NAME = "scarcity-router"
WORKER_STATE_DIR_NAME = "worker"
LOCALAPPDATA_DIR_NAME = "AppData"
WORKER_IDENTITY_KEY = "identity"

_ALL_KEYS: frozenset[str] = frozenset({
    "worker_id",
    "credential",
    "server_origin",
    "device_label",
    "adapter_allowlist",
})


def worker_state_dir(
    *,
    platform: str,
    env: Mapping[str, str] | None = None,
    home: Callable[[str], str] | None = None,
) -> str:
    """The platform-appropriate worker state directory.

    Windows and WSL/Linux resolve to different roots by construction; the
    environment mapping is injectable so tests stay deterministic.
    """
    environment = os.environ if env is None else env
    if platform == "win32":
        local_app_data = environment.get("LOCALAPPDATA", "")
        if local_app_data:
            return (
                local_app_data + "\\" + STATE_DIR_NAME + "\\" + WORKER_STATE_DIR_NAME
            )
        profile = environment.get("USERPROFILE", "")
        if profile:
            return (
                profile
                + "\\"
                + LOCALAPPDATA_DIR_NAME
                + "\\Local\\"
                + STATE_DIR_NAME
                + "\\"
                + WORKER_STATE_DIR_NAME
            )
        raise ValueError("worker_state_dir: no Windows local-app-data location available")
    # WSL/Linux (and every other platform this program supports today):
    # the XDG data home hierarchy, with "/" separators. WSL never resolves
    # to a Windows path.
    xdg_data_home = environment.get("XDG_DATA_HOME", "")
    if xdg_data_home and os.path.isabs(xdg_data_home):
        base = xdg_data_home
    else:
        home_value = environment.get("HOME", "")
        if home_value:
            base = home_value + "/.local/share"
        else:
            resolve_home = home if home is not None else os.path.expanduser
            base = resolve_home("~") + "/.local/share"
    return os.path.join(base, STATE_DIR_NAME, WORKER_STATE_DIR_NAME)


def default_worker_state_dir() -> str:
    """The state directory for the RUNNING platform (real environment)."""
    import sys

    return worker_state_dir(platform=sys.platform)


@dataclass(frozen=True)
class WorkerLocalIdentity:
    """The paired per-device identity as stored locally."""

    worker_id: str
    credential: str
    server_origin: str
    device_label: str | None = None


def _canonical(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkerLocalStore:
    """The worker's bounded local state (identity, origin, allowlist).

    One SQLite database in the platform-appropriate state directory.
    Thread-safe; every write is atomic.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path: str = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(self._path))
        if parent:
            ensure_private_tree(parent)
        self._lock: threading.Lock = threading.Lock()
        self._connection: sqlite3.Connection = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False
        )
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            self._connection.close()
            raise
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            _ = self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            row = cast(
                "tuple[object] | None",
                self._connection.execute(
                    "SELECT value FROM worker_state WHERE key = 'schema_version'"
                ).fetchone(),
            )
            if row is None:
                _ = self._connection.execute(
                    "INSERT INTO worker_state (key, value, updated_at) VALUES (?, ?, ?)",
                    ("schema_version", str(WORKER_LOCAL_STORE_SCHEMA_VERSION), _canonical(_utcnow())),
                )
            elif str(row[0]) != str(WORKER_LOCAL_STORE_SCHEMA_VERSION):
                self._connection.close()
                raise ValueError(
                    "worker_local_store: schema version "
                    + f"{row[0]} is not {WORKER_LOCAL_STORE_SCHEMA_VERSION}; "
                    + "an explicit migration is required"
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ── Identity (one atomic row) ────────────────────────────────────

    def save_identity(self, identity: WorkerLocalIdentity) -> None:
        """Persist the paired identity atomically (never half-written)."""
        _ = v_safe_id(identity.worker_id, "identity.worker_id")
        _ = v_text(identity.credential, "identity.credential", max_len=512)
        _ = v_text(identity.server_origin, "identity.server_origin", max_len=512)
        label = (
            None
            if identity.device_label is None
            else v_safe_id(identity.device_label, "identity.device_label")
        )
        document = f"{identity.worker_id}\n{identity.credential}\n{identity.server_origin}\n{label or ''}"
        with self._lock:
            _ = self._connection.execute(
                """
                INSERT INTO worker_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (WORKER_IDENTITY_KEY, document, _canonical(_utcnow())),
            )

    def load_identity(self) -> WorkerLocalIdentity | None:
        """The stored identity, or ``None`` when this device is unpaired."""
        with self._lock:
            row = cast(
                "tuple[object] | None",
                self._connection.execute(
                    "SELECT value FROM worker_state WHERE key = ?",
                    (WORKER_IDENTITY_KEY,),
                ).fetchone(),
            )
        if row is None:
            return None
        parts = str(row[0]).split("\n")
        if len(parts) != 4:
            # A corrupted identity is a loud failure, never a guess.
            raise ValueError("worker_local_store: the stored identity is malformed")
        worker_id, credential, origin, label = parts
        return WorkerLocalIdentity(
            worker_id=worker_id,
            credential=credential,
            server_origin=origin,
            device_label=label or None,
        )

    def clear_identity(self) -> None:
        """Forget the paired identity (unpair/re-register path)."""
        with self._lock:
            _ = self._connection.execute(
                "DELETE FROM worker_state WHERE key = ?", (WORKER_IDENTITY_KEY,)
            )

    # ── Small configuration values ───────────────────────────────────

    def save_value(self, key: str, value: str) -> None:
        checked_key = v_safe_id(key, "worker_state.key")
        if checked_key == "schema_version":
            raise ValueError("worker_local_store: schema_version is reserved")
        _ = v_text(value, f"worker_state[{key}]", max_len=65536)
        with self._lock:
            _ = self._connection.execute(
                """
                INSERT INTO worker_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (checked_key, value, _canonical(_utcnow())),
            )

    def load_value(self, key: str) -> str | None:
        checked_key = v_safe_id(key, "worker_state.key")
        with self._lock:
            row = cast(
                "tuple[object] | None",
                self._connection.execute(
                    "SELECT value FROM worker_state WHERE key = ?", (checked_key,)
                ).fetchone(),
            )
        return None if row is None else str(row[0])


__all__ = [
    "STATE_DIR_NAME",
    "WORKER_LOCAL_STORE_SCHEMA_VERSION",
    "WORKER_STATE_DIR_NAME",
    "WorkerLocalIdentity",
    "WorkerLocalStore",
    "default_worker_state_dir",
    "worker_state_dir",
]
