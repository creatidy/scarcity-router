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
one-time code) and ``python -m scarcity_router.worker_client run``
(connect and serve). Windows autostart/service mechanics are M10 scope.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .errors import CapacityValidationError
from .gateway_adapters import AdapterResult, AdapterStreamChunk
from .gateway_validation import v_safe_id, v_text
from .resource_state import ResourceStateSnapshot, WorkerStateReport
from .worker_local_adapters import (
    AdapterNotAllowedError,
    LocalAdapterRegistry,
    run_allowlisted,
)
from .worker_local_store import (
    WorkerLocalIdentity,
    WorkerLocalStore,
    default_worker_state_dir,
)
from .worker_protocol import (
    AttemptInterruptedMessage,
    CancelMessage,
    CredentialRotatedMessage,
    ErrorMessage,
    ExecuteMessage,
    ExecuteResultMessage,
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


def default_connect_factory(origin: WorkerOrigin) -> FrameTransport:
    """TCP (+ TLS when the origin requires it) transport for one origin."""
    raw = socket.create_connection((origin.host, origin.port), timeout=10.0)
    try:
        if origin.tls:
            raw = tls_context_for_worker().wrap_socket(
                raw, server_hostname=origin.host
            )
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
    """One in-flight local execution tracked by the runtime."""

    def __init__(self, attempt_id: str) -> None:
        self.attempt_id: str = attempt_id
        self.cancel_event: threading.Event = threading.Event()
        self.thread: threading.Thread | None = None


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
        """Ask the run loop to stop at the next boundary."""
        self._stop_event.set()
        self._stop_reason = STOP_REQUESTED

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
                # Never overwrite a live tracker: a duplicate attempt id
                # would orphan the first execution's cancellation and
                # result routing. Definitively refuse the duplicate.
                self._send_result(
                    ExecuteResultMessage(
                        attempt_id=message.attempt_id,
                        status="failed",
                        calls=(),
                        note="a duplicate attempt id is already in flight",
                    )
                )
                return
            attempt = _WorkerAttempt(message.attempt_id)
            self._attempts[message.attempt_id] = attempt
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
        self._send_result(self._result_message(message.attempt_id, result))

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

    def _send_initial_state_report(self) -> None:
        self._send_state_report()

    def _send_state_report(self) -> None:
        """Build and send one M01 worker report (the shared path)."""
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
        try:
            self._sender.send(StateReportMessage(report=report.to_dict()).to_payload())
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


def build_parser() -> argparse.ArgumentParser:
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    prog = (
        invoked
        if invoked == "scarcity-router-worker"
        else "python -m scarcity_router.worker_client"
    )
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
    run = commands.add_parser("run", help="connect and serve (reconnecting)")
    _ = run.add_argument("--server", default=None, metavar="URL",
                         help="server origin (stored value when omitted)")
    _ = run.add_argument("--label", default=None, metavar="LABEL")
    _ = run.add_argument("--state-dir", default=None, metavar="DIR")
    _ = run.add_argument("--allow-ollama", action="store_true",
                         help="allow the loopback Ollama local adapter")
    _ = run.add_argument("--resource", default=None, metavar="ID",
                         help="the registry resource id the loopback adapter serves")
    _ = run.add_argument("--ollama-host", default="127.0.0.1", metavar="HOST")
    _ = run.add_argument("--ollama-port", type=int, default=11434, metavar="PORT")
    return parser


def _open_store(state_dir: str | None) -> WorkerLocalStore:
    directory = state_dir if state_dir else default_worker_state_dir()
    return WorkerLocalStore(Path(directory) / "worker-state.db")


def _build_registry(arguments: Mapping[str, object]) -> LocalAdapterRegistry | None:
    if not arguments.get("allow_ollama"):
        return None
    from .resource_state import ResourceIdentity
    from .worker_local_adapters import LoopbackOllamaAdapter

    resource_id = arguments.get("resource")
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
    registry = LocalAdapterRegistry()
    registry.register(adapter)
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = cast("dict[str, object]", vars(parser.parse_args(argv)))
    command = arguments.get("command")
    try:
        store = _open_store(
            str(arguments["state_dir"]) if arguments.get("state_dir") else None
        )
        if command == "pair":
            origin = WorkerOrigin.parse(str(arguments["server"]))
            runtime = WorkerRuntime(origin=origin, store=store)
            identity = runtime.pair(str(arguments["code"]))
            print(f"paired as {identity.worker_id}; identity stored")
            store.close()
            return 0
        if command == "run":
            server_value = arguments.get("server")
            if server_value is not None:
                origin = WorkerOrigin.parse(str(server_value))
            else:
                stored = store.load_identity()
                if stored is None:
                    print("worker: not paired; run the pair command first", file=sys.stderr)
                    store.close()
                    return 2
                origin = WorkerOrigin.parse(stored.server_origin)
            registry = _build_registry(arguments)
            runtime = WorkerRuntime(origin=origin, store=store, local_adapters=registry)
            try:
                reason = runtime.run()
            finally:
                for line in runtime.diagnostics():
                    print(f"worker: {line}", file=sys.stderr)
            store.close()
            if reason == "reconnect_budget_exhausted":
                return 3
            if reason == "fatal":
                return 2
            return 0
        parser.error("unknown command")
    except (WorkerConfigError, ValueError, OSError) as exc:
        print(f"worker: {exc}", file=sys.stderr)
        return 2


__all__ = [
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
    "default_connect_factory",
    "main",
    "tls_context_for_worker",
]


if __name__ == "__main__":
    raise SystemExit(main())
