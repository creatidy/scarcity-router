"""Secure OpenAI Codex app-server capacity acquisition.

Production acquisition boundary between a locally installed Codex app-server
binary and the pure ``parse_codex_rate_limits_result`` parser:

    discovered VS Code ChatGPT extension installation
      -> validated ``codex app-server`` subprocess (stderr discarded)
      -> JSONL: initialize request -> initialized notification
         -> account/rateLimits/read request
      -> responses matched by request identity and message structure,
         never by timing or line order
      -> existing parse_codex_rate_limits_result(...)
      -> CapacitySnapshot

Security contract (docs/security.md):

- no credential is read, stored, attached or exposed by this module: the
  app-server binary uses its own authenticated local state and this module
  never opens it;
- the child's stderr is discarded at the process level (``DEVNULL``) and is
  never read into the process, so no upstream tool output can leak through
  exceptions, diagnostics or snapshots;
- stdout is read only through a bounded reader: per-line and total byte
  budgets, strict UTF-8 and strict JSON decoding that rejects duplicate
  object keys at every nesting depth and non-finite numbers (literal
  NaN/Infinity constants and non-finite exponent results such as
  ``1e10000``), with decoder recursion failures on adversarially nested
  payloads treated as drift; so each protocol message has exactly one
  finite interpretation. Budget and decoding violations surface as safe
  statuses, never as raw content in exceptions;
- requests are exactly three bounded writes (initialize, initialized
  notification, rate-limits read); there is no retry, no prompt and no other
  method call, so collection can never issue a model request;
- every failure path attempts bounded termination and reap — including a
  reader startup failure before the session begins: stdin is closed, the
  process is terminated (then killed if it refuses to exit) and the reader
  is joined. Collection degrades to ``unavailable`` if a wait cannot prove
  that the child was reaped;
- binary discovery is read-only and bounded: it lists only the two evidenced
  VS Code extension roots, considers only ``openai.chatgpt-*`` directories,
  and validates the installation layout through the extension's own
  ``codex-package.json`` (``layoutVersion`` 1, ``variant`` "codex"). It never
  searches browser profiles, PATH, home-directory trees at large, and never
  installs, upgrades or reconfigures anything;
- the selected binary path, extension version, codex version, initialize
  result contents (which include local paths) and any error-response message
  text never enter the returned snapshot: v1 has no field for them. Error
  responses map to ``unknown`` without parsing their free text, because no
  OpenAI failure-shape evidence is validated.

Discovery evidence (U-001, docs/poc-evidence.md 2026-09-03 reconnaissance):
the PoC binary lives in a VS Code ChatGPT extension at
``<extensions>/openai.chatgpt-<version>-<platform>/bin/<platform-dir>/codex``
with a sibling ``codex-package.json``. Supported roots are
``~/.vscode/extensions`` and ``~/.vscode-server/extensions`` (the remote
server layout is the directly evidenced PoC environment); among matching
installations the highest extension version wins deterministically. Other
installation sources (npm/standalone ``codex`` on PATH, other editors) are
intentionally unsupported in M1.

Expected operational conditions normalize to safe v1 snapshots
(docs/capacity-model.md): no installation, spawn failure, process exit or
timeout maps to ``unavailable``; an installation whose layout cannot be
validated maps to ``unsupported``; malformed or incompatible JSONL
(including budget violations) maps to ``schema_changed``; a protocol error
response for one of our requests maps to ``unknown``. Programmer errors are
not disguised as telemetry failures and raise ``RuntimeError`` with
credential-free, path-free messages.
"""

from __future__ import annotations

import json
import math
import os
import platform
import queue
import select
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, Protocol, cast

from ..capacity import CapacityDiagnostic, CapacitySnapshot
from .openai_codex import (
    PROVIDER,
    SOURCE,
    classify_app_server_message,
    parse_codex_rate_limits_result,
)

# ── Discovery contract (U-001 minimum defensible behavior) ───────────────────

DEFAULT_DISCOVERY_ROOTS: tuple[Path, ...] = (
    Path.home() / ".vscode" / "extensions",
    Path.home() / ".vscode-server" / "extensions",
)

_EXTENSION_PREFIX = "openai.chatgpt-"
_CODEX_PACKAGE_NAME = "codex-package.json"
_REQUIRED_LAYOUT_VERSION = 1
_REQUIRED_VARIANT = "codex"
MAX_PACKAGE_BYTES = 64 * 1024

# Evidenced platform directory under the extension's ``bin/``. linux-x86_64
# is directly evidenced (PoC environment); descriptor-bound execution is
# intentionally supported only on Linux until another platform is evidenced.
_PLATFORM_DIRS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "aarch64"): "linux-arm64",
}

DiscoveryOutcome = Literal["found", "not_installed", "unsupported_installation"]


@dataclass
class CodexInstallation:
    """One validated, supported Codex app-server installation."""

    binary: Path
    binary_fd: int
    extension_version: str
    codex_version: str

    def close(self) -> None:
        if self.binary_fd >= 0:
            try:
                os.close(self.binary_fd)
            except OSError:
                pass
            self.binary_fd = -1

    def __del__(self) -> None:
        self.close()


@dataclass
class _DiscoveryCandidate:
    """A candidate held open beneath its configured extension root."""

    root_fd: int
    candidate_fd: int
    path: Path


def _directory_flags(*, listing: bool = False) -> int:
    return (
        (os.O_RDONLY if listing else getattr(os, "O_PATH", os.O_RDONLY))
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_at(parent_fd: int, name: str) -> int | None:
    """Open and verify one directory component relative to a held directory."""
    try:
        fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            os.close(fd)
            return None
        if not stat.S_ISDIR(mode):
            os.close(fd)
            return None
        return fd
    except OSError:
        return None


def _open_directory(path: Path) -> int | None:
    """Open and verify a configured root without following a symlink."""
    try:
        fd = os.open(str(path), _directory_flags(listing=True))
    except OSError:
        return None
    try:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return None


def _open_regular_executable_at(parent_fd: int, name: str) -> int | None:
    """Open one executable relative to a held directory without symlinks."""
    flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode) and mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            return fd
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return None


def _open_package_at(parent_fd: int) -> int | None:
    """Open the package file relative to the held platform directory."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        fd = os.open(_CODEX_PACKAGE_NAME, flags, dir_fd=parent_fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return None


# ── Process/session bounds ────────────────────────────────────────────────────

STARTUP_TIMEOUT_SECONDS = 10.0
SESSION_TIMEOUT_SECONDS = 20.0
TERMINATE_TIMEOUT_SECONDS = 2.0
MAX_TIMEOUT_SECONDS = threading.TIMEOUT_MAX
MAX_LINE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 1024 * 1024

_INITIALIZE_ID = 1
_READ_RATE_LIMITS_ID = 2
_RATE_LIMITS_METHOD = "account/rateLimits/read"
_INITIALIZED_NOTIFICATION_METHOD = "initialized"
_INITIALIZE_RESPONSE_FIELDS = (
    "userAgent",
    "codexHome",
    "platformFamily",
    "platformOs",
)


class _AmbiguousJson(ValueError):
    """Raised for non-standard JSON constants (NaN/Infinity/-Infinity).

    Standard JSON has no such values; a stream containing them is not the
    validated protocol and must fail closed. The message never carries any
    decoded content.
    """


def _reject_json_constant(_name: str) -> object:
    """``json.loads`` ``parse_constant`` hook: reject NaN/Infinity."""
    raise _AmbiguousJson()


def _finite_float(text: str) -> float:
    """``json.loads`` ``parse_float`` hook: reject non-finite results.

    Standard JSON numeric syntax such as ``1e10000`` parses to ``inf`` in
    Python; a non-finite quantity is not the validated protocol and must
    fail closed exactly like the literal NaN/Infinity constants.
    """
    value = float(text)
    if not math.isfinite(value):
        raise _AmbiguousJson()
    return value


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """JSON ``object_pairs_hook`` that refuses duplicate keys anywhere.

    Applied at every nesting depth of every decoded document (protocol
    messages and the installation package file alike): a duplicate key
    makes the message layout ambiguous, and decoding must fail closed
    instead of trusting last-key-wins parsing. The message never contains
    any document value.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _AmbiguousJson()
        result[key] = value
    return result


def _is_protocol_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    """True for a real JSON integer; booleans are not integers."""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow a decoded boundary value to a ``str``-keyed dict, or ``None``."""
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def platform_directory() -> str | None:
    """The evidenced ``bin/`` subdirectory for the current platform, if any.

    Public because tests build host-matching installation trees from it and
    future ``doctor`` output reports it; it never touches the filesystem.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    return _PLATFORM_DIRS.get((system, machine))


def _extension_version_key(name: str) -> tuple[int, ...] | None:
    """Deterministic sort key from the extension directory's version token.

    ``openai.chatgpt-26.825.51511-linux-x64`` -> ``(26, 825, 51511)``.
    Only ASCII numeric segments are accepted: Unicode digit-lookalikes
    (``²`` and friends) make the version malformed, and the candidate is
    reported as ``None`` so discovery skips or refuses it deterministically
    instead of raising.
    """
    rest = name[len(_EXTENSION_PREFIX) :]
    first = rest.split("-", 1)[0]
    if not first:
        return None
    parts: list[int] = []
    for segment in first.split("."):
        if not (segment.isascii() and segment.isdigit()):
            return None
        parts.append(int(segment))
    return tuple(parts)


def _extension_version_string(name: str) -> str:
    rest = name[len(_EXTENSION_PREFIX) :]
    return rest.split("-", 1)[0]


def _candidate_directories(
    roots: Sequence[Path],
) -> tuple[list[_DiscoveryCandidate], int]:
    """``openai.chatgpt-*`` extension directories, newest first.

    Each root is listed once (no recursion); unreadable or absent roots are
    skipped. Returns the versioned candidates ordered deterministically
    (version descending, then directory name as a tie-break) plus the count
    of malformed-version directories that were skipped (they make discovery
    report ``unsupported_installation`` when nothing usable exists, rather
    than pretending nothing is installed).
    """
    valid: list[tuple[tuple[int, ...], str, _DiscoveryCandidate]] = []
    malformed = 0
    for root in roots:
        root_fd = _open_directory(root)
        if root_fd is None:
            continue
        try:
            entries = os.listdir(root_fd)
        except OSError:
            os.close(root_fd)
            continue
        found = False
        for name in entries:
            if not name.startswith(_EXTENSION_PREFIX):
                continue
            candidate_fd = _open_directory_at(root_fd, name)
            if candidate_fd is None:
                malformed += 1
                continue
            key = _extension_version_key(name)
            if key is None:
                malformed += 1
                os.close(candidate_fd)
                continue
            valid.append(
                (
                    key,
                    name,
                    _DiscoveryCandidate(root_fd, candidate_fd, root / name),
                )
            )
            found = True
        if not found:
            try:
                os.close(root_fd)
            except OSError:
                pass
    valid.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [candidate for _key, _name, candidate in valid], malformed


def _read_validated_package(
    package_fd: int,
) -> dict[str, object] | None:
    """Read and validate an already-open ``codex-package.json`` descriptor.

    The descriptor was opened relative to a held platform directory without
    following symlinks and verified to be a regular file (a FIFO or socket can
    never block discovery). The read is bounded to ``MAX_PACKAGE_BYTES``.
    Decoding is strict UTF-8/JSON with
    duplicate-key rejection, non-finite-number rejection (literal
    NaN/Infinity constants and non-finite exponent results) and
    recursion-safe handling, exactly like the JSONL transport. The
    evidenced layout contract is enforced: integer ``layoutVersion == 1``,
    string ``variant == "codex"`` and a non-empty string ``version``. Any
    failure makes this candidate unusable (``None``).
    """
    fd = package_fd
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_PACKAGE_BYTES:
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                return None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    raw = b"".join(chunks)
    if len(raw) > MAX_PACKAGE_BYTES:
        return None
    try:
        document = _as_object_dict(
            cast(
                "object",
                json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_object_without_duplicate_keys,
                    parse_constant=_reject_json_constant,
                    parse_float=_finite_float,
                ),
            )
        )
    except (ValueError, RecursionError):
        return None
    if document is None:
        return None
    if not _is_int(document.get("layoutVersion")):
        return None
    if cast(int, document["layoutVersion"]) != _REQUIRED_LAYOUT_VERSION:
        return None
    if document.get("variant") != _REQUIRED_VARIANT:
        return None
    version = document.get("version")
    if not isinstance(version, str) or not version:
        return None
    return document


def _close_discovery_candidates(candidates: Sequence[_DiscoveryCandidate]) -> None:
    closed_roots: set[int] = set()
    for candidate in candidates:
        try:
            os.close(candidate.candidate_fd)
        except OSError:
            pass
        if candidate.root_fd not in closed_roots:
            closed_roots.add(candidate.root_fd)
            try:
                os.close(candidate.root_fd)
            except OSError:
                pass


def discover_codex_installation(
    roots: Sequence[Path] | None = None,
) -> tuple[CodexInstallation | None, DiscoveryOutcome]:
    """Discover one supported Codex app-server installation, read-only.

    Returns ``(installation, outcome)``. ``not_installed`` means no
    ``openai.chatgpt-*`` extension directory exists in any root (or the
    current platform has no evidenced platform directory at all);
    ``unsupported_installation`` means installations exist but none carries
    a validated layout (executable ``codex`` binary in the platform
    directory plus a layout-version-1 ``codex`` package file). Candidates
    are tried newest-first, so a newer unsupported layout never masks an
    older supported one.
    """
    search_roots = DEFAULT_DISCOVERY_ROOTS if roots is None else roots
    candidates, malformed = _candidate_directories(search_roots)
    if not candidates and not malformed:
        return None, "not_installed"
    platform_directory_ = platform_directory()
    if platform_directory_ is None:
        _close_discovery_candidates(candidates)
        return None, "unsupported_installation"
    try:
        for candidate in candidates:
            bin_fd = _open_directory_at(candidate.candidate_fd, "bin")
            if bin_fd is None:
                continue
            platform_fd = _open_directory_at(bin_fd, platform_directory_)
            if platform_fd is None:
                os.close(bin_fd)
                continue
            binary_fd = _open_regular_executable_at(platform_fd, "codex")
            if binary_fd is None:
                os.close(platform_fd)
                os.close(bin_fd)
                continue
            package_fd = _open_package_at(platform_fd)
            if package_fd is None:
                os.close(binary_fd)
                os.close(platform_fd)
                os.close(bin_fd)
                continue
            package = _read_validated_package(package_fd)
            os.close(platform_fd)
            os.close(bin_fd)
            if package is None:
                os.close(binary_fd)
                continue
            installation = CodexInstallation(
                binary=candidate.path / "bin" / platform_directory_ / "codex",
                binary_fd=binary_fd,
                extension_version=_extension_version_string(candidate.path.name),
                codex_version=cast("str", package["version"]),
            )
            return installation, "found"
        return None, "unsupported_installation"
    finally:
        _close_discovery_candidates(candidates)


# ── Bounded subprocess stdout reader ──────────────────────────────────────────


class _Poller(Protocol):
    def register(self, fd: int, eventmask: int) -> None: ...

    def poll(self, timeout: int) -> list[tuple[int, int]]: ...


def _fd_readable(fd: int) -> bool:
    """Wait briefly for a descriptor without ``select``'s FD_SETSIZE limit."""
    poll_factory = cast(
        "Callable[[], _Poller] | None", getattr(select, "poll", None)
    )
    if poll_factory is not None:
        poller = poll_factory()
        poller.register(
            fd,
            select.POLLIN | select.POLLHUP | select.POLLERR,
        )
        return bool(poller.poll(100))
    ready, _, _ = select.select([fd], [], [], 0.1)
    return bool(ready)


class BoundedLineReader:
    """Background bounded reader for the child's stdout.

    Lines (bytes) are delivered through a queue. Per-line and total byte
    budgets are enforced by stopping the reader and emitting an ``oversized``
    marker; I/O failures emit ``failed`` and end-of-file emits ``eof``. The
    reader never raises with content: violations and errors surface as bare
    markers, and line bytes never appear in any exception or flag.
    """

    _stream: IO[bytes]
    _max_line_bytes: int
    _max_total_bytes: int
    _queue: "queue.Queue[tuple[str, bytes | None]]"
    _fd: int | None
    _thread: threading.Thread
    _stop: threading.Event
    _started: bool

    def __init__(
        self,
        stream: IO[bytes],
        *,
        max_line_bytes: int,
        max_total_bytes: int,
    ) -> None:
        self._stream = stream
        self._max_line_bytes = max_line_bytes
        self._max_total_bytes = max_total_bytes
        try:
            self._fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            self._fd = None
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="codex-app-server-stdout"
        )
        self._started = False

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def get(self, timeout: float) -> tuple[str, bytes | None]:
        """Fetch the next reader event, blocking up to ``timeout`` seconds.

        Raises :class:`queue.Empty` when the timeout elapses first.
        """
        return self._queue.get(timeout=timeout)

    def join(self, timeout: float) -> None:
        """Join the reader thread if it was ever started; never raises."""
        if self._started:
            self._thread.join(timeout=timeout)

    def close(self) -> None:
        """Stop and close the stream so a retained pipe writer cannot block it."""
        self._stop.set()
        if not self._started:
            self._close_stream()

    def stopped(self) -> bool:
        """Whether the reader thread has stopped, including if never started."""
        return not self._started or not self._thread.is_alive()

    def _close_stream(self) -> None:
        try:
            self._stream.close()
        except (OSError, ValueError):
            pass

    def _put(self, kind: str, chunk: bytes | None) -> None:
        self._queue.put((kind, chunk))

    def _loop(self) -> None:
        try:
            if self._fd is not None:
                self._loop_fd(self._fd)
            else:
                self._loop_stream()
        finally:
            self._close_stream()

    def _loop_fd(self, fd: int) -> None:
        """Read a real pipe without blocking on a partial line."""
        try:
            os.set_blocking(fd, False)
        except OSError:
            self._put("failed", None)
            return
        buffer = bytearray()
        total = 0
        while not self._stop.is_set():
            try:
                ready = _fd_readable(fd)
            except (OSError, ValueError):
                if not self._stop.is_set():
                    self._put("failed", None)
                return
            if not ready:
                continue
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                continue
            except OSError:
                if not self._stop.is_set():
                    self._put("failed", None)
                return
            if not chunk:
                if buffer:
                    if len(buffer) > self._max_line_bytes:
                        self._put("oversized", None)
                        return
                    total += len(buffer)
                    if total > self._max_total_bytes:
                        self._put("oversized", None)
                        return
                    self._put("line", bytes(buffer))
                self._put("eof", None)
                return
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > self._max_line_bytes:
                        self._put("oversized", None)
                        return
                    break
                line = bytes(buffer[: newline + 1])
                del buffer[: newline + 1]
                if len(line) > self._max_line_bytes:
                    self._put("oversized", None)
                    return
                total += len(line)
                if total > self._max_total_bytes:
                    self._put("oversized", None)
                    return
                self._put("line", line)

    def _loop_stream(self) -> None:
        """Fallback for non-file test streams with a stoppable read loop."""
        total = 0
        while not self._stop.is_set():
            try:
                chunk = self._stream.readline(self._max_line_bytes + 1)
            except (OSError, ValueError):
                if not self._stop.is_set():
                    self._put("failed", None)
                return
            if not chunk:
                self._put("eof", None)
                return
            if len(chunk) > self._max_line_bytes:
                self._put("oversized", None)
                return
            total += len(chunk)
            if total > self._max_total_bytes:
                self._put("oversized", None)
                return
            self._put("line", chunk)


# ── Subprocess seam ───────────────────────────────────────────────────────────


def _fd_execution_path(fd: int) -> str | None:
    if sys.platform.startswith("linux"):
        return f"/proc/self/fd/{fd}"
    return None


def spawn_app_server(
    argv: Sequence[str],
    *,
    executable_fd: int | None = None,
) -> "subprocess.Popen[bytes]":
    """Launch the Codex app-server subprocess.

    Exactly one process, never a shell; stdout is captured for the JSONL
    session and stderr is discarded at the process level (never a pipe, so
    upstream tool output can never be read back or leak). Test seam: tests
    replace this function with fakes; no test executes a real binary.
    """
    command = list(argv)
    pass_fds: tuple[int, ...] = ()
    if executable_fd is not None:
        execution_path = _fd_execution_path(executable_fd)
        if execution_path is None:
            raise OSError("platform cannot execute an identity-bound descriptor")
        command[0] = execution_path
        pass_fds = (executable_fd,)
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        pass_fds=pass_fds,
    )


def _shutdown(
    proc: "subprocess.Popen[bytes]",
    reader: BoundedLineReader,
) -> bool:
    """Terminate the child and report whether a bounded wait proved reaping.

    Order: close stdin (EOF), terminate (SIGTERM-equivalent), wait bounded,
    kill if the child refuses to exit, then join the reader briefly. The
    child's death closes the pipe, which releases the reader thread. A wait
    failure is never treated as proof of reaping; the caller must degrade
    safely when this returns ``False``.
    """
    reaped = False
    # Release the parent's stdout descriptor before waiting for the child.
    # The child can exit while another process still holds a duplicate write
    # end, so process reaping alone must not leave the reader blocked.
    reader.close()
    try:
        stdin = proc.stdin
        if stdin is not None:
            stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        _ = proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        reaped = True
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            _ = proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            reaped = True
        except (subprocess.TimeoutExpired, OSError):
            pass
    except OSError:
        # The process may already have exited, but an unsuccessful wait does
        # not prove that it was reaped. Try the kill/final-wait path once.
        try:
            proc.kill()
        except OSError:
            pass
        try:
            _ = proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            reaped = True
        except (subprocess.TimeoutExpired, OSError):
            pass
    reader.join(timeout=1.0)
    return reaped and reader.stopped()


# ── JSONL session ─────────────────────────────────────────────────────────────


def _initialize_request() -> dict[str, object]:
    return {
        "id": _INITIALIZE_ID,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "scarcity-router",
                "title": "Scarcity Router",
                "version": "0.0.0",
            },
            "capabilities": {},
        },
    }


def _initialized_notification() -> dict[str, object]:
    return {
        "method": _INITIALIZED_NOTIFICATION_METHOD,
    }


def _read_rate_limits_request() -> dict[str, object]:
    return {
        "id": _READ_RATE_LIMITS_ID,
        "method": _RATE_LIMITS_METHOD,
    }


def _send_message(
    proc: "subprocess.Popen[bytes]",
    payload: Mapping[str, object],
) -> bool:
    """One bounded write; ``False`` means the child is gone/unwritable."""
    stdin = cast("IO[bytes]", proc.stdin)
    try:
        _ = stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        stdin.flush()
    except (OSError, ValueError):
        return False
    return True


def _next_message(
    reader: BoundedLineReader,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[str, object | None]:
    """Read and decode the next JSONL message, bounded by ``deadline``.

    Returns one of ``("message", decoded)``, ``("timeout", None)``,
    ``("eof", None)``, ``("failed", None)``, ``("oversized", None)`` or
    ``("malformed", None)``. Blank separator lines are tolerated and
    skipped; every other line must decode as strict UTF-8 JSON with no
    duplicate object keys at any nesting depth and no non-finite numbers
    (literal NaN/Infinity constants and non-finite exponent results such as
    ``1e10000``), so one deliberate finite interpretation exists per
    message. Decoder recursion failures on adversarially nested payloads
    are protocol drift, not crashes.
    """
    while True:
        remaining = deadline - clock()
        if remaining <= 0.0:
            return "timeout", None
        try:
            kind, chunk = reader.get(remaining)
        except queue.Empty:
            return "timeout", None
        if kind != "line":
            return kind, None
        data = cast(bytes, chunk)
        if not data.strip():
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "malformed", None
        try:
            return "message", json.loads(
                text,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
                parse_float=_finite_float,
            )
        except (ValueError, RecursionError):
            # Covers JSONDecodeError, duplicate keys, NaN/Infinity and
            # non-finite exponents (all ValueError), and adversarially
            # deep nesting (RecursionError). Narrow by design: nothing
            # else is swallowed.
            return "malformed", None


def _await_response(
    reader: BoundedLineReader,
    expected_id: int,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[str, Mapping[str, object] | None]:
    """Wait for the response carrying our request identity.

    Every decoded message is deliberately classified first: notifications
    and server-initiated requests are ignored, responses for other request
    ids (including stale initialize responses) are ignored, and structurally
    invalid messages are protocol drift. The matching response is identified
    by its integer ``id`` equal to ``expected_id`` — never by arrival order
    or timing. ``message["result"]`` is the only payload consumed by the
    caller; error bodies are never read.
    """
    while True:
        kind, value = _next_message(reader, deadline, clock)
        if kind != "message":
            return kind, None
        envelope: Mapping[str, object] | None = None
        if isinstance(value, Mapping):
            envelope = cast("Mapping[str, object]", value)
        if envelope is None:
            return "malformed", None
        message_kind = classify_app_server_message(envelope)
        if message_kind == "invalid":
            return "malformed", None
        if message_kind != "response":
            continue
        message_id = envelope.get("id")
        if _is_int(message_id) and message_id == expected_id:
            return "response", envelope


def _valid_initialize_result(value: object) -> bool:
    """Validate required InitializeResponse strings without retaining them."""
    if not isinstance(value, Mapping):
        return False
    result = cast(Mapping[str, object], value)
    return all(isinstance(result.get(field), str) for field in _INITIALIZE_RESPONSE_FIELDS)


def _valid_protocol_error(value: object) -> bool:
    """Validate JSON-RPC error shape without reading or returning its text."""
    if not isinstance(value, Mapping):
        return False
    error = cast(Mapping[str, object], value)
    return _is_protocol_int(error.get("code")) and isinstance(
        error.get("message"), str
    )


def _snapshot(
    status: str,
    diagnostic_code: str,
    retrieved_at: str,
) -> CapacitySnapshot:
    """Safe failure snapshot; carries only allowlisted safe identifiers."""
    return CapacitySnapshot(
        schema_version=1,
        provider=PROVIDER,
        source=SOURCE,
        retrieved_at=retrieved_at,
        status=status,
        windows=(),
        diagnostics=(CapacityDiagnostic(code=diagnostic_code),),
        plan=None,
    )


def _run_session(
    proc: "subprocess.Popen[bytes]",
    reader: BoundedLineReader,
    *,
    retrieved_at: str,
    startup_timeout: float,
    session_timeout: float,
) -> CapacitySnapshot:
    clock: Callable[[], float] = time.monotonic
    session_deadline = clock() + session_timeout
    startup_deadline = min(clock() + startup_timeout, session_deadline)

    if not _send_message(proc, _initialize_request()):
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    kind, message = _await_response(
        reader, _INITIALIZE_ID, startup_deadline, clock
    )
    if kind in ("timeout", "eof", "failed"):
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    if kind in ("malformed", "oversized"):
        return _snapshot("schema_changed", "schema_changed", retrieved_at)
    response = cast("Mapping[str, object]", message)
    if "error" in response:
        if not _valid_protocol_error(response["error"]):
            return _snapshot("schema_changed", "schema_changed", retrieved_at)
        # The mechanism answered with a protocol error whose free-text body
        # is never inspected or surfaced (no validated failure evidence).
        return _snapshot("unknown", "telemetry_unknown", retrieved_at)
    if not _valid_initialize_result(response.get("result")):
        return _snapshot("schema_changed", "schema_changed", retrieved_at)
    # The validated initialize fields are deliberately not retained: codexHome
    # can be a local path and v1 has no field for handshake metadata.

    if not _send_message(proc, _initialized_notification()):
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    if not _send_message(proc, _read_rate_limits_request()):
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    kind, message = _await_response(
        reader, _READ_RATE_LIMITS_ID, session_deadline, clock
    )
    if kind in ("timeout", "eof", "failed"):
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    if kind in ("malformed", "oversized"):
        return _snapshot("schema_changed", "schema_changed", retrieved_at)
    response = cast("Mapping[str, object]", message)
    if "error" in response:
        if not _valid_protocol_error(response["error"]):
            return _snapshot("schema_changed", "schema_changed", retrieved_at)
        return _snapshot("unknown", "telemetry_unknown", retrieved_at)
    return parse_codex_rate_limits_result(
        response.get("result"), retrieved_at=retrieved_at
    )


def _validated_timeout(value: object, name: str) -> float:
    """Reject non-finite or non-positive timeouts before any use.

    NaN, infinity and values beyond the wait primitive's bound must never
    reach deadline arithmetic or queue/process waits. They are caller
    configuration errors and raise ``ValueError`` immediately, before
    discovery, spawning or deadline computation, so no child process can be
    created with unusable bounds.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number of seconds")
    try:
        numeric = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be a finite positive number of seconds")
    if (
        not math.isfinite(numeric)
        or numeric <= 0
        or numeric > MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"{name} must be a finite positive number of seconds")
    return numeric


def collect_openai_codex_capacity(
    *,
    retrieved_at: str,
    discovery_roots: Sequence[Path] | None = None,
    startup_timeout: float | None = None,
    session_timeout: float | None = None,
) -> CapacitySnapshot:
    """Acquire one OpenAI subscription-capacity snapshot via Codex app-server.

    ``retrieved_at`` stays caller-supplied; this function introduces no
    freshness policy (U-003). ``discovery_roots`` defaults to the evidenced
    VS Code extension roots and exists for deterministic tests and
    controlled local configuration. ``startup_timeout``/``session_timeout``
    default to the module bounds and must be finite positive numbers (a
    ``ValueError`` is raised before any process is spawned otherwise). The
    discovered path and versions never enter the returned snapshot; every
    expected operational condition normalizes to a safe failure snapshot
    instead of leaking. An invalid ``retrieved_at`` keeps failing through
    the typed capacity-contract validation rather than being misreported as
    provider telemetry.
    """
    startup = (
        STARTUP_TIMEOUT_SECONDS
        if startup_timeout is None
        else _validated_timeout(startup_timeout, "startup_timeout")
    )
    session = (
        SESSION_TIMEOUT_SECONDS
        if session_timeout is None
        else _validated_timeout(session_timeout, "session_timeout")
    )
    installation, outcome = discover_codex_installation(discovery_roots)
    if outcome == "not_installed":
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    if installation is None:
        return _snapshot("unsupported", "unsupported_source", retrieved_at)

    argv: list[str] = [str(installation.binary), "app-server"]
    try:
        proc = spawn_app_server(argv, executable_fd=installation.binary_fd)
    except OSError:
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    finally:
        installation.close()

    reader = BoundedLineReader(
        cast("IO[bytes]", proc.stdout),
        max_line_bytes=MAX_LINE_BYTES,
        max_total_bytes=MAX_TOTAL_BYTES,
    )
    try:
        reader.start()
    except RuntimeError:
        # Reader startup failed (for example the thread could not be
        # started): an operational transport failure, not telemetry. The
        # explicit shutdown still attempts termination and reports an
        # unproven reap conservatively; the session never began.
        _ = _shutdown(proc, reader)
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    snapshot: CapacitySnapshot | None = None
    reaped = False
    try:
        snapshot = _run_session(
            proc,
            reader,
            retrieved_at=retrieved_at,
            startup_timeout=startup,
            session_timeout=session,
        )
    finally:
        reaped = _shutdown(proc, reader)
    if not reaped:
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    assert snapshot is not None
    return snapshot
