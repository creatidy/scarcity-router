"""Server-side worker-protocol endpoint: pairing, sessions, dispatch (M05).

This is the server half of the worker protocol (issue #90, D-043/D-044):
the composition seam the ONE server component uses to terminate the
worker transport, plus a standalone/dev entrypoint
(``python -m scarcity_router.worker_endpoint``). It never performs
routing decisions and never executes anything itself; it authenticates
workers, applies their state reports through M01's registry, forwards
admitted executions to the right connected worker, and stays honest about
ambiguity.

Frozen behaviors implemented here:

- **Worker-initiated connections only.** The endpoint listens (on the
  SERVER, which already has inbound surfaces); workers dial out. A worker
  never opens an inbound port and never needs firewall configuration.
- **Pairing bootstrap.** A ``pair_request`` carrying a valid, unexpired,
  unredeemed one-time code (issued by the administrator through
  :class:`~scarcity_router.worker_identity_store.WorkerIdentityStore`)
  yields the per-device identity. Code reuse, expiry and invalidity are
  distinct typed failures.
- **Per-device authentication.** ``hello`` authenticates the stored
  identity on every connection; revoked credentials fail closed with
  ``credential_revoked``.
- **Protocol-version negotiation.** The handshake negotiates one mutually
  supported version; disjoint version sets are an explicit, fatal
  ``protocol_version_unsupported`` on both ends.
- **State reports flow through the ONE normalization path.** A
  ``state_report`` is validated as an M01 :class:`WorkerStateReport` and
  applied with ``ResourceRegistry.apply_worker_report`` -- there is no
  second collector or normalizer for worker-reported resources.
- **Attempt registry.** Every dispatched execution is tracked by its
  server-issued ``attempt_id``. A session loss resolves every in-flight
  attempt as *interrupted* -- an AMBIGUOUS outcome the adapter surface
  reports upward (never a silent re-dispatch, D-043). A late
  ``execute_result`` for an unknown attempt is answered with a typed
  ``attempt_unknown`` error instead of being attributed to some other
  request.
- **Bounded resources.** Frame sizes, pending attempts per session,
  concurrent sessions and session liveness (heartbeat silence with a
  grace multiplier) are all bounded; exceeding a bound closes the session
  (fail closed), never queues without limit.

No message in this protocol can ask a worker to run a shell command:
the only execution message is ``execute`` over the closed typed
:class:`~scarcity_router.gateway_adapters.AdapterCall` vocabulary, and
the worker enforces its own local allowlist on top.
"""

from __future__ import annotations

import socket
import ssl
import sys
import threading
import time
import argparse
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast

from .errors import CapacityValidationError
from .gateway_validation import v_int
from .model_inventory import (
    DiscoveredModel,
    ModelInventoryError,
    ModelInventoryReport,
    SourceInventory,
    is_source_resource_id,
)
from .resource_state import (
    ResourceRegistry,
    ResourceStateSnapshot,
    WorkerStateReport,
)
from .worker_identity_store import (
    ERR_CREDENTIAL_REVOKED,
    ERR_PAIRING_EXPIRED,
    ERR_PAIRING_INVALID,
    ERR_PAIRING_USED,
    WorkerIdentityError,
    WorkerIdentityStore,
)
from .worker_protocol import (
    ERR_ATTEMPT_UNKNOWN,
    ERR_INTERNAL,
    ERR_MALFORMED,
    ERR_UNKNOWN_MESSAGE,
    ERR_UNAUTHORIZED,
    AttemptInterruptedMessage,
    CancelMessage,
    CredentialRotatedMessage,
    ErrorMessage,
    ExecuteChunkMessage,
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
    RotateCredentialMessage,
    SocketTransport,
    StateReportAckMessage,
    StateReportMessage,
    SERVER_SUPPORTED_PROTOCOL_VERSIONS,
    WORKER_PROTOCOL_VERSION,
    WorkerProtocolError,
    negotiate_version,
    parse_worker_message,
)

# Default listen parameters (dev entrypoint). Non-loopback binds require
# TLS, exactly like the execution gateway (D-044).
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8790

# Session bounds (defense in depth; the coordinator's admission limits
# remain the primary concurrency control for executions).
MAX_SESSIONS = 64
MAX_PENDING_ATTEMPTS_PER_SESSION = 32
MAX_INTERRUPTED_ATTEMPT_RECORDS = 256

# Liveness: a session is closed after this many heartbeat intervals of
# total silence (worker heartbeats arrive every heartbeat_interval_seconds).
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15
LIVENESS_GRACE_INTERVALS = 3


class WorkerDispatchError(Exception):
    """No worker can currently receive an execution for one resource.

    Definitive, not ambiguous: raised BEFORE any protocol message is
    sent, so the adapter surface may translate it into a permanent
    failure (nothing was ever dispatched, so nothing can be duplicated).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


class WorkerReportSink(Protocol):
    """Where authenticated state reports are applied (composition seam).

    :class:`~scarcity_router.resource_state.ResourceRegistry` satisfies
    this structurally (the standalone entrypoint passes one). The
    integrated server component passes the M09 control plane instead, so
    worker-reported observations survive the control plane's
    configuration rebuilds and every surface (routing, diagnostics) sees
    the same state — there is no second collector for worker reports.
    """

    def apply_worker_report(self, report: WorkerStateReport) -> None: ...


OwnerResolver = Callable[[str], "str | None"]
"""Resource-id -> configured-owner worker-id resolver (authorization seam).

CURRENT administrator configuration is the only source of resource-to-
worker ownership (D-049 amendment): for a resource id the resolver
returns the worker identity the administrator assigned it to, or
``None`` when the resource is unknown, disabled, not worker-bridged, or
unassigned. Worker telemetry can never appear here. The integrated
server supplies a live resolver over M09's configuration document, so
every configuration change is authoritative immediately; a deployment
without administrator configuration supplies a deny-all resolver —
nothing is reportable or executable there.
"""


@dataclass(frozen=True)
class WorkerConnectionState:
    """One worker device's real connection state (M09 display seam).

    ``connected`` is derived from the live authenticated session table;
    ``last_connected_at`` is the wall-clock stamp of the device's most
    recent successful authentication (pairing or hello) in this endpoint
    process's lifetime. Nothing here is fabricated: a stamp that was
    never made (device not yet seen, or lost to a restart) reads ``None``.
    """

    worker_id: str
    connected: bool
    last_connected_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "connected": self.connected,
            "last_connected_at": self.last_connected_at,
        }


@dataclass
class AttemptOutcome:
    """The terminal state of one tracked attempt as the endpoint saw it."""

    status: str  # "completed" | "failed" | "cancelled" | "interrupted"
    result: ExecuteResultMessage | None = None
    note: str | None = None


class PendingAttempt:
    """The shared per-execution state between the read loop and a waiter.

    The session's read loop appends arriving chunks and the terminal
    result; the adapter thread (the coordinator's dispatch thread)
    consumes them in order through :meth:`take`. A connection loss marks
    the attempt ``interrupted`` -- the honest ambiguous outcome.
    """

    def __init__(self, attempt_id: str) -> None:
        self.attempt_id: str = attempt_id
        self._condition: threading.Condition = threading.Condition()
        self._chunks: list[ExecuteChunkMessage] = []
        self._outcome: AttemptOutcome | None = None
        self.cancel_requested: bool = False

    def push_chunk(self, message: ExecuteChunkMessage) -> None:
        with self._condition:
            self._chunks.append(message)
            self._condition.notify_all()

    def resolve(self, outcome: AttemptOutcome) -> None:
        with self._condition:
            if self._outcome is None:
                self._outcome = outcome
            self._condition.notify_all()

    def resolve_interrupted(self, note: str) -> None:
        self.resolve(AttemptOutcome(status="interrupted", note=note))

    def take(
        self, timeout_seconds: float
    ) -> tuple[str, ExecuteChunkMessage | AttemptOutcome | None]:
        """One chunk, the terminal outcome, or ``("timeout", None)``.

        Returns ``("chunk", message)`` for a stream chunk (in arrival
        order), ``("outcome", outcome)`` once, then ``("outcome", None)``
        forever after, and ``("timeout", None)`` when nothing arrived
        within the budget.
        """
        with self._condition:
            if not self._chunks and self._outcome is None:
                _ = self._condition.wait(timeout_seconds)
            if self._chunks:
                return "chunk", self._chunks.pop(0)
            if self._outcome is not None:
                outcome = self._outcome
                return "outcome", outcome
            return "timeout", None

    @property
    def finished(self) -> bool:
        with self._condition:
            return self._outcome is not None


@dataclass
class _SessionState:
    """Mutable per-connection bookkeeping."""

    reader: FrameReader
    writer: FrameWriter
    transport: FrameTransport
    worker_id: str | None = None
    negotiated_version: int | None = None
    authenticated: bool = False
    closed: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    pending: dict[str, PendingAttempt] = field(default_factory=dict)


class WorkerSession:
    """One authenticated worker connection (server side).

    Public methods (``submit_execute``, ``send_cancel``, ``close``) are
    called from other threads (the coordinator's dispatch threads, the
    endpoint, the liveness monitor); the ``run`` loop owns the read side.
    All cross-thread mutation goes through the endpoint's lock or the
    per-attempt conditions.
    """

    def __init__(self, endpoint: "WorkerEndpoint", state: _SessionState) -> None:
        self._endpoint: "WorkerEndpoint" = endpoint
        self._state: _SessionState = state

    @property
    def worker_id(self) -> str | None:
        return self._state.worker_id

    @property
    def authenticated(self) -> bool:
        return self._state.authenticated

    @property
    def negotiated_version(self) -> int | None:
        return self._state.negotiated_version

    def submit_execute(self, message: ExecuteMessage) -> PendingAttempt:
        """Send one execute request and track its attempt.

        Raises :class:`WorkerDispatchError` when the session is already
        closed (nothing was sent -- definitive) and
        :class:`WorkerProtocolError` when the frame cannot be encoded or
        written after being sent (the worker MAY have received it --
        ambiguous).
        """
        with self._endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            if self._state.closed:
                raise WorkerDispatchError(
                    "worker_offline", "the worker session is closed"
                )
            if len(self._state.pending) >= MAX_PENDING_ATTEMPTS_PER_SESSION:
                raise WorkerDispatchError(
                    "worker_overloaded",
                    "the worker session is at its pending-attempt bound",
                )
            if message.attempt_id in self._state.pending:
                # Attempt ids are unique per dispatch by contract; a
                # duplicate would make one response resolve the OTHER
                # execution's tracker. Refuse before anything is sent
                # (definitive -- the first execution is untouched).
                raise WorkerDispatchError(
                    "duplicate_attempt",
                    "an attempt with this id is already in flight on this "
                    + "session",
                )
            attempt = PendingAttempt(message.attempt_id)
            self._state.pending[message.attempt_id] = attempt
        try:
            self._state.writer.write_message(message.to_payload())
        except (OSError, WorkerProtocolError) as exc:
            # The frame may have been partially delivered: the attempt's
            # fate is unknown, so the waiter must see ambiguity, never a
            # clean retry path.
            self._discard_attempt(message.attempt_id)
            self.close(note="execute delivery failed")
            raise WorkerDispatchError(
                "worker_connection_lost",
                "the execute request could not be delivered deterministically",
            ) from exc
        return attempt

    def send_cancel(self, attempt_id: str) -> None:
        """Best-effort cancellation notice; never raises to the caller."""
        with self._endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            if self._state.closed:
                return
        try:
            self._state.writer.write_message(CancelMessage(attempt_id=attempt_id).to_payload())
        except (OSError, WorkerProtocolError):
            self.close(note="cancel delivery failed")

    def close(self, *, note: str) -> None:
        """Close the session and resolve every pending attempt as interrupted.

        The session row is removed UNCONDITIONALLY: the listener path
        registers every accepted connection before authentication, so a
        pre-auth failure (malformed frame, depth bomb, plain disconnect)
        must still release its row -- otherwise pre-auth connections pin
        the bounded session table until the endpoint refuses every
        legitimate connection. Popping an id that was never registered
        (in-process ``attach_transport`` sessions that never
        authenticated) is a no-op by ``dict.pop`` semantics.
        """
        with self._endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            if self._state.closed:
                return
            self._state.closed = True
            pending = list(self._state.pending.values())
            self._state.pending.clear()
            _ = self._endpoint._sessions.pop(id(self), None)  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
        for attempt in pending:
            attempt.resolve_interrupted(note or "session closed")
        try:
            self._state.transport.close()
        except OSError:
            pass

    # ── The read loop ────────────────────────────────────────────────

    def run(self) -> None:
        """Read and dispatch messages until EOF, error or close."""
        try:
            self._handshake()
            while True:
                if self._state.closed:
                    return
                payload = self._state.reader.read_message()
                if payload is None:
                    self.close(note="worker disconnected")
                    return
                with self._endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
                    self._state.last_seen = self._endpoint._monotonic()  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
                self._handle_message(payload)
        except WorkerProtocolError as exc:
            self._send_error(ErrorMessage(code=exc.code, message=exc.message, fatal=True))
            self.close(note=f"protocol error: {exc.code}")
        except RecursionError:
            # Defense in depth: a parse failure that surfaced as a bare
            # RecursionError must still end in a CLEAN close -- the
            # session row removed and every pending attempt resolved --
            # never a leaked thread, socket or session (the depth-bomb
            # wedging scenario). The error frame is skipped: the stack
            # may be nearly exhausted, so answer nothing and just close.
            self.close(note="unparseable frame")
        except (OSError, ConnectionError, TimeoutError):
            self.close(note="connection error")

    def _handshake(self) -> None:
        payload = self._state.reader.read_message()
        if payload is None:
            self.close(note="worker disconnected before handshake")
            return
        message_type = payload.get("type")
        if message_type == "pair_request":
            request = PairRequestMessage.from_payload(payload)
            self._negotiate_or_die(request.supported_versions)
            self._pair(request)
            return
        if message_type == "hello":
            hello = HelloMessage.from_payload(payload)
            self._negotiate_or_die(hello.supported_versions)
            self._authenticate(hello)
            return
        raise WorkerProtocolError(
            ERR_MALFORMED,
            "the first message must be a hello or pair_request handshake",
        )

    def _negotiate_or_die(self, offered: tuple[int, ...]) -> None:
        try:
            # D-053: the server still accepts version-1 workers (they
            # never send the version-2 inventory section); negotiation
            # picks the highest mutually supported version.
            version = negotiate_version(SERVER_SUPPORTED_PROTOCOL_VERSIONS, offered)
        except WorkerProtocolError:
            # The fatal error frame is sent by the run() error handler.
            raise
        self._state.negotiated_version = version

    def _pair(self, request: PairRequestMessage) -> None:
        endpoint = self._endpoint
        try:
            worker_id, credential = endpoint.identity_store.redeem_pairing_code(
                request.pairing_code
            )
        except WorkerIdentityError as exc:
            code = {
                ERR_PAIRING_INVALID: ERR_PAIRING_INVALID,
                ERR_PAIRING_EXPIRED: ERR_PAIRING_EXPIRED,
                ERR_PAIRING_USED: ERR_PAIRING_USED,
            }.get(exc.code, ERR_INTERNAL)
            self._send_error(ErrorMessage(code=code, message=exc.message, fatal=True))
            self.close(note=f"pairing rejected: {exc.code}")
            return
        with endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            self._state.worker_id = worker_id
            self._state.authenticated = True
            endpoint._sessions[id(self)] = self  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
        endpoint.record_connected(worker_id)
        self._state.writer.write_message(
            PairResultMessage(
                negotiated_version=self._state.negotiated_version or WORKER_PROTOCOL_VERSION,
                worker_id=worker_id,
                credential=credential,
                heartbeat_interval_seconds=endpoint.heartbeat_interval_seconds,
            ).to_payload()
        )

    def _authenticate(self, hello: HelloMessage) -> None:
        endpoint = self._endpoint
        try:
            endpoint.identity_store.authenticate(hello.worker_id, hello.credential)
        except WorkerIdentityError as exc:
            code = ERR_CREDENTIAL_REVOKED if exc.code == ERR_CREDENTIAL_REVOKED else ERR_UNAUTHORIZED
            self._send_error(ErrorMessage(code=code, message=exc.message, fatal=True))
            self.close(note=f"authentication rejected: {code}")
            return
        with endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            self._state.worker_id = hello.worker_id
            self._state.authenticated = True
            endpoint._sessions[id(self)] = self  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
        endpoint.record_connected(hello.worker_id)
        self._state.writer.write_message(
            HelloAckMessage(
                negotiated_version=self._state.negotiated_version or WORKER_PROTOCOL_VERSION,
                heartbeat_interval_seconds=endpoint.heartbeat_interval_seconds,
            ).to_payload()
        )

    def _handle_message(self, payload: Mapping[str, object]) -> None:
        message = parse_worker_message(payload)
        if isinstance(message, HeartbeatMessage):
            self._state.writer.write_message(
                HeartbeatAckMessage(seq=message.seq).to_payload()
            )
            return
        if isinstance(message, StateReportMessage):
            self._apply_state_report(message)
            return
        if isinstance(message, ExecuteChunkMessage):
            self._route_to_attempt(message.attempt_id, message)
            return
        if isinstance(message, ExecuteResultMessage):
            self._complete_attempt(message)
            return
        if isinstance(message, AttemptInterruptedMessage):
            self._register_interrupted(message)
            return
        if isinstance(message, RotateCredentialMessage):
            self._rotate_credential()
            return
        if isinstance(message, (HelloMessage, PairRequestMessage)):
            raise WorkerProtocolError(
                ERR_MALFORMED, "a handshake message arrived after the handshake"
            )
        raise WorkerProtocolError(ERR_UNKNOWN_MESSAGE, "unhandled worker message")

    def _apply_state_report(self, message: StateReportMessage) -> None:
        endpoint = self._endpoint
        worker_id = self._state.worker_id
        if worker_id is None or not self._state.authenticated:
            raise WorkerProtocolError(ERR_MALFORMED, "state report before authentication")
        try:
            report = WorkerStateReport.from_dict(dict(message.report))
        except (CapacityValidationError, ValueError) as exc:
            self._send_error(
                ErrorMessage(
                    code=ERR_MALFORMED,
                    message=f"the state report was rejected: {exc}",
                    fatal=False,
                )
            )
            return
        if report.worker_id != worker_id:
            self._send_error(
                ErrorMessage(
                    code=ERR_MALFORMED,
                    message="the state report's worker id does not match the "
                    + "authenticated identity",
                    fatal=False,
                )
            )
            return
        # D-053 (protocol version 2): optional bounded discovery
        # documents, validated and applied BEFORE the resource section:
        # adoption may materialize derived resources that THIS report's
        # snapshots then reference. Every document is validated
        # fail-closed by the inventory contract and must name the
        # authenticated worker; a bad document rejects the whole report
        # (same atomicity as the resource section). No sink wired means
        # inventories are acknowledged and dropped — never applied.
        applied_inventories: list[ModelInventoryReport] = []
        for raw_inventory in message.inventories:
            try:
                inventory = ModelInventoryReport.from_dict(dict(raw_inventory))
                if inventory.worker_id != worker_id:
                    raise ModelInventoryError(
                        "inventory.worker_id does not match the authenticated identity"
                    )
                # D-053 authorization (Daybreak finding 1): EVERY source
                # named by the inventory must be configured on THIS
                # authenticated worker. A report naming another worker's
                # source is rejected whole — an authenticated worker can
                # never mutate, adopt or retire another worker's source.
                for source_inventory in inventory.sources:
                    if not endpoint.is_source_bound_source(
                        source_inventory.source_id, worker_id
                    ):
                        raise ModelInventoryError(
                            "source "
                            + repr(source_inventory.source_id)
                            + " is not configured for this worker"
                        )
            except (ModelInventoryError, ValueError) as exc:
                self._send_error(
                    ErrorMessage(
                        code=ERR_MALFORMED,
                        message=f"the state report's inventory was rejected: {exc}",
                        fatal=False,
                    )
                )
                return
            applied_inventories.append(inventory)
        # Daybreak blocker 4: a source may occur at most once across the
        # ENTIRE state report. Duplicates would multiply absence/miss
        # counting (instant retirement) — reject the whole report.
        seen_source_ids: set[str] = set()
        for inventory in applied_inventories:
            for source_inventory in inventory.sources:
                if source_inventory.source_id in seen_source_ids:
                    self._send_error(
                        ErrorMessage(
                            code=ERR_MALFORMED,
                            message="the state report carries duplicate "
                            + "inventories for source "
                            + repr(source_inventory.source_id),
                            fatal=False,
                        )
                    )
                    return
                seen_source_ids.add(source_inventory.source_id)
        for inventory in applied_inventories:
            sink = endpoint.inventory_sink
            if sink is not None:
                # The sink (server-side adoption) is guarded exactly like
                # the resource section: a server-side adoption failure is
                # a non-fatal, client-classifiable error frame — never an
                # unhandled exception on the session thread (D-053 point
                # 11: a discovery failure isolates to its source).
                try:
                    sink(inventory)
                except ValueError as exc:
                    self._send_error(
                        ErrorMessage(
                            code=ERR_INTERNAL,
                            message=f"the inventory could not be adopted: {exc}",
                            fatal=False,
                        )
                    )
                    return
        # AUTHORIZATION (D-049 amendment): current administrator
        # configuration is the only source of resource-to-worker
        # ownership. Every reported resource must be configured to THIS
        # authenticated worker (or derived from a source configured on
        # it, D-053); otherwise the ENTIRE report is rejected
        # fail-closed — the M01 registry and the observed/live bindings
        # change nothing (atomic, matching apply_worker_report), so an
        # unauthorized report can never overwrite a previously valid one.
        for snapshot in report.resources:
            configured = endpoint.configured_owner(snapshot.identity.resource_id)
            if configured != worker_id:
                self._send_error(
                    ErrorMessage(
                        code=ERR_MALFORMED,
                        message="resource "
                        + repr(snapshot.identity.resource_id)
                        + " is not configured for this worker; the report "
                        + "was rejected",
                        fatal=False,
                    )
                )
                return
        # D-053 reconciliation: a source worker honestly reports EVERY
        # model its runtime lists, but only ADOPTED models are registered
        # (restricted/unclassified models never materialize). A
        # source-derived snapshot for this authenticated worker that has
        # no registration is therefore DROPPED from the applied set — its
        # state lives in the source view, never the resource registry —
        # while every other unregistered resource still rejects the whole
        # report (the spoof-protection atomicity is unchanged), and any
        # failure among the applied snapshots remains atomic.
        applied: list[ResourceStateSnapshot] = []
        # Daybreak blocker 8: a source whose applied inventory is NOT
        # authenticated cannot have healthy snapshots in the same report —
        # they would be stale pre-auth-loss state contradicting the
        # inventory. Drop them so a closed source never looks routable.
        unauthenticated_sources = {
            s.source_id
            for inv in applied_inventories
            for s in inv.sources
            if s.auth_state != "authenticated"
        }
        for snapshot in report.resources:
            rid = snapshot.identity.resource_id
            if not endpoint.is_registered(rid) and endpoint.is_source_bound(
                rid, worker_id
            ):
                continue
            if is_source_resource_id(rid):
                source_id = rid.partition(":")[0]
                if source_id in unauthenticated_sources:
                    continue
            applied.append(snapshot)
        if len(applied) != len(report.resources):
            report = WorkerStateReport(
                schema_version=report.schema_version,
                worker_id=report.worker_id,
                reported_at=report.reported_at,
                resources=tuple(applied),
            )
        try:
            endpoint.registry.apply_worker_report(report)
        except (CapacityValidationError, ValueError) as exc:
            self._send_error(
                ErrorMessage(
                    code=ERR_MALFORMED,
                    message=f"the state report was not applied: {exc}",
                    fatal=False,
                )
            )
            return
        # OBSERVED/LIVE state only: record which worker reported each
        # resource (availability evidence, NOT authorization — dispatch
        # authorization always re-reads the configured owner).
        with endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            for snapshot in report.resources:
                endpoint._observed_bindings[snapshot.identity.resource_id] = worker_id  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
        self._state.writer.write_message(StateReportAckMessage().to_payload())

    def _route_to_attempt(self, attempt_id: str, message: ExecuteChunkMessage) -> None:
        with self._endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            attempt = self._state.pending.get(attempt_id)
        if attempt is None:
            self._send_error(
                ErrorMessage(
                    code=ERR_ATTEMPT_UNKNOWN,
                    message="a chunk arrived for an attempt this session is not "
                    + "tracking",
                    fatal=False,
                )
            )
            return
        attempt.push_chunk(message)

    def _complete_attempt(self, message: ExecuteResultMessage) -> None:
        with self._endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            attempt = self._state.pending.pop(message.attempt_id, None)
        if attempt is None:
            self._send_error(
                ErrorMessage(
                    code=ERR_ATTEMPT_UNKNOWN,
                    message="a result arrived for an attempt this session is not "
                    + "tracking",
                    fatal=False,
                )
            )
            return
        status = message.status
        if status == "completed" and (message.message is None or message.finish_reason is None):
            attempt.resolve(
                AttemptOutcome(
                    status="failed",
                    note="the worker reported an incomplete execution result",
                )
            )
            return
        attempt.resolve(AttemptOutcome(status=status, result=message))

    def _register_interrupted(self, message: AttemptInterruptedMessage) -> None:
        endpoint = self._endpoint
        with endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            attempt = self._state.pending.pop(message.attempt_id, None)
            endpoint._interrupted_attempts.append(  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
                (self._state.worker_id, message.attempt_id)
            )
            if len(endpoint._interrupted_attempts) > MAX_INTERRUPTED_ATTEMPT_RECORDS:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
                _ = endpoint._interrupted_attempts.popleft()  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
        if attempt is not None:
            attempt.resolve_interrupted("reported interrupted by the worker")

    def _rotate_credential(self) -> None:
        worker_id = self._state.worker_id
        if worker_id is None or not self._state.authenticated:
            raise WorkerProtocolError(ERR_MALFORMED, "credential rotation before authentication")
        try:
            credential = self._endpoint.identity_store.rotate_credential(worker_id)
        except WorkerIdentityError as exc:
            self._send_error(ErrorMessage(code=ERR_UNAUTHORIZED, message=exc.message, fatal=True))
            self.close(note="rotation failed")
            return
        try:
            self._state.writer.write_message(
                CredentialRotatedMessage(credential=credential).to_payload()
            )
        except (OSError, WorkerProtocolError):
            self.close(note="rotation delivery failed")

    def _send_error(self, error: ErrorMessage) -> None:
        try:
            self._state.writer.write_message(error.to_payload())
        except (OSError, WorkerProtocolError):
            self.close(note="error delivery failed")

    def _discard_attempt(self, attempt_id: str) -> None:
        with self._endpoint._lock:  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            _ = self._state.pending.pop(attempt_id, None)


class WorkerEndpoint:
    """The composed worker-protocol surface of the server component.

    Owns the identity store (D-044 worker identities), the M01 registry
    the reports are applied through, the connected-session table and the
    resource→worker bindings derived from authenticated state reports.
    The TCP/TLS listener is optional (``attach_listener``) so tests and
    in-process compositions can drive sessions directly over injected
    transports.
    """

    def __init__(
        self,
        *,
        identity_store: WorkerIdentityStore,
        registry: WorkerReportSink,
        configured_owner: OwnerResolver,
        heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        _ = v_int(heartbeat_interval_seconds, "heartbeat_interval_seconds", lo=1)
        self.identity_store: WorkerIdentityStore = identity_store
        self.registry: WorkerReportSink = registry
        # Authorization seam: the CURRENT administrator configuration is
        # the only source of resource-to-worker ownership. Kept as a
        # callable so the supplier (the M09 control plane) can rebind its
        # configuration atomically — the endpoint never caches ownership.
        self.configured_owner: OwnerResolver = configured_owner
        # D-053: optional sink for validated source-inventory documents
        # (wired by the composed server; None means inventories are
        # accepted-and-dropped for v2 workers with no source domain).
        self.inventory_sink: Callable[[ModelInventoryReport], None] | None = None
        #: D-053 seams (wired by the composed server): membership over the
        #: live M01 registry, and the source-derived-binding test for the
        #: authenticated worker. Defaults deny everything.
        self.is_registered: Callable[[str], bool] = lambda resource_id: False
        self.is_source_bound: Callable[[str, str], bool] = (
            lambda resource_id, worker_id: False
        )
        #: Source-id-level ownership probe (Daybreak finding 1): does THIS
        #: worker own the named execution source?
        self.is_source_bound_source: Callable[[str, str], bool] = (
            lambda source_id, worker_id: False
        )
        self.heartbeat_interval_seconds: int = heartbeat_interval_seconds
        self._monotonic: Callable[[], float] = monotonic
        self._wall_clock: Callable[[], datetime] = (
            wall_clock if wall_clock is not None else _utc_wall_now
        )
        self._lock: threading.Lock = threading.Lock()
        self._sessions: dict[int, WorkerSession] = {}
        # OBSERVED/LIVE state only: which worker last successfully
        # reported each resource. Never consulted for authorization.
        self._observed_bindings: dict[str, str] = {}
        self._last_connected_at: dict[str, str] = {}
        self._interrupted_attempts: deque[tuple[str | None, str]] = deque()
        self._listener: "_EndpointListener | None" = None

    # ── Session plumbing (tests/in-process composition) ──────────────

    def attach_transport(self, transport: FrameTransport) -> WorkerSession:
        """Adopt one already-connected transport as a new session.

        This is the composition seam for in-process assembly; the TCP
        listener path uses it internally as well.
        """
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise WorkerDispatchError(
                    "endpoint_busy", "the endpoint is at its concurrent-session bound"
                )
        state = _SessionState(
            reader=FrameReader(transport),
            writer=FrameWriter(transport),
            transport=transport,
            last_seen=self._monotonic(),
        )
        return WorkerSession(self, state)

    def register_attached(self, session: WorkerSession) -> None:
        with self._lock:
            self._sessions[id(session)] = session

    def session_for_resource(self, resource_id: str) -> WorkerSession:
        """The one session that may execute ``resource_id`` — the CONFIGURED
        owner's live authenticated session, with the owner's own valid
        report as availability evidence.

        Authorization comes only from the configured owner resolver
        (administrator configuration, D-049 amendment); the observed
        bindings gate availability only. There is no fallback: an
        offline or unreported configured owner is an explicit failure,
        never a route to another worker.
        """
        configured = self.configured_owner(resource_id)
        if configured is None:
            raise WorkerDispatchError(
                "resource_unbound",
                "no worker is configured to execute this resource",
            )
        with self._lock:
            reported_by = self._observed_bindings.get(resource_id)
            sessions = [
                s for s in self._sessions.values() if s.worker_id == configured
            ]
        if reported_by != configured:
            # The configured owner exists but has not (yet) reported this
            # resource: no availability evidence, and telemetry from any
            # OTHER worker can never substitute.
            raise WorkerDispatchError(
                "resource_unbound",
                "the configured worker has not reported this resource",
            )
        for session in sessions:
            if session.authenticated:
                return session
        raise WorkerDispatchError(
            "worker_offline", "the configured worker for this resource is not connected"
        )

    def session_count(self) -> int:
        """How many live session rows the bounded table currently holds
        (authenticated or not -- every accepted connection holds a row
        until its session closes)."""
        with self._lock:
            return len(self._sessions)

    def connected_worker_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted({s.worker_id for s in self._sessions.values() if s.worker_id}))

    def worker_connection_state(self) -> tuple[WorkerConnectionState, ...]:
        """The real connection state of every worker this endpoint knows.

        Workers with a liveness stamp or a live session appear; unknown
        devices do not. This is the display seam M09's workers view and
        diagnostics render (liveness stamps recorded at successful
        authentication), replacing any second connection bookkeeping.
        """
        with self._lock:
            connected = {
                s.worker_id for s in self._sessions.values() if s.worker_id
            }
            stamps = dict(self._last_connected_at)
        known: set[str] = set(connected) | set(stamps)
        return tuple(
            sorted(
                (
                    WorkerConnectionState(
                        worker_id=worker_id,
                        connected=worker_id in connected,
                        last_connected_at=stamps.get(worker_id),
                    )
                    for worker_id in known
                ),
                key=lambda state: state.worker_id,
            )
        )

    def observed_worker_bindings(self) -> dict[str, str]:
        """OBSERVED/LIVE state: resource id -> last reporting worker.

        Telemetry bookkeeping for diagnostics and availability evidence —
        never authorization (see ``configured_owner``).
        """
        with self._lock:
            return dict(self._observed_bindings)

    def interrupted_attempt_log(self) -> tuple[tuple[str | None, str], ...]:
        with self._lock:
            return tuple(self._interrupted_attempts)

    # ── Execution dispatch (used by the worker_bridged adapter) ──────

    def record_connected(self, worker_id: str) -> None:
        """Stamp a successful authentication (liveness display seam)."""
        moment = self._wall_clock()
        stamp = moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock:
            self._last_connected_at[worker_id] = stamp.replace("+00:00", "Z")

    def submit_execute_for_resource(self, message: ExecuteMessage, resource_id: str) -> PendingAttempt:
        """Route one execute message to the session bound to ``resource_id``."""
        session = self.session_for_resource(resource_id)
        return session.submit_execute(message)

    # ── Administration (M09 composes these) ──────────────────────────

    def revoke_worker(self, worker_id: str) -> None:
        """Revoke one device identity and close its live sessions now."""
        self.identity_store.revoke(worker_id)
        with self._lock:
            sessions = [s for s in self._sessions.values() if s.worker_id == worker_id]
        for session in sessions:
            session.close(note="worker revoked")

    def close_all_sessions(self, *, note: str = "endpoint shutdown") -> None:
        """Close every live session (server shutdown / test teardown)."""
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.close(note=note)

    # ── Liveness ─────────────────────────────────────────────────────

    def enforce_liveness(self) -> tuple[str, ...]:
        """Close sessions silent for more than the liveness grace window.

        Returns the closed sessions' worker ids. The runtime monitor calls
        this periodically; tests may call it directly with a frozen
        monotonic clock for deterministic liveness assertions.
        """
        deadline = self.heartbeat_interval_seconds * LIVENESS_GRACE_INTERVALS
        now = self._monotonic()
        with self._lock:
            stale = [
                s for s in self._sessions.values() if now - s._state.last_seen > deadline  # pyright: ignore[reportPrivateUsage] - same-program endpoint seam
            ]
        for session in stale:
            session.close(note="liveness timeout")
        return tuple(sorted({s.worker_id or "" for s in stale}))

    # ── TCP/TLS listener ─────────────────────────────────────────────

    def attach_listener(
        self,
        *,
        host: str = BIND_HOST,
        port: int = DEFAULT_PORT,
        tls_context: ssl.SSLContext | None = None,
    ) -> "_EndpointListener":
        """Bind the worker-protocol listener (server side).

        Non-loopback binds require TLS, exactly like the execution
        gateway (D-044). The listener never reaches out to workers; it
        only accepts their outbound connections.
        """
        if self._listener is not None:
            raise ValueError("worker_endpoint: a listener is already attached")
        listener = _EndpointListener(self, host=host, port=port, tls_context=tls_context)
        self._listener = listener
        return listener


class _EndpointListener:
    """Accept loop for worker connections (one thread, daemonic)."""

    def __init__(
        self,
        endpoint: WorkerEndpoint,
        *,
        host: str,
        port: int,
        tls_context: ssl.SSLContext | None,
    ) -> None:
        self._endpoint: WorkerEndpoint = endpoint
        self._tls_context: ssl.SSLContext | None = tls_context
        loopback = host in (BIND_HOST, "localhost", "::1")
        if not loopback and tls_context is None:
            raise ValueError(
                "a non-loopback worker-protocol listener requires TLS "
                + "(certificate and key files)"
            )
        self._server_socket: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server_socket.bind((host, port))
            self._server_socket.listen(16)
        except OSError:
            self._server_socket.close()
            raise
        self._accept_thread: threading.Thread | None = None
        self._stopping: bool = False

    @property
    def bound_port(self) -> int:
        address = cast("tuple[str, int]", self._server_socket.getsockname())
        return int(address[1])

    def serve_in_background(self) -> None:
        if self._accept_thread is not None:
            return
        self._accept_thread = threading.Thread(
            target=self.serve, name="worker-endpoint-accept", daemon=True
        )
        self._accept_thread.start()

    def serve(self) -> None:
        while not self._stopping:
            try:
                accepted = cast(
                    "tuple[socket.socket, tuple[str, int]]",
                    self._server_socket.accept(),
                )
                connection = accepted[0]
                _ = accepted[1]
            except OSError:
                if self._stopping:
                    return
                continue
            try:
                self._handle_connection(connection)
            except OSError:
                try:
                    connection.close()
                except OSError:
                    pass

    def _handle_connection(self, connection: socket.socket) -> None:
        wrapped: socket.socket = connection
        if self._tls_context is not None:
            try:
                wrapped = self._tls_context.wrap_socket(connection, server_side=True)
            except (ssl.SSLError, OSError):
                try:
                    connection.close()
                except OSError:
                    pass
                return
        transport = SocketTransport(wrapped, recv_timeout_seconds=None)
        try:
            session = self._endpoint.attach_transport(transport)
        except WorkerDispatchError:
            transport.close()
            return
        self._endpoint.register_attached(session)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()

    def shutdown(self) -> None:
        self._stopping = True
        try:
            self._server_socket.close()
        except OSError:
            pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5)
            self._accept_thread = None


def _utc_wall_now() -> datetime:
    return datetime.now(timezone.utc)


def build_tls_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """A server-authenticated TLS context (no ``verify=false`` exists)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"TLS configuration failed: {exc}") from None
    return context


# ── Standalone/dev entrypoint ─────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    prog = (
        invoked
        if invoked == "scarcity-router-worker-endpoint"
        else "python -m scarcity_router.worker_endpoint"
    )
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Scarcity Router worker-protocol endpoint: accepts authenticated "
            + "worker connections, applies their state reports to the resource "
            + "registry and forwards executions."
        ),
    )
    _ = parser.add_argument(
        "--host", default=BIND_HOST, metavar="HOST",
        help=f"bind address (default: {BIND_HOST}; non-loopback requires TLS)",
    )
    _ = parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, metavar="PORT",
        help=f"TCP port to bind (default: {DEFAULT_PORT})",
    )
    _ = parser.add_argument(
        "--store", required=True, metavar="FILE",
        help="worker identity store path (SQLite, created 0o600 in a 0o700 directory)",
    )
    _ = parser.add_argument(
        "--tls-certfile", default=None, metavar="FILE",
        help="TLS certificate chain (required for non-loopback binds)",
    )
    _ = parser.add_argument(
        "--tls-keyfile", default=None, metavar="FILE",
        help="TLS private key (required for non-loopback binds)",
    )
    _ = parser.add_argument(
        "--heartbeat-interval", type=int, default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        metavar="SECONDS",
        help="worker heartbeat interval the endpoint advertises",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = cast("dict[str, object]", vars(parser.parse_args(argv)))
    host = arguments["host"]
    if not isinstance(host, str) or not host:
        parser.error("--host must be a non-empty string")
    port = arguments["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        parser.error("--port must be an integer between 0 and 65535")
    store_value = arguments["store"]
    if not isinstance(store_value, str) or not store_value:
        parser.error("--store is required")
    heartbeat = arguments["heartbeat_interval"]
    if not isinstance(heartbeat, int) or isinstance(heartbeat, bool) or heartbeat < 1:
        parser.error("--heartbeat-interval must be a positive integer")
    certfile = arguments["tls_certfile"]
    keyfile = arguments["tls_keyfile"]
    if (certfile is None) != (keyfile is None):
        parser.error("--tls-certfile and --tls-keyfile must be used together")
    loopback = host in (BIND_HOST, "localhost", "::1")
    if not loopback and certfile is None:
        parser.error("a non-loopback bind requires --tls-certfile/--tls-keyfile")
    try:
        store = WorkerIdentityStore(store_value)
        # The dev entrypoint has no administrator configuration, and
        # configuration is the ONLY ownership source (D-049 amendment):
        # deny everything — no resource is reportable or executable here.
        # The composed server supplies the real resolver; tests construct
        # endpoints with explicit in-memory resolvers.
        endpoint = WorkerEndpoint(
            identity_store=store,
            registry=ResourceRegistry(),
            configured_owner=_unconfigured_owner,
            heartbeat_interval_seconds=heartbeat,
        )
        tls_context = (
            build_tls_context(str(certfile), str(keyfile))
            if certfile is not None and keyfile is not None
            else None
        )
        listener = endpoint.attach_listener(
            host=host, port=port, tls_context=tls_context
        )
    except (ValueError, OSError) as exc:
        print(f"worker endpoint: {exc}", file=sys.stderr)
        return 2
    scheme = "tls" if tls_context is not None else "plaintext-loopback"
    print(
        "scarcity-router worker endpoint "
        + f"(protocol v{WORKER_PROTOCOL_VERSION}) listening on "
        + f"{scheme}://{host}:{listener.bound_port}",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        listener.shutdown()
        store.close()
    return 0


def _unconfigured_owner(_resource_id: str) -> "str | None":
    """Deny-all ownership resolver (deployments without M09 configuration)."""
    return None


__all__ = [
    "BIND_HOST",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_PORT",
    "LIVENESS_GRACE_INTERVALS",
    "MAX_INTERRUPTED_ATTEMPT_RECORDS",
    "MAX_PENDING_ATTEMPTS_PER_SESSION",
    "MAX_SESSIONS",
    "AttemptOutcome",
    "PendingAttempt",
    "WorkerConnectionState",
    "WorkerDispatchError",
    "OwnerResolver",
    "WorkerEndpoint",
    "WorkerReportSink",
    "WorkerSession",
    "build_parser",
    "build_tls_context",
    "main",
]
