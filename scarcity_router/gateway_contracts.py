"""Execution-surface v1 contracts: limits, errors and client identity (M03).

Issue #88 under the A0 execution-gateway architecture (D-040 through D-045).
This module holds the frozen execution-surface building blocks that the
gateway coordinator, the OpenAI-compatible wire mapping and the execution
server all share:

- :data:`EXECUTION_SURFACE_VERSION` — the separately versioned
  OpenAI-compatible execution contract ("execution surface v1", D-045).
  Its version is independent of machine-interface v1, of the route-decision
  contract and of every other versioned family; they are never collapsed.
- :class:`GatewayLimits` — the administrator-configurable admission limits
  (D-044): request-body size, estimated input context, output tokens,
  global and per-client concurrency, and the execution-time bound. Limits
  are enforced at admission, before any dispatch.
- :class:`GatewayError` — the typed execution-surface error with its own
  OpenAI-compatible vocabulary (``invalid_request_error``,
  ``authentication_error``, ``permission_error``, ``not_found_error``,
  ``rate_limit_error``, ``timeout_error``, ``api_error``). Per D-045 this
  vocabulary NEVER reuses or extends the closed machine-interface
  ``invalid_request``/``internal_error`` pair.
- :class:`UsageTokens` and :class:`UsageAccounting` — honest usage
  representation (D-043): provider-reported and estimated usage stay
  distinguishable, one external request may carry several internal calls,
  and "unavailable" is explicit instead of a fabricated zero.
- :class:`ClientKeyDirectory` — the authenticated inference-client
  identity seam (D-044): inference clients authenticate with API keys in
  the ``Authorization: Bearer`` header (never in URLs); the directory
  holds only SHA-256 key hashes, so a directory disclosure never leaks
  usable keys. Issuance/rotation/revocation UX is M09 scope.

Nothing in this module performs I/O, reads the environment or touches a
provider. Every value is construction-validated, deterministic and free of
prompt/response content and secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .gateway_validation import (
    exact_shape,
    v_instance,
    v_int,
    v_safe_id,
    v_str,
    v_text,
)

# ── Frozen value sets ─────────────────────────────────────────────────────────

EXECUTION_SURFACE_VERSION = 1

# Closed execution-surface error-type vocabulary (OpenAI-compatible client
# conventions, D-045). Deliberately disjoint from the machine-interface v1
# error codes ``invalid_request``/``internal_error``.
ERROR_TYPE_INVALID_REQUEST = "invalid_request_error"
ERROR_TYPE_AUTHENTICATION = "authentication_error"
ERROR_TYPE_PERMISSION = "permission_error"
ERROR_TYPE_NOT_FOUND = "not_found_error"
ERROR_TYPE_RATE_LIMIT = "rate_limit_error"
ERROR_TYPE_TIMEOUT = "timeout_error"
ERROR_TYPE_API = "api_error"

ERROR_TYPES: frozenset[str] = frozenset({
    ERROR_TYPE_INVALID_REQUEST,
    ERROR_TYPE_AUTHENTICATION,
    ERROR_TYPE_PERMISSION,
    ERROR_TYPE_NOT_FOUND,
    ERROR_TYPE_RATE_LIMIT,
    ERROR_TYPE_TIMEOUT,
    ERROR_TYPE_API,
})

# Closed usage-source vocabulary for honest accounting (D-043).
USAGE_SOURCE_PROVIDER_REPORTED = "provider_reported"
USAGE_SOURCE_ESTIMATED = "estimated"
USAGE_SOURCE_MIXED = "mixed"
USAGE_SOURCE_UNAVAILABLE = "unavailable"

USAGE_SOURCES: frozenset[str] = frozenset({
    USAGE_SOURCE_PROVIDER_REPORTED,
    USAGE_SOURCE_ESTIMATED,
    USAGE_SOURCE_MIXED,
    USAGE_SOURCE_UNAVAILABLE,
})

_KEY_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_LIMIT_FIELDS: tuple[str, ...] = (
    "max_request_body_bytes",
    "max_input_context_tokens",
    "max_output_tokens",
    "max_concurrent_executions",
    "max_concurrent_executions_per_client",
    "execution_time_limit_seconds",
)


# ── Admission limits (D-044) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GatewayLimits:
    """Administrator-configurable admission limits with safe defaults (D-044).

    Every limit is enforced at admission, before any dispatch: the request
    body against ``max_request_body_bytes`` (HTTP layer), the estimated
    input context against ``max_input_context_tokens`` and the requested
    output ceiling against ``max_output_tokens`` (coordinator), concurrent
    executions against ``max_concurrent_executions`` /
    ``max_concurrent_executions_per_client`` (reservation), and the wall
    clock against ``execution_time_limit_seconds`` (dispatch deadline).
    Spending ceilings are the routing core's :class:`SpendingLimit`
    (M02/D-042) — there is no second spending-limit system.

    There is no unlimited sentinel: every field is a positive integer.
    """

    max_request_body_bytes: int = 1_048_576
    max_input_context_tokens: int = 131_072
    max_output_tokens: int = 16_384
    max_concurrent_executions: int = 4
    max_concurrent_executions_per_client: int = 2
    execution_time_limit_seconds: int = 300

    def __post_init__(self) -> None:
        values: tuple[tuple[str, int], ...] = (
            ("max_request_body_bytes", self.max_request_body_bytes),
            ("max_input_context_tokens", self.max_input_context_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("max_concurrent_executions", self.max_concurrent_executions),
            (
                "max_concurrent_executions_per_client",
                self.max_concurrent_executions_per_client,
            ),
            ("execution_time_limit_seconds", self.execution_time_limit_seconds),
        )
        for name, value in values:
            _ = v_int(value, f"gateway_limits.{name}", lo=1)
        if self.max_concurrent_executions_per_client > self.max_concurrent_executions:
            raise ValueError(
                "gateway_limits: per-client concurrency "
                + f"{self.max_concurrent_executions_per_client} exceeds the "
                + f"global limit {self.max_concurrent_executions}"
            )

    @classmethod
    def from_dict(cls, d: object) -> "GatewayLimits":
        dd = exact_shape(d, (), _LIMIT_FIELDS, "gateway_limits")
        values: dict[str, int] = {}
        for name in _LIMIT_FIELDS:
            if name in dd:
                values[name] = v_int(dd[name], f"gateway_limits.{name}", lo=1)
        return cls(**values)

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in _LIMIT_FIELDS}


# ── Usage accounting (D-043 honesty rules) ────────────────────────────────────


def _v_usage(value: object, field: str) -> "UsageTokens":
    return v_instance(value, UsageTokens, f"usage_accounting.{field}")


@dataclass(frozen=True)
class UsageTokens:
    """Token usage of one internal provider call (or an aggregate).

    ``prompt_tokens``/``completion_tokens`` are non-negative integers; a
    missing number is represented by omitting the object, never by zero
    (zero is a known measured value, not an absent one).
    """

    prompt_tokens: int
    completion_tokens: int

    def __post_init__(self) -> None:
        _ = v_int(self.prompt_tokens, "usage.prompt_tokens", lo=0)
        _ = v_int(self.completion_tokens, "usage.completion_tokens", lo=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @classmethod
    def from_dict(cls, d: object) -> "UsageTokens":
        dd = exact_shape(d, ("prompt_tokens", "completion_tokens"), (), "usage")
        return cls(
            prompt_tokens=v_int(dd["prompt_tokens"], "usage.prompt_tokens", lo=0),
            completion_tokens=v_int(
                dd["completion_tokens"], "usage.completion_tokens", lo=0
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    def __add__(self, other: "UsageTokens") -> "UsageTokens":
        _ = v_instance(other, UsageTokens, "usage.__add__")
        return UsageTokens(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


@dataclass(frozen=True)
class UsageAccounting:
    """Honest usage summary across one request's internal calls (D-043).

    Provider-reported and estimated totals are kept separately and
    ``usage_source`` says which one the response's plain ``usage`` numbers
    represent: ``provider_reported`` when the aggregate's numbers are all
    provider-reported, ``estimated`` when only estimates exist, ``mixed``
    when a multi-call fan-out carries both kinds, and ``unavailable`` when
    no call produced any usage fact (never fabricated as zero-reported).
    """

    usage_source: str
    provider_reported_usage: UsageTokens | None = None
    estimated_usage: UsageTokens | None = None

    def __post_init__(self) -> None:
        from .gateway_validation import v_enum

        _ = v_enum(self.usage_source, USAGE_SOURCES, "usage_accounting.usage_source")
        if self.provider_reported_usage is not None:
            _ = _v_usage(self.provider_reported_usage, "provider_reported_usage")
        if self.estimated_usage is not None:
            _ = _v_usage(self.estimated_usage, "estimated_usage")
        if self.usage_source == USAGE_SOURCE_UNAVAILABLE and (
            self.provider_reported_usage is not None or self.estimated_usage is not None
        ):
            raise ValueError(
                "usage_accounting: 'unavailable' carries no usage totals"
            )
        if self.usage_source == USAGE_SOURCE_PROVIDER_REPORTED and (
            self.provider_reported_usage is None
        ):
            raise ValueError(
                "usage_accounting: 'provider_reported' requires "
                + "provider_reported_usage"
            )
        if self.usage_source == USAGE_SOURCE_ESTIMATED and (
            self.estimated_usage is None
        ):
            raise ValueError(
                "usage_accounting: 'estimated' requires estimated_usage"
            )
        if self.usage_source == USAGE_SOURCE_MIXED and (
            self.provider_reported_usage is None or self.estimated_usage is None
        ):
            raise ValueError(
                "usage_accounting: 'mixed' requires both usage totals"
            )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"usage_source": self.usage_source}
        if self.provider_reported_usage is not None:
            out["provider_reported_usage"] = self.provider_reported_usage.to_dict()
        if self.estimated_usage is not None:
            out["estimated_usage"] = self.estimated_usage.to_dict()
        return out


# ── Typed execution-surface errors (OpenAI-compatible vocabulary) ─────────────


@dataclass(frozen=True)
class GatewayError(Exception):
    """One typed execution-surface failure (D-045 error vocabulary).

    ``error_type`` is from the closed OpenAI-compatible vocabulary above,
    ``http_status`` is the paired transport status, ``code`` is a stable
    machine-readable detail token and ``param`` names the offending request
    parameter. ``message`` is a safe structural message: never a traceback,
    never a provider payload, never request content values (parameter
    NAMES are safe; values never are).
    """

    http_status: int
    error_type: str
    message: str
    code: str | None = None
    param: str | None = None

    def __post_init__(self) -> None:
        from .gateway_validation import v_enum

        _ = v_enum(self.error_type, ERROR_TYPES, "gateway_error.error_type")
        _ = v_int(self.http_status, "gateway_error.http_status", lo=400)
        _ = v_text(self.message, "gateway_error.message", max_len=500)
        if self.code is not None:
            _ = v_safe_id(self.code, "gateway_error.code")
        if self.param is not None:
            _ = v_text(self.param, "gateway_error.param", max_len=128)

    @staticmethod
    def invalid_request(
        message: str, *, code: str | None = None, param: str | None = None
    ) -> "GatewayError":
        return GatewayError(
            http_status=400,
            error_type=ERROR_TYPE_INVALID_REQUEST,
            message=message,
            code=code,
            param=param,
        )

    @staticmethod
    def request_too_large(message: str) -> "GatewayError":
        return GatewayError(
            http_status=413,
            error_type=ERROR_TYPE_INVALID_REQUEST,
            message=message,
            code="request_too_large",
        )

    @staticmethod
    def authentication(message: str = "invalid API key") -> "GatewayError":
        return GatewayError(
            http_status=401,
            error_type=ERROR_TYPE_AUTHENTICATION,
            message=message,
        )

    @staticmethod
    def permission(message: str, *, code: str | None = None) -> "GatewayError":
        return GatewayError(
            http_status=403,
            error_type=ERROR_TYPE_PERMISSION,
            message=message,
            code=code,
        )

    @staticmethod
    def not_found(message: str, *, code: str | None = None) -> "GatewayError":
        return GatewayError(
            http_status=404,
            error_type=ERROR_TYPE_NOT_FOUND,
            message=message,
            code=code,
        )

    @staticmethod
    def rate_limit(message: str, *, code: str | None = None) -> "GatewayError":
        return GatewayError(
            http_status=429,
            error_type=ERROR_TYPE_RATE_LIMIT,
            message=message,
            code=code,
        )

    @staticmethod
    def timeout(message: str, *, code: str | None = None) -> "GatewayError":
        return GatewayError(
            http_status=408,
            error_type=ERROR_TYPE_TIMEOUT,
            message=message,
            code=code,
        )

    @staticmethod
    def api(
        message: str, *, code: str | None = None, http_status: int = 500
    ) -> "GatewayError":
        if not 500 <= http_status <= 599:
            raise ValueError("gateway_error: api errors use a 5xx status")
        return GatewayError(
            http_status=http_status,
            error_type=ERROR_TYPE_API,
            message=message,
            code=code,
        )

    def to_payload(self) -> dict[str, object]:
        """The OpenAI-compatible error envelope for this failure."""
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


# ── Inference-client identity (D-044) ─────────────────────────────────────────


def hash_client_key(secret: str) -> str:
    """SHA-256 hex digest of one API key (the only stored representation).

    The directory never stores key material, only this digest; comparing
    digests with :meth:`ClientKeyDirectory.authenticate` is a constant-time
    comparison. Keys themselves are transient input and are never logged,
    returned or persisted anywhere else.
    """
    _ = v_text(secret, "client key", max_len=4096)
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ClientKeyDirectory:
    """Authenticated inference-client identities (D-044 identity class).

    Maps client ids to SHA-256 API-key hashes. Authentication compares the
    presented bearer key's digest against the stored hashes in constant
    time; a missing directory entry, a wrong key and an empty key are all
    the same unauthenticated outcome. Client API keys authorize inference
    only — no administration surface exists anywhere in the gateway.

    Constructed from administrator configuration (in-process seam; the
    issuance/rotation/revocation UX and the durable D-044 store are M09
    scope). A disclosed directory yields only hashes, never usable keys.
    """

    def __init__(self, entries: Mapping[str, str]) -> None:
        self._hashes: dict[str, str] = {}
        for client_id, key_hash in entries.items():
            _ = v_safe_id(client_id, "client_key_directory.client_id")
            digest = v_str(key_hash, f"client_key_directory[{client_id!r}]")
            if not _KEY_HASH_RE.match(digest):
                raise ValueError(
                    f"client_key_directory[{client_id!r}]: expected a 64-char "
                    + "lowercase hex SHA-256 key hash"
                )
            self._hashes[client_id] = digest

    @classmethod
    def from_secrets(cls, entries: Mapping[str, str]) -> "ClientKeyDirectory":
        """Build a directory from in-memory key material (test/bootstrap seam).

        Hashes each secret immediately; the caller is responsible for the
        lifetime of its own in-memory secrets. Nothing here stores or logs
        the secret values.
        """
        return cls(
            {
                client_id: hash_client_key(secret)
                for client_id, secret in entries.items()
            }
        )

    def __len__(self) -> int:
        return len(self._hashes)

    @property
    def client_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._hashes))

    def authenticate(self, bearer_key: str | None) -> str | None:
        """The client id for this presented key, or ``None``.

        Constant-time digest comparison; a ``None``/empty presented key is
        unauthenticated without touching the directory.
        """
        if not bearer_key:
            return None
        presented = hash_client_key(bearer_key)
        for client_id, key_hash in self._hashes.items():
            if hmac.compare_digest(presented, key_hash):
                return client_id
        return None

    def hash_for(self, client_id: str) -> str:
        """The stored SHA-256 hash for one client id (migration seam).

        Exists for the M09 one-time ``--import-client-keys`` migration
        (``scarcity_router.control_server``), which copies hashes — never
        key material — into the durable store. Raises for unknown ids.
        """
        try:
            return self._hashes[client_id]
        except KeyError:
            raise ValueError(f"unknown client id {client_id!r}") from None


__all__ = [
    "ERROR_TYPES",
    "ERROR_TYPE_API",
    "ERROR_TYPE_AUTHENTICATION",
    "ERROR_TYPE_INVALID_REQUEST",
    "ERROR_TYPE_NOT_FOUND",
    "ERROR_TYPE_PERMISSION",
    "ERROR_TYPE_RATE_LIMIT",
    "ERROR_TYPE_TIMEOUT",
    "EXECUTION_SURFACE_VERSION",
    "USAGE_SOURCES",
    "USAGE_SOURCE_ESTIMATED",
    "USAGE_SOURCE_MIXED",
    "USAGE_SOURCE_PROVIDER_REPORTED",
    "USAGE_SOURCE_UNAVAILABLE",
    "ClientKeyDirectory",
    "GatewayError",
    "GatewayLimits",
    "UsageAccounting",
    "UsageTokens",
    "hash_client_key",
]
