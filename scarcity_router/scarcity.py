"""Scarcity primitives for Scarcity Router (M2d, D-026; blend per D-037).

Pure, provider-independent, standard-library only. No filesystem, network,
environment, subprocess, clock or provider access: callers supply the model
catalog entry and the current normalized capacity snapshots explicitly, and
every function is deterministic over its arguments.

Implements the frozen scarcity parameters (D-026, finalizing D-005;
aggregation amended by D-037):

- the continuous integer scarcity penalty
  ``penalty_units = (100 - remaining_percent)^2`` on scale
  ``SCARCITY_PENALTY_SCALE = 10_000`` (normalized penalty =
  ``penalty_units / 10000``); ranking must use the integer units, never
  floats;
- the explanatory scarcity labels (explanation only, never ranking input);
- candidate capacity applicability over explicit catalog
  ``capacity_bindings`` — never inferred from ``window_id``, ``limitName``,
  ``normalModelSlug``, model names or provider aliases;
- the frozen window roles (D-037): ``kind: weekly`` windows are strategic,
  ``kind: five_hour`` and ``resource: time`` windows are tactical, and
  unknown-kind token windows are conservatively strategic;
- most-restrictive aggregation within each role across every applicable
  scope, then the frozen role blend ``effective = (a + 5*b) // 6`` —
  weekly quota is planned as five times the five-hour quota — with
  unrelated scopes ignored and missing roles defaulting to the other
  role's value;
- the explicit ``known`` / ``unknown`` / ``unavailable`` assessment states,
  including the distinction between explicit current exhaustion and
  untrustworthy telemetry.

This module does not rank candidates, does not choose a model and does not
implement ``select``; the selector combines these assessments with
capability eligibility, hard constraints and deterministic ranking. Unknown
scarcity has no numeric penalty: it is incomparable to numeric scarcity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, TypeVar, cast

from .capacity import CapacitySnapshot, CapacityWindow
from .errors import SelectionContractValidationError
from .selection_types import CapacityScopeRef, ModelCatalogEntry

# ── Frozen value sets ─────────────────────────────────────────────────────────

SCARCITY_PENALTY_SCALE = 10_000

SCARCITY_LABELS: frozenset[str] = frozenset({
    "plentiful",
    "normal",
    "scarce",
    "critical",
    "unavailable",
    "unknown",
})

SCARCITY_STATES: frozenset[str] = frozenset({
    "known",
    "unknown",
    "unavailable",
})

SCARCITY_REASON_CODES: frozenset[str] = frozenset({
    "capacity_bindings_unknown",
    "missing_provider_snapshot",
    "provider_snapshot_not_ok",
    "missing_scope_window",
    "window_percentage_unknown",
    "capacity_exhausted",
})

# Mirrors of the v3 capacity vocabularies (docs/capacity-model.md). The
# capacity module owns the authoritative sets; this module keeps local copies
# so its serialized contracts stay validated without a private import.
_RESOURCE_VALUES: frozenset[str] = frozenset({"tokens", "time", "unknown"})
_KIND_VALUES: frozenset[str] = frozenset({"five_hour", "weekly", "unknown"})

# ── Window roles and the strategic/tactical blend (D-037) ────────────────────

WINDOW_ROLE_STRATEGIC = "strategic"
WINDOW_ROLE_TACTICAL = "tactical"

WINDOW_ROLES: frozenset[str] = frozenset({
    WINDOW_ROLE_STRATEGIC,
    WINDOW_ROLE_TACTICAL,
})

# The owner's frozen planning assumption: one weekly bucket holds as much
# quota as five five-hour buckets, so the blend has six total units.
WEEKLY_TO_FIVE_HOUR_RATIO = 5
BLEND_UNIT_COUNT = WEEKLY_TO_FIVE_HOUR_RATIO + 1

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


def window_role(resource: str, kind: str) -> str:
    """The frozen D-037 role of one normalized window.

    ``kind: weekly`` is strategic — exhausting it blocks the provider for
    days. ``kind: five_hour`` and every ``resource: time`` window (the Z.ai
    ``TIME_LIMIT`` normalization carries ``kind: unknown``) are tactical —
    they gate whether work can happen right now and self-heal in hours.
    Unknown-kind token windows are conservatively strategic so the
    pre-D-037 most-restrictive outcome is preserved for exactly that shape.
    The role is a ranking-relevant classification of already-normalized
    fields; it is never inferred from window ids, model names or providers.
    """
    checked_kind = _v_enum(kind, _KIND_VALUES, "window_role.kind")
    checked_resource = _v_enum(resource, _RESOURCE_VALUES, "window_role.resource")
    if checked_kind == "weekly":
        return WINDOW_ROLE_STRATEGIC
    if checked_kind == "five_hour":
        return WINDOW_ROLE_TACTICAL
    if checked_resource == "time":
        return WINDOW_ROLE_TACTICAL
    return WINDOW_ROLE_STRATEGIC


def blended_effective_remaining_percent(
    tactical_remaining: int | None, strategic_remaining: int | None
) -> int:
    """The frozen D-037 blend of the two role representatives.

    ``a`` is the tactical and ``b`` the strategic representative (each the
    most restrictive known remaining of its role). With weekly quota planned
    as five times the five-hour quota:
    ``effective = (a + 5*b) // 6`` on integer units, 0..100, floor division.
    A role with no known evidence defaults its slot to the other role's
    value, so a single-role candidate blends to exactly its own percentage
    and no capacity is invented for the missing bucket. Both arguments
    ``None`` is a caller contract violation: a numeric assessment always has
    at least one role representative.
    """
    if tactical_remaining is None:
        if strategic_remaining is None:
            raise SelectionContractValidationError(
                "blended_effective_remaining_percent: at least one role "
                + "representative is required"
            )
        _ = _v_pct(strategic_remaining, "strategic_remaining")
        # Tactical-only evidence: the strategic value fills both slots.
        a = strategic_remaining
        b = strategic_remaining
    else:
        _ = _v_pct(tactical_remaining, "tactical_remaining")
        if strategic_remaining is None:
            # Strategic-only evidence: the tactical value fills both slots.
            a = tactical_remaining
            b = tactical_remaining
        else:
            _ = _v_pct(strategic_remaining, "strategic_remaining")
            a = tactical_remaining
            b = strategic_remaining
    units = a + WEEKLY_TO_FIVE_HOUR_RATIO * b
    return units // BLEND_UNIT_COUNT

# ── Validators (single source of truth for the M2d scarcity rules) ────────────

_T = TypeVar("_T")


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


def _v_pct(value: object, fld: str) -> int:
    """Integer percentage 0..100; booleans and floats are never percentages."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectionContractValidationError(
            f"{fld}: expected int, got {type(value).__name__} ({value!r})"
        )
    if value < 0 or value > 100:
        raise SelectionContractValidationError(
            f"{fld}: value {value} outside 0..100"
        )
    return value


def _v_reason_codes(value: object, fld: str) -> tuple[str, ...]:
    """Non-duplicate tuple of normalized scarcity reason codes."""
    if not isinstance(value, tuple):
        raise SelectionContractValidationError(
            f"{fld}: expected tuple, got {type(value).__name__}"
        )
    seen: set[str] = set()
    for item in cast("tuple[object, ...]", value):
        code = _v_enum(item, SCARCITY_REASON_CODES, fld)
        if code in seen:
            raise SelectionContractValidationError(
                f"{fld}: duplicate reason code {code!r}"
            )
        seen.add(code)
    # Canonical stored form: sorted codes make equality and serialized output
    # deterministic and independent of construction order.
    return tuple(sorted(seen))


def _v_window_id(value: object, fld: str) -> str:
    """Safe diagnostic window identifier (capacity safe-ID grammar)."""
    s = _v_str(value, fld)
    if not _SAFE_ID_RE.match(s):
        raise SelectionContractValidationError(
            f"{fld}: unsafe identifier {s!r}; "
            + "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
        )
    return s


# ── Continuous penalty and explanatory labels ─────────────────────────────────


def scarcity_penalty_units(remaining_percent: int) -> int:
    """The frozen continuous scarcity penalty in integer units.

    Exactly ``(100 - remaining_percent)^2`` on scale
    ``SCARCITY_PENALTY_SCALE``: remaining 100% costs 0 units and remaining
    0% costs 10000 units. Ranking must compare these integer units, never
    normalized floats. The penalty deliberately contains no linear or
    logarithmic term, no reset proximity, no provider price, no capability
    score, no model prestige and no provider preference.
    """
    pct = _v_pct(remaining_percent, "remaining_percent")
    used_percent = 100 - pct
    return used_percent * used_percent


def scarcity_label(remaining_percent: int) -> str:
    """The frozen explanatory scarcity label for one remaining percentage.

    Exact boundaries: ``>= 80`` plentiful, ``>= 50`` normal, ``>= 20``
    scarce, ``>= 1`` critical, ``== 0`` unavailable. Labels are explanatory
    only and never replace the continuous penalty: two candidates labelled
    ``scarce`` may still have different penalties. ``unknown`` is never
    produced from a number; it represents insufficient trustworthy capacity
    information and exists only as an assessment state.
    """
    pct = _v_pct(remaining_percent, "remaining_percent")
    if pct >= 80:
        return "plentiful"
    if pct >= 50:
        return "normal"
    if pct >= 20:
        return "scarce"
    if pct >= 1:
        return "critical"
    return "unavailable"


# ── Governing-window evidence ─────────────────────────────────────────────────


@dataclass(frozen=True)
class GoverningWindowEvidence:
    """Explanation-only reference to the governing capacity window.

    Carries normalized fields only (scope, resource, kind, remaining and the
    optional diagnostic window identifier). ``window_id`` is explanation
    data: it never participates in the penalty and is never parsed. This
    evidence never contains credentials, raw provider payloads or account
    data.
    """

    scope: CapacityScopeRef
    resource: str
    kind: str
    remaining_percent: int
    window_id: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "scope",
        "resource",
        "kind",
        "remaining_percent",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("window_id",)

    def __post_init__(self) -> None:
        _ = _v_instance_of(self.scope, CapacityScopeRef, "governing_window.scope")
        _ = _v_enum(self.resource, _RESOURCE_VALUES, "governing_window.resource")
        _ = _v_enum(self.kind, _KIND_VALUES, "governing_window.kind")
        _ = _v_pct(self.remaining_percent, "governing_window.remaining_percent")
        if self.window_id is not None:
            _ = _v_window_id(self.window_id, "governing_window.window_id")

    @classmethod
    def from_dict(cls, d: object) -> "GoverningWindowEvidence":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "governing_window")
        window_id: str | None = None
        if dd.get("window_id") is not None:
            window_id = _v_window_id(dd["window_id"], "governing_window.window_id")
        return cls(
            scope=CapacityScopeRef.from_dict(dd["scope"]),
            resource=_v_enum(
                dd["resource"], _RESOURCE_VALUES, "governing_window.resource"
            ),
            kind=_v_enum(dd["kind"], _KIND_VALUES, "governing_window.kind"),
            remaining_percent=_v_pct(
                dd["remaining_percent"], "governing_window.remaining_percent"
            ),
            window_id=window_id,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "scope": self.scope.to_dict(),
            "resource": self.resource,
            "kind": self.kind,
            "remaining_percent": self.remaining_percent,
        }
        if self.window_id is not None:
            out["window_id"] = self.window_id
        return out


# ── Scarcity assessment ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScarcityAssessment:
    """The normalized scarcity result for one candidate's bound capacity.

    States are explicit and mutually exclusive:

    - ``known`` — every applicable scope was completely assessed from
      trustworthy telemetry; ``penalty_units`` and
      ``effective_remaining_percent`` are numeric and ``label`` is one of
      the four numeric labels;
    - ``unavailable`` — an applicable known window is explicitly exhausted
      (``remaining_percent == 0``): penalty ``10000``, effective remaining
      ``0``, label ``unavailable``; this wins even when another bound scope
      is unknown;
    - ``unknown`` — at least one bound scope could not be completely
      assessed and no known window is exhausted; ``penalty_units``,
      ``effective_remaining_percent`` and ``governing_window`` are absent
      because unknown is incomparable to numeric scarcity (no numeric
      sentinel such as 0, 10000 or -1 exists).

    ``reason_codes`` is the normalized explanation vocabulary; an ``unknown``
    or ``unavailable`` state always carries at least one code.
    ``applicable_scopes`` always names the candidate's applicable capacity
    scopes: empty only when the capacity bindings themselves are unknown
    (``capacity_bindings = None``), which is exactly the state whose reason
    code is ``capacity_bindings_unknown`` alone. Known bindings are preserved
    even when their telemetry cannot be completely assessed — the failure is
    in the telemetry, never in the applicability, and the two unknown classes
    never mix. A ``known`` state carries no
    reason codes. The stored scopes and codes are canonical (sorted),
    so equality and serialization are deterministic and independent of
    construction order. Serialized unknown values are omitted, never
    represented as ``null``.

    Role evidence (D-037): a ``known`` state carries ``strategic_window``
    and/or ``tactical_window`` — the governing evidence of each role that
    had a known applicable window, each remaining role-consistent and never
    fabricated for a missing role. The ``governing_window`` is the strategic
    representative when one exists, else the tactical one, and the numeric
    fields must satisfy the frozen blend
    (``effective == blended(tactical, strategic)``) instead of the
    pre-D-037 governing-equality rule. An ``unavailable`` or ``unknown``
    state carries no role evidence (exhaustion is carried by the governing
    window; unknown telemetry has no numeric evidence at all). A serialized
    known assessment without role evidence fails validation loudly on this
    reader — accepted per D-037 because decision deserialization is not an
    input boundary.
    """

    state: str
    label: str
    penalty_units: int | None
    effective_remaining_percent: int | None
    applicable_scopes: tuple[CapacityScopeRef, ...]
    governing_window: GoverningWindowEvidence | None
    reason_codes: tuple[str, ...]
    # D-037 additive role evidence: present exactly when that role had a
    # known applicable window in a known assessment.
    strategic_window: GoverningWindowEvidence | None = None
    tactical_window: GoverningWindowEvidence | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "state",
        "label",
        "applicable_scopes",
        "reason_codes",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "penalty_units",
        "effective_remaining_percent",
        "governing_window",
        "strategic_window",
        "tactical_window",
    )

    def __post_init__(self) -> None:
        _ = _v_enum(self.state, SCARCITY_STATES, "scarcity_assessment.state")
        _ = _v_tuple_of(
            self.applicable_scopes,
            CapacityScopeRef,
            "scarcity_assessment.applicable_scopes",
        )
        codes = _v_reason_codes(
            self.reason_codes, "scarcity_assessment.reason_codes"
        )
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(
            self,
            "applicable_scopes",
            tuple(
                sorted(
                    self.applicable_scopes,
                    key=lambda s: (s.provider, s.scope_id),
                )
            ),
        )

        if self.state == "unknown":
            if self.penalty_units is not None or (
                self.effective_remaining_percent is not None
            ):
                raise SelectionContractValidationError(
                    "scarcity_assessment: unknown state must not carry a "
                    + "numeric penalty or effective remaining"
                )
            if self.governing_window is not None:
                raise SelectionContractValidationError(
                    "scarcity_assessment: unknown state has no governing "
                    + "window evidence"
                )
            if (
                self.strategic_window is not None
                or self.tactical_window is not None
            ):
                raise SelectionContractValidationError(
                    "scarcity_assessment: unknown state has no role window "
                    + "evidence (D-037)"
                )
            if self.label != "unknown":
                raise SelectionContractValidationError(
                    "scarcity_assessment: unknown state requires the exact "
                    + f"label 'unknown', got {self.label!r}"
                )
            if not self.reason_codes:
                raise SelectionContractValidationError(
                    "scarcity_assessment: unknown state requires at least "
                    + "one reason code"
                )
            if "capacity_exhausted" in self.reason_codes:
                raise SelectionContractValidationError(
                    "scarcity_assessment: unknown state must not claim "
                    + "capacity_exhausted"
                )
            # Applicability semantics: unknown applicability and unknown
            # telemetry are mutually exclusive, and each pins the scopes.
            # Empty applicable scopes are reserved for unknown bindings;
            # telemetry causes mean the bindings are known and must be
            # preserved.
            if "capacity_bindings_unknown" in self.reason_codes:
                if self.reason_codes != ("capacity_bindings_unknown",):
                    raise SelectionContractValidationError(
                        "scarcity_assessment: unknown applicability carries "
                        + "exactly the 'capacity_bindings_unknown' reason "
                        + "code; telemetry causes require known bindings"
                    )
                if self.applicable_scopes:
                    raise SelectionContractValidationError(
                        "scarcity_assessment: unknown applicability has no "
                        + "normalized applicable scopes"
                    )
            elif not self.applicable_scopes:
                raise SelectionContractValidationError(
                    "scarcity_assessment: unknown telemetry requires at "
                    + "least one applicable scope; empty applicable scopes "
                    + "are reserved for unknown applicability"
                )
            return

        # known / unavailable: fully numeric states.
        effective = _v_pct(
            self.effective_remaining_percent,
            "scarcity_assessment.effective_remaining_percent",
        )
        penalty = self.penalty_units
        if isinstance(penalty, bool) or not isinstance(penalty, int):
            raise SelectionContractValidationError(
                "scarcity_assessment.penalty_units: expected int, got "
                + f"{type(penalty).__name__}"
            )
        expected_penalty = scarcity_penalty_units(effective)
        if penalty != expected_penalty:
            raise SelectionContractValidationError(
                "scarcity_assessment: penalty_units "
                + f"{penalty} does not equal the frozen penalty "
                + f"({expected_penalty}) for effective remaining {effective}"
            )
        expected_label = scarcity_label(effective)
        if self.label != expected_label:
            raise SelectionContractValidationError(
                "scarcity_assessment: label "
                + f"{self.label!r} does not equal the frozen label "
                + f"{expected_label!r} for effective remaining {effective}"
            )
        # Runtime type guard before any attribute access: an ill-typed
        # direct value must fail with the contract error, never with an
        # ordinary AttributeError.
        governing = _v_instance_of(
            self.governing_window,
            GoverningWindowEvidence,
            "scarcity_assessment.governing_window",
        )
        # D-037 role evidence: shape, role consistency and (for known) blend
        # consistency replace the pre-D-037 governing-equality rule.
        strategic = self.strategic_window
        if strategic is not None:
            strategic = _v_instance_of(
                strategic,
                GoverningWindowEvidence,
                "scarcity_assessment.strategic_window",
            )
            if window_role(strategic.resource, strategic.kind) != (
                WINDOW_ROLE_STRATEGIC
            ):
                raise SelectionContractValidationError(
                    "scarcity_assessment: strategic_window "
                    + f"({strategic.resource}, {strategic.kind}) is not a "
                    + "strategic-role window"
                )
        tactical = self.tactical_window
        if tactical is not None:
            tactical = _v_instance_of(
                tactical,
                GoverningWindowEvidence,
                "scarcity_assessment.tactical_window",
            )
            if window_role(tactical.resource, tactical.kind) != (
                WINDOW_ROLE_TACTICAL
            ):
                raise SelectionContractValidationError(
                    "scarcity_assessment: tactical_window "
                    + f"({tactical.resource}, {tactical.kind}) is not a "
                    + "tactical-role window"
                )
        if self.state == "unavailable":
            # Exhaustion evidence is carried by the governing window; role
            # representatives would add nothing (everything is 0).
            if strategic is not None or tactical is not None:
                raise SelectionContractValidationError(
                    "scarcity_assessment: the unavailable state carries no "
                    + "role window evidence (D-037)"
                )
        else:  # known
            if strategic is None and tactical is None:
                raise SelectionContractValidationError(
                    "scarcity_assessment: a known assessment carries the "
                    + "D-037 role window evidence of at least one role"
                )
            # The both-None case is rejected above, so the strategic value
            # fills the slot whenever it exists (blend contract D-037).
            expected_governing = strategic if strategic is not None else tactical
            if governing != expected_governing:
                # Value equality: deserialization reconstructs equal but
                # distinct evidence objects.
                raise SelectionContractValidationError(
                    "scarcity_assessment: governing_window must equal the "
                    + "strategic representative when one exists, else the "
                    + "tactical representative (D-037)"
                )
            expected_effective = blended_effective_remaining_percent(
                None if tactical is None else tactical.remaining_percent,
                None if strategic is None else strategic.remaining_percent,
            )
            if effective != expected_effective:
                raise SelectionContractValidationError(
                    "scarcity_assessment: effective remaining "
                    + f"{effective} does not equal the frozen D-037 blend "
                    + f"({expected_effective}) of the role representatives"
                )
        if self.state == "known" and strategic is not None:
            # The governing window is the strategic representative: its
            # remaining is the strategic role minimum, which the blend
            # (validated above) may legitimately differ from.
            pass
        elif governing.remaining_percent != effective:
            raise SelectionContractValidationError(
                "scarcity_assessment: governing window remaining "
                + f"{governing.remaining_percent} does not "
                + f"match effective remaining {effective}"
            )
        if not self.applicable_scopes:
            raise SelectionContractValidationError(
                "scarcity_assessment: a numeric state requires at least "
                + "one applicable scope"
            )
        if governing.scope not in self.applicable_scopes:
            raise SelectionContractValidationError(
                "scarcity_assessment: governing window scope "
                + f"({governing.scope.provider}, {governing.scope.scope_id}) "
                + "must belong to the applicable scopes"
            )
        if self.state == "known":
            if effective == 0:
                raise SelectionContractValidationError(
                    "scarcity_assessment: state 'known' requires a nonzero "
                    + "effective remaining; use state 'unavailable'"
                )
            if self.reason_codes:
                raise SelectionContractValidationError(
                    "scarcity_assessment: a fully known state carries no "
                    + "reason codes"
                )
        else:  # unavailable
            if effective != 0:
                raise SelectionContractValidationError(
                    "scarcity_assessment: state 'unavailable' requires "
                    + "effective remaining 0"
                )
            if "capacity_exhausted" not in self.reason_codes:
                raise SelectionContractValidationError(
                    "scarcity_assessment: state 'unavailable' requires the "
                    + "'capacity_exhausted' reason code"
                )

    @classmethod
    def from_dict(cls, d: object) -> "ScarcityAssessment":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "scarcity_assessment")
        scopes_raw = dd["applicable_scopes"]
        if not isinstance(scopes_raw, list):
            raise SelectionContractValidationError(
                "scarcity_assessment.applicable_scopes: expected list, got "
                + f"{type(scopes_raw).__name__}"
            )
        scopes = tuple(
            CapacityScopeRef.from_dict(x) for x in cast("list[object]", scopes_raw)
        )
        codes_raw = dd["reason_codes"]
        if not isinstance(codes_raw, list):
            raise SelectionContractValidationError(
                "scarcity_assessment.reason_codes: expected list, got "
                + f"{type(codes_raw).__name__}"
            )
        codes = tuple(
            _v_enum(code, SCARCITY_REASON_CODES, "scarcity_assessment.reason_codes")
            for code in cast("list[object]", codes_raw)
        )
        governing: GoverningWindowEvidence | None = None
        if dd.get("governing_window") is not None:
            governing = GoverningWindowEvidence.from_dict(dd["governing_window"])
        strategic: GoverningWindowEvidence | None = None
        if dd.get("strategic_window") is not None:
            strategic = GoverningWindowEvidence.from_dict(dd["strategic_window"])
        tactical: GoverningWindowEvidence | None = None
        if dd.get("tactical_window") is not None:
            tactical = GoverningWindowEvidence.from_dict(dd["tactical_window"])
        raw_penalty = dd.get("penalty_units")
        penalty: int | None = None
        if raw_penalty is not None:
            if isinstance(raw_penalty, bool) or not isinstance(raw_penalty, int):
                raise SelectionContractValidationError(
                    "scarcity_assessment.penalty_units: expected int, got "
                    + f"{type(raw_penalty).__name__}"
                )
            penalty = raw_penalty
        raw_effective = dd.get("effective_remaining_percent")
        effective: int | None = None
        if raw_effective is not None:
            effective = _v_pct(
                raw_effective, "scarcity_assessment.effective_remaining_percent"
            )
        return cls(
            state=_v_enum(dd["state"], SCARCITY_STATES, "scarcity_assessment.state"),
            label=_v_enum(dd["label"], SCARCITY_LABELS, "scarcity_assessment.label"),
            penalty_units=penalty,
            effective_remaining_percent=effective,
            applicable_scopes=scopes,
            governing_window=governing,
            reason_codes=codes,
            strategic_window=strategic,
            tactical_window=tactical,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "state": self.state,
            "label": self.label,
        }
        if self.penalty_units is not None:
            out["penalty_units"] = self.penalty_units
        if self.effective_remaining_percent is not None:
            out["effective_remaining_percent"] = self.effective_remaining_percent
        out["applicable_scopes"] = [s.to_dict() for s in self.applicable_scopes]
        if self.governing_window is not None:
            out["governing_window"] = self.governing_window.to_dict()
        # D-037 additive role evidence: absent from serialized output when
        # the role had no known window.
        if self.strategic_window is not None:
            out["strategic_window"] = self.strategic_window.to_dict()
        if self.tactical_window is not None:
            out["tactical_window"] = self.tactical_window.to_dict()
        out["reason_codes"] = list(self.reason_codes)
        return out


# ── Candidate capacity applicability ──────────────────────────────────────────


def _canonical_window_key(
    scope: CapacityScopeRef, window: CapacityWindow
) -> tuple[str, str, str, str, str]:
    """Stable ordering key over normalized fields only (explanation tie-break)."""
    return (
        scope.provider,
        scope.scope_id,
        window.resource,
        window.kind,
        window.window_id or "",
    )


def _pick_governing(
    candidates: list[tuple[CapacityScopeRef, CapacityWindow, int]],
) -> tuple[CapacityScopeRef, CapacityWindow, int]:
    """Choose the governing window: maximum penalty, then canonical key.

    ``candidates`` carry the already-validated remaining percentage next to
    the window so the tie-break never re-derives optionality.
    """
    max_penalty = max(
        scarcity_penalty_units(remaining) for _, _, remaining in candidates
    )
    return min(
        (pair for pair in candidates if scarcity_penalty_units(pair[2]) == max_penalty),
        key=lambda pair: _canonical_window_key(pair[0], pair[1]),
    )


def _unknown_assessment(
    reasons: tuple[str, ...],
    applicable_scopes: tuple[CapacityScopeRef, ...] = (),
) -> ScarcityAssessment:
    """Build an unknown assessment, preserving the candidate's applicability.

    ``applicable_scopes`` must be empty only when the model's capacity
    bindings themselves are unknown (``capacity_bindings is None``). When
    the bindings are known but their telemetry cannot be completely
    assessed, the failure is in the telemetry — the scopes the candidate
    consumes are still known and must be preserved for explanation.
    """
    return ScarcityAssessment(
        state="unknown",
        label="unknown",
        penalty_units=None,
        effective_remaining_percent=None,
        applicable_scopes=applicable_scopes,
        governing_window=None,
        reason_codes=reasons,
    )


def _governing_evidence(
    scope: CapacityScopeRef, window: CapacityWindow, remaining: int
) -> GoverningWindowEvidence:
    return GoverningWindowEvidence(
        scope=scope,
        resource=window.resource,
        kind=window.kind,
        remaining_percent=remaining,
        window_id=window.window_id,
    )


def assess_scarcity(
    entry: ModelCatalogEntry,
    snapshots: Sequence[CapacitySnapshot],
) -> ScarcityAssessment:
    """Assess how scarce a candidate's bound subscription capacity is now.

    Pure and deterministic: the caller supplies the catalog entry and the
    current normalized snapshots; nothing is fetched, cached or clocked.

    Rules frozen by D-026, aggregation amended by D-037:

    - Applicability comes only from the entry's explicit
      ``capacity_bindings`` (exact ``CapacityScopeRef`` pairs); unknown
      bindings (``None``) yield an unknown assessment — never a guessed
      scope.
    - At most one snapshot per provider is accepted; duplicate provider
      snapshots are ambiguous and fail typed validation.
    - Only windows whose ``(snapshot.provider, scope_id)`` matches a binding
      participate. Unrelated scopes — including a 0% bucket — never
      penalize the candidate.
    - Every applicable window with a usable percentage pair participates
      (token and provider-normalized ``time`` windows alike; the resource is
      never reinterpreted). Within each D-037 role the most restrictive
      window represents the role: the strategic role is the ``min`` over its
      windows, likewise the tactical role, and the frozen blend
      ``effective = (tactical + 5 * strategic) // 6`` produces the single
      effective remaining, penalty and label. A role without known windows
      defaults its blend slot to the other role's value.
    - Any known applicable window at ``remaining_percent == 0`` makes the
      assessment explicitly ``unavailable`` (penalty 10000), even when
      another bound scope is unknown.
    - Otherwise any incompleteness — missing provider snapshot, snapshot
      whose status is not ``ok`` (telemetry is not trustworthy; this is
      never read as quota exhaustion), no matching window for a bound
      scope, or an applicable window without a percentage pair — makes the
      assessment ``unknown`` with no numeric penalty.

    The result carries normalized reason codes; governing-window evidence is
    explanation-only and tie-broken by a stable canonical key over
    normalized fields, independent of input order. For a known assessment
    the governing window is the strategic representative when one exists,
    else the tactical one (D-037), and both role representatives are
    carried as evidence where they exist.
    """
    _ = _v_instance_of(entry, ModelCatalogEntry, "assess_scarcity.entry")
    snapshot_by_provider: dict[str, CapacitySnapshot] = {}
    for snapshot in snapshots:
        _ = _v_instance_of(snapshot, CapacitySnapshot, "assess_scarcity.snapshots")
        if snapshot.provider in snapshot_by_provider:
            raise SelectionContractValidationError(
                "assess_scarcity.snapshots: duplicate provider snapshot for "
                + f"{snapshot.provider!r}; at most one snapshot per provider "
                + "is permitted per assessment"
            )
        snapshot_by_provider[snapshot.provider] = snapshot

    bindings = entry.capacity_bindings
    if bindings is None:
        return _unknown_assessment(("capacity_bindings_unknown",))

    reason_codes: set[str] = set()
    # (scope, window, remaining) — remaining is validated non-None here so the
    # aggregation below never re-derives it.
    known_windows: list[tuple[CapacityScopeRef, CapacityWindow, int]] = []
    for binding in bindings:
        snapshot = snapshot_by_provider.get(binding.provider)
        if snapshot is None:
            reason_codes.add("missing_provider_snapshot")
            continue
        if snapshot.status != "ok":
            reason_codes.add("provider_snapshot_not_ok")
            continue
        matched = False
        for window in snapshot.windows:
            if window.scope_id != binding.scope_id:
                continue
            matched = True
            if window.used_percent is None or window.remaining_percent is None:
                reason_codes.add("window_percentage_unknown")
            else:
                known_windows.append((binding, window, window.remaining_percent))
        if not matched:
            reason_codes.add("missing_scope_window")

    exhausted = [pair for pair in known_windows if pair[2] == 0]
    if exhausted:
        governing_scope, governing, _ = _pick_governing(exhausted)
        return ScarcityAssessment(
            state="unavailable",
            label="unavailable",
            penalty_units=10000,
            effective_remaining_percent=0,
            applicable_scopes=bindings,
            governing_window=_governing_evidence(governing_scope, governing, 0),
            reason_codes=tuple(sorted(reason_codes | {"capacity_exhausted"})),
        )

    if reason_codes:
        return _unknown_assessment(tuple(sorted(reason_codes)), bindings)

    if not known_windows:  # structurally unreachable for known bindings
        raise SelectionContractValidationError(
            "assess_scarcity: no applicable window evidence and no "
            + "incompleteness reason; internal contract violation"
        )

    # D-037: partition the known windows by frozen role, then let the
    # most-restrictive window of each role represent it (canonical tie-break
    # via the shared governing picker).
    strategic_evidence: tuple[CapacityScopeRef, CapacityWindow, int] | None = None
    tactical_evidence: tuple[CapacityScopeRef, CapacityWindow, int] | None = None
    strategic_pool = [
        triplet
        for triplet in known_windows
        if window_role(triplet[1].resource, triplet[1].kind)
        == WINDOW_ROLE_STRATEGIC
    ]
    tactical_pool = [
        triplet
        for triplet in known_windows
        if window_role(triplet[1].resource, triplet[1].kind)
        == WINDOW_ROLE_TACTICAL
    ]
    if strategic_pool:
        strategic_evidence = _pick_governing(strategic_pool)
    if tactical_pool:
        tactical_evidence = _pick_governing(tactical_pool)
    strategic_remaining = (
        strategic_evidence[2] if strategic_evidence is not None else None
    )
    tactical_remaining = (
        tactical_evidence[2] if tactical_evidence is not None else None
    )
    effective = blended_effective_remaining_percent(
        tactical_remaining, strategic_remaining
    )
    # Build each evidence object exactly once: the governing window shares
    # the object identity of its role representative (validated by the
    # assessment contract).
    strategic_evidence_out = (
        _governing_evidence(
            strategic_evidence[0], strategic_evidence[1], strategic_evidence[2]
        )
        if strategic_evidence is not None
        else None
    )
    tactical_evidence_out = (
        _governing_evidence(
            tactical_evidence[0], tactical_evidence[1], tactical_evidence[2]
        )
        if tactical_evidence is not None
        else None
    )
    governing_evidence = (
        strategic_evidence_out
        if strategic_evidence_out is not None
        else tactical_evidence_out
    )
    if governing_evidence is None:  # roles partition the non-empty windows
        raise SelectionContractValidationError(
            "assess_scarcity: no role evidence despite known windows; "
            + "internal contract violation"
        )
    return ScarcityAssessment(
        state="known",
        label=scarcity_label(effective),
        penalty_units=scarcity_penalty_units(effective),
        effective_remaining_percent=effective,
        applicable_scopes=bindings,
        governing_window=governing_evidence,
        reason_codes=(),
        strategic_window=strategic_evidence_out,
        tactical_window=tactical_evidence_out,
    )
