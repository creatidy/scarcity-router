"""Worker protocol v1: versioned messages over bounded TLS frames (M05).

The native worker and the server's worker-protocol endpoint speak exactly
one versioned message protocol (D-043/D-044, issue #90) over a single
outbound, worker-initiated TLS connection. This module owns the complete
wire contract so both ends share one strict parser, one framing rule and
one closed message vocabulary; neither end ever improvises message shapes.

Transport decision (issue #90 evidence, per A0 "a simple versioned
protocol over TLS/WSS"): **length-prefixed JSON frames over verified TLS**
-- the "plain length-prefixed TLS framing" option the issue names as
acceptable. Rejected alternatives: RFC 6455 WebSocket framing (an HTTP
upgrade, per-message masking and fragmentation machinery this dedicated
machine-to-machine channel never benefits from); gRPC/HTTP2 (a new runtime
dependency, unjustified under D-013 for one internal channel). TLS itself
is always verified: the worker builds its context with
``ssl.create_default_context()`` (certificate and hostname verification,
no bypass option exists anywhere in this program), and the protocol layer
is deliberately transport-agnostic (it consumes any ``recv_exact``/
``send_all`` pair) so tests exercise it over in-memory transports
deterministically.

Framing: one frame is a 4-byte big-endian unsigned payload length followed
by exactly that many bytes of UTF-8 JSON. Payloads larger than
:data:`MAX_FRAME_BYTES` are a protocol failure, not a truncation risk.
Payloads are parsed strictly (duplicate object keys and non-finite
constants are rejected) and must be JSON objects with an exact key set for
their message type.

Versioning: :data:`WORKER_PROTOCOL_VERSION` is the version this build
speaks. The ``hello``/``pair_request`` handshake carries the worker's
supported-version list; the server selects one mutually supported version
(or fails the connection with ``protocol_version_unsupported``). An
incompatible version is a clean, explicit, safe failure on both ends --
never a best-effort parse of foreign frames.

The message vocabulary is CLOSED. Every message type below is a typed
record with an exact key set; an unknown ``type``, an unknown key or a
malformed value raises :class:`WorkerProtocolError` and the connection is
closed. There is deliberately NO shell, SSH, arbitrary-command or
file-transfer message: the only execution-carrying message is ``execute``,
whose payload is the M03 :class:`~scarcity_router.gateway_adapters.AdapterCall`
vocabulary plus a worker-local adapter identifier. The worker enforces its
own local allowlist even if the server asks for something else.
"""

from __future__ import annotations

import json
import socket
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol as StructuralProtocol, cast

from .gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterStreamChunk,
    AdapterToolCall,
    CallObservation,
    CALL_STATUSES,
    CHUNK_KINDS,
    FINISH_REASONS,
)
from .gateway_contracts import UsageTokens
from .gateway_validation import exact_shape, v_bool, v_int, v_safe_id, v_str, v_text
from .selection_app import load_strict_json
from .resource_state import ResourceIdentity
from .selection_types import ModelIdentity

# ── Versioning ────────────────────────────────────────────────────────────────

WORKER_PROTOCOL_VERSION = 1

# ── Framing bounds ────────────────────────────────────────────────────────────

# Bounded frames (D-043 strict parsing): 4-byte length prefix, then at most
# this many payload bytes. The gateway's default request-body admission limit
# is 1 MiB; the frame bound leaves bounded headroom for JSON encoding of one
# admitted call plus envelope, and nothing larger can ever enter a session.
MAX_FRAME_BYTES = 16 * 1_048_576

_LENGTH_PREFIX = struct.Struct(">I")

# ── Closed message-type vocabulary ────────────────────────────────────────────

MSG_HELLO = "hello"  # worker -> server: authenticate a paired device
MSG_PAIR_REQUEST = "pair_request"  # worker -> server: redeem a one-time code
MSG_HEARTBEAT = "heartbeat"  # worker -> server: liveness
MSG_STATE_REPORT = "state_report"  # worker -> server: M01 worker report
MSG_EXECUTE_CHUNK = "execute_chunk"  # worker -> server: one stream chunk
MSG_EXECUTE_RESULT = "execute_result"  # worker -> server: terminal result
MSG_ATTEMPT_INTERRUPTED = "attempt_interrupted"  # worker -> server: lost attempt
MSG_ROTATE_CREDENTIAL = "rotate_credential"  # worker -> server: rotate identity
MSG_HELLO_ACK = "hello_ack"  # server -> worker: session accepted
MSG_PAIR_RESULT = "pair_result"  # server -> worker: issued device identity
MSG_HEARTBEAT_ACK = "heartbeat_ack"  # server -> worker: liveness confirmed
MSG_STATE_REPORT_ACK = "state_report_ack"  # server -> worker: report applied
MSG_EXECUTE = "execute"  # server -> worker: run one allowlisted local adapter
MSG_CANCEL = "cancel"  # server -> worker: stop one attempt
MSG_CREDENTIAL_ROTATED = "credential_rotated"  # server -> worker: new credential
MSG_ERROR = "error"  # server -> worker: typed protocol error

WORKER_TO_SERVER_TYPES: frozenset[str] = frozenset({
    MSG_HELLO,
    MSG_PAIR_REQUEST,
    MSG_HEARTBEAT,
    MSG_STATE_REPORT,
    MSG_EXECUTE_CHUNK,
    MSG_EXECUTE_RESULT,
    MSG_ATTEMPT_INTERRUPTED,
    MSG_ROTATE_CREDENTIAL,
})

SERVER_TO_WORKER_TYPES: frozenset[str] = frozenset({
    MSG_HELLO_ACK,
    MSG_PAIR_RESULT,
    MSG_HEARTBEAT_ACK,
    MSG_STATE_REPORT_ACK,
    MSG_EXECUTE,
    MSG_CANCEL,
    MSG_CREDENTIAL_ROTATED,
    MSG_ERROR,
})

# ── Closed error-code vocabulary ──────────────────────────────────────────────

ERR_PROTOCOL_VERSION = "protocol_version_unsupported"
ERR_UNAUTHORIZED = "unauthorized"
ERR_CREDENTIAL_REVOKED = "credential_revoked"
ERR_PAIRING_INVALID = "pairing_code_invalid"
ERR_PAIRING_EXPIRED = "pairing_code_expired"
ERR_PAIRING_USED = "pairing_code_used"
ERR_PAIRING_UNAVAILABLE = "pairing_unavailable"
ERR_UNKNOWN_MESSAGE = "unknown_message_type"
ERR_MALFORMED = "malformed_message"
ERR_FRAME_TOO_LARGE = "frame_too_large"
ERR_ATTEMPT_UNKNOWN = "attempt_unknown"
ERR_INTERNAL = "internal_error"

PROTOCOL_ERROR_CODES: frozenset[str] = frozenset({
    ERR_PROTOCOL_VERSION,
    ERR_UNAUTHORIZED,
    ERR_CREDENTIAL_REVOKED,
    ERR_PAIRING_INVALID,
    ERR_PAIRING_EXPIRED,
    ERR_PAIRING_USED,
    ERR_PAIRING_UNAVAILABLE,
    ERR_UNKNOWN_MESSAGE,
    ERR_MALFORMED,
    ERR_FRAME_TOO_LARGE,
    ERR_ATTEMPT_UNKNOWN,
    ERR_INTERNAL,
})

# Closed execution-result status vocabulary (mirrors M03's AdapterResult
# statuses; the worker reports exactly what its local adapter reported).
EXECUTE_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

_MAX_VERSION_LIST = 8
_MAX_DEVICE_LABEL = 64
_MAX_NOTE = 200
_MAX_CHUNK_TEXT = 1_048_576
_MAX_TOOL_ARGUMENTS = 1_048_576
_MAX_IDENTIFIER_TEXT = 256


class WorkerProtocolError(Exception):
    """A fatal protocol failure; the connection must be closed.

    ``code`` is one of the closed :data:`PROTOCOL_ERROR_CODES` vocabulary;
    ``message`` is a safe structural string (never a payload excerpt, never
    a credential). Both ends translate every parse/validation failure into
    this error and close the session -- fail closed, never resynchronize
    mid-stream.
    """

    def __init__(self, code: str, message: str) -> None:
        if code not in PROTOCOL_ERROR_CODES:
            raise ValueError(f"worker_protocol_error: unknown code {code!r}")
        super().__init__(message)
        self.code: str = code
        self.message: str = message


# ── Transport seam ────────────────────────────────────────────────────────────


class FrameTransport(StructuralProtocol):
    """The byte-stream seam the framing layer consumes.

    A connected TLS socket (production) or an in-memory pair (tests)
    implements this. ``recv_exact`` returns exactly ``size`` bytes, or
    ``None`` on clean EOF; ``send_all`` writes everything or raises OSError.
    """

    def recv_exact(self, size: int) -> bytes | None: ...

    def send_all(self, data: bytes) -> None: ...

    def close(self) -> None: ...


def encode_frame(payload: Mapping[str, object]) -> bytes:
    """Encode one protocol message as a bounded length-prefixed frame."""
    try:
        encoded = json.dumps(
            payload, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        # RecursionError: a payload nested too deeply for the JSON encoder
        # (e.g. unbounded client-supplied structures). A frame that cannot
        # be encoded must become a typed protocol failure, never a thread
        # crash.
        raise WorkerProtocolError(
            ERR_MALFORMED, "frame encoding failed"
        ) from None
    if len(encoded) > MAX_FRAME_BYTES:
        raise WorkerProtocolError(
            ERR_FRAME_TOO_LARGE,
            f"frame payload exceeds the maximum of {MAX_FRAME_BYTES} bytes",
        )
    return _LENGTH_PREFIX.pack(len(encoded)) + encoded


def decode_frame(payload: bytes) -> dict[str, object]:
    """Decode one bounded frame payload into a strictly parsed JSON object."""
    if len(payload) > MAX_FRAME_BYTES:
        raise WorkerProtocolError(
            ERR_FRAME_TOO_LARGE,
            f"frame payload exceeds the maximum of {MAX_FRAME_BYTES} bytes",
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkerProtocolError(
            ERR_MALFORMED, "frame payload is not valid UTF-8"
        ) from None
    try:
        document = load_strict_json(text, label="worker frame")
    except ValueError as exc:
        raise WorkerProtocolError(ERR_MALFORMED, f"frame payload rejected: {exc}") from None
    except RecursionError:
        # json.loads raises RecursionError (not ValueError) on deeply
        # nested input. The frame must fail as a typed protocol error so
        # the session loop can answer and close cleanly instead of dying
        # with the transport open and attempts unresolved (a depth bomb
        # must never leak sessions or threads).
        raise WorkerProtocolError(
            ERR_MALFORMED, "frame payload exceeds the JSON nesting bound"
        ) from None
    if not isinstance(document, dict):
        raise WorkerProtocolError(ERR_MALFORMED, "frame payload must be a JSON object")
    return cast("dict[str, object]", document)


class FrameReader:
    """Reads length-prefixed protocol frames from a transport."""

    def __init__(self, transport: FrameTransport) -> None:
        self._transport: FrameTransport = transport

    def read_message(self) -> dict[str, object] | None:
        """One decoded message, or ``None`` on clean EOF between frames."""
        header = self._transport.recv_exact(_LENGTH_PREFIX.size)
        if header is None:
            return None
        if len(header) != _LENGTH_PREFIX.size:
            raise WorkerProtocolError(ERR_MALFORMED, "truncated frame header")
        unpacked = cast("tuple[int]", _LENGTH_PREFIX.unpack(header))
        length = unpacked[0]
        if length > MAX_FRAME_BYTES:
            raise WorkerProtocolError(
                ERR_FRAME_TOO_LARGE,
                f"declared frame length {length} exceeds the maximum",
            )
        if length == 0:
            raise WorkerProtocolError(ERR_MALFORMED, "zero-length frame")
        payload = self._transport.recv_exact(length)
        if payload is None or len(payload) != length:
            raise WorkerProtocolError(ERR_MALFORMED, "truncated frame payload")
        return decode_frame(payload)


class FrameWriter:
    """Writes length-prefixed protocol frames to a transport."""

    def __init__(self, transport: FrameTransport) -> None:
        self._transport: FrameTransport = transport

    def write_message(self, payload: Mapping[str, object]) -> None:
        self._transport.send_all(encode_frame(payload))


class SocketTransport:
    """The production :class:`FrameTransport` over a (TLS-wrapped) socket.

    ``recv_exact`` loops until the exact byte count or a clean EOF; a
    peer stall longer than ``recv_timeout_seconds`` (when set) raises
    :class:`TimeoutError` so sessions cannot block forever on a dead peer.
    """

    def __init__(self, sock: socket.socket, *, recv_timeout_seconds: float | None = None) -> None:
        self._socket: socket.socket = sock
        if recv_timeout_seconds is not None:
            self._socket.settimeout(recv_timeout_seconds)

    def recv_exact(self, size: int) -> bytes | None:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            try:
                chunk = self._socket.recv(min(remaining, 65_536))
            except TimeoutError as exc:
                raise TimeoutError("peer read timed out") from exc
            except OSError as exc:
                raise ConnectionError("peer read failed") from exc
            if not chunk:
                if chunks:
                    raise WorkerProtocolError(ERR_MALFORMED, "connection closed mid-frame")
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def send_all(self, data: bytes) -> None:
        try:
            self._socket.sendall(data)
        except TimeoutError as exc:
            raise TimeoutError("peer write timed out") from exc
        except OSError as exc:
            raise ConnectionError("peer write failed") from exc

    def close(self) -> None:
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass


# ── AdapterCall serialization (the one execution payload vocabulary) ──────────


def message_to_dict(message: AdapterMessage) -> dict[str, object]:
    """Serialize one M03 conversation message for the wire."""
    out: dict[str, object] = {"role": message.role}
    if message.content is not None:
        out["content"] = message.content
    if message.tool_call_id is not None:
        out["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        out["name"] = message.name
    if message.tool_calls:
        out["tool_calls"] = [
            {
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in message.tool_calls
        ]
    return out


def message_from_dict(d: object) -> AdapterMessage:
    """Rebuild one strictly validated M03 conversation message."""
    dd = _payload_shape(
        d,
        ("role",),
        ("content", "tool_calls", "tool_call_id", "name"),
        "protocol.message",
    )
    raw_calls = dd.get("tool_calls")
    tool_calls: tuple[AdapterToolCall, ...] = ()
    if raw_calls is not None:
        if not isinstance(raw_calls, list):
            raise WorkerProtocolError(
                ERR_MALFORMED, "protocol.message.tool_calls: expected a list"
            )
        calls: list[AdapterToolCall] = []
        for raw_call in cast("list[object]", raw_calls):
            call_doc = _payload_shape(raw_call, ("id", "name", "arguments"), (), "protocol.tool_call")
            try:
                calls.append(
                    AdapterToolCall(
                        id=v_text(call_doc["id"], "protocol.tool_call.id", max_len=256),
                        name=v_text(call_doc["name"], "protocol.tool_call.name", max_len=256),
                        arguments=v_text(
                            call_doc["arguments"], "protocol.tool_call.arguments", max_len=1_048_576
                        ),
                    )
                )
            except ValueError as exc:
                raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None
        tool_calls = tuple(calls)
    try:
        return AdapterMessage(
            role=v_safe_id(dd["role"], "protocol.message.role"),
            content=(
                None
                if dd.get("content") is None
                else v_str(dd["content"], "protocol.message.content")
            ),
            tool_calls=tool_calls,
            tool_call_id=(
                None
                if dd.get("tool_call_id") is None
                else v_text(dd["tool_call_id"], "protocol.message.tool_call_id", max_len=256)
            ),
            name=(
                None
                if dd.get("name") is None
                else v_text(dd["name"], "protocol.message.name", max_len=256)
            ),
        )
    except ValueError as exc:
        raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


def adapter_call_to_dict(call: AdapterCall) -> dict[str, object]:
    """Serialize one admitted call for the ``execute`` message payload.

    This is the ENTIRE execution surface a server can express to a worker:
    the typed M03 call vocabulary plus identities. No field for an
    executable path, a shell command, environment variables, a filesystem
    root or subprocess flags exists in this schema, and the strict parser
    rejects any additional key -- a compromised server cannot smuggle an
    arbitrary-command channel into a worker.
    """
    document: dict[str, object] = {
        "resource": call.resource.to_dict(),
        "model": call.model.to_dict(),
        "messages": [message_to_dict(message) for message in call.messages],
        "stream": call.stream,
        "tools": [dict(tool) for tool in call.tools],
        "tool_choice": call.tool_choice,
        "response_format": (
            None if call.response_format is None else dict(call.response_format)
        ),
        "reasoning_effort": call.reasoning_effort,
        "max_output_tokens": call.max_output_tokens,
        "generation_params": dict(call.generation_params),
    }
    return document


def adapter_call_from_dict(d: object) -> AdapterCall:
    """Rebuild one strictly validated :class:`AdapterCall` (worker side)."""
    dd = _payload_shape(
        d,
        ("resource", "model", "messages", "stream", "tools", "tool_choice",
         "response_format", "reasoning_effort", "max_output_tokens",
         "generation_params"),
        (),
        "protocol.call",
    )
    raw_messages = dd["messages"]
    if not isinstance(raw_messages, list):
        raise WorkerProtocolError(ERR_MALFORMED, "protocol.call.messages: expected a list")
    raw_tools = dd["tools"]
    if not isinstance(raw_tools, list):
        raise WorkerProtocolError(ERR_MALFORMED, "protocol.call.tools: expected a list")
    try:
        tools: list[Mapping[str, object]] = []
        for raw_tool in cast("list[object]", raw_tools):
            if not isinstance(raw_tool, Mapping):
                raise WorkerProtocolError(ERR_MALFORMED, "protocol.call.tools: expected objects")
            tools.append(dict(cast("Mapping[str, object]", raw_tool)))
        return AdapterCall(
            resource=_resource_identity_from_dict(dd["resource"]),
            model=ModelIdentity.from_dict(dd["model"]),
            messages=tuple(
                message_from_dict(item) for item in cast("list[object]", raw_messages)
            ),
            stream=v_bool(dd["stream"], "protocol.call.stream"),
            tools=tuple(tools),
            tool_choice=dd["tool_choice"],
            response_format=(
                None
                if dd["response_format"] is None
                else dict(cast("Mapping[str, object]", dd["response_format"]))
            ),
            reasoning_effort=(
                None
                if dd["reasoning_effort"] is None
                else v_safe_id(dd["reasoning_effort"], "protocol.call.reasoning_effort")
            ),
            max_output_tokens=(
                None
                if dd["max_output_tokens"] is None
                else v_int(dd["max_output_tokens"], "protocol.call.max_output_tokens", lo=1)
            ),
            generation_params=dict(cast("Mapping[str, object]", dd["generation_params"])),
        )
    except WorkerProtocolError:
        raise
    except (ValueError, TypeError) as exc:
        raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


def _resource_identity_from_dict(d: object) -> ResourceIdentity:
    """Validate the resource-identity document through the M01 contract."""
    try:
        return ResourceIdentity.from_dict(d)
    except ValueError as exc:
        raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


def usage_to_dict(usage: UsageTokens) -> dict[str, object]:
    """The wire form: exactly the two measured fields (never a computed
    total; the strict reader rejects unknown keys by design)."""
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
    }


def usage_from_dict(d: object) -> UsageTokens:
    try:
        return UsageTokens.from_dict(d)
    except (ValueError, TypeError) as exc:
        raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


def chunk_to_dict(chunk: AdapterStreamChunk) -> dict[str, object]:
    """Serialize one normalized stream chunk for ``execute_chunk``."""
    out: dict[str, object] = {"kind": chunk.kind}
    if chunk.text is not None:
        out["text"] = chunk.text
    if chunk.tool_call is not None:
        out["tool_call"] = {
            "id": chunk.tool_call.id,
            "name": chunk.tool_call.name,
            "arguments": chunk.tool_call.arguments,
        }
    if chunk.finish_reason is not None:
        out["finish_reason"] = chunk.finish_reason
    if chunk.usage is not None:
        out["usage"] = usage_to_dict(chunk.usage)
    return out


def chunk_from_dict(d: object) -> AdapterStreamChunk:
    """Rebuild one strictly validated normalized stream chunk."""
    dd = _payload_shape(
        d, ("kind",), ("text", "tool_call", "finish_reason", "usage"), "protocol.chunk"
    )
    kind = dd["kind"]
    if kind not in CHUNK_KINDS:
        raise WorkerProtocolError(
            ERR_MALFORMED, f"protocol.chunk.kind: {kind!r} is not a chunk kind"
        )
    try:
        raw_tool_call = dd.get("tool_call")
        tool_call: AdapterToolCall | None = None
        if raw_tool_call is not None:
            call_doc = _payload_shape(
                raw_tool_call, ("id", "name", "arguments"), (), "protocol.chunk.tool_call"
            )
            tool_call = AdapterToolCall(
                id=v_text(call_doc["id"], "protocol.chunk.tool_call.id", max_len=_MAX_IDENTIFIER_TEXT),
                name=v_text(call_doc["name"], "protocol.chunk.tool_call.name", max_len=_MAX_IDENTIFIER_TEXT),
                arguments=v_text(
                    call_doc["arguments"],
                    "protocol.chunk.tool_call.arguments",
                    max_len=_MAX_TOOL_ARGUMENTS,
                ),
            )
        finish_reason = dd.get("finish_reason")
        if finish_reason is not None:
            if finish_reason not in FINISH_REASONS:
                raise WorkerProtocolError(
                    ERR_MALFORMED,
                    f"protocol.chunk.finish_reason: {finish_reason!r} is not a finish reason",
                )
        text = dd.get("text")
        if text is not None:
            _ = v_text(text, "protocol.chunk.text", max_len=_MAX_CHUNK_TEXT)
        usage = None if dd.get("usage") is None else usage_from_dict(dd["usage"])
        return AdapterStreamChunk(
            kind=v_str(kind, "protocol.chunk.kind"),
            text=cast("str | None", text),
            tool_call=tool_call,
            finish_reason=cast("str | None", finish_reason),
            usage=usage,
        )
    except WorkerProtocolError:
        raise
    except ValueError as exc:
        raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


def call_observation_to_dict(observation: CallObservation) -> dict[str, object]:
    """Serialize one M03 call observation (honest per-call usage, D-043)."""
    out: dict[str, object] = {
        "call_index": observation.call_index,
        "started_at": observation.started_at,
        "ended_at": observation.ended_at,
        "status": observation.status,
    }
    if observation.provider_reported_usage is not None:
        out["provider_reported_usage"] = usage_to_dict(observation.provider_reported_usage)
    if observation.estimated_usage is not None:
        out["estimated_usage"] = usage_to_dict(observation.estimated_usage)
    if observation.note is not None:
        out["note"] = observation.note
    return out


def call_observation_from_dict(d: object) -> CallObservation:
    """Rebuild one strictly validated call observation."""
    dd = _payload_shape(
        d,
        ("call_index", "started_at", "ended_at", "status"),
        ("provider_reported_usage", "estimated_usage", "note"),
        "protocol.call_observation",
    )
    try:
        status = dd["status"]
        if status not in CALL_STATUSES:
            raise WorkerProtocolError(
                ERR_MALFORMED, f"protocol.call_observation.status: {status!r} unknown"
            )
        reported = dd.get("provider_reported_usage")
        estimated = dd.get("estimated_usage")
        note = dd.get("note")
        return CallObservation(
            call_index=v_int(dd["call_index"], "protocol.call_observation.call_index", lo=0),
            started_at=v_str(dd["started_at"], "protocol.call_observation.started_at"),
            ended_at=v_str(dd["ended_at"], "protocol.call_observation.ended_at"),
            status=v_str(status, "protocol.call_observation.status"),
            provider_reported_usage=None if reported is None else usage_from_dict(reported),
            estimated_usage=None if estimated is None else usage_from_dict(estimated),
            note=(
                None
                if note is None
                else v_text(note, "protocol.call_observation.note", max_len=_MAX_NOTE)
            ),
        )
    except WorkerProtocolError:
        raise
    except ValueError as exc:
        raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


# ── Typed handshake / session records ─────────────────────────────────────────


def _payload_shape(
    d: object,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    label: str,
) -> Mapping[str, object]:
    """The strict exact-key-set check; every failure is a protocol error.

    Session loops rely on one typed failure kind
    (:class:`WorkerProtocolError`) -- a raw validation ValueError must
    never escape a parse path.
    """
    try:
        return exact_shape(d, required, optional, label)
    except WorkerProtocolError:
        raise
    except ValueError as exc:
        raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class HelloMessage:
    """Worker -> server: authenticate a paired per-device identity."""

    worker_id: str
    credential: str
    supported_versions: tuple[int, ...]
    device_label: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": MSG_HELLO,
            "worker_id": self.worker_id,
            "credential": self.credential,
            "supported_versions": list(self.supported_versions),
        }
        if self.device_label is not None:
            payload["device_label"] = self.device_label
        return payload

    @classmethod
    def from_payload(cls, d: object) -> "HelloMessage":
        dd = _payload_shape(
            d, ("type", "worker_id", "credential", "supported_versions"),
            ("device_label",), "hello",
        )
        _typed(dd, MSG_HELLO)
        versions = _version_list(dd["supported_versions"])
        try:
            return cls(
                worker_id=v_safe_id(dd["worker_id"], "hello.worker_id"),
                credential=v_text(dd["credential"], "hello.credential", max_len=512),
                supported_versions=versions,
                device_label=(
                    None
                    if dd.get("device_label") is None
                    else v_safe_id(dd["device_label"], "hello.device_label")
                ),
            )
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class PairRequestMessage:
    """Worker -> server: redeem a short-lived one-time pairing code."""

    pairing_code: str
    supported_versions: tuple[int, ...]
    device_label: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": MSG_PAIR_REQUEST,
            "pairing_code": self.pairing_code,
            "supported_versions": list(self.supported_versions),
        }
        if self.device_label is not None:
            payload["device_label"] = self.device_label
        return payload

    @classmethod
    def from_payload(cls, d: object) -> "PairRequestMessage":
        dd = _payload_shape(
            d, ("type", "pairing_code", "supported_versions"),
            ("device_label",), "pair_request",
        )
        _typed(dd, MSG_PAIR_REQUEST)
        try:
            return cls(
                pairing_code=v_text(dd["pairing_code"], "pair_request.pairing_code", max_len=512),
                supported_versions=_version_list(dd["supported_versions"]),
                device_label=(
                    None
                    if dd.get("device_label") is None
                    else v_safe_id(dd["device_label"], "pair_request.device_label")
                ),
            )
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class HelloAckMessage:
    """Server -> worker: session accepted at the negotiated version."""

    negotiated_version: int
    heartbeat_interval_seconds: int

    def to_payload(self) -> dict[str, object]:
        return {
            "type": MSG_HELLO_ACK,
            "negotiated_version": self.negotiated_version,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
        }

    @classmethod
    def from_payload(cls, d: object) -> "HelloAckMessage":
        dd = _payload_shape(
            d, ("type", "negotiated_version", "heartbeat_interval_seconds"), (), "hello_ack"
        )
        _typed(dd, MSG_HELLO_ACK)
        try:
            return cls(
                negotiated_version=v_int(dd["negotiated_version"], "hello_ack.negotiated_version", lo=1),
                heartbeat_interval_seconds=v_int(
                    dd["heartbeat_interval_seconds"], "hello_ack.heartbeat_interval_seconds", lo=1
                ),
            )
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class PairResultMessage:
    """Server -> worker: the issued per-device identity (over verified TLS)."""

    negotiated_version: int
    worker_id: str
    credential: str
    heartbeat_interval_seconds: int

    def to_payload(self) -> dict[str, object]:
        return {
            "type": MSG_PAIR_RESULT,
            "negotiated_version": self.negotiated_version,
            "worker_id": self.worker_id,
            "credential": self.credential,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
        }

    @classmethod
    def from_payload(cls, d: object) -> "PairResultMessage":
        dd = _payload_shape(
            d,
            ("type", "negotiated_version", "worker_id", "credential",
             "heartbeat_interval_seconds"),
            (), "pair_result",
        )
        _typed(dd, MSG_PAIR_RESULT)
        try:
            return cls(
                negotiated_version=v_int(dd["negotiated_version"], "pair_result.negotiated_version", lo=1),
                worker_id=v_safe_id(dd["worker_id"], "pair_result.worker_id"),
                credential=v_text(dd["credential"], "pair_result.credential", max_len=512),
                heartbeat_interval_seconds=v_int(
                    dd["heartbeat_interval_seconds"], "pair_result.heartbeat_interval_seconds", lo=1
                ),
            )
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class HeartbeatMessage:
    """Worker -> server: liveness with a per-session sequence number."""

    seq: int

    def to_payload(self) -> dict[str, object]:
        return {"type": MSG_HEARTBEAT, "seq": self.seq}

    @classmethod
    def from_payload(cls, d: object) -> "HeartbeatMessage":
        dd = _payload_shape(d, ("type", "seq"), (), "heartbeat")
        _typed(dd, MSG_HEARTBEAT)
        try:
            return cls(seq=v_int(dd["seq"], "heartbeat.seq", lo=0))
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class HeartbeatAckMessage:
    """Server -> worker: liveness confirmed (echoes the sequence number)."""

    seq: int

    def to_payload(self) -> dict[str, object]:
        return {"type": MSG_HEARTBEAT_ACK, "seq": self.seq}

    @classmethod
    def from_payload(cls, d: object) -> "HeartbeatAckMessage":
        dd = _payload_shape(d, ("type", "seq"), (), "heartbeat_ack")
        _typed(dd, MSG_HEARTBEAT_ACK)
        try:
            return cls(seq=v_int(dd["seq"], "heartbeat_ack.seq", lo=0))
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class StateReportMessage:
    """Worker -> server: one M01 :class:`WorkerStateReport` document.

    The report document is validated by the M01 contract itself
    (``WorkerStateReport.from_dict``) when the server applies it through
    ``ResourceRegistry.apply_worker_report`` -- this module deliberately
    does not re-implement any of that normalization.
    """

    report: Mapping[str, object]

    def to_payload(self) -> dict[str, object]:
        return {"type": MSG_STATE_REPORT, "report": dict(self.report)}

    @classmethod
    def from_payload(cls, d: object) -> "StateReportMessage":
        dd = _payload_shape(d, ("type", "report"), (), "state_report")
        _typed(dd, MSG_STATE_REPORT)
        if not isinstance(dd["report"], Mapping):
            raise WorkerProtocolError(ERR_MALFORMED, "state_report.report: expected an object")
        return cls(report=cast("Mapping[str, object]", dd["report"]))


@dataclass(frozen=True)
class StateReportAckMessage:
    """Server -> worker: the state report was applied to the registry."""

    def to_payload(self) -> dict[str, object]:
        return {"type": MSG_STATE_REPORT_ACK}

    @classmethod
    def from_payload(cls, d: object) -> "StateReportAckMessage":
        dd = _payload_shape(d, ("type",), (), "state_report_ack")
        _typed(dd, MSG_STATE_REPORT_ACK)
        return cls()


@dataclass(frozen=True)
class ExecuteMessage:
    """Server -> worker: run one allowlisted local adapter for one attempt.

    ``attempt_id`` is server-issued and unique per dispatch (request id +
    attempt id are the explicit identity pair that makes duplicate
    inference impossible to trigger silently). ``adapter_id`` names a
    worker-local adapter; the worker enforces its own allowlist against it.
    ``deadline`` is the absolute canonical admission deadline the worker
    must honor. ``call`` is the strict :class:`AdapterCall` vocabulary.
    """

    request_id: str
    attempt_id: str
    adapter_id: str
    deadline: str
    call: AdapterCall

    def to_payload(self) -> dict[str, object]:
        return {
            "type": MSG_EXECUTE,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "adapter_id": self.adapter_id,
            "deadline": self.deadline,
            "call": adapter_call_to_dict(self.call),
        }

    @classmethod
    def from_payload(cls, d: object) -> "ExecuteMessage":
        dd = _payload_shape(
            d, ("type", "request_id", "attempt_id", "adapter_id", "deadline", "call"),
            (), "execute",
        )
        _typed(dd, MSG_EXECUTE)
        try:
            return cls(
                request_id=v_safe_id(dd["request_id"], "execute.request_id"),
                attempt_id=v_safe_id(dd["attempt_id"], "execute.attempt_id"),
                adapter_id=v_safe_id(dd["adapter_id"], "execute.adapter_id"),
                deadline=v_str(dd["deadline"], "execute.deadline"),
                call=adapter_call_from_dict(dd["call"]),
            )
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class CancelMessage:
    """Server -> worker: stop one in-flight attempt (D-043 propagation)."""

    attempt_id: str

    def to_payload(self) -> dict[str, object]:
        return {"type": MSG_CANCEL, "attempt_id": self.attempt_id}

    @classmethod
    def from_payload(cls, d: object) -> "CancelMessage":
        dd = _payload_shape(d, ("type", "attempt_id"), (), "cancel")
        _typed(dd, MSG_CANCEL)
        try:
            return cls(attempt_id=v_safe_id(dd["attempt_id"], "cancel.attempt_id"))
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class ExecuteChunkMessage:
    """Worker -> server: one normalized stream chunk for one attempt."""

    attempt_id: str
    chunk: Mapping[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "type": MSG_EXECUTE_CHUNK,
            "attempt_id": self.attempt_id,
            "chunk": dict(self.chunk),
        }

    @classmethod
    def from_payload(cls, d: object) -> "ExecuteChunkMessage":
        dd = _payload_shape(d, ("type", "attempt_id", "chunk"), (), "execute_chunk")
        _typed(dd, MSG_EXECUTE_CHUNK)
        if not isinstance(dd["chunk"], Mapping):
            raise WorkerProtocolError(ERR_MALFORMED, "execute_chunk.chunk: expected an object")
        try:
            return cls(
                attempt_id=v_safe_id(dd["attempt_id"], "execute_chunk.attempt_id"),
                chunk=cast("Mapping[str, object]", dd["chunk"]),
            )
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class ExecuteResultMessage:
    """Worker -> server: the terminal result of one attempt.

    Exactly one ``execute_result`` is sent per attempt. ``calls`` carries
    the M03 :class:`CallObservation` documents so worker-side usage stays
    honest and distinguishable end to end.
    """

    attempt_id: str
    status: str
    calls: tuple[Mapping[str, object], ...]
    message: Mapping[str, object] | None = None
    finish_reason: str | None = None
    note: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": MSG_EXECUTE_RESULT,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "calls": [dict(call) for call in self.calls],
        }
        if self.message is not None:
            payload["message"] = dict(self.message)
        if self.finish_reason is not None:
            payload["finish_reason"] = self.finish_reason
        if self.note is not None:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_payload(cls, d: object) -> "ExecuteResultMessage":
        dd = _payload_shape(
            d, ("type", "attempt_id", "status", "calls"),
            ("message", "finish_reason", "note"), "execute_result",
        )
        _typed(dd, MSG_EXECUTE_RESULT)
        raw_calls = dd["calls"]
        if not isinstance(raw_calls, list):
            raise WorkerProtocolError(ERR_MALFORMED, "execute_result.calls: expected a list")
        for raw_call in cast("list[object]", raw_calls):
            if not isinstance(raw_call, Mapping):
                raise WorkerProtocolError(ERR_MALFORMED, "execute_result.calls: expected objects")
        status = v_str(dd["status"], "execute_result.status")
        if status not in EXECUTE_STATUSES:
            raise WorkerProtocolError(
                ERR_MALFORMED, f"execute_result.status: {status!r} not in {sorted(EXECUTE_STATUSES)}"
            )
        try:
            return cls(
                attempt_id=v_safe_id(dd["attempt_id"], "execute_result.attempt_id"),
                status=status,
                calls=tuple(cast("list[Mapping[str, object]]", raw_calls)),
                message=(
                    None
                    if dd.get("message") is None
                    else cast("Mapping[str, object]", dd["message"])
                ),
                finish_reason=(
                    None
                    if dd.get("finish_reason") is None
                    else v_safe_id(dd["finish_reason"], "execute_result.finish_reason")
                ),
                note=(
                    None
                    if dd.get("note") is None
                    else v_text(dd["note"], "execute_result.note", max_len=_MAX_NOTE)
                ),
            )
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class AttemptInterruptedMessage:
    """Worker -> server: a previously started attempt whose fate is unknown.

    Sent on reconnect for every attempt that was in flight when the
    connection dropped. The server resolves such attempts as ambiguous
    execution state (D-043) -- never restarted, never retried blindly.
    """

    attempt_id: str

    def to_payload(self) -> dict[str, object]:
        return {"type": MSG_ATTEMPT_INTERRUPTED, "attempt_id": self.attempt_id}

    @classmethod
    def from_payload(cls, d: object) -> "AttemptInterruptedMessage":
        dd = _payload_shape(d, ("type", "attempt_id"), (), "attempt_interrupted")
        _typed(dd, MSG_ATTEMPT_INTERRUPTED)
        try:
            return cls(attempt_id=v_safe_id(dd["attempt_id"], "attempt_interrupted.attempt_id"))
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class RotateCredentialMessage:
    """Worker -> server: rotate this device's credential (authenticated)."""

    def to_payload(self) -> dict[str, object]:
        return {"type": MSG_ROTATE_CREDENTIAL}

    @classmethod
    def from_payload(cls, d: object) -> "RotateCredentialMessage":
        dd = _payload_shape(d, ("type",), (), "rotate_credential")
        _typed(dd, MSG_ROTATE_CREDENTIAL)
        return cls()


@dataclass(frozen=True)
class CredentialRotatedMessage:
    """Server -> worker: the new per-device credential (over verified TLS)."""

    credential: str

    def to_payload(self) -> dict[str, object]:
        return {"type": MSG_CREDENTIAL_ROTATED, "credential": self.credential}

    @classmethod
    def from_payload(cls, d: object) -> "CredentialRotatedMessage":
        dd = _payload_shape(d, ("type", "credential"), (), "credential_rotated")
        _typed(dd, MSG_CREDENTIAL_ROTATED)
        try:
            return cls(credential=v_text(dd["credential"], "credential_rotated.credential", max_len=512))
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


@dataclass(frozen=True)
class ErrorMessage:
    """Server -> worker: one typed protocol error.

    ``fatal`` marks errors that end the session (authentication, version,
    pairing failures); non-fatal errors (for example a rejected state
    report) leave the session up.
    """

    code: str
    message: str
    fatal: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "type": MSG_ERROR,
            "code": self.code,
            "message": self.message,
            "fatal": self.fatal,
        }

    @classmethod
    def from_payload(cls, d: object) -> "ErrorMessage":
        dd = _payload_shape(d, ("type", "code", "message", "fatal"), (), "error")
        _typed(dd, MSG_ERROR)
        code = v_str(dd["code"], "error.code")
        if code not in PROTOCOL_ERROR_CODES:
            raise WorkerProtocolError(
                ERR_MALFORMED, f"error.code: {code!r} is not a protocol error code"
            )
        try:
            return cls(
                code=code,
                message=v_text(dd["message"], "error.message", max_len=500),
                fatal=v_bool(dd["fatal"], "error.fatal"),
            )
        except ValueError as exc:
            raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


def _typed(dd: Mapping[str, object], expected: str) -> None:
    actual = dd.get("type")
    if actual != expected:
        raise WorkerProtocolError(
            ERR_MALFORMED, f"message type {actual!r} does not match {expected!r}"
        )


def _version_list(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise WorkerProtocolError(ERR_MALFORMED, "supported_versions: expected a list")
    items = cast("list[object]", value)
    if not items or len(items) > _MAX_VERSION_LIST:
        raise WorkerProtocolError(
            ERR_MALFORMED,
            f"supported_versions: expected 1..{_MAX_VERSION_LIST} entries",
        )
    versions: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise WorkerProtocolError(
                ERR_MALFORMED, "supported_versions: expected positive integers"
            )
        versions.append(item)
    ordered = tuple(sorted(set(versions), reverse=True))
    return ordered


def negotiate_version(supported: tuple[int, ...], offered: tuple[int, ...]) -> int:
    """Pick the highest mutually supported protocol version.

    Raises :class:`WorkerProtocolError` (``protocol_version_unsupported``)
    when the sets are disjoint: incompatible versions fail safely on both
    ends with an explicit error, never a guessed interpretation.
    """
    common = sorted(set(supported) & set(offered), reverse=True)
    if not common:
        raise WorkerProtocolError(
            ERR_PROTOCOL_VERSION,
            "no mutually supported protocol version "
            + f"(server speaks {sorted(supported)}; worker offered {sorted(offered)})",
        )
    return common[0]


def parse_worker_message(payload: Mapping[str, object]) -> object:
    """Strictly parse one worker->server message into its typed record.

    Unknown ``type`` values -- including any hypothetical shell/ssh or
    arbitrary-command type -- raise :class:`WorkerProtocolError`: the
    vocabulary is closed and there is no extension path without a protocol
    version bump.
    """
    message_type = payload.get("type")
    if message_type == MSG_HELLO:
        return HelloMessage.from_payload(payload)
    if message_type == MSG_PAIR_REQUEST:
        return PairRequestMessage.from_payload(payload)
    if message_type == MSG_HEARTBEAT:
        return HeartbeatMessage.from_payload(payload)
    if message_type == MSG_STATE_REPORT:
        return StateReportMessage.from_payload(payload)
    if message_type == MSG_EXECUTE_CHUNK:
        return ExecuteChunkMessage.from_payload(payload)
    if message_type == MSG_EXECUTE_RESULT:
        return ExecuteResultMessage.from_payload(payload)
    if message_type == MSG_ATTEMPT_INTERRUPTED:
        return AttemptInterruptedMessage.from_payload(payload)
    if message_type == MSG_ROTATE_CREDENTIAL:
        return RotateCredentialMessage.from_payload(payload)
    raise WorkerProtocolError(ERR_UNKNOWN_MESSAGE, f"unknown worker message type {message_type!r}")


def parse_server_message(payload: Mapping[str, object]) -> object:
    """Strictly parse one server->worker message into its typed record."""
    message_type = payload.get("type")
    if message_type == MSG_HELLO_ACK:
        return HelloAckMessage.from_payload(payload)
    if message_type == MSG_PAIR_RESULT:
        return PairResultMessage.from_payload(payload)
    if message_type == MSG_HEARTBEAT_ACK:
        return HeartbeatAckMessage.from_payload(payload)
    if message_type == MSG_STATE_REPORT_ACK:
        return StateReportAckMessage.from_payload(payload)
    if message_type == MSG_EXECUTE:
        return ExecuteMessage.from_payload(payload)
    if message_type == MSG_CANCEL:
        return CancelMessage.from_payload(payload)
    if message_type == MSG_CREDENTIAL_ROTATED:
        return CredentialRotatedMessage.from_payload(payload)
    if message_type == MSG_ERROR:
        return ErrorMessage.from_payload(payload)
    raise WorkerProtocolError(ERR_UNKNOWN_MESSAGE, f"unknown server message type {message_type!r}")


def safe_identifier(value: str, field: str) -> str:
    """Validate one safe protocol identifier (shared grammar)."""
    try:
        return v_safe_id(value, field)
    except ValueError as exc:
        raise WorkerProtocolError(ERR_MALFORMED, str(exc)) from None


__all__ = [
    "ERR_ATTEMPT_UNKNOWN",
    "ERR_CREDENTIAL_REVOKED",
    "ERR_FRAME_TOO_LARGE",
    "ERR_INTERNAL",
    "ERR_MALFORMED",
    "ERR_PAIRING_EXPIRED",
    "ERR_PAIRING_INVALID",
    "ERR_PAIRING_UNAVAILABLE",
    "ERR_PAIRING_USED",
    "ERR_PROTOCOL_VERSION",
    "ERR_UNAUTHORIZED",
    "ERR_UNKNOWN_MESSAGE",
    "MAX_FRAME_BYTES",
    "MSG_ATTEMPT_INTERRUPTED",
    "MSG_CANCEL",
    "MSG_CREDENTIAL_ROTATED",
    "MSG_EXECUTE",
    "MSG_EXECUTE_CHUNK",
    "MSG_EXECUTE_RESULT",
    "MSG_ERROR",
    "MSG_HELLO",
    "MSG_HELLO_ACK",
    "MSG_HEARTBEAT",
    "MSG_HEARTBEAT_ACK",
    "MSG_PAIR_REQUEST",
    "MSG_PAIR_RESULT",
    "MSG_ROTATE_CREDENTIAL",
    "MSG_STATE_REPORT",
    "PROTOCOL_ERROR_CODES",
    "AttemptInterruptedMessage",
    "CancelMessage",
    "CredentialRotatedMessage",
    "ErrorMessage",
    "ExecuteChunkMessage",
    "ExecuteMessage",
    "ExecuteResultMessage",
    "FrameReader",
    "FrameTransport",
    "FrameWriter",
    "HelloAckMessage",
    "HelloMessage",
    "HeartbeatAckMessage",
    "HeartbeatMessage",
    "PairRequestMessage",
    "PairResultMessage",
    "RotateCredentialMessage",
    "SERVER_TO_WORKER_TYPES",
    "SocketTransport",
    "StateReportAckMessage",
    "StateReportMessage",
    "WORKER_PROTOCOL_VERSION",
    "WORKER_TO_SERVER_TYPES",
    "WorkerProtocolError",
    "adapter_call_from_dict",
    "adapter_call_to_dict",
    "call_observation_from_dict",
    "call_observation_to_dict",
    "chunk_from_dict",
    "chunk_to_dict",
    "decode_frame",
    "encode_frame",
    "message_from_dict",
    "message_to_dict",
    "negotiate_version",
    "parse_server_message",
    "parse_worker_message",
    "safe_identifier",
    "usage_from_dict",
    "usage_to_dict",
]
