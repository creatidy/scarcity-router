"""Secure local Ollama runtime capacity acquisition.

Production acquisition boundary between the explicitly configured local
Ollama endpoint and the pure parsers in ``ollama``:

    explicit local endpoint + configured target model
      -> strict canonicalization (numeric loopback, before any I/O)
      -> GET /api/version   (reachability probe)
      -> GET /api/tags      (model listing, presence, digest identity)
      -> GET /api/ps        (loaded models, effective context)
      -> strict JSON decoding + pure ollama parsers
      -> CapacitySnapshot (+ LocalRuntime)

Security contract (docs/security.md):

- the endpoint must be an explicitly configured **local** endpoint: plain
  ``http`` on exactly the numeric loopback hosts ``127.0.0.1`` or ``::1``.
  ``localhost`` and every other name is rejected outright — socket setup
  uses an explicit address family from ``ipaddress`` and ``getaddrinfo``
  restricted to ``AI_NUMERICHOST`` on the validated literal, so no
  resolver, DNS, hosts file or name-based path can ever be consulted and
  no resolve/connect race exists. The omitted port canonically defaults to the
  documented Ollama port 11434 (never an implicit socket default), an
  explicitly empty port is rejected, the base path must be empty or root, and
  empty-but-present query/fragment delimiters, leading/trailing whitespace and control characters are
  rejected. Collection contacts exactly one canonical endpoint; there is
  no LAN scanning, no host discovery, no proxy routing (the transport is
  one direct ``http.client`` connection — proxies are not consulted by
  construction) and no redirect following (a 3xx is surfaced as a status,
  never followed);
- one monotonic collection deadline (``COLLECTION_DEADLINE_SECONDS``)
  spans connect, headers and body reads **end to end**:
  each read executes inside a single bounded worker and the collector
  waits at most the remaining time on it. On deadline the collector
  requests cancellation through the registered exact raw-socket handle using
  non-blocking built-in ``shutdown``/``close`` and then unconditionally joins
  the worker. A worker registered after cancellation is cancelled immediately;
  no return or raise path precedes proof of worker termination. The worker
  re-checks the deadline between all blocking phases and again after EOF
  before the body is consumed; the collector re-checks it before any
  worker result is consumed, so a completion landing at or past the
  deadline fails closed. Each blocking phase is additionally budgeted the
  remaining time. A cancelled worker never feeds results back; a
  genuinely unexpected worker error is re-raised in the collector thread
  instead of being swallowed. Bounded worker count (at most one per read,
  at most three sequential workers per collection);
- transport results are narrowly protocol-validated before use (an
  integer status in the HTTP range plus a callable ``read``; body chunks
  must be ``bytes``): a malformed response object or non-bytes chunk
  normalizes to the documented degraded statuses instead of an uncaught
  ``TypeError``, and no payload or path detail ever leaks;
- every response body is read under validated HTTP framing and a hard size
  bound; declared ``Content-Length`` must be fully satisfied, conflicting
  framing and unsupported transfer codings fail closed, and a truncated
  chunked body is never accepted; non-200 responses
  (including declined redirects and error statuses) are never read, and
  the collector never explicitly invokes response/connection ``close`` —
  CPython's response/file finalization may close its buffered file as the
  response is released — while socket and file-descriptor cleanup is performed through
  the registered raw socket handles; oversized bodies are never parsed; expected
  response-operation failures — including provider-controlled exception text
  — normalize to safe outcomes and are never propagated, logged or retained;
  internal framing/programming errors remain distinguishable and are
  re-raised;
- response bodies decode under a strict JSON contract: duplicate object
  keys at any depth, the NaN/Infinity constants, any non-finite float
  result (e.g. ``1e10000``) and any integer outside the validated signed
  64-bit band are rejected, as is input so deeply nested that the decoder
  recurses out — all normalize to ``schema_changed``, never to an uncaught
  exception or partial decoding;
- no credential exists or is attached: Ollama's local interface is
  credential-free and no Authorization material is ever constructed. Raw
  response fragments, endpoint URLs, local paths, digests and exception
  text never enter the returned snapshot, diagnostics or output, and this
  module emits no stdout/stderr output.

Read-only boundary (issue #14): only version/health, model listing and
loaded-model metadata reads are performed. No generation, no model loading,
no pull/delete, no runtime or configuration mutation.

Snapshot semantics (docs/capacity-model.md): a healthy local runtime
normally reports ``windows=[]`` with no quota semantics — never unlimited
quota, percentage sentinels or subscription windows. ``local_runtime.reachable``
is the result of the explicit reachability probe (a validated
``/api/version`` exchange) during this collection attempt; facts validated
before a later read failed are preserved rather than discarded. The
configured context is reported only when independently supplied through the
configuration boundary; the effective context only from validated
``/api/ps`` ``context_length`` evidence; the two are never inferred from
each other. The effective context is accepted only when the loaded entry's
validated ``sha256`` digest matches the listing's validated digest for the
configured name — a missing, invalid or mismatched digest preserves the
validated reachability/presence facts but degrades the telemetry to
``unknown`` and omits the effective context rather than attributing it to
an unverifiable model image.

Failure mapping (deterministic per outcome class):

- reachability-probe transport/deadline failure (connection refused,
  timeout, DNS, broken HTTP, deadline exceeded) ->
  ``unavailable``/``source_unavailable`` with ``runtime_unreachable``;
- probe HTTP error, unreadable body or malformed transport-response
  object -> ``unknown``/``telemetry_unknown``;
- probe body not matching the evidenced shape (including strict-decode
  rejection, overlarge integers included) -> ``schema_changed``;
- mid-collection transport/HTTP/unreadable/deadline/response-object
  failures keep facts already validated by earlier reads and report the
  corresponding degraded status;
- a malformed model listing or malformed ps body is ``schema_changed``;
- a reachable runtime whose validated listing lacks the configured model
  reports ``unavailable``/``source_unavailable`` plus ``model_missing``
  (collection stops after two reads; ``/api/ps`` is not queried);
- a present model whose effective context cannot be validated (not
  loaded, supplemental read failed, or digest evidence missing/mismatched)
  omits the effective context and degrades to ``ok`` or ``unknown``
  telemetry without discarding the validated presence facts.

Program/configuration errors (unapproved endpoint, unsafe model identity,
invalid configured context) raise ``ValueError`` before any I/O; genuinely
unexpected internal worker errors are re-raised in the collector thread,
never swallowed or misreported as telemetry. Their messages never echo the
offending value. An invalid ``retrieved_at`` keeps failing through the
typed capacity-contract validation.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from http.client import HTTPConnection, HTTPException, HTTPResponse, IncompleteRead
from typing import Literal, Protocol, cast

from ..capacity import (
    CapacityDiagnostic,
    CapacitySnapshot,
    LocalRuntime,
)
from .ollama import (
    PROVIDER,
    SOURCE,
    parse_ollama_ps_response,
    parse_ollama_tags_response,
    parse_ollama_version_response,
)

# Evidenced default local endpoint (Ollama's documented local default);
# canonical form with the port explicit.
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
_DEFAULT_PORT = 11434

# One attempt per fixed read, bounded by one monotonic collection deadline
# spanning connect, headers and body reads. TIMEOUT_SECONDS caps a single
# transport phase; the deadline caps the whole collection so a trickling
# peer cannot extend it indefinitely.
TIMEOUT_SECONDS = 5.0
COLLECTION_DEADLINE_SECONDS = 15.0
MAX_BODY_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024

# After deadline cancellation, the registered raw socket handle is
# cancelled with non-blocking ``shutdown``/``close`` and the worker is
# reclaimed with an unconditional join. Registration is synchronized with the
# cancellation state, so a late handle is cancelled immediately. Production
# socket operations are bounded by the socket timeout; foreign handles are
# rejected before they can introduce an unbounded operation.

# The v1 safe-identifier grammar (docs/capacity-model.md). The capacity
# module remains the single source of truth for the rule; this boundary
# check exists so invalid configuration fails before any I/O instead of as
# a late contract-validation error after runtime reads. Matching is
# full-string (``fullmatch``), so a trailing newline can never satisfy it.
_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")

# Numeric loopback only: no name resolution is ever performed, so neither
# DNS, the hosts file, a proxy nor a LAN address can be reached.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})

_CallOutcome = Literal[
    "ok",
    "transport_fail",
    "http_error",
    "unreadable",
    "malformed",
    "invalid_response",
]
_BodyFraming = Literal["content_length", "chunked", "eof"]


class _AmbiguousJson(ValueError):
    """Raised by the strict-decode hooks for ambiguous JSON input.

    Duplicate keys at any depth, the NaN/Infinity constants, non-finite
    float results and overlarge integers make a body ambiguous or
    non-validating; decoding must fail closed instead of trusting
    last-key-wins parsing or infinities. Its message never contains any
    document value.
    """


# Validated integer band for decoded JSON values: signed 64-bit, matching
# the width-checked Codex precedent. Any integer outside the band is a
# strict-contract rejection (``schema_changed``), never truncated, never
# emitted.
_MAX_JSON_INT = 2**63 - 1
_MIN_JSON_INT = -(2**63)


def _bounded_int(text: str) -> int:
    """``json.loads`` ``parse_int`` hook: reject overlarge integers.

    Python integers are unbounded, so a hostile body could otherwise carry
    an arbitrarily large number into otherwise-tolerated additive fields or
    the effective-context slot. Values outside the validated signed 64-bit
    band fail closed exactly like the other strict-decode rejections.
    """
    value = int(text)
    if value > _MAX_JSON_INT or value < _MIN_JSON_INT:
        raise _AmbiguousJson()
    return value


def _reject_json_constant(_name: str) -> object:
    """``json.loads`` ``parse_constant`` hook: reject NaN/Infinity."""
    raise _AmbiguousJson()


def _finite_float(text: str) -> float:
    """``json.loads`` ``parse_float`` hook: reject non-finite results.

    Standard JSON numeric syntax such as ``1e10000`` parses to ``inf`` in
    Python; a non-finite quantity is not the validated contract and must
    fail closed exactly like the literal NaN/Infinity constants.
    """
    value = float(text)
    if not math.isfinite(value):
        raise _AmbiguousJson()
    return value


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """JSON ``object_pairs_hook`` that refuses duplicate keys anywhere.

    Applied at every nesting depth of every decoded body: a duplicate key
    makes the document ambiguous, and decoding must fail closed instead of
    trusting last-key-wins parsing. The message never contains any
    document value.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _AmbiguousJson()
        result[key] = value
    return result


def _decode_strict(body: bytes) -> object:
    """Strictly decode one bounded response body.

    Rejects invalid UTF-8, duplicate object keys at all depths, the
    NaN/Infinity constants, non-finite float results, integers outside the
    validated signed 64-bit band, and input nested beyond the decoder's
    recursion limit — every rejection raises a ``ValueError`` (or
    ``RecursionError`` for adversarial nesting), which the caller
    normalizes to ``schema_changed``. The raw bytes are never copied into
    any exception message or output.
    """
    return cast(
        "object",
        json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_float=_finite_float,
            parse_int=_bounded_int,
        ),
    )


def canonical_local_endpoint(url: object) -> str:
    """Canonicalize and validate the explicit local endpoint policy.

    The configured endpoint is untrusted boundary input. Accepts only
    plain ``http`` on exactly the numeric loopback hosts ``127.0.0.1`` or
    ``::1`` (``localhost`` and all other names are rejected: the collector
    never resolves names), with an optional explicit port defaulting to
    the documented Ollama port 11434 — never an implicit socket default —
    rejects explicitly empty port syntax, and accepts an empty or root base
    path. Rejects at minimum: non-strings,
    leading/trailing whitespace, any control character or space, empty
    query/fragment delimiters (``?``/``#`` anywhere), any other scheme,
    any non-loopback or suffixed host, userinfo, non-root paths and
    malformed URLs.

    Returns the canonical endpoint with the port explicit (for example
    ``http://127.0.0.1:11434``); raises ``ValueError`` on any violation,
    with a message that never echoes the offending URL.
    """
    if not isinstance(url, str) or url != url.strip():
        raise ValueError(
            "endpoint: refusing to collect; the configured endpoint is not"
            + " a canonical local endpoint string"
        )
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in url):
        raise ValueError(
            "endpoint: refusing to collect; the configured endpoint"
            + " contains whitespace or control characters"
        )
    if "?" in url or "#" in url:
        raise ValueError(
            "endpoint: refusing to collect; the configured endpoint"
            + " carries query or fragment delimiters"
        )
    try:
        parts = urllib.parse.urlsplit(url)
        valid = (
            parts.scheme == "http"
            and parts.hostname in _LOCAL_HOSTS
            and parts.path in ("", "/")
            and parts.username is None
            and parts.password is None
        )
        host_port = parts.netloc.rsplit("@", 1)[-1]
        explicit_empty_port = host_port.endswith(":") and (
            host_port.count(":") == 1 or host_port.endswith("]:")
        )
        port = parts.port  # may raise ValueError for a malformed port
    except ValueError:
        raise ValueError(
            "endpoint: refusing to collect; the configured endpoint is"
            + " malformed"
        ) from None
    if (
        explicit_empty_port
        or not valid
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError(
            "endpoint: refusing to collect; the configured endpoint does"
            + " not satisfy the local endpoint policy (plain http on"
            + " 127.0.0.1 or ::1 only)"
        )
    host = parts.hostname
    assert host is not None  # guaranteed by the policy check above
    shown_host = f"[{host}]" if ":" in host else host
    return f"http://{shown_host}:{_DEFAULT_PORT if port is None else port}"


def is_approved_local_endpoint(url: object) -> bool:
    """True when :func:`canonical_local_endpoint` accepts the endpoint."""
    try:
        _ = canonical_local_endpoint(url)
    except ValueError:
        return False
    return True


def _require_config(
    endpoint: str,
    model_name: object,
    configured_context_tokens: object,
) -> str:
    """Validate the configuration boundary before any I/O.

    The model identity and context value are untrusted boundary objects:
    Python does not enforce annotations at runtime, so they are
    type-checked here. The model identifier must satisfy the v1 safe-ID
    grammar as a full string (``fullmatch``: a trailing newline can never
    satisfy it) and must contain no control characters. Returns the
    canonical endpoint base for collection. Raises ``ValueError`` with a
    message that never echoes the offending endpoint URL, model name or
    context value: configuration values are not silently trusted and must
    never be copied into diagnostics or logs.
    """
    base = canonical_local_endpoint(endpoint)
    if not isinstance(model_name, str) or not _SAFE_ID_RE.fullmatch(model_name) or any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in model_name
    ):
        raise ValueError(
            "model_name: refusing to collect; the configured target model"
            + " is not a safe v1 identifier (lowercase, max 64 chars,"
            + " [a-z0-9._:-], no control characters) and could never be"
            + " emitted"
        )
    context_is_valid = configured_context_tokens is None or (
        isinstance(configured_context_tokens, int)
        and not isinstance(configured_context_tokens, bool)
        and configured_context_tokens > 0
    )
    if not context_is_valid:
        raise ValueError(
            "configured_context_tokens: refusing to collect; expected a"
            + " positive integer or None"
        )
    return base


class _ConnectionProtocol(Protocol):
    """The narrow connection contract the collector relies on.

    The connection is paired with a raw socket registered by
    :func:`open_connection`; cancellation is performed through that socket,
    not through response or connection ``close`` methods.
    """

    def request(
        self, method: str, path: str, /, *, headers: Mapping[str, str]
    ) -> None: ...

    def getresponse(self) -> object: ...

class _ResponseProtocol(Protocol):
    """The narrow validated-response contract (status + body reads).

    ``read`` is declared to return ``object`` deliberately: the runtime
    object behind this protocol is untrusted, and body chunks are required
    to be real ``bytes`` at the boundary before use.
    """

    status: int

    def read(self, size: int = -1, /) -> object: ...

    def getheader(self, name: str, default: object = None, /) -> object: ...


class _NonClosingHTTPResponse(HTTPResponse):
    """Keep the buffered file inspectable until framing checks finish."""

    def _close_conn(self) -> None:
        # Prevent HTTPResponse.read() from discarding bytes before the
        # collector's nonblocking trailing-data probe. Raw socket cleanup
        # remains the collector's ownership boundary.
        pass


_MAX_VERSION_LENGTH = 128


def _numeric_sockaddr(host: str, port: int) -> tuple[object, ...]:
    """Resolve the already-validated numeric literal to a sockaddr.

    The host is guaranteed numeric loopback by the endpoint policy, and
    ``getaddrinfo`` runs with ``AI_NUMERICHOST`` and an explicit family:
    it can only parse the literal — it can never consult a resolver,
    DNS, a hosts file or any name-based path.
    """
    ip = ipaddress.ip_address(host)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    info = socket.getaddrinfo(
        host,
        port,
        family,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        socket.AI_NUMERICHOST,
    )
    return cast("tuple[object, ...]", info[0][4])


def _open_raw_socket(host: str, timeout: float) -> socket.socket:
    """Open the family-correct raw socket for the numeric literal.

    The address family comes from ``ipaddress.ip_address`` on the already
    validated numeric loopback host, and the sockaddr from
    :func:`_numeric_sockaddr` (``AI_NUMERICHOST``): no resolver, DNS or
    name-based path can ever be consulted. The returned socket is
    unconnected with the phase timeout applied; the caller registers it
    as the cancellation handle *before* connecting.
    """
    ip = ipaddress.ip_address(host)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    return sock


def open_connection(
    host: str,
    port: int,
    timeout: float,
    register_handle: Callable[[object], None],
) -> _ConnectionProtocol:
    """Create one direct, connected HTTP connection for a single fixed GET.

    Direct by construction: ``http.client`` never consults environment
    proxies and never follows redirects — a 3xx is surfaced as a status,
    not followed. The raw socket is created with an explicit address
    family (from the validated numeric literal, so no name resolution is
    possible) and **registered as the cancellation handle before
    ``connect``**: the handle stays valid across ``Connection: close``
    ownership transfer to the response object, and ``shutdown``/``close``
    on it are non-blocking syscalls that unblock connect, read and any
    later phase — deadline cancellation never waits on a stuck close.
    Connection setup runs against the phase timeout and one monotonic
    deadline; failures normalize to the caller's transport outcome
    without retaining exception text. Test seam: tests replace this
    function with fake connection factories (invoking ``register_handle``
    with their fake socket); no test contacts a runtime or network.
    """
    sock = _open_raw_socket(host, timeout)
    register_handle(sock)
    sock.connect(_numeric_sockaddr(host, port))
    connection = HTTPConnection(host, port)
    connection.sock = sock
    connection.response_class = _NonClosingHTTPResponse
    return connection


def _cancel_sockets(handles: list[object]) -> None:
    """Cancel in-flight work through built-in non-blocking primitives.

    Production registers an exact built-in ``socket.socket`` before connect.
    Tests may register an exact ``threading.Event`` as an in-process
    cancellation primitive. Foreign objects are ignored: no arbitrary
    ``fileno``, ``shutdown`` or ``close`` attribute is looked up or invoked on
    the collector's critical path. Direct socket methods are invoked through
    the built-in descriptor, so subclasses cannot replace the OS operation.
    """
    for handle in handles:
        if type(handle) is socket.socket:
            try:
                socket.socket.shutdown(handle, socket.SHUT_RDWR)
            except OSError:  # already closed/cancelled: nothing to do
                pass
            try:
                socket.socket.close(handle)
            except OSError:  # already closed/cancelled: nothing to do
                pass
        elif type(handle) is threading.Event:
            threading.Event.set(handle)


def _response_protocol_ok(
    response: object,
) -> tuple[int, Callable[[int], object]] | None:
    """Narrow transport-response protocol validation.

    Capture the integer status and callable ``read`` exactly once. A transport
    result without both is a malformed response object, not telemetry, and is
    handled as a safe degraded outcome instead of an uncaught ``TypeError``.
    Programming/configuration errors elsewhere are never swallowed through
    this check.
    """
    try:
        status: object = getattr(response, "status", None)
        read: object = getattr(response, "read", None)
    except Exception:
        return None
    if (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 100 <= status <= 599
        and callable(read)
    ):
        return status, cast("Callable[[int], object]", read)
    return None


def _response_body_framing(
    response: _ResponseProtocol,
) -> tuple[_BodyFraming, int | None] | None:
    """Validate the response framing headers before consuming its body.

    ``http.client`` enforces a declared content length for its own reads, but
    it does not make a short close-delimited body distinguishable from EOF to
    this loop. The collector's non-closing response subclass keeps the buffer
    available for a nonblocking trailing-data probe. Conflicting
    ``Content-Length``/``Transfer-Encoding`` headers,
    repeated lengths with different values and unsupported transfer codings
    are ambiguous and therefore fail closed. A response seam without
    ``getheader`` remains the synthetic EOF-framed contract used by the unit
    fakes; real HTTP responses expose the header reader.
    """
    try:
        getheader_object: object = getattr(response, "getheader", None)
    except Exception:
        return None
    if getheader_object is None:
        return "eof", None
    if not callable(getheader_object):
        return None
    getheader = cast("Callable[[str], object]", getheader_object)
    try:
        content_length = getheader("Content-Length")
        transfer_encoding = getheader("Transfer-Encoding")
    except Exception:
        # Header access is an untrusted transport boundary; never retain its
        # exception or confuse it with a framing implementation defect.
        return None
    if content_length is not None and not isinstance(content_length, str):
        return None
    if transfer_encoding is not None and not isinstance(transfer_encoding, str):
        return None
    if content_length is not None and transfer_encoding is not None:
        return None
    if transfer_encoding is not None:
        codings = [part.strip(" \t").lower() for part in transfer_encoding.split(",")]
        if codings != ["chunked"]:
            return None
        return "chunked", None
    if content_length is not None:
        values = [part.strip(" \t") for part in content_length.split(",")]
        if not values or any(
            not value
            or any(character < "0" or character > "9" for character in value)
            for value in values
        ):
            return None
        if len(set(values)) != 1:
            return None
        try:
            length = int(values[0])
        except ValueError:
            return None
        return "content_length", length
    try:
        will_close = getattr(response, "will_close", None)
    except Exception:
        return None
    if will_close is not None and not isinstance(will_close, bool):
        return None
    if will_close is False:
        # EOF is not a frame boundary on a reusable HTTP/1.1 connection.
        return None
    return "eof", None


def _has_buffered_http_extra(response: HTTPResponse) -> bool:
    """Probe for trailing bytes without waiting on a persistent peer.

    ``HTTPResponse.read`` closes its buffered file as soon as a declared
    length reaches zero, which would discard bytes after that frame. Reading
    the exact body directly from the standard-library buffer leaves the
    buffer inspectable. A nonblocking ``peek`` sees already-buffered or
    immediately available trailing bytes, while a quiet keep-alive socket is
    accepted without waiting for EOF.
    """
    file_object: object = cast("object", response.fp)
    if file_object is None:
        return False
    peek = getattr(file_object, "peek", None)
    raw = getattr(file_object, "raw", None)
    raw_socket = getattr(raw, "_sock", None)
    if not callable(peek) or type(raw_socket) is not socket.socket:
        return False
    timeout = socket.socket.gettimeout(raw_socket)
    socket.socket.setblocking(raw_socket, False)
    try:
        try:
            extra = peek(1)
        except BlockingIOError:
            return False
        return isinstance(extra, bytes) and bool(extra)
    finally:
        socket.socket.settimeout(raw_socket, timeout)


def _execute_read(
    connection: _ConnectionProtocol, path: str, deadline: float
) -> tuple[_CallOutcome, object]:
    """Run one full transport-plus-read phase against the deadline.

    Executed only inside the bounded worker thread; the collector holds
    the connection's raw socket (attached by ``open_connection``) as the
    cancellation handle, which stays valid regardless of any socket
    ownership transfer to the response object. Each blocking phase is
    budgeted the remaining time (capped at ``TIMEOUT_SECONDS``). Response
    framing is validated before body consumption: a declared
    ``Content-Length`` must be completely read, only a single ``chunked``
    transfer coding is accepted, and EOF is accepted only for an EOF-framed
    response. The deadline is re-checked after every blocking operation and
    again after EOF before the body is consumed, and chunks must be ``bytes``.
    ``ok`` payloads are raw body bytes, strictly decoded by the caller. Outcome
    classes: ``transport_fail``
    (connection problem, deadline exceeded, or response-operation
    boundary anomaly — exception text is never inspected, retained or
    propagated), ``http_error`` (non-200 status, including declined
    redirects), ``unreadable`` (oversized body), ``invalid_response``
    (malformed response object or non-bytes chunk), ``malformed`` (invalid
    framing) and ``ok``.

    The collector never explicitly invokes ``response.close()`` or
    ``connection.close()``. CPython's response/file finalization may
    nevertheless close its buffered file as the response is released;
    socket/file-descriptor
    cleanup is performed through the registered raw-socket handles by the
    collector's non-blocking shutdown/close; see :func:`_cancel_sockets`.
    """
    response: object | None = None
    try:
        if deadline - time.monotonic() <= 0:
            return "transport_fail", None
        connection.request(
            "GET", path, headers={"Accept": "application/json"}
        )
        response = connection.getresponse()
        try:
            validated_response = _response_protocol_ok(response)
        except Exception:
            # Response protocol inspection is an untrusted boundary. Keep
            # provider-controlled descriptor errors out of the result.
            return "invalid_response", None
        if validated_response is None:
            # A malformed transport result is a degraded outcome, never an
            # uncaught TypeError; the raw socket is released by the
            # collector's socket-handle cancellation.
            return "invalid_response", None
        response_status, body_reader = validated_response
        typed_response = cast("_ResponseProtocol", response)
        if response_status != 200:
            # Non-200 (including declined 3xx): the body is never read.
            return "http_error", None

        framing = _response_body_framing(typed_response)
        if framing is None:
            return "malformed", None
        frame_kind, expected_length = framing
        if expected_length is not None and expected_length > MAX_BODY_BYTES:
            return "unreadable", None

        real_response: HTTPResponse | None = None
        if isinstance(cast(object, typed_response), HTTPResponse):
            real_response = cast("HTTPResponse", cast(object, typed_response))

        body = bytearray()
        while True:
            if deadline - time.monotonic() <= 0:
                return "transport_fail", None
            if expected_length is not None and len(body) == expected_length:
                break
            remaining = (
                expected_length - len(body)
                if expected_length is not None
                else MAX_BODY_BYTES + 1 - len(body)
            )
            try:
                chunk = body_reader(min(READ_CHUNK_BYTES, remaining))
            except IncompleteRead:
                return "malformed", None
            except (HTTPException, OSError):
                return "transport_fail", None
            except Exception:
                # Response reads are an untrusted provider boundary; their
                # exception text must never escape or be retained.
                return "transport_fail", None
            if not isinstance(chunk, bytes):
                # Non-bytes chunk: the transport violated the response
                # contract (e.g. a ``str``); degrade safely, never decode.
                return "invalid_response", None
            if not chunk:
                if expected_length is not None and len(body) != expected_length:
                    # ``HTTPResponse.read(size)`` can turn an early socket EOF
                    # into b"" instead of raising IncompleteRead.
                    return "malformed", None
                break
            if len(chunk) > remaining:
                return "malformed", None
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                return "unreadable", None
            body.extend(chunk)
            if (
                frame_kind == "chunked"
                and real_response is not None
                and real_response.chunk_left is None
            ):
                # The terminal chunk was consumed by this read. With the
                # non-closing response subclass, do not ask HTTPResponse to
                # parse a nonexistent next chunk.
                break
        if expected_length is not None and len(body) != expected_length:
            return "malformed", None
        if real_response is not None:
            try:
                if _has_buffered_http_extra(real_response):
                    return "malformed", None
            except OSError:
                return "transport_fail", None
        if deadline - time.monotonic() <= 0:
            # Delayed EOF must never smuggle a late body past the deadline.
            return "transport_fail", None
        return "ok", bytes(body)
    except (HTTPException, OSError):
        # BadStatusLine & co. derive from HTTPException; ConnectionError,
        # TimeoutError (socket.timeout) and cancellation-by-shutdown derive
        # from OSError. Partial content is discarded.
        return "transport_fail", None
    # The collector makes no explicit response/connection close call. CPython
    # may close the response's buffered file during its own read/finalization;
    # the retained raw socket is the collector's socket/fd cleanup guarantee.


def _open_and_read(
    host: str,
    port: int,
    path: str,
    deadline: float,
    phase_timeout: float,
    register_handle: Callable[[object], None],
) -> tuple[_CallOutcome, object]:
    """Open the connection, then run one full read phase.

    Connection setup (socket creation, handle registration, connect) and
    the read share the phase timeout and the collection deadline; setup
    failures surface as ``OSError``/``HTTPException`` for the caller to
    normalize.
    """
    connection = open_connection(host, port, phase_timeout, register_handle)
    return _execute_read(connection, path, deadline)


def _read_call(
    host: str, port: int, path: str, deadline: float
) -> tuple[_CallOutcome, object]:
    """Perform one deadline-bounded local read and classify the outcome.

    The whole read runs inside a single bounded **non-daemon** worker and
    the collector waits at most the remaining time on it. On deadline the
    collector requests cancellation through the registered raw-socket
  handles (``shutdown`` then ``close``), then joins the worker. A handle
  registered after cancellation is cancelled before the worker continues.
  The path does not return or re-raise until the worker has been proven
  reclaimed. The deadline is
    re-checked before any worker result is consumed, so a completion that
    lands at or past the deadline fails closed. Exception text is deliberately
    never inspected, retained or propagated; a genuinely unexpected internal
    worker error is re-raised in this thread so programming errors are never
    swallowed or misreported as telemetry.
    """
    if deadline - time.monotonic() <= 0:
        return "transport_fail", None

    results: list[tuple[_CallOutcome, object]] = []
    unexpected: list[Exception] = []
    registered_sockets: list[object] = []
    registration_lock = threading.Lock()
    cancellation_requested = False

    def _register(handle: object) -> None:
        # Registration races with the collector's deadline path. Resolve the
        # race under the lock, then cancel a late handle before the worker can
        # enter another blocking phase.
        if type(handle) not in (socket.socket, threading.Event):
            raise RuntimeError("internal error: unsupported cancellation handle")
        with registration_lock:
            cancel_now = cancellation_requested
            if not cancel_now:
                registered_sockets.append(handle)
        if cancel_now:
            _cancel_sockets([handle])

    def _request_cancellation() -> None:
        nonlocal cancellation_requested
        with registration_lock:
            cancellation_requested = True
            handles = list(registered_sockets)
        _cancel_sockets(handles)

    def _join_after_cancellation(worker: threading.Thread) -> None:
        _request_cancellation()
        # Cancellation cannot invoke foreign methods: production work is
        # bounded by the exact socket's timeout and test work by an Event.
        # The unconditional join proves no worker or socket operation remains
        # before the caller returns or re-raises.
        worker.join()

    def _work() -> None:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(("transport_fail", None))
                return
            outcome, payload = _open_and_read(
                host,
                port,
                path,
                deadline,
                min(TIMEOUT_SECONDS, remaining),
                _register,
            )
            if outcome == "ok":
                try:
                    payload = _decode_strict(cast("bytes", payload))
                except (ValueError, RecursionError, MemoryError):
                    # Duplicate keys, NaN/Infinity constants, non-finite
                    # floats, overlarge integers, invalid UTF-8, recursion-limit
                    # nesting and decoder resource exhaustion are all
                    # strict-contract rejections: nothing about the shape is
                    # trusted or retained.
                    outcome, payload = "malformed", None
            results.append((outcome, payload))
        except (OSError, HTTPException):
            # Connection-setup and transport-boundary failures (refusal,
            # unreachability, timeout, cancellation-by-shutdown): normalized
            # to a transport outcome; exception text is never inspected,
            # retained or propagated.
            results.append(("transport_fail", None))
        except Exception as exc:  # re-raised by the caller, never swallowed
            unexpected.append(exc)

    worker = threading.Thread(
        target=_work, name="scarcity-router-ollama-read", daemon=False
    )
    worker_started = False
    try:
        worker.start()
        worker_started = True
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            # Deadline cleanup below requests cancellation and performs the
            # bounded reclaim join. Its result, if any, is discarded.
            return "transport_fail", None
        if deadline - time.monotonic() <= 0:
            # A completion that lands at or past the deadline fails closed.
            return "transport_fail", None
        if unexpected:
            raise unexpected[0]
        if results:
            return results[0]
        return "transport_fail", None
    finally:
        # Every started worker is cancelled through the registered raw
        # sockets and then joined on every return and exception path. The
        # registry also cancels handles that arrive during this cleanup.
        if worker_started:
            _join_after_cancellation(worker)


def _snapshot(
    *,
    retrieved_at: str,
    status: str,
    reachable: bool,
    model_presence: str,
    model_name: str,
    configured_context_tokens: int | None,
    effective_context_tokens: int | None,
) -> CapacitySnapshot:
    """Assemble the snapshot with deterministic diagnostic rules.

    Diagnostic codes are emitted by uniform predicates, in a fixed order,
    so equal inputs always normalize to an equal snapshot:

    - the status-required code (only for non-``ok`` statuses);
    - ``runtime_unreachable`` exactly when the probe did not validate;
    - ``model_missing`` exactly when presence is ``missing``;
    - ``model_presence_unknown`` exactly when presence is ``unknown``;
    - ``configured_context_unknown`` exactly when no configured context was
      supplied through the configuration boundary;
    - ``effective_context_unknown`` exactly when the model is present but
      no validated effective context exists.
    """
    diagnostics: list[CapacityDiagnostic] = []
    required_code = {
        "unavailable": "source_unavailable",
        "unknown": "telemetry_unknown",
        "schema_changed": "schema_changed",
    }.get(status)
    if required_code is not None:
        diagnostics.append(CapacityDiagnostic(code=required_code))
    if not reachable:
        diagnostics.append(CapacityDiagnostic(code="runtime_unreachable"))
    if model_presence == "missing":
        diagnostics.append(CapacityDiagnostic(code="model_missing"))
    if model_presence == "unknown":
        diagnostics.append(CapacityDiagnostic(code="model_presence_unknown"))
    if configured_context_tokens is None:
        diagnostics.append(CapacityDiagnostic(code="configured_context_unknown"))
    if model_presence == "present" and effective_context_tokens is None:
        diagnostics.append(CapacityDiagnostic(code="effective_context_unknown"))

    return CapacitySnapshot(
        schema_version=1,
        provider=PROVIDER,
        source=SOURCE,
        retrieved_at=retrieved_at,
        status=status,
        windows=(),
        diagnostics=tuple(diagnostics),
        plan=None,
        local_runtime=LocalRuntime(
            reachable=reachable,
            model_presence=model_presence,
            model_name=model_name,
            configured_context_tokens=configured_context_tokens,
            effective_context_tokens=effective_context_tokens,
        ),
    )


def _map_call_outcome(outcome: _CallOutcome) -> str:
    """Map a primary-read (probe/tags) outcome class to a snapshot status."""
    if outcome == "transport_fail":
        return "unavailable"
    if outcome in ("http_error", "unreadable", "invalid_response"):
        return "unknown"
    return "schema_changed"


def collect_ollama_capacity(
    *,
    retrieved_at: str,
    model_name: str,
    endpoint: str = DEFAULT_ENDPOINT,
    configured_context_tokens: int | None = None,
) -> CapacitySnapshot:
    """Acquire one local Ollama runtime capacity snapshot.

    ``retrieved_at`` stays caller-supplied; this function introduces no
    clock or freshness policy (U-003). ``model_name`` is required: the
    target model is explicit configuration, never a hard-coded default.
    ``configured_context_tokens`` is the only source of the configured
    context fact; the effective context is only ever taken from validated
    ``/api/ps`` evidence whose digest agrees with the listing's digest for
    the configured name, and is never inferred from the configured value.
    The collection makes at most three reads against the single canonical
    endpoint — two when the validated listing proves the configured model
    absent — under one monotonic deadline; the endpoint URL never enters
    the snapshot, diagnostics or error messages.
    """
    base = _require_config(endpoint, model_name, configured_context_tokens)
    parts = urllib.parse.urlsplit(base)
    host = cast("str", parts.hostname)  # canonical: numeric loopback host
    port = cast("int", parts.port)  # canonical: port is always explicit
    deadline = time.monotonic() + COLLECTION_DEADLINE_SECONDS

    # 1. Reachability probe: a validated version exchange is the defensible
    # reachability fact; an HTTP 200 alone proves only that a server
    # answered, not that the local Ollama interface did.
    probe_outcome, probe_payload = _read_call(host, port, "/api/version", deadline)
    if probe_outcome != "ok":
        status = _map_call_outcome(probe_outcome)
        return _snapshot(
            retrieved_at=retrieved_at,
            status=status,
            reachable=False,
            model_presence="unknown",
            model_name=model_name,
            configured_context_tokens=configured_context_tokens,
            effective_context_tokens=None,
        )
    if not parse_ollama_version_response(probe_payload):
        return _snapshot(
            retrieved_at=retrieved_at,
            status="schema_changed",
            reachable=False,
            model_presence="unknown",
            model_name=model_name,
            configured_context_tokens=configured_context_tokens,
            effective_context_tokens=None,
        )

    def _degrade_on_expiry(outcome: _CallOutcome, failure_status: str) -> str:
        """Deadline expiry during a read is never misreported.

        A shared-budget expiry after a validated phase must not claim the
        source was merely unavailable: it degrades to ``unknown`` while
        the already-validated facts stay in the snapshot.
        """
        if outcome == "transport_fail" and time.monotonic() >= deadline:
            return "unknown"
        return failure_status

    # 2. Model listing: presence from exact listed-name identity, with the
    # listing's digest as the identity evidence for the effective context.
    tags_outcome, tags_payload = _read_call(host, port, "/api/tags", deadline)
    if tags_outcome != "ok":
        status = _degrade_on_expiry(tags_outcome, _map_call_outcome(tags_outcome))
        return _snapshot(
            retrieved_at=retrieved_at,
            status=status,
            reachable=True,
            model_presence="unknown",
            model_name=model_name,
            configured_context_tokens=configured_context_tokens,
            effective_context_tokens=None,
        )
    listed = parse_ollama_tags_response(tags_payload)
    if listed is None:
        return _snapshot(
            retrieved_at=retrieved_at,
            status="schema_changed",
            reachable=True,
            model_presence="unknown",
            model_name=model_name,
            configured_context_tokens=configured_context_tokens,
            effective_context_tokens=None,
        )
    if model_name not in listed:
        # The runtime explicitly listed its models without the configured
        # target: presence is confirmed missing, not merely unchecked. The
        # loaded-model read is pointless for a missing model and is skipped.
        return _snapshot(
            retrieved_at=retrieved_at,
            status="unavailable",
            reachable=True,
            model_presence="missing",
            model_name=model_name,
            configured_context_tokens=configured_context_tokens,
            effective_context_tokens=None,
        )
    listing_digest = listed[model_name]

    # 3. Loaded-model read: the only validated effective-context evidence.
    # A model that is present but not loaded is normal operation, not a
    # runtime failure; a supplemental ps transport failure therefore leaves
    # the validated facts intact and omits only the optional effective
    # context. A ps body violating the evidenced shape is schema drift. A
    # loaded entry whose digest is missing, invalid or disagrees with the
    # listing's digest cannot prove which image the context belongs to:
    # the effective context is withheld and the telemetry degrades to
    # ``unknown`` while the validated presence facts are preserved.
    ps_outcome, ps_payload = _read_call(host, port, "/api/ps", deadline)
    effective_context_tokens: int | None = None
    status = "unknown" if listing_digest is None else "ok"
    if ps_outcome == "ok":
        loaded = parse_ollama_ps_response(ps_payload)
        if loaded is None:
            status = "schema_changed"
        elif (entry := loaded.get(model_name)) is not None:
            ps_digest, context_length = entry
            if (
                listing_digest is not None
                and ps_digest is not None
                and ps_digest == listing_digest
            ):
                effective_context_tokens = context_length
            else:
                status = "unknown"
    elif ps_outcome == "malformed":
        status = "schema_changed"
    else:
        # Supplemental read failure: validated facts are preserved and
        # only the optional effective context is unknown — but a deadline
        # expiry during the read degrades the snapshot to ``unknown``
        # instead of a clean ``ok``.
        status = _degrade_on_expiry(ps_outcome, "ok")
    if status == "ok" and time.monotonic() >= deadline:
        # Final deadline check: no ok result may be produced after the
        # collection budget has expired.
        status = "unknown"
    return _snapshot(
        retrieved_at=retrieved_at,
        status=status,
        reachable=True,
        model_presence="present",
        model_name=model_name,
        configured_context_tokens=configured_context_tokens,
        effective_context_tokens=effective_context_tokens,
    )
