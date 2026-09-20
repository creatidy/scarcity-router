"""Deterministic synthetic OpenAI-compatible provider server (M04 tests).

Everything here is synthetic and loopback-only: an in-thread
``http.server`` speaking just enough HTTP for the M04 adapter tests. No
live provider, no real credential (the one fake key is conspicuously
fake), no external network. Mirrors the ``tests/gateway_fixtures.py``
discipline: deterministic, behavior-focused, self-contained.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast, override

FAKE_PROVIDER_KEY = "sk-FAKE-provider-key-000000000000"

#: Marker header the adapter must stamp on every outbound request.
MARKER_HEADER = "X-Scarcity-Router-Gateway"


@dataclass(frozen=True)
class RecordedRequest:
    """One inbound request recorded by the synthetic server."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> object:
        document: object = cast("object", json.loads(self.body.decode("utf-8")))
        return document


@dataclass(frozen=True)
class ScriptedResponse:
    """One canned non-streaming response."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


ResponseBehavior = Callable[[RecordedRequest], ScriptedResponse]
StreamBehavior = Callable[[RecordedRequest, "StreamSink"], None]


@dataclass(frozen=True)
class StreamEntry:
    """A queued streaming behavior (distinct type for dispatch narrowing)."""

    behavior: StreamBehavior


class StreamSink:
    """The write end a streaming behavior uses (frames then [DONE])."""

    _handler: BaseHTTPRequestHandler

    def __init__(self, handler: BaseHTTPRequestHandler, status: int, headers: dict[str, str]) -> None:
        self._handler = handler
        _ = handler.send_response(status)
        merged = {"Content-Type": "text/event-stream", "Connection": "close"}
        merged.update(headers)
        for name, value in merged.items():
            _ = handler.send_header(name, value)
        _ = handler.end_headers()

    def frame(self, payload: object) -> None:
        text = f"data: {json.dumps(payload, allow_nan=False)}\n\n"
        _ = self._handler.wfile.write(text.encode("utf-8"))
        _ = self._handler.wfile.flush()

    def done(self) -> None:
        _ = self._handler.wfile.write(b"data: [DONE]\n\n")
        _ = self._handler.wfile.flush()


def completion_frame(
    *,
    text: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, object] | None = None,
    role: str | None = None,
) -> dict[str, object]:
    delta: dict[str, object] = {}
    if role is not None:
        delta["role"] = role
    if text is not None:
        delta["content"] = text
    choices: list[dict[str, object]] = [
        {"index": 0, "delta": delta, "finish_reason": finish_reason}
    ]
    if usage is not None:
        # OpenAI-style terminal usage chunk: empty choices.
        choices = []
    frame: dict[str, object] = {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": "synthetic-model",
        "choices": choices,
    }
    if usage is not None:
        frame["usage"] = usage
    return frame


def completion_body(
    *,
    content: str = "synthetic reply",
    finish_reason: str = "stop",
    usage: dict[str, object] | None = None,
    model: str = "synthetic-model",
    tool_calls: list[dict[str, object]] | None = None,
) -> bytes:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
        message["content"] = None
    document: dict[str, object] = {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        document["usage"] = usage
    return json.dumps(document).encode("utf-8")


USAGE: dict[str, object] = {
    "prompt_tokens": 12,
    "completion_tokens": 5,
    "total_tokens": 17,
}


class ScriptedProviderServer:
    """An in-thread scripted OpenAI-compatible provider (loopback only).

    Behaviors run in FIFO order; a request with no queued behavior gets a
    plain 500. Streaming behaviors receive a :class:`StreamSink` and may
    delay or gate their frames for deterministic mid-stream tests.
    """

    _lock: threading.Lock
    requests: list[RecordedRequest]
    _behaviors: list[ResponseBehavior | StreamEntry]
    _server: ThreadingHTTPServer | None
    _thread: threading.Thread | None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = []
        self._behaviors = []
        self._server = None
        self._thread = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version: str = "HTTP/1.1"
            close_connection: bool

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                record = RecordedRequest(
                    method="POST",
                    path=self.path,
                    headers={
                        name.lower(): value
                        for name, value in self.headers.items()
                    },
                    body=body,
                )
                with server._lock:
                    server.requests.append(record)
                    behavior = (
                        server._behaviors.pop(0)
                        if server._behaviors
                        else None
                    )
                if behavior is None:
                    _ = self.send_response(500)
                    _ = self.send_header("Content-Length", "0")
                    _ = self.end_headers()
                    return
                try:
                    if isinstance(behavior, StreamEntry):
                        sink = StreamSink(self, 200, {})
                        behavior.behavior(record, sink)
                        self.close_connection = True
                        return
                    response = behavior(record)
                    _ = self.send_response(response.status)
                    for name, value in response.headers.items():
                        _ = self.send_header(name, value)
                    _ = self.send_header("Content-Length", str(len(response.body)))
                    _ = self.end_headers()
                    _ = self.wfile.write(response.body)
                except (BrokenPipeError, ConnectionResetError):
                    # The peer (adapter under test) hung up: expected in
                    # timeout and cancellation scenarios.
                    self.close_connection = True

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                record = RecordedRequest(
                    method="GET",
                    path=self.path,
                    headers={
                        name.lower(): value
                        for name, value in self.headers.items()
                    },
                    body=b"",
                )
                with server._lock:
                    server.requests.append(record)
                    behavior = (
                        server._behaviors.pop(0)
                        if server._behaviors
                        else None
                    )
                if behavior is None:
                    _ = self.send_response(500)
                    _ = self.send_header("Content-Length", "0")
                    _ = self.end_headers()
                    return
                assert not isinstance(behavior, StreamEntry)
                try:
                    response = behavior(record)
                    _ = self.send_response(response.status)
                    for name, value in response.headers.items():
                        _ = self.send_header(name, value)
                    _ = self.send_header("Content-Length", str(len(response.body)))
                    _ = self.end_headers()
                    _ = self.wfile.write(response.body)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            @override
            def log_message(self, format: str, *args: object) -> None:
                _ = format, args  # quiet by design

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ── Scripting ─────────────────────────────────────────────────────────

    def enqueue(self, behavior: ResponseBehavior | StreamEntry) -> None:
        with self._lock:
            self._behaviors.append(behavior)

    def enqueue_json(
        self,
        status: int = 200,
        document: object = None,
        *,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = raw if raw is not None else json.dumps(document).encode("utf-8")
        self.enqueue(lambda _request: ScriptedResponse(status, dict(headers or {}), body))

    def enqueue_completion(
        self,
        *,
        content: str = "synthetic reply",
        finish_reason: str = "stop",
        usage: dict[str, object] | None = USAGE,
        tool_calls: list[dict[str, object]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = completion_body(
            content=content,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls,
        )
        self.enqueue(
            lambda _request: ScriptedResponse(200, dict(headers or {}), body)
        )

    def enqueue_error(
        self, status: int, envelope: object, *, raw: bytes | None = None
    ) -> None:
        body = raw if raw is not None else json.dumps(envelope).encode("utf-8")
        self.enqueue(lambda _request: ScriptedResponse(status, {}, body))

    def enqueue_redirect(self, status: int, location: str) -> None:
        self.enqueue(
            lambda _request: ScriptedResponse(
                status, {"Location": location}, b""
            )
        )

    def enqueue_stream(
        self,
        frames: list[dict[str, object]],
        *,
        delay_seconds: float = 0.0,
        gate: threading.Event | None = None,
        gate_after: int = 1,
        send_done: bool = True,
    ) -> None:
        """Stream frames with optional gating for mid-stream tests.

        ``gate`` blocks the sink before writing frame ``gate_after`` until
        the event is set, so a test can cancel deterministically after the
        first frames arrived.
        """

        def behavior(_request: RecordedRequest, sink: StreamSink) -> None:
            for index, frame in enumerate(frames):
                if gate is not None and index == gate_after:
                    _ = gate.wait(timeout=10)
                if delay_seconds:
                    _ = threading.Event().wait(timeout=delay_seconds)
                sink.frame(frame)
            if send_done:
                sink.done()

        self.enqueue(StreamEntry(behavior))

    # ── Inspection ────────────────────────────────────────────────────────

    @property
    def last_request(self) -> RecordedRequest:
        with self._lock:
            assert self.requests, "the synthetic server received no request"
            return self.requests[-1]

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)


# ── Adapter dispatch builders ─────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

from scarcity_router.gateway_adapters import (  # noqa: E402
    AdapterCall,
    AdapterMessage,
    AdapterStreamChunk,
    ExecutionContext,
)
from scarcity_router.providers.http_origin import (  # noqa: E402
    ProviderCredential,
    ProviderOrigin,
)
from scarcity_router.providers.openai_http_adapter import (  # noqa: E402
    OpenAICompatibleHttpAdapter,
    ResourceBinding,
)
from scarcity_router.providers.openai_http_presets import (  # noqa: E402
    ProviderPreset,
    preset_by_id,
)
from scarcity_router.resource_state import ResourceIdentity  # noqa: E402
from scarcity_router.selection_types import ModelIdentity  # noqa: E402


def canonical_deadline(seconds: float = 60.0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_resource_identity(
    resource_id: str = "openai-http",
    provider: str = "openai",
    model: str = "gpt-5.6-luna",
    channel: str = "server_direct_http",
) -> ResourceIdentity:
    return ResourceIdentity(
        resource_id=resource_id,
        channel=channel,
        provider=provider,
        model=model,
        entitlement="payg_metered",
    )


def make_call(
    *,
    resource_id: str = "openai-http",
    provider: str = "openai",
    model: str = "gpt-5.6-luna",
    variant: str = "high",
    stream: bool = False,
    messages: tuple[AdapterMessage, ...] | None = None,
    tools: tuple[dict[str, object], ...] = (),
    tool_choice: object = None,
    response_format: dict[str, object] | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
    generation_params: dict[str, object] | None = None,
    catalog_provider: str = "openai",
) -> AdapterCall:
    """Build one dispatch.

    ``provider`` is the RESOURCE's physical backend (open safe-id grammar:
    deepseek/ollama/openrouter/zai/...). ``catalog_provider`` is the
    calibrated ModelIdentity provider -- a CLOSED contract vocabulary
    (``openai``/``zai``); the adapter never reads it (it resolves presets
    by resource_id), so tests keep the catalog contract intact.
    """
    return AdapterCall(
        resource=make_resource_identity(resource_id, provider, model),
        model=ModelIdentity(
            provider=catalog_provider, model=model, variant=variant
        ),
        messages=messages
        if messages is not None
        else (AdapterMessage(role="user", content="synthetic prompt"),),
        stream=stream,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
        generation_params=generation_params or {},
    )


def make_context(
    *,
    emit: Callable[[AdapterStreamChunk], None] | None = None,
    deadline_seconds: float = 60.0,
) -> ExecutionContext:
    return ExecutionContext(
        request_id="chatcmpl-test-0001",
        deadline=canonical_deadline(deadline_seconds),
        emit_chunk=emit,
    )


def make_binding(
    server: ScriptedProviderServer,
    preset: ProviderPreset,
    *,
    resource_id: str = "openai-http",
    with_credential: bool = True,
    wire_model: str | None = None,
) -> ResourceBinding:
    return ResourceBinding(
        resource_id=resource_id,
        preset=preset,
        origin=ProviderOrigin.parse(server.origin),
        credential=(
            ProviderCredential(FAKE_PROVIDER_KEY) if with_credential else None
        ),
        wire_model=wire_model,
    )


def make_adapter(
    *bindings: ResourceBinding,
    socket_timeout_seconds: float = 10.0,
) -> OpenAICompatibleHttpAdapter:
    return OpenAICompatibleHttpAdapter(
        {binding.resource_id: binding for binding in bindings},
        socket_timeout_seconds=socket_timeout_seconds,
    )


def preset(preset_id: str) -> ProviderPreset:
    found = preset_by_id(preset_id)
    assert found is not None
    return found
