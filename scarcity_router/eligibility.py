"""Execution-eligibility contract for Scarcity Router (v1).

Pure, provider-independent, standard-library only. Decision D-039.

The capacity contract (docs/capacity-model.md) answers "how much quota does
this provider have right now"; this contract answers a different question:
"may unattended execution on this provider's subscription billing path start
at all". The two concepts are deliberately never collapsed: a provider can
have healthy quota windows and still be execution-ineligible (for OpenAI, a
purchased-credit balance present), and an ineligible provider is excluded
BEFORE routing so the selector can choose the next safe candidate.

Separate concepts, separate versions: this contract carries its own
``schema_version`` (integer ``1``) and is never merged into
``CapacitySnapshot`` (v3). Eligibility reports are typed selector inputs like
capacity snapshots: provider-generic data with a closed state vocabulary and
a closed reason-code vocabulary. Only providers with a billing/execution
surface that can observe eligibility produce reports today (OpenAI/Codex);
absence of a report for a provider means the eligibility stage never applies
to that provider — it is never read as "eligible".

Invariants enforced at construction (mirroring the capacity module):

- ``state`` is a closed-vocabulary member; ``reason_codes`` is a closed
  deterministic tuple;
- ``state == "eligible"`` carries no reason codes;
- any other state carries at least one reason code;
- serialized shape is exact: unknown keys are rejected on deserialization and
  unset optional values are omitted (never ``null``).

Raw provider text, account ids, balances and credentials never enter any
value this module accepts or emits. Reasons are classifications, not provider
payload excerpts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, cast

from .errors import CapacityValidationError

ELIGIBILITY_SCHEMA_VERSION = 1

ELIGIBILITY_STATES: frozenset[str] = frozenset({
    "eligible",
    "allowance_unavailable",
    "policy_blocked",
    "unknown",
})

ELIGIBILITY_REASON_CODES: frozenset[str] = frozenset({
    # Explicitly forbidden account states (owner policy: purchased-credit
    # usage is forbidden for unattended execution).
    "purchased_credits_present",
    # Mandatory account fields that could not be established explicitly
    # (unknown == unsafe; never inferred from missing telemetry).
    "credits_state_unknown",
    "ordinary_usage_unknown",
    "spend_control_state_unknown",
    # Included-allowance blockers and exhaustion.
    "ordinary_usage_not_allowed",
    "spend_control_reached",
    "rate_limit_reached",
    "individual_limit_exhausted",
    "upsell_present",
    "included_window_exhausted",
    # Telemetry could not be read as a validated observation at all.
    "telemetry_unavailable",
    "telemetry_auth_required",
    "telemetry_unsupported",
    "telemetry_invalid",
})

_CANONICAL_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


def _v_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CapacityValidationError(
            f"{field}: expected str, got {type(value).__name__}"
        )
    return value


def _v_safe_id(value: object, field: str) -> str:
    s = _v_str(value, field)
    if not _SAFE_ID_RE.match(s):
        raise CapacityValidationError(
            f"{field}: unsafe identifier {s!r}; "
            + "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
        )
    return s


def _v_enum(value: object, allowed: frozenset[str], field: str) -> str:
    s = _v_str(value, field)
    if s not in allowed:
        raise CapacityValidationError(
            f"{field}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def _v_ts(value: object, field: str) -> str:
    s = _v_str(value, field)
    if not _CANONICAL_TS_RE.match(s):
        raise CapacityValidationError(
            f"{field}: non-canonical timestamp {s!r}; "
            + "expected YYYY-MM-DDTHH:MM:SS.sssZ"
        )
    try:
        _ = datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError:
        raise CapacityValidationError(f"{field}: invalid date/time in {s!r}")
    return s


def _as_str_object_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


@dataclass(frozen=True)
class ExecutionEligibility:
    """One normalized execution-eligibility observation (v1, D-039).

    ``state`` classifies whether unattended execution on this provider's
    subscription path may start now; ``reason_codes`` carries every detected
    blocker classification (empty exactly when ``state == "eligible"``).
    Reports are observations, not commands: the selector owns exclusion, the
    caller owns execution. The fields describe the same one observation as
    the paired capacity snapshot (identical ``provider``/``source``/
    ``retrieved_at``); no freshness policy exists here.
    """

    schema_version: int
    provider: str
    source: str
    retrieved_at: str
    state: str
    reason_codes: tuple[str, ...]

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "provider",
        "source",
        "retrieved_at",
        "state",
        "reason_codes",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        # Runtime shape guard: direct construction can bypass the annotation.
        sv = self.schema_version
        if sv.__class__ is not int:
            raise CapacityValidationError(
                f"schema_version: expected int, got {type(sv).__name__}"
            )
        if sv != ELIGIBILITY_SCHEMA_VERSION:
            raise CapacityValidationError(
                f"schema_version: expected int {ELIGIBILITY_SCHEMA_VERSION}, got {sv!r}"
            )
        _ = _v_safe_id(self.provider, "provider")
        _ = _v_safe_id(self.source, "source")
        _ = _v_ts(self.retrieved_at, "retrieved_at")
        state = _v_enum(self.state, ELIGIBILITY_STATES, "state")
        # Runtime shape guard: direct construction can bypass the annotation.
        if self.reason_codes.__class__ is not tuple:
            raise CapacityValidationError(
                "reason_codes: expected tuple, got "
                + f"{type(self.reason_codes).__name__}"
            )
        codes = tuple(
            _v_enum(code, ELIGIBILITY_REASON_CODES, "reason_codes.code")
            for code in self.reason_codes
        )
        if len(set(codes)) != len(codes):
            raise CapacityValidationError(
                "reason_codes: duplicate reason codes are not permitted"
            )
        canonical = tuple(sorted(codes))
        if canonical != self.reason_codes:
            raise CapacityValidationError(
                "reason_codes: must be deterministically sorted"
            )
        if state == "eligible" and canonical:
            raise CapacityValidationError(
                "state 'eligible' must not carry reason codes"
            )
        if state != "eligible" and not canonical:
            raise CapacityValidationError(
                f"state {state!r} requires at least one reason code"
            )

    @classmethod
    def from_dict(cls, d: object) -> "ExecutionEligibility":
        m = _as_str_object_mapping(d)
        if m is None:
            raise CapacityValidationError(
                "eligibility: expected a dict-like mapping, got "
                + f"{type(d).__name__}"
            )
        allowed = frozenset(cls._REQUIRED) | frozenset(cls._OPTIONAL)
        extra = set(m.keys()) - allowed
        if extra:
            raise CapacityValidationError(
                f"eligibility: unknown keys {sorted(extra)}"
            )
        missing = [key for key in cls._REQUIRED if key not in m]
        if missing:
            raise CapacityValidationError(
                f"eligibility: missing required keys {sorted(missing)}"
            )
        raw_codes = m["reason_codes"]
        if not isinstance(raw_codes, list):
            raise CapacityValidationError(
                "reason_codes: expected list, got "
                + f"{type(raw_codes).__name__}"
            )
        return cls(
            schema_version=cast(int, m["schema_version"]),
            provider=_v_safe_id(m["provider"], "provider"),
            source=_v_safe_id(m["source"], "source"),
            retrieved_at=_v_ts(m["retrieved_at"], "retrieved_at"),
            state=_v_enum(m["state"], ELIGIBILITY_STATES, "state"),
            reason_codes=tuple(cast("list[str]", raw_codes)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "source": self.source,
            "retrieved_at": self.retrieved_at,
            "state": self.state,
            "reason_codes": list(self.reason_codes),
        }

    def validate(self) -> "ExecutionEligibility":
        """Re-validate an already-constructed report; returns self or raises."""
        return ExecutionEligibility.from_dict(self.to_dict())
