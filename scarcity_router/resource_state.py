"""Versioned resource-state contract, registry and shared normalization (M01).

Pure, provider-independent, standard-library only. Issue #86 (M01) under the
A0 execution-gateway architecture (D-040 through D-045).

This module is the new versioned resource-state snapshot contract that is a
SIBLING to capacity contract v3 (``docs/capacity-model.md``, preserved and
unchanged). Capacity v3 answers "how much quota does one provider observation
report"; this contract answers "what is the full observable state of one
executable resource" — an HTTP provider, a worker-backed provider, a local
Ollama instance or a CLI/app adapter surface — so the later routing core
(M02) never has to guess:

- identity: execution channel/surface, backend provider, physical model and
  variant, entitlement class, confirmed quota-pool membership;
- execution capabilities and monetary cost as administrator-CONFIGURED
  facts (never ratings, never telemetry-derived; the evidence-based OpenAI
  compatibility matrix remains an M03-owned contract and is not represented
  here). They live on the administrator registration and are composed into
  registry reads; observations deliberately do not carry them, so there is
  exactly one canonical value per resource and no silent observation
  override. Observed (as opposed to configured) capability/cost facts
  would require an explicit future contract extension;
- health with the same six-status vocabulary as capacity v3 (1:1 mapping,
  zero information loss) plus its allowlisted diagnostics;
- freshness and bounded polling/cache metadata — this is the U-003
  resolution (D-041 assigns it to M01): the administrator registration
  carries an explicit positive ``freshness_ttl_seconds`` and an optional
  ``poll_interval_seconds``, and that server-side policy is AUTHORITATIVE.
  Observations carry no policy fields at all, so an accepted observation —
  including a worker-reported one — can never enlarge its own freshness
  window or change its polling cadence. The registry classifies each
  resource as ``fresh``, ``stale`` or ``never_observed`` against an
  injected instant, and a future-dated observation is rejected rather than
  treated as fresh (producing server-comparable observation times is the
  reporting side's responsibility, M05; no clock-skew tolerance protocol is
  invented here). There is no background refresh; the server's request loop
  (M03) calls :meth:`ResourceRegistry.refresh_due`;
- promotions as separate observations (source, observation time,
  execution-channel/provider/model/plan scopes, validity period, timezone).
  A promotion record never asserts that an execution qualifies for it;
  D-039 eligibility gating remains the proof path and is deliberately NOT
  embedded here (two contract versions in one document is exactly the shape
  D-039 rejected for capacity v4);
- monetary cost facts are registration-owned configured data, never
  duplicated onto observations;
- every quota fact as an existing capacity v3 ``CapacityWindow`` paired with
  an observation class: ``direct_observation``, ``provider_telemetry``,
  ``estimate``, ``local_limit`` or ``unknown`` (D-042). Tokens reported by
  an adapter are never equated with subscription quota percentages, and the
  router never assumes it observes account usage happening outside it.

Identity discipline (D-042): the physical model/variant, the execution
channel/surface, the entitlement, the quota pool and the client-visible
routing profile are five separate concepts and are never collapsed. This
contract represents the first four as explicit fields; client-visible
routing profiles are a routing-core (M02) concept and are absent here by
design. The same subscription discovered through Desktop, CLI, Windows or
WSL does not become multiple resources or pools by itself, and pool sharing
is never assumed in either direction: quota pools exist only as explicit
confirmed ``quota_pool_ids``.

Worker reports: when telemetry is only available locally, the native worker
(M05) reports :class:`ResourceStateSnapshot` observation documents produced
by this same module — one shared normalization, never a second collector
implementation for the same provider/account. :class:`WorkerStateReport` is
the M01 contract boundary for that input; the M05 transport, pairing and
authentication wrap it. Reports are validated fail-closed: wrong report
version, malformed entries, server-direct channels, duplicate resources or
future observation times reject the whole report; nothing is merged
silently. Because observation documents carry no policy fields, a worker
cannot enlarge the freshness window, change the polling cadence or override
the configured capabilities/cost of the resource it reports for — the
server's administrator registration stays authoritative.

Security: snapshots never contain secrets, account identifiers or raw
provider payloads. Every identifier is a safe opaque ASCII token validated
against the same restricted alphabet as the capacity contract; free-text,
credential-shaped or provider-payload values cannot be represented. The
informational capacity ``plan`` is not carried: plan labels are scope
inputs for promotions and display, not resource identity.

State behavior: the registry is deliberately IN-MEMORY. Durable server
state (the SQLite-class store) is D-041/M03/M09 scope and is not introduced
here; a restart reconstructs the registry from administrator resource
configuration and fresh collection. No external database, cache or
message-queue service exists at this boundary.

Invariants are enforced at construction (mirroring ``capacity.py`` and
``eligibility.py``): closed vocabularies, canonical timestamps, safe
identifiers, positive bounded TTL/polling values, and deterministically
ordered collections (unordered arrays serialize canonically or the
construction is rejected, like ``ExecutionEligibility.reason_codes``).
Serialization omits unknown optional values (never ``null``) except where
an explicit null is the contract's representation of a meaningful absent
state (``ResourceRegistryEntry.observation`` and
``ModelCatalogEntry.capacity_bindings`` share that rule).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar, cast

from .capacity import CapacityDiagnostic, CapacitySnapshot, CapacityWindow
from .errors import CapacityValidationError

# ── Frozen value sets ─────────────────────────────────────────────────────────

RESOURCE_STATE_SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1
WORKER_REPORT_SCHEMA_VERSION = 1

# D-042 execution channel/surface: exactly the three A0 members. The
# resource's channel is a fact of how it is reached, never inferred from a
# model name.
EXECUTION_CHANNELS: frozenset[str] = frozenset({
    "server_direct_http",  # server-direct OpenAI-compatible HTTP adapter (M04)
    "worker_bridged",  # OpenAI-compatible HTTP bridged by a native worker (M05)
    "local_app_adapter",  # local CLI/app adapter surface (M06 Codex, M07 ZCode)
})

# A worker can only report resources it actually reaches locally. A
# server-direct HTTP resource is observed by the server itself, never by a
# worker report.
WORKER_REPORTABLE_CHANNELS: frozenset[str] = frozenset({
    "worker_bridged",
    "local_app_adapter",
})

# D-042 entitlement classes plus the explicit unknown state. Entitlement is
# per-resource state, never implied by a model name; unknown stays unknown.
ENTITLEMENT_CLASSES: frozenset[str] = frozenset({
    "subscription_included",
    "promotional",
    "payg_metered",
    "prepaid_credits",
    "local_ungated",
    "unknown",
})

# D-042 observation classes carried by every quota/cost fact
# (docs/capacity-model.md, "Executable resources, observation classes and
# quota pools").
OBSERVATION_CLASSES: frozenset[str] = frozenset({
    "direct_observation",
    "provider_telemetry",
    "estimate",
    "local_limit",
    "unknown",
})

# Health status uses the capacity v3 status vocabulary unchanged: the
# normalization from a ``CapacitySnapshot`` is the identity mapping, so
# provider telemetry semantics (including fail-closed diagnostics) are
# preserved without translation. Staleness is deliberately NOT a stored
# health status — it is evaluated freshness (``FRESHNESS_STATES``) against
# the snapshot's explicit TTL. A zero/exhausted quota window is likewise
# never a health state: capacity v3's ``remaining_percent == 0`` inside a
# ``status: "ok"`` observation keeps the resource healthy.
RESOURCE_HEALTH_STATUSES: frozenset[str] = frozenset({
    "ok",
    "unavailable",
    "auth_required",
    "unsupported",
    "schema_changed",
    "unknown",
})

_HEALTH_REQUIRED_DIAGNOSTIC: dict[str, str] = {
    "unavailable": "source_unavailable",
    "auth_required": "auth_required",
    "unsupported": "unsupported_source",
    "schema_changed": "schema_changed",
    "unknown": "telemetry_unknown",
}

FRESHNESS_STATES: frozenset[str] = frozenset({
    "fresh",
    "stale",
    "never_observed",
})

# ── Validators (single source of truth for the resource-state rules) ─────────

_CANONICAL_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


def _v_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise CapacityValidationError(
            f"{field_name}: expected str, got {type(value).__name__}"
        )
    return value


def _v_safe_id(value: object, field_name: str) -> str:
    s = _v_str(value, field_name)
    if not _SAFE_ID_RE.match(s):
        raise CapacityValidationError(
            f"{field_name}: unsafe identifier {s!r}; "
            + "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
        )
    return s


def _v_enum(value: object, allowed: frozenset[str], field_name: str) -> str:
    s = _v_str(value, field_name)
    if s not in allowed:
        raise CapacityValidationError(
            f"{field_name}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def _v_ts(value: object, field_name: str) -> str:
    s = _v_str(value, field_name)
    if not _CANONICAL_TS_RE.match(s):
        raise CapacityValidationError(
            f"{field_name}: non-canonical timestamp {s!r}; "
            + "expected YYYY-MM-DDTHH:MM:SS.sssZ"
        )
    try:
        _ = datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError:
        raise CapacityValidationError(f"{field_name}: invalid date/time in {s!r}")
    return s


def _v_opt_safe_id(value: object | None, field_name: str) -> str | None:
    return None if value is None else _v_safe_id(value, field_name)


def _v_opt_ts(value: object | None, field_name: str) -> str | None:
    return None if value is None else _v_ts(value, field_name)


def _v_int(
    value: object,
    field_name: str,
    *,
    lo: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapacityValidationError(
            f"{field_name}: expected int, got {type(value).__name__} ({value!r})"
        )
    if lo is not None and value < lo:
        raise CapacityValidationError(f"{field_name}: value {value} < {lo}")
    return value


def _v_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CapacityValidationError(
            f"{field_name}: expected bool, got {type(value).__name__}"
        )
    return value


def _v_opt_bool(value: object | None, field_name: str) -> bool | None:
    return None if value is None else _v_bool(value, field_name)


def _v_opt_int(
    value: object | None,
    field_name: str,
    *,
    lo: int | None = None,
) -> int | None:
    return None if value is None else _v_int(value, field_name, lo=lo)


def _ts_to_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:  # pragma: no cover - regex guarantees awareness
        raise CapacityValidationError(f"timestamp {value!r} is not timezone-aware")
    return parsed


def _age_seconds(later: str, earlier: str) -> float:
    return (_ts_to_datetime(later) - _ts_to_datetime(earlier)).total_seconds()


def _age_or_reject(later: str, earlier: str, *, context: str) -> float:
    """Age of ``earlier`` at instant ``later``; fail closed on future facts.

    The one shared time seam for every rule that evaluates a timestamp
    against an authoritative instant: a fact dated after its evaluation
    instant is rejected, never silently evaluated with a negative age.
    (Comparing two reported times to each other — a promotion's validity
    bounds or a worker report's observed/reported pair — is a different
    rule and deliberately does not go through here.) No clock-skew
    tolerance exists at this boundary; producing server-comparable
    observation times is the reporting side's responsibility (M05).
    """
    age = _age_seconds(later, earlier)
    if age < 0:
        raise CapacityValidationError(
            f"{context}: {earlier!r} is after the evaluation instant "
            + f"{later!r}; a future-dated fact is rejected instead of being "
            + "silently evaluated"
        )
    return age


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
    """Validate the serialized shape of one record and narrow it to a mapping.

    Verifies the value is a mapping, rejects unknown keys, and reports any
    missing required keys as :class:`CapacityValidationError`. Semantics of
    the field values are NOT checked here; the ``_v_*`` validators and
    ``__post_init__`` own those rules.
    """
    m = _as_str_object_mapping(obj)
    if m is None:
        raise CapacityValidationError(
            f"{label}: expected a dict-like mapping, got {type(obj).__name__}"
        )
    allowed = frozenset(required) | frozenset(optional)
    extra = set(m.keys()) - allowed
    if extra:
        raise CapacityValidationError(f"{label}: unknown keys {sorted(extra)}")
    missing = [key for key in required if key not in m]
    if missing:
        raise CapacityValidationError(f"{label}: missing required keys {sorted(missing)}")
    return m


def _v_instance_of(value: object, item_type: type, label: str) -> None:
    if not isinstance(value, item_type):
        raise CapacityValidationError(
            f"{label}: expected {item_type.__name__}, got {type(value).__name__}"
        )


def _v_tuple_of(value: object, item_type: type, label: str) -> None:
    if not isinstance(value, tuple):
        raise CapacityValidationError(
            f"{label}: expected tuple, got {type(value).__name__}"
        )
    for item in cast("tuple[object, ...]", value):
        _v_instance_of(item, item_type, label)


def _v_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if list(values) != sorted(values):
        raise CapacityValidationError(f"{label}: must be deterministically sorted")
    if len(set(values)) != len(values):
        raise CapacityValidationError(f"{label}: duplicate values are not permitted")


# ── Canonical ordering keys ───────────────────────────────────────────────────

_DIAGNOSTIC_SORT_KEY_ORDER = {"tokens": 0, "time": 1, "unknown": 2}
_WINDOW_KIND_ORDER = {"five_hour": 0, "weekly": 1, "unknown": 2}


def _quota_fact_sort_key(fact: "QuotaFact") -> tuple[object, ...]:
    window = fact.window
    return (
        fact.observation_class,
        _DIAGNOSTIC_SORT_KEY_ORDER.get(window.resource, len(_DIAGNOSTIC_SORT_KEY_ORDER)),
        window.resource,
        _WINDOW_KIND_ORDER.get(window.kind, len(_WINDOW_KIND_ORDER)),
        window.kind,
        window.scope_id or "",
        window.window_id or "",
        window.duration_seconds if window.duration_seconds is not None else -1,
        window.resets_at or "",
        window.used_percent if window.used_percent is not None else -1,
        window.remaining_percent if window.remaining_percent is not None else -1,
    )


def _promotion_sort_key(promotion: "PromotionObservation") -> tuple[str, ...]:
    return (
        promotion.source,
        promotion.observed_at,
        promotion.channel or "",
        promotion.provider or "",
        promotion.model or "",
        promotion.plan or "",
        promotion.valid_from or "",
        promotion.valid_until or "",
        promotion.timezone or "",
    )


def _diagnostic_sort_key(diagnostic: CapacityDiagnostic) -> tuple[str, str]:
    return diagnostic.code, diagnostic.window_id or ""


# ── Quota and cost facts ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuotaFact:
    """One quota/limit fact: a capacity v3 window plus its observation class.

    The window is the existing, unchanged ``CapacityWindow`` — provider
    telemetry normalization is reused verbatim, never duplicated. The
    observation class (D-042) says where the fact came from: a collector
    window is ``provider_telemetry``, a worker-enforced local bound is
    ``local_limit``, a router-measured value is ``direct_observation``, a
    modeled value is ``estimate``. ``unknown`` is the honest class when only
    the fact's existence is known. A fact's class is never upgraded without
    evidence: an estimate is never serialized as telemetry.
    """

    observation_class: str
    window: CapacityWindow

    _REQUIRED: ClassVar[tuple[str, ...]] = ("observation_class", "window")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_enum(self.observation_class, OBSERVATION_CLASSES, "quota_fact.observation_class")
        _v_instance_of(self.window, CapacityWindow, "quota_fact.window")

    @classmethod
    def from_dict(cls, d: object) -> "QuotaFact":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "quota_fact")
        return cls(
            observation_class=_v_enum(
                dd["observation_class"], OBSERVATION_CLASSES, "quota_fact.observation_class"
            ),
            window=CapacityWindow.from_dict(dd["window"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_class": self.observation_class,
            "window": self.window.to_dict(),
        }


@dataclass(frozen=True)
class ResourceCost:
    """Monetary cost facts for one resource, in fixed-point micro-USD.

    Amounts are integer micro-USD per million tokens (``$2.00/Mtok`` is
    ``2000000``), so serialization stays deterministic without floats. The
    observation class follows D-042's shared quota/cost vocabulary: a
    provider-published price is ``provider_telemetry``, a modeled price is
    ``estimate``, a router-measured realized cost is ``direct_observation``
    (M03 accounting), and ``unknown`` is the honest class for placeholder
    facts. Unknown amounts are omitted, never zero — zero is a known free
    cost (``local_ungated``), not a missing number.
    """

    observation_class: str
    input_micro_usd_per_mtoken: int | None = None
    output_micro_usd_per_mtoken: int | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("observation_class",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "input_micro_usd_per_mtoken",
        "output_micro_usd_per_mtoken",
    )

    def __post_init__(self) -> None:
        _ = _v_enum(self.observation_class, OBSERVATION_CLASSES, "cost.observation_class")
        _ = _v_opt_int(self.input_micro_usd_per_mtoken, "cost.input_micro_usd_per_mtoken", lo=0)
        _ = _v_opt_int(self.output_micro_usd_per_mtoken, "cost.output_micro_usd_per_mtoken", lo=0)
        if (
            self.input_micro_usd_per_mtoken is None
            and self.output_micro_usd_per_mtoken is None
        ):
            raise CapacityValidationError(
                "cost: at least one amount must be present; omit the whole cost "
                + "record when no amount is known"
            )

    @classmethod
    def from_dict(cls, d: object) -> "ResourceCost":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "cost")
        return cls(
            observation_class=_v_enum(
                dd["observation_class"], OBSERVATION_CLASSES, "cost.observation_class"
            ),
            input_micro_usd_per_mtoken=_v_opt_int(
                dd.get("input_micro_usd_per_mtoken"),
                "cost.input_micro_usd_per_mtoken",
                lo=0,
            ),
            output_micro_usd_per_mtoken=_v_opt_int(
                dd.get("output_micro_usd_per_mtoken"),
                "cost.output_micro_usd_per_mtoken",
                lo=0,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"observation_class": self.observation_class}
        if self.input_micro_usd_per_mtoken is not None:
            out["input_micro_usd_per_mtoken"] = self.input_micro_usd_per_mtoken
        if self.output_micro_usd_per_mtoken is not None:
            out["output_micro_usd_per_mtoken"] = self.output_micro_usd_per_mtoken
        return out


# ── Promotions ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromotionObservation:
    """One promotion observation; never proof that an execution qualifies.

    Recorded with its source, observation time, execution-channel scope,
    provider/model/plan scopes, validity period and timezone, exactly as the
    A0 contract map requires. A routing preference based on this record is a
    routing-core (M02) concern; whether a specific execution actually
    qualifies is decided by the D-039 eligibility proof path, not here. Any
    scope that is not evidenced is omitted (unknown) — a missing scope never
    means "applies everywhere".
    """

    source: str
    observed_at: str
    channel: str | None = None
    provider: str | None = None
    model: str | None = None
    plan: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    timezone: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("source", "observed_at")
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "channel",
        "provider",
        "model",
        "plan",
        "valid_from",
        "valid_until",
        "timezone",
    )

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.source, "promotion.source")
        _ = _v_ts(self.observed_at, "promotion.observed_at")
        if self.channel is not None:
            _ = _v_enum(self.channel, EXECUTION_CHANNELS, "promotion.channel")
        _ = _v_opt_safe_id(self.provider, "promotion.provider")
        _ = _v_opt_safe_id(self.model, "promotion.model")
        _ = _v_opt_safe_id(self.plan, "promotion.plan")
        _ = _v_opt_ts(self.valid_from, "promotion.valid_from")
        _ = _v_opt_ts(self.valid_until, "promotion.valid_until")
        _ = _v_opt_safe_id(self.timezone, "promotion.timezone")
        if self.valid_from is not None and self.valid_until is not None:
            if _age_seconds(self.valid_until, self.valid_from) < 0:
                raise CapacityValidationError(
                    "promotion: valid_from must not be after valid_until"
                )

    @classmethod
    def from_dict(cls, d: object) -> "PromotionObservation":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "promotion")
        return cls(
            source=_v_safe_id(dd["source"], "promotion.source"),
            observed_at=_v_ts(dd["observed_at"], "promotion.observed_at"),
            channel=(
                None
                if dd.get("channel") is None
                else _v_enum(dd.get("channel"), EXECUTION_CHANNELS, "promotion.channel")
            ),
            provider=_v_opt_safe_id(dd.get("provider"), "promotion.provider"),
            model=_v_opt_safe_id(dd.get("model"), "promotion.model"),
            plan=_v_opt_safe_id(dd.get("plan"), "promotion.plan"),
            valid_from=_v_opt_ts(dd.get("valid_from"), "promotion.valid_from"),
            valid_until=_v_opt_ts(dd.get("valid_until"), "promotion.valid_until"),
            timezone=_v_opt_safe_id(dd.get("timezone"), "promotion.timezone"),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "source": self.source,
            "observed_at": self.observed_at,
        }
        if self.channel is not None:
            out["channel"] = self.channel
        if self.provider is not None:
            out["provider"] = self.provider
        if self.model is not None:
            out["model"] = self.model
        if self.plan is not None:
            out["plan"] = self.plan
        if self.valid_from is not None:
            out["valid_from"] = self.valid_from
        if self.valid_until is not None:
            out["valid_until"] = self.valid_until
        if self.timezone is not None:
            out["timezone"] = self.timezone
        return out


# ── Resource identity, capabilities, health ───────────────────────────────────


@dataclass(frozen=True)
class ResourceIdentity:
    """Identity of one executable resource (D-042 target dimensions).

    Five concepts stay separate and are never collapsed:

    - ``resource_id``: stable opaque registry identifier of THIS executable
      resource (one surface, one entitlement);
    - ``channel``: the execution channel/surface (server-direct HTTP,
      worker-bridged, local app adapter);
    - ``provider``/``model``/``variant``: the physical backend and
      model/variant it executes — deliberately separate from the catalog's
      calibrated ``ModelIdentity`` (an invocation configuration), which a
      later binding step (M02) may relate to it;
    - ``entitlement``: what billing class this surface draws on;
    - ``quota_pool_ids``: the CONFIRMED shared quota pools this resource
      draws from. Absent pools mean "no confirmed sharing is recorded" —
      never "independently metered" and never "shares the pool of any
      similar-looking resource". Pools come from verified evidence or
      administrator configuration only; they are never inferred from
      provider, account, host or model similarity.

    ``variant is None`` means no variant qualifier is configured for this
    resource (distinct from any variant value). The empty tuple means no
    confirmed pool; unknown sharing is simply never asserted.
    """

    resource_id: str
    channel: str
    provider: str
    model: str
    entitlement: str
    variant: str | None = None
    quota_pool_ids: tuple[str, ...] = ()

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "resource_id",
        "channel",
        "provider",
        "model",
        "entitlement",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("variant", "quota_pool_ids")

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.resource_id, "resource_identity.resource_id")
        _ = _v_enum(self.channel, EXECUTION_CHANNELS, "resource_identity.channel")
        _ = _v_safe_id(self.provider, "resource_identity.provider")
        _ = _v_safe_id(self.model, "resource_identity.model")
        _ = _v_enum(self.entitlement, ENTITLEMENT_CLASSES, "resource_identity.entitlement")
        _ = _v_opt_safe_id(self.variant, "resource_identity.variant")
        # Runtime shape guard: direct construction can bypass the annotation.
        if self.quota_pool_ids.__class__ is not tuple:
            raise CapacityValidationError(
                "resource_identity.quota_pool_ids: expected tuple, got "
                + f"{type(self.quota_pool_ids).__name__}"
            )
        pools = tuple(
            _v_safe_id(pool_id, "resource_identity.quota_pool_ids")
            for pool_id in self.quota_pool_ids
        )
        _v_sorted_unique(pools, "resource_identity.quota_pool_ids")

    @classmethod
    def from_dict(cls, d: object) -> "ResourceIdentity":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "resource_identity")
        raw_pools = dd.get("quota_pool_ids")
        pools: tuple[str, ...] = ()
        if raw_pools is not None:
            if not isinstance(raw_pools, list):
                raise CapacityValidationError(
                    "resource_identity.quota_pool_ids: expected list, got "
                    + f"{type(raw_pools).__name__}"
                )
            pools = tuple(cast("list[str]", raw_pools))
        return cls(
            resource_id=_v_safe_id(dd["resource_id"], "resource_identity.resource_id"),
            channel=_v_enum(dd["channel"], EXECUTION_CHANNELS, "resource_identity.channel"),
            provider=_v_safe_id(dd["provider"], "resource_identity.provider"),
            model=_v_safe_id(dd["model"], "resource_identity.model"),
            entitlement=_v_enum(
                dd["entitlement"], ENTITLEMENT_CLASSES, "resource_identity.entitlement"
            ),
            variant=_v_opt_safe_id(dd.get("variant"), "resource_identity.variant"),
            quota_pool_ids=pools,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "resource_id": self.resource_id,
            "channel": self.channel,
            "provider": self.provider,
            "model": self.model,
            "entitlement": self.entitlement,
        }
        if self.variant is not None:
            out["variant"] = self.variant
        if self.quota_pool_ids:
            out["quota_pool_ids"] = list(self.quota_pool_ids)
        return out


@dataclass(frozen=True)
class ExecutionCapabilities:
    """Observed/configured execution-capability facts for one resource.

    Every field is a three-valued fact: ``True`` (evidenced/configured
    support), ``False`` (evidenced/configured absence) or ``None``
    (unknown). None is unknown, never "supported"; consumers fail closed on
    unknown. These are resource-level facts supplied by administrator
    configuration or adapter observation — they are NOT the evidence-based
    OpenAI compatibility matrix (an M03-owned contract with dated evidence
    per adapter/version/backend) and they are NEVER derived from quota
    telemetry: quota never changes an execution fact, exactly like quota
    never changes a capability rating (D-002).
    """

    streaming: bool | None = None
    tool_calls: bool | None = None
    structured_output: bool | None = None
    reasoning_controls: bool | None = None
    usage_reporting: bool | None = None
    cancellation: bool | None = None
    context_limit_tokens: int | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "streaming",
        "tool_calls",
        "structured_output",
        "reasoning_controls",
        "usage_reporting",
        "cancellation",
        "context_limit_tokens",
    )

    def __post_init__(self) -> None:
        _ = _v_opt_bool(self.streaming, "capabilities.streaming")
        _ = _v_opt_bool(self.tool_calls, "capabilities.tool_calls")
        _ = _v_opt_bool(self.structured_output, "capabilities.structured_output")
        _ = _v_opt_bool(self.reasoning_controls, "capabilities.reasoning_controls")
        _ = _v_opt_bool(self.usage_reporting, "capabilities.usage_reporting")
        _ = _v_opt_bool(self.cancellation, "capabilities.cancellation")
        _ = _v_opt_int(self.context_limit_tokens, "capabilities.context_limit_tokens", lo=1)

    @classmethod
    def from_dict(cls, d: object) -> "ExecutionCapabilities":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "capabilities")
        return cls(
            streaming=_v_opt_bool(dd.get("streaming"), "capabilities.streaming"),
            tool_calls=_v_opt_bool(dd.get("tool_calls"), "capabilities.tool_calls"),
            structured_output=_v_opt_bool(
                dd.get("structured_output"), "capabilities.structured_output"
            ),
            reasoning_controls=_v_opt_bool(
                dd.get("reasoning_controls"), "capabilities.reasoning_controls"
            ),
            usage_reporting=_v_opt_bool(
                dd.get("usage_reporting"), "capabilities.usage_reporting"
            ),
            cancellation=_v_opt_bool(dd.get("cancellation"), "capabilities.cancellation"),
            context_limit_tokens=_v_opt_int(
                dd.get("context_limit_tokens"), "capabilities.context_limit_tokens", lo=1
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.streaming is not None:
            out["streaming"] = self.streaming
        if self.tool_calls is not None:
            out["tool_calls"] = self.tool_calls
        if self.structured_output is not None:
            out["structured_output"] = self.structured_output
        if self.reasoning_controls is not None:
            out["reasoning_controls"] = self.reasoning_controls
        if self.usage_reporting is not None:
            out["usage_reporting"] = self.usage_reporting
        if self.cancellation is not None:
            out["cancellation"] = self.cancellation
        if self.context_limit_tokens is not None:
            out["context_limit_tokens"] = self.context_limit_tokens
        return out


@dataclass(frozen=True)
class ResourceHealth:
    """Health of one resource observation, in the capacity v3 vocabulary.

    ``status`` uses exactly the six capacity v3 status values so the
    normalization from a ``CapacitySnapshot`` is the identity mapping:
    ``ok``, ``unavailable``, ``auth_required``, ``unsupported``,
    ``schema_changed``, ``unknown``. As in capacity v3, a known exhausted
    quota (``remaining_percent == 0``) is NOT a health failure and
    ``unknown`` is never read as usable or as zero. Every non-``ok`` status
    must carry its status-level diagnostic code (same mapping as capacity
    v3); window-scoped diagnostics (``percentage_unknown``,
    ``reset_unknown``, ``window_semantics_unknown``) ride along in
    ``diagnostics`` with their window ids.
    """

    status: str
    diagnostics: tuple[CapacityDiagnostic, ...] = ()

    _REQUIRED: ClassVar[tuple[str, ...]] = ("status",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("diagnostics",)

    def __post_init__(self) -> None:
        status = _v_enum(self.status, RESOURCE_HEALTH_STATUSES, "health.status")
        _v_tuple_of(self.diagnostics, CapacityDiagnostic, "health.diagnostics")
        ordered = tuple(sorted(self.diagnostics, key=_diagnostic_sort_key))
        if ordered != self.diagnostics:
            raise CapacityValidationError(
                "health.diagnostics: must be deterministically sorted"
            )
        required_code = _HEALTH_REQUIRED_DIAGNOSTIC.get(status)
        if required_code is not None:
            codes = {diagnostic.code for diagnostic in self.diagnostics}
            if required_code not in codes:
                raise CapacityValidationError(
                    f"health: status={status!r} requires diagnostic code "
                    + f"{required_code!r}"
                )

    @classmethod
    def from_dict(cls, d: object) -> "ResourceHealth":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "health")
        raw_diagnostics = dd.get("diagnostics")
        diagnostics: tuple[CapacityDiagnostic, ...] = ()
        if raw_diagnostics is not None:
            if not isinstance(raw_diagnostics, list):
                raise CapacityValidationError(
                    f"health.diagnostics: expected list, got {type(raw_diagnostics).__name__}"
                )
            diagnostics = tuple(
                CapacityDiagnostic.from_dict(x)
                for x in cast("list[object]", raw_diagnostics)
            )
        return cls(
            status=_v_enum(dd["status"], RESOURCE_HEALTH_STATUSES, "health.status"),
            diagnostics=diagnostics,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"status": self.status}
        if self.diagnostics:
            out["diagnostics"] = [diagnostic.to_dict() for diagnostic in self.diagnostics]
        return out


# ── Resource-state snapshot (the versioned M01 contract) ──────────────────────


@dataclass(frozen=True)
class ResourceStateSnapshot:
    """One versioned resource-state observation (``schema_version = 1``).

    The OBSERVED facts for one executable resource at one observation
    instant: identity, health, quota facts with observation classes and
    promotion observations. It is deliberately ONLY an observation record:
    freshness/polling policy, execution capabilities and cost facts are
    administrator-owned registration state and are composed into registry
    reads (:class:`ResourceRegistryEntry`), never carried by observations.
    This is the single-canonical-truth rule behind the U-003 resolution: a
    worker-reported observation is structurally unable to enlarge its own
    freshness window, change its polling cadence or override configured
    capabilities/cost, because the observation document has no such fields.
    The observation document still travels unchanged inside a worker report
    or a registry read, and its ``observed_at`` plus the registry revision
    identify it for audit provenance.

    ``observed_at`` states WHEN the observation was made and never claims
    freshness by itself; the registry evaluates it against the
    registration's TTL at an explicit evaluation instant. An observation
    dated after the evaluation (or application) instant is rejected, never
    silently treated as fresh.

    Collections serialize canonically (quota facts and promotions in their
    deterministic sort order) and unsorted input is rejected at
    construction, mirroring ``ExecutionEligibility.reason_codes``.
    """

    schema_version: int
    identity: ResourceIdentity
    observed_at: str
    health: ResourceHealth
    quota_facts: tuple[QuotaFact, ...] = ()
    promotions: tuple[PromotionObservation, ...] = ()

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "identity",
        "observed_at",
        "health",
        "quota_facts",
        "promotions",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        sv = _v_int(self.schema_version, "schema_version")
        if sv != RESOURCE_STATE_SCHEMA_VERSION:
            raise CapacityValidationError(
                f"schema_version: expected int {RESOURCE_STATE_SCHEMA_VERSION}, got {sv!r}"
            )
        _v_instance_of(self.identity, ResourceIdentity, "snapshot.identity")
        _ = _v_ts(self.observed_at, "snapshot.observed_at")
        _v_instance_of(self.health, ResourceHealth, "snapshot.health")

        _v_tuple_of(self.quota_facts, QuotaFact, "snapshot.quota_facts")
        if tuple(sorted(self.quota_facts, key=_quota_fact_sort_key)) != self.quota_facts:
            raise CapacityValidationError(
                "snapshot.quota_facts: must be deterministically sorted"
            )
        _v_tuple_of(self.promotions, PromotionObservation, "snapshot.promotions")
        if tuple(sorted(self.promotions, key=_promotion_sort_key)) != self.promotions:
            raise CapacityValidationError(
                "snapshot.promotions: must be deterministically sorted"
            )

    @classmethod
    def from_dict(cls, d: object) -> "ResourceStateSnapshot":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "resource_state_snapshot")
        raw_facts = dd["quota_facts"]
        if not isinstance(raw_facts, list):
            raise CapacityValidationError(
                f"snapshot.quota_facts: expected list, got {type(raw_facts).__name__}"
            )
        raw_promotions = dd["promotions"]
        if not isinstance(raw_promotions, list):
            raise CapacityValidationError(
                f"snapshot.promotions: expected list, got {type(raw_promotions).__name__}"
            )
        return cls(
            schema_version=cast(int, dd["schema_version"]),
            identity=ResourceIdentity.from_dict(dd["identity"]),
            observed_at=_v_ts(dd["observed_at"], "snapshot.observed_at"),
            health=ResourceHealth.from_dict(dd["health"]),
            quota_facts=tuple(
                QuotaFact.from_dict(x) for x in cast("list[object]", raw_facts)
            ),
            promotions=tuple(
                PromotionObservation.from_dict(x)
                for x in cast("list[object]", raw_promotions)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "observed_at": self.observed_at,
            "health": self.health.to_dict(),
            "quota_facts": [fact.to_dict() for fact in self.quota_facts],
            "promotions": [promotion.to_dict() for promotion in self.promotions],
        }

    def validate(self) -> "ResourceStateSnapshot":
        """Re-validate an already-constructed snapshot; returns self or raises."""
        return ResourceStateSnapshot.from_dict(self.to_dict())


# ── Shared normalization (the one path from telemetry to resource state) ──────


def resource_snapshot_from_capacity(
    snapshot: CapacitySnapshot,
    *,
    identity: ResourceIdentity,
    promotions: Sequence[PromotionObservation] = (),
    quota_observation_class: str = "provider_telemetry",
) -> ResourceStateSnapshot:
    """Normalize one capacity v3 snapshot into a resource-state observation.

    This is the single shared normalization used by server-side collection
    and by worker-side reporting alike (M01/M05): provider telemetry enters
    resource state only through this path, so there is never a second
    collector or parser for the same provider/account.

    The health status mapping is the identity mapping over the shared six-
    value vocabulary, and the capacity snapshot's diagnostics are carried
    over unchanged, so fail-closed semantics (``schema_changed``,
    ``auth_required``, ``unknown`` never becoming healthy) are preserved
    verbatim. Every validated window becomes a ``QuotaFact`` with
    ``quota_observation_class`` (default ``provider_telemetry``). The
    informational ``plan`` label is deliberately not carried into resource
    state; it stays a capacity/status concern and an explicit promotion
    scope. Freshness/polling policy, capabilities and cost are NOT inputs
    here: they are administrator registration state and are composed at the
    registry boundary, never onto observations.

    Raises :class:`CapacityValidationError` when the snapshot's provider
    differs from the resource identity's provider: normalizing telemetry
    from a different backend into a resource is a caller bug and fails
    closed.
    """
    _ = _v_enum(
        quota_observation_class, OBSERVATION_CLASSES, "quota_observation_class"
    )
    if snapshot.provider != identity.provider:
        raise CapacityValidationError(
            "resource_snapshot_from_capacity: snapshot provider "
            + f"{snapshot.provider!r} does not match resource identity provider "
            + f"{identity.provider!r}"
        )
    facts = tuple(
        sorted(
            (
                QuotaFact(observation_class=quota_observation_class, window=window)
                for window in snapshot.windows
            ),
            key=_quota_fact_sort_key,
        )
    )
    ordered_promotions = tuple(sorted(promotions, key=_promotion_sort_key))
    return ResourceStateSnapshot(
        schema_version=RESOURCE_STATE_SCHEMA_VERSION,
        identity=identity,
        observed_at=snapshot.retrieved_at,
        health=ResourceHealth(
            status=snapshot.status, diagnostics=snapshot.diagnostics
        ),
        quota_facts=facts,
        promotions=ordered_promotions,
    )


# ── Worker state-report boundary (M05 reports; M01 owns the contract) ─────────


@dataclass(frozen=True)
class WorkerStateReport:
    """One authenticated worker's report of locally observed resource state.

    This is the M01 contract boundary for worker-reported state; the M05
    worker protocol (pairing, transport, heartbeat) wraps it. The report
    carries opaque per-device identity and complete
    :class:`ResourceStateSnapshot` documents produced by the SAME shared
    normalization the server uses — never raw provider payloads, never a
    second normalization.

    Fail-closed validation: only worker-reachable channels
    (``worker_bridged``, ``local_app_adapter``) may be reported; resource
    ids must be unique; every ``observed_at`` must not be after
    ``reported_at``; the snapshot documents must be canonically ordered by
    resource id. Any violation rejects the report at construction, and
    :meth:`ResourceRegistry.apply_worker_report` applies reports
    atomically — a report with any unregistered or mismatched entry changes
    nothing.
    """

    schema_version: int
    worker_id: str
    reported_at: str
    resources: tuple[ResourceStateSnapshot, ...]

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "worker_id",
        "reported_at",
        "resources",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        sv = _v_int(self.schema_version, "schema_version")
        if sv != WORKER_REPORT_SCHEMA_VERSION:
            raise CapacityValidationError(
                f"schema_version: expected int {WORKER_REPORT_SCHEMA_VERSION}, got {sv!r}"
            )
        _ = _v_safe_id(self.worker_id, "worker_report.worker_id")
        _ = _v_ts(self.reported_at, "worker_report.reported_at")
        _v_tuple_of(self.resources, ResourceStateSnapshot, "worker_report.resources")
        resource_ids = [snapshot.identity.resource_id for snapshot in self.resources]
        _v_sorted_unique(tuple(resource_ids), "worker_report.resources")
        for snapshot in self.resources:
            if snapshot.identity.channel not in WORKER_REPORTABLE_CHANNELS:
                raise CapacityValidationError(
                    "worker_report: resource "
                    + f"{snapshot.identity.resource_id!r} has channel "
                    + f"{snapshot.identity.channel!r}; workers may only report "
                    + f"{sorted(WORKER_REPORTABLE_CHANNELS)}"
                )
            if _age_seconds(self.reported_at, snapshot.observed_at) < 0:
                raise CapacityValidationError(
                    f"worker_report: resource {snapshot.identity.resource_id!r} "
                    + "has observed_at after reported_at"
                )

    @classmethod
    def from_dict(cls, d: object) -> "WorkerStateReport":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "worker_state_report")
        raw_resources = dd["resources"]
        if not isinstance(raw_resources, list):
            raise CapacityValidationError(
                f"worker_report.resources: expected list, got {type(raw_resources).__name__}"
            )
        return cls(
            schema_version=cast(int, dd["schema_version"]),
            worker_id=_v_safe_id(dd["worker_id"], "worker_report.worker_id"),
            reported_at=_v_ts(dd["reported_at"], "worker_report.reported_at"),
            resources=tuple(
                ResourceStateSnapshot.from_dict(x)
                for x in cast("list[object]", raw_resources)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "worker_id": self.worker_id,
            "reported_at": self.reported_at,
            "resources": [snapshot.to_dict() for snapshot in self.resources],
        }

    def validate(self) -> "WorkerStateReport":
        """Re-validate an already-constructed report; returns self or raises."""
        return WorkerStateReport.from_dict(self.to_dict())


# ── Registry read model ───────────────────────────────────────────────────────


def _identity_observation_key(identity: ResourceIdentity) -> tuple[str | None, ...]:
    """The observation-relevant identity fields (pool ids excluded).

    Quota-pool membership is registration-owned policy (D-042/D-053);
    observations are matched and merged on the execution identity only.
    """
    return (
        identity.resource_id,
        identity.channel,
        identity.provider,
        identity.model,
        identity.entitlement,
        identity.variant,
    )


@dataclass(frozen=True)
class ResourceRegistryEntry:
    """One registry read entry: administrator policy plus evaluated state.

    ``freshness_ttl_seconds`` and ``poll_interval_seconds`` are the
    administrator registration's authoritative server policy, and
    ``capabilities``/``cost`` are its configured facts; all four are
    composed here FROM THE REGISTRATION, never from the observation, so the
    read model is self-contained while observations stay pure observation
    records. An accepted observation can therefore never enlarge its own
    freshness window, change its polling cadence or override configured
    facts.

    Cross-resource invariance (fail-closed, including on deserialization):
    whenever ``observation`` is present, ``observation.identity`` must
    equal this entry's ``identity`` exactly.

    ``observation`` is ``None`` exactly when the resource is registered but
    never observed; that state is ``freshness == "never_observed"`` — an
    honest unknown, never a healthy or usable reading. ``freshness`` is
    evaluated against the registry snapshot's ``generated_at`` instant
    using the registration's TTL: ``stale`` means a real last observation
    exists but its freshness window has elapsed. The observation's own
    health is never rewritten by staleness.
    """

    identity: ResourceIdentity
    freshness_ttl_seconds: int
    poll_interval_seconds: int | None
    capabilities: ExecutionCapabilities
    cost: ResourceCost | None
    observation: ResourceStateSnapshot | None
    freshness: str
    refresh_due: bool

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "identity",
        "freshness_ttl_seconds",
        "observation",
        "freshness",
        "refresh_due",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "poll_interval_seconds",
        "capabilities",
        "cost",
    )

    def __post_init__(self) -> None:
        _v_instance_of(self.identity, ResourceIdentity, "registry_entry.identity")
        _ = _v_int(
            self.freshness_ttl_seconds, "registry_entry.freshness_ttl_seconds", lo=1
        )
        _ = _v_opt_int(
            self.poll_interval_seconds, "registry_entry.poll_interval_seconds", lo=1
        )
        _v_instance_of(
            self.capabilities, ExecutionCapabilities, "registry_entry.capabilities"
        )
        if self.cost is not None:
            _v_instance_of(self.cost, ResourceCost, "registry_entry.cost")
        if self.observation is not None:
            _v_instance_of(
                self.observation, ResourceStateSnapshot, "registry_entry.observation"
            )
            # Quota-pool membership is registration-owned policy
            # (D-042/D-053, see ResourceRegistry._checked_observation):
            # the observation match ignores pool ids, so an observation can
            # never alter pool membership while cross-resource state still
            # fails closed.
            if _identity_observation_key(
                self.observation.identity
            ) != _identity_observation_key(self.identity):
                raise CapacityValidationError(
                    "registry_entry: observation identity for "
                    + f"{self.observation.identity.resource_id!r} does not match "
                    + f"the entry identity {self.identity.resource_id!r}; "
                    + "cross-resource registry state fails closed"
                )
        freshness = _v_enum(self.freshness, FRESHNESS_STATES, "registry_entry.freshness")
        _ = _v_bool(self.refresh_due, "registry_entry.refresh_due")
        if self.observation is None and freshness != "never_observed":
            raise CapacityValidationError(
                "registry_entry: a resource without observation must be "
                + "'never_observed'"
            )
        if self.observation is not None and freshness == "never_observed":
            raise CapacityValidationError(
                "registry_entry: an observed resource is never 'never_observed'"
            )

    @classmethod
    def from_dict(cls, d: object) -> "ResourceRegistryEntry":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "registry_entry")
        raw_observation = dd["observation"]
        raw_capabilities = dd.get("capabilities")
        return cls(
            identity=ResourceIdentity.from_dict(dd["identity"]),
            freshness_ttl_seconds=cast(int, dd["freshness_ttl_seconds"]),
            poll_interval_seconds=_v_opt_int(
                dd.get("poll_interval_seconds"),
                "registry_entry.poll_interval_seconds",
                lo=1,
            ),
            capabilities=(
                ExecutionCapabilities()
                if raw_capabilities is None
                else ExecutionCapabilities.from_dict(raw_capabilities)
            ),
            cost=(
                None
                if dd.get("cost") is None
                else ResourceCost.from_dict(dd.get("cost"))
            ),
            observation=(
                None
                if raw_observation is None
                else ResourceStateSnapshot.from_dict(raw_observation)
            ),
            freshness=_v_enum(dd["freshness"], FRESHNESS_STATES, "registry_entry.freshness"),
            refresh_due=cast(bool, dd["refresh_due"]),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "identity": self.identity.to_dict(),
            "freshness_ttl_seconds": self.freshness_ttl_seconds,
        }
        if self.poll_interval_seconds is not None:
            out["poll_interval_seconds"] = self.poll_interval_seconds
        if self.capabilities.to_dict():
            out["capabilities"] = self.capabilities.to_dict()
        if self.cost is not None:
            out["cost"] = self.cost.to_dict()
        out["observation"] = (
            None if self.observation is None else self.observation.to_dict()
        )
        out["freshness"] = self.freshness
        out["refresh_due"] = self.refresh_due
        return out


@dataclass(frozen=True)
class QuotaPoolGroup:
    """One confirmed quota pool and the resources that draw from it.

    Exists only from explicit confirmed ``quota_pool_ids`` on resource
    identities; it is never inferred. Members of one pool are one budget,
    never independent capacity (D-042). A pool with a single member is
    valid and still explicit.
    """

    pool_id: str
    resource_ids: tuple[str, ...]

    _REQUIRED: ClassVar[tuple[str, ...]] = ("pool_id", "resource_ids")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.pool_id, "quota_pool_group.pool_id")
        if self.resource_ids.__class__ is not tuple:
            raise CapacityValidationError(
                "quota_pool_group.resource_ids: expected tuple, got "
                + f"{type(self.resource_ids).__name__}"
            )
        members = tuple(
            _v_safe_id(resource_id, "quota_pool_group.resource_ids")
            for resource_id in self.resource_ids
        )
        _v_sorted_unique(members, "quota_pool_group.resource_ids")
        if not members:
            raise CapacityValidationError(
                "quota_pool_group: a pool must have at least one member resource"
            )

    @classmethod
    def from_dict(cls, d: object) -> "QuotaPoolGroup":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "quota_pool_group")
        raw_members = dd["resource_ids"]
        if not isinstance(raw_members, list):
            raise CapacityValidationError(
                f"quota_pool_group.resource_ids: expected list, got {type(raw_members).__name__}"
            )
        return cls(
            pool_id=_v_safe_id(dd["pool_id"], "quota_pool_group.pool_id"),
            resource_ids=tuple(cast("list[str]", raw_members)),
        )

    def to_dict(self) -> dict[str, object]:
        return {"pool_id": self.pool_id, "resource_ids": list(self.resource_ids)}


@dataclass(frozen=True)
class RegistrySnapshot:
    """One versioned read of the whole registry (``schema_version = 1``).

    ``revision`` is the store's monotonic mutation counter and together
    with ``generated_at`` identifies this read for audit provenance (the
    D-043 audit field "state snapshot identity/version"). Entries are
    canonically ordered by resource id; ``quota_pools`` is exactly the
    confirmed-pool derivation of the entries (validated at construction),
    ordered by pool id.
    """

    schema_version: int
    revision: int
    generated_at: str
    entries: tuple[ResourceRegistryEntry, ...]
    quota_pools: tuple[QuotaPoolGroup, ...]

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "revision",
        "generated_at",
        "entries",
        "quota_pools",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        sv = _v_int(self.schema_version, "schema_version")
        if sv != REGISTRY_SCHEMA_VERSION:
            raise CapacityValidationError(
                f"schema_version: expected int {REGISTRY_SCHEMA_VERSION}, got {sv!r}"
            )
        _ = _v_int(self.revision, "registry_snapshot.revision", lo=0)
        _ = _v_ts(self.generated_at, "registry_snapshot.generated_at")
        _v_tuple_of(self.entries, ResourceRegistryEntry, "registry_snapshot.entries")
        resource_ids = [entry.identity.resource_id for entry in self.entries]
        _v_sorted_unique(tuple(resource_ids), "registry_snapshot.entries")
        _v_tuple_of(self.quota_pools, QuotaPoolGroup, "registry_snapshot.quota_pools")
        pool_ids = [pool.pool_id for pool in self.quota_pools]
        _v_sorted_unique(tuple(pool_ids), "registry_snapshot.quota_pools")

        derived: dict[str, list[str]] = {}
        for entry in self.entries:
            for pool_id in entry.identity.quota_pool_ids:
                derived.setdefault(pool_id, []).append(entry.identity.resource_id)
        expected_pools = tuple(
            QuotaPoolGroup(pool_id=pool_id, resource_ids=tuple(sorted(members)))
            for pool_id, members in sorted(derived.items())
        )
        if expected_pools != self.quota_pools:
            raise CapacityValidationError(
                "registry_snapshot.quota_pools: must be exactly the confirmed-"
                + "pool derivation of the entries"
            )

    @classmethod
    def from_dict(cls, d: object) -> "RegistrySnapshot":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "registry_snapshot")
        raw_entries = dd["entries"]
        if not isinstance(raw_entries, list):
            raise CapacityValidationError(
                f"registry_snapshot.entries: expected list, got {type(raw_entries).__name__}"
            )
        raw_pools = dd["quota_pools"]
        if not isinstance(raw_pools, list):
            raise CapacityValidationError(
                f"registry_snapshot.quota_pools: expected list, got {type(raw_pools).__name__}"
            )
        return cls(
            schema_version=cast(int, dd["schema_version"]),
            revision=cast(int, dd["revision"]),
            generated_at=_v_ts(dd["generated_at"], "registry_snapshot.generated_at"),
            entries=tuple(
                ResourceRegistryEntry.from_dict(x)
                for x in cast("list[object]", raw_entries)
            ),
            quota_pools=tuple(
                QuotaPoolGroup.from_dict(x) for x in cast("list[object]", raw_pools)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "generated_at": self.generated_at,
            "entries": [entry.to_dict() for entry in self.entries],
            "quota_pools": [pool.to_dict() for pool in self.quota_pools],
        }


# ── Registration and the in-memory registry store ─────────────────────────────


@dataclass(frozen=True)
class ResourceRegistration:
    """Administrator-side registration of one executable resource.

    The registry's configuration input (A0: "administrator resource
    configuration"): identity, the bounded freshness policy and polling
    cadence for the resource, and its configured capability/cost facts.
    Promotions are NOT registration data — they are observations and ride
    on snapshots. ``freshness_ttl_seconds`` is required: a registered
    resource never has an unbounded freshness policy.
    """

    identity: ResourceIdentity
    freshness_ttl_seconds: int
    poll_interval_seconds: int | None = None
    capabilities: ExecutionCapabilities = field(default_factory=ExecutionCapabilities)
    cost: ResourceCost | None = None

    def __post_init__(self) -> None:
        _v_instance_of(self.identity, ResourceIdentity, "registration.identity")
        _ = _v_int(self.freshness_ttl_seconds, "registration.freshness_ttl_seconds", lo=1)
        _ = _v_opt_int(self.poll_interval_seconds, "registration.poll_interval_seconds", lo=1)
        _v_instance_of(
            self.capabilities, ExecutionCapabilities, "registration.capabilities"
        )
        if self.cost is not None:
            _v_instance_of(self.cost, ResourceCost, "registration.cost")


# Local clock seam: deliberately not imported from ``status.py`` — the status
# module composes provider acquisition shells, and the domain must stay free
# of provider imports.
Clock = Callable[[], str]


def _default_clock() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def classify_freshness(
    *,
    observed_at: str,
    now: str,
    freshness_ttl_seconds: int,
) -> str:
    """Classify one observation as ``fresh`` or ``stale`` against ``now``.

    The U-003 staleness rule, scoped to the server's state store: an
    observation is fresh while its age is at most ``freshness_ttl_seconds``
    and stale strictly beyond it. There is no default TTL, no score and no
    partial freshness: the TTL is always the administrator registration's
    explicit bound.

    A future observation (``observed_at`` after ``now``) is rejected with
    :class:`CapacityValidationError` — it is never silently treated as
    fresh. This is the smallest fail-closed rule at the M01 evaluation
    boundary; producing server-comparable observation times (including any
    clock-skew tolerance protocol) is the reporting side's responsibility
    and belongs to the worker transport (M05), not here.
    """
    _ = _v_ts(observed_at, "observed_at")
    _ = _v_ts(now, "now")
    _ = _v_int(freshness_ttl_seconds, "freshness_ttl_seconds", lo=1)
    if _age_or_reject(now, observed_at, context="classify_freshness") > (
        freshness_ttl_seconds
    ):
        return "stale"
    return "fresh"


class ResourceRegistry:
    """In-memory registry of executable resources and their latest state.

    One server-side state store for M01: registrations from administrator
    configuration, latest observations from the shared normalization
    (direct collection or authenticated worker reports), and evaluated
    reads for the routing core (M02). Deliberately in-memory per D-041 —
    the durable SQLite-class store lands with M03/M09 and is not introduced
    here; a restart reconstructs from configuration plus fresh collection.

    Mutation is fail-closed: observations are accepted only for registered
    resources with an exactly matching identity (a report can never
    redefine a resource), and only when they are not dated after the
    server's current instant (a future observation is rejected, never
    stored as fresh). Freshness and polling policy are taken from the
    registration, never from the observation: an accepted observation —
    including a worker report — cannot change the server's freshness or
    polling behavior, and configured capabilities/cost stay
    registration-owned. Worker reports are applied atomically, so a report
    with any invalid entry changes nothing. Every accepted mutation bumps
    the monotonic ``revision``. Clock is injectable and every read accepts
    an explicit ``now`` for deterministic evaluation.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock: Clock = clock if clock is not None else _default_clock
        self._registrations: dict[str, ResourceRegistration] = {}
        self._observations: dict[str, ResourceStateSnapshot] = {}
        self._revision: int = 0

    def register(self, registration: ResourceRegistration) -> None:
        """Register one executable resource; duplicate ids fail closed."""
        _v_instance_of(registration, ResourceRegistration, "registration")
        resource_id = registration.identity.resource_id
        if resource_id in self._registrations:
            raise CapacityValidationError(
                f"registry: resource {resource_id!r} is already registered"
            )
        self._registrations[resource_id] = registration
        self._revision += 1

    def _checked_observation(self, snapshot: ResourceStateSnapshot) -> None:
        _v_instance_of(snapshot, ResourceStateSnapshot, "snapshot")
        resource_id = snapshot.identity.resource_id
        registration = self._registrations.get(resource_id)
        if registration is None:
            raise CapacityValidationError(
                f"registry: resource {resource_id!r} is not registered"
            )
        # Quota-pool membership is REGISTRATION-owned policy (D-042/D-053):
        # like freshness/polling policy and configured facts, an observation
        # can never change it. The identity match therefore ignores the
        # pool ids — the read model composes pools from the registration
        # alone, so an accepted observation cannot alter pool membership.
        if _identity_observation_key(
            snapshot.identity
        ) != _identity_observation_key(registration.identity):
            raise CapacityValidationError(
                f"registry: snapshot identity for {resource_id!r} does not match "
                + "the registered identity"
            )
        _ = _age_or_reject(
            self._clock(),
            snapshot.observed_at,
            context=f"registry: observation for {resource_id!r}",
        )

    def is_registered(self, resource_id: str) -> bool:
        """Whether a resource id currently has a registration here."""
        return resource_id in self._registrations

    def apply_snapshot(self, snapshot: ResourceStateSnapshot) -> None:
        """Record one normalized observation for a registered resource."""
        self._checked_observation(snapshot)
        self._observations[snapshot.identity.resource_id] = snapshot
        self._revision += 1

    def apply_worker_report(self, report: WorkerStateReport) -> None:
        """Apply one worker report atomically, or change nothing.

        Every entry is checked against the registrations and the server's
        current instant first; any unregistered, mismatched or
        future-dated resource rejects the whole report (fail-closed, never
        merged silently). Unrelated resources are never affected either
        way, and a report cannot change any resource's registered
        freshness/polling policy or configured facts because observation
        documents carry none.
        """
        _v_instance_of(report, WorkerStateReport, "worker_report")
        for snapshot in report.resources:
            self._checked_observation(snapshot)
        for snapshot in report.resources:
            self._observations[snapshot.identity.resource_id] = snapshot
        self._revision += 1

    def refresh_due(self, *, now: str | None = None) -> tuple[str, ...]:
        """Resource ids whose bounded polling cadence says refresh now.

        A resource is due when polling is configured and either it has
        never been observed or its last observation is at least
        ``poll_interval_seconds`` old. Resources without a polling cadence
        are never due. An observation dated after ``now`` fails closed
        with :class:`CapacityValidationError`, consistent with freshness
        evaluation — a future observation is never silently treated as
        not due. Ordering is deterministic (resource id).
        """
        current = self._clock() if now is None else _v_ts(now, "now")
        due: list[str] = []
        for resource_id in sorted(self._registrations):
            registration = self._registrations[resource_id]
            if registration.poll_interval_seconds is None:
                continue
            observation = self._observations.get(resource_id)
            if observation is None:
                due.append(resource_id)
                continue
            age = _age_or_reject(
                current,
                observation.observed_at,
                context=f"refresh_due({resource_id!r})",
            )
            if age >= registration.poll_interval_seconds:
                due.append(resource_id)
        return tuple(due)

    def registry_snapshot(self, *, now: str | None = None) -> RegistrySnapshot:
        """One evaluated, versioned read of the whole registry.

        Freshness is evaluated per resource against this read's instant
        using the registration's authoritative TTL. Evaluating with an
        instant earlier than a stored observation (a backwards-moving
        clock or an incoherent explicit ``now``) fails closed with
        :class:`CapacityValidationError` instead of classifying a future
        observation as fresh.
        """
        generated_at = self._clock() if now is None else _v_ts(now, "now")
        entries: list[ResourceRegistryEntry] = []
        pool_members: dict[str, list[str]] = {}
        for resource_id in sorted(self._registrations):
            registration = self._registrations[resource_id]
            observation = self._observations.get(resource_id)
            if observation is None:
                freshness = "never_observed"
            else:
                freshness = classify_freshness(
                    observed_at=observation.observed_at,
                    now=generated_at,
                    freshness_ttl_seconds=registration.freshness_ttl_seconds,
                )
            refresh_due = self._entry_refresh_due(
                registration=registration,
                observation=observation,
                now=generated_at,
            )
            entries.append(
                ResourceRegistryEntry(
                    identity=registration.identity,
                    freshness_ttl_seconds=registration.freshness_ttl_seconds,
                    poll_interval_seconds=registration.poll_interval_seconds,
                    capabilities=registration.capabilities,
                    cost=registration.cost,
                    observation=observation,
                    freshness=freshness,
                    refresh_due=refresh_due,
                )
            )
            for pool_id in registration.identity.quota_pool_ids:
                pool_members.setdefault(pool_id, []).append(resource_id)
        pools = tuple(
            QuotaPoolGroup(pool_id=pool_id, resource_ids=tuple(sorted(members)))
            for pool_id, members in sorted(pool_members.items())
        )
        return RegistrySnapshot(
            schema_version=REGISTRY_SCHEMA_VERSION,
            revision=self._revision,
            generated_at=generated_at,
            entries=tuple(entries),
            quota_pools=pools,
        )

    @staticmethod
    def _entry_refresh_due(
        *,
        registration: ResourceRegistration,
        observation: ResourceStateSnapshot | None,
        now: str,
    ) -> bool:
        if registration.poll_interval_seconds is None:
            return False
        if observation is None:
            return True
        return (
            _age_or_reject(
                now,
                observation.observed_at,
                context=f"registry read of {registration.identity.resource_id!r}",
            )
            >= registration.poll_interval_seconds
        )


__all__ = [
    "ENTITLEMENT_CLASSES",
    "EXECUTION_CHANNELS",
    "FRESHNESS_STATES",
    "OBSERVATION_CLASSES",
    "REGISTRY_SCHEMA_VERSION",
    "RESOURCE_HEALTH_STATUSES",
    "RESOURCE_STATE_SCHEMA_VERSION",
    "WORKER_REPORTABLE_CHANNELS",
    "WORKER_REPORT_SCHEMA_VERSION",
    "Clock",
    "ExecutionCapabilities",
    "PromotionObservation",
    "QuotaFact",
    "QuotaPoolGroup",
    "ResourceCost",
    "ResourceHealth",
    "ResourceIdentity",
    "ResourceRegistration",
    "ResourceRegistry",
    "ResourceRegistryEntry",
    "ResourceStateSnapshot",
    "RegistrySnapshot",
    "WorkerStateReport",
    "classify_freshness",
    "resource_snapshot_from_capacity",
]
