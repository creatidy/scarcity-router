"""Route-decision contract for authorized executable targets (M02, D-042).

Pure, provider-independent, standard-library only. Issue #87 (M02) under the
A0 execution-gateway architecture (D-040 through D-045). No filesystem,
network, environment, subprocess or clock access: callers supply the model
catalog, the profile catalog, the M01 registry snapshot, the capacity v3
snapshots, the typed authorization/profile/request layers, the typed
compatibility cells, the D-039 eligibility reports and one timezone-aware
evaluation instant, and every function is deterministic over its arguments.

This module layers executable-target selection ON TOP of the SINGLE scoring
system of the existing balanced selector — never a second selector. It works
in three steps:

1. **Requirement binding (precedence, D-042).** The frozen authorization
   precedence is
   ``administrator constraints > client authorization > request
   requirements > configured routing profile > explicitly selected
   target/model > optimization preferences``. The administrator and client
   layers intersect into one effective authorization (each layer may only
   narrow; client request content never writes any authorization field).
   The configured routing profile resolves through the existing
   :class:`~scarcity_router.selection_types.TaskProfileCatalog` (aliases
   are bindings, not a second scoring system), and the request's structural
   requirements (tools present, structured output, streaming, reasoning
   controls, context/output sizes) merge into it monotonically through
   :func:`scarcity_router.selector.tighten_requirement` — a request can
   tighten the profile requirement but never loosen it. Explicit
   model/variant/target selections are pins: they narrow the candidate set
   and are honored or explicitly failed, never silently replaced. The
   optimization-preference layer remains exactly the caller's
   :class:`~scarcity_router.selector.SelectorPolicy` (preference order,
   short-window floor, unknown-capacity mode, reservations, schedules).

2. **Model selection.** :func:`scarcity_router.selector.select_model` runs
   UNMODIFIED over the narrowed catalog view with the merged requirement,
   the caller's capacity snapshots, replenishment states, D-039 eligibility
   reports and selector policy, preserving the frozen D-027/D-032/D-037
   ranking semantics exactly (identical inputs and evaluation time produce
   identical decisions; quota state never raises capability ratings). The
   narrowed view keeps the catalog's version provenance; identities removed
   by target narrowing are reported as ``unroutable_identities``.

3. **Target binding.** Registry resources bind to catalog identities by
   exact ``(provider, model)`` plus the variant rule (a resource without a
   variant qualifier binds every calibrated variant of the model; a
   variant-qualified resource binds only the exact variant). Only
   identities with at least one authorized, available and compatible
   resource are routable. Every registered resource that fails a gate gets
   a typed exclusion record, with the frozen stage order
   ``binding -> authorization -> availability -> compatibility`` (the
   first failing stage wins and later stages never run merely to populate
   fields; all of that stage's reason codes are reported).

Typed input contracts (all construction-validated, deterministic
``from_dict``/``to_dict``, exact serialized shapes):

- :class:`AdministratorConstraints` and :class:`ClientAuthorization`: the
  allowed providers/channels/entitlements, blocked resource ids and the
  spending limit. Administrator-configured identities, profiles and limits
  are never sourced from client request content.
- :class:`SpendingLimit`: a per-Mtoken micro-USD ceiling. It gates only
  entitlements with a marginal monetary spend (``payg_metered``,
  ``prepaid_credits``); subscription, promotional and local/ungated
  surfaces have no marginal spend. A metered surface without a cost record
  is ``spend_limit_unverifiable`` (fail closed), never free.
- :class:`ClientRoutingProfile`: the administrator-configured binding of
  this client/request to an existing profile id, with an optional
  provider narrowing. The profile requirement is the monotone base; the
  request may only tighten it.
- :class:`RequestBinding`: the request-derived structural requirements and
  the explicit pins (profile alias, pinned executable target, explicit
  model/variant). Pins are honored or explicitly failed.
- :class:`CompatibilityCell`: the typed representation of one OpenAI
  compatibility-matrix cell (D-043) keyed by
  ``(channel, provider, model, variant, feature)`` with a frozen
  ``PASS``/``PARTIAL``/``UNSUPPORTED``/``UNKNOWN`` value, the adapter and
  its version, and a dated evidence reference. ``UNKNOWN``, ``UNSUPPORTED``
  and a missing cell fail closed. The matrix is the routing core's feature
  authority; the numeric context ceiling comes from the M01 registration's
  ``ExecutionCapabilities.context_limit_tokens`` (fail closed when
  unknown), because that is where a number — not a support fact — lives.
  The M01 boolean capability facts are not independently gating here: the
  evidenced matrix subsumes them.

Output contracts (serialize-only ``to_dict``, mirroring
:class:`~scarcity_router.selector.SelectionDecision` discipline):

- :class:`RouteTarget`: one executable target separating the five D-042
  dimensions — physical model/variant (:class:`ModelIdentity`), execution
  channel/surface and the executable-target reference (``resource_id``),
  entitlement, confirmed quota pools, and the client routing profile —
  plus the promotion provenance that contributed a target-level
  preference.
- :class:`RouteDecision`: the decision with a deterministic ``decision_id``
  (SHA-256 over the canonical serialized decision content, so identical
  inputs and evaluation time produce the identical identifier), the
  embedded model-level :class:`~scarcity_router.selector.SelectionDecision`
  as provenance, target alternatives, target exclusions, unroutable
  identities and expired-promotion provenance. No raw provider payloads,
  credentials or account identifiers are representable.
- :class:`AdmissionDecision`: the recommendation-to-execution binding
  (D-042): :func:`admit_pinned_target` evaluates exactly one pinned target
  through the gates only — authorization, limits, availability,
  compatibility — and never re-runs competitive ranking. A recommendation
  is not a reservation: admission re-checks current inputs and may reject
  explicitly.

Promotions are observations, never proof that an execution qualifies: an
active, scope-matching promotion contributes a target-level routing
preference (ordered ahead within the same model identity, like the D-035
happy-hour preference group) and an expired one never contributes — it is
reported under ``expired_promotions`` so the "why did the preference
disappear?" question answers itself from the decision alone.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar, TypeVar, cast

from .capacity import CapacitySnapshot
from .eligibility import ExecutionEligibility
from .errors import RouteContractValidationError
from .policy import ReplenishmentState
from .resource_state import (
    ENTITLEMENT_CLASSES,
    EXECUTION_CHANNELS,
    FRESHNESS_STATES,
    RESOURCE_HEALTH_STATUSES,
    PromotionObservation,
    RegistrySnapshot,
    ResourceIdentity,
    ResourceRegistryEntry,
)
from .selector import (
    SelectionDecision,
    SelectorPolicy,
    canonical_instant,
    select_model,
    tighten_requirement,
)
from .selection_types import (
    CapabilityMinima,
    EvidenceRef,
    HardConstraints,
    ModelCatalog,
    ModelIdentity,
    ModelRef,
    TaskProfileCatalog,
    TaskRequirement,
)

# ── Frozen value sets ─────────────────────────────────────────────────────────

ROUTE_DECISION_SCHEMA_VERSION = 1

# Route-level decision status.
ROUTE_STATUS_SELECTED = "selected"
ROUTE_STATUS_NO_SOLUTION = "no_solution"
ROUTE_STATUSES: frozenset[str] = frozenset({
    ROUTE_STATUS_SELECTED,
    ROUTE_STATUS_NO_SOLUTION,
})

# Target-level gate stages; the tuple order is the frozen evaluation order
# (first failing stage wins). A resource with no calibrated catalog binding
# is furthest from runnable; compatibility is the last gate before a target
# qualifies.
ROUTE_EXCLUSION_STAGES: tuple[str, ...] = (
    "binding",
    "authorization",
    "availability",
    "compatibility",
)

# Closed target-exclusion reason vocabulary, grouped by stage. Dimension and
# backend details never enter reason-code strings; structured detail fields
# carry them.
TARGET_EXCLUSION_REASON_CODES: frozenset[str] = frozenset({
    # binding
    "capability_unassessed",
    # authorization
    "unauthorized_provider",
    "unauthorized_channel",
    "unauthorized_entitlement",
    "resource_blocked",
    "spend_limit_exceeded",
    "spend_limit_unverifiable",
    # availability
    "resource_stale",
    "resource_never_observed",
    "resource_unhealthy",
    "execution_ineligible",
    # compatibility
    "compatibility_unsupported",
    "compatibility_unknown",
    "context_limit_unknown",
    "context_limit_insufficient",
})

_STAGE_REASONS: dict[str, frozenset[str]] = {
    "binding": frozenset({"capability_unassessed"}),
    "authorization": frozenset({
        "unauthorized_provider",
        "unauthorized_channel",
        "unauthorized_entitlement",
        "resource_blocked",
        "spend_limit_exceeded",
        "spend_limit_unverifiable",
    }),
    "availability": frozenset({
        "resource_stale",
        "resource_never_observed",
        "resource_unhealthy",
        "execution_ineligible",
    }),
    "compatibility": frozenset({
        "compatibility_unsupported",
        "compatibility_unknown",
        "context_limit_unknown",
        "context_limit_insufficient",
    }),
}

# Route-level decision reason codes. Model-level selection reasons live in
# the embedded ``SelectionDecision``; these describe the route layer only.
ROUTE_REASON_CODES: frozenset[str] = frozenset({
    "route_selected",
    "no_eligible_target",
    "pinned_request_failed",
    "pin_target_not_found",
    "pin_model_not_found",
})

# Admission-only decision reason codes (recommendation-to-execution binding).
ADMISSION_REASON_CODES: frozenset[str] = frozenset({
    "admission_approved",
    "admission_rejected",
    "pin_target_not_found",
})

# Entitlement classes with a marginal monetary spend: the only ones a
# spending limit gates. Subscription-included, promotional and local/ungated
# surfaces have no marginal per-token monetary cost; ``unknown`` fails
# closed when a limit is configured.
METERED_ENTITLEMENTS: frozenset[str] = frozenset({
    "payg_metered",
    "prepaid_credits",
})

# D-043 OpenAI compatibility-matrix feature vocabulary (safe-id renderings
# of the frozen dimension list) and cell values.
COMPAT_FEATURES: tuple[str, ...] = (
    "roles_history",
    "streaming",
    "tool_calls",
    "tool_results",
    "structured_output",
    "reasoning_controls",
    "context_limits",
    "error_semantics",
    "usage_reporting",
    "cancellation",
)

COMPAT_CELL_VALUES: frozenset[str] = frozenset({
    "PASS",
    "PARTIAL",
    "UNSUPPORTED",
    "UNKNOWN",
})

# The request feature -> compatibility-matrix feature mapping (fixed order).
_REQUEST_FEATURE_MAP: tuple[tuple[Callable[[RequestBinding], bool], str], ...] = (
    (lambda request: request.requires_tool_calls, "tool_calls"),
    (lambda request: request.requires_structured_output, "structured_output"),
    (lambda request: request.requires_streaming, "streaming"),
    (lambda request: request.requires_reasoning_controls, "reasoning_controls"),
)

# ── Validators (single source of truth for the route-contract rules) ──────────

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

_CANONICAL_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

_T = TypeVar("_T")


def _v_str(value: object, fld: str) -> str:
    if not isinstance(value, str):
        raise RouteContractValidationError(
            f"{fld}: expected str, got {type(value).__name__}"
        )
    return value


def _v_safe_id(value: object, fld: str) -> str:
    s = _v_str(value, fld)
    if not _SAFE_ID_RE.match(s):
        raise RouteContractValidationError(
            f"{fld}: unsafe identifier {s!r}; "
            + "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
        )
    return s


def _v_enum(value: object, allowed: frozenset[str], fld: str) -> str:
    s = _v_str(value, fld)
    if s not in allowed:
        raise RouteContractValidationError(
            f"{fld}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def _v_bool(value: object, fld: str) -> bool:
    if not isinstance(value, bool):
        raise RouteContractValidationError(
            f"{fld}: expected bool, got {type(value).__name__} ({value!r})"
        )
    return value


def _v_int(value: object, fld: str, *, lo: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteContractValidationError(
            f"{fld}: expected int, got {type(value).__name__} ({value!r})"
        )
    if lo is not None and value < lo:
        raise RouteContractValidationError(f"{fld}: value {value} < {lo}")
    return value


def _v_opt_int(value: object | None, fld: str, *, lo: int | None = None) -> int | None:
    return None if value is None else _v_int(value, fld, lo=lo)


def _v_text(value: object, fld: str, *, max_len: int) -> str:
    """Non-empty bounded text without control characters or padding."""
    s = _v_str(value, fld)
    if not s:
        raise RouteContractValidationError(f"{fld}: must be non-empty")
    if len(s) > max_len:
        raise RouteContractValidationError(f"{fld}: exceeds maximum length {max_len}")
    if any(unicodedata.category(ch) == "Cc" for ch in s):
        raise RouteContractValidationError(f"{fld}: control characters are not allowed")
    if s != s.strip():
        raise RouteContractValidationError(
            f"{fld}: leading/trailing whitespace is not allowed"
        )
    return s


def _v_aware_datetime(value: object, fld: str) -> datetime:
    """A timezone-aware ``datetime`` instant; naive local times are invalid."""
    if not isinstance(value, datetime):
        raise RouteContractValidationError(
            f"{fld}: expected datetime, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise RouteContractValidationError(
            f"{fld}: expected a timezone-aware datetime, got a naive datetime"
        )
    return value


def _v_instance_of(value: object, cls: type[_T], label: str) -> _T:
    if not isinstance(value, cls):
        raise RouteContractValidationError(
            f"{label}: expected a {cls.__name__}, got {type(value).__name__}"
        )
    return value


def _v_tuple_of(value: object, item_type: type[_T], label: str) -> tuple[_T, ...]:
    if not isinstance(value, tuple):
        raise RouteContractValidationError(
            f"{label}: expected tuple, got {type(value).__name__}"
        )
    for item in cast("tuple[object, ...]", value):
        _ = _v_instance_of(item, item_type, label)
    return cast("tuple[_T, ...]", value)


def _v_sorted_safe_ids(
    value: object,
    fld: str,
    *,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """A deterministically sorted, duplicate-free tuple of safe ids."""
    if not isinstance(value, tuple):
        raise RouteContractValidationError(
            f"{fld}: expected tuple, got {type(value).__name__}"
        )
    ids = tuple(_v_safe_id(item, fld) for item in cast("tuple[object, ...]", value))
    if len(set(ids)) != len(ids):
        raise RouteContractValidationError(f"{fld}: duplicate values are not permitted")
    if list(ids) != sorted(ids):
        raise RouteContractValidationError(f"{fld}: must be deterministically sorted")
    if allowed is not None:
        for item in ids:
            if item not in allowed:
                raise RouteContractValidationError(
                    f"{fld}: value {item!r} not in allowed set {sorted(allowed)}"
                )
    return ids


def _as_str_object_mapping(value: object) -> Mapping[str, object] | None:
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
        raise RouteContractValidationError(
            f"{label}: expected a dict-like mapping, got {type(obj).__name__}"
        )
    allowed = frozenset(required) | frozenset(optional)
    extra = set(m.keys()) - allowed
    if extra:
        raise RouteContractValidationError(f"{label}: unknown keys {sorted(extra)}")
    missing = [key for key in required if key not in m]
    if missing:
        raise RouteContractValidationError(
            f"{label}: missing required keys {sorted(missing)}"
        )
    return m


def _optional_present(d: Mapping[str, object], key: str) -> bool:
    """True when an optional serialized key carries a non-null value."""
    return d.get(key) is not None


def _id_list_field(
    value: object,
    fld: str,
    *,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Convert one serialized safe-id list into the stored canonical tuple."""
    if not isinstance(value, list):
        raise RouteContractValidationError(
            f"{fld}: expected list, got {type(value).__name__}"
        )
    return _v_sorted_safe_ids(tuple(cast("list[str]", value)), fld, allowed=allowed)


def _parse_canonical_ts(value: str, fld: str) -> datetime:
    """Parse one canonical UTC millisecond timestamp into an aware datetime."""
    if not _CANONICAL_TS_RE.match(value):
        raise RouteContractValidationError(
            f"{fld}: non-canonical timestamp {value!r}; "
            + "expected YYYY-MM-DDTHH:MM:SS.sssZ"
        )
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(timezone.utc)


def _identity_key(identity: ModelIdentity) -> tuple[str, str, str]:
    return (identity.provider, identity.model, identity.variant)


# ── Authorization layer inputs ────────────────────────────────────────────────


@dataclass(frozen=True)
class SpendingLimit:
    """One spending ceiling in fixed-point micro-USD per million tokens.

    The routing core compares the resource's configured ``ResourceCost``
    prices against this ceiling; it never estimates a request's token
    count. A limit of ``0`` permits only surfaces with no known positive
    price (or no marginal monetary spend at all).
    """

    micro_usd_per_mtoken_max: int

    _REQUIRED: ClassVar[tuple[str, ...]] = ("micro_usd_per_mtoken_max",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_int(
            self.micro_usd_per_mtoken_max,
            "spending_limit.micro_usd_per_mtoken_max",
            lo=0,
        )

    @classmethod
    def from_dict(cls, d: object) -> "SpendingLimit":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "spending_limit")
        return cls(
            micro_usd_per_mtoken_max=_v_int(
                dd["micro_usd_per_mtoken_max"],
                "spending_limit.micro_usd_per_mtoken_max",
                lo=0,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {"micro_usd_per_mtoken_max": self.micro_usd_per_mtoken_max}


@dataclass(frozen=True)
class AdministratorConstraints:
    """The administrator's routing constraints (strongest D-042 layer).

    Every ``allowed_*`` tuple narrows the permitted space; ``None`` means
    that dimension is unrestricted at this layer. ``blocked_resource_ids``
    refuses named registry resources outright. ``spend_limit`` caps the
    marginal monetary spend per request. These constraints are
    administrator configuration: nothing in a client request can write
    them, and a client layer can only narrow them further.
    """

    allowed_providers: tuple[str, ...] | None = None
    allowed_channels: tuple[str, ...] | None = None
    allowed_entitlements: tuple[str, ...] | None = None
    blocked_resource_ids: tuple[str, ...] = ()
    spend_limit: SpendingLimit | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "allowed_providers",
        "allowed_channels",
        "allowed_entitlements",
        "blocked_resource_ids",
        "spend_limit",
    )

    def __post_init__(self) -> None:
        if self.allowed_providers is not None:
            _ = _v_sorted_safe_ids(
                self.allowed_providers, "administrator_constraints.allowed_providers"
            )
        if self.allowed_channels is not None:
            _ = _v_sorted_safe_ids(
                self.allowed_channels,
                "administrator_constraints.allowed_channels",
                allowed=EXECUTION_CHANNELS,
            )
        if self.allowed_entitlements is not None:
            _ = _v_sorted_safe_ids(
                self.allowed_entitlements,
                "administrator_constraints.allowed_entitlements",
                allowed=ENTITLEMENT_CLASSES,
            )
        _ = _v_sorted_safe_ids(
            self.blocked_resource_ids, "administrator_constraints.blocked_resource_ids"
        )
        if self.spend_limit is not None:
            _ = _v_instance_of(
                self.spend_limit,
                SpendingLimit,
                "administrator_constraints.spend_limit",
            )

    @classmethod
    def from_dict(cls, d: object) -> "AdministratorConstraints":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "administrator_constraints")
        return cls(
            allowed_providers=(
                None
                if not _optional_present(dd, "allowed_providers")
                else _id_list_field(
                    dd["allowed_providers"],
                    "administrator_constraints.allowed_providers",
                )
            ),
            allowed_channels=(
                None
                if not _optional_present(dd, "allowed_channels")
                else _id_list_field(
                    dd["allowed_channels"],
                    "administrator_constraints.allowed_channels",
                    allowed=EXECUTION_CHANNELS,
                )
            ),
            allowed_entitlements=(
                None
                if not _optional_present(dd, "allowed_entitlements")
                else _id_list_field(
                    dd["allowed_entitlements"],
                    "administrator_constraints.allowed_entitlements",
                    allowed=ENTITLEMENT_CLASSES,
                )
            ),
            blocked_resource_ids=(
                ()
                if not _optional_present(dd, "blocked_resource_ids")
                else _id_list_field(
                    dd["blocked_resource_ids"],
                    "administrator_constraints.blocked_resource_ids",
                )
            ),
            spend_limit=(
                None
                if not _optional_present(dd, "spend_limit")
                else SpendingLimit.from_dict(dd["spend_limit"])
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.allowed_providers is not None:
            out["allowed_providers"] = list(self.allowed_providers)
        if self.allowed_channels is not None:
            out["allowed_channels"] = list(self.allowed_channels)
        if self.allowed_entitlements is not None:
            out["allowed_entitlements"] = list(self.allowed_entitlements)
        if self.blocked_resource_ids:
            out["blocked_resource_ids"] = list(self.blocked_resource_ids)
        if self.spend_limit is not None:
            out["spend_limit"] = self.spend_limit.to_dict()
        return out


@dataclass(frozen=True)
class ClientAuthorization:
    """One client identity's administrator-issued authorization grant.

    Structurally identical to :class:`AdministratorConstraints` but a
    strictly weaker layer (D-042): the effective authorization is the
    intersection of both layers, so a client grant can only narrow what the
    administrator allows and can never expand authorization, provider
    access or spending limits. Issued by the administrator per client
    identity; never sourced from client request content.
    """

    allowed_providers: tuple[str, ...] | None = None
    allowed_channels: tuple[str, ...] | None = None
    allowed_entitlements: tuple[str, ...] | None = None
    blocked_resource_ids: tuple[str, ...] = ()
    spend_limit: SpendingLimit | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "allowed_providers",
        "allowed_channels",
        "allowed_entitlements",
        "blocked_resource_ids",
        "spend_limit",
    )

    def __post_init__(self) -> None:
        if self.allowed_providers is not None:
            _ = _v_sorted_safe_ids(
                self.allowed_providers, "client_authorization.allowed_providers"
            )
        if self.allowed_channels is not None:
            _ = _v_sorted_safe_ids(
                self.allowed_channels,
                "client_authorization.allowed_channels",
                allowed=EXECUTION_CHANNELS,
            )
        if self.allowed_entitlements is not None:
            _ = _v_sorted_safe_ids(
                self.allowed_entitlements,
                "client_authorization.allowed_entitlements",
                allowed=ENTITLEMENT_CLASSES,
            )
        _ = _v_sorted_safe_ids(
            self.blocked_resource_ids, "client_authorization.blocked_resource_ids"
        )
        if self.spend_limit is not None:
            _ = _v_instance_of(
                self.spend_limit, SpendingLimit, "client_authorization.spend_limit"
            )

    @classmethod
    def from_dict(cls, d: object) -> "ClientAuthorization":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "client_authorization")
        return cls(
            allowed_providers=(
                None
                if not _optional_present(dd, "allowed_providers")
                else _id_list_field(
                    dd["allowed_providers"], "client_authorization.allowed_providers"
                )
            ),
            allowed_channels=(
                None
                if not _optional_present(dd, "allowed_channels")
                else _id_list_field(
                    dd["allowed_channels"],
                    "client_authorization.allowed_channels",
                    allowed=EXECUTION_CHANNELS,
                )
            ),
            allowed_entitlements=(
                None
                if not _optional_present(dd, "allowed_entitlements")
                else _id_list_field(
                    dd["allowed_entitlements"],
                    "client_authorization.allowed_entitlements",
                    allowed=ENTITLEMENT_CLASSES,
                )
            ),
            blocked_resource_ids=(
                ()
                if not _optional_present(dd, "blocked_resource_ids")
                else _id_list_field(
                    dd["blocked_resource_ids"],
                    "client_authorization.blocked_resource_ids",
                )
            ),
            spend_limit=(
                None
                if not _optional_present(dd, "spend_limit")
                else SpendingLimit.from_dict(dd["spend_limit"])
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.allowed_providers is not None:
            out["allowed_providers"] = list(self.allowed_providers)
        if self.allowed_channels is not None:
            out["allowed_channels"] = list(self.allowed_channels)
        if self.allowed_entitlements is not None:
            out["allowed_entitlements"] = list(self.allowed_entitlements)
        if self.blocked_resource_ids:
            out["blocked_resource_ids"] = list(self.blocked_resource_ids)
        if self.spend_limit is not None:
            out["spend_limit"] = self.spend_limit.to_dict()
        return out


@dataclass(frozen=True)
class ClientRoutingProfile:
    """The administrator-configured routing profile for this request.

    The ``model`` field of an OpenAI-compatible client may carry an
    administrator-defined alias; this record is the resolved configured
    binding of that alias (or of the client identity) to an EXISTING
    profile id in the caller's :class:`TaskProfileCatalog` — never a second
    simplified scoring system (D-042). The optional provider narrowing is
    administrator-configured per-profile authorization narrowing: it
    constrains the explicitly selected target/model layer below it.
    """

    profile_id: str
    allowed_providers: tuple[str, ...] | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("profile_id",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("allowed_providers",)

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.profile_id, "client_routing_profile.profile_id")
        if self.allowed_providers is not None:
            _ = _v_sorted_safe_ids(
                self.allowed_providers, "client_routing_profile.allowed_providers"
            )

    @classmethod
    def from_dict(cls, d: object) -> "ClientRoutingProfile":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "client_routing_profile")
        return cls(
            profile_id=_v_safe_id(
                dd["profile_id"], "client_routing_profile.profile_id"
            ),
            allowed_providers=(
                None
                if not _optional_present(dd, "allowed_providers")
                else _id_list_field(
                    dd["allowed_providers"],
                    "client_routing_profile.allowed_providers",
                )
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"profile_id": self.profile_id}
        if self.allowed_providers is not None:
            out["allowed_providers"] = list(self.allowed_providers)
        return out


# ── Request binding and explicit pins ────────────────────────────────────────


@dataclass(frozen=True)
class PinnedTarget:
    """One pinned executable-target reference (recommendation binding).

    ``resource_id`` is the registry identifier of the target to execute —
    the reference a route decision publishes for exactly this purpose.
    ``decision_id``, when supplied, is the prior recommendation's decision
    id carried for audit provenance only: a recommendation is not a
    reservation, so admission re-checks current inputs and never trusts the
    prior decision's validity.
    """

    resource_id: str
    decision_id: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("resource_id",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("decision_id",)

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.resource_id, "pinned_target.resource_id")
        if self.decision_id is not None:
            _ = _v_safe_id(self.decision_id, "pinned_target.decision_id")

    @classmethod
    def from_dict(cls, d: object) -> "PinnedTarget":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "pinned_target")
        return cls(
            resource_id=_v_safe_id(dd["resource_id"], "pinned_target.resource_id"),
            decision_id=(
                None
                if not _optional_present(dd, "decision_id")
                else _v_safe_id(dd["decision_id"], "pinned_target.decision_id")
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"resource_id": self.resource_id}
        if self.decision_id is not None:
            out["decision_id"] = self.decision_id
        return out


def _request_bool_field(d: Mapping[str, object], key: str) -> bool:
    """Read an optional strict-boolean serialized field (absent = False).

    An explicitly present null is malformed, never "not required": a hard
    requirement must not be silently relaxed by malformed external input.
    """
    if key not in d:
        return False
    return _v_bool(d[key], f"request_binding.{key}")


@dataclass(frozen=True)
class RequestBinding:
    """The request-derived requirements and explicit pins (D-042 layers 3/5).

    The structural fields are compatibility requirements derived from
    request structure — never rankings and never an LLM request classifier:
    ``requires_tool_calls`` (tools present), ``requires_structured_output``,
    ``requires_streaming`` and ``requires_reasoning_controls`` gate the
    matching compatibility-matrix features (and ``requires_tool_calls``/
    ``requires_reasoning_controls`` tighten the merged task requirement's
    hard constraints), while the context/output sizes become hard-constraint
    minima in the merged requirement.

    The pin fields are the explicitly-selected-target layer: they narrow
    the candidate set and are honored or explicitly failed, never silently
    replaced. ``profile_alias`` and ``explicit_model`` are two readings of
    the same client ``model`` field and are mutually exclusive.
    ``explicit_variant`` is the effort pin (catalog variants identify the
    configured effort, D-032).
    """

    requires_tool_calls: bool = False
    requires_structured_output: bool = False
    requires_streaming: bool = False
    requires_reasoning_controls: bool = False
    minimum_input_context_tokens: int | None = None
    maximum_output_tokens: int | None = None
    profile_alias: str | None = None
    explicit_model: ModelRef | None = None
    explicit_variant: str | None = None
    pinned_target: PinnedTarget | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "requires_tool_calls",
        "requires_structured_output",
        "requires_streaming",
        "requires_reasoning_controls",
        "minimum_input_context_tokens",
        "maximum_output_tokens",
        "profile_alias",
        "explicit_model",
        "explicit_variant",
        "pinned_target",
    )

    def __post_init__(self) -> None:
        _ = _v_bool(self.requires_tool_calls, "request_binding.requires_tool_calls")
        _ = _v_bool(
            self.requires_structured_output,
            "request_binding.requires_structured_output",
        )
        _ = _v_bool(self.requires_streaming, "request_binding.requires_streaming")
        _ = _v_bool(
            self.requires_reasoning_controls,
            "request_binding.requires_reasoning_controls",
        )
        _ = _v_opt_int(
            self.minimum_input_context_tokens,
            "request_binding.minimum_input_context_tokens",
            lo=1,
        )
        _ = _v_opt_int(
            self.maximum_output_tokens,
            "request_binding.maximum_output_tokens",
            lo=1,
        )
        if self.profile_alias is not None:
            _ = _v_safe_id(self.profile_alias, "request_binding.profile_alias")
        if self.explicit_model is not None:
            _ = _v_instance_of(
                self.explicit_model, ModelRef, "request_binding.explicit_model"
            )
        if self.explicit_variant is not None:
            _ = _v_safe_id(self.explicit_variant, "request_binding.explicit_variant")
        if self.pinned_target is not None:
            _ = _v_instance_of(
                self.pinned_target, PinnedTarget, "request_binding.pinned_target"
            )
        if self.profile_alias is not None and self.explicit_model is not None:
            raise RouteContractValidationError(
                "request_binding: profile_alias and explicit_model are two "
                + "readings of the same client model field and are mutually "
                + "exclusive"
            )

    @classmethod
    def from_dict(cls, d: object) -> "RequestBinding":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "request_binding")
        explicit_model: ModelRef | None = None
        if _optional_present(dd, "explicit_model"):
            explicit_model = ModelRef.from_dict(dd["explicit_model"])
        pinned_target: PinnedTarget | None = None
        if _optional_present(dd, "pinned_target"):
            pinned_target = PinnedTarget.from_dict(dd["pinned_target"])
        return cls(
            requires_tool_calls=_request_bool_field(dd, "requires_tool_calls"),
            requires_structured_output=_request_bool_field(
                dd, "requires_structured_output"
            ),
            requires_streaming=_request_bool_field(dd, "requires_streaming"),
            requires_reasoning_controls=_request_bool_field(
                dd, "requires_reasoning_controls"
            ),
            minimum_input_context_tokens=_v_opt_int(
                dd.get("minimum_input_context_tokens"),
                "request_binding.minimum_input_context_tokens",
                lo=1,
            ),
            maximum_output_tokens=_v_opt_int(
                dd.get("maximum_output_tokens"),
                "request_binding.maximum_output_tokens",
                lo=1,
            ),
            profile_alias=(
                None
                if not _optional_present(dd, "profile_alias")
                else _v_safe_id(dd["profile_alias"], "request_binding.profile_alias")
            ),
            explicit_model=explicit_model,
            explicit_variant=(
                None
                if not _optional_present(dd, "explicit_variant")
                else _v_safe_id(
                    dd["explicit_variant"], "request_binding.explicit_variant"
                )
            ),
            pinned_target=pinned_target,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.requires_tool_calls:
            out["requires_tool_calls"] = True
        if self.requires_structured_output:
            out["requires_structured_output"] = True
        if self.requires_streaming:
            out["requires_streaming"] = True
        if self.requires_reasoning_controls:
            out["requires_reasoning_controls"] = True
        if self.minimum_input_context_tokens is not None:
            out["minimum_input_context_tokens"] = self.minimum_input_context_tokens
        if self.maximum_output_tokens is not None:
            out["maximum_output_tokens"] = self.maximum_output_tokens
        if self.profile_alias is not None:
            out["profile_alias"] = self.profile_alias
        if self.explicit_model is not None:
            out["explicit_model"] = self.explicit_model.to_dict()
        if self.explicit_variant is not None:
            out["explicit_variant"] = self.explicit_variant
        if self.pinned_target is not None:
            out["pinned_target"] = self.pinned_target.to_dict()
        return out


# ── OpenAI compatibility-matrix cell representation (D-043) ──────────────────


@dataclass(frozen=True)
class CompatibilityCell:
    """One typed OpenAI compatibility-matrix cell (D-043 representation).

    Keyed by ``(channel, provider, model, variant, feature)`` — the
    execution surface and physical backend, with the adapter and its
    version and a dated evidence reference exactly as the frozen matrix
    requires. ``variant`` ``None`` is a backend-level cell that applies to
    every calibrated variant of the model for variant-qualified resources;
    a variant-qualified cell applies only to that variant and takes
    precedence over a backend-level cell. Values are the frozen
    ``PASS``/``PARTIAL``/``UNSUPPORTED``/``UNKNOWN`` tokens; the routing
    core serves a request feature only on ``PASS`` or ``PARTIAL`` and fails
    closed on everything else, including a missing cell. M02 defines this
    representation; the per-backend cell VALUES are evidence that
    M03/M04/M06/M07 supply later.
    """

    channel: str
    provider: str
    model: str
    feature: str
    value: str
    adapter: str
    adapter_version: str
    evidence: EvidenceRef
    variant: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "channel",
        "provider",
        "model",
        "feature",
        "value",
        "adapter",
        "adapter_version",
        "evidence",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("variant",)

    def __post_init__(self) -> None:
        _ = _v_enum(self.channel, EXECUTION_CHANNELS, "compatibility_cell.channel")
        _ = _v_safe_id(self.provider, "compatibility_cell.provider")
        _ = _v_safe_id(self.model, "compatibility_cell.model")
        _ = _v_enum(
            self.feature, frozenset(COMPAT_FEATURES), "compatibility_cell.feature"
        )
        _ = _v_enum(self.value, COMPAT_CELL_VALUES, "compatibility_cell.value")
        _ = _v_safe_id(self.adapter, "compatibility_cell.adapter")
        _ = _v_text(
            self.adapter_version, "compatibility_cell.adapter_version", max_len=128
        )
        _ = _v_instance_of(self.evidence, EvidenceRef, "compatibility_cell.evidence")
        if self.variant is not None:
            _ = _v_safe_id(self.variant, "compatibility_cell.variant")

    @classmethod
    def from_dict(cls, d: object) -> "CompatibilityCell":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "compatibility_cell")
        return cls(
            channel=_v_enum(
                dd["channel"], EXECUTION_CHANNELS, "compatibility_cell.channel"
            ),
            provider=_v_safe_id(dd["provider"], "compatibility_cell.provider"),
            model=_v_safe_id(dd["model"], "compatibility_cell.model"),
            feature=_v_enum(
                dd["feature"], frozenset(COMPAT_FEATURES), "compatibility_cell.feature"
            ),
            value=_v_enum(dd["value"], COMPAT_CELL_VALUES, "compatibility_cell.value"),
            adapter=_v_safe_id(dd["adapter"], "compatibility_cell.adapter"),
            adapter_version=_v_text(
                dd["adapter_version"],
                "compatibility_cell.adapter_version",
                max_len=128,
            ),
            evidence=EvidenceRef.from_dict(dd["evidence"]),
            variant=(
                None
                if not _optional_present(dd, "variant")
                else _v_safe_id(dd["variant"], "compatibility_cell.variant")
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "channel": self.channel,
            "provider": self.provider,
            "model": self.model,
            "feature": self.feature,
            "value": self.value,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "evidence": self.evidence.to_dict(),
        }
        if self.variant is not None:
            out["variant"] = self.variant
        return out


# ── Effective authorization (pure intersection of the grant layers) ──────────


@dataclass(frozen=True)
class _EffectiveAuthorization:
    """The narrowed authorization space after all grant layers (internal)."""

    allowed_providers: frozenset[str] | None
    allowed_channels: frozenset[str] | None
    allowed_entitlements: frozenset[str] | None
    blocked_resource_ids: frozenset[str]
    spend_limit: SpendingLimit | None


def _intersect_optional(
    a: tuple[str, ...] | None, b: tuple[str, ...] | None
) -> frozenset[str] | None:
    """Intersection of two optional allow-sets; ``None`` = unrestricted."""
    if a is None:
        return None if b is None else frozenset(b)
    if b is None:
        return frozenset(a)
    return frozenset(a) & frozenset(b)


def _effective_authorization(
    admin: AdministratorConstraints,
    client: ClientAuthorization,
    profile: ClientRoutingProfile | None,
) -> _EffectiveAuthorization:
    """Intersect administrator, client and profile-provider narrowing.

    Each layer may only narrow: the allowed sets intersect and the blocked
    sets and spending limits take the most restrictive combination. A
    client grant broader than the administrator's changes nothing.
    """
    allowed_providers = _intersect_optional(
        admin.allowed_providers, client.allowed_providers
    )
    if profile is not None and profile.allowed_providers is not None:
        allowed_providers = (
            frozenset(profile.allowed_providers)
            if allowed_providers is None
            else allowed_providers & frozenset(profile.allowed_providers)
        )
    limits = [
        limit
        for limit in (admin.spend_limit, client.spend_limit)
        if limit is not None
    ]
    spend_limit = (
        min(limits, key=lambda limit: limit.micro_usd_per_mtoken_max)
        if limits
        else None
    )
    return _EffectiveAuthorization(
        allowed_providers=allowed_providers,
        allowed_channels=_intersect_optional(
            admin.allowed_channels, client.allowed_channels
        ),
        allowed_entitlements=_intersect_optional(
            admin.allowed_entitlements, client.allowed_entitlements
        ),
        blocked_resource_ids=frozenset(admin.blocked_resource_ids)
        | frozenset(client.blocked_resource_ids),
        spend_limit=spend_limit,
    )


# ── Internal per-resource gate evaluation ────────────────────────────────────


@dataclass(frozen=True)
class _ResourceGate:
    """The gate outcome for one registry resource (internal).

    ``qualified`` resources may serve their bound identities; excluded
    resources carry the first failing stage, all of that stage's reason
    codes and the structured detail fields that explain them.
    """

    entry: ResourceRegistryEntry
    bound_identities: tuple[ModelIdentity, ...]
    qualified: bool
    stage: str | None = None
    reason_codes: tuple[str, ...] = ()
    health_status: str | None = None
    compatibility_feature: str | None = None
    compatibility_value: str | None = None
    eligibility: ExecutionEligibility | None = None
    promotion_sources: tuple[str, ...] = ()


def _bind_identities(
    entry: ResourceRegistryEntry, catalog: ModelCatalog
) -> tuple[ModelIdentity, ...]:
    """Catalog identities this resource can physically execute.

    Exact ``(provider, model)`` match; a resource without a variant
    qualifier binds every calibrated variant of the model, a
    variant-qualified resource binds only the exact variant.
    """
    identity = entry.identity
    return tuple(
        catalog_entry.identity
        for catalog_entry in catalog.entries
        if catalog_entry.identity.provider == identity.provider
        and catalog_entry.identity.model == identity.model
        and (
            identity.variant is None
            or identity.variant == catalog_entry.identity.variant
        )
    )


def _lookup_cell(
    cells: tuple[CompatibilityCell, ...],
    identity: ResourceIdentity,
    feature: str,
) -> CompatibilityCell | None:
    """The current matrix cell for one resource and feature, or ``None``.

    A variant-qualified cell takes precedence over a backend-level cell for
    a variant-qualified resource; a variant-specific cell never applies to
    a resource that carries no variant qualifier (the granularities
    disagree — fail closed as unknown).
    """
    identity_variant = identity.variant
    specific: CompatibilityCell | None = None
    backend: CompatibilityCell | None = None
    for cell in cells:
        if (
            cell.channel == identity.channel
            and cell.provider == identity.provider
            and cell.model == identity.model
            and cell.feature == feature
        ):
            if cell.variant is None:
                backend = cell
            elif identity_variant is not None and cell.variant == identity_variant:
                specific = cell
    return specific if specific is not None else backend


def _spend_limit_codes(
    entry: ResourceRegistryEntry, limit: SpendingLimit
) -> tuple[str, ...]:
    """Spending-limit reason codes for one resource (fail closed).

    Only entitlements with a marginal monetary spend are gated: a
    subscription, promotional or local/ungated surface has no marginal
    per-token monetary cost, so a configured limit cannot exclude it. A
    metered surface without a cost record is unverifiable (never free), and
    any known price above the ceiling exceeds the limit.
    """
    identity = entry.identity
    if identity.entitlement not in METERED_ENTITLEMENTS:
        if identity.entitlement == "unknown":
            return ("spend_limit_unverifiable",)
        return ()
    cost = entry.cost
    if cost is None:
        return ("spend_limit_unverifiable",)
    prices = [
        price
        for price in (
            cost.input_micro_usd_per_mtoken,
            cost.output_micro_usd_per_mtoken,
        )
        if price is not None
    ]
    if not prices:
        return ("spend_limit_unverifiable",)
    if any(price > limit.micro_usd_per_mtoken_max for price in prices):
        return ("spend_limit_exceeded",)
    return ()


def _authorization_failure_codes(
    entry: ResourceRegistryEntry, effective: _EffectiveAuthorization
) -> tuple[str, ...]:
    """All authorization-stage reason codes for one resource, in fixed order."""
    identity = entry.identity
    codes: list[str] = []
    if (
        effective.allowed_providers is not None
        and identity.provider not in effective.allowed_providers
    ):
        codes.append("unauthorized_provider")
    if (
        effective.allowed_channels is not None
        and identity.channel not in effective.allowed_channels
    ):
        codes.append("unauthorized_channel")
    if (
        effective.allowed_entitlements is not None
        and identity.entitlement not in effective.allowed_entitlements
    ):
        codes.append("unauthorized_entitlement")
    if identity.resource_id in effective.blocked_resource_ids:
        codes.append("resource_blocked")
    if effective.spend_limit is not None:
        codes.extend(_spend_limit_codes(entry, effective.spend_limit))
    return tuple(codes)


def _availability_failure_codes(
    entry: ResourceRegistryEntry,
    eligibility_reports: tuple[ExecutionEligibility, ...],
) -> tuple[str, ...]:
    """Availability-stage reason codes (empty when fully available).

    Fail closed on every non-fresh, non-``ok`` or execution-ineligible
    state: ``never_observed`` is an honest unknown (never healthy),
    staleness never rewrites the observed health, and a D-039 report that
    is not ``eligible`` blocks the provider's resources. Absence of a
    report means the eligibility stage never applies — never "eligible" by
    default (D-039, preserved verbatim).
    """
    codes: list[str] = []
    if entry.freshness == "never_observed":
        codes.append("resource_never_observed")
    elif entry.freshness == "stale":
        codes.append("resource_stale")
    observation = entry.observation
    if observation is not None and observation.health.status != "ok":
        codes.append("resource_unhealthy")
    report = next(
        (r for r in eligibility_reports if r.provider == entry.identity.provider),
        None,
    )
    if report is not None and report.state != "eligible":
        codes.append("execution_ineligible")
    return tuple(codes)


def _compatibility_failure(
    entry: ResourceRegistryEntry,
    request: RequestBinding,
    cells: tuple[CompatibilityCell, ...],
    requirement: TaskRequirement,
) -> tuple[tuple[str, ...], str | None, str | None]:
    """Compatibility-stage failure codes plus the first failing feature.

    Requested features must have a ``PASS`` or ``PARTIAL`` matrix cell;
    ``UNSUPPORTED``, ``UNKNOWN`` and a missing cell fail closed with the
    distinguishing code. The numeric context ceiling is the M01
    registration's ``context_limit_tokens``: unknown fails closed when a
    context minimum is required, and a known ceiling below the requirement
    is insufficient. Returns the sorted-duplicate-free code tuple in fixed
    evaluation order together with the first failing matrix feature and its
    cell value (``None`` when the failure is the context ceiling or the
    cell is missing).
    """
    codes: list[str] = []
    first_feature: str | None = None
    first_value: str | None = None
    for flag_getter, feature in _REQUEST_FEATURE_MAP:
        if not flag_getter(request):
            continue
        cell = _lookup_cell(cells, entry.identity, feature)
        if cell is None:
            code, value = "compatibility_unknown", None
        elif cell.value == "UNKNOWN":
            code, value = "compatibility_unknown", cell.value
        elif cell.value == "UNSUPPORTED":
            code, value = "compatibility_unsupported", cell.value
        else:
            continue
        codes.append(code)
        if first_feature is None:
            first_feature = feature
            first_value = value
    minimum_context = requirement.hard_constraints.minimum_input_context_tokens
    if minimum_context is not None:
        context_limit = entry.capabilities.context_limit_tokens
        if context_limit is None:
            codes.append("context_limit_unknown")
        elif context_limit < minimum_context:
            codes.append("context_limit_insufficient")
    return tuple(codes), first_feature, first_value


def _parse_promotion_bounds(
    promotion: PromotionObservation,
) -> tuple[datetime | None, datetime | None]:
    valid_from = (
        None
        if promotion.valid_from is None
        else _parse_canonical_ts(promotion.valid_from, "promotion.valid_from")
    )
    valid_until = (
        None
        if promotion.valid_until is None
        else _parse_canonical_ts(promotion.valid_until, "promotion.valid_until")
    )
    return valid_from, valid_until


def _promotion_matches(
    promotion: PromotionObservation, entry: ResourceRegistryEntry
) -> bool:
    """Whether a promotion's evidenced scopes match one resource.

    Every present scope must match; a scope that is absent is unknown and
    never means "applies everywhere" (M01). A ``plan`` scope cannot be
    verified against a resource at all (resource identity carries no plan),
    so a promotion carrying one never contributes a preference — fail
    closed, exactly like every other unverifiable fact.
    """
    if promotion.plan is not None:
        return False
    identity = entry.identity
    if promotion.channel is not None and promotion.channel != identity.channel:
        return False
    if promotion.provider is not None and promotion.provider != identity.provider:
        return False
    if promotion.model is not None and promotion.model != identity.model:
        return False
    return True


def _promotion_preference(
    entry: ResourceRegistryEntry, evaluated_at: datetime
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Active contributing promotion sources and expired relevant sources.

    A promotion contributes a target-level routing preference only while
    its validity period covers the evaluation instant and its evidenced
    scopes match the resource. An expired promotion whose scopes WOULD
    match is reported separately (the "why did the preference disappear?"
    provenance) and never contributes. A promotion is never proof that an
    execution qualifies for it (D-039 gating remains the proof path).
    """
    observation = entry.observation
    if observation is None or not observation.promotions:
        return (), ()
    active: set[str] = set()
    expired: set[str] = set()
    for promotion in observation.promotions:
        if not _promotion_matches(promotion, entry):
            continue
        valid_from, valid_until = _parse_promotion_bounds(promotion)
        if valid_until is not None and valid_until < evaluated_at:
            expired.add(promotion.source)
            continue
        if valid_from is not None and valid_from > evaluated_at:
            continue
        active.add(promotion.source)
    return tuple(sorted(active)), tuple(sorted(expired))


def _evaluate_resource(
    entry: ResourceRegistryEntry,
    catalog: ModelCatalog,
    effective: _EffectiveAuthorization,
    request: RequestBinding,
    cells: tuple[CompatibilityCell, ...],
    requirement: TaskRequirement,
    eligibility_reports: tuple[ExecutionEligibility, ...],
    evaluated_at: datetime,
) -> _ResourceGate:
    """Run the frozen gate pipeline for one resource.

    Stage order: ``binding -> authorization -> availability ->
    compatibility``; the first failing stage wins and later stages never
    run merely to populate fields. Promotion preference is evaluated for
    every resource regardless of the gate outcome so expired preferences
    stay explainable.
    """
    bound = _bind_identities(entry, catalog)
    promotion_sources, _ = _promotion_preference(entry, evaluated_at)
    if not bound:
        return _ResourceGate(
            entry=entry,
            bound_identities=(),
            qualified=False,
            stage="binding",
            reason_codes=("capability_unassessed",),
            promotion_sources=promotion_sources,
        )
    authorization_codes = _authorization_failure_codes(entry, effective)
    if authorization_codes:
        return _ResourceGate(
            entry=entry,
            bound_identities=bound,
            qualified=False,
            stage="authorization",
            reason_codes=authorization_codes,
            promotion_sources=promotion_sources,
        )
    availability_codes = _availability_failure_codes(entry, eligibility_reports)
    if availability_codes:
        report = next(
            (
                r
                for r in eligibility_reports
                if r.provider == entry.identity.provider and r.state != "eligible"
            ),
            None,
        )
        return _ResourceGate(
            entry=entry,
            bound_identities=bound,
            qualified=False,
            stage="availability",
            reason_codes=availability_codes,
            health_status=(
                None if entry.observation is None else entry.observation.health.status
            ),
            eligibility=report,
            promotion_sources=promotion_sources,
        )
    compatibility_codes, first_feature, first_value = _compatibility_failure(
        entry, request, cells, requirement
    )
    if compatibility_codes:
        return _ResourceGate(
            entry=entry,
            bound_identities=bound,
            qualified=False,
            stage="compatibility",
            reason_codes=compatibility_codes,
            compatibility_feature=first_feature,
            compatibility_value=first_value,
            promotion_sources=promotion_sources,
        )
    return _ResourceGate(
        entry=entry,
        bound_identities=bound,
        qualified=True,
        promotion_sources=promotion_sources,
    )


# ── Output contracts ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RouteTarget:
    """One authorized executable target (the five D-042 dimensions).

    ``resource`` carries the execution channel/surface, the physical
    backend model and the entitlement; ``resource.resource_id`` is the
    executable-target reference a client pins to execute this choice.
    ``model`` is the calibrated catalog identity (physical model/variant)
    the target executes. ``quota_pools`` are the resource's CONFIRMED
    shared pools (D-042): pools exist only from explicit confirmed
    ``quota_pool_ids``, never inferred. ``routing_profile`` is the client
    routing profile under which the decision was made.
    ``shares_quota_pool_with_selected`` is ``True`` only on target
    alternatives whose confirmed pools intersect the selected target's —
    they draw one budget and are never independent fallback capacity
    (there is no automatic failover).
    """

    resource: ResourceIdentity
    model: ModelIdentity
    quota_pools: tuple[str, ...] = ()
    routing_profile: str | None = None
    promotion_sources: tuple[str, ...] = ()
    shares_quota_pool_with_selected: bool = False

    def __post_init__(self) -> None:
        _ = _v_instance_of(self.resource, ResourceIdentity, "route_target.resource")
        _ = _v_instance_of(self.model, ModelIdentity, "route_target.model")
        _ = _v_sorted_safe_ids(self.quota_pools, "route_target.quota_pools")
        if self.routing_profile is not None:
            _ = _v_safe_id(self.routing_profile, "route_target.routing_profile")
        _ = _v_sorted_safe_ids(
            self.promotion_sources, "route_target.promotion_sources"
        )
        _ = _v_bool(
            self.shares_quota_pool_with_selected,
            "route_target.shares_quota_pool_with_selected",
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "resource": self.resource.to_dict(),
            "model": self.model.to_dict(),
            "quota_pools": list(self.quota_pools),
        }
        if self.routing_profile is not None:
            out["routing_profile"] = self.routing_profile
        if self.promotion_sources:
            out["promotion_sources"] = list(self.promotion_sources)
        if self.shares_quota_pool_with_selected:
            out["shares_quota_pool_with_selected"] = True
        return out


@dataclass(frozen=True)
class TargetExclusion:
    """One typed per-resource exclusion record (first failing stage wins).

    ``stage`` is from the closed ``ROUTE_EXCLUSION_STAGES`` vocabulary and
    ``reason_codes`` carries at least one primary code for that stage plus
    any additional codes of the same stage, sorted. The structured detail
    fields carry the failing state honestly (health status, freshness,
    compatibility feature/value, the paired D-039 report) without ever
    embedding raw provider payloads or account identifiers.
    """

    resource_id: str
    stage: str
    reason_codes: tuple[str, ...]
    channel: str | None = None
    provider: str | None = None
    model: str | None = None
    health_status: str | None = None
    freshness: str | None = None
    compatibility_feature: str | None = None
    compatibility_value: str | None = None
    execution_eligibility: ExecutionEligibility | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("resource_id", "stage", "reason_codes")
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "channel",
        "provider",
        "model",
        "health_status",
        "freshness",
        "compatibility_feature",
        "compatibility_value",
        "execution_eligibility",
    )

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.resource_id, "target_exclusion.resource_id")
        stage = _v_enum(
            self.stage, frozenset(ROUTE_EXCLUSION_STAGES), "target_exclusion.stage"
        )
        codes = _v_sorted_safe_ids(self.reason_codes, "target_exclusion.reason_codes")
        if not codes:
            raise RouteContractValidationError(
                "target_exclusion: an excluded resource requires at least one "
                + "reason code"
            )
        object.__setattr__(self, "reason_codes", codes)
        stage_reasons = _STAGE_REASONS[stage]
        if not stage_reasons & set(codes):
            raise RouteContractValidationError(
                "target_exclusion: reason codes "
                + f"{list(codes)} carry no primary code for stage {stage!r}"
            )
        if self.channel is not None:
            _ = _v_enum(self.channel, EXECUTION_CHANNELS, "target_exclusion.channel")
        if self.provider is not None:
            _ = _v_safe_id(self.provider, "target_exclusion.provider")
        if self.model is not None:
            _ = _v_safe_id(self.model, "target_exclusion.model")
        if self.health_status is not None:
            _ = _v_enum(
                self.health_status,
                RESOURCE_HEALTH_STATUSES,
                "target_exclusion.health_status",
            )
        if self.freshness is not None:
            _ = _v_enum(
                self.freshness, FRESHNESS_STATES, "target_exclusion.freshness"
            )
        if self.compatibility_feature is not None:
            _ = _v_enum(
                self.compatibility_feature,
                frozenset(COMPAT_FEATURES),
                "target_exclusion.compatibility_feature",
            )
            if stage != "compatibility":
                raise RouteContractValidationError(
                    "target_exclusion: compatibility_feature is carried only "
                    + "on compatibility-stage exclusions"
                )
        if self.compatibility_value is not None:
            _ = _v_enum(
                self.compatibility_value,
                COMPAT_CELL_VALUES,
                "target_exclusion.compatibility_value",
            )
        if self.execution_eligibility is not None:
            _ = _v_instance_of(
                self.execution_eligibility,
                ExecutionEligibility,
                "target_exclusion.execution_eligibility",
            )
            if stage != "availability":
                raise RouteContractValidationError(
                    "target_exclusion: execution_eligibility is carried only "
                    + "on availability-stage exclusions"
                )

    @classmethod
    def from_dict(cls, d: object) -> "TargetExclusion":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "target_exclusion")
        raw_codes = dd["reason_codes"]
        if not isinstance(raw_codes, list):
            raise RouteContractValidationError(
                f"target_exclusion.reason_codes: expected list, got "
                + f"{type(raw_codes).__name__}"
            )
        eligibility: ExecutionEligibility | None = None
        if _optional_present(dd, "execution_eligibility"):
            eligibility = ExecutionEligibility.from_dict(dd["execution_eligibility"])
        return cls(
            resource_id=_v_safe_id(dd["resource_id"], "target_exclusion.resource_id"),
            stage=_v_enum(
                dd["stage"],
                frozenset(ROUTE_EXCLUSION_STAGES),
                "target_exclusion.stage",
            ),
            reason_codes=tuple(cast("list[str]", raw_codes)),
            channel=(
                None
                if not _optional_present(dd, "channel")
                else _v_enum(
                    dd["channel"], EXECUTION_CHANNELS, "target_exclusion.channel"
                )
            ),
            provider=(
                None
                if not _optional_present(dd, "provider")
                else _v_safe_id(dd["provider"], "target_exclusion.provider")
            ),
            model=(
                None
                if not _optional_present(dd, "model")
                else _v_safe_id(dd["model"], "target_exclusion.model")
            ),
            health_status=(
                None
                if not _optional_present(dd, "health_status")
                else _v_enum(
                    dd["health_status"],
                    RESOURCE_HEALTH_STATUSES,
                    "target_exclusion.health_status",
                )
            ),
            freshness=(
                None
                if not _optional_present(dd, "freshness")
                else _v_enum(
                    dd["freshness"], FRESHNESS_STATES, "target_exclusion.freshness"
                )
            ),
            compatibility_feature=(
                None
                if not _optional_present(dd, "compatibility_feature")
                else _v_enum(
                    dd["compatibility_feature"],
                    frozenset(COMPAT_FEATURES),
                    "target_exclusion.compatibility_feature",
                )
            ),
            compatibility_value=(
                None
                if not _optional_present(dd, "compatibility_value")
                else _v_enum(
                    dd["compatibility_value"],
                    COMPAT_CELL_VALUES,
                    "target_exclusion.compatibility_value",
                )
            ),
            execution_eligibility=eligibility,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "resource_id": self.resource_id,
            "stage": self.stage,
            "reason_codes": list(self.reason_codes),
        }
        if self.channel is not None:
            out["channel"] = self.channel
        if self.provider is not None:
            out["provider"] = self.provider
        if self.model is not None:
            out["model"] = self.model
        if self.health_status is not None:
            out["health_status"] = self.health_status
        if self.freshness is not None:
            out["freshness"] = self.freshness
        if self.compatibility_feature is not None:
            out["compatibility_feature"] = self.compatibility_feature
        if self.compatibility_value is not None:
            out["compatibility_value"] = self.compatibility_value
        if self.execution_eligibility is not None:
            out["execution_eligibility"] = self.execution_eligibility.to_dict()
        return out


def _derive_decision_id(content: Mapping[str, object]) -> str:
    """Derive the deterministic decision id from serialized decision content.

    SHA-256 over the canonical JSON rendering (sorted keys, compact
    separators) of the decision content EXCLUDING the decision id itself,
    prefixed ``rd-``. Identical inputs and evaluation time therefore
    produce the identical identifier, and any content difference — however
    small — produces a different one.
    """
    rendered = json.dumps(content, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return f"rd-{digest[:32]}"


@dataclass(frozen=True)
class RouteDecision:
    """The structured deterministic route decision (D-042).

    ``selection`` embeds the complete model-level
    :class:`~scarcity_router.selector.SelectionDecision` — evaluated
    instant, resolved requirement, catalog/policy versions, profile
    provenance, ranked candidates, model-level exclusions, closest
    candidates and degraded state — so nothing about the underlying
    balanced decision is hidden. The route layer adds the executable
    target (five D-042 dimensions), its target-level alternatives, the
    per-resource exclusion records, the identities removed by target
    narrowing and the expired-promotion provenance.

    ``decision_id`` is derived deterministically from the serialized
    decision content (excluding the id itself) when omitted — the
    recommended path, which makes the id content-addressed; an explicitly
    supplied value must be a valid safe identifier and is preserved
    verbatim. Like the selector's output contracts this record serializes
    with ``to_dict`` only — deserialization of decisions is not an input
    boundary.
    """

    status: str
    selection: SelectionDecision
    registry_revision: int
    registry_generated_at: str
    target: RouteTarget | None = None
    target_alternatives: tuple[RouteTarget, ...] = ()
    unroutable_identities: tuple[ModelIdentity, ...] = ()
    target_exclusions: tuple[TargetExclusion, ...] = ()
    expired_promotions: tuple[str, ...] = ()
    pinned: bool = False
    reason_codes: tuple[str, ...] = ()
    decision_id: str | None = None
    schema_version: int = ROUTE_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _ = _v_int(
            self.schema_version,
            "route_decision.schema_version",
            lo=ROUTE_DECISION_SCHEMA_VERSION,
        )
        if self.schema_version != ROUTE_DECISION_SCHEMA_VERSION:
            raise RouteContractValidationError(
                f"route_decision.schema_version: expected int "
                + f"{ROUTE_DECISION_SCHEMA_VERSION}, got {self.schema_version!r}"
            )
        _ = _v_enum(self.status, ROUTE_STATUSES, "route_decision.status")
        _ = _v_instance_of(
            self.selection, SelectionDecision, "route_decision.selection"
        )
        _ = _v_int(self.registry_revision, "route_decision.registry_revision", lo=0)
        _ = _v_text(
            self.registry_generated_at,
            "route_decision.registry_generated_at",
            max_len=64,
        )
        _ = _parse_canonical_ts(
            self.registry_generated_at, "route_decision.registry_generated_at"
        )
        if self.target is not None:
            _ = _v_instance_of(self.target, RouteTarget, "route_decision.target")
        _ = _v_tuple_of(
            self.target_alternatives,
            RouteTarget,
            "route_decision.target_alternatives",
        )
        _ = _v_tuple_of(
            self.unroutable_identities,
            ModelIdentity,
            "route_decision.unroutable_identities",
        )
        seen_keys = {_identity_key(identity) for identity in self.unroutable_identities}
        if len(seen_keys) != len(self.unroutable_identities):
            raise RouteContractValidationError(
                "route_decision.unroutable_identities: duplicate identities "
                + "are not permitted"
            )
        _ = _v_tuple_of(
            self.target_exclusions, TargetExclusion, "route_decision.target_exclusions"
        )
        _ = _v_sorted_safe_ids(
            self.expired_promotions, "route_decision.expired_promotions"
        )
        _ = _v_bool(self.pinned, "route_decision.pinned")
        codes = _v_sorted_safe_ids(
            self.reason_codes, "route_decision.reason_codes", allowed=ROUTE_REASON_CODES
        )
        object.__setattr__(self, "reason_codes", codes)
        if self.status == ROUTE_STATUS_SELECTED:
            if self.target is None:
                raise RouteContractValidationError(
                    "route_decision: a selected decision requires a target"
                )
            if self.target.shares_quota_pool_with_selected:
                raise RouteContractValidationError(
                    "route_decision: the selected target never shares a pool "
                    + "with itself"
                )
            if set(codes) != {"route_selected"}:
                raise RouteContractValidationError(
                    "route_decision: a selected decision carries exactly the "
                    + "'route_selected' reason code"
                )
        else:
            if self.target is not None or self.target_alternatives:
                raise RouteContractValidationError(
                    "route_decision: a no-solution result has no target"
                )
            if "no_eligible_target" not in codes:
                raise RouteContractValidationError(
                    "route_decision: a no-solution result requires the "
                    + "'no_eligible_target' reason code"
                )
            if self.pinned and "pinned_request_failed" not in codes:
                raise RouteContractValidationError(
                    "route_decision: a failed pinned request carries the "
                    + "'pinned_request_failed' reason code"
                )
        if self.decision_id is None:
            object.__setattr__(
                self, "decision_id", _derive_decision_id(self._content_dict())
            )
        else:
            _ = _v_safe_id(self.decision_id, "route_decision.decision_id")

    def _content_dict(self) -> dict[str, object]:
        """The deterministic serialized content EXCLUDING the decision id."""
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "selection": self.selection.to_dict(),
            "registry_revision": self.registry_revision,
            "registry_generated_at": self.registry_generated_at,
            "target": self.target.to_dict() if self.target is not None else None,
            "target_alternatives": [t.to_dict() for t in self.target_alternatives],
            "unroutable_identities": [
                identity.to_dict() for identity in self.unroutable_identities
            ],
            "target_exclusions": [e.to_dict() for e in self.target_exclusions],
            "expired_promotions": list(self.expired_promotions),
            "pinned": self.pinned,
            "reason_codes": list(self.reason_codes),
        }

    def to_dict(self) -> dict[str, object]:
        decision_id = self.decision_id
        if decision_id is None:  # pragma: no cover - __post_init__ derives it
            raise RouteContractValidationError(
                "route_decision: decision_id was not derived"
            )
        out = self._content_dict()
        out["decision_id"] = decision_id
        return out


@dataclass(frozen=True)
class AdmissionDecision:
    """The admission-only outcome for one pinned target (D-042 binding).

    ``admit_pinned_target`` performs exactly the admission gates —
    authorization, limits, availability, compatibility — for the one named
    resource and never re-runs competitive ranking. ``approved`` carries
    the target's five-dimension facts; a rejection carries the typed
    exclusion (or the ``pin_target_not_found`` code when the reference
    names no registered resource). ``bound_decision_id`` echoes the prior
    recommendation's decision id for audit provenance; it is never a
    reservation and never trusted as a validity proof.
    """

    resource_id: str
    approved: bool
    reason_codes: tuple[str, ...]
    target: RouteTarget | None = None
    exclusion: TargetExclusion | None = None
    bound_decision_id: str | None = None

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.resource_id, "admission_decision.resource_id")
        _ = _v_bool(self.approved, "admission_decision.approved")
        codes = _v_sorted_safe_ids(
            self.reason_codes,
            "admission_decision.reason_codes",
            allowed=ADMISSION_REASON_CODES | TARGET_EXCLUSION_REASON_CODES,
        )
        if not codes:
            raise RouteContractValidationError(
                "admission_decision: reason codes are required"
            )
        object.__setattr__(self, "reason_codes", codes)
        if self.approved:
            if self.target is None:
                raise RouteContractValidationError(
                    "admission_decision: an approval carries its target"
                )
            if self.exclusion is not None:
                raise RouteContractValidationError(
                    "admission_decision: an approval carries no exclusion"
                )
            if "admission_approved" not in codes:
                raise RouteContractValidationError(
                    "admission_decision: an approval requires the "
                    + "'admission_approved' reason code"
                )
        else:
            if self.target is not None:
                raise RouteContractValidationError(
                    "admission_decision: a rejection carries no target"
                )
            if "admission_rejected" not in codes:
                raise RouteContractValidationError(
                    "admission_decision: a rejection requires the "
                    + "'admission_rejected' reason code"
                )
        if self.exclusion is not None:
            _ = _v_instance_of(
                self.exclusion, TargetExclusion, "admission_decision.exclusion"
            )
        if self.bound_decision_id is not None:
            _ = _v_safe_id(
                self.bound_decision_id, "admission_decision.bound_decision_id"
            )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "resource_id": self.resource_id,
            "approved": self.approved,
            "reason_codes": list(self.reason_codes),
        }
        if self.target is not None:
            out["target"] = self.target.to_dict()
        if self.exclusion is not None:
            out["exclusion"] = self.exclusion.to_dict()
        if self.bound_decision_id is not None:
            out["bound_decision_id"] = self.bound_decision_id
        return out


# ── The typed request bundle ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RouteRequest:
    """Every observation and layer one route decision consumes.

    Pure and construction-validated: the caller supplies the catalog and
    profile artifacts (typed), the M01 registry snapshot, the capacity v3
    snapshots, the D-039 eligibility reports, the typed compatibility
    cells, both authorization layers, the configured routing profile, the
    request binding and one timezone-aware evaluation instant. Nothing is
    read from the filesystem, the environment, the network or a clock.

    Cross-input invariants enforced at construction: the evaluation instant
    must not precede the registry snapshot's ``generated_at`` (a decision
    before its own state snapshot is incoherent and fails closed), a
    ``profile_alias`` requires the configured routing profile, eligibility
    reports admit at most one per provider (D-039) and compatibility cells
    admit at most one per ``(channel, provider, model, variant, feature)``
    key (the caller supplies the current matrix, never an ambiguous
    history). Report and cell collections are stored in their canonical
    sorted order, so caller input order never affects the decision.
    """

    catalog: ModelCatalog
    profiles: TaskProfileCatalog
    registry_snapshot: RegistrySnapshot
    policy: SelectorPolicy
    evaluated_at: datetime
    capacity_snapshots: tuple[CapacitySnapshot, ...] = ()
    replenishment_states: tuple[ReplenishmentState, ...] = ()
    eligibility_reports: tuple[ExecutionEligibility, ...] = ()
    compatibility_cells: tuple[CompatibilityCell, ...] = ()
    admin_constraints: AdministratorConstraints = field(
        default_factory=AdministratorConstraints
    )
    client_authorization: ClientAuthorization = field(
        default_factory=ClientAuthorization
    )
    routing_profile: ClientRoutingProfile | None = None
    request: RequestBinding = field(default_factory=RequestBinding)
    profile_policy_version: int | None = None

    def __post_init__(self) -> None:
        _ = _v_instance_of(self.catalog, ModelCatalog, "route_request.catalog")
        _ = _v_instance_of(self.profiles, TaskProfileCatalog, "route_request.profiles")
        _ = _v_instance_of(
            self.registry_snapshot, RegistrySnapshot, "route_request.registry_snapshot"
        )
        _ = _v_instance_of(self.policy, SelectorPolicy, "route_request.policy")
        _ = _v_aware_datetime(self.evaluated_at, "route_request.evaluated_at")
        _ = _v_tuple_of(
            self.capacity_snapshots,
            CapacitySnapshot,
            "route_request.capacity_snapshots",
        )
        _ = _v_tuple_of(
            self.replenishment_states,
            ReplenishmentState,
            "route_request.replenishment_states",
        )
        reports = _v_tuple_of(
            self.eligibility_reports,
            ExecutionEligibility,
            "route_request.eligibility_reports",
        )
        seen_providers: set[str] = set()
        for report in reports:
            if report.provider in seen_providers:
                raise RouteContractValidationError(
                    "route_request.eligibility_reports: duplicate eligibility "
                    + f"report for provider {report.provider!r}; at most one "
                    + "report per provider is permitted"
                )
            seen_providers.add(report.provider)
        object.__setattr__(
            self,
            "eligibility_reports",
            tuple(sorted(reports, key=lambda report: report.provider)),
        )
        cells = _v_tuple_of(
            self.compatibility_cells,
            CompatibilityCell,
            "route_request.compatibility_cells",
        )
        seen_cells: set[tuple[str, str, str, str | None, str]] = set()
        for cell in cells:
            key = (cell.channel, cell.provider, cell.model, cell.variant, cell.feature)
            if key in seen_cells:
                raise RouteContractValidationError(
                    "route_request.compatibility_cells: duplicate cell for "
                    + f"{key}; at most one current cell per key is permitted"
                )
            seen_cells.add(key)
        object.__setattr__(
            self,
            "compatibility_cells",
            tuple(
                sorted(
                    cells,
                    key=lambda cell: (
                        cell.channel,
                        cell.provider,
                        cell.model,
                        cell.variant or "",
                        cell.feature,
                    ),
                )
            ),
        )
        _ = _v_instance_of(
            self.admin_constraints,
            AdministratorConstraints,
            "route_request.admin_constraints",
        )
        _ = _v_instance_of(
            self.client_authorization,
            ClientAuthorization,
            "route_request.client_authorization",
        )
        if self.routing_profile is not None:
            _ = _v_instance_of(
                self.routing_profile,
                ClientRoutingProfile,
                "route_request.routing_profile",
            )
        _ = _v_instance_of(self.request, RequestBinding, "route_request.request")
        if self.request.profile_alias is not None and self.routing_profile is None:
            raise RouteContractValidationError(
                "route_request: a profile_alias requires the configured "
                + "routing profile that resolves it"
            )
        if self.profile_policy_version is not None:
            _ = _v_int(
                self.profile_policy_version,
                "route_request.profile_policy_version",
                lo=1,
            )
        generated_at = _parse_canonical_ts(
            self.registry_snapshot.generated_at,
            "route_request.registry_snapshot.generated_at",
        )
        if self.evaluated_at < generated_at:
            raise RouteContractValidationError(
                "route_request: evaluated_at "
                + f"{canonical_instant(self.evaluated_at)} precedes the "
                + "registry snapshot's generated_at "
                + f"{self.registry_snapshot.generated_at!r}; a decision is "
                + "never evaluated before its own state snapshot"
            )


# ── Requirement resolution (precedence layers 3 and 4) ───────────────────────


def _resolve_requirement(
    request: RouteRequest,
) -> tuple[TaskRequirement, str | None]:
    """Resolve the merged task requirement and the profile provenance.

    The configured routing profile resolves through the existing
    ``TaskProfileCatalog.resolve`` (an unknown profile raises the
    resolver's existing typed error — never a fallback) and supplies the
    monotone base. The request's structural constraints merge through
    ``tighten_requirement`` — the request may tighten but never loosen the
    profile. Unprofiled requests resolve to the minimal honest requirement
    (``L0``, no fabricated minima) plus their structural constraints.
    """
    profile = request.routing_profile
    if profile is not None:
        base = request.profiles.resolve(profile.profile_id)
        profile_id: str | None = profile.profile_id
    else:
        base = TaskRequirement(
            task_level="L0",
            capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(),
        )
        profile_id = None
    structural = TaskRequirement(
        task_level=base.task_level,
        capability_minima=CapabilityMinima(),
        hard_constraints=HardConstraints(
            requires_tool_use=request.request.requires_tool_calls,
            requires_reasoning_mode=request.request.requires_reasoning_controls,
            minimum_input_context_tokens=request.request.minimum_input_context_tokens,
            minimum_output_tokens=request.request.maximum_output_tokens,
        ),
    )
    return tighten_requirement(base, structural), profile_id


# ── Route decision assembly ──────────────────────────────────────────────────


def _route_target(
    gate: _ResourceGate,
    identity: ModelIdentity,
    routing_profile: str | None,
    shares_with_selected: bool,
) -> RouteTarget:
    resource = gate.entry.identity
    return RouteTarget(
        resource=resource,
        model=identity,
        quota_pools=resource.quota_pool_ids,
        routing_profile=routing_profile,
        promotion_sources=gate.promotion_sources,
        shares_quota_pool_with_selected=shares_with_selected,
    )


def _target_exclusion(gate: _ResourceGate) -> TargetExclusion:
    entry = gate.entry
    identity = entry.identity
    return TargetExclusion(
        resource_id=identity.resource_id,
        stage=cast(str, gate.stage),
        reason_codes=gate.reason_codes,
        channel=identity.channel,
        provider=identity.provider,
        model=identity.model,
        health_status=gate.health_status,
        freshness=entry.freshness,
        compatibility_feature=gate.compatibility_feature,
        compatibility_value=gate.compatibility_value,
        execution_eligibility=gate.eligibility,
    )


def _order_key(gate: _ResourceGate) -> tuple[int, str]:
    """Target ordering within one model identity: promotion preference first.

    A target preferred by an active promotion ranks ahead of one that is
    not (a routing preference only — never an eligibility bypass); the
    stable registry identifier is the deterministic tie-break. No other
    preference exists at the target level and no hidden penalty is added.
    """
    preference = 0 if gate.promotion_sources else 1
    return (preference, gate.entry.identity.resource_id)


def _narrowed_catalog(
    request: RouteRequest, routable_keys: set[tuple[str, str, str]]
) -> ModelCatalog:
    """The narrowed catalog view: the same versioned artifact, fewer entries.

    The catalog versions are preserved so the embedded selection's
    provenance stays exact; identities removed by target narrowing are
    reported as ``unroutable_identities`` on the route decision. An empty
    view is valid and yields the selector's explicit no-solution result.
    """
    return ModelCatalog(
        catalog_version=request.catalog.catalog_version,
        updated_on=request.catalog.updated_on,
        entries=tuple(
            sorted(
                (
                    entry
                    for entry in request.catalog.entries
                    if _identity_key(entry.identity) in routable_keys
                ),
                key=lambda entry: _identity_key(entry.identity),
            )
        ),
    )


def _run_selection(
    request: RouteRequest,
    catalog: ModelCatalog,
    requirement: TaskRequirement,
    profile_id: str | None,
) -> SelectionDecision:
    """Run the UNMODIFIED balanced selector over the narrowed catalog view."""
    return select_model(
        catalog=catalog,
        requirement=requirement,
        policy=request.policy,
        snapshots=request.capacity_snapshots,
        evaluated_at=request.evaluated_at,
        replenishment_states=request.replenishment_states,
        eligibility_reports=request.eligibility_reports,
        profile_id=profile_id,
        profile_policy_version=(
            request.profile_policy_version
            if request.routing_profile is not None
            else None
        ),
    )


def _expired_promotion_sources(
    gates: list[_ResourceGate], evaluated_at: datetime
) -> tuple[str, ...]:
    """The sorted unique expired promotion sources across all resources."""
    sources: set[str] = set()
    for gate in gates:
        _, expired = _promotion_preference(gate.entry, evaluated_at)
        sources.update(expired)
    return tuple(sorted(sources))


def _no_solution_decision(
    request: RouteRequest,
    selection: SelectionDecision,
    target_exclusions: tuple[TargetExclusion, ...],
    unroutable_keys: tuple[tuple[str, str, str], ...],
    expired_promotions: tuple[str, ...],
    reason_codes: tuple[str, ...],
    pinned: bool,
) -> RouteDecision:
    """Assemble the explicit no-solution route decision.

    A no-solution result is a legitimate, fully explained outcome: the
    embedded selection carries the model-level exclusions and closest
    candidates, the target exclusions carry every failed resource gate,
    and the route reason codes carry the pin state. Requirements are never
    relaxed and no candidate is silently substituted.
    """
    return RouteDecision(
        status=ROUTE_STATUS_NO_SOLUTION,
        selection=selection,
        registry_revision=request.registry_snapshot.revision,
        registry_generated_at=request.registry_snapshot.generated_at,
        unroutable_identities=tuple(
            ModelIdentity(provider=key[0], model=key[1], variant=key[2])
            for key in unroutable_keys
        ),
        target_exclusions=target_exclusions,
        expired_promotions=expired_promotions,
        pinned=pinned,
        reason_codes=reason_codes,
    )


def route_request(request: RouteRequest) -> RouteDecision:
    """The authoritative deterministic route decision for executable targets.

    Pipeline: resolve the merged requirement (profile base, request
    tightening) -> evaluate every registered resource through the frozen
    gate pipeline -> narrow the catalog view to identities with at least
    one qualified resource (pins narrow further, honored or explicitly
    failed) -> run the UNMODIFIED balanced selector over the narrowed view
    -> bind the selected identity to its qualified resources. Deterministic
    over the request's inputs and evaluation instant: identical inputs
    produce identical decisions and identical ``decision_id`` values.
    """
    requirement, profile_id = _resolve_requirement(request)
    effective = _effective_authorization(
        request.admin_constraints,
        request.client_authorization,
        request.routing_profile,
    )
    gates = sorted(
        (
            _evaluate_resource(
                entry,
                request.catalog,
                effective,
                request.request,
                request.compatibility_cells,
                requirement,
                request.eligibility_reports,
                request.evaluated_at,
            )
            for entry in request.registry_snapshot.entries
        ),
        key=lambda gate: gate.entry.identity.resource_id,
    )

    qualified_by_identity: dict[tuple[str, str, str], list[_ResourceGate]] = {}
    for gate in gates:
        if not gate.qualified:
            continue
        for identity in gate.bound_identities:
            qualified_by_identity.setdefault(_identity_key(identity), []).append(gate)

    pin = request.request.pinned_target
    pin_model = request.request.explicit_model
    pin_variant = request.request.explicit_variant
    has_pin = pin is not None or pin_model is not None or pin_variant is not None

    codes_extra: set[str] = set()
    routable_keys: set[tuple[str, str, str]] = set(qualified_by_identity)
    pinned_gate: _ResourceGate | None = None
    if pin is not None:
        pinned_gate = next(
            (
                gate
                for gate in gates
                if gate.entry.identity.resource_id == pin.resource_id
            ),
            None,
        )
        if pinned_gate is None:
            codes_extra.update({"pinned_request_failed", "pin_target_not_found"})
            routable_keys = set()
        elif not pinned_gate.qualified:
            codes_extra.add("pinned_request_failed")
            routable_keys = set()
        else:
            routable_keys = {
                _identity_key(identity) for identity in pinned_gate.bound_identities
            }

    if pin_model is not None or pin_variant is not None:
        pinned_keys = {
            key
            for key in routable_keys
            if (
                pin_model is None
                or (key[0], key[1]) == (pin_model.provider, pin_model.model)
            )
            and (pin_variant is None or key[2] == pin_variant)
        }
        if not pinned_keys:
            codes_extra.add("pinned_request_failed")
            if pin_model is not None and not any(
                (entry.identity.provider, entry.identity.model)
                == (pin_model.provider, pin_model.model)
                for entry in request.catalog.entries
            ):
                codes_extra.add("pin_model_not_found")
            routable_keys = set()
        else:
            routable_keys = pinned_keys

    selection = _run_selection(
        request, _narrowed_catalog(request, routable_keys), requirement, profile_id
    )
    target_exclusions = tuple(
        _target_exclusion(gate) for gate in gates if not gate.qualified
    )
    all_keys = {_identity_key(entry.identity) for entry in request.catalog.entries}
    unroutable_keys = tuple(sorted(all_keys - routable_keys))
    expired_promotions = _expired_promotion_sources(gates, request.evaluated_at)

    if selection.selected is None:
        if has_pin:
            codes_extra.add("pinned_request_failed")
        return _no_solution_decision(
            request=request,
            selection=selection,
            target_exclusions=target_exclusions,
            unroutable_keys=unroutable_keys,
            expired_promotions=expired_promotions,
            reason_codes=tuple(sorted({"no_eligible_target"} | codes_extra)),
            pinned=has_pin,
        )

    selected_identity = selection.selected.identity
    selected_gates = sorted(
        qualified_by_identity[_identity_key(selected_identity)], key=_order_key
    )
    if pinned_gate is not None:
        # A pinned target is honored exactly: the pinned resource is the
        # target and there are no alternatives — never a competitive pick
        # among surfaces for the pinned model.
        pinned_resource_id = pinned_gate.entry.identity.resource_id
        selected_gate = next(
            (
                gate
                for gate in selected_gates
                if gate.entry.identity.resource_id == pinned_resource_id
            ),
            None,
        )
        if selected_gate is None:  # pragma: no cover - pin narrowing guarantees it
            raise RouteContractValidationError(
                "route_request: the qualified pinned target vanished from "
                + "its own bound identity's candidate set"
            )
        target_alternatives: tuple[RouteTarget, ...] = ()
    else:
        selected_gate = selected_gates[0]
        selected_pools = set(selected_gate.entry.identity.quota_pool_ids)
        target_alternatives = tuple(
            _route_target(
                gate,
                selected_identity,
                profile_id,
                shares_with_selected=bool(
                    set(gate.entry.identity.quota_pool_ids) & selected_pools
                ),
            )
            for gate in selected_gates[1:]
        )
    target = _route_target(selected_gate, selected_identity, profile_id, False)
    return RouteDecision(
        status=ROUTE_STATUS_SELECTED,
        selection=selection,
        registry_revision=request.registry_snapshot.revision,
        registry_generated_at=request.registry_snapshot.generated_at,
        target=target,
        target_alternatives=target_alternatives,
        unroutable_identities=tuple(
            ModelIdentity(provider=key[0], model=key[1], variant=key[2])
            for key in unroutable_keys
        ),
        target_exclusions=target_exclusions,
        expired_promotions=expired_promotions,
        pinned=has_pin,
        reason_codes=("route_selected",),
    )


# ── Recommendation-to-execution binding (admission only) ─────────────────────


def admit_pinned_target(
    request: RouteRequest,
    *,
    resource_id: str,
    decision_id: str | None = None,
) -> AdmissionDecision:
    """Admit one pinned executable target — admission only, never re-ranking.

    The recommendation-to-execution binding of D-042: a client that first
    used ``select`` pins the decision's executable-target reference (and
    may carry its ``decision_id`` for audit provenance); the gateway runs
    admission — authorization, limits, availability, compatibility — and
    dispatches. This function never re-runs competitive ranking, so no
    unexpected second routing decision can occur: the named resource is
    evaluated alone against the current inputs, and a resource that is not
    the ranking winner is still approved when it passes every gate. A
    recommendation is not a reservation: admission re-checks current state
    and may reject explicitly.

    For a resource without a variant qualifier that binds several
    calibrated variants of one model, the admitted target's ``model``
    dimension is the canonically first bound identity unless the request
    pins an explicit variant; the gateway should carry the variant pin for
    unambiguous dispatch.
    """
    _ = _v_safe_id(resource_id, "admit_pinned_target.resource_id")
    if decision_id is not None:
        _ = _v_safe_id(decision_id, "admit_pinned_target.decision_id")
    requirement, profile_id = _resolve_requirement(request)
    effective = _effective_authorization(
        request.admin_constraints,
        request.client_authorization,
        request.routing_profile,
    )
    entry = next(
        (
            registry_entry
            for registry_entry in request.registry_snapshot.entries
            if registry_entry.identity.resource_id == resource_id
        ),
        None,
    )
    if entry is None:
        return AdmissionDecision(
            resource_id=resource_id,
            approved=False,
            reason_codes=("admission_rejected", "pin_target_not_found"),
            bound_decision_id=decision_id,
        )
    gate = _evaluate_resource(
        entry,
        request.catalog,
        effective,
        request.request,
        request.compatibility_cells,
        requirement,
        request.eligibility_reports,
        request.evaluated_at,
    )
    if not gate.qualified or not gate.bound_identities:
        return AdmissionDecision(
            resource_id=resource_id,
            approved=False,
            reason_codes=("admission_rejected", *gate.reason_codes),
            exclusion=_target_exclusion(gate),
            bound_decision_id=decision_id,
        )
    pinned_variant = request.request.explicit_variant
    identity = next(
        (
            bound
            for bound in gate.bound_identities
            if pinned_variant is None or bound.variant == pinned_variant
        ),
        gate.bound_identities[0],
    )
    return AdmissionDecision(
        resource_id=resource_id,
        approved=True,
        reason_codes=("admission_approved",),
        target=_route_target(gate, identity, profile_id, False),
        bound_decision_id=decision_id,
    )


__all__ = [
    "ADMISSION_REASON_CODES",
    "COMPAT_CELL_VALUES",
    "COMPAT_FEATURES",
    "METERED_ENTITLEMENTS",
    "ROUTE_DECISION_SCHEMA_VERSION",
    "ROUTE_EXCLUSION_STAGES",
    "ROUTE_REASON_CODES",
    "ROUTE_STATUSES",
    "ROUTE_STATUS_NO_SOLUTION",
    "ROUTE_STATUS_SELECTED",
    "TARGET_EXCLUSION_REASON_CODES",
    "AdministratorConstraints",
    "AdmissionDecision",
    "ClientAuthorization",
    "ClientRoutingProfile",
    "CompatibilityCell",
    "PinnedTarget",
    "RequestBinding",
    "RouteDecision",
    "RouteRequest",
    "RouteTarget",
    "SpendingLimit",
    "TargetExclusion",
    "admit_pinned_target",
    "route_request",
]
