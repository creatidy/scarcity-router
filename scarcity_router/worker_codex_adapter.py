"""Worker-local Codex execution adapter: the official App Server over stdio (M06).

The worker-side execution edge for subscription Codex capacity (issue #91
Stage 2). It implements the M05 :class:`~scarcity_router.worker_local_adapters.LocalAdapter`
protocol, so the server reaches it only through the existing one-protocol
bridge::

    M03 coordinator -> worker_bridged adapter (server) -> M05 worker
      -> THIS adapter (allowlist id ``"codex"``)
      -> official ``codex app-server`` subprocess over stdio JSONL

There is deliberately no second worker protocol, no server-to-local-Codex
path and no new execution channel: server-side the resource stays
``worker_bridged`` with a declared ``local_adapter_id`` (D-049).

Security boundary (docs/security.md, D-018/D-044, issue #91):

- **Credentials stay provider-managed.** This module never reads, copies,
  serializes, persists or logs any Codex token or ``auth.json``. Authentication
  state is verified through the official ``account/read`` method and reduced to
  a typed verdict; account emails and plan labels are never retained.
- **Strictly read-only against the provider.** This adapter performs NO
  provider-state mutation of any kind — no login, logout, refresh, redemption
  or config write, and no retry loop that could imply one. The program's
  single owner-approved mutation exception (the bounded managed-auth refresh
  after the evidenced rate-limits ``-32603`` shape) belongs to the OpenAI
  capacity collector alone, in its ``account/rateLimits/read`` phase
  (docs/decisions.md D-018, unamended); on any ``account/read`` protocol
  error this adapter fails closed to the typed ``auth_unverified`` verdict
  with the official interactive sign-in remediation.
- **Client content controls nothing.** Subprocess argv, environment, working
  directory, sandbox policy and approval policy are built entirely by this
  adapter. No request field can set an environment variable, a flag or a path.
- **Isolation profile (all official mechanisms, adapter-composed):** a
  dedicated controlled ``CODEX_HOME`` owned by the adapter under the worker's
  state directory (``0o700``, minimal generated ``config.toml``, never the
  user's ``~/.codex``); ephemeral threads; per-turn ``cwd`` pinned to a
  per-attempt scratch directory; ``workspaceWrite`` sandbox with
  ``writableRoots`` limited to that directory and network access disabled;
  ``approvalPolicy: "never"`` plus a defensive handler that answers any
  arriving approval server-request with the ``cancel`` decision (never
  ``accept``). ``dangerFullAccess`` is never sent. The bubblewrap sandbox
  prerequisite is probed on Linux; its absence, WSL1 and non-evidenced
  platforms make the resource ineligible (fail closed, with remediation
  documented in docs/providers.md).
- **Forbidden surfaces are never called:** ``thread/shellCommand``,
  ``process/*``, ``fs/*``, ``dynamicTools``/``item/tool/call``, the
  ``chatgptAuthTokens`` login mode and config-mutating methods. The
  ``initialize`` handshake omits ``experimentalApi`` (stable surface only).
  Tool-bearing requests are rejected BEFORE any execution.

Protocol shapes implemented here follow the version-pinned generated schemas
of the Stage-1 evidenced generation (``openai/codex`` tag ``rust-v0.155.1``,
schemas inspected 2026-09-20; see docs/codex-adapter-stage1-evidence.md).
Generation-aware parsing (D-019/U-011): every decoded message is structurally
classified, matched to request ids by identity (never timing), and anything
that does not fit the validated shape is a typed protocol failure — never a
wedged worker. Strict JSONL decode discipline (duplicate keys, NaN/Infinity,
non-finite exponents, deep nesting, per-line and cumulative byte budgets) is
shared provenance with ``providers/openai_codex_acquisition.py`` (the bounded
reader and the extension discovery are reused from there).

Execution honesty (D-043): exactly one Codex invocation per dispatched call —
no retry, no second process, no model or resource fallback. A process exit
before the turn started is a definitive failure; a process loss after the turn
may have started is reported with the ``unknown`` call-observation status so
the ambiguity survives to the audit trail. Provider-reported usage comes only
from ``thread/tokenUsage/updated``; absent usage stays absent.
"""

from __future__ import annotations

import json
import math
import os
import platform as _platform
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Protocol, cast

from .capacity import SCHEMA_VERSION, CapacityDiagnostic, CapacitySnapshot
from .gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterResult,
    AdapterStreamChunk,
    CallObservation,
    CHUNK_TEXT_DELTA,
    FINISH_STOP,
)
from .gateway_contracts import UsageTokens
from .providers.openai_codex import classify_app_server_message
from .providers.openai_codex_acquisition import (
    BoundedLineReader,
    discover_codex_installation,
)
from .resource_state import (
    ResourceIdentity,
    ResourceStateSnapshot,
    resource_snapshot_from_capacity,
)
from .worker_identity_store import ensure_private_tree

# ── Adapter identity and version contract ─────────────────────────────────────

CODEX_ADAPTER_ID = "codex"

#: The provider identity shared with the existing Codex collector
#: (``providers/openai_codex.py``); resource identity and snapshots must use
#: the same provider vocabulary.
CODEX_PROVIDER = "openai"

#: The evidenced supported protocol generation is the 0.154/0.155 series
#: (Stage 1: local probe of ``codex-cli 0.154.0-alpha.6.2`` on 2026-09-19;
#: stable release 0.155.1 published 2026-09-18). The turn/thread surfaces this
#: adapter needs (``thread/inject_items``, ``developerInstructions``,
#: ``ephemeral``, the detailed ``sandboxPolicy``, the bubblewrap-based Linux
#: sandbox) are evidenced in that series; anything older is unsupported —
#: never "compatible because the binary exists". Pre-release segments after
#: the numeric triple are tolerated.
MIN_SUPPORTED_CODEX_VERSION: tuple[int, int, int] = (0, 154, 0)

_VERSION_PREFIX = "codex-cli "
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

# ── Bounded-session budgets ───────────────────────────────────────────────────

VERSION_PROBE_TIMEOUT_SECONDS = 10.0
STARTUP_TIMEOUT_SECONDS = 10.0
TERMINATE_TIMEOUT_SECONDS = 2.0
INTERRUPT_ACK_SECONDS = 10.0
MAX_PROBE_OUTPUT_BYTES = 4 * 1024

MAX_LINE_BYTES = 256 * 1024
MAX_SESSION_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SESSION_EVENTS = 20_000
MAX_UNKNOWN_NOTIFICATIONS = 1_024
MAX_DELTA_CHARS = 262_144
MAX_MESSAGE_CHARS = 8_000_000
MAX_STDERR_BYTES = 8 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
MAX_SCHEMA_DEPTH = 32
MAX_MODEL_PAGES = 10

#: The documented ``codexErrorInfo`` discriminants (camelCase app-server
#: encoding, ``rust-v0.155.1``) mapped onto CLOSED safe notes. Free-text error
#: messages are never read: they can carry prompt content.
_CODEX_ERROR_INFO_NOTES: dict[str, str] = {
    "contextwindowexceeded": "the codex turn failed: context window exceeded",
    "sessionbudgetexceeded": "the codex turn failed: session budget exceeded",
    "usagelimitexceeded": "the codex turn failed: usage limit exceeded",
    "ratelimitexceeded": "the codex turn failed: rate limit exceeded",
    "serveroverloaded": "the codex turn failed: the backend is overloaded",
    "cyberpolicy": "the codex turn failed: blocked by backend policy",
    "misalignmentpolicyviolation": "the codex turn failed: blocked by backend policy",
    "unauthorized": "the codex turn failed: the backend reported an auth failure",
    "badrequest": "the codex turn failed: the backend rejected the request",
    "sandboxerror": "the codex turn failed: the sandbox refused an action",
    "internalservererror": "the codex turn failed: backend internal error",
    "httpconnectionfailed": "the codex turn failed: backend connection failed",
    "responsestreamconnectionfailed": "the codex turn failed: response stream failed",
    "responsestreamdisconnected": "the codex turn failed: response stream interrupted",
    "responsetoomanyfailedattempts": "the codex turn failed: backend retry limit reached",
    "threadrollbackfailed": "the codex turn failed",
    "activeturnnotsteerable": "the codex turn failed: the turn was not steerable",
    "other": "the codex turn failed",
}

# Protocol method names (documented stable surface, rust-v0.155.1).
_METHOD_INITIALIZE = "initialize"
_METHOD_INITIALIZED = "initialized"
_METHOD_ACCOUNT_READ = "account/read"
_METHOD_MODEL_LIST = "model/list"
_METHOD_THREAD_START = "thread/start"
_METHOD_THREAD_INJECT = "thread/inject_items"
_METHOD_TURN_START = "turn/start"
_METHOD_TURN_INTERRUPT = "turn/interrupt"
_NOTIFICATION_TURN_COMPLETED = "turn/completed"
_NOTIFICATION_AGENT_DELTA = "item/agentMessage/delta"
_NOTIFICATION_TOKEN_USAGE = "thread/tokenUsage/updated"
_SERVER_REQUEST_COMMAND_APPROVAL = "item/commandExecution/requestApproval"
_SERVER_REQUEST_FILE_APPROVAL = "item/fileChange/requestApproval"

_INITIALIZE_RESPONSE_FIELDS = ("userAgent", "codexHome", "platformFamily", "platformOs")

_TURN_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "interrupted", "failed"})


# ── Typed internal failures (worker-seam vocabulary) ──────────────────────────


class CodexIneligible(Exception):
    """The resource is not executable; raised BEFORE any turn starts.

    ``reason`` is a closed safe reason code; ``remediation`` is a short safe
    action hint (an official install/login step — never token copying).
    """

    def __init__(self, reason: str, remediation: str = "") -> None:
        super().__init__(reason)
        self.reason: str = reason
        self.remediation: str = remediation


class CodexProtocolFailure(Exception):
    """The runtime's protocol drifted from the validated shape (fail closed)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason: str = reason


class CodexProcessLost(Exception):
    """The runtime process was lost.

    ``after_start`` marks whether execution may already have begun — the
    honest ambiguity that must survive to the result (never retried).
    """

    def __init__(self, after_start: bool) -> None:
        super().__init__("codex process lost")
        self.after_start: bool = after_start


class _Cancelled(Exception):
    """Internal: the dispatch was cancelled/deadlined; nothing to report."""


# ── Process seam (the injectable transport) ───────────────────────────────────


@dataclass(frozen=True)
class CodexSpawnSpec:
    """One fully adapter-determined subprocess launch.

    Every field is composed by this adapter from its own configuration and
    state — never from request content. Tests replace the spawner and assert
    on the spec to pin the isolation posture (minimal env, controlled
    ``CODEX_HOME``, scratch ``cwd``, fixed argv).
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str


class CodexProcess(Protocol):
    """The bounded process handle the adapter needs (structural ``Popen``)."""

    stdin: IO[bytes] | None
    stdout: IO[bytes] | None
    stderr: IO[bytes] | None
    pid: int

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


CodexSpawner = Callable[[CodexSpawnSpec], CodexProcess]


def _minimal_environment(
    env: Mapping[str, str] | None = None,
    *,
    codex_home: str | None = None,
) -> dict[str, str]:
    """A minimal child environment — never the worker's full user env.

    Only ``PATH`` (so the runtime can reach its own tooling inside the
    sandbox) and ``HOME`` are inherited; on Windows the process-creation
    basics are added defensively (untested — this host cannot verify);
    ``CODEX_HOME`` is set to the controlled home when given. No request
    content can ever add a variable here.
    """
    parent = dict(os.environ) if env is None else dict(env)
    child: dict[str, str] = {}
    for key in ("PATH", "HOME"):
        value = parent.get(key)
        if value is not None:
            child[key] = value
    if sys.platform == "win32":
        for key in ("SystemRoot", "COMSPEC"):
            value = parent.get(key)
            if value is not None:
                child[key] = value
    if codex_home is not None:
        child["CODEX_HOME"] = codex_home
    return child


def default_codex_spawner(spec: CodexSpawnSpec) -> CodexProcess:
    """Launch exactly one process, never a shell, in its own process group.

    The new session/process group lets shutdown clean up the whole group
    (no orphans). stderr is captured for the bounded drainer (never forwarded
    verbatim); tests replace this function entirely.
    """
    proc = subprocess.Popen(  # noqa: S603 - fixed argv composed by the adapter
        list(spec.argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(spec.env),
        cwd=spec.cwd,
        start_new_session=True,
    )
    return cast(CodexProcess, proc)


# ── Discovery (deterministic, bounded, allowlisted) ───────────────────────────


@dataclass(frozen=True)
class CodexBinary:
    """One discovered binary candidate (safe facts only)."""

    path: Path
    source: str  # "pinned" | "path" | "extension"


def _regular_executable(path: Path, *, allow_symlink: bool) -> bool:
    """Regular, executable file check; the pinned path must not be a symlink."""
    try:
        st = os.stat(path) if allow_symlink else os.lstat(path)
    except OSError:
        return False
    if not allow_symlink and stat.S_ISLNK(st.st_mode):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return os.access(path, os.X_OK)


def discover_codex_binary(
    *,
    pinned_binary: Path | None = None,
    discovery_roots: Sequence[Path] | None = None,
    path_lookup: Callable[[str], str | None] = shutil.which,
) -> tuple[CodexBinary | None, str | None]:
    """Deterministic discovery: pinned path, then ``PATH``, then U-001 layout.

    Order and rules (issue #91 Stage 2):

    1. an explicit administrator pin (``--codex-bin``, mirroring the D-039
       ``SCARCITY_ROUTER_CODEX_BIN`` override): must be a regular, executable,
       NON-SYMLINK file;
    2. the official standalone CLI install channel: ``codex`` on ``PATH``
       (a regular executable file; PATH entries legitimately resolve through
       system symlinks);
    3. the U-001 VS Code ChatGPT extension layout, reusing the collector's
       read-only fd-based discovery (shared provenance, not a duplicate).

    Desktop-bundled Codex is NOT discovered (Stage 1: UNKNOWN reachability).
    Returns ``(binary, None)`` or ``(None, closed_reason)``.
    """
    if pinned_binary is not None:
        if _regular_executable(pinned_binary, allow_symlink=False):
            return CodexBinary(path=pinned_binary, source="pinned"), None
        return None, "pinned_binary_invalid"
    path_hit = path_lookup("codex")
    if path_hit:
        candidate = Path(path_hit)
        if _regular_executable(candidate, allow_symlink=True):
            return CodexBinary(path=candidate, source="path"), None
        return None, "path_binary_invalid"
    installation, outcome = discover_codex_installation(discovery_roots)
    if installation is not None:
        installation.close()
        return CodexBinary(path=installation.binary, source="extension"), None
    if outcome == "unsupported_installation":
        return None, "unsupported_installation"
    return None, "source_unavailable"


# ── Version probe ─────────────────────────────────────────────────────────────


def probe_codex_version(
    binary: Path,
    *,
    spawner: CodexSpawner,
    timeout: float = VERSION_PROBE_TIMEOUT_SECONDS,
) -> tuple[tuple[int, int, int] | None, str | None]:
    """Run ``<binary> --version`` (bounded) and parse the evidenced shape.

    Returns ``(version_tuple, None)`` on a supported generation or
    ``(None, closed_reason)`` with one of ``version_probe_failed``,
    ``version_unparseable`` or ``version_unsupported``.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("version probe timeout must be a positive number")
    spec = CodexSpawnSpec(
        argv=(str(binary), "--version"),
        env=_minimal_environment(),
        cwd=os.getcwd(),
    )
    try:
        proc = spawner(spec)
    except OSError:
        return None, "version_probe_failed"
    reason: str | None = None
    try:
        try:
            _ = proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            reason = "version_probe_failed"
    finally:
        # All writers are dead after the bounded termination, so the
        # post-termination read below always returns promptly.
        _ = terminate_codex_process(proc)
    stdout = proc.stdout
    try:
        if reason is not None:
            return None, reason
        if stdout is None:
            return None, "version_probe_failed"
        try:
            output = stdout.read(MAX_PROBE_OUTPUT_BYTES + 1) or b""
        except (OSError, ValueError, AttributeError):
            return None, "version_probe_failed"
        text = output[:MAX_PROBE_OUTPUT_BYTES].decode(
            encoding="utf-8", errors="replace"
        ).strip()
        if len(output) > MAX_PROBE_OUTPUT_BYTES or not text.startswith(
            _VERSION_PREFIX
        ):
            return None, "version_unparseable"
        match = _VERSION_RE.match(text[len(_VERSION_PREFIX) :])
        if match is None:
            return None, "version_unparseable"
        version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if version < MIN_SUPPORTED_CODEX_VERSION:
            return None, "version_unsupported"
        return version, None
    finally:
        # The process is reaped, so both pipes are at EOF: close them here
        # so the probe owns every descriptor it created (no GC-timed
        # ResourceWarnings, no leaked pipe fds under repeated probing).
        for stream in (stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


# ── Sandbox availability (honest platform gating) ─────────────────────────────


def check_sandbox_availability(
    *,
    platform_name: str = sys.platform,
    release: str | None = None,
    path_lookup: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """The bounded sandbox-availability verdict; ``None`` means available.

    Linux (and WSL2, which runs the Linux bubblewrap sandbox) requires the
    documented bubblewrap prerequisite on ``PATH``. WSL1 is unsupported since
    codex 0.115 (Stage 1 evidence). Windows-native sandbox paths and other
    platforms are not evidenced on this code base's verification host and are
    reported honestly ineligible (``platform_not_evidenced``), never guessed.
    """
    if platform_name != "linux":
        return "platform_not_evidenced"
    if release is None:
        try:
            release = _platform.release()
        except OSError:  # pragma: no cover - defensive
            release = ""
    lowered = release.lower()
    if "microsoft" in lowered and "wsl2" not in lowered:
        # WSL1 kernel strings say "Microsoft" without "WSL2".
        return "wsl1_unsupported"
    if path_lookup("bwrap") is None:
        return "sandbox_prerequisite_missing"
    return None


# ── Controlled CODEX_HOME ─────────────────────────────────────────────────────

CONTROLLED_CONFIG_NAME = "config.toml"

_CONTROLLED_CONFIG_NOTICE = (
    "# Generated by the Scarcity Router Codex execution adapter. Do not edit.\n"
    "# Controlled isolation home: no MCP servers, no plugins, no apps or\n"
    "# connectors, no trust defaults. Scarcity Router never reads credentials\n"
    "# from this home; sign in with the official CLI against CODEX_HOME.\n"
)

_HOME_MODE_OK_MASK = 0o077  # group/other bits must be absent


class ControlledCodexHome:
    """The adapter-owned, permissioned ``CODEX_HOME`` (never the user's).

    Created lazily under the worker's state area, ``0o700``, with a minimal
    generated ``config.toml``. The path is derived from the adapter's state
    directory alone — arbitrary ``CODEX_HOME`` values from outside are not
    accepted. Validity (mode and exact config content) is re-checked before
    every session so silent tampering fails closed.
    """

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        root = Path(state_dir) / "codex"
        self._home: Path = root / "codex-home"
        self._scratch_root: Path = root / "scratch"
        self._lock: threading.Lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._home

    def ensure(self) -> None:
        """Create the controlled tree; raises ``OSError`` when impossible."""
        with self._lock:
            ensure_private_tree(str(self._home))
            ensure_private_tree(str(self._scratch_root))
            config = self._home / CONTROLLED_CONFIG_NAME
            if not config.exists():
                _write_private(config, _CONTROLLED_CONFIG_NOTICE)
            self._prune_stale_scratch()

    def validate(self) -> str | None:
        """``None`` when the controlled home is intact, else a safe reason."""
        with self._lock:
            try:
                st = os.stat(self._home)
                if not stat.S_ISDIR(st.st_mode):
                    return "codex_home_invalid"
                if st.st_mode & _HOME_MODE_OK_MASK:
                    return "codex_home_invalid"
                config = self._home / CONTROLLED_CONFIG_NAME
                if (
                    not config.is_file()
                    or config.read_bytes() != _CONTROLLED_CONFIG_NOTICE.encode("utf-8")
                ):
                    return "codex_home_invalid"
            except OSError:
                return "codex_home_invalid"
        return None

    def new_scratch_dir(self) -> Path:
        """One per-attempt scratch working directory (created ``0o700``)."""
        with self._lock:
            ensure_private_tree(str(self._scratch_root))
            scratch = self._scratch_root / f"turn-{uuid.uuid4().hex}"
            scratch.mkdir(mode=0o700)
            os.chmod(scratch, 0o700)
            return scratch

    def _prune_stale_scratch(self, *, max_age_seconds: float = 24 * 3600.0) -> None:
        """Best-effort bounded hygiene for scratch dirs left by crashed calls."""
        try:
            now = time.time()
            for entry in self._scratch_root.iterdir():
                try:
                    if now - entry.stat().st_mtime > max_age_seconds:
                        shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            return


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _ = os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


# ── Strict JSON decode (shared provenance with the collector) ─────────────────


class _AmbiguousJson(ValueError):
    """Non-standard JSON constants or duplicate keys: drift, fail closed."""


def _reject_json_constant(_name: str) -> object:
    raise _AmbiguousJson()


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _AmbiguousJson()
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _AmbiguousJson()
        result[key] = value
    return result


def _decode_strict(line: bytes) -> object:
    """Strict UTF-8 JSON with exactly one finite interpretation per message."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise CodexProtocolFailure("protocol_malformed") from None
    try:
        return cast(
            "object",
            json.loads(
                text,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
                parse_float=_finite_float,
            ),
        )
    except (ValueError, RecursionError):
        # JSONDecodeError, duplicate keys, NaN/Infinity, non-finite exponents
        # and adversarially deep nesting are all drift, never crashes.
        raise CodexProtocolFailure("protocol_malformed") from None


def _as_object(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_protocol_error(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    error = cast("Mapping[str, object]", value)
    return _is_int(error.get("code")) and isinstance(error.get("message"), str)


def _protocol_error_code(envelope: Mapping[str, object]) -> int | None:
    error = envelope.get("error")
    if not _valid_protocol_error(error):
        return None
    code = cast("Mapping[str, object]", error).get("code")
    return cast(int, code) if _is_int(code) else None


class _ProtocolError(Exception):
    """A validated JSON-RPC error response (only its numeric code is kept)."""

    def __init__(self, code: int | None) -> None:
        super().__init__(f"protocol error {code}")
        self.code: int | None = code


# ── The stdio JSONL session ───────────────────────────────────────────────────

_KNOWN_NOTIFICATIONS: frozenset[str] = frozenset(
    {
        _METHOD_INITIALIZED,
        _NOTIFICATION_TURN_COMPLETED,
        _NOTIFICATION_AGENT_DELTA,
        _NOTIFICATION_TOKEN_USAGE,
        "turn/started",
        "item/started",
        "item/completed",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/textDelta",
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
        "configWarning",
        "error",
        "warning",
    }
)


class CodexSession:
    """One bounded app-server session over the spawned process's stdio.

    Strict framing: request/response matching by identity (never timing or
    line order), structural classification of every message, bounded
    line/byte/event budgets, bounded stderr capture (never forwarded
    verbatim), and defensive server-request handling (approvals are answered
    with ``cancel``, never ``accept``).
    """

    def __init__(
        self,
        proc: CodexProcess,
        *,
        max_line_bytes: int = MAX_LINE_BYTES,
        max_total_bytes: int = MAX_SESSION_TOTAL_BYTES,
        max_events: int = MAX_SESSION_EVENTS,
        stderr_cap_bytes: int = MAX_STDERR_BYTES,
    ) -> None:
        self._proc: CodexProcess = proc
        self._stdin: IO[bytes] | None = proc.stdin
        self._reader: BoundedLineReader = BoundedLineReader(
            cast("IO[bytes]", proc.stdout),
            max_line_bytes=max_line_bytes,
            max_total_bytes=max_total_bytes,
        )
        self._stderr_cap: int = stderr_cap_bytes
        self._stderr_bytes_seen: int = 0
        self._next_id: int = 1
        self._events_seen: int = 0
        self._unknown_notifications: int = 0
        self._closed: bool = False
        self._drainer: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._reader.start()
        stderr = self._proc.stderr
        if stderr is not None:
            thread = threading.Thread(
                target=self._drain_stderr,
                args=(stderr,),
                daemon=True,
                name="codex-app-server-stderr",
            )
            self._drainer = thread
            thread.start()

    def _drain_stderr(self, stream: IO[bytes]) -> None:
        """Capture stderr with a hard byte cap; never expose its content."""
        try:
            while self._stderr_bytes_seen <= self._stderr_cap:
                chunk = stream.read(4096)
                if not chunk:
                    return
                self._stderr_bytes_seen += len(chunk)
        except (OSError, ValueError):
            return

    # -- writing -----------------------------------------------------------

    def send(self, payload: Mapping[str, object]) -> None:
        """One bounded write; a dead process raises :class:`CodexProcessLost`."""
        if self._closed:
            raise CodexProcessLost(after_start=False)
        stdin = self._stdin
        if stdin is None:
            raise CodexProcessLost(after_start=False)
        try:
            _ = stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            stdin.flush()
        except (OSError, ValueError):
            raise CodexProcessLost(after_start=False) from None

    # -- reading -----------------------------------------------------------

    def await_response(
        self,
        expected_id: int,
        deadline: float,
        *,
        abort: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Wait for the response carrying ``expected_id`` (never by timing).

        Notifications and server-initiated requests are dispatched along the
        way; anything structurally invalid is protocol drift. Raises
        :class:`CodexProcessLost` on EOF/reader failure,
        :class:`CodexProtocolFailure` on drift or budget exhaustion and
        :class:`_Cancelled` when ``abort`` fires.
        """
        while True:
            kind, value = self.next_event(deadline, abort=abort)
            if kind == "response":
                envelope = cast("dict[str, object]", value)
                message_id = envelope.get("id")
                if _is_int(message_id) and cast(int, message_id) == expected_id:
                    return envelope
                continue
            if kind == "notification":
                continue
            raise CodexProtocolFailure("protocol_timeout")

    def next_event(
        self, deadline: float, *, abort: Callable[[], bool] | None = None
    ) -> tuple[str, object]:
        """The next classified event, bounded by the absolute ``deadline``.

        Returns ``("response", envelope)`` or ``("notification", envelope)``,
        ``("timeout", None)`` when ``deadline`` passed, or raises the typed
        failures. Server requests are answered inline (approvals cancelled).
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return "timeout", None
            if abort is not None and abort():
                raise _Cancelled()
            try:
                kind, chunk = self._reader.get(min(0.1, remaining))
            except queue.Empty:
                continue
            if kind == "line":
                data = cast(bytes, chunk)
                if not data.strip():
                    continue
                self._events_seen += 1
                if self._events_seen > MAX_SESSION_EVENTS:
                    raise CodexProtocolFailure("event_budget_exceeded")
                envelope = self._classify_line(data)
                message_kind = envelope[0]
                if message_kind == "response":
                    return "response", envelope[1]
                if message_kind == "notification":
                    return "notification", envelope[1]
                continue  # server requests are answered inline
            if kind == "oversized":
                raise CodexProtocolFailure("protocol_budget_exceeded")
            # "eof" / "failed" from the bounded reader: the process is gone
            # or its pipe failed — the honest loss of the runtime.
            raise CodexProcessLost(after_start=False)

    def _classify_line(self, data: bytes) -> tuple[str, object]:
        decoded = _decode_strict(data)
        envelope = _as_object(decoded)
        if envelope is None:
            raise CodexProtocolFailure("protocol_malformed")
        message_kind = classify_app_server_message(envelope)
        if message_kind == "invalid":
            raise CodexProtocolFailure("protocol_malformed")
        if message_kind == "response":
            if "error" in envelope and not _valid_protocol_error(envelope["error"]):
                raise CodexProtocolFailure("protocol_malformed")
            return "response", envelope
        if message_kind == "request":
            self._answer_server_request(envelope)
            return "server-request", envelope
        method = envelope.get("method")
        self._observe_notification(cast("str", method))
        return "notification", envelope

    def _observe_notification(self, method: str) -> None:
        """Unknown notification methods are counted and bounded (policy)."""
        if method not in _KNOWN_NOTIFICATIONS:
            self._unknown_notifications += 1
            if self._unknown_notifications > MAX_UNKNOWN_NOTIFICATIONS:
                raise CodexProtocolFailure("notification_budget_exceeded")

    def _answer_server_request(self, envelope: dict[str, object]) -> None:
        """Approvals are CANCELLED (deny + stop the turn); never accepted."""
        request_id = envelope.get("id")
        method = envelope.get("method")
        if method in (_SERVER_REQUEST_COMMAND_APPROVAL, _SERVER_REQUEST_FILE_APPROVAL):
            self.send({"id": request_id, "result": {"decision": "cancel"}})
            return
        # Any other server-initiated request is refused with the standard
        # JSON-RPC "method not found" code; a fixed safe message only.
        self.send(
            {
                "id": request_id,
                "error": {"code": -32601, "message": "not supported by this client"},
            }
        )

    # -- request helpers ---------------------------------------------------

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None,
        deadline: float,
        *,
        abort: Callable[[], bool] | None = None,
    ) -> object:
        """One request -> the validated result payload, or a typed failure."""
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, object] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self.send(payload)
        envelope = self.await_response(request_id, deadline, abort=abort)
        if "error" in envelope:
            raise _ProtocolError(_protocol_error_code(envelope))
        return envelope.get("result")

    def notify(self, method: str) -> None:
        self.send({"method": method})

    # -- shutdown ----------------------------------------------------------

    def close(self) -> bool:
        """Graceful stdin-close shutdown, then bounded terminate/kill/reap.

        Shutdown owns every activity it started: the reader thread is
        stopped and joined, the stderr drainer is joined, and both pipe
        handles are closed explicitly once the child is reaped (both are
        at EOF then) — no descriptor or thread outlives the session.
        """
        if self._closed:
            return True
        self._closed = True
        reaped = terminate_codex_process(self._proc)
        self._reader.close()
        self._reader.join(timeout=1.0)
        if self._drainer is not None:
            self._drainer.join(timeout=1.0)
            self._drainer = None
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        return reaped

    @property
    def stderr_bytes_seen(self) -> int:
        return self._stderr_bytes_seen


def _kill_process_group(proc: CodexProcess) -> None:
    """Best-effort whole-group cleanup, guarded to the child's own group.

    The default spawner starts the child in a new session, so the child is
    its own process-group leader; only then is the group signalled. A child
    spawned by some other seam (no new session) must never cause a signal to
    a foreign group.
    """
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg is None or getpgid is None:  # pragma: no cover - non-POSIX
        return
    try:
        if getpgid(proc.pid) != proc.pid:
            return
        killpg(proc.pid, 9)
    except OSError:
        return


def terminate_codex_process(proc: CodexProcess) -> bool:
    """Close stdin, terminate, bounded wait, kill the group; prove the reap.

    Returns whether a bounded wait proved that the child was reaped; a wait
    failure is never treated as proof of reaping.
    """
    stdin = proc.stdin
    if stdin is not None:
        try:
            stdin.close()
        except (OSError, ValueError):
            pass
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        _ = proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    _kill_process_group(proc)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        _ = proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        return True
    except (subprocess.TimeoutExpired, OSError):
        return False


# ── Request mapping (client vocabulary -> thread/turn surface) ────────────────


@dataclass(frozen=True)
class MappedConversation:
    """The validated mapping of one ``AdapterCall`` onto the thread surface.

    ``base_instructions``/``developer_instructions`` carry the joined
    ``system``/``developer`` text; ``history_items`` are the Responses API
    items injected before the turn; ``turn_input`` is the final user message.
    A conversation that does not end with a user message is rejected before
    execution (continuing it would have to be invented or double-delivered).
    """

    base_instructions: str | None
    developer_instructions: str | None
    history_items: tuple[dict[str, object], ...]
    turn_input: str


def map_conversation(messages: Sequence[AdapterMessage]) -> MappedConversation:
    """Validate and map the conversation; typed rejection before execution.

    Roles map as: ``system`` -> ``baseInstructions`` (joined when repeated),
    ``developer`` -> ``developerInstructions`` (joined), prior
    ``user``/``assistant`` messages -> ``thread/inject_items`` Responses API
    items (structure preserved, never collapsed into one blob), and the final
    user message -> ``turn/start`` input. ``tool`` role messages and any
    ``tool_calls`` are UNSUPPORTED on the stable surface (client tools return
    to clients per D-043; Codex-internal tools are not client tool calls) and
    are rejected here, before anything executes.
    """
    base_parts: list[str] = []
    developer_parts: list[str] = []
    history: list[dict[str, object]] = []
    for message in messages:
        if message.role == "tool" or message.tool_calls or message.tool_call_id:
            raise CodexIneligible("tool_messages_unsupported")
        if message.content is None:
            raise CodexIneligible("empty_message_unsupported")
        if message.role == "system":
            base_parts.append(message.content)
            continue
        if message.role == "developer":
            developer_parts.append(message.content)
            continue
        if message.role == "user":
            history.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": message.content}],
                }
            )
            continue
        if message.role == "assistant":
            history.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": message.content}],
                }
            )
            continue
        raise CodexIneligible("message_role_unsupported")
    if not history:
        raise CodexIneligible("no_user_input")
    last = history[-1]
    if last.get("role") != "user":
        raise CodexIneligible("conversation_must_end_with_user_message")
    last_content = cast("list[dict[str, object]]", last["content"])
    turn_input = cast("str", last_content[0]["text"])
    return MappedConversation(
        base_instructions="\n\n".join(base_parts) if base_parts else None,
        developer_instructions=(
            "\n\n".join(developer_parts) if developer_parts else None
        ),
        history_items=tuple(history[:-1]),
        turn_input=turn_input,
    )


def validate_structured_schema(
    response_format: Mapping[str, object] | None,
) -> object | None:
    """Map ``response_format`` to ``turn/start {outputSchema}`` or reject.

    ``json_schema`` requires a JSON-object schema within sane size/depth
    bounds; ``json_object`` (schema-less) is explicitly unsupported here —
    never silently discarded; ``text``/absent maps to no schema.
    """
    if response_format is None:
        return None
    kind = response_format.get("type")
    if kind == "text":
        return None
    if kind == "json_object":
        raise CodexIneligible("response_format_json_object_unsupported")
    if kind != "json_schema":
        raise CodexIneligible("response_format_unsupported")
    schema_wrapper = _as_object(response_format.get("json_schema"))
    if schema_wrapper is None:
        raise CodexIneligible("response_format_invalid")
    schema = schema_wrapper.get("schema")
    if _as_object(schema) is None:
        raise CodexIneligible("response_format_invalid")
    try:
        serialized = json.dumps(schema)
    except (TypeError, ValueError):
        raise CodexIneligible("response_format_invalid") from None
    if len(serialized) > MAX_SCHEMA_BYTES:
        raise CodexIneligible("response_format_too_large")
    if _json_depth(schema) > MAX_SCHEMA_DEPTH:
        raise CodexIneligible("response_format_too_deep")
    return schema


def _json_depth(value: object, depth: int = 0) -> int:
    if depth > MAX_SCHEMA_DEPTH:
        return depth
    if isinstance(value, dict):
        mapping = cast("dict[str, object]", value)
        return max(
            (_json_depth(item, depth + 1) for item in mapping.values()),
            default=depth,
        )
    if isinstance(value, list):
        items = cast("list[object]", value)
        return max((_json_depth(item, depth + 1) for item in items), default=depth)
    return depth


# ── Model/effort verification (exact binding, never substitution) ─────────────


def parse_model_listing(result: object) -> dict[str, tuple[str, ...]]:
    """Validate one ``model/list`` result page into ``{slug: efforts}``.

    Accepts the evidenced members (``model`` primary slug, ``id`` fallback)
    and reads ``supportedReasoningEfforts`` as the evidenced option list;
    structural drift in any entry fails closed for the whole listing.
    """
    envelope = _as_object(result)
    if envelope is None:
        raise CodexProtocolFailure("model_list_malformed")
    data = envelope.get("data")
    if not isinstance(data, list):
        raise CodexProtocolFailure("model_list_malformed")
    models: dict[str, tuple[str, ...]] = {}
    for entry in cast("list[object]", data):
        model = _as_object(entry)
        if model is None:
            raise CodexProtocolFailure("model_list_malformed")
        slug = model.get("model")
        if not isinstance(slug, str) or not slug:
            slug = model.get("id")
            if not isinstance(slug, str) or not slug:
                raise CodexProtocolFailure("model_list_malformed")
        efforts: list[str] = []
        efforts_raw = model.get("supportedReasoningEfforts")
        if efforts_raw is not None:
            if not isinstance(efforts_raw, list):
                raise CodexProtocolFailure("model_list_malformed")
            for option in cast("list[object]", efforts_raw):
                if isinstance(option, str):
                    efforts.append(option)
                    continue
                option_map = _as_object(option)
                if option_map is None or not isinstance(
                    option_map.get("reasoningEffort"), str
                ):
                    raise CodexProtocolFailure("model_list_malformed")
                efforts.append(cast("str", option_map["reasoningEffort"]))
        models[slug] = tuple(efforts)
    return models


def next_model_cursor(result: object) -> str | None:
    """The pagination cursor of a validated listing page, when present."""
    envelope = _as_object(result)
    if envelope is None:
        return None
    cursor = envelope.get("nextCursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def verify_model_and_effort(
    models: Mapping[str, tuple[str, ...]], slug: str, effort: str | None
) -> str | None:
    """The exact-binding check; ``None`` means the pair is representable."""
    efforts = models.get(slug)
    if efforts is None:
        return "model_not_listed"
    if effort is not None and effort not in efforts:
        return "effort_not_supported"
    return None


def parse_token_usage(value: object) -> UsageTokens | None:
    """Map the evidenced ``ThreadTokenUsage`` breakdown, or ``None``.

    ``inputTokens`` -> ``prompt_tokens``; ``outputTokens`` ->
    ``completion_tokens``; cached/reasoning components have no normalized
    representation and are never merged into the reported numbers. Malformed
    usage is ignored (usage stays unavailable — never invented).
    """
    usage = _as_object(value)
    if usage is None:
        return None
    breakdown = usage.get("last")
    if breakdown is None:
        breakdown = usage.get("total")
    breakdown_map = _as_object(breakdown) if breakdown is not None else None
    if breakdown_map is None:
        return None
    input_tokens = breakdown_map.get("inputTokens")
    output_tokens = breakdown_map.get("outputTokens")
    if not _is_int(input_tokens) or not _is_int(output_tokens):
        return None
    if cast(int, input_tokens) < 0 or cast(int, output_tokens) < 0:
        return None
    return UsageTokens(
        prompt_tokens=cast(int, input_tokens), completion_tokens=cast(int, output_tokens)
    )


def safe_error_note(turn_error: object) -> str:
    """A SAFE note from ``turn.error`` via the documented ``codexErrorInfo``
    vocabulary only. The free-text ``message`` (which can carry prompt
    content) is never read."""
    error = _as_object(turn_error)
    if error is None:
        return "the codex turn failed"
    info = error.get("codexErrorInfo")
    discriminant: str | None = None
    if isinstance(info, str):
        discriminant = info
    else:
        info_map = _as_object(info)
        if info_map is not None and info_map:
            discriminant = next(iter(info_map))
    if discriminant is not None:
        note = _CODEX_ERROR_INFO_NOTES.get(discriminant.lower())
        if note is not None:
            return note
    return "the codex turn failed"


# ── Auth verdict (D-018 boundary absolute) ────────────────────────────────────


@dataclass(frozen=True)
class AuthVerdict:
    """The typed auth-state verdict; no credential or identifier survives."""

    ok: bool
    reason: str | None
    remediation: str


def verify_account_auth(session: CodexSession, deadline: float) -> AuthVerdict:
    """Verify subscription auth via the official ``account/read`` method.

    This is a READ-ONLY verification: the execution adapter performs NO
    provider-state mutation of any kind. The program's single owner-approved
    mutation exception (the bounded managed-auth refresh) belongs to the
    OpenAI capacity collector alone, in its ``account/rateLimits/read``
    phase (docs/decisions.md D-018, unamended) — it is deliberately NOT
    extended to this adapter.

    Requires ``account.type == "chatgpt"``. API-key auth is NOT eligible for
    execution (PAYG conversion is forbidden by owner policy, D-039/M4.1):
    the remediation is the official ``codex login``, never token copying.
    Missing/absent auth is ``auth_missing`` with the same remediation. A
    protocol error — including the collector's evidenced ``-32603``
    internal-error shape, whose free text this adapter never reads — cannot
    establish an auth state and fails closed to ``auth_unverified`` with the
    official sign-in remediation. Email/plan content is never retained.
    """
    try:
        result = session.request(_METHOD_ACCOUNT_READ, None, deadline)
    except _ProtocolError:
        # Fail closed, read-only: no refresh, no retry, no mutation. The
        # error's free text is never inspected (it may carry sensitive
        # content and cannot prove an auth condition anyway).
        return AuthVerdict(
            False,
            "auth_unverified",
            "could not verify sign-in; run the official codex login "
            + "(browser or device code) against the controlled home",
        )
    return _auth_verdict_from(result)


def _auth_verdict_from(result: object) -> AuthVerdict:
    unverifiable = AuthVerdict(
        False,
        "auth_unverified",
        "could not verify sign-in; run the official codex login "
        + "(browser or device code) against the controlled home",
    )
    envelope = _as_object(result)
    if envelope is None or not isinstance(envelope.get("requiresOpenaiAuth"), bool):
        return unverifiable
    account = envelope.get("account")
    account_map = _as_object(account) if account is not None else None
    if account_map is None:
        return AuthVerdict(
            False,
            "auth_missing",
            "sign in with the official CLI: CODEX_HOME=<controlled home> codex login",
        )
    account_type = account_map.get("type")
    if account_type == "chatgpt":
        return AuthVerdict(True, None, "")
    if account_type == "apiKey":
        return AuthVerdict(
            False,
            "auth_payg_unsupported",
            "API-key auth is not an execution resource; run the official "
            + "codex login for the ChatGPT subscription path",
        )
    return unverifiable


# ── The adapter ───────────────────────────────────────────────────────────────


#: Closed eligibility-reason families mapped onto the capacity v3 status
#: vocabulary (the detailed reason code itself is not part of that frozen
#: contract, so the closest honest status-level code is carried; remediation
#: guidance lives in docs/providers.md).
_SCHEMA_CHANGED_REASONS: frozenset[str] = frozenset(
    {
        "protocol_malformed",
        "protocol_budget_exceeded",
        "event_budget_exceeded",
        "notification_budget_exceeded",
        "message_budget_exceeded",
        "initialize_schema_changed",
        "thread_start_schema_changed",
        "thread_inject_schema_changed",
        "turn_start_schema_changed",
        "turn_status_unrecognized",
        "model_list_malformed",
        "controlled_home_not_adopted",
    }
)
_UNSUPPORTED_REASONS: frozenset[str] = frozenset(
    {
        "pinned_binary_invalid",
        "path_binary_invalid",
        "unsupported_installation",
        "version_unsupported",
        "version_unparseable",
        "version_probe_failed",
        "sandbox_prerequisite_missing",
        "wsl1_unsupported",
        "platform_not_evidenced",
        "codex_home_invalid",
    }
)


def _snapshot_class(reason: str) -> tuple[str, str]:
    """Map one eligibility reason onto (status, required diagnostic code)."""
    if reason in ("auth_missing", "auth_payg_unsupported"):
        # A STRUCTURED account/read verdict (account.type), not an inference
        # from a conflated error code: honestly reachable here.
        return "auth_required", "auth_required"
    if reason in ("auth_unverified", "initialize_refused"):
        # The auth state (or the runtime's state generally) could not be
        # established — insufficient evidence, never guessed.
        return "unknown", "telemetry_unknown"
    if reason in _SCHEMA_CHANGED_REASONS:
        return "schema_changed", "schema_changed"
    if reason in _UNSUPPORTED_REASONS:
        return "unsupported", "unsupported_source"
    # Everything else is an availability failure (no binary, lost process,
    # unresponsive runtime).
    return "unavailable", "source_unavailable"


def _canonical_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class CodexLocalAdapter:
    """The worker-local adapter for the official Codex App Server (``codex``).

    Construction performs no I/O; eligibility is established lazily and
    honestly per call and per snapshot, so a broken Codex installation never
    prevents the worker from serving its other adapters (issue #91: only the
    affected adapter/mode is marked ineligible — the rest of the router keeps
    working).
    """

    adapter_id: str = CODEX_ADAPTER_ID

    def __init__(
        self,
        *,
        resource: ResourceIdentity,
        state_dir: str | os.PathLike[str],
        pinned_binary: Path | None = None,
        discovery_roots: Sequence[Path] | None = None,
        path_lookup: Callable[[str], str | None] = shutil.which,
        spawner: CodexSpawner = default_codex_spawner,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
        version_probe_timeout: float = VERSION_PROBE_TIMEOUT_SECONDS,
        interrupt_ack_seconds: float = INTERRUPT_ACK_SECONDS,
        platform_name: str = sys.platform,
        platform_release: str | None = None,
    ) -> None:
        if resource.provider != CODEX_PROVIDER:
            raise ValueError(
                "codex_adapter: the resource provider must be "
                + f"{CODEX_PROVIDER!r}, got {resource.provider!r}"
            )
        self.resource_ids: tuple[str, ...] = (resource.resource_id,)
        self._resource: ResourceIdentity = resource
        self._home: ControlledCodexHome = ControlledCodexHome(state_dir)
        self._pinned_binary: Path | None = pinned_binary
        self._discovery_roots: Sequence[Path] | None = discovery_roots
        self._path_lookup: Callable[[str], str | None] = path_lookup
        self._spawner: CodexSpawner = spawner
        self._startup_timeout: float = startup_timeout
        self._version_probe_timeout: float = version_probe_timeout
        self._interrupt_ack_seconds: float = interrupt_ack_seconds
        self._platform_name: str = platform_name
        self._platform_release: str | None = platform_release

    @property
    def resource(self) -> ResourceIdentity:
        """The resource identity this adapter serves (its physical model)."""
        return self._resource

    # ── The LocalAdapter entry point ──────────────────────────────────

    def invoke(
        self,
        call: AdapterCall,
        *,
        cancel_event: threading.Event,
        deadline: str,
        emit: Callable[[AdapterStreamChunk], None],
    ) -> AdapterResult:
        started = _canonical_now()
        try:
            return self._invoke(
                call,
                cancel_event=cancel_event,
                deadline=deadline,
                emit=emit,
                started=started,
            )
        except _Cancelled:
            return self._cancelled_result(started)
        except CodexIneligible as exc:
            return self._failed_result(started, exc.reason, exc.remediation)
        except CodexProtocolFailure as exc:
            return self._failed_result(started, exc.reason, "")
        except CodexProcessLost as exc:
            if exc.after_start:
                return self._ambiguous_result(started)
            return self._failed_result(started, "codex_process_lost", "")
        except (OSError, RuntimeError):
            # Spawner or reader-thread startup failures are definitive for
            # this attempt (exactly one invocation — never retried here).
            return self._failed_result(started, "codex_process_lost", "")

    def _invoke(
        self,
        call: AdapterCall,
        *,
        cancel_event: threading.Event,
        deadline: str,
        emit: Callable[[AdapterStreamChunk], None],
        started: str,
    ) -> AdapterResult:
        if cancel_event.is_set():
            return self._cancelled_result(started)
        if call.resource.resource_id not in self.resource_ids:
            # The adapter serves only its configured resource id (M05/M09
            # ownership stays authoritative in the server composition).
            raise CodexIneligible("resource_not_served")
        if (
            call.model.provider != self._resource.provider
            or call.model.model != self._resource.model
        ):
            # D-042: this adapter's resource represents ONE physical model
            # (its ResourceIdentity). A selected identity outside it is
            # never mapped or substituted — typed rejection BEFORE any
            # turn (and before any thread): the runtime's model/list
            # verification below stays the exact-binding check for the
            # effort within that model, never a model selector.
            raise CodexIneligible("model_not_served")
        remaining = self._deadline_remaining(deadline)
        if remaining is None or remaining <= 0.0:
            return self._cancelled_result(started)
        now = time.monotonic()
        call_deadline = now + remaining

        # 1. Preflight mapping — typed rejections BEFORE anything executes.
        if call.tools:
            # Client tools return to clients (D-043); Codex-internal tools
            # are not client tool calls, and the experimental dynamic-tools
            # surface is never enabled. Fail closed before execution.
            raise CodexIneligible("tool_calls_unsupported")
        if call.max_output_tokens is not None or call.generation_params:
            # No evidenced stable-surface mapping exists for an output token
            # ceiling or extra generation parameters; silently dropping
            # requested semantics is forbidden (the M04 refuse-not-drop
            # precedent). Fail closed before execution.
            raise CodexIneligible("request_parameters_unsupported")
        conversation = map_conversation(call.messages)
        output_schema = validate_structured_schema(call.response_format)
        slug = call.model.model
        effort = call.reasoning_effort

        # 2. Local eligibility: discovery, version, sandbox, controlled home.
        binary = self._discover()
        _version, version_reason = probe_codex_version(
            binary.path,
            spawner=self._spawner,
            timeout=self._version_probe_timeout,
        )
        if version_reason is not None:
            raise CodexIneligible(version_reason)
        sandbox_reason = check_sandbox_availability(
            platform_name=self._platform_name,
            release=self._platform_release,
            path_lookup=self._path_lookup,
        )
        if sandbox_reason is not None:
            raise CodexIneligible(sandbox_reason)
        try:
            self._home.ensure()
        except OSError:
            raise CodexIneligible("codex_home_invalid") from None
        home_invalid = self._home.validate()
        if home_invalid is not None:
            raise CodexIneligible(home_invalid)
        scratch = self._home.new_scratch_dir()

        # 3. The single bounded app-server session (never a second one).
        spec = CodexSpawnSpec(
            argv=(str(binary.path), "app-server"),
            env=_minimal_environment(codex_home=str(self._home.path)),
            cwd=str(scratch),
        )
        session: CodexSession | None = None
        try:
            proc = self._spawner(spec)
            session = CodexSession(proc)
            session.start()
            startup_deadline = min(now + self._startup_timeout, call_deadline)
            self._handshake(session, startup_deadline, cancel_event)

            # 4. Auth verdict (chatgpt subscription path only).
            verdict = verify_account_auth(session, call_deadline)
            if not verdict.ok:
                raise CodexIneligible(
                    verdict.reason or "auth_unverified", verdict.remediation
                )

            # 5. Exact model/effort binding against model/list.
            models = self._load_models(session, call_deadline, cancel_event)
            mismatch = verify_model_and_effort(models, slug, effort)
            if mismatch is not None:
                raise CodexIneligible(mismatch)

            # 6. The isolated ephemeral thread and the pinned turn. From the
            # moment the turn/start request is SENT, a process loss is
            # ambiguous (the backend may have consumed the request).
            thread_id = self._start_thread(
                session, conversation, call_deadline, cancel_event, scratch
            )
            if conversation.history_items:
                self._inject_history(
                    session, thread_id, conversation, call_deadline, cancel_event
                )
            try:
                turn_id = self._start_turn(
                    session,
                    thread_id=thread_id,
                    turn_input=conversation.turn_input,
                    slug=slug,
                    effort=effort,
                    output_schema=output_schema,
                    deadline=call_deadline,
                    cancel_event=cancel_event,
                    scratch=scratch,
                )
            except CodexProcessLost:
                raise CodexProcessLost(after_start=True) from None
            return self._run_turn(
                session,
                thread_id=thread_id,
                turn_id=turn_id,
                call_deadline=call_deadline,
                cancel_event=cancel_event,
                emit=emit if call.stream else None,
                started=started,
            )
        finally:
            if session is not None:
                _ = session.close()
            shutil.rmtree(scratch, ignore_errors=True)

    # ── Session phases ────────────────────────────────────────────────

    def _discover(self) -> CodexBinary:
        binary, discovery_reason = discover_codex_binary(
            pinned_binary=self._pinned_binary,
            discovery_roots=self._discovery_roots,
            path_lookup=self._path_lookup,
        )
        if binary is None:
            raise CodexIneligible(discovery_reason or "source_unavailable")
        return binary

    def _handshake(
        self,
        session: CodexSession,
        deadline: float,
        cancel_event: threading.Event,
    ) -> None:
        """``initialize`` (stable surface) + ``initialized`` notification.

        The result must carry the four evidenced string members, and
        ``codexHome`` must equal the controlled home — proof the runtime
        adopted the adapter-owned isolation boundary. Values are validated
        and compared, never retained or logged.
        """
        session.send(
            {
                "id": 1,
                "method": _METHOD_INITIALIZE,
                "params": {
                    "clientInfo": {
                        "name": "scarcity-router",
                        "title": "Scarcity Router",
                        "version": "0.0.0",
                    },
                    "capabilities": {},
                },
            }
        )
        envelope = session.await_response(
            1, deadline, abort=cancel_event.is_set
        )
        if "error" in envelope:
            raise CodexProtocolFailure("initialize_refused")
        result = _as_object(envelope.get("result"))
        if result is None or not all(
            isinstance(result.get(field), str) for field in _INITIALIZE_RESPONSE_FIELDS
        ):
            raise CodexProtocolFailure("initialize_schema_changed")
        if cast("str", result["codexHome"]) != str(self._home.path):
            raise CodexProtocolFailure("controlled_home_not_adopted")
        session.notify(_METHOD_INITIALIZED)

    def _load_models(
        self,
        session: CodexSession,
        deadline: float,
        cancel_event: threading.Event,
    ) -> dict[str, tuple[str, ...]]:
        """Paginated ``model/list`` (bounded pages) for the exact binding."""
        models: dict[str, tuple[str, ...]] = {}
        cursor: str | None = None
        for _page in range(MAX_MODEL_PAGES):
            params: dict[str, object] | None = (
                {"cursor": cursor} if cursor is not None else None
            )
            result = session.request(
                _METHOD_MODEL_LIST, params, deadline, abort=cancel_event.is_set
            )
            models.update(parse_model_listing(result))
            cursor = next_model_cursor(result)
            if cursor is None:
                return models
        # The listing still advertises more pages after the bounded page
        # budget: the catalog is incomplete, so the exact-binding decision
        # would rest on partial evidence. Fail closed with the explicit
        # budget reason (never a generic model-not-listed verdict).
        raise CodexIneligible("model_listing_budget_exceeded")

    def _start_thread(
        self,
        session: CodexSession,
        conversation: MappedConversation,
        deadline: float,
        cancel_event: threading.Event,
        scratch: Path,
    ) -> str:
        """The isolated ephemeral thread (official isolation knobs only)."""
        params: dict[str, object] = {
            "ephemeral": True,
            "cwd": str(scratch),
            "approvalPolicy": "never",
            "sandbox": "workspace-write",
        }
        if conversation.base_instructions is not None:
            params["baseInstructions"] = conversation.base_instructions
        if conversation.developer_instructions is not None:
            params["developerInstructions"] = conversation.developer_instructions
        result = session.request(
            _METHOD_THREAD_START, params, deadline, abort=cancel_event.is_set
        )
        result_map = _as_object(result)
        thread = result_map.get("thread") if result_map else None
        thread_body = _as_object(thread) if thread is not None else None
        thread_id = thread_body.get("id") if thread_body else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexProtocolFailure("thread_start_schema_changed")
        return thread_id

    def _inject_history(
        self,
        session: CodexSession,
        thread_id: str,
        conversation: MappedConversation,
        deadline: float,
        cancel_event: threading.Event,
    ) -> None:
        params: dict[str, object] = {
            "threadId": thread_id,
            "items": [dict(item) for item in conversation.history_items],
        }
        result = session.request(
            _METHOD_THREAD_INJECT, params, deadline, abort=cancel_event.is_set
        )
        if _as_object(result) is None:
            raise CodexProtocolFailure("thread_inject_schema_changed")

    def _start_turn(
        self,
        session: CodexSession,
        *,
        thread_id: str,
        turn_input: str,
        slug: str,
        effort: str | None,
        output_schema: object | None,
        deadline: float,
        cancel_event: threading.Event,
        scratch: Path,
    ) -> str:
        """The pinned turn: model slug, effort, schema, isolation policy.

        The detailed per-turn sandbox policy pins the writable root to the
        attempt's scratch directory and disables network access;
        ``dangerFullAccess`` is never sent. The model/effort pair was already
        verified against ``model/list`` — the runtime cannot silently choose
        another one.
        """
        params: dict[str, object] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": turn_input}],
            "model": slug,
            "approvalPolicy": "never",
            "cwd": str(scratch),
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [str(scratch)],
                "networkAccess": False,
            },
        }
        if effort is not None:
            params["effort"] = effort
        if output_schema is not None:
            params["outputSchema"] = output_schema
        result = session.request(
            _METHOD_TURN_START, params, deadline, abort=cancel_event.is_set
        )
        result_map = _as_object(result)
        turn = result_map.get("turn") if result_map else None
        turn_body = _as_object(turn) if turn is not None else None
        turn_id = turn_body.get("id") if turn_body else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexProtocolFailure("turn_start_schema_changed")
        return turn_id

    # ── The turn event loop ───────────────────────────────────────────

    def _run_turn(
        self,
        session: CodexSession,
        *,
        thread_id: str,
        turn_id: str,
        call_deadline: float,
        cancel_event: threading.Event,
        emit: Callable[[AdapterStreamChunk], None] | None,
        started: str,
    ) -> AdapterResult:
        parts: list[str] = []
        message_chars = 0
        usage: UsageTokens | None = None
        try:
            while True:
                kind, value = session.next_event(
                    call_deadline, abort=cancel_event.is_set
                )
                if kind == "timeout":
                    # Deadline exceeded: interrupt, bounded settle, cancelled.
                    self._interrupt(session, thread_id, turn_id, call_deadline)
                    return self._cancelled_result(
                        started, note="the execution deadline passed"
                    )
                if kind == "response":
                    continue  # no inline requests exist during the turn
                envelope = cast("dict[str, object]", value)
                method = cast("str", envelope.get("method"))
                params = _as_object(envelope.get("params")) or {}
                if method == _NOTIFICATION_TURN_COMPLETED:
                    turn = _as_object(params.get("turn"))
                    status = turn.get("status") if turn else None
                    if not isinstance(status, str) or status not in _TURN_TERMINAL_STATUSES:
                        # Unknown turn/completed status: fail closed.
                        raise CodexProtocolFailure("turn_status_unrecognized")
                    if status == "interrupted":
                        return self._cancelled_result(started)
                    if status == "failed":
                        note = safe_error_note(turn.get("error") if turn else None)
                        return self._failed_result(started, note, "")
                    # status == "completed": never report completed after a
                    # confirmed cancellation.
                    if cancel_event.is_set():
                        return self._cancelled_result(started)
                    message = AdapterMessage(
                        role="assistant", content="".join(parts) or None
                    )
                    observation = CallObservation(
                        call_index=0,
                        started_at=started,
                        ended_at=_canonical_now(),
                        status="completed",
                        provider_reported_usage=usage,
                    )
                    return AdapterResult(
                        status="completed",
                        calls=(observation,),
                        message=message,
                        finish_reason=FINISH_STOP,
                    )
                if method == _NOTIFICATION_AGENT_DELTA:
                    delta = params.get("delta")
                    if not isinstance(delta, str):
                        raise CodexProtocolFailure("protocol_malformed")
                    if len(delta) > MAX_DELTA_CHARS:
                        raise CodexProtocolFailure("protocol_budget_exceeded")
                    if message_chars + len(delta) > MAX_MESSAGE_CHARS:
                        raise CodexProtocolFailure("message_budget_exceeded")
                    message_chars += len(delta)
                    parts.append(delta)
                    if emit is not None and not cancel_event.is_set():
                        emit(AdapterStreamChunk(kind=CHUNK_TEXT_DELTA, text=delta))
                    continue
                if method == _NOTIFICATION_TOKEN_USAGE:
                    mapped = parse_token_usage(params.get("tokenUsage"))
                    if mapped is not None:
                        usage = mapped
                    continue
                # Known lifecycle notifications are ignored; unknown ones were
                # already counted and bounded by the session.
        except _Cancelled:
            # The client went away (or the deadline timer fired) mid-turn:
            # interrupt, then report cancelled — never completed.
            self._interrupt(session, thread_id, turn_id, call_deadline)
            return self._cancelled_result(started)
        except CodexProcessLost:
            raise CodexProcessLost(after_start=True) from None

    def _interrupt(
        self, session: CodexSession, thread_id: str, turn_id: str, call_deadline: float
    ) -> None:
        """One bounded ``turn/interrupt``; never raises past this point.

        The bounded wait for the interrupt acknowledgement is the deadline of
        the interrupt request itself (the evidenced interrupted status may
        arrive as its own notification, which this bounded wait consumes).
        The bound is independent of the (possibly already passed) call
        deadline so the stop is always given a bounded chance to land.
        """
        _ = call_deadline
        try:
            bound = time.monotonic() + self._interrupt_ack_seconds
            _ = session.request(
                _METHOD_TURN_INTERRUPT,
                {"threadId": thread_id, "turnId": turn_id},
                bound,
            )
        except (_ProtocolError, CodexProtocolFailure, CodexProcessLost):
            return

    # ── Result helpers ────────────────────────────────────────────────

    def _failed_result(
        self, started: str, reason: str, remediation: str
    ) -> AdapterResult:
        note = reason if not remediation else f"{reason}: {remediation}"
        return AdapterResult(
            status="failed",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at=started,
                    ended_at=_canonical_now(),
                    status="failed",
                    note=note[:200],
                ),
            ),
        )

    def _ambiguous_result(self, started: str) -> AdapterResult:
        return AdapterResult(
            status="failed",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at=started,
                    ended_at=_canonical_now(),
                    status="unknown",
                    note=(
                        "the codex runtime was lost during execution; the "
                        + "outcome is unknown and nothing was retried"
                    ),
                ),
            ),
        )

    def _cancelled_result(self, started: str, *, note: str = "") -> AdapterResult:
        return AdapterResult(
            status="cancelled",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at=started,
                    ended_at=_canonical_now(),
                    status="cancelled",
                    note=note[:200] if note else None,
                ),
            ),
        )

    def _deadline_remaining(self, deadline: str) -> float | None:
        try:
            parsed = datetime.fromisoformat(deadline[:-1] + "+00:00")
        except (ValueError, IndexError, TypeError):
            return None
        return (parsed - datetime.now(timezone.utc)).total_seconds()

    # ── Resource snapshots ────────────────────────────────────────────

    def resource_snapshots(self, observed_at: str) -> tuple[ResourceStateSnapshot, ...]:
        """One safe eligibility observation; installed is not eligible.

        The probe walks the same closed gates as execution (discovery,
        version, sandbox, controlled home, and the bounded ``account/read``
        auth verdict) and reports honest diagnostics in the closed capacity
        v3 vocabulary (the detailed reason maps onto the closest status-level
        code; the remediation steps live in docs/providers.md). No quota
        telemetry is observed here — the existing collector owns it — and
        nothing about a successful probe proves promotional eligibility
        (D-042).
        """
        reason = self._eligibility_reason()
        if reason is None:
            snapshot = CapacitySnapshot(
                schema_version=SCHEMA_VERSION,
                provider=self._resource.provider,
                source=f"worker-local:{CODEX_ADAPTER_ID}",
                retrieved_at=observed_at,
                status="ok",
                windows=(),
                diagnostics=(),
            )
        else:
            status, code = _snapshot_class(reason)
            snapshot = CapacitySnapshot(
                schema_version=SCHEMA_VERSION,
                provider=self._resource.provider,
                source=f"worker-local:{CODEX_ADAPTER_ID}",
                retrieved_at=observed_at,
                status=status,
                windows=(),
                diagnostics=(CapacityDiagnostic(code=code),),
            )
        return (
            resource_snapshot_from_capacity(
                snapshot,
                identity=self._resource,
                quota_observation_class="unknown",
            ),
        )

    def _eligibility_reason(self) -> str | None:
        """The first failing eligibility gate, or ``None`` when eligible."""
        try:
            binary = self._discover()
            _version, version_reason = probe_codex_version(
                binary.path,
                spawner=self._spawner,
                timeout=self._version_probe_timeout,
            )
            if version_reason is not None:
                return version_reason
            sandbox_reason = check_sandbox_availability(
                platform_name=self._platform_name,
                release=self._platform_release,
                path_lookup=self._path_lookup,
            )
            if sandbox_reason is not None:
                return sandbox_reason
            self._home.ensure()
            home_invalid = self._home.validate()
            if home_invalid is not None:
                return home_invalid
            verdict = self._probe_auth(binary)
            if not verdict.ok:
                return verdict.reason or "auth_unverified"
        except CodexIneligible as exc:
            return exc.reason
        except CodexProtocolFailure as exc:
            return exc.reason
        except CodexProcessLost:
            return "codex_process_lost"
        except (OSError, RuntimeError):
            return "codex_process_lost"
        return None

    def _probe_auth(self, binary: CodexBinary) -> AuthVerdict:
        """One bounded session verifying the handshake + auth verdict."""
        scratch = self._home.new_scratch_dir()
        session: CodexSession | None = None
        try:
            spec = CodexSpawnSpec(
                argv=(str(binary.path), "app-server"),
                env=_minimal_environment(codex_home=str(self._home.path)),
                cwd=str(scratch),
            )
            proc = self._spawner(spec)
            session = CodexSession(proc)
            session.start()
            deadline = time.monotonic() + self._startup_timeout
            self._handshake(session, deadline, threading.Event())
            return verify_account_auth(session, deadline)
        finally:
            if session is not None:
                _ = session.close()
            shutil.rmtree(scratch, ignore_errors=True)


__all__ = [
    "AuthVerdict",
    "CODEX_ADAPTER_ID",
    "CODEX_PROVIDER",
    "CONTROLLED_CONFIG_NAME",
    "CodexBinary",
    "CodexIneligible",
    "CodexLocalAdapter",
    "CodexProcess",
    "CodexProcessLost",
    "CodexProtocolFailure",
    "CodexSession",
    "CodexSpawnSpec",
    "CodexSpawner",
    "ControlledCodexHome",
    "MIN_SUPPORTED_CODEX_VERSION",
    "MappedConversation",
    "check_sandbox_availability",
    "default_codex_spawner",
    "discover_codex_binary",
    "map_conversation",
    "next_model_cursor",
    "parse_model_listing",
    "parse_token_usage",
    "probe_codex_version",
    "safe_error_note",
    "validate_structured_schema",
    "verify_account_auth",
    "verify_model_and_effort",
    "terminate_codex_process",
]
