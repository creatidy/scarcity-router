"""Worker-local ZCode execution adapter: the official ZCode CLI headless run (M07).

The worker-side execution edge for subscription Z.ai capacity through the
official ZCode CLI (issue #92 Stage 2, authorized by decision **D-061**).
It implements the M05 :class:`~scarcity_router.worker_local_adapters.LocalAdapter`
protocol, so the server reaches it only through the existing one-protocol
bridge — the same shape as the Codex adapter (M06)::

    M03 coordinator -> worker_bridged adapter (server) -> M05 worker
      -> THIS adapter (allowlist id ``zcode:<source_id>``)
      -> official ``zcode`` CLI subprocess, one headless run per call
      -> ``--output-format stream-json`` NDJSON events + typed final result

There is deliberately no second worker protocol, no server-to-local-ZCode
path and no new execution channel. This is source-mode only (D-053): the
adapter is one SOURCE INSTANCE and serves the source's single plan lane.

Scope facts pinned by D-061 and its evidence record
(docs/zcode-adapter-stage2-evidence.md, CLI v3.14.3 / bundle 0.16.9,
evidence 2026-09-28 — capability claims are pinned to that dated
evidence, D-043 discipline):

- **One execution boundary.** The official ``zcode`` executable invoked
  by argv only. Prompts, workspace paths and mode values are data — never
  shell input (no ``shell=True``, no command strings, ever).
- **Explicit safe permission mode, always.** Headless ``--prompt``
  defaults to ``yolo`` (vendor-documented); every router-controlled
  invocation passes ``--mode edit`` explicitly — the LEAST-AUTHORITY
  officially supported mode that still permits the coding workflow.
  First-party evidence (official source
  ``apps/zcode-cli/packages/core/src/permission/service.ts`` at the
  D-061-pinned commit ``29628c9``, verified identical in the shipped
  0.16.9 bundle): edit mode explicitly ALLOWS tools whose permission
  name is ``edit`` and whose side-effect scope is ``workspace``
  (``mode.edit.fileEdit`` — inspecting and modifying files in the
  authorized workspace); everything else falls through to build-mode
  logic — read-only tools allowed, critical/high-risk tools (e.g.
  command execution) require an ``ask`` that a headless run cannot
  answer, so the vendor's default broker DENIES it, fail closed. The
  CLI confines its file tools to the workspace (outside paths are
  rejected; symlinks judged by real path). Denials are never bypassed,
  never pre-approved, and the mode is never downgraded to yolo.
- **No model identity claims.** ZCode exposes no supported model listing
  and no headless model steering: the executed physical model is managed
  by the user's own ZCode configuration/plan. This adapter invents NO
  model: the source's inventory carries exactly one effort-less
  plan-lane descriptor (``plan-managed`` — a reserved router-side name
  for "the plan-managed lane", not a physical model claim, no variant,
  no reasoning-effort claims). Server-side adoption therefore stays
  closed (visible, never routable) until owner-approved ``zai`` track
  evidence lands in the track registry — the normal D-053 behavior for
  unevidenced slugs. Model identities observed in structured results are
  not parsed at all: no result member is evidenced as a trustworthy
  executed-model identity.
- **Credentials stay provider-managed.** This module never reads, copies,
  serializes, persists or logs any ZCode/Z.ai credential, and never
  touches ZCode's own configuration or security-restriction files
  (D-061 constraint 3). Authentication state is NOT probeable without
  paid inference on the evidenced surface (no supported non-inference
  auth check exists), so a healthy CLI reports the honest ``unverified``
  state — never "authenticated because the binary exists". The owner's
  official ``zcode login zai`` remains the only sign-in path; the login
  itself is never wrapped, automated or fallback-ed.
- **Workspace authority is construction-time, administrator-owned.**
  The adapter is constructed with ONE explicitly authorized project
  workspace (an existing real directory, canonicalized with
  ``realpath`` at construction and re-validated before every run) and
  passes that exact canonical directory as both ``--cwd`` and the
  process cwd — ZCode inspects and edits THE AUTHORIZED PROJECT, not an
  adapter-created scratch directory. Request content can never supply,
  alter or select a path (no request field reaches the workspace
  decision), the ambient cwd is never trusted, and the adapter's own
  private state is never substituted: the adapter keeps NO execution
  directory of its own at all. ZCode's runtime state stays in ZCode's
  own supported installation; the adapter never touches credentials or
  ZCode's configuration/security files (D-061 constraint 3).
- **Honest lifecycle and output.** Exactly one CLI invocation per
  dispatched call — no retry, no second process, no fallback. Structured
  events are consumed incrementally under bounded budgets (per-line,
  total bytes, event count, unknown-event count, stderr bytes — stderr
  content is never read, only counted). The evidenced answer surface is
  the terminal ``type:"result"`` line: no incremental text shape is
  evidenced on the stream, so no text delta is ever synthesized, and a
  completed classification requires the documented terminal contract
  (result line AND exit 0). Anything else is a typed failure or an
  explicit ``unknown`` observation — the ambiguity survives to the audit
  trail, and usage stays absent (the result's optional usage member has
  no evidenced internal field names to map).

Process-seam provenance: the spawn spec, process protocol, default
spawner and bounded terminate/reap helper are the Codex adapter's
proven process-lifecycle seam (M06), imported unchanged under neutral
names — one lifecycle implementation, two consumers. Environment
building and discovery differ (no controlled config home: ZCode runs
with its own supported installation and login state) and are local.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, cast

from .capacity import SCHEMA_VERSION, CapacityDiagnostic, CapacitySnapshot
from .gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterResult,
    AdapterStreamChunk,
    CallObservation,
    FINISH_STOP,
)
from .gateway_validation import v_safe_id
from .model_inventory import (
    SOURCE_ID_MAX_LENGTH,
    DiscoveredModel,
    ModelInventoryError,
    SourceInventory,
    source_resource_id,
)
from .providers.openai_codex_acquisition import BoundedLineReader
from .resource_state import (
    ResourceIdentity,
    ResourceStateSnapshot,
    resource_snapshot_from_capacity,
)
from .worker_codex_adapter import (
    CodexProcess as ChildProcess,
    CodexSpawnSpec as SpawnSpec,
    CodexSpawner as ChildSpawner,
    default_codex_spawner as default_child_spawner,
    terminate_codex_process as terminate_child_process,
)
# ── Adapter identity and version contract ─────────────────────────────────────

ZCODE_ADAPTER_ID = "zcode"

#: The provider identity for the Z.ai subscription lane; resource identity
#: and snapshots use the same provider vocabulary as the existing Z.ai
#: catalog entries (model-catalog.json).
ZCODE_PROVIDER = "zai"

#: The single reserved plan-lane descriptor slug. This is a ROUTER-SIDE
#: name for "the source's plan-managed execution lane", deliberately NOT
#: a physical model claim: ZCode has no supported listing or steering, so
#: no physical identity, variant or reasoning effort is ever asserted.
#: The lane resource is effort-less (``<source_id>:plan-managed``, no
#: variant) and routes through the normal D-053/D-055 machinery as a
#: vendor-managed lane (D-063: the owner-reviewed ``zai/plan`` track):
#: routing to it means exactly "execute through this ZCode plan-managed
#: lane in the authorized workspace", never "execute through GLM-…".
PLAN_LANE_SLUG = "plan-managed"

#: The evidenced bundle version of the pinned official release
#: (v3.14.3, 2026-09-24, commit ``29628c9`` reports ``0.16.9``; D-061
#: evidence 2026-09-28). Anything older is unsupported — never
#: "compatible because the binary exists". Pre-release segments after
#: the numeric triple are tolerated.
MIN_SUPPORTED_ZCODE_VERSION: tuple[int, int, int] = (0, 16, 9)

#: The one permission mode this adapter ever selects: the
#: LEAST-AUTHORITY officially supported mode that still permits the
#: coding workflow (inspect, reason over, and modify files in the
#: authorized workspace). First-party evidence, two agreeing sources:
#: the official repo's ``permission/service.ts`` at the D-061-pinned
#: commit ``29628c9`` and the identical shipped 0.16.9 bundle —
#: edit mode explicitly ALLOWS workspace-scoped file-edit tools
#: (``permissionName == "edit" && sideEffectScope == "workspace"`` →
#: ``mode.edit.fileEdit``); every other side-effecting or high/critical
#: risk tool still returns ``ask``, which a headless run cannot answer,
#: so the vendor's default broker DENIES it (fail closed). Headless
#: ``--prompt`` defaults to ``yolo``; the default is never relied on,
#: never restored, and never fallen back to.
SAFE_PERMISSION_MODE = "edit"

_VERSION_RE = re.compile(r"(^|\s)(\d+)\.(\d+)\.(\d+)")

# ── Bounded-run budgets ───────────────────────────────────────────────────────

VERSION_PROBE_TIMEOUT_SECONDS = 10.0
DOCTOR_PROBE_TIMEOUT_SECONDS = 10.0
TERMINATE_TIMEOUT_SECONDS = 2.0

MAX_PROBE_OUTPUT_BYTES = 4 * 1024

#: The terminal result line carries the whole response text, so the
#: per-line bound is sized by the response bound; progress-event lines
#: are far below it and the total stream budget still bounds everything.
MAX_RESPONSE_CHARS = 8_000_000
MAX_LINE_BYTES = 8 * 1024 * 1024 + 64 * 1024
MAX_STREAM_TOTAL_BYTES = 32 * 1024 * 1024
MAX_STREAM_EVENTS = 20_000
MAX_UNKNOWN_EVENTS = 1_024
MAX_STDERR_BYTES = 8 * 1024

#: The prompt is one argv element (the documented ``--prompt`` surface).
#: Linux caps a single argument at 128 KiB (``MAX_ARG_STRLEN``), so the
#: encoded-prompt bound stays safely under it; larger conversations are a
#: typed rejection, never a truncation or an E2BIG surprise.
MAX_PROMPT_BYTES = 64 * 1024

_MAX_ID_BOUND = 256

#: Evidenced session event ``type`` values from the D-061 live spike
#: (evidence section 4). Unknown types are tolerated and counted, never
#: parsed for content.
_KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "result",
        "session.titleUpdated",
        "session.updated",
        "turn.started",
        "turn.completed",
        "turn.failed",
    }
)


# ── Typed internal failures (worker-seam vocabulary) ──────────────────────────


class ZCodeIneligible(Exception):
    """The source is not executable; raised BEFORE any run starts.

    ``reason`` is a closed safe reason code; ``remediation`` is a short
    safe action hint (an official install/login step — never token
    copying).
    """

    def __init__(self, reason: str, remediation: str = "") -> None:
        super().__init__(reason)
        self.reason: str = reason
        self.remediation: str = remediation


class ZCodeProtocolFailure(Exception):
    """The CLI's structured output drifted from the validated shape."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason: str = reason


class ZCodeProcessLost(Exception):
    """The CLI process was lost.

    ``after_start`` marks whether execution may already have begun — the
    honest ambiguity that must survive to the result (never retried).
    """

    def __init__(self, after_start: bool) -> None:
        super().__init__("zcode process lost")
        self.after_start: bool = after_start


class _Cancelled(Exception):
    """Internal: the dispatch was cancelled/deadlined; nothing to report."""

    def __init__(self, note: str = "") -> None:
        super().__init__(note or "cancelled")
        self.note: str = note


# ── Environment (narrow, never the full inherited env) ────────────────────────


def _minimal_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A minimal child environment — never the worker's full user env.

    Only ``PATH`` (so the CLI can reach its own runtime/tooling) and
    ``HOME`` (ZCode's supported installation/login state location) are
    inherited; on Windows the process-creation basics are added
    defensively (untested — this host cannot verify). No request content
    can ever add a variable here, and no unrelated secret can leak into
    the child or any log.
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
    return child


# ── Discovery (deterministic, bounded, allowlisted) ───────────────────────────


@dataclass(frozen=True)
class ZCodeBinary:
    """One discovered binary candidate (safe facts only)."""

    path: Path
    source: str  # "pinned" | "path"


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


def discover_zcode_binary(
    *,
    pinned_binary: Path | None = None,
    path_lookup: Callable[[str], str | None] = shutil.which,
) -> tuple[ZCodeBinary | None, str | None]:
    """Deterministic discovery: explicit pin first, then ``PATH``.

    Returns ``(binary, None)`` or ``(None, closed_reason)`` with one of
    ``pinned_binary_invalid`` or ``source_unavailable``. There is no
    third discovery channel: vendor-bundled desktop installs are not
    evidenced as invocable and are never guessed at.
    """
    if pinned_binary is not None:
        if _regular_executable(pinned_binary, allow_symlink=False):
            return ZCodeBinary(path=pinned_binary, source="pinned"), None
        return None, "pinned_binary_invalid"
    path_hit = path_lookup("zcode")
    if path_hit:
        candidate = Path(path_hit)
        if _regular_executable(candidate, allow_symlink=True):
            return ZCodeBinary(path=candidate, source="path"), None
        return None, "source_unavailable"
    return None, "source_unavailable"


# ── Bounded probes (version, doctor) ──────────────────────────────────────────


def _bounded_probe_output(
    proc: ChildProcess,
) -> str | None:
    """Read a finished probe process's stdout under the probe byte cap.

    The caller has already bounded-waited and terminated the process, so
    both pipes are at EOF and the post-termination read returns promptly.
    Returns the decoded text, or ``None`` when the pipe failed or the
    output exceeded the probe cap.
    """
    stdout = proc.stdout
    if stdout is None:
        return None
    try:
        output = stdout.read(MAX_PROBE_OUTPUT_BYTES + 1) or b""
    except (OSError, ValueError, AttributeError):
        return None
    finally:
        # The process is reaped, so both pipes are at EOF: close them here
        # so the probe owns every descriptor it created.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
    if len(output) > MAX_PROBE_OUTPUT_BYTES:
        return None
    return output.decode(encoding="utf-8", errors="replace")


def probe_zcode_version(
    binary: Path,
    *,
    spawner: ChildSpawner,
    timeout: float = VERSION_PROBE_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> tuple[tuple[int, int, int] | None, str | None]:
    """Run ``<binary> --version`` (bounded) and parse the evidenced shape.

    Returns ``(version_tuple, None)`` on a supported generation or
    ``(None, closed_reason)`` with one of ``version_probe_failed``,
    ``version_unparseable`` or ``version_unsupported``.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("version probe timeout must be a positive number")
    spec = SpawnSpec(
        argv=(str(binary), "--version"),
        env=_minimal_environment(),
        cwd=str(cwd) if cwd is not None else os.getcwd(),
    )
    try:
        proc = spawner(spec)
    except OSError:
        return None, "version_probe_failed"
    reason: str | None = None
    try:
        try:
            status = proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            reason = "version_probe_failed"
        else:
            if status != 0:
                # A failing exit is not a version answer, even when the
                # output looks like one.
                reason = "version_probe_failed"
    finally:
        # All writers are dead after the bounded termination, so the
        # post-termination read below always returns promptly.
        _ = terminate_child_process(proc)
    if reason is not None:
        return None, reason
    text = _bounded_probe_output(proc)
    if text is None:
        return None, "version_probe_failed"
    match = _VERSION_RE.search(text.strip())
    if match is None:
        return None, "version_unparseable"
    version = (int(match.group(2)), int(match.group(3)), int(match.group(4)))
    if version < MIN_SUPPORTED_ZCODE_VERSION:
        return None, "version_unsupported"
    return version, None


# ── Strict JSON decode (shared discipline with the Codex path) ────────────────


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
    """Strict UTF-8 JSON with exactly one finite interpretation per line."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise ZCodeProtocolFailure("protocol_malformed") from None
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
        # JSONDecodeError, duplicate keys, NaN/Infinity, non-finite
        # exponents and adversarially deep nesting are all drift.
        raise ZCodeProtocolFailure("protocol_malformed") from None


def _as_object(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def _bounded_str(value: object) -> str | None:
    """A bounded opaque string, or ``None`` (absent/mistyped stays absent)."""
    if isinstance(value, str) and value and len(value) <= _MAX_ID_BOUND:
        return value
    return None


# ── The doctor probe (structured runtime evidence) ────────────────────────────


def probe_zcode_doctor(
    binary: Path,
    *,
    spawner: ChildSpawner,
    timeout: float = DOCTOR_PROBE_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> str | None:
    """Run ``<binary> doctor --json`` (bounded) and validate the shape.

    The evidenced probe result carries ``cli.name`` and ``cli.version``
    as strings (D-061 evidence sections 2 and 4); anything else is
    drift. Values are validated and never retained or logged. Returns
    ``None`` when the CLI answered healthily, else a closed reason.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("doctor probe timeout must be a positive number")
    spec = SpawnSpec(
        argv=(str(binary), "doctor", "--json"),
        env=_minimal_environment(),
        cwd=str(cwd) if cwd is not None else os.getcwd(),
    )
    try:
        proc = spawner(spec)
    except OSError:
        return "doctor_probe_failed"
    reason: str | None = None
    try:
        try:
            status = proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            reason = "doctor_probe_failed"
        else:
            if status != 0:
                reason = "doctor_probe_failed"
    finally:
        _ = terminate_child_process(proc)
    if reason is not None:
        return reason
    text = _bounded_probe_output(proc)
    if text is None:
        return "doctor_probe_failed"
    try:
        decoded = _decode_strict(text.strip().encode("utf-8"))
    except ZCodeProtocolFailure:
        return "doctor_schema_changed"
    doctor = _as_object(decoded)
    if doctor is None:
        return "doctor_schema_changed"
    cli = _as_object(doctor.get("cli"))
    if cli is None or not isinstance(cli.get("name"), str):
        return "doctor_schema_changed"
    if not isinstance(cli.get("version"), str):
        return "doctor_schema_changed"
    return None


# ── Workspace authority (administrator-owned, canonical, re-validated) ────────

_WORKSPACE_MODE_OK_MASK = 0o077  # group/other bits must be absent on the path itself


def _canonical_workspace(path: str | os.PathLike[str]) -> Path:
    """The canonical authorized workspace for a configured path.

    ``realpath`` resolves every symlink and alias BEFORE any check, so
    the directory handed to ZCode is the real project directory the
    administrator named — a symlinked configuration path is resolved,
    never followed blindly at run time. Raises ``NotADirectoryError``
    when the resolved path is not a real directory.
    """
    resolved = Path(os.path.realpath(Path(path)))
    st = os.lstat(resolved)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(str(resolved))
    return resolved


def validate_workspace(workspace: Path) -> str | None:
    """Re-check the authorized workspace; ``None`` when intact.

    Fresh per run: the directory must still exist as a real directory
    (a deleted or replaced workspace fails closed before any spawn).
    Group/other permission bits on the directory itself do not invalidate
    a user-owned project, so they are deliberately not required away
    here — authority comes from the administrator's construction-time
    grant, not from filesystem mode bits.
    """
    try:
        st = os.lstat(workspace)
    except OSError:
        return "workspace_unavailable"
    if not stat.S_ISDIR(st.st_mode):
        return "workspace_invalid"
    return None


# ── Structured result parsing ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ZCodeRunResult:
    """The validated terminal result of one headless run.

    Only the evidenced members are represented. ``response`` is the run's
    answer text; ``session_id`` is an opaque bounded reference kept only
    as call provenance. Usage is deliberately absent: the terminal
    result carries an OPTIONAL usage member whose internal field names
    are NOT evidenced (D-061 evidence section 2), so nothing is mapped —
    unknown stays unknown, never invented.
    """

    response: str
    session_id: str | None


def parse_run_result(value: object) -> ZCodeRunResult:
    """Validate the terminal ``type:"result"`` event, or fail closed.

    Required: ``response`` as a bounded string (the run's answer).
    ``sessionId`` is accepted as a bounded opaque string when present.
    ``projection.status`` must be a string when a ``projection`` member
    is present (structural check only — completion is established by the
    terminal line AND the exit status, never by an un-evidenced status
    vocabulary). Unknown members are ignored (the terminal schema may
    grow); mistyped evidenced members are drift.
    """
    result = _as_object(value)
    if result is None:
        raise ZCodeProtocolFailure("result_malformed")
    response = result.get("response")
    if not isinstance(response, str):
        raise ZCodeProtocolFailure("result_malformed")
    if len(response) > MAX_RESPONSE_CHARS:
        raise ZCodeProtocolFailure("response_budget_exceeded")
    projection = result.get("projection")
    if projection is not None:
        projection_map = _as_object(projection)
        if projection_map is None or not isinstance(
            projection_map.get("status"), str
        ):
            raise ZCodeProtocolFailure("result_malformed")
    session_id = _bounded_str(result.get("sessionId"))
    if session_id is None:
        session_id = _bounded_str(result.get("session_id"))
    return ZCodeRunResult(response=response, session_id=session_id)


# ── The prompt mapping (client vocabulary -> one prompt) ──────────────────────


def map_prompt(messages: Sequence[AdapterMessage]) -> str:
    """Validate the conversation onto the single-prompt surface, or reject.

    The evidenced headless surface is ONE prompt string: no system or
    developer instruction channel, no history injection, no tool-result
    continuation. Exactly one non-empty user message is therefore
    representable; every other shape is a typed rejection BEFORE any
    execution (the refuse-not-drop precedent — collapsing roles into one
    blob would invent semantics). Client tools return to clients (D-043);
    ZCode's internal tools are its own and are never client tool calls.
    """
    if not messages:
        raise ZCodeIneligible("no_user_input")
    if len(messages) != 1:
        raise ZCodeIneligible("conversation_shape_unsupported")
    message = messages[0]
    if message.role == "tool" or message.tool_calls or message.tool_call_id:
        raise ZCodeIneligible("tool_messages_unsupported")
    if message.role != "user":
        raise ZCodeIneligible("conversation_shape_unsupported")
    if message.content is None or not message.content:
        raise ZCodeIneligible("empty_message_unsupported")
    return message.content


def build_run_argv(
    *,
    binary: Path,
    prompt: str,
    workspace: Path,
) -> tuple[str, ...]:
    """The one argv vector this adapter ever spawns (no shell, ever).

    Every value is composed by the adapter; the prompt and workspace path
    are argv DATA. ``--mode build`` is always explicit (the headless
    default is ``yolo`` and is never relied on), the workspace is always
    explicit, and ``--output-format stream-json`` always selects the
    documented structured surface (an unknown value would be an error —
    the CLI never silently downgrades). ``--no-browser`` matches the
    demonstrated D-061 invocation (no browser may spawn from a headless
    run).
    """
    return (
        str(binary),
        "--prompt",
        prompt,
        "--cwd",
        str(workspace),
        "--mode",
        SAFE_PERMISSION_MODE,
        "--output-format",
        "stream-json",
        "--no-browser",
    )


# ── The adapter ───────────────────────────────────────────────────────────────


def _canonical_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _drain_stderr(stream: IO[bytes], cap_bytes: int) -> None:
    """Capture stderr with a hard byte cap; never expose its content.

    The headless CLI writes failure text (``Error: <message> (traceId:
    …)``) to stderr; the message may carry provider or prompt-derived
    text, so it is counted, never read or forwarded.
    """
    seen = 0
    try:
        while seen <= cap_bytes:
            chunk = stream.read(4096)
            if not chunk:
                return
            seen += len(chunk)
    except (OSError, ValueError):
        return


class ZCodeLocalAdapter:
    """The worker-local adapter for the official ZCode CLI (``zcode``).

    Source-mode only (D-053): the adapter is the instance
    ``zcode:<source_id>`` and serves the source's single plan lane
    against ONE administrator-authorized project workspace. Eligibility
    is established lazily and honestly per call and per snapshot, so a
    broken ZCode installation never prevents the worker from serving its
    other adapters.

    The lane resource is served ONLY while the latest bounded probes
    passed AND the authorized workspace is intact: an installed-but-
    broken CLI — or a vanished project directory — serves nothing
    (installed is not eligible). Server-side, the lane routes through
    the normal adoption machinery as a vendor-managed lane (D-063);
    this adapter never claims a physical model.
    """

    adapter_id: str

    def __init__(
        self,
        *,
        source_id: str,
        authorized_workspace: str | os.PathLike[str],
        pinned_binary: Path | None = None,
        path_lookup: Callable[[str], str | None] = shutil.which,
        spawner: ChildSpawner = default_child_spawner,
        version_probe_timeout: float = VERSION_PROBE_TIMEOUT_SECONDS,
        doctor_probe_timeout: float = DOCTOR_PROBE_TIMEOUT_SECONDS,
        inventory_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source_id: str = v_safe_id(source_id, "zcode_source_id")
        if len(self._source_id) > SOURCE_ID_MAX_LENGTH:
            raise ValueError(
                f"zcode_adapter: source_id {source_id!r} longer than "
                + f"{SOURCE_ID_MAX_LENGTH} chars"
            )
        if ":" in self._source_id:
            # The derived namespace partitions on the first colon; a
            # colon inside a source id would make ownership ambiguous
            # (the same rule as the server-side source configuration).
            raise ValueError(
                "zcode_adapter: source_id ':' is not allowed (the derived "
                + "resource namespace is <source_id>:<slug>)"
            )
        self.adapter_id = f"{ZCODE_ADAPTER_ID}:{self._source_id}"
        # Workspace authority is a construction-time grant: the path is
        # canonicalized (realpath) and verified ONCE here — a configured
        # non-directory fails the adapter's construction loudly instead
        # of serving a lane that cannot do coding work. The canonical
        # directory is re-validated fresh before every run.
        self._workspace: Path = _canonical_workspace(authorized_workspace)
        self._pinned_binary: Path | None = pinned_binary
        self._path_lookup: Callable[[str], str | None] = path_lookup
        self._spawner: ChildSpawner = spawner
        self._version_probe_timeout: float = version_probe_timeout
        self._doctor_probe_timeout: float = doctor_probe_timeout
        self._inventory_ttl: float = inventory_ttl_seconds
        self._clock: Callable[[], float] = clock
        # D-053 discovery state: the latest bounded runtime observation.
        # Written only by the observation path; reads are from the
        # worker's own session thread plus snapshot/report paths.
        self._inventory_lock: threading.Lock = threading.Lock()
        self._inventory: SourceInventory | None = None
        self._inventory_monotonic: float | None = None

    @property
    def source_id(self) -> str:
        """The execution-source instance id."""
        return self._source_id

    @property
    def lane_resource_id(self) -> str:
        """The derived id of the single plan-lane resource."""
        return source_resource_id(self._source_id, PLAN_LANE_SLUG)

    @property
    def resource_ids(self) -> tuple[str, ...]:
        """The served resources: the lane iff probes passed AND the
        authorized workspace is intact."""
        inventory = self.inventory_report()
        if inventory is None or inventory.auth_state == "unavailable":
            return ()
        if validate_workspace(self._workspace) is not None:
            return ()
        return (self.lane_resource_id,)

    @property
    def authorized_workspace(self) -> Path:
        """The canonical authorized project workspace (ZCode's ``--cwd``)."""
        return self._workspace

    # ── D-053 runtime discovery ───────────────────────────────────────

    def observe_inventory(self, now: float | None = None) -> SourceInventory:
        """One bounded discovery observation (version + doctor probes).

        Deterministic and bounded: both probes are non-inference (no
        quota is ever spent to prove availability), and any failure maps
        to the honest ``unavailable`` auth state with an EMPTY model
        list. A healthy CLI reports ``unverified``: no supported
        non-inference auth probe exists on the evidenced surface, so an
        auth state is never claimed. The result replaces the previous
        observation atomically.
        """
        moment = self._clock() if now is None else now
        auth_state = "unavailable"
        runtime_version = ""
        models: tuple[DiscoveredModel, ...] = ()
        try:
            binary = self._discover()
            workspace_invalid = validate_workspace(self._workspace)
            if workspace_invalid is not None:
                raise ZCodeIneligible(workspace_invalid)
            version, version_reason = probe_zcode_version(
                binary.path,
                spawner=self._spawner,
                timeout=self._version_probe_timeout,
            )
            if version is None or version_reason is not None:
                raise ZCodeIneligible(version_reason or "version_unsupported")
            doctor_reason = probe_zcode_doctor(
                binary.path,
                spawner=self._spawner,
                timeout=self._doctor_probe_timeout,
            )
            if doctor_reason is not None:
                raise ZCodeIneligible(doctor_reason)
            auth_state = "unverified"
            runtime_version = ".".join(str(part) for part in version)
            # The single plan-lane descriptor: no physical model claim,
            # no effort claim (see PLAN_LANE_SLUG).
            models = (DiscoveredModel(slug=PLAN_LANE_SLUG, reasoning_efforts=()),)
        except ZCodeIneligible:
            pass
        except (ZCodeProtocolFailure, ModelInventoryError):
            auth_state = "unavailable"
            models = ()
        except (OSError, RuntimeError):
            auth_state = "unavailable"
        inventory = SourceInventory(
            source_id=self._source_id,
            adapter_id=self.adapter_id,
            kind="zcode_subscription",
            observed_at=_canonical_now(),
            auth_state=auth_state,  # type: ignore[arg-type]
            runtime_name="zcode",
            runtime_version=runtime_version,
            models=models,
        )
        with self._inventory_lock:
            self._inventory = inventory
            self._inventory_monotonic = moment
        return inventory

    def inventory_if_due(self) -> bool:
        """Whether a discovery observation is due (bounded cadence)."""
        with self._inventory_lock:
            if self._inventory_monotonic is None:
                return True
            return (self._clock() - self._inventory_monotonic) >= self._inventory_ttl

    def refresh_inventory_if_due(self) -> None:
        """One due discovery observation; failures isolate to this source."""
        if not self.inventory_if_due():
            return
        try:
            _ = self.observe_inventory()
        except (OSError, RuntimeError, ZCodeProtocolFailure):
            # observe_inventory maps failures into honest states; a raise
            # here would risk the worker's report loop, which is exactly
            # the isolation this program forbids.
            return

    def inventory_report(self) -> SourceInventory | None:
        """The latest observation for the worker-protocol inventory section."""
        with self._inventory_lock:
            return self._inventory

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
        _ = emit  # no evidenced incremental text surface: nothing synthesized
        try:
            return self._invoke(
                call,
                cancel_event=cancel_event,
                deadline=deadline,
                started=started,
            )
        except _Cancelled as exc:
            return self._cancelled_result(started, note=exc.note)
        except ZCodeIneligible as exc:
            return self._failed_result(started, exc.reason, exc.remediation)
        except ZCodeProtocolFailure as exc:
            return self._failed_result(started, exc.reason, "")
        except ZCodeProcessLost as exc:
            if exc.after_start:
                return self._ambiguous_result(started)
            return self._failed_result(started, "zcode_process_lost", "")
        except (OSError, RuntimeError):
            # Spawner or reader-thread startup failures are definitive for
            # this attempt (exactly one invocation — never retried here).
            return self._failed_result(started, "zcode_process_lost", "")

    def _invoke(
        self,
        call: AdapterCall,
        *,
        cancel_event: threading.Event,
        deadline: str,
        started: str,
    ) -> AdapterResult:
        if cancel_event.is_set():
            return self._cancelled_result(started)
        # Ownership: exactly the lane resource of THIS source, with the
        # lane's own provider/model identity. The request's logical
        # ModelIdentity is deliberately NOT compared: ZCode cannot verify
        # or steer a physical model, so the administrator's binding is
        # the only model-identity authority (the loopback-Ollama trust
        # model), and the executed identity is provenance, never a claim.
        if (
            call.resource.resource_id != self.lane_resource_id
            or call.resource.provider != ZCODE_PROVIDER
            or call.resource.model != PLAN_LANE_SLUG
        ):
            raise ZCodeIneligible("resource_not_served")
        remaining = self._deadline_remaining(deadline)
        if remaining is None or remaining <= 0.0:
            return self._cancelled_result(started)
        call_deadline = time.monotonic() + remaining

        # 1. Preflight mapping — typed rejections BEFORE anything executes.
        if call.tools:
            raise ZCodeIneligible("tool_calls_unsupported")
        if call.max_output_tokens is not None:
            # The evidenced headless surface carries NO output-token
            # control; the #136 normalization rule governs composed
            # dispatch, and this backstop refuses any direct call that
            # reaches the adapter with a limit (never silently dropped).
            raise ZCodeIneligible("output_limit_unenforceable")
        if call.generation_params:
            raise ZCodeIneligible("request_parameters_unsupported")
        if call.response_format is not None and call.response_format.get("type") != "text":
            raise ZCodeIneligible("response_format_unsupported")
        if call.reasoning_effort is not None:
            # No supported headless steering exists: an effort request can
            # never be honored here, and silently substituting the plan's
            # own effort is forbidden (D-056 exact-identity invariant).
            raise ZCodeIneligible("reasoning_effort_not_enforceable")
        prompt = map_prompt(call.messages)
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ZCodeIneligible("prompt_too_large")

        # 2. Fresh workspace authority, then fresh eligibility probes
        # (never stale state; probes run INSIDE the authorized workspace
        # so no ambient cwd is ever involved).
        workspace_invalid = validate_workspace(self._workspace)
        if workspace_invalid is not None:
            raise ZCodeIneligible(workspace_invalid)
        binary = self._discover()
        version, version_reason = probe_zcode_version(
            binary.path,
            spawner=self._spawner,
            timeout=self._version_probe_timeout,
            cwd=self._workspace,
        )
        if version is None or version_reason is not None:
            raise ZCodeIneligible(version_reason or "version_unsupported")
        doctor_reason = probe_zcode_doctor(
            binary.path,
            spawner=self._spawner,
            timeout=self._doctor_probe_timeout,
            cwd=self._workspace,
        )
        if doctor_reason is not None:
            raise ZCodeIneligible(doctor_reason)

        # 3. The single bounded headless run (never a second one) in THE
        # AUTHORIZED PROJECT WORKSPACE — both --cwd and process cwd.
        version_text = ".".join(str(part) for part in version)
        spec = SpawnSpec(
            argv=build_run_argv(
                binary=binary.path, prompt=prompt, workspace=self._workspace
            ),
            env=_minimal_environment(),
            cwd=str(self._workspace),
        )
        proc: ChildProcess | None = None
        reader: BoundedLineReader | None = None
        stderr_thread: threading.Thread | None = None
        try:
            proc = self._spawner(spec)
            stdin = proc.stdin
            if stdin is not None:
                # The documented headless surface reads no stdin; close it
                # immediately so the child can never wait on it.
                try:
                    stdin.close()
                except (OSError, ValueError):
                    pass
            stderr = proc.stderr
            if stderr is not None:
                stderr_thread = threading.Thread(
                    target=_drain_stderr,
                    args=(stderr, MAX_STDERR_BYTES),
                    daemon=True,
                    name="zcode-run-stderr",
                )
                stderr_thread.start()
            assert proc.stdout is not None  # the default spawner pipes stdout
            reader = BoundedLineReader(
                proc.stdout,
                max_line_bytes=MAX_LINE_BYTES,
                max_total_bytes=MAX_STREAM_TOTAL_BYTES,
            )
            reader.start()
            run_result = self._consume_stream(
                reader, call_deadline, cancel_event
            )
            # The terminal line (or stream end) was reached: bound the
            # wait for the exit status that completes the contract.
            exit_status = _bounded_exit(proc)
            return self._classify(
                run_result,
                exit_status,
                version_text=version_text,
                started=started,
                cancel_event=cancel_event,
            )
        except _Cancelled as exc:
            # The client went away (or the deadline passed) mid-run:
            # stop the child, then report cancelled — never completed.
            if proc is not None:
                _ = terminate_child_process(proc)
            return self._cancelled_result(started, note=exc.note)
        except ZCodeProcessLost:
            if proc is not None:
                _ = terminate_child_process(proc)
            raise
        finally:
            if proc is not None:
                _ = terminate_child_process(proc)
            if reader is not None:
                reader.close()
                reader.join(timeout=1.0)
            if stderr_thread is not None:
                stderr_thread.join(timeout=1.0)

    # ── Stream consumption ────────────────────────────────────────────

    def _consume_stream(
        self,
        reader: BoundedLineReader,
        call_deadline: float,
        cancel_event: threading.Event,
    ) -> ZCodeRunResult | None:
        """Consume the bounded NDJSON stream to its terminal result.

        Returns the parsed terminal result, or ``None`` when the stream
        ended without one (the caller then classifies by exit status).
        Malformed structure or budget exhaustion raise the typed
        failures; a lost process without a knowable exit raises
        :class:`ZCodeProcessLost(after_start=True)` (execution may have
        begun — the spawn already succeeded).
        """
        events_seen = 0
        unknown_events = 0
        while True:
            remaining = call_deadline - time.monotonic()
            if remaining <= 0.0:
                raise _Cancelled("the execution deadline passed")
            if cancel_event.is_set():
                raise _Cancelled()
            try:
                kind, chunk = reader.get(min(0.1, remaining))
            except queue.Empty:
                continue
            if kind == "line":
                data = cast(bytes, chunk)
                if not data.strip():
                    continue
                events_seen += 1
                if events_seen > MAX_STREAM_EVENTS:
                    raise ZCodeProtocolFailure("event_budget_exceeded")
                decoded = _decode_strict(data)
                envelope = _as_object(decoded)
                if envelope is None:
                    raise ZCodeProtocolFailure("protocol_malformed")
                event_type = envelope.get("type")
                if not isinstance(event_type, str) or not event_type:
                    raise ZCodeProtocolFailure("protocol_malformed")
                if event_type == "result":
                    return parse_run_result(envelope)
                if event_type not in _KNOWN_EVENT_TYPES:
                    unknown_events += 1
                    if unknown_events > MAX_UNKNOWN_EVENTS:
                        raise ZCodeProtocolFailure(
                            "unknown_event_budget_exceeded"
                        )
                # Progress events carry no evidenced response text; the
                # terminal result is the only evidenced answer surface.
                continue
            if kind == "oversized":
                raise ZCodeProtocolFailure("stream_budget_exceeded")
            # "eof" / "failed": the process is gone or its pipe failed.
            # Whether execution began is classified by the exit status
            # the caller can now observe (a self-exited process waits
            # promptly); an un-reapable one stays honestly ambiguous.
            return None

    def _classify(
        self,
        run_result: ZCodeRunResult | None,
        exit_status: int | None,
        *,
        version_text: str,
        started: str,
        cancel_event: threading.Event,
    ) -> AdapterResult:
        """The terminal contract: result line AND exit 0 — nothing else.

        If cancellation arrived before the terminal contract completed,
        the outcome is cancelled even when a result line exists (never
        completed after a confirmed cancellation).
        """
        if cancel_event.is_set():
            return self._cancelled_result(started)
        note = f"zcode {version_text}; mode={SAFE_PERMISSION_MODE}"
        if run_result is not None and run_result.session_id:
            note = f"{note}; session {run_result.session_id}"
        if run_result is not None and exit_status == 0:
            observation = CallObservation(
                call_index=0,
                started_at=started,
                ended_at=_canonical_now(),
                status="completed",
                provider_reported_usage=None,
                note=note[:200],
            )
            return AdapterResult(
                status="completed",
                calls=(observation,),
                message=AdapterMessage(
                    role="assistant", content=run_result.response
                ),
                finish_reason=FINISH_STOP,
            )
        if run_result is not None:
            if exit_status is not None:
                # A terminal line followed by a failing exit: the CLI
                # contradicts its own documented success contract.
                return self._failed_result(
                    started, f"zcode_run_failed_exit_{exit_status}", ""
                )
            # The answer arrived but the exit status could not be proven
            # within the bounded wait: ambiguity, never a fabricated
            # completion.
            return AdapterResult(
                status="failed",
                calls=(
                    CallObservation(
                        call_index=0,
                        started_at=started,
                        ended_at=_canonical_now(),
                        status="unknown",
                        note=(
                            "the zcode run answered but its exit status "
                            + "could not be proven; the outcome is unknown "
                            + "and nothing was retried"
                        ),
                    ),
                ),
            )
        if exit_status == 0:
            # Clean exit without the documented terminal line: the run
            # happened (tokens may have been spent) but the answer is not
            # recoverable — ambiguity, never a fabricated completion.
            return AdapterResult(
                status="failed",
                calls=(
                    CallObservation(
                        call_index=0,
                        started_at=started,
                        ended_at=_canonical_now(),
                        status="unknown",
                        note=(
                            "the zcode run exited cleanly without the "
                            + "documented result line; the outcome is "
                            + "unknown and nothing was retried"
                        ),
                    ),
                ),
            )
        if exit_status is not None:
            # The documented failure shape (exit 1 + bounded stderr) and
            # every other failing exit; stderr content is never read.
            return self._failed_result(
                started, f"zcode_run_failed_exit_{exit_status}", ""
            )
        raise ZCodeProcessLost(after_start=True)

    # ── Helpers ────────────────────────────────────────────────────────

    def _discover(self) -> ZCodeBinary:
        binary, discovery_reason = discover_zcode_binary(
            pinned_binary=self._pinned_binary,
            path_lookup=self._path_lookup,
        )
        if binary is None:
            raise ZCodeIneligible(discovery_reason or "source_unavailable")
        return binary

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
                        "the zcode process was lost during execution; the "
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
        """One safe eligibility observation for the lane; probes gate it.

        Source mode: one snapshot for the plan lane, health derived from
        the source's own observation state; before the first discovery
        this is honestly empty (the source reports nothing it has not
        observed). No quota telemetry is observed here and nothing about
        a successful probe proves promotional eligibility (D-042).
        """
        # The snapshot's own timestamp is the observation's recorded
        # instant (inventory.observed_at), never the caller's clock.
        _ = observed_at
        with self._inventory_lock:
            inventory = self._inventory
        if inventory is None:
            return ()
        # D-063 (remediation): the lane's OBSERVED health is what the
        # bounded probes establish. A healthy CLI + intact authorized
        # workspace reports "ok" — the same probe-health semantics as the
        # loopback adapter — with the `telemetry_unknown` diagnostic kept
        # so the un-establishable sign-in state survives to the UI (it is
        # never upgraded to "authenticated" anywhere). An actual sign-in
        # failure surfaces as a typed execution failure, and any probe
        # failure reports the lane honestly unavailable.
        status_by_auth: dict[str, tuple[str, str]] = {
            "unverified": ("ok", "telemetry_unknown"),
            "unavailable": ("unavailable", "source_unavailable"),
        }
        if inventory.auth_state in status_by_auth:
            status, code = status_by_auth[inventory.auth_state]
        else:
            # Defensive: the closed vocabulary gains a state only through
            # an explicit contract change; report it honestly as unknown.
            status, code = "unknown", "telemetry_unknown"
        if validate_workspace(self._workspace) is not None:
            status, code = "unavailable", "source_unavailable"
        identity = ResourceIdentity(
            resource_id=self.lane_resource_id,
            channel="worker_bridged",
            provider=ZCODE_PROVIDER,
            model=PLAN_LANE_SLUG,
            entitlement="subscription_included",
        )
        snapshot = CapacitySnapshot(
            schema_version=SCHEMA_VERSION,
            provider=ZCODE_PROVIDER,
            source=f"worker-local:{self.adapter_id}",
            retrieved_at=inventory.observed_at,
            status=status,  # type: ignore[arg-type]
            windows=(),
            diagnostics=(CapacityDiagnostic(code=code),),
        )
        return (
            resource_snapshot_from_capacity(
                snapshot,
                identity=identity,
                quota_observation_class="unknown",
            ),
        )


# ── Module-level helpers ──────────────────────────────────────────────────────


def _bounded_exit(proc: ChildProcess) -> int | None:
    """Bounded wait for a self-exited child's exit status.

    After the stream ended the child normally exits within milliseconds;
    the bounded wait proves the exit (returncode) or gives up (``None``),
    which keeps the outcome honestly ambiguous instead of guessed.
    """
    try:
        return proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return None


__all__ = [
    "DOCTOR_PROBE_TIMEOUT_SECONDS",
    "MAX_PROMPT_BYTES",
    "MAX_RESPONSE_CHARS",
    "MIN_SUPPORTED_ZCODE_VERSION",
    "PLAN_LANE_SLUG",
    "SAFE_PERMISSION_MODE",
    "VERSION_PROBE_TIMEOUT_SECONDS",
    "ZCODE_ADAPTER_ID",
    "ZCODE_PROVIDER",
    "ZCodeBinary",
    "ZCodeIneligible",
    "ZCodeLocalAdapter",
    "ZCodeProcessLost",
    "ZCodeProtocolFailure",
    "ZCodeRunResult",
    "validate_workspace",
    "build_run_argv",
    "discover_zcode_binary",
    "map_prompt",
    "parse_run_result",
    "probe_zcode_doctor",
    "probe_zcode_version",
    "source_resource_id",
]
