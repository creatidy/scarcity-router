"""Shared deterministic fixtures for the M05 worker/transport tests.

Everything here is synthetic and self-contained: no live providers, no
paid inference, no real credentials (every secret in this module is a
conspicuous ``SYNTHETIC``-marked fake), no wall-clock reads where
determinism matters (clocks are injected). Transports are in-memory by
default so the protocol, endpoint, adapter and runtime logic are all
exercised without real sockets; exactly one test file additionally
exercises a loopback TCP listener.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from scarcity_router.capacity import CapacitySnapshot, CapacityWindow
from scarcity_router.gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterResult,
    AdapterStreamChunk,
    CHUNK_FINISH,
    CHUNK_TEXT_DELTA,
    CallObservation,
)
from scarcity_router.resource_state import (
    ResourceHealth,
    ResourceIdentity,
    ResourceRegistration,
    ResourceRegistry,
    ResourceStateSnapshot,
)
from scarcity_router.worker_identity_store import WorkerIdentityStore
from scarcity_router.worker_local_adapters import LocalAdapterRegistry
from scarcity_router.worker_protocol import (
    CancelMessage,
    ErrorMessage,
    ExecuteChunkMessage,
    ExecuteMessage,
    ExecuteResultMessage,
    FrameReader,
    FrameTransport,
    FrameWriter,
    WorkerProtocolError,
    HelloMessage,
    HeartbeatAckMessage,
    HeartbeatMessage,
    PairRequestMessage,
    StateReportMessage,
    AttemptInterruptedMessage,
    parse_server_message,
)

# ── Deterministic time ────────────────────────────────────────────────────────

T_EVAL = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
T_NOW = "2026-09-20T12:00:00.000Z"

# Conspicuous synthetic credential material (never a real secret).
SYNTHETIC_CREDENTIAL = "SYNTHETIC-TEST-CREDENTIAL-7f3a9c"
SYNTHETIC_CODE = "SYNTHETIC-TEST-CODE-0000"


def fixed_clock() -> Callable[[], datetime]:
    return MutableClock()


def canonical(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class MutableClock:
    """A controllable wall clock (canonical datetime) for endpoint tests."""

    def __init__(self) -> None:
        self.now: datetime = T_EVAL

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FrozenMonotonic:
    """A controllable monotonic clock for liveness and backoff tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._value: float = start
        self._lock: threading.Lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += seconds


# ── In-memory transports ──────────────────────────────────────────────────────


class MemoryTransport:
    """One side of a deterministic in-memory byte pipe."""

    def __init__(self) -> None:
        self._buffer: bytearray = bytearray()
        self._condition: threading.Condition = threading.Condition()
        self._closed: bool = False
        self._peer: "MemoryTransport | None" = None

    @classmethod
    def pair(cls) -> tuple["MemoryTransport", "MemoryTransport"]:
        a, b = cls(), cls()
        a._peer, b._peer = b, a
        return a, b

    def recv_exact(self, size: int) -> bytes | None:
        with self._condition:
            while len(self._buffer) < size and not self._closed:
                _ = self._condition.wait(timeout=5.0)
            if len(self._buffer) >= size:
                data = bytes(self._buffer[:size])
                del self._buffer[:size]
                return data
            if self._buffer and self._closed:
                raise ConnectionError("transport closed with a partial frame pending")
            return None

    def send_all(self, data: bytes) -> None:
        peer = self._peer
        if peer is None or self._closed:
            raise ConnectionError("transport is closed")
        with peer._condition:
            peer._buffer.extend(data)
            peer._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        peer = self._peer
        if peer is not None:
            with peer._condition:
                peer._closed = True
                peer._condition.notify_all()


class NullTransport:
    """A transport that is immediately at EOF (connect-failure stand-in)."""

    def recv_exact(self, size: int) -> bytes | None:
        _ = size
        return None

    def send_all(self, data: bytes) -> None:
        _ = data
        raise ConnectionError("null transport")

    def close(self) -> None:
        return None


# ── Scripted worker peer (drives the server endpoint from tests) ─────────────


_EOF = object()
"""Inbox sentinel: the connection reached EOF (distinct from a timeout)."""


class ScriptedWorker:
    """A synchronous fake worker speaking the real protocol frames.

    The test drives it step by step while the server-side session runs on
    its own thread; every message is parsed strictly, so a server that
    misbehaves fails the test loudly.
    """

    def __init__(self, transport: FrameTransport) -> None:
        self._reader: FrameReader = FrameReader(transport)
        self._writer: FrameWriter = FrameWriter(transport)
        self.transport: FrameTransport = transport
        self.received_execute_count: int = 0
        self.executes: list[ExecuteMessage] = []
        self.cancels: list[str] = []
        self.interrupted_reports: list[str] = []
        self.server_errors: list[str] = []
        self._inbox: deque[object] = deque()
        self._inbox_cond: threading.Condition = threading.Condition()
        self._reader_thread: threading.Thread | None = None

    def start_reader(self) -> None:
        """Record incoming messages on a background thread.

        Cancels and errors are classified into lists; executes queue for
        :meth:`next_execute`. Needed whenever the server sends messages
        while the test thread is blocked elsewhere.
        """
        if self._reader_thread is not None:
            return

        def loop() -> None:
            while True:
                try:
                    message = self.read_server_message()
                except AssertionError:
                    return
                except WorkerProtocolError:
                    return
                with self._inbox_cond:
                    if message is None:
                        self._inbox.append(_EOF)
                        self._inbox_cond.notify_all()
                        return
                    if isinstance(message, CancelMessage):
                        self.cancels.append(message.attempt_id)
                        continue
                    if isinstance(message, ErrorMessage):
                        self.server_errors.append(message.code)
                        continue
                    if isinstance(message, HeartbeatAckMessage):
                        continue
                    self._inbox.append(message)
                    self._inbox_cond.notify_all()

        self._reader_thread = threading.Thread(target=loop, daemon=True)
        self._reader_thread.start()

    def wait_for_cancel(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancels:
                return True
            _ = time.sleep(0.005)
        return bool(self.cancels)

    def _unused(self) -> None:
        return None

    # -- handshake helpers -------------------------------------------------

    def send_hello(self, worker_id: str, credential: str) -> object:
        self._writer.write_message(
            HelloMessage(
                worker_id=worker_id,
                credential=credential,
                supported_versions=(1,),
            ).to_payload()
        )
        return self.read_server_message()

    def send_pair(self, pairing_code: str) -> object:
        self._writer.write_message(
            PairRequestMessage(
                pairing_code=pairing_code,
                supported_versions=(1,),
                device_label="test-worker",
            ).to_payload()
        )
        return self.read_server_message()

    def send_bad_version_hello(self, worker_id: str, credential: str) -> object:
        payload = HelloMessage(
            worker_id=worker_id, credential=credential, supported_versions=(99,)
        ).to_payload()
        payload["supported_versions"] = [99]
        self._writer.write_message(payload)
        return self.read_server_message()

    # -- session helpers ----------------------------------------------------

    def read_server_message(self) -> object:
        payload = self._reader.read_message()
        if payload is None:
            return None
        return parse_server_message(payload)

    def read_raw(self) -> dict[str, object] | None:
        return self._reader.read_message()

    def send_heartbeat(self, seq: int) -> object:
        self._writer.write_message(HeartbeatMessage(seq=seq).to_payload())
        return self.read_server_message()

    def send_state_report(self, report: dict[str, object]) -> object:
        """Send one report; returns the ack or the rejection error."""
        self._writer.write_message(StateReportMessage(report=report).to_payload())
        message = self.read_server_message()
        assert not isinstance(message, HeartbeatAckMessage), message
        return message

    def send_interrupted(self, attempt_id: str) -> None:
        self._writer.write_message(
            AttemptInterruptedMessage(attempt_id=attempt_id).to_payload()
        )

    def send_raw(self, payload: dict[str, object]) -> None:
        self._writer.write_message(payload)

    def next_execute(self, timeout: float = 10.0) -> ExecuteMessage:
        """The next execute, from the reader queue or by reading directly."""
        if self._reader_thread is not None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with self._inbox_cond:
                    if self._inbox:
                        message: object = self._inbox.popleft()
                    else:
                        _ = self._inbox_cond.wait(
                            timeout=min(0.05, max(0.0, deadline - time.monotonic()))
                        )
                        continue
                if message is _EOF:
                    break
                if isinstance(message, ErrorMessage):
                    raise AssertionError(f"server error: {message.code}")
                if isinstance(message, ExecuteMessage):
                    self.received_execute_count += 1
                    self.executes.append(message)
                    return message
            raise AssertionError("connection closed before an execute arrived")
        while True:
            message = self.read_server_message()
            if message is None:
                raise AssertionError("connection closed before an execute arrived")
            if isinstance(message, HeartbeatAckMessage):
                continue
            if isinstance(message, ErrorMessage):
                raise AssertionError(f"server error: {message.code} {message.message}")
            if isinstance(message, ExecuteMessage):
                self.received_execute_count += 1
                self.executes.append(message)
                return message

    def complete(
        self,
        execute: ExecuteMessage,
        *,
        content: str = "worker reply",
        stream: bool = True,
        prompt_tokens: int = 5,
        completion_tokens: int = 7,
    ) -> None:
        """Stream the conventional reply for one execute, then finish it."""
        if stream:
            self.send_chunk(execute.attempt_id, {"kind": "text_delta", "text": content[:4]})
            self.send_chunk(execute.attempt_id, {"kind": "text_delta", "text": content[4:]})
        self._writer.write_message(
            ExecuteResultMessage(
                attempt_id=execute.attempt_id,
                status="completed",
                calls=(
                    {
                        "call_index": 0,
                        "started_at": T_NOW,
                        "ended_at": T_NOW,
                        "status": "completed",
                        "provider_reported_usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                        },
                    },
                ),
                message={"role": "assistant", "content": content},
                finish_reason="stop",
            ).to_payload()
        )

    def send_chunk(self, attempt_id: str, chunk: dict[str, object]) -> None:
        self._writer.write_message(
            ExecuteChunkMessage(attempt_id=attempt_id, chunk=chunk).to_payload()
        )

    def fail(self, execute: ExecuteMessage, note: str) -> None:
        self._writer.write_message(
            ExecuteResultMessage(
                attempt_id=execute.attempt_id, status="failed", calls=(), note=note
            ).to_payload()
        )


# ── Synthetic local adapter (worker side) ─────────────────────────────────────


class SyntheticLocalAdapter:
    """A deterministic local adapter for runtime tests (test fixture only).

    Emits the conventional two-delta stream, honors the cancel event, and
    reports one scripted resource snapshot. Records every invocation so
    tests can assert that an allowlisted (or rejected) call did or did
    not reach local execution.
    """

    def __init__(
        self,
        *,
        adapter_id: str = "synthetic",
        resource_id: str = "synthetic-resource",
        behavior: "Callable[[AdapterCall, threading.Event], AdapterResult] | None" = None,
    ) -> None:
        self.adapter_id: str = adapter_id
        self.resource_ids: tuple[str, ...] = (resource_id,)
        self._resource_id: str = resource_id
        self.behavior: Callable[[AdapterCall, threading.Event], AdapterResult] | None = behavior
        self.invocations: list[AdapterCall] = []
        self.lock: threading.Lock = threading.Lock()

    def invoke(
        self,
        call: AdapterCall,
        *,
        cancel_event: threading.Event,
        deadline: str,
        emit: Callable[[AdapterStreamChunk], None],
    ) -> AdapterResult:
        _ = deadline
        with self.lock:
            self.invocations.append(call)
        if self.behavior is not None:
            return self.behavior(call, cancel_event)
        if cancel_event.is_set():
            return AdapterResult(status="cancelled", calls=())
        emit(AdapterStreamChunk(kind=CHUNK_TEXT_DELTA, text="syn"))
        emit(AdapterStreamChunk(kind=CHUNK_TEXT_DELTA, text="thetic"))
        emit(AdapterStreamChunk(kind=CHUNK_FINISH, finish_reason="stop"))
        return AdapterResult(
            status="completed",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at=T_NOW,
                    ended_at=T_NOW,
                    status="completed",
                ),
            ),
            message=_assistant_message("synthetic reply"),
            finish_reason="stop",
        )

    def resource_snapshots(self, observed_at: str) -> tuple[ResourceStateSnapshot, ...]:
        snapshot = CapacitySnapshot(
            schema_version=3,
            provider="synthetic",
            source="worker-local:synthetic",
            retrieved_at=observed_at,
            status="ok",
            windows=(
                CapacityWindow(
                    resource="tokens",
                    kind="weekly",
                    scope_id="synthetic-scope",
                    used_percent=10,
                    remaining_percent=90,
                ),
            ),
            diagnostics=(),
        )
        identity = ResourceIdentity(
            resource_id=self._resource_id,
            channel="worker_bridged",
            provider="synthetic",
            model="syn-model",
            entitlement="local_ungated",
        )
        from scarcity_router.resource_state import resource_snapshot_from_capacity

        return (resource_snapshot_from_capacity(snapshot, identity=identity),)


def _assistant_message(content: str) -> "AdapterMessage":
    from scarcity_router.gateway_adapters import AdapterMessage

    return AdapterMessage(role="assistant", content=content)


def build_local_registry(
    *, adapter: SyntheticLocalAdapter | None = None
) -> LocalAdapterRegistry:
    registry = LocalAdapterRegistry()
    registry.register(adapter if adapter is not None else SyntheticLocalAdapter())
    return registry


# ── Server-side world ─────────────────────────────────────────────────────────


def build_registry_with_resource(
    resource_id: str = "synthetic-resource",
    *,
    clock: "Callable[[], str] | None" = None,
) -> ResourceRegistry:
    registry = ResourceRegistry(clock=clock if clock is not None else (lambda: T_NOW))
    identity = ResourceIdentity(
        resource_id=resource_id,
        channel="worker_bridged",
        provider="synthetic",
        model="syn-model",
        entitlement="local_ungated",
    )
    registry.register(
        ResourceRegistration(identity=identity, freshness_ttl_seconds=300)
    )
    return registry


def realtime_canonical() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def build_identity_store(tmp_dir: str) -> WorkerIdentityStore:
    return WorkerIdentityStore(
        f"{tmp_dir}/identity/identities.db", clock=fixed_clock()
    )


def build_worker_report(
    *,
    worker_id: str,
    resource_id: str = "synthetic-resource",
    provider: str = "synthetic",
    model: str = "syn-model",
    entitlement: str = "local_ungated",
    reported_at: str = T_NOW,
) -> dict[str, object]:
    identity = ResourceIdentity(
        resource_id=resource_id,
        channel="worker_bridged",
        provider=provider,
        model=model,
        entitlement=entitlement,
    )
    snapshot = ResourceStateSnapshot(
        schema_version=1,
        identity=identity,
        observed_at=reported_at,
        health=ResourceHealth(status="ok", diagnostics=()),
        quota_facts=(),
        promotions=(),
    )
    return {
        "schema_version": 1,
        "worker_id": worker_id,
        "reported_at": reported_at,
        "resources": [snapshot.to_dict()],
    }


_ = deque

__all__ = [
    "SYNTHETIC_CODE",
    "SYNTHETIC_CREDENTIAL",
    "T_EVAL",
    "T_NOW",
    "FrozenMonotonic",
    "MemoryTransport",
    "MutableClock",
    "NullTransport",
    "ScriptedWorker",
    "SyntheticLocalAdapter",
    "build_identity_store",
    "build_local_registry",
    "build_registry_with_resource",
    "build_worker_report",
    "canonical",
    "realtime_canonical",
    "fixed_clock",
]
