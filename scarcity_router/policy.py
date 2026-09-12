"""Resource-policy primitives for Scarcity Router (M2d, D-026).

Pure, provider-independent, standard-library only. No filesystem, network,
environment, subprocess or provider access, and no clock reads: callers
supply every observation and every timezone-aware evaluation instant
explicitly, and every function is deterministic over its arguments. The only
environment-derived data is the IANA time-zone database read through the
standard-library ``zoneinfo`` module when a configured weekly blackout rule
is constructed or evaluated — no third-party timezone package.

Implements the frozen M2d resource-policy contracts (D-026, D-021):

- the unknown-capacity policy modes ``degraded`` and ``strict`` (unknown
  capacity has no numeric penalty and is never ranked against one);
- reservation rules that target a capacity *scope* (``CapacityScopeRef``),
  never a model name, because shared subscriptions — Luna/Sol on
  ``openai/codex``, GLM-5.3/GLM-5.3-Flash on ``zai/coding_plan`` — mean a
  model-specific reservation would incorrectly imply independent quota;
- timezone-aware weekly blackout rules with half-open ``[start, end)``
  local-time semantics, cross-midnight support and no hard-coded vendor
  schedule;
- timezone-aware weekly happy-hour rules with the same schedule semantics
  plus optional inclusive local date bounds (limited-time vendor campaigns):
  a matching window marks candidates as quota-preferred for ranking only
  and never changes any eligibility outcome;
- the normalized D-021 ``ReplenishmentState`` with the ``ignore``,
  ``advisory`` and ``recoverable`` visibility modes; replenishment is never
  current capacity, never changes a scarcity assessment and is never
  consumed or redeemed by the broker;
- the ``UserPolicy`` core container with deterministic canonical
  serialization.

This module evaluates single candidates under policy primitives only. It
does not compare candidates, does not rank, does not implement ranking modes
(``quality-first``, ``conserve-openai``, ``conserve-zai``) and does not
implement ``select`` — that is the M2e selector's responsibility.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar, TypeVar, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .capacity import CapacitySnapshot
from .errors import SelectionContractValidationError
from .scarcity import ScarcityAssessment
from .selection_types import (
    SUPPORTED_PROVIDERS,
    TASK_LEVELS,
    CapacityScopeRef,
    ModelIdentity,
)

# ── Frozen value sets ─────────────────────────────────────────────────────────

UNKNOWN_CAPACITY_MODE_DEGRADED = "degraded"
UNKNOWN_CAPACITY_MODE_STRICT = "strict"

UNKNOWN_CAPACITY_MODES: frozenset[str] = frozenset({
    UNKNOWN_CAPACITY_MODE_DEGRADED,
    UNKNOWN_CAPACITY_MODE_STRICT,
})

REPLENISHMENT_MODE_IGNORE = "ignore"
REPLENISHMENT_MODE_ADVISORY = "advisory"
REPLENISHMENT_MODE_RECOVERABLE = "recoverable"

REPLENISHMENT_MODES: frozenset[str] = frozenset({
    REPLENISHMENT_MODE_IGNORE,
    REPLENISHMENT_MODE_ADVISORY,
    REPLENISHMENT_MODE_RECOVERABLE,
})

RESERVATION_STATES: frozenset[str] = frozenset({"known", "unknown"})

# A reservation targets a known normalized resource and a known window kind;
# "unknown" resource/kind can never identify a concrete target window.
RESERVATION_RESOURCES: frozenset[str] = frozenset({"tokens", "time"})
RESERVATION_WINDOW_KINDS: frozenset[str] = frozenset({"five_hour", "weekly"})

# Explicit weekday vocabulary, in canonical order.
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

POLICY_REASON_CODES: frozenset[str] = frozenset({
    # unknown-capacity policy
    "unknown_capacity_degraded",
    "unknown_capacity_blocked",
    # explicit exhaustion (scarcity reason reused by the policy layer)
    "capacity_exhausted",
    # reservation evaluation
    "reservation_triggered",
    "reservation_blocked",
    "reservation_permitted_by_task_level",
    "reservation_scope_snapshot_missing",
    "reservation_scope_snapshot_not_ok",
    "reservation_window_missing",
    "reservation_percentage_unknown",
    # blackout policy
    "policy_blocked",
    # replenishment visibility
    "replenishment_available",
    "replenishment_recoverable",
    "human_action_required",
})

# Pre-sorted reason-code tuples for exact deterministic decisions.
_RECOVERABLE_CODES: tuple[str, ...] = (
    "human_action_required",
    "replenishment_available",
    "replenishment_recoverable",
)
_AVAILABLE_CODES: tuple[str, ...] = ("replenishment_available",)
_POLICY_BLOCKED_CODES: tuple[str, ...] = ("policy_blocked",)

# ── Validators (single source of truth for the M2d policy rules) ──────────────

_T = TypeVar("_T")

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

_HM_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

_TZ_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_+\-]*(/[A-Za-z0-9][A-Za-z0-9_+\-]*)*$"
)

_CANONICAL_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _v_str(value: object, fld: str) -> str:
    if not isinstance(value, str):
        raise SelectionContractValidationError(
            f"{fld}: expected str, got {type(value).__name__}"
        )
    return value


def _v_enum(value: object, allowed: frozenset[str], fld: str) -> str:
    s = _v_str(value, fld)
    if s not in allowed:
        raise SelectionContractValidationError(
            f"{fld}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def _v_bool(value: object, fld: str) -> bool:
    if not isinstance(value, bool):
        raise SelectionContractValidationError(
            f"{fld}: expected bool, got {type(value).__name__} ({value!r})"
        )
    return value


def _v_int(
    value: object,
    fld: str,
    *,
    lo: int | None = None,
    hi: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectionContractValidationError(
            f"{fld}: expected int, got {type(value).__name__} ({value!r})"
        )
    if lo is not None and value < lo:
        raise SelectionContractValidationError(f"{fld}: value {value} < {lo}")
    if hi is not None and value > hi:
        raise SelectionContractValidationError(f"{fld}: value {value} > {hi}")
    return value


def _v_safe_id(value: object, fld: str) -> str:
    s = _v_str(value, fld)
    if not _SAFE_ID_RE.match(s):
        raise SelectionContractValidationError(
            f"{fld}: unsafe identifier {s!r}; "
            + "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
        )
    return s


def _v_provider(value: object, fld: str) -> str:
    s = _v_safe_id(value, fld)
    if s not in SUPPORTED_PROVIDERS:
        raise SelectionContractValidationError(
            f"{fld}: unsupported provider {s!r}; supported providers are "
            + f"exactly {sorted(SUPPORTED_PROVIDERS)}"
        )
    return s


def _v_ts(value: object, fld: str) -> str:
    """Canonical UTC timestamp: RFC 3339 with literal Z and exactly .sss."""
    s = _v_str(value, fld)
    if not _CANONICAL_TS_RE.match(s):
        raise SelectionContractValidationError(
            f"{fld}: non-canonical timestamp {s!r}; "
            + "expected YYYY-MM-DDTHH:MM:SS.sssZ"
        )
    try:
        _ = datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError:
        raise SelectionContractValidationError(
            f"{fld}: invalid date/time in {s!r}"
        )
    return s


def _v_hm(value: object, fld: str) -> str:
    """Strict 24-hour ``HH:MM`` local wall time."""
    s = _v_str(value, fld)
    if not _HM_RE.match(s):
        raise SelectionContractValidationError(
            f"{fld}: invalid local time {s!r}; expected strict 24-hour HH:MM"
        )
    return s


def _v_date(value: object, fld: str) -> str:
    """Strict local calendar date ``YYYY-MM-DD``."""
    s = _v_str(value, fld)
    if not _DATE_RE.match(s):
        raise SelectionContractValidationError(
            f"{fld}: invalid date {s!r}; expected strict YYYY-MM-DD"
        )
    try:
        _ = date.fromisoformat(s)
    except ValueError:
        raise SelectionContractValidationError(
            f"{fld}: invalid calendar date in {s!r}"
        ) from None
    return s


def _hm_minutes(value: str) -> int:
    match = _HM_RE.match(value)
    if match is None:  # construction-validated; guard the parse anyway
        raise SelectionContractValidationError(
            f"invalid local time {value!r}; expected strict 24-hour HH:MM"
        )
    hours = int(match.group(1))
    minutes = int(match.group(2))
    return hours * 60 + minutes


def _v_timezone(value: object, fld: str) -> str:
    """IANA time-zone name resolvable through the standard-library zoneinfo."""
    s = _v_str(value, fld)
    if not _TZ_KEY_RE.match(s):
        raise SelectionContractValidationError(
            f"{fld}: invalid IANA time-zone name {s!r}"
        )
    try:
        _ = ZoneInfo(s)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise SelectionContractValidationError(
            f"{fld}: unknown or unloadable IANA time zone {s!r}"
        ) from exc
    return s


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise SelectionContractValidationError(
            f"unknown or unloadable IANA time zone {timezone!r}"
        ) from exc


def _weekly_window_contains(
    timezone: str,
    weekdays: tuple[str, ...],
    start_local: str,
    end_local: str,
    at: datetime,
) -> bool:
    """Half-open ``[start, end)`` weekly local-window test for one instant.

    Shared by blackout and happy-hour rules so both schedule kinds have
    exactly the same boundary semantics: strict ``HH:MM`` wall times, the
    explicit weekday vocabulary, and cross-midnight intervals that run from
    start on each configured weekday through end on the following day.
    """
    local = at.astimezone(_zone(timezone))
    minute_of_day = local.hour * 60 + local.minute
    start = _hm_minutes(start_local)
    end = _hm_minutes(end_local)
    day = WEEKDAYS[local.weekday()]
    previous_day = WEEKDAYS[(local.weekday() - 1) % 7]
    configured = frozenset(weekdays)
    if start < end:
        return day in configured and start <= minute_of_day < end
    # Cross-midnight: [start, 24:00) on the configured start days plus
    # [00:00, end) on the following day.
    if day in configured and minute_of_day >= start:
        return True
    return previous_day in configured and minute_of_day < end


def _v_weekdays(value: object, fld: str) -> tuple[str, ...]:
    """Non-empty duplicate-free weekday tuple in canonical order."""
    if not isinstance(value, tuple):
        raise SelectionContractValidationError(
            f"{fld}: expected tuple, got {type(value).__name__}"
        )
    seen: set[str] = set()
    for item in cast("tuple[object, ...]", value):
        day = _v_enum(item, frozenset(WEEKDAYS), fld)
        if day in seen:
            raise SelectionContractValidationError(
                f"{fld}: duplicate weekday {day!r}"
            )
        seen.add(day)
    if not seen:
        raise SelectionContractValidationError(
            f"{fld}: at least one weekday is required"
        )
    # Canonical stored form: canonical weekday order makes equality and
    # serialized output independent of construction order.
    return tuple(day for day in WEEKDAYS if day in seen)


def _v_aware_datetime(value: object, fld: str) -> datetime:
    """A timezone-aware ``datetime`` instant; naive local times are invalid."""
    if not isinstance(value, datetime):
        raise SelectionContractValidationError(
            f"{fld}: expected datetime, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise SelectionContractValidationError(
            f"{fld}: expected a timezone-aware datetime, got a naive datetime"
        )
    return value


def _v_instance_of(value: object, cls: type[_T], label: str) -> _T:
    """Runtime shape guard for directly constructed contract values."""
    if not isinstance(value, cls):
        raise SelectionContractValidationError(
            f"{label}: expected a {cls.__name__}, got {type(value).__name__}"
        )
    return value


def _v_tuple_of(value: object, item_type: type[_T], label: str) -> None:
    """Runtime shape guard: ``value`` must be a tuple of ``item_type``."""
    if not isinstance(value, tuple):
        raise SelectionContractValidationError(
            f"{label}: expected tuple, got {type(value).__name__}"
        )
    for item in cast("tuple[object, ...]", value):
        if not isinstance(item, item_type):
            raise SelectionContractValidationError(
                f"{label}: element must be a {item_type.__name__}, "
                + f"got {type(item).__name__}"
            )


def _v_reason_codes(value: object, fld: str) -> tuple[str, ...]:
    """Non-duplicate tuple of normalized policy reason codes."""
    if not isinstance(value, tuple):
        raise SelectionContractValidationError(
            f"{fld}: expected tuple, got {type(value).__name__}"
        )
    seen: set[str] = set()
    for item in cast("tuple[object, ...]", value):
        code = _v_enum(item, POLICY_REASON_CODES, fld)
        if code in seen:
            raise SelectionContractValidationError(
                f"{fld}: duplicate reason code {code!r}"
            )
        seen.add(code)
    # Canonical stored form: sorted codes make equality and serialized output
    # deterministic and independent of construction order.
    return tuple(sorted(seen))


def _as_str_object_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary mapping to ``str`` keys, or return ``None``."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _v_exact_shape(
    obj: object,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    label: str,
) -> Mapping[str, object]:
    """Validate the serialized shape of one record and narrow it to a mapping."""
    m = _as_str_object_mapping(obj)
    if m is None:
        raise SelectionContractValidationError(
            f"{label}: expected a dict-like mapping, got {type(obj).__name__}"
        )
    allowed = frozenset(required) | frozenset(optional)
    extra = set(m.keys()) - allowed
    if extra:
        raise SelectionContractValidationError(
            f"{label}: unknown keys {sorted(extra)}"
        )
    missing = [key for key in required if key not in m]
    if missing:
        raise SelectionContractValidationError(
            f"{label}: missing required keys {sorted(missing)}"
        )
    return m


def _v_task_level(value: object, fld: str) -> str:
    s = _v_str(value, fld)
    if s not in TASK_LEVELS:
        raise SelectionContractValidationError(
            f"{fld}: value {s!r} not in allowed set {list(TASK_LEVELS)}"
        )
    return s


# ── Unknown-capacity policy (D-026) ───────────────────────────────────────────


@dataclass(frozen=True)
class UnknownCapacityDecision:
    """One candidate's eligibility under the active unknown-capacity mode.

    An unknown scarcity assessment carries no numeric penalty, so this
    decision never compares it against one: in ``degraded`` mode the
    candidate may remain conditionally usable (``degraded = true``) and the
    future selector must rank a known sufficient candidate ahead; in
    ``strict`` mode unknown capacity is blocked. Known nonzero capacity is
    eligible in both modes; known ``unavailable`` (explicit exhaustion) is
    blocked in both — a policy mode never creates capacity.
    """

    mode: str
    eligible_by_unknown_policy: bool
    degraded: bool
    reason_codes: tuple[str, ...]

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "mode",
        "eligible_by_unknown_policy",
        "degraded",
        "reason_codes",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_enum(self.mode, UNKNOWN_CAPACITY_MODES, "unknown_capacity.mode")
        _ = _v_bool(
            self.eligible_by_unknown_policy,
            "unknown_capacity.eligible_by_unknown_policy",
        )
        _ = _v_bool(self.degraded, "unknown_capacity.degraded")
        codes = _v_reason_codes(
            self.reason_codes, "unknown_capacity.reason_codes"
        )
        object.__setattr__(self, "reason_codes", codes)
        if self.degraded:
            if self.mode != UNKNOWN_CAPACITY_MODE_DEGRADED or (
                not self.eligible_by_unknown_policy
            ):
                raise SelectionContractValidationError(
                    "unknown_capacity: degraded=True requires the degraded "
                    + "mode and eligibility"
                )
            if codes != ("unknown_capacity_degraded",):
                raise SelectionContractValidationError(
                    "unknown_capacity: degraded=True requires exactly the "
                    + "'unknown_capacity_degraded' reason code"
                )
        elif self.eligible_by_unknown_policy:
            if codes:
                raise SelectionContractValidationError(
                    "unknown_capacity: plain eligibility carries no reason "
                    + "codes"
                )
        elif not codes or not (
            {"unknown_capacity_blocked", "capacity_exhausted"} & set(codes)
        ):
            raise SelectionContractValidationError(
                "unknown_capacity: ineligibility requires the "
                + "'unknown_capacity_blocked' or 'capacity_exhausted' "
                + "reason code"
            )

    @classmethod
    def from_dict(cls, d: object) -> "UnknownCapacityDecision":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "unknown_capacity")
        codes_raw = dd["reason_codes"]
        if not isinstance(codes_raw, list):
            raise SelectionContractValidationError(
                "unknown_capacity.reason_codes: expected list, got "
                + f"{type(codes_raw).__name__}"
            )
        codes = tuple(
            _v_enum(code, POLICY_REASON_CODES, "unknown_capacity.reason_codes")
            for code in cast("list[object]", codes_raw)
        )
        return cls(
            mode=_v_enum(
                dd["mode"], UNKNOWN_CAPACITY_MODES, "unknown_capacity.mode"
            ),
            eligible_by_unknown_policy=_v_bool(
                dd["eligible_by_unknown_policy"],
                "unknown_capacity.eligible_by_unknown_policy",
            ),
            degraded=_v_bool(dd["degraded"], "unknown_capacity.degraded"),
            reason_codes=codes,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "eligible_by_unknown_policy": self.eligible_by_unknown_policy,
            "degraded": self.degraded,
            "reason_codes": list(self.reason_codes),
        }


def apply_unknown_capacity_mode(
    mode: str,
    assessment: ScarcityAssessment,
) -> UnknownCapacityDecision:
    """Apply the active unknown-capacity mode to one scarcity assessment.

    Pure and single-candidate: this primitive never compares candidates and
    never ranks unknown capacity against a numeric penalty.
    """
    checked_mode = _v_enum(mode, UNKNOWN_CAPACITY_MODES, "unknown_capacity.mode")
    _ = _v_instance_of(
        assessment, ScarcityAssessment, "apply_unknown_capacity_mode.assessment"
    )
    if assessment.state == "known":
        return UnknownCapacityDecision(
            mode=checked_mode,
            eligible_by_unknown_policy=True,
            degraded=False,
            reason_codes=(),
        )
    if assessment.state == "unavailable":
        return UnknownCapacityDecision(
            mode=checked_mode,
            eligible_by_unknown_policy=False,
            degraded=False,
            reason_codes=("capacity_exhausted",),
        )
    # unknown capacity
    if checked_mode == UNKNOWN_CAPACITY_MODE_DEGRADED:
        return UnknownCapacityDecision(
            mode=checked_mode,
            eligible_by_unknown_policy=True,
            degraded=True,
            reason_codes=("unknown_capacity_degraded",),
        )
    return UnknownCapacityDecision(
        mode=checked_mode,
        eligible_by_unknown_policy=False,
        degraded=False,
        reason_codes=("unknown_capacity_blocked",),
    )


# ── Reservation rules (D-026) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ReservationRule:
    """One preservation rule over a shared capacity scope.

    The target identity is the exact ``CapacityScopeRef``
    ``(provider, scope_id)`` — never a model name — because models sharing a
    subscription (Luna/Sol on ``openai/codex``, GLM-5.3/GLM-5.3-Flash on
    ``zai/coding_plan``) consume one quota together. The rule carries no
    model ratings and no model affinity.

    Trigger boundary is strict: the reservation triggers iff
    ``remaining_percent < when_remaining_below`` (remaining 20 at threshold
    20 does not trigger; 19 does). When triggered, use is blocked below
    ``minimum_task_level`` and permitted at or above it. A reservation never
    creates capacity: it cannot override explicit exhaustion.
    """

    rule_id: str
    scope: CapacityScopeRef
    resource: str
    kind: str
    when_remaining_below: int
    minimum_task_level: str

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "rule_id",
        "scope",
        "resource",
        "kind",
        "when_remaining_below",
        "minimum_task_level",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.rule_id, "reservation_rule.rule_id")
        _ = _v_instance_of(self.scope, CapacityScopeRef, "reservation_rule.scope")
        _ = _v_enum(self.resource, RESERVATION_RESOURCES, "reservation_rule.resource")
        _ = _v_enum(self.kind, RESERVATION_WINDOW_KINDS, "reservation_rule.kind")
        _ = _v_int(
            self.when_remaining_below,
            "reservation_rule.when_remaining_below",
            lo=1,
            hi=100,
        )
        _ = _v_task_level(
            self.minimum_task_level, "reservation_rule.minimum_task_level"
        )

    @classmethod
    def from_dict(cls, d: object) -> "ReservationRule":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "reservation_rule")
        return cls(
            rule_id=_v_safe_id(dd["rule_id"], "reservation_rule.rule_id"),
            scope=CapacityScopeRef.from_dict(dd["scope"]),
            resource=_v_enum(
                dd["resource"], RESERVATION_RESOURCES, "reservation_rule.resource"
            ),
            kind=_v_enum(
                dd["kind"], RESERVATION_WINDOW_KINDS, "reservation_rule.kind"
            ),
            when_remaining_below=_v_int(
                dd["when_remaining_below"],
                "reservation_rule.when_remaining_below",
                lo=1,
                hi=100,
            ),
            minimum_task_level=_v_task_level(
                dd["minimum_task_level"], "reservation_rule.minimum_task_level"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "scope": self.scope.to_dict(),
            "resource": self.resource,
            "kind": self.kind,
            "when_remaining_below": self.when_remaining_below,
            "minimum_task_level": self.minimum_task_level,
        }


@dataclass(frozen=True)
class ReservationDecision:
    """One reservation rule's evaluation against current capacity evidence.

    ``state="known"`` means the rule's target window was identified with a
    usable percentage pair: ``triggered`` and ``blocked`` are decided and
    ``evidence_remaining_percent`` records the (most restrictive) matching
    remaining value. ``state="unknown"`` means the target window could not
    be identified or lacks usable percentage evidence — the reservation is
    never silently treated as not triggered, and the numeric fields are
    absent rather than defaulted.
    """

    rule_id: str
    state: str
    triggered: bool | None
    blocked: bool | None
    evidence_remaining_percent: int | None
    reason_codes: tuple[str, ...]

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "rule_id",
        "state",
        "reason_codes",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "triggered",
        "blocked",
        "evidence_remaining_percent",
    )

    _KNOWN_CODES: ClassVar[frozenset[str]] = frozenset({
        "reservation_triggered",
        "reservation_blocked",
        "reservation_permitted_by_task_level",
    })
    _UNKNOWN_CODES: ClassVar[frozenset[str]] = frozenset({
        "reservation_scope_snapshot_missing",
        "reservation_scope_snapshot_not_ok",
        "reservation_window_missing",
        "reservation_percentage_unknown",
    })

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.rule_id, "reservation_decision.rule_id")
        _ = _v_enum(self.state, RESERVATION_STATES, "reservation_decision.state")
        codes = _v_reason_codes(
            self.reason_codes, "reservation_decision.reason_codes"
        )
        object.__setattr__(self, "reason_codes", codes)
        if self.state == "unknown":
            if self.triggered is not None or self.blocked is not None or (
                self.evidence_remaining_percent is not None
            ):
                raise SelectionContractValidationError(
                    "reservation_decision: unknown state must not carry a "
                    + "triggered/blocked/percentage value"
                )
            if not codes or not self._UNKNOWN_CODES.issuperset(codes):
                raise SelectionContractValidationError(
                    "reservation_decision: unknown state requires at least "
                    + "one unknown-cause reason code"
                )
            return

        triggered = _v_bool(self.triggered, "reservation_decision.triggered")
        blocked = _v_bool(self.blocked, "reservation_decision.blocked")
        _ = _v_int(
            self.evidence_remaining_percent,
            "reservation_decision.evidence_remaining_percent",
            lo=0,
            hi=100,
        )
        if not self._KNOWN_CODES.issuperset(codes):
            raise SelectionContractValidationError(
                "reservation_decision: known state requires known-cause "
                + "reason codes"
            )
        if not triggered:
            if blocked:
                raise SelectionContractValidationError(
                    "reservation_decision: blocked=True requires a "
                    + "triggered reservation"
                )
            if codes:
                raise SelectionContractValidationError(
                    "reservation_decision: an untriggered reservation "
                    + "carries no reason codes"
                )
            return
        if "reservation_triggered" not in codes:
            raise SelectionContractValidationError(
                "reservation_decision: triggered=True requires the "
                + "'reservation_triggered' reason code"
            )
        if blocked:
            if "reservation_blocked" not in codes:
                raise SelectionContractValidationError(
                    "reservation_decision: blocked=True requires the "
                    + "'reservation_blocked' reason code"
                )
        else:
            if "reservation_blocked" in codes:
                raise SelectionContractValidationError(
                    "reservation_decision: blocked=False must not carry the "
                    + "'reservation_blocked' reason code"
                )
            if "reservation_permitted_by_task_level" not in codes:
                raise SelectionContractValidationError(
                    "reservation_decision: a triggered, permitted "
                    + "reservation requires the "
                    + "'reservation_permitted_by_task_level' reason code"
                )

    @classmethod
    def from_dict(cls, d: object) -> "ReservationDecision":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "reservation_decision")
        codes_raw = dd["reason_codes"]
        if not isinstance(codes_raw, list):
            raise SelectionContractValidationError(
                "reservation_decision.reason_codes: expected list, got "
                + f"{type(codes_raw).__name__}"
            )
        codes = tuple(
            _v_enum(code, POLICY_REASON_CODES, "reservation_decision.reason_codes")
            for code in cast("list[object]", codes_raw)
        )
        triggered: bool | None = None
        if dd.get("triggered") is not None:
            triggered = _v_bool(dd["triggered"], "reservation_decision.triggered")
        blocked: bool | None = None
        if dd.get("blocked") is not None:
            blocked = _v_bool(dd["blocked"], "reservation_decision.blocked")
        evidence: int | None = None
        if dd.get("evidence_remaining_percent") is not None:
            evidence = _v_int(
                dd["evidence_remaining_percent"],
                "reservation_decision.evidence_remaining_percent",
                lo=0,
                hi=100,
            )
        return cls(
            rule_id=_v_safe_id(dd["rule_id"], "reservation_decision.rule_id"),
            state=_v_enum(
                dd["state"], RESERVATION_STATES, "reservation_decision.state"
            ),
            triggered=triggered,
            blocked=blocked,
            evidence_remaining_percent=evidence,
            reason_codes=codes,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "rule_id": self.rule_id,
            "state": self.state,
        }
        if self.triggered is not None:
            out["triggered"] = self.triggered
        if self.blocked is not None:
            out["blocked"] = self.blocked
        if self.evidence_remaining_percent is not None:
            out["evidence_remaining_percent"] = self.evidence_remaining_percent
        out["reason_codes"] = list(self.reason_codes)
        return out


def _reservation_unknown(
    rule_id: str, reasons: tuple[str, ...]
) -> ReservationDecision:
    return ReservationDecision(
        rule_id=rule_id,
        state="unknown",
        triggered=None,
        blocked=None,
        evidence_remaining_percent=None,
        reason_codes=reasons,
    )


def evaluate_reservation(
    rule: ReservationRule,
    snapshots: Sequence[CapacitySnapshot],
    task_level: str,
) -> ReservationDecision:
    """Evaluate one reservation rule against current capacity evidence.

    The rule's target window is identified by its exact
    ``(provider, scope_id)`` scope plus its normalized ``resource`` and
    ``kind`` — never by window identifiers. When several windows match, the
    most restrictive remaining value governs, consistent with the capacity
    aggregation invariant. Triggering uses the strict ``<`` threshold
    boundary; a triggered rule blocks task levels below
    ``minimum_task_level`` and permits levels at or above it. If the
    provider snapshot is missing or not ``ok``, or no window matches, or a
    matching window lacks a percentage pair, the evaluation is explicitly
    unknown.

    This primitive does not create capacity: a permitted reservation on an
    explicitly exhausted scope does not restore eligibility; the scarcity
    layer still blocks it.
    """
    _ = _v_instance_of(rule, ReservationRule, "evaluate_reservation.rule")
    level = _v_task_level(task_level, "evaluate_reservation.task_level")
    snapshot_by_provider: dict[str, CapacitySnapshot] = {}
    for snapshot in snapshots:
        _ = _v_instance_of(snapshot, CapacitySnapshot, "evaluate_reservation.snapshots")
        if snapshot.provider in snapshot_by_provider:
            raise SelectionContractValidationError(
                "evaluate_reservation.snapshots: duplicate provider snapshot "
                + f"for {snapshot.provider!r}; at most one snapshot per "
                + "provider is permitted per evaluation"
            )
        snapshot_by_provider[snapshot.provider] = snapshot

    snapshot = snapshot_by_provider.get(rule.scope.provider)
    if snapshot is None:
        return _reservation_unknown(
            rule.rule_id, ("reservation_scope_snapshot_missing",)
        )
    if snapshot.status != "ok":
        return _reservation_unknown(
            rule.rule_id, ("reservation_scope_snapshot_not_ok",)
        )

    remaining_values: list[int] = []
    for window in snapshot.windows:
        if (
            window.scope_id != rule.scope.scope_id
            or window.resource != rule.resource
            or window.kind != rule.kind
        ):
            continue
        if window.used_percent is None or window.remaining_percent is None:
            return _reservation_unknown(
                rule.rule_id, ("reservation_percentage_unknown",)
            )
        remaining_values.append(window.remaining_percent)
    if not remaining_values:
        return _reservation_unknown(rule.rule_id, ("reservation_window_missing",))

    evidence_remaining = min(remaining_values)
    triggered = evidence_remaining < rule.when_remaining_below
    blocked = (
        triggered
        and TASK_LEVELS.index(level) < TASK_LEVELS.index(rule.minimum_task_level)
    )
    codes: tuple[str, ...] = ()
    if triggered:
        codes = (
            ("reservation_triggered", "reservation_blocked")
            if blocked
            else ("reservation_triggered", "reservation_permitted_by_task_level")
        )
    return ReservationDecision(
        rule_id=rule.rule_id,
        state="known",
        triggered=triggered,
        blocked=blocked,
        evidence_remaining_percent=evidence_remaining,
        reason_codes=codes,
    )


# ── Weekly blackout policy (D-026) ────────────────────────────────────────────


@dataclass(frozen=True)
class AvailabilityTarget:
    """Exact policy target identity: provider, optional model and variant.

    Matching is exact equality with a candidate's ``ModelIdentity``; there
    is no wildcard string syntax. ``variant`` requires ``model``. A
    provider-only target matches every model of that provider.
    """

    provider: str
    model: str | None = None
    variant: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("provider",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("model", "variant")

    def __post_init__(self) -> None:
        _ = _v_provider(self.provider, "availability_target.provider")
        if self.model is not None:
            _ = _v_safe_id(self.model, "availability_target.model")
        if self.variant is not None:
            if self.model is None:
                raise SelectionContractValidationError(
                    "availability_target: variant requires model"
                )
            _ = _v_safe_id(self.variant, "availability_target.variant")

    def matches(self, identity: ModelIdentity) -> bool:
        """Exact-equality match against one candidate identity."""
        _ = _v_instance_of(
            identity, ModelIdentity, "availability_target.matches.identity"
        )
        if identity.provider != self.provider:
            return False
        if self.model is not None and identity.model != self.model:
            return False
        if self.variant is not None and identity.variant != self.variant:
            return False
        return True

    @classmethod
    def from_dict(cls, d: object) -> "AvailabilityTarget":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "availability_target")
        model: str | None = None
        if dd.get("model") is not None:
            model = _v_safe_id(dd["model"], "availability_target.model")
        variant: str | None = None
        if dd.get("variant") is not None:
            variant = _v_safe_id(dd["variant"], "availability_target.variant")
        return cls(
            provider=_v_provider(dd["provider"], "availability_target.provider"),
            model=model,
            variant=variant,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"provider": self.provider}
        if self.model is not None:
            out["model"] = self.model
        if self.variant is not None:
            out["variant"] = self.variant
        return out


@dataclass(frozen=True)
class WeeklyBlackoutRule:
    """One user-configured weekly blackout window (user policy, not telemetry).

    The schedule is entirely user policy: no vendor peak/off-peak schedule is
    hard-coded anywhere. ``timezone`` is an IANA name resolved through the
    standard-library ``zoneinfo``; ``weekdays`` uses the explicit ``mon``
    …``sun`` vocabulary, rejects duplicates and is stored canonically
    ordered. ``start_local``/``end_local`` are strict 24-hour ``HH:MM`` wall
    times with half-open ``[start, end)`` semantics: exactly at start is
    blocked, exactly at end is not. ``start == end`` is invalid — never a
    24-hour blackout. A cross-midnight interval (``start > end``) blocks
    from start on each configured weekday through end on the following day.
    """

    rule_id: str
    target: AvailabilityTarget
    timezone: str
    weekdays: tuple[str, ...]
    start_local: str
    end_local: str
    reason_code: str

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "rule_id",
        "target",
        "timezone",
        "weekdays",
        "start_local",
        "end_local",
        "reason_code",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.rule_id, "weekly_blackout_rule.rule_id")
        _ = _v_instance_of(
            self.target, AvailabilityTarget, "weekly_blackout_rule.target"
        )
        _ = _v_timezone(self.timezone, "weekly_blackout_rule.timezone")
        object.__setattr__(
            self,
            "weekdays",
            _v_weekdays(self.weekdays, "weekly_blackout_rule.weekdays"),
        )

        start = _v_hm(self.start_local, "weekly_blackout_rule.start_local")
        end = _v_hm(self.end_local, "weekly_blackout_rule.end_local")
        if start == end:
            raise SelectionContractValidationError(
                "weekly_blackout_rule: start_local equals end_local; an "
                + "empty interval is invalid and is never a 24-hour blackout"
            )
        _ = _v_safe_id(self.reason_code, "weekly_blackout_rule.reason_code")

    def blocks_at(self, at: datetime) -> bool:
        """Half-open ``[start, end)`` check for one timezone-aware instant.

        The argument is validated here — a naive datetime is rejected with
        the contract error instead of being silently interpreted in the
        host's local timezone — so this public path stays deterministic
        independently of :func:`evaluate_blackouts`.
        """
        checked_at = _v_aware_datetime(at, "weekly_blackout_rule.blocks_at.at")
        return _weekly_window_contains(
            self.timezone, self.weekdays, self.start_local, self.end_local,
            checked_at,
        )

    @classmethod
    def from_dict(cls, d: object) -> "WeeklyBlackoutRule":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "weekly_blackout_rule")
        weekdays_raw = dd["weekdays"]
        if not isinstance(weekdays_raw, list):
            raise SelectionContractValidationError(
                "weekly_blackout_rule.weekdays: expected list, got "
                + f"{type(weekdays_raw).__name__}"
            )
        weekdays = tuple(
            _v_enum(item, frozenset(WEEKDAYS), "weekly_blackout_rule.weekdays")
            for item in cast("list[object]", weekdays_raw)
        )
        return cls(
            rule_id=_v_safe_id(dd["rule_id"], "weekly_blackout_rule.rule_id"),
            target=AvailabilityTarget.from_dict(dd["target"]),
            timezone=_v_timezone(dd["timezone"], "weekly_blackout_rule.timezone"),
            weekdays=weekdays,
            start_local=_v_hm(dd["start_local"], "weekly_blackout_rule.start_local"),
            end_local=_v_hm(dd["end_local"], "weekly_blackout_rule.end_local"),
            reason_code=_v_safe_id(
                dd["reason_code"], "weekly_blackout_rule.reason_code"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "target": self.target.to_dict(),
            "timezone": self.timezone,
            "weekdays": list(self.weekdays),
            "start_local": self.start_local,
            "end_local": self.end_local,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class BlackoutDecision:
    """The blackout evaluation for one candidate at one instant.

    A matching active rule is a hard policy exclusion: ``blocked`` with the
    normalized ``policy_blocked`` reason code plus the matching rule's
    identity and its configured reason code. It never rewrites capacity
    status, percentages, scarcity or capability.
    """

    blocked: bool
    rule_id: str | None = None
    reason_code: str | None = None
    reason_codes: tuple[str, ...] = ()

    _REQUIRED: ClassVar[tuple[str, ...]] = ("blocked",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("rule_id", "reason_code", "reason_codes")

    def __post_init__(self) -> None:
        _ = _v_bool(self.blocked, "blackout_decision.blocked")
        codes = _v_reason_codes(
            self.reason_codes, "blackout_decision.reason_codes"
        )
        object.__setattr__(self, "reason_codes", codes)
        if self.blocked:
            if self.rule_id is None or self.reason_code is None:
                raise SelectionContractValidationError(
                    "blackout_decision: a policy block must name the "
                    + "matching rule and its reason code"
                )
            _ = _v_safe_id(self.rule_id, "blackout_decision.rule_id")
            _ = _v_safe_id(self.reason_code, "blackout_decision.reason_code")
            if codes != _POLICY_BLOCKED_CODES:
                raise SelectionContractValidationError(
                    "blackout_decision: blocked=True requires exactly the "
                    + "'policy_blocked' reason code"
                )
        else:
            if self.rule_id is not None or self.reason_code is not None:
                raise SelectionContractValidationError(
                    "blackout_decision: no block must not name a rule"
                )
            if codes:
                raise SelectionContractValidationError(
                    "blackout_decision: no block carries no reason codes"
                )

    @classmethod
    def from_dict(cls, d: object) -> "BlackoutDecision":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "blackout_decision")
        codes_raw = dd.get("reason_codes")
        codes: tuple[str, ...] = ()
        if codes_raw is not None:
            if not isinstance(codes_raw, list):
                raise SelectionContractValidationError(
                    "blackout_decision.reason_codes: expected list, got "
                    + f"{type(codes_raw).__name__}"
                )
            codes = tuple(
                _v_enum(
                    code, POLICY_REASON_CODES, "blackout_decision.reason_codes"
                )
                for code in cast("list[object]", codes_raw)
            )
        rule_id: str | None = None
        if dd.get("rule_id") is not None:
            rule_id = _v_safe_id(dd["rule_id"], "blackout_decision.rule_id")
        reason_code: str | None = None
        if dd.get("reason_code") is not None:
            reason_code = _v_safe_id(
                dd["reason_code"], "blackout_decision.reason_code"
            )
        return cls(
            blocked=_v_bool(dd["blocked"], "blackout_decision.blocked"),
            rule_id=rule_id,
            reason_code=reason_code,
            reason_codes=codes,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"blocked": self.blocked}
        if self.rule_id is not None:
            out["rule_id"] = self.rule_id
        if self.reason_code is not None:
            out["reason_code"] = self.reason_code
        if self.reason_codes:
            out["reason_codes"] = list(self.reason_codes)
        return out


def evaluate_blackouts(
    rules: Sequence[WeeklyBlackoutRule],
    identity: ModelIdentity,
    at: datetime,
) -> BlackoutDecision:
    """Evaluate weekly blackout rules for one candidate at one aware instant.

    ``at`` must be a timezone-aware ``datetime``; naive local times are
    rejected — the caller supplies the instant and the rule's configured
    zone converts it. Rules are checked in the given order and the first
    matching active rule decides (the ``UserPolicy`` container stores them
    canonically sorted by ``rule_id``, so policy evaluation is
    deterministic). This is policy only: capacity snapshots, percentages and
    scarcity results are never touched.
    """
    _ = _v_instance_of(
        identity, ModelIdentity, "evaluate_blackouts.identity"
    )
    checked_at = _v_aware_datetime(at, "evaluate_blackouts.at")
    for rule in rules:
        _ = _v_instance_of(rule, WeeklyBlackoutRule, "evaluate_blackouts.rules")
        if rule.target.matches(identity) and rule.blocks_at(checked_at):
            return BlackoutDecision(
                blocked=True,
                rule_id=rule.rule_id,
                reason_code=rule.reason_code,
                reason_codes=_POLICY_BLOCKED_CODES,
            )
    return BlackoutDecision(blocked=False)


# ── Weekly happy-hour policy (D-035) ─────────────────────────────────────────


@dataclass(frozen=True)
class WeeklyHappyHourRule:
    """One user-configured weekly quota-preference window (D-035).

    Happy hours are the preference-side counterpart of blackouts: during a
    matching window the targeted candidates are strongly preferred by the
    ranking because consuming their quota is cheap or free (for example a
    vendor usage campaign), so spending other plans' quota instead is what
    conservation wants to avoid. The schedule reuses the blackout semantics
    exactly: an explicit IANA ``timezone``, the duplicate-free canonical
    ``weekdays`` vocabulary, strict 24-hour ``HH:MM`` wall times and
    half-open ``[start, end)`` local intervals with cross-midnight support.
    ``start == end`` is invalid — never a 24-hour window.

    Unlike a blackout, a happy hour may be limited-time: the optional
    inclusive local calendar bounds ``start_date``/``end_date`` (``YYYY-MM-DD``
    in the rule's own zone, ``start_date <= end_date``) restrict the window
    to a campaign period; both absent means a standing recurring window.
    The rule never changes eligibility, capacity telemetry, scarcity or
    capability — it contributes a ranking preference only.
    """

    rule_id: str
    target: AvailabilityTarget
    timezone: str
    weekdays: tuple[str, ...]
    start_local: str
    end_local: str
    reason_code: str
    start_date: str | None = None
    end_date: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "rule_id",
        "target",
        "timezone",
        "weekdays",
        "start_local",
        "end_local",
        "reason_code",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("start_date", "end_date")

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.rule_id, "weekly_happy_hour_rule.rule_id")
        _ = _v_instance_of(
            self.target, AvailabilityTarget, "weekly_happy_hour_rule.target"
        )
        _ = _v_timezone(self.timezone, "weekly_happy_hour_rule.timezone")
        object.__setattr__(
            self,
            "weekdays",
            _v_weekdays(self.weekdays, "weekly_happy_hour_rule.weekdays"),
        )
        start = _v_hm(self.start_local, "weekly_happy_hour_rule.start_local")
        end = _v_hm(self.end_local, "weekly_happy_hour_rule.end_local")
        if start == end:
            raise SelectionContractValidationError(
                "weekly_happy_hour_rule: start_local equals end_local; an "
                + "empty interval is invalid and is never a 24-hour window"
            )
        _ = _v_safe_id(self.reason_code, "weekly_happy_hour_rule.reason_code")
        if self.start_date is not None:
            _ = _v_date(self.start_date, "weekly_happy_hour_rule.start_date")
        if self.end_date is not None:
            _ = _v_date(self.end_date, "weekly_happy_hour_rule.end_date")
        if (
            self.start_date is not None
            and self.end_date is not None
            and date.fromisoformat(self.start_date)
            > date.fromisoformat(self.end_date)
        ):
            raise SelectionContractValidationError(
                "weekly_happy_hour_rule: start_date "
                + f"{self.start_date} is after end_date {self.end_date}"
            )

    def active_at(self, at: datetime) -> bool:
        """Whether the preference window covers one timezone-aware instant.

        The weekly interval uses half-open ``[start, end)`` local semantics
        in the rule's zone; the optional campaign bounds compare the local
        calendar date inclusively on both ends. A naive datetime is rejected
        with the contract error, never interpreted in the host's zone.
        """
        checked_at = _v_aware_datetime(at, "weekly_happy_hour_rule.active_at.at")
        if not _weekly_window_contains(
            self.timezone, self.weekdays, self.start_local, self.end_local,
            checked_at,
        ):
            return False
        if self.start_date is None and self.end_date is None:
            return True
        local_date = checked_at.astimezone(_zone(self.timezone)).date()
        if self.start_date is not None and (
            local_date < date.fromisoformat(self.start_date)
        ):
            return False
        if self.end_date is not None and (
            local_date > date.fromisoformat(self.end_date)
        ):
            return False
        return True

    @classmethod
    def from_dict(cls, d: object) -> "WeeklyHappyHourRule":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "weekly_happy_hour_rule")
        weekdays_raw = dd["weekdays"]
        if not isinstance(weekdays_raw, list):
            raise SelectionContractValidationError(
                "weekly_happy_hour_rule.weekdays: expected list, got "
                + f"{type(weekdays_raw).__name__}"
            )
        weekdays = tuple(
            _v_enum(item, frozenset(WEEKDAYS), "weekly_happy_hour_rule.weekdays")
            for item in cast("list[object]", weekdays_raw)
        )
        start_date: str | None = None
        if dd.get("start_date") is not None:
            start_date = _v_date(dd["start_date"], "weekly_happy_hour_rule.start_date")
        end_date: str | None = None
        if dd.get("end_date") is not None:
            end_date = _v_date(dd["end_date"], "weekly_happy_hour_rule.end_date")
        return cls(
            rule_id=_v_safe_id(dd["rule_id"], "weekly_happy_hour_rule.rule_id"),
            target=AvailabilityTarget.from_dict(dd["target"]),
            timezone=_v_timezone(dd["timezone"], "weekly_happy_hour_rule.timezone"),
            weekdays=weekdays,
            start_local=_v_hm(dd["start_local"], "weekly_happy_hour_rule.start_local"),
            end_local=_v_hm(dd["end_local"], "weekly_happy_hour_rule.end_local"),
            reason_code=_v_safe_id(
                dd["reason_code"], "weekly_happy_hour_rule.reason_code"
            ),
            start_date=start_date,
            end_date=end_date,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "rule_id": self.rule_id,
            "target": self.target.to_dict(),
            "timezone": self.timezone,
            "weekdays": list(self.weekdays),
            "start_local": self.start_local,
            "end_local": self.end_local,
            "reason_code": self.reason_code,
        }
        if self.start_date is not None:
            out["start_date"] = self.start_date
        if self.end_date is not None:
            out["end_date"] = self.end_date
        return out


@dataclass(frozen=True)
class HappyHourDecision:
    """The happy-hour preference evaluation for one candidate at one instant.

    ``preferred=True`` names the first matching active rule and its
    configured reason code; it is a ranking preference only and never an
    eligibility claim. A non-preferred decision names no rule and carries no
    extras. Capacity telemetry, scarcity and capability are never touched.
    """

    preferred: bool
    rule_id: str | None = None
    reason_code: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("preferred",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("rule_id", "reason_code")

    def __post_init__(self) -> None:
        _ = _v_bool(self.preferred, "happy_hour_decision.preferred")
        if self.preferred:
            if self.rule_id is None or self.reason_code is None:
                raise SelectionContractValidationError(
                    "happy_hour_decision: a preference must name the "
                    + "matching rule and its reason code"
                )
            _ = _v_safe_id(self.rule_id, "happy_hour_decision.rule_id")
            _ = _v_safe_id(self.reason_code, "happy_hour_decision.reason_code")
        else:
            if self.rule_id is not None or self.reason_code is not None:
                raise SelectionContractValidationError(
                    "happy_hour_decision: no preference must not name a rule"
                )

    @classmethod
    def from_dict(cls, d: object) -> "HappyHourDecision":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "happy_hour_decision")
        rule_id: str | None = None
        if dd.get("rule_id") is not None:
            rule_id = _v_safe_id(dd["rule_id"], "happy_hour_decision.rule_id")
        reason_code: str | None = None
        if dd.get("reason_code") is not None:
            reason_code = _v_safe_id(
                dd["reason_code"], "happy_hour_decision.reason_code"
            )
        return cls(
            preferred=_v_bool(dd["preferred"], "happy_hour_decision.preferred"),
            rule_id=rule_id,
            reason_code=reason_code,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"preferred": self.preferred}
        if self.rule_id is not None:
            out["rule_id"] = self.rule_id
        if self.reason_code is not None:
            out["reason_code"] = self.reason_code
        return out


def evaluate_happy_hours(
    rules: Sequence[WeeklyHappyHourRule],
    identity: ModelIdentity,
    at: datetime,
) -> HappyHourDecision:
    """Evaluate happy-hour rules for one candidate at one aware instant.

    Rules are checked in the given order and the first matching active rule
    decides (the ``UserPolicy`` container stores them canonically sorted by
    ``rule_id``, so evaluation is deterministic). Pure policy evaluation:
    capacity snapshots, percentages, scarcity and capability are never
    touched, and the decision never changes eligibility.
    """
    _ = _v_instance_of(identity, ModelIdentity, "evaluate_happy_hours.identity")
    checked_at = _v_aware_datetime(at, "evaluate_happy_hours.at")
    for rule in rules:
        _ = _v_instance_of(rule, WeeklyHappyHourRule, "evaluate_happy_hours.rules")
        if rule.target.matches(identity) and rule.active_at(checked_at):
            return HappyHourDecision(
                preferred=True,
                rule_id=rule.rule_id,
                reason_code=rule.reason_code,
            )
    return HappyHourDecision(preferred=False)


# ── Replenishment state and visibility (D-021, D-026) ─────────────────────────


@dataclass(frozen=True)
class ReplenishmentState:
    """The normalized D-021 replenishment observation.

    Minimal safe facts only: no provider free-text, no credit IDs, no
    titles, no descriptions and no account identity. ``kind`` is a safe
    normalized identifier (the evidenced OpenAI concept is
    ``rate_limit_reset``). Timestamps use the canonical UTC
    ``YYYY-MM-DDTHH:MM:SS.sssZ`` form. ``earliest_expiry`` is present only
    when details are known and at least one credit is available; a zero
    count never carries an expiry. ``details_known`` being true does not
    require an expiry — some credits may have no safely usable expiry.

    Replenishment is never current capacity: this state never changes a
    ``ScarcityAssessment``, never restores eligibility and is never consumed
    or redeemed by the broker.
    """

    provider: str
    kind: str
    available_count: int
    details_known: bool
    earliest_expiry: str | None
    retrieved_at: str

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "provider",
        "kind",
        "available_count",
        "details_known",
        "retrieved_at",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("earliest_expiry",)

    def __post_init__(self) -> None:
        _ = _v_provider(self.provider, "replenishment_state.provider")
        _ = _v_safe_id(self.kind, "replenishment_state.kind")
        _ = _v_int(
            self.available_count, "replenishment_state.available_count", lo=0
        )
        _ = _v_bool(self.details_known, "replenishment_state.details_known")
        _ = _v_ts(self.retrieved_at, "replenishment_state.retrieved_at")
        if self.earliest_expiry is not None:
            _ = _v_ts(self.earliest_expiry, "replenishment_state.earliest_expiry")
            if self.available_count == 0:
                raise SelectionContractValidationError(
                    "replenishment_state: a zero available count must not "
                    + "carry earliest_expiry"
                )
            if not self.details_known:
                raise SelectionContractValidationError(
                    "replenishment_state: earliest_expiry requires "
                    + "details_known"
                )

    @classmethod
    def from_dict(cls, d: object) -> "ReplenishmentState":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "replenishment_state")
        expiry: str | None = None
        if dd.get("earliest_expiry") is not None:
            expiry = _v_ts(
                dd["earliest_expiry"], "replenishment_state.earliest_expiry"
            )
        return cls(
            provider=_v_provider(dd["provider"], "replenishment_state.provider"),
            kind=_v_safe_id(dd["kind"], "replenishment_state.kind"),
            available_count=_v_int(
                dd["available_count"],
                "replenishment_state.available_count",
                lo=0,
            ),
            details_known=_v_bool(
                dd["details_known"], "replenishment_state.details_known"
            ),
            earliest_expiry=expiry,
            retrieved_at=_v_ts(dd["retrieved_at"], "replenishment_state.retrieved_at"),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "provider": self.provider,
            "kind": self.kind,
            "available_count": self.available_count,
            "details_known": self.details_known,
        }
        if self.earliest_expiry is not None:
            out["earliest_expiry"] = self.earliest_expiry
        out["retrieved_at"] = self.retrieved_at
        return out


@dataclass(frozen=True)
class ReplenishmentDecision:
    """One candidate's replenishment visibility under the active mode.

    Visibility modes are policy semantics, not actions: ``ignore`` keeps
    replenishment out of policy output entirely; ``advisory`` exposes the
    availability facts without marking capacity recovered; ``recoverable``
    additionally marks a positive available count as ``recoverable`` with
    ``human_action_required`` — the broker never redeems anything and the
    candidate's current eligibility is never restored by this decision.
    Visibility is not availability: a visible decision with
    ``available_count == 0`` carries no reason codes; the zero count itself
    is the normalized explanation.
    """

    mode: str
    visible: bool
    available_count: int | None = None
    details_known: bool | None = None
    recoverable: bool = False
    human_action_required: bool = False
    reason_codes: tuple[str, ...] = ()

    _REQUIRED: ClassVar[tuple[str, ...]] = ("mode", "visible")
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "available_count",
        "details_known",
        "recoverable",
        "human_action_required",
        "reason_codes",
    )

    def __post_init__(self) -> None:
        _ = _v_enum(self.mode, REPLENISHMENT_MODES, "replenishment.mode")
        _ = _v_bool(self.visible, "replenishment.visible")
        codes = _v_reason_codes(
            self.reason_codes, "replenishment.reason_codes"
        )
        object.__setattr__(self, "reason_codes", codes)
        if not self.visible:
            if self.available_count is not None or self.details_known is not None:
                raise SelectionContractValidationError(
                    "replenishment: an invisible decision carries no "
                    + "availability facts"
                )
            if self.recoverable or self.human_action_required:
                raise SelectionContractValidationError(
                    "replenishment: an invisible decision is never "
                    + "recoverable"
                )
            if codes:
                raise SelectionContractValidationError(
                    "replenishment: an invisible decision carries no "
                    + "reason codes"
                )
            return

        available_count = _v_int(
            self.available_count, "replenishment.available_count", lo=0
        )
        _ = _v_bool(self.details_known, "replenishment.details_known")
        _ = _v_bool(self.recoverable, "replenishment.recoverable")
        _ = _v_bool(
            self.human_action_required, "replenishment.human_action_required"
        )
        if self.recoverable != self.human_action_required:
            raise SelectionContractValidationError(
                "replenishment: recoverable and human_action_required "
                + "always agree"
            )
        if self.recoverable:
            if self.mode != REPLENISHMENT_MODE_RECOVERABLE or (
                available_count <= 0
            ):
                raise SelectionContractValidationError(
                    "replenishment: recoverable requires the recoverable "
                    + "mode and a positive available count"
                )
            if codes != _RECOVERABLE_CODES:
                raise SelectionContractValidationError(
                    "replenishment: recoverable requires exactly the "
                    + "recoverable reason codes"
                )
        elif available_count > 0:
            if codes != _AVAILABLE_CODES:
                raise SelectionContractValidationError(
                    "replenishment: visible available replenishment "
                    + "requires exactly the 'replenishment_available' "
                    + "reason code"
                )
        elif codes:
            # Visibility of the observation is not availability of a reset:
            # a zero count is its own explanation, no invented code needed.
            raise SelectionContractValidationError(
                "replenishment: a visible zero available count carries no "
                + "reason codes"
            )

    @classmethod
    def from_dict(cls, d: object) -> "ReplenishmentDecision":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "replenishment")
        codes_raw = dd.get("reason_codes")
        codes: tuple[str, ...] = ()
        if codes_raw is not None:
            if not isinstance(codes_raw, list):
                raise SelectionContractValidationError(
                    "replenishment.reason_codes: expected list, got "
                    + f"{type(codes_raw).__name__}"
                )
            codes = tuple(
                _v_enum(code, POLICY_REASON_CODES, "replenishment.reason_codes")
                for code in cast("list[object]", codes_raw)
            )
        available_count: int | None = None
        if dd.get("available_count") is not None:
            available_count = _v_int(
                dd["available_count"], "replenishment.available_count", lo=0
            )
        details_known: bool | None = None
        if dd.get("details_known") is not None:
            details_known = _v_bool(
                dd["details_known"], "replenishment.details_known"
            )
        recoverable = (
            _v_bool(dd["recoverable"], "replenishment.recoverable")
            if dd.get("recoverable") is not None
            else False
        )
        human = (
            _v_bool(dd["human_action_required"], "replenishment.human_action_required")
            if dd.get("human_action_required") is not None
            else False
        )
        return cls(
            mode=_v_enum(dd["mode"], REPLENISHMENT_MODES, "replenishment.mode"),
            visible=_v_bool(dd["visible"], "replenishment.visible"),
            available_count=available_count,
            details_known=details_known,
            recoverable=recoverable,
            human_action_required=human,
            reason_codes=codes,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "mode": self.mode,
            "visible": self.visible,
        }
        if self.visible:
            out["available_count"] = self.available_count
            out["details_known"] = self.details_known
            out["recoverable"] = self.recoverable
            out["human_action_required"] = self.human_action_required
        if self.reason_codes:
            out["reason_codes"] = list(self.reason_codes)
        return out


def apply_replenishment_mode(
    mode: str,
    state: ReplenishmentState | None,
) -> ReplenishmentDecision:
    """Apply one visibility mode to an optional replenishment observation.

    Pure and side-effect free: the decision never changes a scarcity
    assessment, never marks current capacity eligible and never performs or
    schedules a reset. A missing observation (``state=None``) is invisible
    under every mode.
    """
    checked_mode = _v_enum(mode, REPLENISHMENT_MODES, "replenishment.mode")
    if state is not None:
        _ = _v_instance_of(
            state, ReplenishmentState, "apply_replenishment_mode.state"
        )
    if checked_mode == REPLENISHMENT_MODE_IGNORE or state is None:
        return ReplenishmentDecision(mode=checked_mode, visible=False)
    if checked_mode == REPLENISHMENT_MODE_ADVISORY:
        return ReplenishmentDecision(
            mode=checked_mode,
            visible=True,
            available_count=state.available_count,
            details_known=state.details_known,
            reason_codes=(
                _AVAILABLE_CODES if state.available_count > 0 else ()
            ),
        )
    # recoverable
    if state.available_count > 0:
        return ReplenishmentDecision(
            mode=checked_mode,
            visible=True,
            available_count=state.available_count,
            details_known=state.details_known,
            recoverable=True,
            human_action_required=True,
            reason_codes=_RECOVERABLE_CODES,
        )
    return ReplenishmentDecision(
        mode=checked_mode,
        visible=True,
        available_count=state.available_count,
        details_known=state.details_known,
    )


# ── UserPolicy container (D-026) ──────────────────────────────────────────────


@dataclass(frozen=True)
class UserPolicy:
    """The compact pure user-policy container.

    ``policy_version >= 1`` identifies the policy generation. Rule IDs must
    be unique across the whole policy (reservations, blackouts and happy
    hours share one namespace) and all rule tuples are stored canonically
    sorted by ``rule_id``, so equality and serialized output are
    deterministic and independent of construction order. This is the type a
    later CLI/application populates; M2d requires no personal policy file.

    ``happy_hours`` is additive (D-035): serialized documents without the
    key load unchanged with an empty tuple, and an empty tuple is never
    serialized, so pre-D-035 documents round-trip byte-identically.
    """

    policy_version: int
    unknown_capacity_mode: str
    replenishment_mode: str
    reservations: tuple[ReservationRule, ...]
    blackouts: tuple[WeeklyBlackoutRule, ...]
    happy_hours: tuple[WeeklyHappyHourRule, ...] = ()

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "policy_version",
        "unknown_capacity_mode",
        "replenishment_mode",
        "reservations",
        "blackouts",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("happy_hours",)

    def __post_init__(self) -> None:
        _ = _v_int(self.policy_version, "user_policy.policy_version", lo=1)
        _ = _v_enum(
            self.unknown_capacity_mode,
            UNKNOWN_CAPACITY_MODES,
            "user_policy.unknown_capacity_mode",
        )
        _ = _v_enum(
            self.replenishment_mode,
            REPLENISHMENT_MODES,
            "user_policy.replenishment_mode",
        )
        _ = _v_tuple_of(
            self.reservations, ReservationRule, "user_policy.reservations"
        )
        _ = _v_tuple_of(
            self.blackouts, WeeklyBlackoutRule, "user_policy.blackouts"
        )
        _ = _v_tuple_of(
            self.happy_hours, WeeklyHappyHourRule, "user_policy.happy_hours"
        )
        seen: set[str] = set()
        for rule in self.reservations:
            if rule.rule_id in seen:
                raise SelectionContractValidationError(
                    "user_policy: duplicate rule id "
                    + f"{rule.rule_id!r} across the policy"
                )
            seen.add(rule.rule_id)
        for rule in self.blackouts:
            if rule.rule_id in seen:
                raise SelectionContractValidationError(
                    "user_policy: duplicate rule id "
                    + f"{rule.rule_id!r} across the policy"
                )
            seen.add(rule.rule_id)
        for rule in self.happy_hours:
            if rule.rule_id in seen:
                raise SelectionContractValidationError(
                    "user_policy: duplicate rule id "
                    + f"{rule.rule_id!r} across the policy"
                )
            seen.add(rule.rule_id)
        # Canonical stored form: rules sorted by stable rule_id make equality
        # and serialized output deterministic and independent of insertion
        # order.
        object.__setattr__(
            self,
            "reservations",
            tuple(sorted(self.reservations, key=lambda r: r.rule_id)),
        )
        object.__setattr__(
            self,
            "blackouts",
            tuple(sorted(self.blackouts, key=lambda r: r.rule_id)),
        )
        object.__setattr__(
            self,
            "happy_hours",
            tuple(sorted(self.happy_hours, key=lambda r: r.rule_id)),
        )

    def apply_unknown_capacity(
        self, assessment: ScarcityAssessment
    ) -> UnknownCapacityDecision:
        """Apply this policy's unknown-capacity mode to one assessment."""
        return apply_unknown_capacity_mode(
            self.unknown_capacity_mode, assessment
        )

    def evaluate_reservations(
        self, snapshots: Sequence[CapacitySnapshot], task_level: str
    ) -> tuple[ReservationDecision, ...]:
        """Evaluate every reservation rule in canonical (rule_id) order."""
        return tuple(
            evaluate_reservation(rule, snapshots, task_level)
            for rule in self.reservations
        )

    def evaluate_blackouts(
        self, identity: ModelIdentity, at: datetime
    ) -> BlackoutDecision:
        """Evaluate this policy's blackout rules for one candidate."""
        return evaluate_blackouts(self.blackouts, identity, at)

    def evaluate_happy_hours(
        self, identity: ModelIdentity, at: datetime
    ) -> HappyHourDecision:
        """Evaluate this policy's happy-hour rules for one candidate."""
        return evaluate_happy_hours(self.happy_hours, identity, at)

    @classmethod
    def from_dict(cls, d: object) -> "UserPolicy":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "user_policy")
        reservations_raw = dd["reservations"]
        if not isinstance(reservations_raw, list):
            raise SelectionContractValidationError(
                "user_policy.reservations: expected list, got "
                + f"{type(reservations_raw).__name__}"
            )
        blackouts_raw = dd["blackouts"]
        if not isinstance(blackouts_raw, list):
            raise SelectionContractValidationError(
                "user_policy.blackouts: expected list, got "
                + f"{type(blackouts_raw).__name__}"
            )
        happy_hours_raw = dd.get("happy_hours")
        if happy_hours_raw is not None and not isinstance(happy_hours_raw, list):
            raise SelectionContractValidationError(
                "user_policy.happy_hours: expected list, got "
                + f"{type(happy_hours_raw).__name__}"
            )
        return cls(
            policy_version=_v_int(
                dd["policy_version"], "user_policy.policy_version", lo=1
            ),
            unknown_capacity_mode=_v_enum(
                dd["unknown_capacity_mode"],
                UNKNOWN_CAPACITY_MODES,
                "user_policy.unknown_capacity_mode",
            ),
            replenishment_mode=_v_enum(
                dd["replenishment_mode"],
                REPLENISHMENT_MODES,
                "user_policy.replenishment_mode",
            ),
            reservations=tuple(
                ReservationRule.from_dict(x)
                for x in cast("list[object]", reservations_raw)
            ),
            blackouts=tuple(
                WeeklyBlackoutRule.from_dict(x)
                for x in cast("list[object]", blackouts_raw)
            ),
            happy_hours=tuple(
                WeeklyHappyHourRule.from_dict(x)
                for x in cast("list[object]", happy_hours_raw or [])
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "policy_version": self.policy_version,
            "unknown_capacity_mode": self.unknown_capacity_mode,
            "replenishment_mode": self.replenishment_mode,
            "reservations": [
                rule.to_dict()
                for rule in sorted(self.reservations, key=lambda r: r.rule_id)
            ],
            "blackouts": [
                rule.to_dict()
                for rule in sorted(self.blackouts, key=lambda r: r.rule_id)
            ],
        }
        # Additive D-035 member: absent from serialized output when empty so
        # pre-D-035 documents round-trip byte-identically.
        if self.happy_hours:
            out["happy_hours"] = [
                rule.to_dict()
                for rule in sorted(self.happy_hours, key=lambda r: r.rule_id)
            ]
        return out
