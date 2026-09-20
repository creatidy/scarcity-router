"""The execution-gateway server component: authenticated ingress (M03, D-044).

This is the NEW, distinct server component that terminates the
OpenAI-compatible execution surface v1 (``GET /v1/models``,
``POST /v1/chat/completions`` with SSE). It is deliberately NOT the
loopback REST adapter of :mod:`scarcity_router.server`, which stays
byte-identical and frozen (D-030/D-045): the two components share no
listener, no error vocabulary and no security boundary.

Security posture (D-044):

- **Authenticated inference clients.** Every request must carry exactly
  one ``Authorization: Bearer <key>`` header; keys are verified against
  the administrator-provided
  :class:`~scarcity_router.gateway_contracts.ClientKeyDirectory`
  (SHA-256 hashes only, constant-time comparison). Client keys authorize
  inference and nothing else — this component exposes no administration
  surface. There is no default key and no anonymous access: the server
  refuses to start without an explicit client-key directory.
- **No bearer secrets in URLs**: credentials are read from headers only;
  query strings and fragments are never interpreted.
- **Listener defaults.** The default bind is ``127.0.0.1``; a
  non-loopback bind requires explicit TLS (certificate and key files)
  and is refused otherwise. There is no ``verify=false`` mode and no
  plaintext non-loopback mode. Plain-HTTP loopback is the documented
  bounded localhost exception for local clients.
- **Host discipline.** Exactly one ``Host`` header is required; on a
  loopback bind its value must identify the loopback listener (the
  DNS-rebinding guard). No CORS headers are ever emitted.
- **Router-loop protection.** Gateway-originated traffic is identifiable
  on ingress: a request carrying the ``X-Scarcity-Router-Gateway`` marker
  header (stamped by this program's outbound adapters) fails loudly with
  ``router_loop_detected`` instead of silently chaining router → router
  (D-044).
- **Admission limits before dispatch** (request-body size, context,
  output, concurrency, execution time, spending): enforced by the
  coordinator; the body-size bound is enforced here at the transport
  edge.
- **No prompt/response logging, no secret logging, redacted diagnostics.**
  The request handler is quiet by design; errors carry safe structural
  messages only.
- **Client disconnect detection.** Streaming writes detect a dropped
  connection and propagate cancellation to the selected backend through
  the coordinator (D-043), honestly and only where the channel supports
  it.

Runtime: the standard library ``ThreadingHTTPServer`` only — no
framework, no runtime dependency. Threads are daemonic; execution
concurrency is bounded by the coordinator's admission reservation, not by
thread count, and a request that cannot reserve an execution slot fails
fast with 429.

Configuration seams are in-process (M09 provides the administration UX
later): routing artifacts load through the shared application loader, and
capacity/eligibility supply is an injected callable. The default
deployment starts with an empty registry, an empty alias table and no
adapters — an honest empty ``/v1/models`` and explicit no-target errors
until the administrator configures resources (M04/M09).
"""

from __future__ import annotations

import argparse
import json
import select
import socket
import ssl
import sys
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, override

from .config import resolve_default_selector_policy
from .gateway_adapters import (
    CHUNK_TOOL_CALL,
    CHUNK_USAGE,
    AdapterStreamChunk,
    ClientDisconnectedError,
)
from .gateway_contracts import (
    EXECUTION_SURFACE_VERSION,
    ClientKeyDirectory,
    GatewayError,
    GatewayLimits,
)
from .gateway_coordinator import GatewayApplication
from .gateway_validation import v_str_object_mapping
from .gateway_openai import (
    ChatCompletionRequest,
    chat_completion_payload,
    chunk_payload,
    models_list_payload,
    parse_chat_completion_request,
)
from .selector import neutral_selector_policy
from .resource_state import ResourceRegistry
from .routing_core import AdministratorConstraints
from .selection_app import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_MODEL_POLICY_PATH,
    load_configured_artifacts,
    load_strict_json,
)

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# Router-loop protection (D-044): the marker this program's outbound
# adapters stamp on provider traffic. Ingress refuses requests carrying it.
GATEWAY_ORIGIN_HEADER = "X-Scarcity-Router-Gateway"

_MAX_KEY_LENGTH = 4096


# ── Client-key directory loading (permissioned file fallback, D-044) ──────────


def load_client_key_directory(path: Path) -> ClientKeyDirectory:
    """Load the client-key directory from a permissioned file.

    The file must be owner-only (``0o600`` or stricter); a group- or
    world-readable file is refused — a permissioned file store is the
    recorded D-044 fallback, never a loose default. The file holds
    SHA-256 key HASHES (never key material):
    ``{"<client_id>": "<64 hex chars>"}``, parsed strictly.
    """
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"client keys file is unreadable: {exc.strerror}") from None
    if not stat.st_mode & 0o100000:
        raise ValueError("client keys path is not a regular file")
    if stat.st_mode & 0o077:
        raise ValueError(
            "client keys file must be accessible only by its owner (0o600)"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("client keys file is not valid UTF-8") from None
    except OSError:
        raise ValueError("client keys file is unreadable") from None
    try:
        document = load_strict_json(text, label="client keys file")
    except ValueError as exc:
        raise ValueError(f"client keys file is invalid: {exc}") from None
    entries_document = v_str_object_mapping(document, "client keys file")
    if not entries_document:
        raise ValueError("client keys file must be a non-empty JSON object")
    entries: dict[str, str] = {}
    for raw_client_id, raw_key_hash in entries_document.items():
        if not isinstance(raw_key_hash, str):
            raise ValueError(
                "client keys file must map client ids to key-hash strings"
            )
        entries[raw_client_id] = raw_key_hash
    try:
        return ClientKeyDirectory(entries)
    except ValueError as exc:
        raise ValueError(f"client keys file is invalid: {exc}") from None


# ── The HTTP server ───────────────────────────────────────────────────────────


class GatewayHTTPServer(ThreadingHTTPServer):
    """Threaded, daemon-mode server for the execution surface.

    Threads are daemonic so shutdown does not wait on long streams;
    execution concurrency itself is bounded by the coordinator's
    admission reservation, so thread count is never the concurrency
    control.
    """

    daemon_threads: bool = True
    allow_reuse_address: bool = True

    application: GatewayApplication

    def __init__(
        self,
        *,
        host: str,
        port: int,
        application: GatewayApplication,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__((host, port), GatewayRequestHandler)
        self.application = application
        if tls_context is not None:
            self.socket: socket.socket = tls_context.wrap_socket(
                self.socket, server_side=True
            )

    @override
    def handle_error(self, request: object, client_address: object) -> None:
        """Stay quiet on broken client connections; never dump tracebacks."""
        _ = request, client_address
        print("gateway: request handling failed", file=sys.stderr)


class GatewayRequestHandler(BaseHTTPRequestHandler):
    """Dispatches the execution surface v1 with the D-044 boundary."""

    # Do not disclose interpreter versions.
    server_version: str = "scarcity-router"
    sys_version: str = ""
    protocol_version: str = "HTTP/1.1"

    # The raw client socket (typed for the disconnect-detection path).
    connection: socket.socket

    close_connection: bool = True

    _sse_committed: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @override
    def setup(self) -> None:
        super().setup()
        # Widen the stalled-client read guard so long streams are not cut
        # by the socket timeout inside the execution-time budget. The raw
        # socket exists only after the base setup has run.
        application = self._application()
        self.connection.settimeout(
            float(application.limits.execution_time_limit_seconds + 60)
        )

    def _application(self) -> GatewayApplication:
        server = cast(GatewayHTTPServer, self.server)
        return server.application

    @override
    def log_message(self, format: str, *args: object) -> None:
        """Quiet by design: no request logging, hence no body logging."""
        _ = format, args

    @override
    def send_error(
        self,
        code: int | HTTPStatus,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace framework HTML error pages with safe empty responses."""
        _ = message, explain
        self.close_connection = True
        self._send_empty(HTTPStatus(int(code)))

    # ── Routing ───────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        try:
            self._require_single_host()
            client_id = self._authenticate()
            self._refuse_router_loops()
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if method == "GET" and path == "/v1/models":
                self._route_models()
                return
            if method == "POST" and path == "/v1/chat/completions":
                self._route_chat_completions(client_id)
                return
            if method in ("GET", "POST", "HEAD"):
                self._send_gateway_error(
                    GatewayError.not_found("unknown request URL")
                )
                return
            self._send_empty(HTTPStatus.METHOD_NOT_ALLOWED)
        except GatewayError as exc:
            self._send_gateway_error(exc)
        except ClientDisconnectedError:
            self.close_connection = True
        except Exception:
            # Fail closed: a fixed structural message only — never
            # exception details, provider payloads or request content.
            self._send_gateway_error(
                GatewayError.api("internal gateway error", code="gateway_error")
            )

    # ── Boundary checks ───────────────────────────────────────────────────

    def _require_single_host(self) -> None:
        """Exactly one Host header, loopback-exact on loopback binds."""
        host_values = self.headers.get_all("Host")
        if host_values is None or len(host_values) != 1:
            raise GatewayError.invalid_request(
                "exactly one Host header is required", code="invalid_host"
            )
        server = cast(GatewayHTTPServer, self.server)
        bound_host, bound_port = cast("tuple[str, int]", server.server_address)
        if bound_host in (BIND_HOST, "localhost", "::1"):
            if host_values[0] not in (bound_host, f"{bound_host}:{bound_port}"):
                raise GatewayError.invalid_request(
                    "Host header must identify this listener", code="invalid_host"
                )

    def _authenticate(self) -> str:
        """Bearer-key authentication; returns the client identity."""
        application = self._application()
        directory = application.client_key_directory
        if directory is None:
            # A server built without a directory is a construction error;
            # refuse everything rather than serve unauthenticated.
            raise GatewayError.authentication("authentication is unavailable")
        values = self.headers.get_all("Authorization")
        if values is None or len(values) != 1:
            raise GatewayError.authentication("missing API key")
        header = values[0].strip()
        scheme, _, key = header.partition(" ")
        if scheme.lower() != "bearer" or not key:
            raise GatewayError.authentication("invalid API key")
        key = key.strip()
        if not key or len(key) > _MAX_KEY_LENGTH:
            raise GatewayError.authentication("invalid API key")
        client_id = directory.authenticate(key)
        if client_id is None:
            raise GatewayError.authentication("invalid API key")
        return client_id

    def _refuse_router_loops(self) -> None:
        """Fail loudly on gateway-originated traffic (D-044)."""
        if self.headers.get(GATEWAY_ORIGIN_HEADER) is not None:
            raise GatewayError.invalid_request(
                "refusing gateway-originated traffic: a Scarcity Router "
                + "endpoint must not serve as another router's backend",
                code="router_loop_detected",
            )

    # ── Operations ────────────────────────────────────────────────────────

    def _route_models(self) -> None:
        application = self._application()
        payload = models_list_payload(application.aliases.aliases)
        self._send_json(HTTPStatus.OK, payload)

    def _route_chat_completions(self, client_id: str) -> None:
        application = self._application()
        document = self._read_json_object(application.limits.max_request_body_bytes)
        request = parse_chat_completion_request(document)
        if request.stream:
            self._run_streaming(application, client_id, request)
        else:
            self._run_nonstreaming(application, client_id, request)

    def _run_nonstreaming(
        self,
        application: GatewayApplication,
        client_id: str,
        request: ChatCompletionRequest,
    ) -> None:
        outcome = application.execute(
            client_id=client_id,
            request=request,
            request_id=f"chatcmpl-{uuid.uuid4().hex}",
        )
        payload = chat_completion_payload(outcome)
        self._send_json(HTTPStatus.OK, payload)

    def _run_streaming(
        self,
        application: GatewayApplication,
        client_id: str,
        request: ChatCompletionRequest,
    ) -> None:
        state = _StreamState(handler=self, request=request)
        try:
            _ = application.execute(
                client_id=client_id,
                request=request,
                emit_chunk=state.emit,
                request_id=state.request_id,
            )
        finally:
            state.finish()

    def mark_sse_committed(self) -> None:
        """Record that a 200 SSE stream has started on this connection."""
        self._sse_committed = True

    # ── Request body ──────────────────────────────────────────────────────

    def _read_json_object(self, max_body_bytes: int) -> dict[str, object]:
        # Any rejection before the declared body is fully consumed leaves
        # unread bytes on the socket; the connection must close so those
        # bytes can never be misread as a pipelined request.
        try:
            return self._read_json_object_bounded(max_body_bytes)
        except GatewayError:
            self.close_connection = True
            raise

    def _read_json_object_bounded(self, max_body_bytes: int) -> dict[str, object]:
        self._require_json_content_type()
        body = self._read_body(max_body_bytes)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise GatewayError.invalid_request(
                "request body is not valid UTF-8", code="invalid_json"
            ) from None
        try:
            document = load_strict_json(text, label="request body")
        except ValueError:
            raise GatewayError.invalid_request(
                "request body is not valid JSON", code="invalid_json"
            ) from None
        if not isinstance(document, dict):
            raise GatewayError.invalid_request(
                "request body must be a JSON object", code="invalid_json"
            )
        return cast("dict[str, object]", document)

    def _require_json_content_type(self) -> None:
        header = self.headers.get("Content-Type")
        if header is None:
            raise GatewayError.invalid_request(
                "content-type application/json is required",
                code="invalid_content_type",
            )
        parameters = [part.strip() for part in header.lower().split(";")]
        if parameters[0] != "application/json":
            raise GatewayError.invalid_request(
                "content-type must be application/json",
                code="invalid_content_type",
            )
        for parameter in parameters[1:]:
            if parameter != "charset=utf-8":
                raise GatewayError.invalid_request(
                    "only the charset=utf-8 parameter is accepted",
                    code="invalid_content_type",
                )

    def _read_body(self, max_body_bytes: int) -> bytes:
        if self.headers.get_all("Transfer-Encoding"):
            raise GatewayError.invalid_request(
                "transfer-encoding is not supported", code="invalid_transfer"
            )
        lengths = self.headers.get_all("Content-Length")
        if lengths is None or len(lengths) != 1:
            raise GatewayError.invalid_request(
                "exactly one content-length header is required",
                code="invalid_content_length",
            )
        raw = lengths[0].strip(" \t")
        if not raw or any(character not in "0123456789" for character in raw):
            raise GatewayError.invalid_request(
                "content-length must be a decimal integer",
                code="invalid_content_length",
            )
        length = int(raw)
        _ = length
        if length > max_body_bytes:
            # Bounded best-effort drain so the client can still read the
            # 413 without a TCP reset; never stored, never logged.
            self._drain_bounded(length)
            raise GatewayError.request_too_large(
                "request body exceeds the maximum request size"
            )
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                raise GatewayError.invalid_request(
                    "incomplete request body", code="invalid_body"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    _MAX_DRAIN_BYTES: int = 4 * 1_048_576

    def _drain_bounded(self, declared_length: int) -> None:
        remaining = min(declared_length, self._MAX_DRAIN_BYTES)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65_536))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            return
        _ = remaining

    # ── Response writing ──────────────────────────────────────────────────

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        _ = self.wfile.write(encoded)

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status.value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_gateway_error(self, exc: GatewayError) -> None:
        payload = exc.to_payload()
        if self._sse_committed:
            # The stream already started; report the failure in-band and
            # close. The error payload names nothing about request content.
            try:
                encoded = json.dumps(
                    payload, sort_keys=True, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                _ = self.wfile.write(b"data: " + encoded + b"\n\n")
                _ = self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            return
        try:
            self._send_json(HTTPStatus(exc.http_status), payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True


# ── SSE streaming plumbing ────────────────────────────────────────────────────


class _StreamState:
    """Lazy SSE writer: headers go out with the first real chunk.

    Deferring the 200 until the first adapter chunk keeps pre-dispatch
    failures clean HTTP errors (the client never sees a 200 followed by a
    fabricated stream). A dispatch that completes without emitting any
    chunk — an adapter that returned a whole message for a streaming
    call — is rendered as a synthesized chunk sequence so the client
    always receives well-formed event framing.
    """

    def __init__(
        self, *, handler: GatewayRequestHandler, request: ChatCompletionRequest
    ) -> None:
        self._handler: GatewayRequestHandler = handler
        self._request: ChatCompletionRequest = request
        self._started: bool = False
        self._tool_call_index: int = 0
        self.request_id: str = f"chatcmpl-{uuid.uuid4().hex}"

    def emit(self, chunk: AdapterStreamChunk) -> None:
        if self._client_gone():
            raise ClientDisconnectedError()
        if chunk.kind == CHUNK_USAGE and not self._request.include_usage:
            return
        self._begin()
        payload = chunk_payload(
            request_id=self.request_id,
            created=self._created,
            model_echo=self._request.model,
            chunk=chunk,
            tool_call_index=self._tool_call_index,
        )
        if chunk.kind == CHUNK_TOOL_CALL:
            self._tool_call_index += 1
        self._write_frame(payload)

    def _begin(self) -> None:
        if self._started:
            return
        self._started = True
        self._handler.mark_sse_committed()
        handler = self._handler
        handler.send_response(HTTPStatus.OK.value)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        # OpenAI clients expect the assistant role in the first delta.
        self._write_frame(
            {
                "id": self.request_id,
                "object": "chat.completion.chunk",
                "created": self._created,
                "model": self._request.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
        )

    def _client_gone(self) -> bool:
        """Whether the client connection is at EOF (disconnect detection).

        A readable request socket during streaming means either EOF (the
        client closed the connection) or pipelined client data, which the
        ``Connection: close`` SSE contract forbids; only EOF cancels. This
        detects disconnects deterministically — write errors alone surface
        them only after kernel-level resets land, which is racy.
        """
        connection = self._handler.connection
        try:
            ready, _, _ = select.select([connection], [], [], 0)
        except (OSError, ValueError):
            return True
        if not ready:
            return False
        try:
            peeked = bytes(connection.recv(1, socket.MSG_PEEK))
        except (OSError, ValueError):
            return True
        _ = len(peeked)
        return peeked == b""

    @property
    def _created(self) -> int:
        from datetime import datetime, timezone

        return int(datetime.now(timezone.utc).timestamp())

    def _write_frame(self, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        try:
            _ = self._handler.wfile.write(b"data: " + encoded + b"\n\n")
            self._handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise ClientDisconnectedError() from exc

    def finish(self) -> None:
        """Close the stream; synthesize framing for a chunkless completion."""
        if not self._started:
            return
        try:
            _ = self._handler.wfile.write(b"data: [DONE]\n\n")
            self._handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._handler.close_connection = True


# ── Server construction and entry point ───────────────────────────────────────


def build_default_application(
    *,
    directory: ClientKeyDirectory,
    limits: GatewayLimits | None = None,
) -> GatewayApplication:
    """Build the default (empty) deployment: honest until configured.

    Routing artifacts load through the shared application loader; the
    registry, alias table, compatibility cells and adapter registry start
    empty and the capacity source supplies honest empty observations, so
    the server starts securely and answers truthfully (an empty model
    list, explicit no-target errors) until administrator configuration
    lands. Real collector wiring for execution resources is M09
    configuration work and generic HTTP adapter work is M04; neither is
    invented here.
    """
    from .capacity import CapacitySnapshot
    from .eligibility import ExecutionEligibility
    from .gateway_adapters import AdapterRegistry
    from .gateway_audit import BoundedAuditTrail
    from .gateway_coordinator import RoutingAliasTable

    catalog, profiles, profile_policy_version = load_configured_artifacts(
        DEFAULT_CATALOG_PATH, DEFAULT_MODEL_POLICY_PATH
    )
    default_policy = resolve_default_selector_policy()

    def empty_capacity_source(
        now: str,
    ) -> tuple[tuple[CapacitySnapshot, ...], tuple[ExecutionEligibility, ...]]:
        _ = now
        return ((), ())

    return GatewayApplication(
        catalog=catalog,
        profiles=profiles,
        profile_policy_version=profile_policy_version,
        policy=(
            default_policy if default_policy is not None else neutral_selector_policy()
        ),
        registry=ResourceRegistry(),
        capacity_source=empty_capacity_source,
        compatibility_cells=(),
        admin_constraints=AdministratorConstraints(),
        aliases=RoutingAliasTable({}),
        adapters=AdapterRegistry(),
        audit=BoundedAuditTrail(),
        limits=limits if limits is not None else GatewayLimits(),
        client_key_directory=directory,
    )


def _loopback(host: str) -> bool:
    return host in (BIND_HOST, "localhost", "::1")


def build_tls_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """A server-authenticated TLS context (no ``verify=false`` exists here)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"TLS configuration failed: {exc}") from None
    return context


def make_gateway_server(
    application: GatewayApplication,
    *,
    host: str = BIND_HOST,
    port: int = DEFAULT_PORT,
    tls_context: ssl.SSLContext | None = None,
) -> GatewayHTTPServer:
    """Bind the execution server; non-loopback binds require TLS (D-044)."""
    if not _loopback(host) and tls_context is None:
        raise ValueError(
            "a non-loopback execution listener requires TLS (certificate and "
            + "key files); the gateway never serves plaintext off localhost"
        )
    return GatewayHTTPServer(
        host=host, port=port, application=application, tls_context=tls_context
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the module parser; the client-key directory is required."""
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    prog = (
        invoked
        if invoked == "scarcity-router-gateway"
        else "python -m scarcity_router.gateway_server"
    )
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Authenticated Scarcity Router execution gateway (execution "
            + "surface v1): GET /v1/models, POST /v1/chat/completions."
        ),
    )
    _ = parser.add_argument(
        "--host",
        default=BIND_HOST,
        metavar="HOST",
        help=f"bind address (default: {BIND_HOST}; non-loopback requires TLS)",
    )
    _ = parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"TCP port to bind (default: {DEFAULT_PORT})",
    )
    _ = parser.add_argument(
        "--client-keys",
        required=True,
        metavar="FILE",
        help="owner-only JSON file mapping client ids to SHA-256 API-key hashes",
    )
    _ = parser.add_argument(
        "--tls-certfile",
        default=None,
        metavar="FILE",
        help="TLS certificate chain (required for non-loopback binds)",
    )
    _ = parser.add_argument(
        "--tls-keyfile",
        default=None,
        metavar="FILE",
        help="TLS private key (required for non-loopback binds)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the execution gateway until interrupted.

    Startup fails closed: without a readable owner-only client-keys file
    there is no deployment (no default credential exists, D-044), and a
    non-loopback bind without TLS is refused.
    """
    parser = build_parser()
    arguments = cast("dict[str, object]", vars(parser.parse_args(argv)))
    host = arguments["host"]
    if not isinstance(host, str) or not host:
        parser.error("--host must be a non-empty string")
    raw_port = arguments["port"]
    if (
        not isinstance(raw_port, int)
        or isinstance(raw_port, bool)
        or not 0 <= raw_port <= 65535
    ):
        parser.error("--port must be an integer between 0 and 65535")
    keys_path_value = arguments["client_keys"]
    if not isinstance(keys_path_value, str):
        parser.error("--client-keys is required")
    certfile = arguments["tls_certfile"]
    keyfile = arguments["tls_keyfile"]
    if (certfile is None) != (keyfile is None):
        parser.error("--tls-certfile and --tls-keyfile must be used together")
    if not _loopback(host) and certfile is None:
        parser.error("a non-loopback bind requires --tls-certfile/--tls-keyfile")
    try:
        directory = load_client_key_directory(Path(keys_path_value))
        application = build_default_application(directory=directory)
        tls_context = (
            build_tls_context(str(certfile), str(keyfile))
            if certfile is not None and keyfile is not None
            else None
        )
        server = make_gateway_server(
            application, host=host, port=raw_port, tls_context=tls_context
        )
    except (ValueError, OSError) as exc:
        print(f"gateway: {exc}", file=sys.stderr)
        return 2
    bound_host, bound_port = cast("tuple[str, int]", server.server_address)
    scheme = "https" if tls_context is not None else "http"
    print(
        f"scarcity-router execution gateway (surface v{EXECUTION_SURFACE_VERSION}) "
        + f"listening on {scheme}://{bound_host}:{bound_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = [
    "BIND_HOST",
    "DEFAULT_PORT",
    "EXECUTION_SURFACE_VERSION",
    "GATEWAY_ORIGIN_HEADER",
    "GatewayHTTPServer",
    "GatewayRequestHandler",
    "build_default_application",
    "build_parser",
    "build_tls_context",
    "load_client_key_directory",
    "main",
    "make_gateway_server",
]


if __name__ == "__main__":
    raise SystemExit(main())
