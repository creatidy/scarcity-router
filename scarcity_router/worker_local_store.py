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

import errno
import os
import sqlite3
import stat
import sys
import threading
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import final, cast

from .gateway_validation import v_safe_id, v_text
from .worker_identity_store import ensure_private_tree, open_private_state_dir

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


class WorkerStateDirLocked(Exception):
    """Another worker process already holds the state directory.

    ``holder_pid`` is best-effort telemetry read from the lock file (the
    OS releases the advisory lock on any process death, so a held lock
    always means a LIVE holder — the pid line can only be stale across
    an unclean kill that also left a new process to reuse the pid).
    """

    def __init__(self, state_dir: str, holder_pid: int | None) -> None:
        self.state_dir: str = state_dir
        self.holder_pid: int | None = holder_pid
        suffix = f" (pid {holder_pid})" if holder_pid is not None else ""
        super().__init__(
            "another worker process is already running for this worker "
            + f"state directory{suffix}; stop it first (with the service "
            + "installed: `systemctl --user stop scarcity-router-worker`), "
            + "or choose a different --state-dir"
        )


class WorkerStateDirLockUnavailable(ValueError):
    """The lock file exists but is not a safe regular file.

    A planted ``worker.lock`` — a symlink or another non-regular object —
    is never followed: acquisition refuses without reading or writing the
    planted object's target (the Daybreak review blocker: following it
    would truncate and overwrite an attacker-chosen file with the
    authority of the worker process).
    """


class WorkerStateDirLock:
    """The lifetime single-instance lock for one worker state directory.

    Exactly one worker runtime may serve a state directory at a time
    (issue #138): a foreground ``run`` and the systemd service must never
    run concurrently — two runtimes would double-report state and race
    for dispatches. The lock is an OS advisory lock on
    ``<state_dir>/worker.lock`` (``flock`` on POSIX, ``msvcrt.locking``
    on Windows), so it is released by the OS on EVERY exit — clean stop,
    unhandled exception, SIGKILL — and a stale lock file can never block
    a later start.

    The lock file itself is opened without following symlinks or reparse
    points and only inside the verified private state directory (the
    Daybreak review blocker): a planted ``worker.lock`` symlink produces
    a typed refusal and its target is never touched. The holder pid is
    read back through the ALREADY-OPEN descriptor, so no second pathname
    resolution can be redirected either.
    """

    LOCK_FILE_NAME: str = "worker.lock"

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self._directory: str = os.fspath(state_dir)
        self._fd: int | None = None
        self._dir_fd: int | None = None
        self._directory_handles: list[int] = []

    def acquire(self) -> None:
        """Take the exclusive lock; :class:`WorkerStateDirLocked` when held.

        Fails closed: an acquisition error is never downgraded to a
        warning, because running a second runtime against one state
        directory corrupts the honest single-worker assumption.
        """
        if self._fd is not None:
            raise WorkerStateDirLocked(self._directory, None)
        if sys.platform == "win32":
            fd, self._directory_handles = _open_lock_file_windows(
                self._directory, self.LOCK_FILE_NAME
            )
        else:
            self._directory = os.path.realpath(self._directory)
            self._dir_fd = open_private_state_dir(self._directory, create=True)
            try:
                fd = _open_lock_file(self._dir_fd, self._directory, self.LOCK_FILE_NAME)
            except BaseException:
                os.close(self._dir_fd)
                self._dir_fd = None
                raise
        try:
            try:
                _lock_fd_exclusive(fd)
            except (OSError, ValueError):
                holder = _holder_pid_from_fd(fd)
                raise WorkerStateDirLocked(self._directory, holder) from None
            # Locked: record the holder pid for the failure message of the
            # NEXT contender. Byte 0 stays reserved for the advisory lock
            # itself (msvcrt.locking locks exactly that byte on Windows,
            # and a locked byte denies reads to contenders — so the pid
            # line starts at byte 1 and stays readable while held). Never
            # remove this file on release (a removal would race a
            # concurrent opener); its content is advisory only.
            _ = os.ftruncate(fd, 0)
            _ = os.lseek(fd, 0, os.SEEK_SET)
            _ = os.write(fd, b"\n" + f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            self._close_directory()
            raise
        self._fd = fd

    def release(self) -> None:
        """Release the lock (idempotent; safe after any process state)."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            _unlock_fd(fd)
        finally:
            os.close(fd)
            self._close_directory()

    def _close_directory(self) -> None:
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None
        if self._directory_handles:
            _close_windows_directory_handles(self._directory_handles)
            self._directory_handles = []


# ── Lock-file opening (no symlink/reparse-point following) ────────────────────

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _open_lock_file(dir_fd: int, directory: str, name: str) -> int:
    """Open (or create) the lock file WITHOUT following a planted link.

    POSIX: the file is opened relative to an already-open descriptor for
    the verified private state directory (``openat`` style, minimizing
    pathname re-resolution) with ``O_NOFOLLOW`` so a planted symlink
    fails with ``ELOOP`` instead of being followed, and the opened
    descriptor is verified with ``fstat`` to be a regular file (a FIFO or
    device planted under the lock name is refused, never opened into).
    The 0o600 mode is enforced on the open descriptor either way, so a
    pre-existing loosened file is tightened before any content is written.

    The caller retains the verified directory descriptor for the entire
    lock lifetime. This function never consults its pathname for lookup.
    """
    try:
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise WorkerStateDirLockUnavailable(
                f"the worker lock file {os.path.join(directory, name)} "
                + "is a symlink (planted?); refusing to follow it — "
                + "remove it and retry"
            ) from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise WorkerStateDirLockUnavailable(
                f"the worker lock file {os.path.join(directory, name)} is "
                + "not a regular file; refusing to use it — remove it and "
                + "retry"
            )
        os.fchmod(fd, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _close_windows_directory_handles(handles: list[int]) -> None:
    """Release a retained Windows lookup chain, leaf first."""
    if not handles:
        return
    import ctypes

    kernel32: object = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = getattr(kernel32, "CloseHandle")  # pyright: ignore[reportAny]
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    for handle in reversed(handles):
        _ = close_handle(ctypes.c_void_p(handle))  # pyright: ignore[reportAny]
    handles.clear()


def _open_lock_file_windows(directory: str, name: str) -> tuple[int, list[int]]:
    """Open the lock through a pinned, no-follow Windows directory chain.

    NtCreateFile's RootDirectory resolves ONE component against an already
    open parent. FILE_OPEN_REPARSE_POINT opens that component itself, and
    GetFileInformationByHandle rejects reparses before any child lookup.
    FILE_OPEN_IF provisions missing directories atomically through the same
    verified handles, never through pathname mkdir. Directories share READ
    only, blocking write-data opens; no handle shares DELETE, blocking
    rename/replacement. Attribute-only in-place junction conversion is
    refused by the next no-follow handle-relative lookup. The lock shares
    READ/WRITE so other workers can contend through msvcrt.locking. The
    caller must keep the returned chain until the lock descriptor is closed.

    API semantics: learn.microsoft.com/windows/win32/api/winternl/
    nf-winternl-ntcreatefile (RootDirectory, FILE_OPEN_REPARSE_POINT,
    FILE_OPEN_IF and ShareAccess). No realpath canonicalization is used.
    """
    import ctypes
    import msvcrt
    import ntpath

    path = ntpath.abspath(directory)
    profile = os.environ.get("USERPROFILE", "")
    if profile and ntpath.normcase(path.rstrip("\\")) == ntpath.normcase(
        ntpath.abspath(profile).rstrip("\\")
    ):
        raise WorkerStateDirLockUnavailable(
            "the worker state directory cannot be the home directory itself; "
            + "choose a dedicated worker state directory"
        )
    drive, tail = ntpath.splitdrive(path)
    if drive.startswith("\\\\?\\"):
        drive = drive[4:]
        if drive.upper().startswith("UNC\\"):
            drive = "\\\\" + drive[4:]
    if drive.startswith("\\\\.\\") or not drive or not tail.startswith("\\"):
        raise WorkerStateDirLockUnavailable(
            "the worker state directory must use a filesystem path, not a device path"
        )
    components = [part for part in tail.split("\\") if part]
    if not components:
        raise WorkerStateDirLockUnavailable(
            "the worker state directory cannot be the filesystem root"
        )
    for component in [*components, name]:
        if (
            not component
            or component in {".", ".."}
            or any(character in component for character in "\\/:\0")
            or component.endswith((".", " "))
        ):
            raise WorkerStateDirLockUnavailable(
                "the worker lock path contains an unsafe filesystem component"
            )
    root = "\\??\\" + (
        "UNC\\" + drive[2:] if drive.startswith("\\\\") else drive
    ) + "\\"

    # Only these Windows APIs cross the untyped ctypes boundary. Fixed ABI
    # declarations keep HANDLE/pointer widths correct on both 32/64-bit hosts.
    kernel32: object = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll: object = ctypes.WinDLL("ntdll")
    create_file = getattr(ntdll, "NtCreateFile")  # pyright: ignore[reportAny]
    status_to_error = getattr(ntdll, "RtlNtStatusToDosError")  # pyright: ignore[reportAny]
    get_file_info = getattr(kernel32, "GetFileInformationByHandle")  # pyright: ignore[reportAny]
    get_file_type = getattr(kernel32, "GetFileType")  # pyright: ignore[reportAny]
    close_handle = getattr(kernel32, "CloseHandle")  # pyright: ignore[reportAny]

    @final
    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_uint16),
            ("MaximumLength", ctypes.c_uint16),
            ("Buffer", ctypes.c_void_p),
        ]

    @final
    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_uint32),
            ("RootDirectory", ctypes.c_void_p),
            ("ObjectName", ctypes.c_void_p),
            ("Attributes", ctypes.c_uint32),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    @final
    class _IO_STATUS_BLOCK(ctypes.Structure):
        # NTSTATUS/pointer union followed by ULONG_PTR; only size/alignment
        # matter here because the synchronous call returns status directly.
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    @final
    class _FILETIME(ctypes.Structure):
        _fields_ = [("dw_low", ctypes.c_uint32), ("dw_high", ctypes.c_uint32)]

    @final
    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", ctypes.c_uint32),
            ("ftCreationTime", _FILETIME),
            ("ftLastAccessTime", _FILETIME),
            ("ftLastWriteTime", _FILETIME),
            ("dwVolumeSerialNumber", ctypes.c_uint32),
            ("nFileSizeHigh", ctypes.c_uint32),
            ("nFileSizeLow", ctypes.c_uint32),
            ("nNumberOfLinks", ctypes.c_uint32),
            ("nFileIndexHigh", ctypes.c_uint32),
            ("nFileIndexLow", ctypes.c_uint32),
        ]

    create_file.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
        ctypes.POINTER(_OBJECT_ATTRIBUTES), ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ]
    create_file.restype = ctypes.c_int32
    status_to_error.argtypes = [ctypes.c_int32]
    status_to_error.restype = ctypes.c_uint32
    get_file_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    get_file_info.restype = ctypes.c_int
    get_file_type.argtypes = [ctypes.c_void_p]
    get_file_type.restype = ctypes.c_uint32
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    def open_component(parent: int | None, component: str, *, is_directory: bool) -> int:
        length = len(component.encode("utf-16-le"))
        if length > 65532:
            raise WorkerStateDirLockUnavailable("the worker lock path component is too long")
        buffer = ctypes.create_unicode_buffer(component)
        unicode_name = _UNICODE_STRING(length, length + 2, ctypes.addressof(buffer))
        attributes = _OBJECT_ATTRIBUTES(
            ctypes.sizeof(_OBJECT_ATTRIBUTES), parent,
            ctypes.addressof(unicode_name), 0x40, None, None,  # OBJ_CASE_INSENSITIVE
        )
        io_status = _IO_STATUS_BLOCK()
        output = ctypes.c_void_p()
        status: int = create_file(  # pyright: ignore[reportAny]
            ctypes.byref(output),
            # Directory: READ_ATTRIBUTES | TRAVERSE | SYNCHRONIZE.
            0x1000A0 if is_directory else 0xC0100000,
            ctypes.byref(attributes), ctypes.byref(io_status), None, 0x80,
            # Directories: SHARE_READ only; lock: READ/WRITE for contention.
            # Neither shares DELETE. Attribute-only mutations bypass sharing;
            # RootDirectory + OPEN_REPARSE_POINT lookups fail closed when
            # an inspected directory is converted in-place to a junction.
            0x1 if is_directory else 0x3,
            1 if parent is None else 3,  # FILE_OPEN root; FILE_OPEN_IF children
            0x200000 | 0x20 | (0x1 if is_directory else 0x40),
            # OPEN_REPARSE_POINT | SYNCHRONOUS_IO_NONALERT | DIRECTORY/NON_DIRECTORY
            None, 0,
        )
        if status < 0:
            error: int = status_to_error(status)  # pyright: ignore[reportAny]
            raise WorkerStateDirLockUnavailable(
                "the worker lock path could not be opened safely "
                + f"(Windows error {error}); remove planted links/reparse points "
                + "or correct directory permissions and retry"
            )
        handle = output.value
        if handle is None:
            raise WorkerStateDirLockUnavailable("Windows returned no worker lock handle")
        try:
            info = _BY_HANDLE_FILE_INFORMATION()
            inspected: int = get_file_info(  # pyright: ignore[reportAny]
                ctypes.c_void_p(handle), ctypes.byref(info)
            )
            if not inspected:
                raise WorkerStateDirLockUnavailable(
                    "the opened worker lock path could not be inspected safely"
                )
            file_attributes = cast(int, info.dwFileAttributes)
            if file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                raise WorkerStateDirLockUnavailable(
                    "the worker lock path is a symlink or reparse point; "
                    + "refusing to follow it - remove it and retry"
                )
            file_type: int = get_file_type(ctypes.c_void_p(handle))  # pyright: ignore[reportAny]
            if bool(file_attributes & 0x10) != is_directory or file_type != 1:
                raise WorkerStateDirLockUnavailable(
                    "the worker lock path is not a filesystem directory/regular file"
                )
        except BaseException:
            _ = close_handle(ctypes.c_void_p(handle))  # pyright: ignore[reportAny]
            raise
        return handle

    handles: list[int] = []
    try:
        handles.append(open_component(None, root, is_directory=True))
        for component in components:
            handles.append(open_component(handles[-1], component, is_directory=True))
        handle = open_component(handles[-1], name, is_directory=False)
        try:
            fd = msvcrt.open_osfhandle(
                handle, os.O_RDWR | os.O_NOINHERIT | os.O_BINARY
            )
        except BaseException:
            _ = close_handle(ctypes.c_void_p(handle))  # pyright: ignore[reportAny]
            raise
    except BaseException:
        _close_windows_directory_handles(handles)
        raise
    return fd, handles


def _holder_pid_from_fd(fd: int) -> int | None:
    """The holder pid recorded in the lock file, read via the open fd.

    Reading the ALREADY-OPEN descriptor (never the path again) keeps the
    pid telemetry on the same object that was verified at open time — no
    second pathname resolution that a planted symlink could redirect. On
    Windows the read starts at byte 1: byte 0 is the byte the holding
    process has locked, and a locked byte denies reads to this contender
    (``flock`` on POSIX locks no bytes, so the full read works there).
    """
    try:
        if sys.platform == "win32":
            _ = os.lseek(fd, 1, os.SEEK_SET)
            text = os.read(fd, 64).decode("utf-8", errors="replace")
        else:
            text = os.pread(fd, 64, 0).decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            return int(line)
        except ValueError:
            return None
    return None


def _lock_fd_exclusive(fd: int) -> None:
    """Take the non-blocking exclusive advisory lock on ``fd``."""
    if sys.platform == "win32":
        import msvcrt

        _ = os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    """Release the advisory lock taken by :func:`_lock_fd_exclusive`."""
    if sys.platform == "win32":
        import msvcrt

        _ = os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = [
    "STATE_DIR_NAME",
    "WORKER_LOCAL_STORE_SCHEMA_VERSION",
    "WORKER_STATE_DIR_NAME",
    "WorkerLocalIdentity",
    "WorkerLocalStore",
    "WorkerStateDirLock",
    "WorkerStateDirLockUnavailable",
    "WorkerStateDirLocked",
    "default_worker_state_dir",
    "worker_state_dir",
]
