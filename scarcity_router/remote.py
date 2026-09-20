"""Optional remote-mode bridge client (M08, issue #93; D-045 topology 4).

This module is the CLIENT side of the optional remote recommendation bridge:
an MCP/control client explicitly configured against a central, authenticated
Scarcity Router server instead of local operation. It never replaces local
mode — the local stdio MCP adapter, loopback REST v1 and CLI are untouched —
and the operating mode is always explicit: remote operation happens only when
a :class:`RemoteServerConfig` is constructed and handed to a
:class:`RemoteScarcityClient`.

**Explicit configuration, explicit failure (D-045).** Every failure of the
configured remote server — unreachable, authentication failure, unexpected
status, malformed or contract-violating response — raises
:class:`~scarcity_router.errors.RemoteBridgeError` and is surfaced to the
caller. The client holds no collectors, no artifacts and no policy, so a
silent fallback to local state or local policy is structurally impossible.

**Interface-side seam expectation (owned here for integration).** The
server-side authenticated control API is owned by M03/M09 and did not exist
when this client landed. The smallest client expectation, frozen here and
exercised against a synthetic in-process server in
``tests/test_interfaces_guardrails.py``:

- Endpoints: the machine-interface v1 logical contract on the configured
  server origin — ``GET /v1/status``, ``POST /v1/select``,
  ``POST /v1/simulate`` (the constants :data:`REMOTE_STATUS_ENDPOINT`,
  :data:`REMOTE_SELECT_ENDPOINT`, :data:`REMOTE_SIMULATE_ENDPOINT`). No
  ``/healthz``: liveness is a local-surface concern.
- Envelopes: exactly the machine-interface v1 envelopes (outer
  ``schema_version`` integer ``1`` plus ``snapshots`` / ``decision`` /
  ``result``), so equivalent state and policy produce responses that are
  semantically equal to the local surfaces (D-045 parity rule). Response
  bodies are parsed with the shared strict JSON parser (duplicate object
  keys and non-finite constants are contract violations, never accepted).
- Authentication: ``Authorization: Bearer <client API key>`` — the D-044
  client identity class. The key is transient client input: it travels only
  in this header, never in a URL, never in logs, never in exceptions, and
  :meth:`RemoteServerConfig.__repr__` redacts it.
- Transport: verified TLS for every non-loopback origin via
  ``ssl.create_default_context()``; no verification bypass exists as an
  option. Plain HTTP is accepted only for explicit loopback origins (the
  bounded D-044 localhost exception). Redirects are never followed.
- Semantics: request documents are validated client-side through the SAME
  shared ``machine_api`` parsers the frozen adapters use (there is no second
  logical parser), then sent verbatim; the remote server remains the
  authoritative application and re-validates.

If M03/M09 later define different control-API paths or envelope versioning,
the reconciliation is confined to this module (endpoint constants plus
envelope validation) and the synthetic-server tests.

Standard library only: no new dependency is added, matching the loopback
REST adapter's discipline (D-030). MCP remains recommendation/control only:
this client recommends and reports; it never executes inference, never
triggers quota-reset actions, never mutates provider entitlements and never
reserves execution capacity.
"""

from __future__ import annotations

import http.client
import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast, override
from urllib.parse import urlsplit

from .errors import RemoteBridgeError
from .machine_api import (
    ENVELOPE_SCHEMA_VERSION,
    parse_selection_document,
    parse_simulation_document,
)
from .selection_app import load_strict_json

REMOTE_STATUS_ENDPOINT = "/v1/status"
REMOTE_SELECT_ENDPOINT = "/v1/select"
REMOTE_SIMULATE_ENDPOINT = "/v1/simulate"

REMOTE_ENDPOINTS: Mapping[str, str] = {
    "status": REMOTE_STATUS_ENDPOINT,
    "select": REMOTE_SELECT_ENDPOINT,
    "simulate": REMOTE_SIMULATE_ENDPOINT,
}

#: The closed machine-interface error vocabulary (D-028). Only these codes
#: are ever echoed from a remote error response; anything else stays opaque.
_CLOSED_ERROR_CODES: frozenset[str] = frozenset(
    {"invalid_request", "internal_error"}
)

#: Upper bound on one remote response body; a larger response is a contract
#: violation, never an unbounded read.
MAX_RESPONSE_BODY_BYTES = 8 * 1024 * 1024

_DEFAULT_TIMEOUT_SECONDS = 60.0

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

_REDACTED_KEY_PLACEHOLDER = "<redacted>"


def _as_str_object_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary value to a ``str``-keyed mapping, or ``None``."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


class RemoteConfigError(RemoteBridgeError):
    """Raised when the explicit remote configuration itself is invalid.

    Distinct subclass so callers can separate "the configuration is wrong"
    from "the configured server failed"; both are explicit failures and
    neither triggers any local fallback.
    """


@dataclass(frozen=True)
class RemoteServerConfig:
    """Explicit configuration for one remote Scarcity Router server.

    ``base_url`` is the server origin (``https://host[:port]``; plain
    ``http://`` is accepted only for explicit loopback origins, the bounded
    D-044 exception). The origin must be bare: no path prefix, query,
    fragment or embedded credentials — the bridge appends the fixed
    machine-interface v1 endpoints. ``api_key`` is the authenticated client
    credential (D-044 identity class); it is transient input, sent only in
    the ``Authorization`` header and redacted from the repr. There is
    deliberately no TLS-verification bypass option (D-044).
    """

    base_url: str
    api_key: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    scheme: str = field(init=False, repr=False, compare=False)
    hostname: str = field(init=False, repr=False, compare=False)
    port: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        scheme, hostname, port = self._parse_origin(self.base_url)
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "hostname", hostname)
        object.__setattr__(self, "port", port)
        self._validate_key(self.api_key)
        # Runtime shape guard: annotations are not enforced; a bool must not
        # masquerade as 1 second. The cast defeats assignment narrowing so
        # the runtime isinstance checks stay meaningful.
        raw_timeout = cast(object, self.timeout_seconds)
        if isinstance(raw_timeout, bool) or not isinstance(
            raw_timeout, (int, float)
        ):
            raise RemoteConfigError("timeout_seconds must be a number")
        timeout = float(raw_timeout)
        if not timeout > 0 or timeout != timeout or timeout == float("inf"):
            raise RemoteConfigError(
                "timeout_seconds must be a positive finite number"
            )
        object.__setattr__(self, "timeout_seconds", timeout)

    @staticmethod
    def _parse_origin(base_url: object) -> tuple[str, str, int]:
        if not isinstance(base_url, str) or not base_url.strip():
            raise RemoteConfigError("base_url is required")
        try:
            parts = urlsplit(base_url)
            port = parts.port
        except ValueError as exc:
            raise RemoteConfigError("base_url is not a valid origin") from exc
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise RemoteConfigError(
                "base_url scheme must be https "
                + "(plain http is accepted only for explicit loopback origins)"
            )
        hostname = parts.hostname
        if not hostname:
            raise RemoteConfigError("base_url must include a hostname")
        hostname = hostname.lower()
        if parts.username is not None or parts.password is not None:
            # Credentials never travel in URLs (D-044).
            raise RemoteConfigError(
                "base_url must not embed credentials; "
                + "pass the client api_key field instead"
            )
        if parts.query or parts.fragment:
            raise RemoteConfigError(
                "base_url must be a bare origin without query or fragment"
            )
        if parts.path not in ("", "/"):
            raise RemoteConfigError(
                "base_url must be a bare origin without a path prefix; "
                + "the bridge appends the fixed v1 endpoints"
            )
        if scheme == "http" and hostname not in _LOOPBACK_HOSTS:
            raise RemoteConfigError(
                "plain HTTP is permitted only for explicit loopback origins; "
                + "non-local servers require verified HTTPS"
            )
        resolved_port = port if port is not None else (443 if scheme == "https" else 80)
        return scheme, hostname, resolved_port

    @staticmethod
    def _validate_key(api_key: object) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise RemoteConfigError(
                "api_key is required for the authenticated remote boundary"
            )
        if not api_key.isascii() or api_key.strip() != api_key or any(
            character.isspace() for character in api_key
        ):
            raise RemoteConfigError(
                "api_key must be a single ASCII token without whitespace"
            )

    @property
    def origin(self) -> str:
        """The safe origin for diagnostics (never carries the credential)."""
        return f"{self.scheme}://{self.hostname}:{self.port}"

    @override
    def __repr__(self) -> str:
        return (
            f"RemoteServerConfig(base_url={self.base_url!r}, "
            + f"api_key={_REDACTED_KEY_PLACEHOLDER!r}, "
            + f"timeout_seconds={self.timeout_seconds!r})"
        )


class RemoteScarcityClient:
    """Transport client for the configured remote control bridge.

    One instance wraps one :class:`RemoteServerConfig`. The public methods
    mirror the machine-interface v1 operations and return the SAME v1
    envelopes the loopback REST adapter returns. The client owns transport
    only: it runs no selection, no collection and no policy, and every
    remote failure raises
    :class:`~scarcity_router.errors.RemoteBridgeError`.
    """

    _config: RemoteServerConfig

    def __init__(self, config: RemoteServerConfig) -> None:
        self._config = config

    @property
    def config(self) -> RemoteServerConfig:
        return self._config

    def status(self) -> dict[str, object]:
        """Fetch the remote status envelope (machine-interface v1)."""
        return self._exchange(
            method="GET",
            endpoint=REMOTE_STATUS_ENDPOINT,
            body=None,
            result_key="snapshots",
        )

    def select(self, request: Mapping[str, object]) -> dict[str, object]:
        """Send one logical ``/v1/select`` request; returns the v1 envelope.

        The request document is validated through the shared logical parser
        first, so an invalid document fails explicitly before any network
        exchange (the same ``ApplicationInputError`` class the local
        adapters raise). The remote server remains authoritative and
        re-validates.
        """
        _ = parse_selection_document(request)
        return self._exchange(
            method="POST",
            endpoint=REMOTE_SELECT_ENDPOINT,
            body=dict(request),
            result_key="decision",
        )

    def simulate(self, request: Mapping[str, object]) -> dict[str, object]:
        """Send one logical ``/v1/simulate`` request; returns the v1 envelope."""
        _ = parse_simulation_document(request)
        return self._exchange(
            method="POST",
            endpoint=REMOTE_SIMULATE_ENDPOINT,
            body=dict(request),
            result_key="result",
        )

    # ── Transport ─────────────────────────────────────────────────────────

    def _connect(self) -> http.client.HTTPConnection:
        config = self._config
        if config.scheme == "https":
            # Verified certificates and hostname checking; D-044: no
            # verification bypass exists as an option.
            return http.client.HTTPSConnection(
                config.hostname,
                config.port,
                context=ssl.create_default_context(),
                timeout=config.timeout_seconds,
            )
        # Plain HTTP reached this point only for a loopback origin, which
        # the configuration validated as the explicit bounded exception.
        return http.client.HTTPConnection(
            config.hostname,
            config.port,
            timeout=config.timeout_seconds,
        )

    def _exchange(
        self,
        *,
        method: str,
        endpoint: str,
        body: dict[str, object] | None,
        result_key: str,
    ) -> dict[str, object]:
        payload = (
            None
            if body is None
            else json.dumps(body, allow_nan=False, sort_keys=True).encode("utf-8")
        )
        connection = self._connect()
        try:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._config.api_key}",
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, endpoint, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BODY_BYTES + 1)
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise RemoteBridgeError(
                f"remote server unreachable at {self._config.origin} "
                + f"for {endpoint}"
            ) from exc
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BODY_BYTES:
            raise RemoteBridgeError(
                f"remote server response exceeds the bounded size for {endpoint}"
            )
        if status != 200:
            raise self._failure_for(status, raw, endpoint)
        document = self._parse_body(raw, endpoint)
        return _validate_envelope(document, result_key, endpoint)

    def _failure_for(
        self, status: int, raw: bytes, endpoint: str
    ) -> RemoteBridgeError:
        """Classify a non-200 response as one explicit, safe failure."""
        base = f"remote server at {self._config.origin} failed for {endpoint}"
        if 300 <= status < 400:
            # Authorization is never forwarded across redirects (D-044); the
            # bridge follows no redirect at all.
            return RemoteBridgeError(f"{base}: unexpected redirect (HTTP {status})")
        if status in (401, 403):
            # Explicit authentication failure — never a reason to fall back.
            return RemoteBridgeError(
                f"{base}: authentication failed (HTTP {status})"
            )
        detail = ""
        server_code = _safe_server_error_code(raw)
        if server_code is not None:
            detail = f" (server error code: {server_code})"
        if status == 400:
            return RemoteBridgeError(f"{base}: invalid request{detail}")
        return RemoteBridgeError(f"{base}: unexpected HTTP status {status}{detail}")

    def _parse_body(self, raw: bytes, endpoint: str) -> object:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise RemoteBridgeError(
                f"remote server returned a non-UTF-8 response for {endpoint}"
            ) from None
        try:
            return load_strict_json(text, label="remote response")
        except ValueError as exc:
            raise RemoteBridgeError(
                f"remote server returned a malformed JSON response for {endpoint}"
            ) from exc


def _safe_server_error_code(raw: bytes) -> str | None:
    """Extract the error code from a v1 error envelope, vocabulary-checked.

    Anything unexpected about the body keeps the failure opaque: the code is
    echoed only when the body parses as the frozen error envelope and the
    code is a member of the closed vocabulary.
    """
    try:
        document = _as_str_object_mapping(
            cast(object, json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if document is None:
        return None
    error = _as_str_object_mapping(document.get("error"))
    if error is None:
        return None
    code = error.get("code")
    if isinstance(code, str) and code in _CLOSED_ERROR_CODES:
        return code
    return None


def _validate_envelope(
    document: object, result_key: str, endpoint: str
) -> dict[str, object]:
    """Require the exact machine-interface v1 envelope for one operation."""
    mapping = _as_str_object_mapping(document)
    if mapping is None:
        raise RemoteBridgeError(
            f"remote server returned a non-object response for {endpoint}"
        )
    if mapping.get("schema_version") != ENVELOPE_SCHEMA_VERSION:
        raise RemoteBridgeError(
            f"remote server returned an unsupported machine-interface "
            + f"envelope schema_version for {endpoint}"
        )
    payload = mapping.get(result_key)
    valid = (
        isinstance(payload, list)
        if result_key == "snapshots"
        else isinstance(payload, Mapping)
    )
    if not valid:
        raise RemoteBridgeError(
            f"remote server response for {endpoint} is missing the "
            + f"{result_key!r} contract document"
        )
    # JSON objects deserialize as dicts; the shape above is already verified.
    return cast("dict[str, object]", mapping)


__all__ = [
    "MAX_RESPONSE_BODY_BYTES",
    "REMOTE_ENDPOINTS",
    "REMOTE_SELECT_ENDPOINT",
    "REMOTE_SIMULATE_ENDPOINT",
    "REMOTE_STATUS_ENDPOINT",
    "RemoteBridgeError",
    "RemoteConfigError",
    "RemoteScarcityClient",
    "RemoteServerConfig",
]


if __name__ == "__main__":  # pragma: no cover - library module, no CLI surface
    raise SystemExit(
        "scarcity_router.remote is a library client; there is no module CLI"
    )
