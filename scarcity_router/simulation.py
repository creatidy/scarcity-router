"""Typed simulation over the same deterministic selector (M2e, D-027).

Pure, provider-independent, standard-library only. No filesystem, network,
environment, subprocess, clock or provider access: callers supply the same
inputs as :func:`selector.select_model` plus a typed
:class:`SimulationOverrides` record, and every function is deterministic over
its arguments.

Core rule: the simulated decision is produced by the SAME authoritative
``select_model`` core as the baseline — there is no copied ranking pipeline
and no simulation-specific selector. Overrides may only:

- replace percentage pairs of EXISTING normalized windows
  (:class:`CapacityPercentageOverride`), never create windows, snapshots,
  provider statuses or fabricated telemetry out of unknown state;
- replace the selector policy for the simulated run;
- replace the replenishment observations (``None`` retains the baseline, an
  explicit empty tuple simulates no observations, a non-empty tuple fully
  replaces them);
- move the simulated blackout-evaluation instant (timezone-aware).

Baseline inputs are never mutated: overridden snapshots are new frozen
copies and every other input is reused read-only. A percentage override
must match exactly one existing window of an ``ok`` snapshot that already
carries a known percentage pair; zero or multiple matches are typed
validation failures (an ambiguous match requires an exact ``window_id``,
which is matched exactly and never parsed).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import ClassVar, TypeVar, cast

from .capacity import CapacitySnapshot
from .errors import SelectionContractValidationError
from .policy import ReplenishmentState
from .selector import SelectionDecision, SelectorPolicy, select_model
from .selection_types import SUPPORTED_PROVIDERS, ModelCatalog, TaskRequirement

# ── Frozen value sets ─────────────────────────────────────────────────────────

# An override targets a concretely identifiable window, exactly like a
# reservation rule: unknown resource/kind semantics can never identify one,
# and unknown telemetry is never turned into fabricated known telemetry.
_OVERRIDE_RESOURCES: frozenset[str] = frozenset({"tokens", "time"})
_OVERRIDE_KINDS: frozenset[str] = frozenset({"five_hour", "weekly"})

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

# ── Validators (single source of truth for the M2e simulation rules) ──────────

_T = TypeVar("_T")


def _v_str(value: object, fld: str) -> str:
    if not isinstance(value, str):
        raise SelectionContractValidationError(
            f"{fld}: expected str, got {type(value).__name__}"
        )
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
            f"{fld}: unsupported provider {s!r}; supported model providers "
            + f"are exactly {sorted(SUPPORTED_PROVIDERS)}"
        )
    return s


def _v_enum(value: object, allowed: frozenset[str], fld: str) -> str:
    s = _v_str(value, fld)
    if s not in allowed:
        raise SelectionContractValidationError(
            f"{fld}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def _v_pct(value: object, fld: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectionContractValidationError(
            f"{fld}: expected int, got {type(value).__name__} ({value!r})"
        )
    if value < 0 or value > 100:
        raise SelectionContractValidationError(f"{fld}: value {value} outside 0..100")
    return value


def _v_instance_of(value: object, cls: type[_T], label: str) -> _T:
    if not isinstance(value, cls):
        raise SelectionContractValidationError(
            f"{label}: expected a {cls.__name__}, got {type(value).__name__}"
        )
    return value


def _v_tuple_of(value: object, item_type: type[_T], label: str) -> None:
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


def _v_aware_datetime(value: object, fld: str) -> datetime:
    if not isinstance(value, datetime):
        raise SelectionContractValidationError(
            f"{fld}: expected datetime, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise SelectionContractValidationError(
            f"{fld}: expected a timezone-aware datetime, got a naive datetime"
        )
    return value


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
    m = _as_str_object_mapping(obj)
    if m is None:
        raise SelectionContractValidationError(
            f"{label}: expected a dict-like mapping, got {type(obj).__name__}"
        )
    allowed = frozenset(required) | frozenset(optional)
    extra = set(m.keys()) - allowed
    if extra:
        raise SelectionContractValidationError(f"{label}: unknown keys {sorted(extra)}")
    missing = [key for key in required if key not in m]
    if missing:
        raise SelectionContractValidationError(
            f"{label}: missing required keys {sorted(missing)}"
        )
    return m


def _parse_instant(value: object, fld: str) -> datetime:
    """Parse a serialized timezone-aware ISO-8601 instant."""
    s = _v_str(value, fld)
    text = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SelectionContractValidationError(
            f"{fld}: invalid timestamp {s!r}"
        ) from exc
    return _v_aware_datetime(parsed, fld)


# ── Capacity percentage override ──────────────────────────────────────────────


@dataclass(frozen=True)
class CapacityPercentageOverride:
    """A typed override of one EXISTING normalized window's percentage pair.

    Matching is exact normalized identity: the supported provider, the exact
    ``(provider, scope_id)`` scope, the exact resource and window kind, and
    optionally one exact diagnostic ``window_id`` (matched exactly, never
    parsed). The override may change only ``used_percent`` and
    ``remaining_percent`` (``remaining`` = configured value, ``used`` =
    ``100 - remaining``); provider, status, source, scope, resource, kind,
    duration, reset, window id, diagnostics, plan and retrieval time are
    preserved. It never creates windows or snapshots, never changes a
    provider status and never turns unknown telemetry into fabricated known
    telemetry.
    """

    provider: str
    scope_id: str
    resource: str
    kind: str
    remaining_percent: int
    window_id: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "provider",
        "scope_id",
        "resource",
        "kind",
        "remaining_percent",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("window_id",)

    def __post_init__(self) -> None:
        _ = _v_provider(self.provider, "capacity_percentage_override.provider")
        _ = _v_safe_id(self.scope_id, "capacity_percentage_override.scope_id")
        _ = _v_enum(
            self.resource, _OVERRIDE_RESOURCES, "capacity_percentage_override.resource"
        )
        _ = _v_enum(self.kind, _OVERRIDE_KINDS, "capacity_percentage_override.kind")
        _ = _v_pct(
            self.remaining_percent, "capacity_percentage_override.remaining_percent"
        )
        if self.window_id is not None:
            _ = _v_safe_id(self.window_id, "capacity_percentage_override.window_id")

    @classmethod
    def from_dict(cls, d: object) -> "CapacityPercentageOverride":
        dd = _v_exact_shape(
            d, cls._REQUIRED, cls._OPTIONAL, "capacity_percentage_override"
        )
        window_id: str | None = None
        if dd.get("window_id") is not None:
            window_id = _v_safe_id(
                dd["window_id"], "capacity_percentage_override.window_id"
            )
        return cls(
            provider=_v_provider(dd["provider"], "capacity_percentage_override.provider"),
            scope_id=_v_safe_id(dd["scope_id"], "capacity_percentage_override.scope_id"),
            resource=_v_enum(
                dd["resource"], _OVERRIDE_RESOURCES, "capacity_percentage_override.resource"
            ),
            kind=_v_enum(dd["kind"], _OVERRIDE_KINDS, "capacity_percentage_override.kind"),
            remaining_percent=_v_pct(
                dd["remaining_percent"],
                "capacity_percentage_override.remaining_percent",
            ),
            window_id=window_id,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "provider": self.provider,
            "scope_id": self.scope_id,
            "resource": self.resource,
            "kind": self.kind,
            "remaining_percent": self.remaining_percent,
        }
        if self.window_id is not None:
            out["window_id"] = self.window_id
        return out


# ── Simulation overrides ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SimulationOverrides:
    """The typed override record applied to one simulated selection run.

    ``capacity_percentages`` apply to copies of the baseline snapshots (the
    same window may not be overridden twice). ``selector_policy``, when
    present, replaces the baseline selector policy for the simulated run.
    ``replenishment_states`` is tri-state: ``None`` retains the baseline
    observations, an explicit empty tuple simulates no observations and a
    non-empty tuple fully replaces them; a replacement set is a set of
    observations, not an ordered policy preference, so it is canonicalized
    by ``(provider, kind)`` and duplicates are rejected at this contract
    boundary. ``evaluated_at``, when present, is
    the simulated blackout-evaluation instant and must be timezone-aware.
    """

    capacity_percentages: tuple[CapacityPercentageOverride, ...] = ()
    selector_policy: SelectorPolicy | None = None
    replenishment_states: tuple[ReplenishmentState, ...] | None = None
    evaluated_at: datetime | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "capacity_percentages",
        "selector_policy",
        "replenishment_states",
        "evaluated_at",
    )

    def __post_init__(self) -> None:
        _ = _v_tuple_of(
            self.capacity_percentages,
            CapacityPercentageOverride,
            "simulation_overrides.capacity_percentages",
        )
        if self.selector_policy is not None:
            _ = _v_instance_of(
                self.selector_policy,
                SelectorPolicy,
                "simulation_overrides.selector_policy",
            )
        if self.replenishment_states is not None:
            _ = _v_tuple_of(
                self.replenishment_states,
                ReplenishmentState,
                "simulation_overrides.replenishment_states",
            )
            seen_states: set[tuple[str, str]] = set()
            for state in self.replenishment_states:
                key = (state.provider, state.kind)
                if key in seen_states:
                    raise SelectionContractValidationError(
                        "simulation_overrides.replenishment_states: duplicate "
                        + f"replenishment state for (provider, kind) {key}; "
                        + "at most one state per (provider, kind) is permitted"
                    )
                seen_states.add(key)
            # A replacement set is not an ordered policy preference:
            # canonicalize by (provider, kind) so to_dict() is stable for
            # semantically identical replacement sets. None vs empty-tuple
            # semantics are unchanged.
            object.__setattr__(
                self,
                "replenishment_states",
                tuple(
                    sorted(
                        self.replenishment_states,
                        key=lambda state: (state.provider, state.kind),
                    )
                ),
            )
        if self.evaluated_at is not None:
            _ = _v_aware_datetime(
                self.evaluated_at, "simulation_overrides.evaluated_at"
            )

    @classmethod
    def from_dict(cls, d: object) -> "SimulationOverrides":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "simulation_overrides")
        percentages: tuple[CapacityPercentageOverride, ...] = ()
        raw_percentages = dd.get("capacity_percentages")
        if raw_percentages is not None:
            if not isinstance(raw_percentages, list):
                raise SelectionContractValidationError(
                    "simulation_overrides.capacity_percentages: expected list, "
                    + f"got {type(raw_percentages).__name__}"
                )
            percentages = tuple(
                CapacityPercentageOverride.from_dict(x)
                for x in cast("list[object]", raw_percentages)
            )
        selector_policy: SelectorPolicy | None = None
        if dd.get("selector_policy") is not None:
            selector_policy = SelectorPolicy.from_dict(dd["selector_policy"])
        replenishment: tuple[ReplenishmentState, ...] | None = None
        raw_replenishment = dd.get("replenishment_states")
        if raw_replenishment is not None:
            if not isinstance(raw_replenishment, list):
                raise SelectionContractValidationError(
                    "simulation_overrides.replenishment_states: expected list, "
                    + f"got {type(raw_replenishment).__name__}"
                )
            replenishment = tuple(
                ReplenishmentState.from_dict(x)
                for x in cast("list[object]", raw_replenishment)
            )
        evaluated_at: datetime | None = None
        if dd.get("evaluated_at") is not None:
            evaluated_at = _parse_instant(
                dd["evaluated_at"], "simulation_overrides.evaluated_at"
            )
        return cls(
            capacity_percentages=percentages,
            selector_policy=selector_policy,
            replenishment_states=replenishment,
            evaluated_at=evaluated_at,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "capacity_percentages": [
                override.to_dict() for override in self.capacity_percentages
            ],
        }
        if self.selector_policy is not None:
            out["selector_policy"] = self.selector_policy.to_dict()
        if self.replenishment_states is not None:
            out["replenishment_states"] = [
                state.to_dict() for state in self.replenishment_states
            ]
        if self.evaluated_at is not None:
            out["evaluated_at"] = (
                self.evaluated_at.astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        return out


# ── Override application (copies only; baseline never mutated) ────────────────


def apply_capacity_overrides(
    snapshots: Sequence[CapacitySnapshot],
    overrides: Sequence[CapacityPercentageOverride],
) -> tuple[CapacitySnapshot, ...]:
    """Apply percentage overrides to COPIES of the baseline snapshots.

    Never mutates the baseline. Each override must match exactly one
    existing window of the target provider's ``ok`` snapshot that already
    carries a known percentage pair; zero matches, ambiguous matches (use an
    exact ``window_id``) and a second override of the same window are typed
    validation failures. Providers, statuses, non-target windows and all
    other snapshot data are preserved as-is.
    """
    snapshot_by_provider: dict[str, CapacitySnapshot] = {}
    for snapshot in snapshots:
        _ = _v_instance_of(snapshot, CapacitySnapshot, "apply_capacity_overrides.snapshots")
        if snapshot.provider in snapshot_by_provider:
            raise SelectionContractValidationError(
                "apply_capacity_overrides.snapshots: duplicate provider "
                + f"snapshot for {snapshot.provider!r}"
            )
        snapshot_by_provider[snapshot.provider] = snapshot

    result: dict[str, CapacitySnapshot] = {}
    overridden_window_keys: set[tuple[str, str, str, str, str | None]] = set()
    for override in overrides:
        _ = _v_instance_of(
            override,
            CapacityPercentageOverride,
            "apply_capacity_overrides.overrides",
        )
        snapshot = snapshot_by_provider.get(override.provider)
        if snapshot is None:
            raise SelectionContractValidationError(
                "apply_capacity_overrides: override target provider "
                + f"{override.provider!r} has no baseline snapshot"
            )
        if snapshot.status != "ok":
            raise SelectionContractValidationError(
                "apply_capacity_overrides: override target snapshot for "
                + f"provider {override.provider!r} has status "
                + f"{snapshot.status!r}; overrides require status 'ok' and "
                + "never turn unknown telemetry into fabricated known telemetry"
            )
        matches = [
            window
            for window in snapshot.windows
            if window.scope_id == override.scope_id
            and window.resource == override.resource
            and window.kind == override.kind
            and (override.window_id is None or window.window_id == override.window_id)
        ]
        if not matches:
            raise SelectionContractValidationError(
                "apply_capacity_overrides: override target window not found "
                + f"for provider {override.provider!r}, scope "
                + f"{override.scope_id!r}, resource {override.resource!r}, "
                + f"kind {override.kind!r}"
                + (
                    f", window_id {override.window_id!r}"
                    if override.window_id is not None
                    else ""
                )
            )
        if len(matches) > 1:
            raise SelectionContractValidationError(
                "apply_capacity_overrides: override matches "
                + f"{len(matches)} windows for provider {override.provider!r}, "
                + f"scope {override.scope_id!r}, resource "
                + f"{override.resource!r}, kind {override.kind!r}; supply an "
                + "exact disambiguating window_id"
            )
        window = matches[0]
        if window.used_percent is None or window.remaining_percent is None:
            raise SelectionContractValidationError(
                "apply_capacity_overrides: override target window "
                + f"{override.scope_id!r}/{override.resource!r}/"
                + f"{override.kind!r} has no known percentage pair; unknown "
                + "telemetry is never fabricated into known telemetry"
            )
        key = (
            override.provider,
            override.scope_id,
            override.resource,
            override.kind,
            window.window_id,
        )
        if key in overridden_window_keys:
            raise SelectionContractValidationError(
                "apply_capacity_overrides: the same window may not be "
                + f"overridden twice ({key})"
            )
        overridden_window_keys.add(key)

        remaining = override.remaining_percent
        new_window = replace(
            window, used_percent=100 - remaining, remaining_percent=remaining
        )
        base = result.get(override.provider, snapshot)
        new_windows = tuple(
            new_window if existing is window else existing for existing in base.windows
        )
        result[override.provider] = replace(base, windows=new_windows)

    return tuple(
        result.get(snapshot.provider, snapshot) for snapshot in snapshots
    )


# ── Simulation result and entry point ─────────────────────────────────────────


@dataclass(frozen=True)
class SimulationResult:
    """The baseline decision, the simulated decision and applied overrides.

    Both decisions are produced by the same authoritative ``select_model``
    core; rendering must clearly distinguish CURRENT from SIMULATED.
    """

    baseline: SelectionDecision
    simulated: SelectionDecision
    applied_overrides: SimulationOverrides

    def __post_init__(self) -> None:
        _ = _v_instance_of(self.baseline, SelectionDecision, "simulation_result.baseline")
        _ = _v_instance_of(
            self.simulated, SelectionDecision, "simulation_result.simulated"
        )
        _ = _v_instance_of(
            self.applied_overrides,
            SimulationOverrides,
            "simulation_result.applied_overrides",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline": self.baseline.to_dict(),
            "simulated": self.simulated.to_dict(),
            "applied_overrides": self.applied_overrides.to_dict(),
        }


def simulate_selection(
    *,
    catalog: ModelCatalog,
    requirement: TaskRequirement,
    policy: SelectorPolicy,
    snapshots: Sequence[CapacitySnapshot],
    evaluated_at: datetime,
    overrides: SimulationOverrides,
    replenishment_states: Sequence[ReplenishmentState] = (),
    profile_id: str | None = None,
    profile_policy_version: int | None = None,
) -> SimulationResult:
    """Run the baseline and the simulated decision through the SAME selector.

    The simulated run applies the overrides to copies of the baseline inputs
    and calls the identical :func:`selector.select_model` core — no copied
    ranking pipeline, no simulation-specific selector. Baseline inputs are
    never mutated.
    """
    _ = _v_instance_of(overrides, SimulationOverrides, "simulate_selection.overrides")

    baseline = select_model(
        catalog=catalog,
        requirement=requirement,
        policy=policy,
        snapshots=snapshots,
        evaluated_at=evaluated_at,
        replenishment_states=replenishment_states,
        profile_id=profile_id,
        profile_policy_version=profile_policy_version,
    )

    simulated_snapshots = (
        apply_capacity_overrides(snapshots, overrides.capacity_percentages)
        if overrides.capacity_percentages
        else tuple(snapshots)
    )
    simulated_policy = (
        overrides.selector_policy if overrides.selector_policy is not None else policy
    )
    simulated_replenishment = (
        overrides.replenishment_states
        if overrides.replenishment_states is not None
        else replenishment_states
    )
    simulated_at = (
        overrides.evaluated_at if overrides.evaluated_at is not None else evaluated_at
    )

    simulated = select_model(
        catalog=catalog,
        requirement=requirement,
        policy=simulated_policy,
        snapshots=simulated_snapshots,
        evaluated_at=simulated_at,
        replenishment_states=simulated_replenishment,
        profile_id=profile_id,
        profile_policy_version=profile_policy_version,
    )

    return SimulationResult(
        baseline=baseline, simulated=simulated, applied_overrides=overrides
    )


__all__ = [
    "CapacityPercentageOverride",
    "SimulationOverrides",
    "SimulationResult",
    "apply_capacity_overrides",
    "select_model",
    "simulate_selection",
]
