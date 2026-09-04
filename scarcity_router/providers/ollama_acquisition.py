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
  ``localhost`` and every other name is rejected outright — no DNS, hosts
  file or resolver is ever consulted, so no resolve/connect race and no
  name-based escape exists. The omitted port canonically defaults to the
  documented Ollama port 11434 (never an implicit socket default), the
  base path must be empty or root, and empty-but-present query/fragment
  delimiters, leading/trailing whitespace and control characters are
  rejected. Collection contacts exactly one canonical endpoint; there is
  no LAN scanning, no host discovery, no proxy routing (the transport is
  one direct ``http.client`` connection — proxies are not consulted by
  construction) and no redirect following (a 3xx is surfaced as a status,
  never followed);
- one monotonic collection deadline (``COLLECTION_DEADLINE_SECONDS``)
  spans connect, headers and body reads **end to end and cancellably**:
  each read executes inside a single bounded worker and the collector
  waits at most the remaining time on it. On deadline the collector
  closes the worker's connection — the cancellation primitive that
  unblocks every in-flight operation (a closed socket makes a blocked
  connect/read fail immediately) — and reclaims the worker with a bounded
  join, so no timeout path returns while a worker can remain blocked and
  no thread, socket or file descriptor is left behind. The worker
  re-checks the deadline between all blocking phases and again after EOF
  before the body is consumed; the collector re-checks it before any
  worker result is consumed, so a completion landing at or past the
  deadline fails closed. Each blocking phase is additionally budgeted the
  remaining time. An abandoned worker never feeds results back; a
  genuinely unexpected worker error is re-raised in the collector thread
  instead of being swallowed. Bounded worker count (at most one per read,
  at most three sequential workers per collection);
- transport results are narrowly protocol-validated before use (an
  integer status in the HTTP range plus a callable ``read``; body chunks
  must be ``bytes``): a malformed response object or non-bytes chunk
  normalizes to the documented degraded statuses instead of an uncaught
  ``TypeError``, anything closable is still closed, and no payload or
  path detail ever leaks;
- every response body is read under a hard size bound; non-200 responses
  (including declined redirects and error statuses) are closed
  deterministically without reading their content; oversized bodies are
  never parsed; response-operation failures of any kind — including
  provider-controlled exception text — normalize to safe outcomes and
  are never propagated, logged or retained;
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

import json
import math
import re
import threading
import time
import urllib.parse
from collections.abc import Mapping
from http.client import HTTPConnection, HTTPException
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

# After deadline cancellation (connection close), the abandoned worker
# unblocks deterministically — real sockets raise immediately on close.
# This bounded join reclaims the thread before the collection returns.
_CANCEL_JOIN_SECONDS = 2.0

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
    and an empty or root base path. Rejects at minimum: non-strings,
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
        port = parts.port  # may raise ValueError for a malformed port
    except ValueError:
        raise ValueError(
            "endpoint: refusing to collect; the configured endpoint is"
            + " malformed"
        ) from None
    if not valid or port is not None and not 1 <= port <= 65535:
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

    ``close()`` is the cancellation primitive: it must unblock any
    in-flight operation on the connection (the real ``HTTPConnection``
    does this by closing the socket). This is what makes the bounded
    worker genuinely reclaimable instead of abandonable.
    """

    def request(
        self, method: str, path: str, /, *, headers: Mapping[str, str]
    ) -> None: ...

    def getresponse(self) -> object: ...

    def close(self) -> None: ...


class _ResponseProtocol(Protocol):
    """The narrow validated-response contract (status + body reads).

    ``read`` is declared to return ``object`` deliberately: the runtime
    object behind this protocol is untrusted, and body chunks are required
    to be real ``bytes`` at the boundary before use.
    """

    status: int

    def read(self, size: int = -1, /) -> object: ...


def open_connection(host: str, port: int, timeout: float) -> _ConnectionProtocol:
    """Create one direct HTTP connection for a single fixed GET.

    Direct by construction: ``http.client`` never consults environment
    proxies and never follows redirects — a 3xx is surfaced as a status,
    not followed. ``timeout`` bounds each transport phase; the collection
    deadline is enforced around it by the bounded worker, and closing the
    connection is the cancellation path. Test seam: tests replace this
    function with fake connection factories; no test contacts a runtime
    or network.
    """
    return HTTPConnection(host, port, timeout=timeout)


def _response_protocol_ok(response: object) -> bool:
    """Narrow transport-response protocol validation.

    The collector only ever needs an integer status in the HTTP range and
    a callable ``read``; a transport result without both is a malformed
    response object, not telemetry, and is handled as a safe degraded
    outcome (with a best-effort close of anything closable) instead of an
    uncaught ``TypeError``. Programming/configuration errors elsewhere
    are never swallowed through this check.
    """
    status = getattr(response, "status", None)
    read = getattr(response, "read", None)
    return (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 100 <= status <= 599
        and callable(read)
    )


def _close_discardable(response: object) -> None:
    """Best-effort close of a response object being discarded.

    Used for protocol-invalid responses that may still carry a callable
    ``close``; a close failure never masks the normalized outcome.
    """
    close = getattr(response, "close", None)
    if callable(close):
        try:
            _ = close()
        except Exception:  # deliberate best-effort close; see docstring
            pass


def _execute_read(
    connection: _ConnectionProtocol, path: str, deadline: float
) -> tuple[_CallOutcome, object]:
    """Run one full transport-plus-read phase against the deadline.

    Executed only inside the bounded worker thread; ``connection.close()``
    (the collector's cancellation primitive) unblocks any of its blocking
    operations. Each blocking phase is budgeted the remaining time (capped
    at ``TIMEOUT_SECONDS``), the deadline is re-checked after every
    blocking operation and again after EOF before the body is consumed,
    chunks must be ``bytes``, and the connection is closed deterministically
    on every exit path. ``ok`` payloads are raw body bytes, strictly
    decoded by the caller. Outcome classes: ``transport_fail``
    (connection problem, deadline exceeded, or response-operation
    boundary anomaly — exception text is never inspected, retained or
    propagated), ``http_error`` (non-200 status, including declined
    redirects), ``unreadable`` (oversized body), ``invalid_response``
    (malformed response object or non-bytes chunk) and ``ok``.
    """
    response: object | None = None
    try:
        if deadline - time.monotonic() <= 0:
            return "transport_fail", None
        connection.request(
            "GET", path, headers={"Accept": "application/json"}
        )
        response = connection.getresponse()
        if not _response_protocol_ok(response):
            # A malformed transport result is a degraded outcome, never an
            # uncaught TypeError; anything closable is still closed.
            _close_discardable(response)
            return "invalid_response", None
        typed_response = cast("_ResponseProtocol", response)
        if typed_response.status != 200:
            # Non-200 (including declined 3xx): the body is never read.
            return "http_error", None

        body = bytearray()
        while True:
            if deadline - time.monotonic() <= 0:
                return "transport_fail", None
            chunk = typed_response.read(
                min(READ_CHUNK_BYTES, MAX_BODY_BYTES + 1 - len(body))
            )
            if not isinstance(chunk, bytes):
                # Non-bytes chunk: the transport violated the response
                # contract (e.g. a ``str``); degrade safely, never decode.
                return "invalid_response", None
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                return "unreadable", None
        if deadline - time.monotonic() <= 0:
            # Delayed EOF must never smuggle a late body past the deadline.
            return "transport_fail", None
        return "ok", bytes(body)
    except (HTTPException, OSError):
        # BadStatusLine & co. derive from HTTPException; ConnectionError,
        # TimeoutError (socket.timeout) and cancellation-by-close derive
        # from OSError. Partial content is discarded.
        return "transport_fail", None
    except Exception:
        # Response-operation/transport boundary anomaly (broken or hostile
        # transport, including provider-controlled exception text): mapped
        # to a safe normalized outcome, never propagated or logged.
        return "transport_fail", None
    finally:
        # Deterministic cleanup on every exit path, including abandonment:
        # the response (if one was produced) and the connection itself.
        # Both closes are idempotent and best-effort by design.
        _close_discardable(response)
        try:
            connection.close()
        except Exception:  # deliberate best-effort close; see docstring
            pass

def _read_call(
    host: str, port: int, path: str, deadline: float
) -> tuple[_CallOutcome, object]:
    """Perform one deadline-bounded local read and classify the outcome.

    The whole read runs inside a single bounded worker and the collector
    waits at most the remaining time on it. On deadline the collector
    closes the worker's connection — the cancellation primitive that
    unblocks the worker — and joins it with a bounded grace, so no timeout
    path returns while the worker can remain blocked and no connection or
    thread is left behind (real sockets unblock deterministically on
    close; the seam contract requires the same of fakes). The deadline is
    re-checked before any worker result is consumed, so a completion that
    lands at or past the deadline fails closed. Exception text is
    deliberately never inspected, retained or propagated; a genuinely
    unexpected internal worker error is re-raised in this thread so
    programming errors are never swallowed or misreported as telemetry.
    """
    if deadline - time.monotonic() <= 0:
        return "transport_fail", None

    results: list[tuple[_CallOutcome, object]] = []
    unexpected: list[Exception] = []
    registered: list[_ConnectionProtocol] = []

    def _work() -> None:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(("transport_fail", None))
                return
            connection = open_connection(
                host, port, min(TIMEOUT_SECONDS, remaining)
            )
            registered.append(connection)
            outcome, payload = _execute_read(connection, path, deadline)
            if outcome == "ok":
                try:
                    payload = _decode_strict(cast("bytes", payload))
                except (ValueError, RecursionError):
                    # Duplicate keys, NaN/Infinity constants, non-finite
                    # floats, overlarge integers, invalid UTF-8 and
                    # recursion-limit nesting are all strict-contract
                    # rejections: nothing about the shape is trusted or
                    # retained.
                    outcome, payload = "malformed", None
            results.append((outcome, payload))
        except Exception as exc:  # re-raised by the caller, never swallowed
            unexpected.append(exc)

    worker = threading.Thread(
        target=_work, name="scarcity-router-ollama-read", daemon=True
    )
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        # Deadline exceeded: cancel through the connection (close unblocks
        # every in-flight operation) and reclaim the worker with a bounded
        # join. Its result, if any, is discarded and never observed.
        for connection in registered:
            try:
                connection.close()
            except Exception:  # best-effort cancel; see docstring
                pass
        worker.join(_CANCEL_JOIN_SECONDS)
        return "transport_fail", None
    if deadline - time.monotonic() <= 0:
        # A completion that lands at or past the deadline fails closed.
        return "transport_fail", None
    if unexpected:
        raise unexpected[0]
    if results:
        return results[0]
    return "transport_fail", None


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

    # 2. Model listing: presence from exact listed-name identity, with the
    # listing's digest as the identity evidence for the effective context.
    tags_outcome, tags_payload = _read_call(host, port, "/api/tags", deadline)
    if tags_outcome != "ok":
        status = _map_call_outcome(tags_outcome)
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
    status = "ok"
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
    return _snapshot(
        retrieved_at=retrieved_at,
        status=status,
        reachable=True,
        model_presence="present",
        model_name=model_name,
        configured_context_tokens=configured_context_tokens,
        effective_context_tokens=effective_context_tokens,
    )
