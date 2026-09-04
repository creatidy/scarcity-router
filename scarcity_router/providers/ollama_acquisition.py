"""Secure local Ollama runtime capacity acquisition.

Production acquisition boundary between the explicitly configured local
Ollama endpoint and the pure parsers in ``ollama``:

    explicit local endpoint + configured target model
      -> strict local-endpoint validation (loopback only, before any I/O)
      -> GET /api/version   (reachability probe)
      -> GET /api/tags      (model listing, presence)
      -> GET /api/ps        (loaded models, effective context)
      -> pure ollama parsers
      -> CapacitySnapshot (+ LocalRuntime)

Security contract (docs/security.md):

- the endpoint must be an explicitly configured **local** endpoint: plain
  ``http`` on exactly ``127.0.0.1``, ``::1`` or ``localhost`` (``localhost``
  with an optional explicit port; default port 11434). Non-loopback hosts,
  other schemes, userinfo, query/fragment, non-root base paths and
  malformed URLs are configuration errors rejected before any I/O; there is
  no LAN scanning, no host discovery and no internet access — at most the
  three fixed reads against the single validated endpoint are issued;
- the connection bypasses environment proxies entirely (an explicit local
  endpoint must never be silently routed through a proxy) and follows no
  redirects;
- no credential exists or is attached: Ollama's local interface is
  credential-free and no Authorization material is ever constructed;
- every response body is read under a hard size bound; oversized bodies are
  never parsed; raw response fragments, endpoint URLs, local paths and
  exception text never enter the returned snapshot, diagnostics or output,
  and this module emits no stdout/stderr output.

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
each other.

Failure mapping (deterministic per outcome class):

- reachability-probe transport failure (connection refused, timeout, DNS,
  broken HTTP) -> ``unavailable``/``source_unavailable`` with
  ``runtime_unreachable``;
- probe HTTP error or unreadable body -> ``unknown``/``telemetry_unknown``;
- probe body not matching the evidenced shape -> ``schema_changed``;
- mid-collection transport/HTTP/unreadable failures keep facts already
  validated by earlier reads and report the corresponding degraded status;
- a malformed model listing or malformed ps body is ``schema_changed``;
- a reachable runtime without the configured model reports
  ``unavailable``/``source_unavailable`` plus ``model_missing``;
- a present model whose effective context cannot be validated (not loaded,
  supplemental read failed) stays ``ok`` with ``effective_context_unknown``.

Program/configuration errors (unapproved endpoint, unsafe model identity,
invalid configured context) raise ``ValueError`` before any I/O; their
messages never echo the offending value. An invalid ``retrieved_at`` keeps
failing through the typed capacity-contract validation.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from http.client import HTTPException, HTTPMessage
from typing import IO, Literal, cast, override

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

# Evidenced default local endpoint (Ollama's documented local default).
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"

# One attempt per fixed read, finite wait, bounded reads. Local reads are
# fast; the bound is generous for a host listing many models.
TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 1024 * 1024

# The v1 safe-identifier grammar (docs/capacity-model.md). The capacity
# module remains the single source of truth for the rule; this boundary
# check exists so invalid configuration fails before any I/O instead of as
# a late contract-validation error after runtime reads.
_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_CallOutcome = Literal["ok", "transport_fail", "http_error", "unreadable", "malformed"]


def is_approved_local_endpoint(url: object) -> bool:
    """Exact-match the local-endpoint policy for the Ollama collector.

    The configured endpoint is untrusted boundary input and is type-checked
    here. Accepts only plain ``http`` on exactly ``127.0.0.1``, ``::1`` or
    ``localhost`` with an optional explicit port (defaulting to the
    documented Ollama port) and an empty or root base path. Rejects at
    minimum: non-strings, any other scheme (including https — the evidenced
    local interface is plain HTTP), any non-loopback or suffixed host,
    userinfo, query, fragment, non-root paths and malformed URLs.
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "http":
            return False
        if parts.hostname not in _LOCAL_HOSTS:
            return False
        if parts.path not in ("", "/"):
            return False
        if parts.query or parts.fragment:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        port = parts.port
    except ValueError:
        return False
    return port is None or 1 <= port <= 65535


def _require_config(
    endpoint: str,
    model_name: object,
    configured_context_tokens: object,
) -> None:
    """Validate the configuration boundary before any I/O.

    The model identity and context value are untrusted boundary objects:
    Python does not enforce annotations at runtime, so they are
    type-checked here. Raises ``ValueError`` with a message that never
    echoes the offending endpoint URL, model name or context value:
    configuration values are not silently trusted and must never be copied
    into diagnostics or logs.
    """
    if not is_approved_local_endpoint(endpoint):
        raise ValueError(
            "endpoint: refusing to collect; the configured endpoint does not"
            + " satisfy the local endpoint policy (plain http on 127.0.0.1,"
            + " ::1 or localhost only)"
        )
    if not isinstance(model_name, str) or not _SAFE_ID_RE.match(model_name):
        raise ValueError(
            "model_name: refusing to collect; the configured target model"
            + " is not a safe v1 identifier (lowercase, max 64 chars,"
            + " [a-z0-9._:-]) and could never be emitted"
        )
    context_is_valid = (
        configured_context_tokens is None
        or (
            isinstance(configured_context_tokens, int)
            and not isinstance(configured_context_tokens, bool)
            and configured_context_tokens > 0
        )
    )
    if not context_is_valid:
        raise ValueError(
            "configured_context_tokens: refusing to collect; expected a"
            + " positive integer or None"
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect policy handler: decline every redirect.

    A local metadata read has no reason to follow a redirect; returning
    ``None`` makes the standard-library opener surface the redirect status
    as an ``HTTPError`` instead of issuing a second request.
    """

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def _build_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})


def open_response(
    request: urllib.request.Request,
    timeout: float,
) -> urllib.response.addinfourl:
    """Perform one local read through a no-proxy, no-redirect opener.

    ``ProxyHandler({})`` disables every environment proxy so an explicit
    loopback endpoint is contacted directly. Test seam: tests replace this
    function with a fake transport; no test contacts a runtime or network.
    """
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    return cast(
        "urllib.response.addinfourl", opener.open(request, timeout=timeout)
    )


def _read_call(url: str) -> tuple[_CallOutcome, object]:
    """Perform one bounded local read and classify the outcome.

    Never raises for expected operational conditions; the exception text of
    transport failures is deliberately not inspected or retained. The
    outcome classes are: ``ok`` (decoded JSON body), ``transport_fail``,
    ``http_error`` (non-2xx including declined redirects), ``unreadable``
    (oversized body) and ``malformed`` (body received but not decodable as
    UTF-8 JSON).
    """
    request = _build_request(url)
    try:
        response = open_response(request, TIMEOUT_SECONDS)
    except urllib.error.HTTPError:
        # Inspected only via the exception class; error bodies are never
        # read. 3xx arrives here because redirects are declined.
        return "http_error", None
    except (HTTPException, OSError):
        # URLError, ConnectionError and TimeoutError (socket.timeout) all
        # derive from OSError; BadStatusLine & co. derive from HTTPException.
        return "transport_fail", None

    try:
        with response:
            body = response.read(MAX_BODY_BYTES + 1)
    except (OSError, HTTPException):
        # The connection failed mid-body; partial content is discarded.
        return "transport_fail", None
    if len(body) > MAX_BODY_BYTES:
        return "unreadable", None

    try:
        return "ok", cast("object", json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "malformed", None


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
    if outcome in ("http_error", "unreadable"):
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
    ``/api/ps`` evidence and is never inferred from the configured value.
    At most the three fixed reads against the single validated endpoint are
    performed; the endpoint URL never enters the snapshot, diagnostics or
    error messages.
    """
    _require_config(endpoint, model_name, configured_context_tokens)
    base = endpoint.rstrip("/")

    # 1. Reachability probe: a validated version exchange is the defensible
    # reachability fact; an HTTP 200 alone proves only that a server
    # answered, not that the local Ollama interface did.
    probe_outcome, probe_payload = _read_call(f"{base}/api/version")
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

    # 2. Model listing: presence from exact listed-name identity.
    tags_outcome, tags_payload = _read_call(f"{base}/api/tags")
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
        # target: presence is confirmed missing, not merely unchecked.
        return _snapshot(
            retrieved_at=retrieved_at,
            status="unavailable",
            reachable=True,
            model_presence="missing",
            model_name=model_name,
            configured_context_tokens=configured_context_tokens,
            effective_context_tokens=None,
        )

    # 3. Loaded-model read: the only validated effective-context evidence.
    # A model that is present but not loaded is normal operation, not a
    # runtime failure; any ps failure therefore leaves the validated facts
    # intact and omits only the optional effective context. Only a ps body
    # that violates the evidenced shape is schema drift.
    ps_outcome, ps_payload = _read_call(f"{base}/api/ps")
    effective_context_tokens: int | None = None
    status = "ok"
    if ps_outcome == "ok":
        loaded = parse_ollama_ps_response(ps_payload)
        if loaded is None:
            status = "schema_changed"
        else:
            effective_context_tokens = loaded.get(model_name)
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
