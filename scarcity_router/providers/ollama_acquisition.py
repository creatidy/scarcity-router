"""Synchronous, read-only Ollama capacity acquisition.

The acquisition boundary validates one explicitly configured numeric loopback
endpoint, performs the three fixed Ollama GETs, and delegates provider shape
validation to :mod:`ollama`.  The local M1 threat model does not require a
custom socket transport: ``http.client.HTTPConnection`` supplies finite
timeouts, HTTP parsing and ordinary connection cleanup for this bounded use.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from collections.abc import Callable, Mapping
from http.client import HTTPConnection
from typing import Literal, Protocol, cast

from ..capacity import CapacityDiagnostic, CapacitySnapshot, LocalRuntime
from .ollama import (
    PROVIDER,
    SOURCE,
    parse_ollama_ps_response,
    parse_ollama_tags_response,
    parse_ollama_version_response,
)

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
_DEFAULT_PORT = 11434
TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 1024 * 1024

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})
_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
_MAX_JSON_INT = 2**63 - 1
_MIN_JSON_INT = -(2**63)

_CallOutcome = Literal[
    "ok",
    "transport_fail",
    "http_error",
    "unreadable",
    "invalid_response",
    "schema_changed",
]


class _AmbiguousJson(ValueError):
    """A strict JSON decoder rejection without retaining body content."""


class _ConnectionProtocol(Protocol):
    def request(
        self, method: str, path: str, /, *, headers: Mapping[str, str]
    ) -> None: ...

    def getresponse(self) -> object: ...

    def close(self) -> None: ...


class _ResponseProtocol(Protocol):
    status: object

    def read(self, size: int = -1, /) -> object: ...


class _CloseProtocol(Protocol):
    def close(self) -> object: ...


def _bounded_int(text: str) -> int:
    value = int(text)
    if value < _MIN_JSON_INT or value > _MAX_JSON_INT:
        raise _AmbiguousJson()
    return value


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _AmbiguousJson()
    return value


def _reject_json_constant(_name: str) -> object:
    raise _AmbiguousJson()


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _AmbiguousJson()
        result[key] = value
    return result


def _decode_strict(body: bytes) -> object:
    """Decode bounded response bytes without ambiguous JSON semantics."""
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
    """Return a canonical numeric-loopback HTTP endpoint or raise ``ValueError``."""
    if not isinstance(url, str) or url != url.strip():
        raise ValueError("endpoint: configured endpoint is not canonical")
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in url):
        raise ValueError("endpoint: configured endpoint contains whitespace or controls")
    if "?" in url or "#" in url:
        raise ValueError("endpoint: configured endpoint has a query or fragment")
    try:
        parts = urllib.parse.urlsplit(url)
        host_port = parts.netloc.rsplit("@", 1)[-1]
        port = parts.port
    except ValueError:
        raise ValueError("endpoint: configured endpoint is malformed") from None

    explicit_empty_port = host_port.endswith(":")
    valid = (
        parts.scheme == "http"
        and parts.hostname in _LOCAL_HOSTS
        and parts.path in ("", "/")
        and parts.username is None
        and parts.password is None
    )
    if explicit_empty_port or not valid or (port is not None and not 1 <= port <= 65535):
        raise ValueError("endpoint: only numeric loopback HTTP endpoints are allowed")

    host = parts.hostname
    assert host is not None
    shown_host = f"[{host}]" if ":" in host else host
    return f"http://{shown_host}:{_DEFAULT_PORT if port is None else port}"


def is_approved_local_endpoint(url: object) -> bool:
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
    base = canonical_local_endpoint(endpoint)
    if not isinstance(model_name, str) or _SAFE_ID_RE.fullmatch(model_name) is None:
        raise ValueError("model_name: configured target is not a safe v1 identifier")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in model_name):
        raise ValueError("model_name: configured target contains controls")
    if configured_context_tokens is not None and (
        not isinstance(configured_context_tokens, int)
        or isinstance(configured_context_tokens, bool)
        or configured_context_tokens <= 0
    ):
        raise ValueError("configured_context_tokens: expected a positive integer or None")
    return base


def open_connection(host: str, port: int, timeout: float) -> _ConnectionProtocol:
    """Create the one direct standard-library connection used by a read."""
    return cast(
        "_ConnectionProtocol",
        HTTPConnection(host, port, timeout=timeout),
    )


def _response_protocol_ok(
    response: object,
) -> tuple[int, Callable[[int], object]] | None:
    try:
        typed_response = cast("_ResponseProtocol", response)
        status = typed_response.status
        read = typed_response.read
    except Exception:
        return None
    if (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 100 <= status <= 599
        and callable(read)
    ):
        return status, read
    return None


def _close_connection(connection: object | None) -> None:
    if connection is None:
        return
    try:
        close = cast("_CloseProtocol", connection).close
        if callable(close):
            _ = close()
    except Exception:
        # Cleanup failures must not replace the safe collection result.
        pass


def _read_call(host: str, port: int, path: str) -> tuple[_CallOutcome, object]:
    """Perform one fixed GET, normalize transport failures, and decode JSON."""
    connection: _ConnectionProtocol | None = None
    try:
        connection = open_connection(host, port, TIMEOUT_SECONDS)
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        validated = _response_protocol_ok(response)
        if validated is None:
            return "invalid_response", None
        status, read = validated
        if status != 200:
            return "http_error", None
        body = read(MAX_BODY_BYTES + 1)
        if not isinstance(body, bytes):
            return "invalid_response", None
        if len(body) > MAX_BODY_BYTES:
            return "unreadable", None
        try:
            return "ok", _decode_strict(body)
        except (ValueError, RecursionError, MemoryError):
            return "schema_changed", None
    except Exception:
        # Provider-controlled transport exceptions never escape or enter output.
        return "transport_fail", None
    finally:
        _close_connection(connection)


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
        local_runtime=LocalRuntime(
            reachable=reachable,
            model_presence=model_presence,
            model_name=model_name,
            configured_context_tokens=configured_context_tokens,
            effective_context_tokens=effective_context_tokens,
        ),
    )


def _primary_status(outcome: _CallOutcome) -> str:
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
    """Collect one honest local runtime snapshot using at most three GETs."""
    base = _require_config(endpoint, model_name, configured_context_tokens)
    parts = urllib.parse.urlsplit(base)
    host = cast("str", parts.hostname)
    port = cast("int", parts.port)

    probe_outcome, probe_payload = _read_call(host, port, "/api/version")
    if probe_outcome != "ok":
        return _snapshot(
            retrieved_at=retrieved_at,
            status=_primary_status(probe_outcome),
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

    tags_outcome, tags_payload = _read_call(host, port, "/api/tags")
    if tags_outcome != "ok":
        return _snapshot(
            retrieved_at=retrieved_at,
            status="schema_changed" if tags_outcome == "schema_changed" else "unknown",
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
    ps_outcome, ps_payload = _read_call(host, port, "/api/ps")
    effective_context_tokens: int | None = None
    status = "unknown" if listing_digest is None else "ok"
    if ps_outcome == "ok":
        loaded = parse_ollama_ps_response(ps_payload)
        if loaded is None:
            status = "schema_changed"
        elif (entry := loaded.get(model_name)) is not None:
            ps_digest, context_length = entry
            if ps_digest is not None and ps_digest == listing_digest:
                effective_context_tokens = context_length
            else:
                status = "unknown"
    elif ps_outcome == "schema_changed":
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
