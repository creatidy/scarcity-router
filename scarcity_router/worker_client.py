"""The native Scarcity Router worker: outbound transport runtime (M05).

One worker process bridges localhost-only resources to the server over a
SINGLE outbound, worker-initiated TLS connection (D-041/D-043/D-044). The
runtime:

- **originates the connection** — no inbound listening port exists in
  normal operation, so no firewall rule, no manual worker-IP
  configuration and no port forwarding is ever needed (asserted by
  tests: the worker never binds or listens);
- **verifies the server** — TLS contexts come from
  ``ssl.create_default_context()`` (certificate AND hostname
  verification). There is no ``verify=false`` option anywhere in this
  program; plaintext is only ever used for explicit loopback origins
  (the bounded localhost exception) and is refused for any other host;
- **pairs** by redeeming a short-lived one-time code (administrator
  initiated) and stores the issued per-device credential in the worker's
  bounded local store (``worker_local_store``);
- **authenticates every connection** with that per-device credential;
  a revoked or invalid identity is rejected by the server and the
  runtime stops (fail closed, no retry storm) instead of retrying;
- **negotiates the protocol version** and stops cleanly on
  incompatibility;
- **executes only allowlisted local adapters** (``worker_local_adapters``),
  streaming normalized chunks back, propagating cancellation, and
  reporting usage honestly through the terminal result;
- **never duplicates an already-started execution**: on connection loss
  an in-flight attempt is cancelled best-effort locally, never
  restarted, and reported to the server as ``attempt_interrupted`` on
  reconnect so the server resolves it as ambiguous state (D-043);
- **reconnects with bounded backoff** (capped exponential delay, bounded
  attempt count) and reports redacted diagnostics only — no secrets, no
  prompts, no provider payloads in any log line;
- **performs no routing decisions** — it executes what the server's
  coordinator dispatches, or refuses it.

Entry points: ``python -m scarcity_router.worker_client pair`` (redeem a
one-time code), ``... run`` (connect and serve) and ``... service``
(install/status/restart/uninstall of the systemd user service, the
normal Linux/WSL deployment — ``run`` is the foreground debugging form;
issue #138). Windows autostart/service mechanics are M10 scope.
"""

from __future__ import annotations

import argparse
import signal
import socket
import ssl
import sys
import threading
import time
import types
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .errors import CapacityValidationError
from .gateway_adapters import AdapterResult, AdapterStreamChunk
from .gateway_validation import v_safe_id, v_text
from .model_inventory import ModelInventoryReport, SourceInventory
from .resource_state import ResourceStateSnapshot, WorkerStateReport

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from .gateway_adapters import AdapterMessage
from .worker_local_adapters import (
    AdapterNotAllowedError,
    LocalAdapterRegistry,
    ToolBridgeCancelled,
    ToolBridgeUnavailable,
    run_allowlisted,
)
from .worker_local_store import (
    WorkerLocalIdentity,
    WorkerLocalStore,
    WorkerStateDirLock,
    WorkerStateDirLockUnavailable,
    WorkerStateDirLocked,
    default_worker_state_dir,
)
from .worker_protocol import (
    AttemptInterruptedMessage,
    CancelMessage,
    CredentialRotatedMessage,
    ErrorMessage,
    ExecuteMessage,
    ExecuteResultMessage,
    ExecuteToolCallMessage,
    ExecuteToolResultMessage,
    FrameReader,
    FrameTransport,
    FrameWriter,
    HelloAckMessage,
    HelloMessage,
    HeartbeatAckMessage,
    HeartbeatMessage,
    PairRequestMessage,
    PairResultMessage,
    SocketTransport,
    StateReportAckMessage,
    StateReportMessage,
    WORKER_PROTOCOL_VERSION,
    WorkerProtocolError,
    chunk_to_dict,
    encode_frame,
    message_to_dict,
    parse_server_message,
)

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_PORT = 8790
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15
DEFAULT_RECONNECT_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_RECONNECT_MAX_DELAY_SECONDS = 60.0
DEFAULT_MAX_RECONNECT_ATTEMPTS = 10
DEFAULT_MAX_CONCURRENT_ADAPTERS = 4

#: The signal-handler shape ``install_stop_signal_handlers`` restores.
SignalHandler = Callable[[int, "types.FrameType | None"], object]

_loopback_hosts: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


class WorkerConfigError(Exception):
    """A worker configuration problem (safe message, no secrets)."""


@dataclass(frozen=True)
class WorkerOrigin:
    """The parsed server origin (credentials never live in URLs)."""

    host: str
    port: int
    tls: bool

    @classmethod
    def parse(cls, text: str) -> "WorkerOrigin":
        _ = v_text(text, "server_origin", max_len=512)
        if "@" in text:
            raise WorkerConfigError(
                "server origin must not contain credentials; only scheme, "
                + "host and port are accepted"
            )
        for scheme in ("srws://", "srw://"):
            if text.startswith(scheme):
                body = text[len(scheme) :]
                host, _, raw_port = body.partition(":")
                if not host:
                    raise WorkerConfigError("server origin must include a host")
                port = int(raw_port) if raw_port else DEFAULT_PORT
                if not 1 <= port <= 65535:
                    raise WorkerConfigError("server origin port out of range")
                origin = cls(host=host.lower(), port=port, tls=scheme == "srws://")
                if not origin.tls and origin.host not in _loopback_hosts:
                    raise WorkerConfigError(
                        "plaintext origins are only permitted for loopback "
                        + "hosts; use srws:// (verified TLS) for every other "
                        + "server"
                    )
                return origin
        raise WorkerConfigError(
            "server origin must start with srws:// (verified TLS) or "
            + "srw:// (loopback plaintext only)"
        )


@dataclass(frozen=True)
class ReconnectPolicy:
    """Bounded reconnect behavior (capped exponential backoff)."""

    initial_delay_seconds: float = DEFAULT_RECONNECT_INITIAL_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_RECONNECT_MAX_DELAY_SECONDS
    max_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS

    def delay_for(self, attempt_index: int) -> float:
        """The delay before reconnect number ``attempt_index`` (0-based)."""
        delay: float = self.initial_delay_seconds * (2.0**attempt_index)
        return min(delay, self.max_delay_seconds)


@dataclass
class Diagnostic:
    """One redacted, bounded worker diagnostic line."""

    at: str
    level: str
    message: str

    def render(self) -> str:
        return f"{self.at} {self.level} {self.message}"


MAX_DIAGNOSTICS = 256


def _canonical_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _utcnow() -> datetime:
    """The default injectable wall clock (timezone-aware)."""
    return datetime.now(timezone.utc)


# ── Connection factory seam ───────────────────────────────────────────────────

ConnectFactory = Callable[[WorkerOrigin], FrameTransport]


def tls_context_for_worker() -> ssl.SSLContext:
    """The ONLY TLS client context this program creates.

    Verified certificates and hostname checking; no verification bypass
    option exists (D-044).
    """
    return ssl.create_default_context()


#: The session socket's I/O ceiling once the connection is established.
#: The protocol keeps the link alive (worker heartbeats at the
#: server-advertised cadence, server acks; the server's liveness monitor
#: closes silent workers at three intervals), so this ceiling sits far
#: above the heartbeat cadence. It must NEVER be the short connect
#: timeout: the worker read loop treats a socket timeout as connection
#: loss and reconnects, so a short ceiling tears down healthy idle
#: sessions (observed as mid-execution drops in the M10 acceptance suite).
SESSION_IO_TIMEOUT_SECONDS = 60.0


def default_connect_factory(origin: WorkerOrigin) -> FrameTransport:
    """TCP (+ TLS when the origin requires it) transport for one origin.

    The connect phase is bounded by ``create_connection``'s 10 s timeout;
    the established session socket then gets the long protocol-sane I/O
    ceiling (:data:`SESSION_IO_TIMEOUT_SECONDS`) — see its documentation.
    """
    raw = socket.create_connection((origin.host, origin.port), timeout=10.0)
    try:
        if origin.tls:
            raw = tls_context_for_worker().wrap_socket(
                raw, server_hostname=origin.host
            )
        _ = raw.settimeout(SESSION_IO_TIMEOUT_SECONDS)
        return SocketTransport(raw)
    except BaseException:
        try:
            raw.close()
        except OSError:
            pass
        raise


# ── The runtime ───────────────────────────────────────────────────────────────

STOP_NONE = "none"
STOP_REQUESTED = "requested"
STOP_FATAL = "fatal"
STOP_EXHAUSTED = "reconnect_budget_exhausted"

# Session-loop outcomes consumed by the run loop.
SESSION_RETRY = "retry"
SESSION_STOP = "stop"


class _WorkerAttempt:
    """One in-flight local execution tracked by the runtime.

    ``tool_call_id``/``tool_result``/``tool_result_event`` carry the
    D-062 continuation slot: while the adapter thread is blocked inside a
    client-tool suspension, the session's read loop deposits exactly one
    harness tool result here (matched to the suspended ``call_id``);
    the blocked ``suspend`` call consumes it at most once.
    """

    def __init__(self, attempt_id: str) -> None:
        self.attempt_id: str = attempt_id
        self.cancel_event: threading.Event = threading.Event()
        self.thread: threading.Thread | None = None
        self.tool_call_id: str | None = None
        self.tool_result: tuple[str, str] | None = None
        self.tool_result_event: threading.Event = threading.Event()
        self.tool_bridge: "_SessionToolBridge | None" = None


class _SessionToolBridge:
    """The per-attempt D-062 continuation channel (protocol version 3).

    The adapter calls :meth:`suspend` from its execution thread; the
    method publishes the suspension to the server (one bounded
    ``execute_tool_call`` frame) and blocks until the harness's result
    for THAT call arrives, the admission deadline or a cancellation fires,
    or the session dies. Exactly one suspension may be pending per
    attempt: a second concurrent call is a typed refusal — never a
    queued second consumer.
    """

    def __init__(
        self,
        session: "_ActiveSession",
        attempt: _WorkerAttempt,
    ) -> None:
        self._session: _ActiveSession = session
        self._attempt: _WorkerAttempt = attempt
        self._suspended: bool = False

    def suspend(
        self,
        call_id: str,
        name: str,
        arguments: str,
        content: str | None,
    ) -> str:
        if self._suspended:
            # Adapter-state-machine defect: one pending client tool call
            # per attempt at a time, by construction.
            raise ToolBridgeUnavailable("a tool call is already pending")
        self._suspended = True
        self._attempt.tool_call_id = call_id
        self._attempt.tool_result = None
        self._attempt.tool_result_event.clear()
        self._session.send_execute_tool_call(
            self._attempt.attempt_id, call_id, name, arguments, content
        )
        while True:
            if self._attempt.tool_result_event.wait(timeout=0.1):
                delivered = self._attempt.tool_result
                if delivered is not None and delivered[0] == call_id:
                    # Re-arm for a further sequential round on the SAME
                    # turn (each round is its own single suspension).
                    self._suspended = False
                    self._attempt.tool_call_id = None
                    return delivered[1]
                # A result for a different call id cannot continue this
                # suspension (exact identity, D-062); keep waiting for
                # the real one — the deadline/cancel paths end the wait.
                continue
            if self._attempt.cancel_event.is_set():
                raise ToolBridgeCancelled("the attempt was cancelled or deadlined")
            if self._session.stopped:
                raise ToolBridgeUnavailable("the session ended")

    def deliver(self, call_id: str, content: str) -> bool:
        """Deposit the harness result (read-loop side); ``False`` when no
        matching suspension is pending."""
        if not self._suspended or self._attempt.tool_call_id != call_id:
            return False
        self._attempt.tool_result = (call_id, content)
        self._attempt.tool_result_event.set()
        return True


class WorkerRuntime:
    """Owns the outbound connection lifecycle of one paired worker.

    Everything network-facing is injectable for deterministic tests: the
    connect factory, clocks and the sleep function. The runtime holds no
    routing knowledge and makes no routing decisions.
    """

    def __init__(
        self,
        *,
        origin: WorkerOrigin,
        store: WorkerLocalStore,
        local_adapters: LocalAdapterRegistry | None = None,
        device_label: str | None = None,
        heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        state_report_interval_seconds: int = 60,
        reconnect: ReconnectPolicy | None = None,
        connect_factory: ConnectFactory = default_connect_factory,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
        max_concurrent_adapters: int = DEFAULT_MAX_CONCURRENT_ADAPTERS,
    ) -> None:
        self._origin: WorkerOrigin = origin
        self._store: WorkerLocalStore = store
        self._local_adapters: LocalAdapterRegistry = (
            local_adapters if local_adapters is not None else LocalAdapterRegistry()
        )
        self._device_label: str | None = (
            v_safe_id(device_label, "device_label") if device_label else None
        )
        self._heartbeat_interval: int = heartbeat_interval_seconds
        self._state_report_interval: int = state_report_interval_seconds
        self._reconnect: ReconnectPolicy = reconnect if reconnect is not None else ReconnectPolicy()
        self._connect_factory: ConnectFactory = connect_factory
        self._monotonic: Callable[[], float] = monotonic
        self._sleep: Callable[[float], None] = sleep
        self._clock: Callable[[], datetime] = (
            clock if clock is not None else _utcnow
        )
        self._max_concurrent_adapters: int = max_concurrent_adapters
        self._diagnostics: deque[Diagnostic] = deque(maxlen=MAX_DIAGNOSTICS)
        self._interrupted: deque[str] = deque(maxlen=1024)
        self._stop_event: threading.Event = threading.Event()
        self._stop_reason: str = STOP_NONE
        self._session_lock: threading.Lock = threading.Lock()
        self._active_sender: "_FrameSender | None" = None

    # ── Public surface ───────────────────────────────────────────────

    @property
    def store(self) -> WorkerLocalStore:
        """The worker's bounded local state store (session seam)."""
        return self._store

    @property
    def local_adapters(self) -> LocalAdapterRegistry:
        """The configured local adapter allowlist (session seam)."""
        return self._local_adapters

    @property
    def stop_requested(self) -> bool:
        """Whether a stop was requested (session seam)."""
        return self._stop_event.is_set()

    @property
    def heartbeat_interval(self) -> int:
        """The heartbeat interval in seconds (session seam)."""
        return self._heartbeat_interval

    @property
    def state_report_interval(self) -> int:
        """The state-report interval in seconds (session seam)."""
        return self._state_report_interval

    @property
    def max_concurrent_adapters(self) -> int:
        """The local concurrent-execution bound (session seam)."""
        return self._max_concurrent_adapters

    def note(self, level: str, message: str) -> None:
        """Record one redacted diagnostic (session seam)."""
        self._diagnostics.append(Diagnostic(at=_canonical_now(), level=level, message=message))

    def record_interrupted(self, attempt_id: str) -> None:
        """Queue one attempt for the reconnect-time interrupted report."""
        self._interrupted.append(attempt_id)

    def monotonic_now(self) -> float:
        """The injected monotonic clock (session seam)."""
        return self._monotonic()

    def current_time(self) -> datetime:
        """The injected wall clock (session seam)."""
        return self._clock()

    def diagnostics(self) -> tuple[str, ...]:
        """Redacted diagnostics (no credentials, no prompts, no payloads)."""
        return tuple(line.render() for line in self._diagnostics)

    def request_stop(self) -> None:
        """Ask the run loop to stop at the next boundary.

        Stop ownership is deterministic, never timing-based: the active
        session's transport is closed here so a read blocked in
        ``recv`` unwinds immediately (EOF/connection error), instead of
        waiting on a socket timeout to happen to break the block.
        """
        self._stop_event.set()
        self._stop_reason = STOP_REQUESTED
        with self._session_lock:
            sender = self._active_sender
        if sender is not None:
            sender.close()

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    def pair(self, pairing_code: str) -> WorkerLocalIdentity:
        """One-shot pairing: redeem the code and store the identity."""
        _ = v_text(pairing_code, "pairing_code", max_len=512)
        transport = self._connect_factory(self._origin)
        try:
            writer = FrameWriter(transport)
            writer.write_message(
                PairRequestMessage(
                    pairing_code=pairing_code,
                    supported_versions=(WORKER_PROTOCOL_VERSION,),
                    device_label=self._device_label,
                ).to_payload()
            )
            reader = FrameReader(transport)
            payload = reader.read_message()
            if payload is None:
                raise WorkerRuntimeError(
                    "the server closed the connection during pairing"
                )
            message = parse_server_message(payload)
            if isinstance(message, ErrorMessage):
                raise WorkerRuntimeError(f"pairing rejected: {message.code}")
            if not isinstance(message, PairResultMessage):
                raise WorkerRuntimeError(
                    "the server answered the pairing request unexpectedly"
                )
            identity = WorkerLocalIdentity(
                worker_id=message.worker_id,
                credential=message.credential,
                server_origin=self._origin_key(),
                device_label=self._device_label,
            )
            self._store.save_identity(identity)
            self._note("info", "pairing completed")
            return identity
        finally:
            transport.close()

    def run(self) -> str:
        """Connect/reconnect loop; returns the terminal stop reason."""
        identity = self._store.load_identity()
        if identity is None:
            self._note("error", "this worker is not paired yet; run pair first")
            self._stop_reason = STOP_FATAL
            return self._stop_reason
        attempt_index = 0
        while not self._stop_event.is_set():
            try:
                transport = self._connect_factory(self._origin)
            except (OSError, ssl.SSLError) as exc:
                self._note("warn", f"connection attempt failed: {type(exc).__name__}")
                if not self._backoff_or_stop(attempt_index):
                    return self._stop_reason
                attempt_index += 1
                continue
            try:
                reason = self._run_session(transport, identity)
            finally:
                transport.close()
            if reason == "stop":
                self._stop_reason = STOP_REQUESTED
                return self._stop_reason
            if reason in ("auth_failed", "revoked", "version"):
                self._stop_reason = STOP_FATAL
                return self._stop_reason
            # reason == "retry": a connection-level failure. Bounded
            # backoff, then another attempt -- never a duplicate dispatch.
            if not self._backoff_or_stop(attempt_index):
                return self._stop_reason
            attempt_index += 1
        self._stop_reason = STOP_REQUESTED
        return self._stop_reason

    # ── Internals ────────────────────────────────────────────────────

    def _origin_key(self) -> str:
        scheme = "srws" if self._origin.tls else "srw"
        return f"{scheme}://{self._origin.host}:{self._origin.port}"

    def _backoff_or_stop(self, attempt_index: int) -> bool:
        if attempt_index + 1 >= self._reconnect.max_attempts:
            self._note("error", "reconnect budget exhausted; worker stopping")
            self._stop_reason = STOP_EXHAUSTED
            return False
        delay = self._reconnect.delay_for(attempt_index)
        self._note("info", f"reconnecting in {delay:.1f}s")
        # Sleep in small slices so request_stop is honored promptly.
        deadline = self._monotonic() + delay
        while self._monotonic() < deadline and not self._stop_event.is_set():
            self._sleep(min(0.05, max(0.0, deadline - self._monotonic())))
        if self._stop_event.is_set():
            self._stop_reason = STOP_REQUESTED
            return False
        return True

    def _run_session(self, transport: FrameTransport, identity: WorkerLocalIdentity) -> str:
        """One connection's session; returns why it ended."""
        sender = _FrameSender(transport)
        reader = FrameReader(transport)
        # Registered so request_stop can close the transport and unblock a
        # read parked in recv (deterministic stop, never timeout-based).
        with self._session_lock:
            self._active_sender = sender
        try:
            result = self._session_body(sender, reader, identity)
        finally:
            with self._session_lock:
                self._active_sender = None
        return result

    def _session_body(
        self,
        sender: "_FrameSender",
        reader: FrameReader,
        identity: WorkerLocalIdentity,
    ) -> str:
        try:
            sender.send(
                HelloMessage(
                    worker_id=identity.worker_id,
                    credential=identity.credential,
                    supported_versions=(WORKER_PROTOCOL_VERSION,),
                    device_label=self._device_label,
                ).to_payload()
            )
            payload = reader.read_message()
            if payload is None:
                self._note("warn", "server closed the connection during handshake")
                return "stop" if self.stop_requested else "retry"
            ack = parse_server_message(payload)
            if isinstance(ack, ErrorMessage):
                self._note("error", f"rejected at handshake: {ack.code}")
                if ack.code == "credential_revoked":
                    return "revoked"
                if ack.code == "unauthorized":
                    return "auth_failed"
                if ack.code == "protocol_version_unsupported":
                    return "version"
                return "stop" if self.stop_requested else "retry"
            if not isinstance(ack, HelloAckMessage):
                self._note("error", "unexpected handshake answer")
                return "retry"
            self._note("info", f"session accepted at protocol v{ack.negotiated_version}")
        except (WorkerProtocolError, ConnectionError, TimeoutError, OSError) as exc:
            self._note("warn", f"handshake failed: {type(exc).__name__}")
            return "retry"

        # Report any attempts interrupted by the previous connection loss
        # BEFORE anything else: the server must learn about lost work
        # first, so it can resolve those attempts as ambiguous (D-043).
        while self._interrupted:
            interrupted_id = self._interrupted.popleft()
            try:
                sender.send(
                    AttemptInterruptedMessage(attempt_id=interrupted_id).to_payload()
                )
            except (ConnectionError, OSError):
                return "retry"

        # The worker heartbeats at the server-advertised cadence so the
        # server's liveness grace window always dominates.
        self._heartbeat_interval = ack.heartbeat_interval_seconds
        session = _ActiveSession(
            runtime=self,
            sender=sender,
            reader=reader,
            identity=identity,
            negotiated_version=ack.negotiated_version,
        )
        return session.run()


    def _note(self, level: str, message: str) -> None:
        self._diagnostics.append(Diagnostic(at=_canonical_now(), level=level, message=message))


class WorkerRuntimeError(Exception):
    """A fatal worker runtime failure (safe message only)."""


class _FrameSender:
    """Thread-safe frame writer (chunks stream from adapter threads)."""

    def __init__(self, transport: FrameTransport) -> None:
        self._transport: FrameTransport = transport
        self._lock: threading.Lock = threading.Lock()

    def send(self, payload: Mapping[str, object]) -> None:
        with self._lock:
            self._transport.send_all(encode_frame(payload))

    def close(self) -> None:
        with self._lock:
            self._transport.close()


def ensure_reasoning_representable(
    negotiated_version: int, message: "AdapterMessage"
) -> None:
    """The D-064 no-silent-loss negotiation gate.

    The `reasoning` member is version-4-only. On a session that
    negotiated below 4 a reasoning-bearing result cannot be represented —
    fail the attempt closed instead of silently dropping the reasoning
    (negotiation falling back to v3 must never lose it). The failure is
    structural: parameter names only, never reasoning content.
    """
    if negotiated_version < 4 and message.reasoning is not None:
        raise WorkerProtocolError(
            "internal_error",
            "reasoning output cannot be represented on a negotiated "
            + "protocol below 4",
        )


class _ActiveSession:
    """The message loop of one established worker session."""

    def __init__(
        self,
        *,
        runtime: WorkerRuntime,
        sender: _FrameSender,
        reader: FrameReader,
        identity: WorkerLocalIdentity,
        negotiated_version: int,
    ) -> None:
        self._runtime: WorkerRuntime = runtime
        self._sender: _FrameSender = sender
        self._reader: FrameReader = reader
        self._identity: WorkerLocalIdentity = identity
        self._version: int = negotiated_version
        self._attempts: dict[str, _WorkerAttempt] = {}
        self._attempts_lock: threading.Lock = threading.Lock()
        self._heartbeat_seq: int = 0
        self._stop_session: threading.Event = threading.Event()

    # ── Lifecycle ────────────────────────────────────────────────────

    def run(self) -> str:
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="worker-heartbeat", daemon=True
        )
        heartbeat.start()
        self._send_initial_state_report()
        try:
            result = self._read_loop()
        finally:
            self._stop_session.set()
            self._fail_in_flight()
        _ = result
        return "stop" if self._runtime.stop_requested else "retry"

    def _read_loop(self) -> str:
        while not self._stop_session.is_set() and not self._runtime.stop_requested:
            try:
                payload = self._reader.read_message()
            except (WorkerProtocolError, ConnectionError, TimeoutError, OSError) as exc:
                self._runtime.note("warn", f"connection lost: {type(exc).__name__}")
                return "retry"
            except RecursionError:
                # A frame the parser could not traverse (depth bomb): the
                # typed path already covers it; this guard guarantees the
                # session still unwinds cleanly and reconnects.
                self._runtime.note("warn", "connection lost: RecursionError")
                return "retry"
            if payload is None:
                self._runtime.note("info", "server closed the connection")
                return "stop" if self._runtime.stop_requested else "retry"
            try:
                message = parse_server_message(payload)
            except WorkerProtocolError as exc:
                self._runtime.note("error", f"protocol violation by server: {exc.code}")
                return "retry"
            self._dispatch(message)
        return "stop" if self._runtime.stop_requested else "retry"

    def _dispatch(self, message: object) -> None:
        if isinstance(message, (HeartbeatAckMessage, HelloAckMessage, StateReportAckMessage)):
            return
        if isinstance(message, CancelMessage):
            with self._attempts_lock:
                attempt = self._attempts.get(message.attempt_id)
            if attempt is not None:
                attempt.cancel_event.set()
            return
        if isinstance(message, ExecuteMessage):
            self._start_execution(message)
            return
        if isinstance(message, ExecuteToolResultMessage):
            self._deliver_tool_result(message)
            return
        if isinstance(message, CredentialRotatedMessage):
            rotated = WorkerLocalIdentity(
                worker_id=self._identity.worker_id,
                credential=message.credential,
                server_origin=self._identity.server_origin,
                device_label=self._identity.device_label,
            )
            self._runtime.store.save_identity(rotated)
            self._identity = rotated
            self._runtime.note("info", "credential rotated")
            return
        if isinstance(message, StateReportMessage):
            return
        if isinstance(message, ErrorMessage):
            if message.fatal:
                self._runtime.note("error", f"fatal server error: {message.code}")
                self._stop_session.set()
                return
            _ = message
            self._runtime.note("warn", f"server reported: {message.code}")
            return
        self._runtime.note("warn", "ignoring unexpected server message")

    def _deliver_tool_result(self, message: ExecuteToolResultMessage) -> None:
        """Route one v3 harness tool result into its pending suspension.

        Version-gated like every v3 frame: a v1/v2 session must never
        receive it. A result for an attempt with no matching pending
        suspension is a non-fatal protocol answer (the server already
        failed the continuation closed) — never attributed elsewhere.
        """
        if self._version < 3:
            self._runtime.note(
                "error", "protocol violation by server: v3 frame on v2 session"
            )
            self._stop_session.set()
            return
        with self._attempts_lock:
            attempt = self._attempts.get(message.attempt_id)
        if attempt is None or attempt.tool_bridge is None:
            self._runtime.note(
                "warn", "tool result for an attempt without a pending suspension"
            )
            return
        _ = attempt.tool_bridge.deliver(message.call_id, message.content)

    # ── Execution ────────────────────────────────────────────────────

    def _start_execution(self, message: ExecuteMessage) -> None:
        with self._attempts_lock:
            if len(self._attempts) >= self._runtime.max_concurrent_adapters:
                self._send_result(
                    ExecuteResultMessage(
                        attempt_id=message.attempt_id,
                        status="failed",
                        calls=(),
                        note="the worker is at its concurrent-execution bound",
                    )
                )
                return
            if message.attempt_id in self._attempts:
                # Never overwrite a live tracker, and never ANSWER the
                # duplicate: a result keyed by this attempt id would be
                # misattributed to the first execution's tracker on the
                # server. Refuse silently -- the duplicate dispatch stays
                # bounded by the coordinator's own deadline, and the
                # first execution's cancellation and result routing stay
                # intact.
                self._runtime.note(
                    "warn", "refused a duplicate in-flight attempt id"
                )
                return
            attempt = _WorkerAttempt(message.attempt_id)
            self._attempts[message.attempt_id] = attempt
        if self._version >= 3:
            # D-062: the continuation channel exists only on protocol
            # version 3 sessions; on older sessions the adapter sees no
            # channel and fails tool-bearing calls closed.
            attempt.tool_bridge = _SessionToolBridge(self, attempt)
        thread = threading.Thread(
            target=self._execute_task,
            args=(message, attempt),
            name=f"worker-execute-{message.attempt_id}",
            daemon=True,
        )
        attempt.thread = thread
        thread.start()

    def _execute_task(self, message: ExecuteMessage, attempt: _WorkerAttempt) -> None:
        deadline_timer: threading.Timer | None = None
        try:
            # Fail closed on an unparseable deadline: the worker never
            # runs a local execution without an enforceable bound.
            remaining = self._deadline_seconds(message.deadline)
            if remaining is None:
                self._send_result(
                    ExecuteResultMessage(
                        attempt_id=message.attempt_id,
                        status="failed",
                        calls=(),
                        note="the execute deadline was malformed",
                    )
                )
                return
            if remaining <= 0:
                attempt.cancel_event.set()
            else:
                deadline_timer = threading.Timer(remaining, attempt.cancel_event.set)
                deadline_timer.daemon = True
                deadline_timer.start()
            result = run_allowlisted(
                self._runtime.local_adapters,
                adapter_id=message.adapter_id,
                call=message.call,
                cancel_event=attempt.cancel_event,
                deadline=message.deadline,
                emit=self._make_emitter(message.attempt_id),
                tool_bridge=attempt.tool_bridge,
            )
        except AdapterNotAllowedError:
            # The allowlist holds even against this server: never invoke,
            # report the typed rejection, nothing else.
            self._send_result(
                ExecuteResultMessage(
                    attempt_id=message.attempt_id,
                    status="failed",
                    calls=(),
                    note="adapter_not_allowed",
                )
            )
            return
        except WorkerProtocolError as exc:
            self._send_result(
                ExecuteResultMessage(
                    attempt_id=message.attempt_id,
                    status="failed",
                    calls=(),
                    note=exc.message[:200],
                )
            )
            return
        except (ConnectionError, OSError):
            # The connection died mid-execution: nothing further can be
            # delivered, so this attempt is reported interrupted on
            # reconnect (never silently restarted, D-043).
            self._runtime.record_interrupted(message.attempt_id)
            return
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            with self._attempts_lock:
                _ = self._attempts.pop(message.attempt_id, None)
        try:
            result_message = self._result_message(message.attempt_id, result)
        except WorkerProtocolError as exc:
            # The typed result-representation failure (e.g. reasoning on a
            # below-v4 negotiation, D-064) fails the attempt closed with
            # the structural note — never a silently truncated result.
            self._send_result(
                ExecuteResultMessage(
                    attempt_id=message.attempt_id,
                    status="failed",
                    calls=(),
                    note=exc.message[:200],
                )
            )
            return
        self._send_result(result_message)

    def _deadline_seconds(self, deadline: str) -> float | None:
        """Remaining seconds to the admission deadline, or ``None`` when
        the deadline string is malformed (the caller fails closed)."""
        try:
            parsed = datetime.fromisoformat(deadline[:-1] + "+00:00")
        except (ValueError, IndexError, TypeError):
            return None
        return (parsed - self._runtime.current_time()).total_seconds()

    def _make_emitter(self, attempt_id: str) -> Callable[[AdapterStreamChunk], None]:
        def emit(chunk: AdapterStreamChunk) -> None:
            self._sender.send(
                {
                    "type": "execute_chunk",
                    "attempt_id": attempt_id,
                    "chunk": chunk_to_dict(chunk),
                }
            )

        return emit

    @property
    def stopped(self) -> bool:
        return self._stop_session.is_set() or self._runtime.stop_requested

    def send_execute_tool_call(
        self,
        attempt_id: str,
        call_id: str,
        name: str,
        arguments: str,
        content: str | None,
    ) -> None:
        """Publish one client-tool suspension to the server (v3).

        A failed send means the server never learned of the suspension;
        the attempt's fate is the familiar interrupted-report path, and
        the blocked adapter is woken through the session teardown.
        """
        message = ExecuteToolCallMessage(
            attempt_id=attempt_id,
            call_id=call_id,
            name=name,
            arguments=arguments,
            content=content,
        )
        try:
            self._sender.send(message.to_payload())
        except (ConnectionError, OSError, WorkerProtocolError):
            self._stop_session.set()
            raise ToolBridgeUnavailable("the session ended") from None

    def _result_message(self, attempt_id: str, result: AdapterResult) -> ExecuteResultMessage:
        from .worker_protocol import usage_to_dict

        calls: list[dict[str, object]] = []
        for observation in result.calls:
            call_doc: dict[str, object] = {
                "call_index": observation.call_index,
                "started_at": observation.started_at,
                "ended_at": observation.ended_at,
                "status": observation.status,
            }
            if observation.provider_reported_usage is not None:
                call_doc["provider_reported_usage"] = usage_to_dict(
                    observation.provider_reported_usage
                )
            if observation.estimated_usage is not None:
                call_doc["estimated_usage"] = usage_to_dict(observation.estimated_usage)
            if observation.note is not None:
                call_doc["note"] = observation.note
            calls.append(call_doc)
        message: Mapping[str, object] | None = None
        if result.message is not None:
            ensure_reasoning_representable(self._version, result.message)
            message = message_to_dict(result.message)
        return ExecuteResultMessage(
            attempt_id=attempt_id,
            status=result.status,
            calls=tuple(calls),
            message=message,
            finish_reason=result.finish_reason,
        )

    def _send_result(self, result: ExecuteResultMessage) -> None:
        try:
            self._sender.send(result.to_payload())
        except (ConnectionError, OSError, WorkerProtocolError):
            # The result could not be delivered: the attempt's fate on the
            # server is unknown, so report it interrupted on reconnect.
            self._record_interrupted(result.attempt_id)

    def _record_interrupted(self, attempt_id: str) -> None:
        self._runtime.record_interrupted(attempt_id)

    # ── Heartbeat and state reporting ─────────────────────────────────

    def _heartbeat_loop(self) -> None:
        next_heartbeat = 0.0
        next_report = 0.0
        while not self._stop_session.is_set():
            now = self._runtime.monotonic_now()
            if now >= next_heartbeat:
                self._heartbeat_seq += 1
                try:
                    self._sender.send(HeartbeatMessage(seq=self._heartbeat_seq).to_payload())
                except (ConnectionError, OSError, WorkerProtocolError):
                    return
                next_heartbeat = now + self._runtime.heartbeat_interval
            if (
                self._runtime.state_report_interval > 0
                and now >= next_report
            ):
                self._send_state_report()
                next_report = now + self._runtime.state_report_interval
            _ = self._stop_session.wait(0.05)

    def _negotiated_version(self) -> int | None:
        # _ActiveSession holds the negotiated protocol version.
        return getattr(self, "_version", None)

    def _send_initial_state_report(self) -> None:
        self._send_state_report()

    def _send_state_report(self) -> None:
        """Build and send one M01 worker report (the shared path).

        Daybreak blocker 8: source inventories are REFRESHED FIRST, and
        snapshots are collected AFTER the refresh — so one report can
        never carry fresh-healthy snapshots alongside an inventory that
        just reported auth loss. Snapshots and inventory always describe
        the same observation instant.
        """
        # Daybreak blocker 8: the refresh runs BEFORE `now` is captured so
        # the report's reported_at is never earlier than the inventory's
        # observed_at (a future-dated observation fails closed).
        negotiated = self._negotiated_version()
        if negotiated is not None and negotiated >= 2:
            for adapter_id in self._runtime.local_adapters.adapter_ids():
                adapter = self._runtime.local_adapters.resolve(adapter_id)
                refresher = getattr(adapter, "refresh_inventory_if_due", None)
                if refresher is None:
                    continue
                try:
                    refresher()
                except (OSError, ValueError, WorkerProtocolError) as exc:
                    self._runtime.note(
                        "warn",
                        f"inventory refresh failed for {adapter_id}: {type(exc).__name__}",
                    )
        now = _canonical_now()
        snapshots: list[ResourceStateSnapshot] = []
        for adapter_id in self._runtime.local_adapters.adapter_ids():
            adapter = self._runtime.local_adapters.resolve(adapter_id)
            if adapter is None:  # pragma: no cover - registry contract
                continue
            try:
                snapshots.extend(adapter.resource_snapshots(now))
            except (
                OSError,
                ValueError,
                WorkerProtocolError,
                CapacityValidationError,
            ) as exc:
                self._runtime.note(
                    "warn", f"state observation failed for {adapter_id}: {type(exc).__name__}"
                )
        try:
            report = WorkerStateReport(
                schema_version=1,
                worker_id=self._identity.worker_id,
                reported_at=now,
                resources=tuple(sorted(snapshots, key=lambda s: s.identity.resource_id)),
            )
        except (CapacityValidationError, ValueError) as exc:
            self._runtime.note("error", f"state report construction failed: {type(exc).__name__}")
            return
        # D-053: source-mode adapters carry their latest discovery in the
        # version-2 inventory section. A v1 session (an old server) never
        # receives it; discovery failures already isolated inside the
        # adapter's own observation path.
        inventories: list[dict[str, object]] = []
        negotiated = self._negotiated_version()
        if negotiated is not None and negotiated >= 2:
            for adapter_id in self._runtime.local_adapters.adapter_ids():
                adapter = self._runtime.local_adapters.resolve(adapter_id)
                report_source = getattr(adapter, "inventory_report", None)
                if report_source is None:
                    continue
                try:
                    inventory = cast(
                        "SourceInventory | None", report_source()
                    )
                except (OSError, ValueError, WorkerProtocolError) as exc:
                    self._runtime.note(
                        "warn",
                        f"inventory collection failed for {adapter_id}: {type(exc).__name__}",
                    )
                    continue
                if inventory is not None:
                    inventories.append(
                        ModelInventoryReport(
                            worker_id=self._identity.worker_id,
                            sources=(inventory,),
                        ).to_dict()
                    )
        try:
            self._sender.send(
                StateReportMessage(
                    report=report.to_dict(), inventories=tuple(inventories)
                ).to_payload()
            )
        except (ConnectionError, OSError, WorkerProtocolError):
            pass

    def _fail_in_flight(self) -> None:
        """Connection lost: cancel in-flight attempts, mark them interrupted.

        D-043: a network loss NEVER becomes a silent re-execution. The
        attempts are cancelled best-effort locally and reported to the
        server as interrupted on the next connection; a result that
        could not be delivered is likewise reported interrupted (the
        server cannot honestly attribute it).
        """
        with self._attempts_lock:
            attempts = list(self._attempts.values())
            self._attempts.clear()
        for attempt in attempts:
            attempt.cancel_event.set()
            self._runtime.record_interrupted(attempt.attempt_id)


# ── Entry point ───────────────────────────────────────────────────────────────


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    """The adapter-selection flags shared by ``run`` and ``service install``.

    One definition for both surfaces, so the service unit can preserve
    exactly the selection ``run`` accepts (issue #138) — the flags, help
    text and defaults can never drift apart.
    """
    _ = parser.add_argument("--state-dir", default=None, metavar="DIR",
                          help="worker state directory (platform default when omitted)")
    _ = parser.add_argument("--allow-ollama", action="store_true",
                         help="allow the loopback Ollama local adapter")
    _ = parser.add_argument("--resource", default=None, metavar="ID",
                         help="the registry resource id the enabled adapter serves "
                              + "(the Ollama adapter's resource when both are enabled)")
    _ = parser.add_argument("--ollama-host", default="127.0.0.1", metavar="HOST")
    _ = parser.add_argument("--ollama-port", type=int, default=11434, metavar="PORT")
    _ = parser.add_argument("--allow-codex", action="store_true",
                         help="allow the Codex local adapter (official app-server)")
    _ = parser.add_argument("--codex-model", default=None, metavar="SLUG",
                         help="the physical model slug the Codex resource "
                              + "represents (required with --allow-codex; must "
                              + "match a calibrated catalog model, e.g. "
                              + "gpt-5.6-sol)")
    _ = parser.add_argument("--codex-resource", default=None, metavar="ID",
                         help="the registry resource id the Codex adapter serves "
                              + "(required with --resource when both adapters are "
                              + "enabled)")
    _ = parser.add_argument("--codex-source", action="append", default=None,
                              metavar="SOURCE_ID", dest="codex_sources",
                              help="enable a codex execution SOURCE instance "
                                   + "(repeatable; D-053 dynamic discovery "
                                   + "replaces --codex-model/--codex-resource)")
    _ = parser.add_argument("--codex-bin", default=None, metavar="PATH",
                         help="pin the Codex binary path (regular executable "
                              + "file; discovery falls back to PATH and the "
                              + "VS Code extension layout)")
    _ = parser.add_argument("--zcode-source", action="append", default=None,
                              metavar="SOURCE_ID", dest="zcode_sources",
                              help="enable a ZCode execution SOURCE instance "
                                   + "(repeatable; official ZCode CLI plan "
                                   + "lane, D-061. Authentication is the "
                                   + "official 'zcode login zai' — never "
                                   + "wrapped or copied)")
    _ = parser.add_argument("--zcode-bin", default=None, metavar="PATH",
                         help="pin the ZCode binary path (regular executable "
                              + "file; discovery falls back to PATH)")
    _ = parser.add_argument("--zcode-workspace", default=None, metavar="DIR",
                         help="the ONE authorized project workspace the ZCode "
                              + "source executes in (required with "
                              + "--zcode-source; canonicalized and verified; "
                              + "request content can never select a path)")


def build_parser() -> argparse.ArgumentParser:
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    if invoked == "scarcity-router-worker":
        prog = invoked
    elif invoked.startswith("scarcity-worker"):
        # The PyInstaller-packaged executable (issue #113): usage text
        # names the binary the user actually ran, never the Python
        # console script the standalone ZIP does not contain.
        prog = invoked
    else:
        prog = "python -m scarcity_router.worker_client"
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Native Scarcity Router worker: connects OUTBOUND to the server "
            + "over verified TLS, reports local resource state and executes "
            + "allowlisted local adapters. No inbound listening port."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pair = commands.add_parser("pair", help="redeem a one-time pairing code")
    _ = pair.add_argument("--server", required=True, metavar="URL",
                          help="server origin (srws://host:port, or srw:// for loopback)")
    _ = pair.add_argument("--code", required=True, metavar="CODE",
                          help="the one-time pairing code from the administrator")
    _ = pair.add_argument("--label", default=None, metavar="LABEL",
                          help="a short device label (safe id)")
    _ = pair.add_argument("--state-dir", default=None, metavar="DIR",
                          help="worker state directory (platform default when omitted)")
    run = commands.add_parser("run", help="connect and serve (reconnecting; the foreground debugging form of the service)")
    _ = run.add_argument("--server", default=None, metavar="URL",
                         help="server origin (stored value when omitted)")
    _ = run.add_argument("--label", default=None, metavar="LABEL")
    _add_selection_arguments(run)
    login = commands.add_parser(
        "codex-login",
        help="run the OFFICIAL codex login against one source's controlled home",
    )
    _ = login.add_argument("--source", default=None, metavar="SOURCE_ID",
                           help="the execution-source instance to log in "
                                + "(the same id given to run --codex-source); "
                                + "when omitted, the source configured in the "
                                + "installed worker service is discovered "
                                + "(issue #168)")
    _ = login.add_argument("--state-dir", default=None, metavar="DIR")
    _ = login.add_argument("--codex-bin", default=None, metavar="PATH")
    service = commands.add_parser(
        "service",
        help="manage the systemd user service (Linux/WSL; the normal deployment)",
        description=(
            "Install, inspect, restart or remove the systemd USER service "
            + "for this paired worker. Operates on the already-paired "
            + "identity and the stored server origin; no root, no secrets "
            + "in the unit (issue #138). `run` stays the foreground "
            + "debugging form."
        ),
    )
    service_commands = service.add_subparsers(
        dest="service_command", required=True
    )
    service_install = service_commands.add_parser(
        "install",
        help="generate, enable and start the systemd user unit (idempotent)",
        description=(
            "Render the systemd user unit from the installed executable "
            + "and the worker state directory, preserving the adapter "
            + "selection, then daemon-reload, enable and start. Idempotent; "
            + "refuses to touch a unit this tooling did not generate."
        ),
    )
    _add_selection_arguments(service_install)
    _ = service_commands.add_parser(
        "status",
        help="show the service status (plus the linger state)",
    )
    _ = service_commands.add_parser(
        "restart",
        help="restart the service (applies a re-installed configuration)",
    )
    _ = service_commands.add_parser(
        "uninstall",
        help="stop, disable and remove the generated unit (identity and state are kept)",
    )
    return parser


def _resolved_state_dir(state_dir: str | None) -> Path:
    """The worker state directory an explicit flag selects (default otherwise)."""
    return Path(state_dir) if state_dir else Path(default_worker_state_dir())


def _open_store(state_dir: str | None) -> WorkerLocalStore:
    return WorkerLocalStore(_resolved_state_dir(state_dir) / "worker-state.db")


def open_worker_store(state_dir: str | None = None) -> WorkerLocalStore:
    """Open the worker's local state store (the public seam).

    The packaged worker's tray entry point shares this exact opening
    path with the CLI so identity state can never diverge between the
    two surfaces (M10, issue #95).
    """
    return _open_store(state_dir)


def build_local_adapter_registry(
    arguments: Mapping[str, object],
    state_dir: str | None = None,
) -> LocalAdapterRegistry | None:
    """Build the local-adapter registry from ``run``-style flags (seam).

    The public form of :func:`build_registry`, used by the packaged
    worker's tray entry point so flag names and the local-adapter
    allowlist semantics are identical to ``scarcity-router-worker run``
    by construction (M10, issue #95). The state directory resolves from
    the explicit argument first, then the ``state_dir`` flag, so the
    adapters' state (including the Codex controlled home) lands in the
    same directory as the identity store opened through
    :func:`open_worker_store`.
    """
    resolved = state_dir
    if resolved is None:
        candidate = arguments.get("state_dir")
        if isinstance(candidate, str) and candidate:
            resolved = candidate
    return build_registry(arguments, state_dir=resolved)


def build_registry(
    arguments: Mapping[str, object],
    *,
    state_dir: str | None = None,
) -> LocalAdapterRegistry | None:
    allow_ollama = bool(arguments.get("allow_ollama"))
    allow_codex = bool(arguments.get("allow_codex"))
    codex_sources = arguments.get("codex_sources")
    zcode_sources = arguments.get("zcode_sources")
    source_ids: tuple[str, ...] = ()
    if isinstance(codex_sources, list):
        source_ids = tuple(
            item
            for item in cast("list[object]", codex_sources)
            if isinstance(item, str) and item
        )
    zcode_source_ids: tuple[str, ...] = ()
    if isinstance(zcode_sources, list):
        zcode_source_ids = tuple(
            item
            for item in cast("list[object]", zcode_sources)
            if isinstance(item, str) and item
        )
    if not allow_ollama and not allow_codex and not source_ids and not zcode_source_ids:
        return None
    from .resource_state import ResourceIdentity

    registry = LocalAdapterRegistry()
    resource_id = arguments.get("resource")
    if allow_ollama:
        from .worker_local_adapters import LoopbackOllamaAdapter

        if not isinstance(resource_id, str) or not resource_id:
            raise WorkerConfigError("--resource is required with --allow-ollama")
        host = arguments.get("ollama_host")
        port = arguments.get("ollama_port")
        resource = ResourceIdentity(
            resource_id=v_safe_id(resource_id, "resource"),
            channel="worker_bridged",
            provider="ollama",
            model="local",
            entitlement="local_ungated",
        )
        adapter = LoopbackOllamaAdapter(
            resource=resource,
            host=str(host) if isinstance(host, str) else "127.0.0.1",
            port=int(port) if isinstance(port, int) and not isinstance(port, bool) else 11434,
        )
        registry.register(adapter)
    if source_ids:
        from pathlib import Path

        from .worker_codex_adapter import (
            SOURCE_ID_MAX_LENGTH,
            CodexLocalAdapter,
        )

        if allow_codex:
            raise WorkerConfigError(
                "--codex-source and --allow-codex are mutually exclusive: "
                + "source mode discovers its resources; the legacy flags "
                + "configure exactly one"
            )
        if state_dir is None:
            from .worker_local_store import default_worker_state_dir

            state_dir = default_worker_state_dir()
        pinned = arguments.get("codex_bin")
        for source_id in source_ids:
            if len(source_id) > SOURCE_ID_MAX_LENGTH:
                raise WorkerConfigError(
                    f"--codex-source {source_id!r}: longer than "
                    + f"{SOURCE_ID_MAX_LENGTH} characters"
                )
            try:
                checked = v_safe_id(source_id, "codex_source")
            except ValueError as exc:
                raise WorkerConfigError(str(exc)) from None
            adapter = CodexLocalAdapter(
                source_id=checked,
                state_dir=state_dir,
                pinned_binary=(
                    Path(str(pinned))
                    if isinstance(pinned, str) and pinned
                    else None
                ),
            )
            registry.register(adapter)
    if zcode_source_ids:
        from pathlib import Path

        from .model_inventory import SOURCE_ID_MAX_LENGTH
        from .worker_zcode_adapter import ZCodeLocalAdapter

        # The authorized project workspace is REQUIRED: a ZCode source
        # without one has no coding capability to offer, so it never
        # constructs (fail closed at configuration time, never a silent
        # scratch-directory substitution).
        zcode_workspace = arguments.get("zcode_workspace")
        if not isinstance(zcode_workspace, str) or not zcode_workspace:
            raise WorkerConfigError(
                "--zcode-workspace is required with --zcode-source: the "
                + "source executes the authorized project workspace"
            )
        zcode_pinned = arguments.get("zcode_bin")
        for zcode_source_id in zcode_source_ids:
            if len(zcode_source_id) > SOURCE_ID_MAX_LENGTH:
                raise WorkerConfigError(
                    f"--zcode-source {zcode_source_id!r}: longer than "
                    + f"{SOURCE_ID_MAX_LENGTH} characters"
                )
            try:
                checked_zcode = v_safe_id(zcode_source_id, "zcode_source")
            except ValueError as exc:
                raise WorkerConfigError(str(exc)) from None
            if ":" in checked_zcode:
                # The derived resource namespace partitions on the first
                # colon (the same rule as the server-side configuration).
                raise WorkerConfigError(
                    f"--zcode-source {zcode_source_id!r}: ':' is not allowed"
                )
            try:
                adapter = ZCodeLocalAdapter(
                    source_id=checked_zcode,
                    authorized_workspace=zcode_workspace,
                    pinned_binary=(
                        Path(str(zcode_pinned))
                        if isinstance(zcode_pinned, str) and zcode_pinned
                        else None
                    ),
                )
            except (OSError, NotADirectoryError) as exc:
                raise WorkerConfigError(
                    f"--zcode-workspace {zcode_workspace!r}: "
                    + f"{type(exc).__name__}"
                ) from None
            registry.register(adapter)
    if allow_codex:
        from pathlib import Path

        from .worker_codex_adapter import CODEX_PROVIDER, CodexLocalAdapter

        codex_resource_id = arguments.get("codex_resource")
        if not isinstance(codex_resource_id, str) or not codex_resource_id:
            if allow_ollama:
                raise WorkerConfigError(
                    "--codex-resource is required with --allow-codex when "
                    + "--allow-ollama also names --resource"
                )
            if not isinstance(resource_id, str) or not resource_id:
                raise WorkerConfigError("--resource is required with --allow-codex")
            codex_resource_id = resource_id
        # D-042: the resource identity names ONE physical model. ``codex``
        # is the execution SURFACE (the local adapter id), never a model;
        # the slug is administrator configuration, never guessed from the
        # installed runtime, the account state or model/list's default —
        # model/list is only runtime verification.
        codex_model = arguments.get("codex_model")
        if not isinstance(codex_model, str) or not codex_model:
            raise WorkerConfigError("--codex-model is required with --allow-codex")
        try:
            model_slug = v_safe_id(codex_model, "codex_model")
        except ValueError as exc:
            raise WorkerConfigError(str(exc)) from None
        if state_dir is None:
            from .worker_local_store import default_worker_state_dir

            state_dir = default_worker_state_dir()
        pinned = arguments.get("codex_bin")
        resource = ResourceIdentity(
            resource_id=v_safe_id(codex_resource_id, "resource"),
            channel="worker_bridged",
            provider=CODEX_PROVIDER,
            model=model_slug,
            entitlement="subscription_included",
        )
        adapter = CodexLocalAdapter(
            resource=resource,
            state_dir=state_dir,
            pinned_binary=Path(str(pinned)) if isinstance(pinned, str) and pinned else None,
        )
        registry.register(adapter)
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = cast("dict[str, object]", vars(parser.parse_args(argv)))
    command = arguments.get("command")
    try:
        if command == "service":
            # The service lifecycle manages the UNIT, not this process's
            # store: it resolves the state directory itself and must not
            # create/open one as a side effect of `status`/`restart`. The
            # worker seams are injected here so the lifecycle module
            # never imports this one (no import cycle).
            from .worker_service import run_service_command

            return run_service_command(
                arguments,
                open_store=open_worker_store,
                build_registry=build_registry,
            )
        store = _open_store(
            str(arguments["state_dir"]) if arguments.get("state_dir") else None
        )
        try:
            # This function opened the store, so it closes it exactly
            # once on every path out of here. The ``run`` runtime uses
            # the store for its whole lifetime, so the close waits for
            # ``runtime.run()`` to finish.
            if command == "pair":
                origin = WorkerOrigin.parse(str(arguments["server"]))
                runtime = WorkerRuntime(origin=origin, store=store)
                identity = runtime.pair(str(arguments["code"]))
                print(f"paired as {identity.worker_id}; identity stored")
                return 0
            if command == "codex-login":
                from .worker_codex_adapter import run_official_codex_login

                source = arguments.get("source")
                state_dir = arguments.get("state_dir")
                codex_bin = arguments.get("codex_bin")
                if not isinstance(source, str) or not source:
                    # No explicit source: read the configured one from the
                    # installed worker service unit (issue #168) — the
                    # persisted home of the operator's adapter selection.
                    # Explicit flags keep their meaning; discovery only
                    # fills what was omitted, and nothing is guessed when
                    # the configuration is absent or ambiguous.
                    from .worker_service import (
                        ServiceUnitReadError,
                        read_installed_service_configuration,
                    )

                    try:
                        installed = read_installed_service_configuration()
                    except ServiceUnitReadError as exc:
                        print(f"worker: {exc}", file=sys.stderr)
                        return 2
                    sources = installed.selection.codex_sources
                    if len(sources) == 0:
                        print(
                            "worker: the installed worker service runs no "
                            + "--codex-source; reinstall it with one "
                            + "('scarcity-router-worker service install "
                            + "--codex-source SOURCE_ID', then "
                            + "'service restart') or pass --source",
                            file=sys.stderr,
                        )
                        return 2
                    if len(sources) > 1:
                        print(
                            "worker: the installed worker service runs "
                            + f"{len(sources)} codex sources ("
                            + ", ".join(sources)
                            + "); pass --source to choose one",
                            file=sys.stderr,
                        )
                        return 2
                    source = sources[0]
                    if not isinstance(state_dir, str) or not state_dir:
                        state_dir = str(installed.state_dir)
                    if not (isinstance(codex_bin, str) and codex_bin):
                        codex_bin = installed.selection.codex_bin
                    print(
                        f"codex-login: using configured source {source!r} "
                        + "(read from the installed worker service)"
                    )
                code = run_official_codex_login(
                    source_id=str(source),
                    state_dir=state_dir
                    if isinstance(state_dir, str) and state_dir
                    else None,
                    pinned_binary=(
                        str(codex_bin)
                        if isinstance(codex_bin, str) and codex_bin
                        else None
                    ),
                )
                return code
            if command == "run":
                server_value = arguments.get("server")
                if server_value is not None:
                    origin = WorkerOrigin.parse(str(server_value))
                else:
                    stored = store.load_identity()
                    if stored is None:
                        print("worker: not paired; run the pair command first", file=sys.stderr)
                        return 2
                    origin = WorkerOrigin.parse(stored.server_origin)
                state_dir = (
                    str(arguments["state_dir"]) if arguments.get("state_dir") else None
                )
                registry = build_registry(arguments, state_dir=state_dir)
                lock = WorkerStateDirLock(_resolved_state_dir(state_dir))
                try:
                    lock.acquire()
                except WorkerStateDirLockUnavailable as exc:
                    # Fail closed: a planted worker.lock (symlink or other
                    # non-regular object) is never followed — its target is
                    # untouched and this process does not run.
                    print(f"worker: {exc}", file=sys.stderr)
                    return 2
                except WorkerStateDirLocked as exc:
                    # Fail closed: a foreground run and the service (or a
                    # second foreground run) must never share a state
                    # directory (issue #138).
                    print(f"worker: {exc}", file=sys.stderr)
                    return 2
                runtime = WorkerRuntime(origin=origin, store=store, local_adapters=registry)
                restore_handlers = install_stop_signal_handlers(runtime)
                try:
                    reason = runtime.run()
                finally:
                    restore_handlers()
                    lock.release()
                    for line in runtime.diagnostics():
                        print(f"worker: {line}", file=sys.stderr)
                if reason == "reconnect_budget_exhausted":
                    return 3
                if reason == "fatal":
                    return 2
                return 0
            parser.error("unknown command")
        finally:
            store.close()
    except (WorkerConfigError, ValueError, OSError) as exc:
        print(f"worker: {exc}", file=sys.stderr)
        return 2


def install_stop_signal_handlers(runtime: object) -> Callable[[], None]:
    """Wire SIGTERM/SIGINT to ``runtime.request_stop`` (clean service stop).

    The systemd stop signal reaches ``run`` as SIGTERM; without this
    wiring the default disposition kills the process outright. The
    handler drives the runtime's deterministic stop path (stop event,
    socket closed) so the process exits cleanly and any in-flight
    attempt is cancelled and reported interrupted. Installs from the
    main thread only — elsewhere (and for runtimes without a stop seam)
    this is a no-op. Returns a callable restoring the previous handlers.
    """
    stop = getattr(runtime, "request_stop", None)
    if not callable(stop):
        return lambda: None

    def _request_stop(
        signum: int, frame: types.FrameType | None
    ) -> None:
        _ = signum, frame
        _ = stop()

    restored: list[tuple[int, SignalHandler]] = []
    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = cast(
                "SignalHandler", signal.getsignal(number)
            )
            _ = signal.signal(number, _request_stop)
        except (ValueError, OSError):
            # Not the main thread, or the platform refuses this signal:
            # skip it rather than fail the run.
            continue
        restored.append((number, previous))

    def restore() -> None:
        for number, previous in restored:
            try:
                _ = signal.signal(number, previous)
            except (ValueError, OSError):
                pass

    return restore


__all__ = [
    "ensure_reasoning_representable",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_MAX_RECONNECT_ATTEMPTS",
    "DEFAULT_PORT",
    "ConnectFactory",
    "ReconnectPolicy",
    "WorkerConfigError",
    "WorkerOrigin",
    "WorkerRuntime",
    "WorkerRuntimeError",
    "build_parser",
    "build_local_adapter_registry",
    "default_connect_factory",
    "install_stop_signal_handlers",
    "main",
    "open_worker_store",
    "tls_context_for_worker",
]


if __name__ == "__main__":
    raise SystemExit(main())
