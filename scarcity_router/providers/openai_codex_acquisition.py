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
  budgets, strict UTF-8 and strict JSON decoding; budget violations surface
  as safe statuses, never as raw content in exceptions;
- requests are exactly three bounded writes (initialize, initialized
  notification, rate-limits read); there is no retry, no prompt and no other
  method call, so collection can never issue a model request;
- every failure path terminates the child: stdin is closed, the process is
  terminated (then killed if it refuses to exit) and the reader is joined;
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
import os
import platform
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, cast

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
_MAX_PACKAGE_BYTES = 64 * 1024

# Evidenced platform directory under the extension's ``bin/``. linux-x86_64
# is directly evidenced (PoC environment); the other entries are structural
# analogues of the same layout, validated per installation by the package
# file, and unlisted platforms are unsupported rather than guessed.
_PLATFORM_DIRS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "aarch64"): "linux-arm64",
    ("darwin", "x86_64"): "darwin-x86_64",
    ("darwin", "arm64"): "darwin-arm64",
}

DiscoveryOutcome = Literal["found", "not_installed", "unsupported_installation"]


@dataclass(frozen=True)
class CodexInstallation:
    """One validated, supported Codex app-server installation."""

    binary: Path
    extension_version: str
    codex_version: str


# ── Process/session bounds ────────────────────────────────────────────────────

STARTUP_TIMEOUT_SECONDS = 10.0
SESSION_TIMEOUT_SECONDS = 20.0
TERMINATE_TIMEOUT_SECONDS = 2.0
MAX_LINE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 1024 * 1024

_INITIALIZE_ID = 1
_READ_RATE_LIMITS_ID = 2
_RATE_LIMITS_METHOD = "account/rateLimits/read"
_INITIALIZED_NOTIFICATION_METHOD = "notifications/initialized"


class _AmbiguousPackageDocument(ValueError):
    """Raised when the package JSON contains duplicate object keys.

    A duplicate key makes the installation layout ambiguous, and discovery
    must fail closed for that candidate instead of trusting last-key-wins
    parsing. The message never contains any document value.
    """


def _is_int(value: object) -> bool:
    """True for a real JSON integer; booleans are not integers."""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow a decoded boundary value to a ``str``-keyed dict, or ``None``."""
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """JSON ``object_pairs_hook`` that refuses duplicate keys anywhere."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _AmbiguousPackageDocument()
        result[key] = value
    return result


def platform_directory() -> str | None:
    """The evidenced ``bin/`` subdirectory for the current platform, if any.

    Public because tests build host-matching installation trees from it and
    future ``doctor`` output reports it; it never touches the filesystem.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    return _PLATFORM_DIRS.get((system, machine))


def _extension_version_key(name: str) -> tuple[int, ...]:
    """Deterministic sort key from the extension directory's version token.

    ``openai.chatgpt-26.825.51511-linux-x64`` -> ``(26, 825, 51511)``.
    Non-numeric segments degrade to 0 so ordering stays total.
    """
    rest = name[len(_EXTENSION_PREFIX) :]
    first = rest.split("-", 1)[0]
    return tuple(
        int(segment) if segment.isdigit() else 0
        for segment in first.split(".")
    )


def _extension_version_string(name: str) -> str:
    rest = name[len(_EXTENSION_PREFIX) :]
    return rest.split("-", 1)[0]


def _candidate_directories(roots: Sequence[Path]) -> list[Path]:
    """All ``openai.chatgpt-*`` extension directories, newest first.

    Each root is listed once (no recursion); unreadable or absent roots are
    skipped. Ordering is fully deterministic: version descending, then the
    directory name as a tie-break.
    """
    candidates: list[Path] = []
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(_EXTENSION_PREFIX) and entry.is_dir():
                candidates.append(entry)
    candidates.sort(
        key=lambda path: (_extension_version_key(path.name), path.name),
        reverse=True,
    )
    return candidates


def _read_validated_package(
    binary_dir: Path,
) -> dict[str, object] | None:
    """Read and validate ``codex-package.json`` beside the binary.

    Bounded read, strict UTF-8, strict JSON with duplicate-key rejection,
    and the evidenced layout contract: integer ``layoutVersion == 1``,
    string ``variant == "codex"`` and a non-empty string ``version``. Any
    failure makes this candidate unusable (``None``).
    """
    try:
        with (binary_dir / _CODEX_PACKAGE_NAME).open("rb") as handle:
            raw = handle.read(_MAX_PACKAGE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_PACKAGE_BYTES:
        return None
    try:
        document = _as_object_dict(
            cast(
                "object",
                json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_object_without_duplicate_keys,
                ),
            )
        )
    except ValueError:
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
    candidates = _candidate_directories(search_roots)
    if not candidates:
        return None, "not_installed"
    platform_directory_ = platform_directory()
    if platform_directory_ is None:
        return None, "unsupported_installation"
    for candidate in candidates:
        binary_dir = candidate / "bin" / platform_directory_
        binary = binary_dir / "codex"
        if not (binary.is_file() and os.access(binary, os.X_OK)):
            continue
        package = _read_validated_package(binary_dir)
        if package is None:
            continue
        installation = CodexInstallation(
            binary=binary,
            extension_version=_extension_version_string(candidate.name),
            codex_version=cast("str", package["version"]),
        )
        return installation, "found"
    return None, "unsupported_installation"


# ── Bounded subprocess stdout reader ──────────────────────────────────────────


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
    _thread: threading.Thread

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
        self._queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="codex-app-server-stdout"
        )

    def start(self) -> None:
        self._thread.start()

    def get(self, timeout: float) -> tuple[str, bytes | None]:
        """Fetch the next reader event, blocking up to ``timeout`` seconds.

        Raises :class:`queue.Empty` when the timeout elapses first.
        """
        return self._queue.get(timeout=timeout)

    def join(self, timeout: float) -> None:
        self._thread.join(timeout=timeout)

    def _put(self, kind: str, chunk: bytes | None) -> None:
        self._queue.put((kind, chunk))

    def _loop(self) -> None:
        total = 0
        while True:
            try:
                chunk = self._stream.readline(self._max_line_bytes + 1)
            except OSError:
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


def spawn_app_server(argv: Sequence[str]) -> "subprocess.Popen[bytes]":
    """Launch the Codex app-server subprocess.

    Exactly one process, never a shell; stdout is captured for the JSONL
    session and stderr is discarded at the process level (never a pipe, so
    upstream tool output can never be read back or leak). Test seam: tests
    replace this function with fakes; no test executes a real binary.
    """
    return subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _shutdown(
    proc: "subprocess.Popen[bytes]",
    reader: BoundedLineReader,
) -> None:
    """Terminate and reap the child on every path; never raises.

    Order: close stdin (EOF), terminate (SIGTERM-equivalent), wait bounded,
    kill if the child refuses to exit, then join the reader briefly. The
    child's death closes the pipe, which releases the reader thread.
    """
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
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            _ = proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            pass
    except OSError:
        pass
    reader.join(timeout=1.0)


# ── JSONL session ─────────────────────────────────────────────────────────────


def _initialize_request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
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
        "jsonrpc": "2.0",
        "method": _INITIALIZED_NOTIFICATION_METHOD,
    }


def _read_rate_limits_request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": _READ_RATE_LIMITS_ID,
        "method": _RATE_LIMITS_METHOD,
        "params": {},
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
    skipped; every other line must decode as strict UTF-8 JSON.
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
            return "message", json.loads(text)
        except ValueError:
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
        # The mechanism answered with a protocol error whose free-text body
        # is never inspected or surfaced (no validated failure evidence).
        return _snapshot("unknown", "telemetry_unknown", retrieved_at)
    if not isinstance(response.get("result"), Mapping):
        return _snapshot("schema_changed", "schema_changed", retrieved_at)
    # The initialize result (userAgent, codexHome, platform facts) is
    # deliberately not inspected: it includes local paths, has no v1 field,
    # and the handshake only needs a successful result object.

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
        return _snapshot("unknown", "telemetry_unknown", retrieved_at)
    return parse_codex_rate_limits_result(
        response.get("result"), retrieved_at=retrieved_at
    )


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
    default to the module bounds. The discovered path and versions never
    enter the returned snapshot; every expected operational condition
    normalizes to a safe failure snapshot instead of leaking. An invalid
    ``retrieved_at`` keeps failing through the typed capacity-contract
    validation rather than being misreported as provider telemetry.
    """
    installation, outcome = discover_codex_installation(discovery_roots)
    if outcome == "not_installed":
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    if installation is None:
        return _snapshot("unsupported", "unsupported_source", retrieved_at)

    argv: list[str] = [str(installation.binary), "app-server"]
    try:
        proc = spawn_app_server(argv)
    except OSError:
        return _snapshot("unavailable", "source_unavailable", retrieved_at)

    reader = BoundedLineReader(
        cast("IO[bytes]", proc.stdout),
        max_line_bytes=MAX_LINE_BYTES,
        max_total_bytes=MAX_TOTAL_BYTES,
    )
    reader.start()
    try:
        return _run_session(
            proc,
            reader,
            retrieved_at=retrieved_at,
            startup_timeout=STARTUP_TIMEOUT_SECONDS
            if startup_timeout is None
            else startup_timeout,
            session_timeout=SESSION_TIMEOUT_SECONDS
            if session_timeout is None
            else session_timeout,
        )
    finally:
        _shutdown(proc, reader)
