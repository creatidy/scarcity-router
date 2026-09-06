"""Deterministic balanced selector for Scarcity Router (M2e, D-027).

Pure, provider-independent, standard-library only. No filesystem, network,
environment, subprocess, clock or provider access: callers supply the model
catalog, the resolved task requirement, the selector policy, the current
normalized capacity snapshots, the replenishment observations and one
timezone-aware evaluation instant explicitly, and every function is
deterministic over its arguments.

Implements the frozen M2e selection semantics (D-027):

- ``SelectorPolicy``: the selector-level policy wrapper — exactly the
  ``balanced`` mode in this slice, the frozen M2d ``UserPolicy`` resource
  policy and an explicit optional preference order that is a late tie-break
  only (never inferred from classes, profiles, providers, catalog order or
  display names);
- ``tighten_requirement``: the pure monotone merge of a calibrated profile
  requirement with an explicit requirement — the explicit input may tighten
  but never loosen the base, and an attempted relaxation is a validation
  error, never a silent ``max()``;
- the hard-constraint evaluator over the frozen nine-member vocabulary with
  tri-state hard-property semantics (unknown is never false) and exact
  privacy-tag semantics (no hierarchy, no cloud/local assumptions);
- capability sufficiency over ``effective_rating`` (HumanOverride-aware)
  with no averaging and no cross-dimension compensation, plus the frozen
  ``balanced`` capability margin: the nonnegative sum of surplus over
  required dimensions only;
- the candidate pipeline: blackout -> hard constraints -> capability ->
  scarcity/unknown-capacity policy -> replenishment visibility -> applicable
  reservations -> ranking; replenishment never changes current eligibility;
- the exact ``balanced`` ranking order: known capacity before degraded
  unknown (no numeric unknown sentinel), then the integer scarcity penalty,
  then the capability margin, then the explicit preference order, then
  stable ``(provider, model, variant)`` identity;
- the structured ``CandidateEvaluation`` and ``SelectionDecision`` output
  contracts, including structured no-solution results with closest
  candidates (stage progress only, capped at 3) and recoverable candidates.

This module does not read files, does not collect capacity and does not
simulate: the application layer composes it (``selection_app.py``), and
``simulation.py`` re-runs this same selector over overridden inputs. The
output contracts are serialize-only (``to_dict``); deserialization of
decisions is not an input boundary in this slice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, TypeVar, cast

from .capacity import CapacitySnapshot
from .errors import SelectionContractValidationError
from .policy import (
    REPLENISHMENT_MODE_ADVISORY,
    UNKNOWN_CAPACITY_MODE_DEGRADED,
    BlackoutDecision,
    ReplenishmentDecision,
    ReplenishmentState,
    ReservationDecision,
    UnknownCapacityDecision,
    UserPolicy,
    apply_replenishment_mode,
    evaluate_reservation,
)
from .scarcity import ScarcityAssessment, assess_scarcity
from .selection_types import (
    CAPABILITY_DIMENSIONS,
    TASK_LEVELS,
    CapabilityAssessment,
    CapabilityAssessments,
    CapabilityMinima,
    HardConstraints,
    ModelCatalog,
    ModelCatalogEntry,
    ModelIdentity,
    ModelRef,
    TaskRequirement,
)

# ── Frozen value sets ─────────────────────────────────────────────────────────

SELECTOR_MODE_BALANCED = "balanced"

SELECTOR_MODES: frozenset[str] = frozenset({SELECTOR_MODE_BALANCED})

# Closed exclusion-stage vocabulary; the tuple order is the stage-progress
# order used by the deterministic closest-candidate rule (a later stage is
# closer to a solution).
EXCLUSION_STAGES: tuple[str, ...] = (
    "policy_blackout",
    "hard_constraint",
    "capability",
    "capacity",
    "reservation",
)

_STAGE_PROGRESS: dict[str, int] = {
    stage: index for index, stage in enumerate(EXCLUSION_STAGES)
}

# Per-stage primary reason codes for excluded candidates.
_STAGE_REASONS: dict[str, frozenset[str]] = {
    "policy_blackout": frozenset({"policy_blocked"}),
    "hard_constraint": frozenset({"hard_constraint_failed"}),
    "capability": frozenset({"capability_failed"}),
    "capacity": frozenset({"capacity_unavailable", "capacity_unknown_blocked"}),
    "reservation": frozenset({"reservation_blocked", "reservation_unknown"}),
}

# The normalized decision/candidate reason vocabulary (D-027). Dimension and
# model details never enter reason-code strings; structured failure records
# carry them.
SELECTION_REASON_CODES: frozenset[str] = frozenset({
    "selected_balanced",
    "no_eligible_candidate",
    "selected_degraded_capacity",
    "policy_blocked",
    "hard_constraint_failed",
    "capability_failed",
    "capacity_unavailable",
    "capacity_unknown_blocked",
    "reservation_blocked",
    "reservation_unknown",
    "replenishment_recoverable",
})

_HARD_CONSTRAINT_NAMES: frozenset[str] = frozenset({
    "minimum_input_context_tokens",
    "minimum_output_tokens",
    "requires_tool_use",
    "requires_vision",
    "requires_reasoning_mode",
    "required_provider",
    "required_model",
    "required_variant",
    "privacy_constraint",
})

_HARD_FAILURE_REASONS: frozenset[str] = frozenset({
    "unknown",
    "insufficient",
    "unsupported",
    "mismatch",
    "privacy_unknown",
    "privacy_unsatisfied",
})

_CAPABILITY_FAILURE_REASONS: frozenset[str] = frozenset({
    "unknown",
    "below_minimum",
})

# ── Validators (single source of truth for the M2e rules) ─────────────────────

_T = TypeVar("_T")


def _v_str(value: object, fld: str) -> str:
    if not isinstance(value, str):
        raise SelectionContractValidationError(
            f"{fld}: expected str, got {type(value).__name__}"
        )
    return value


def _v_nonempty_str(value: object, fld: str) -> str:
    s = _v_str(value, fld)
    if not s:
        raise SelectionContractValidationError(f"{fld}: must be non-empty")
    return s


def _v_enum(value: object, allowed: frozenset[str], fld: str) -> str:
    s = _v_str(value, fld)
    if s not in allowed:
        raise SelectionContractValidationError(
            f"{fld}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def _v_int(value: object, fld: str, *, lo: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectionContractValidationError(
            f"{fld}: expected int, got {type(value).__name__} ({value!r})"
        )
    if lo is not None and value < lo:
        raise SelectionContractValidationError(f"{fld}: value {value} < {lo}")
    return value


def _v_bool(value: object, fld: str) -> bool:
    if not isinstance(value, bool):
        raise SelectionContractValidationError(
            f"{fld}: expected bool, got {type(value).__name__} ({value!r})"
        )
    return value


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
        raise SelectionContractValidationError(f"{label}: unknown keys {sorted(extra)}")
    missing = [key for key in required if key not in m]
    if missing:
        raise SelectionContractValidationError(
            f"{label}: missing required keys {sorted(missing)}"
        )
    return m


def _v_selection_reason_codes(value: object, fld: str) -> tuple[str, ...]:
    """Non-duplicate tuple of normalized selection reason codes (sorted)."""
    if not isinstance(value, tuple):
        raise SelectionContractValidationError(
            f"{fld}: expected tuple, got {type(value).__name__}"
        )
    seen: set[str] = set()
    for item in cast("tuple[object, ...]", value):
        code = _v_enum(item, SELECTION_REASON_CODES, fld)
        if code in seen:
            raise SelectionContractValidationError(f"{fld}: duplicate reason code {code!r}")
        seen.add(code)
    return tuple(sorted(seen))


def canonical_instant(instant: datetime) -> str:
    """Canonical UTC millisecond rendering of one aware instant."""
    checked = _v_aware_datetime(instant, "instant")
    return (
        checked.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ── SelectorPolicy ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SelectorPolicy:
    """The selector-level policy wrapper (D-027).

    Separate from the frozen M2d ``UserPolicy``: ``mode`` selects the ranking
    behavior (exactly ``balanced`` in this slice), ``resource_policy`` is the
    ``UserPolicy`` whose unknown-capacity mode, reservations, blackouts and
    replenishment visibility the selector enforces, and ``preference_order``
    is the explicit user preference used only as the fourth ranking
    tie-break. The preference is never inferred from model classes, profile
    names, provider names, catalog entry order or display names, and a
    preference entry absent from a particular catalog is harmless and
    ignored for that catalog.
    """

    mode: str
    resource_policy: UserPolicy
    preference_order: tuple[ModelIdentity, ...] = ()

    _REQUIRED: ClassVar[tuple[str, ...]] = ("mode", "resource_policy")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("preference_order",)

    def __post_init__(self) -> None:
        _ = _v_enum(self.mode, SELECTOR_MODES, "selector_policy.mode")
        _ = _v_instance_of(
            self.resource_policy, UserPolicy, "selector_policy.resource_policy"
        )
        _ = _v_tuple_of(
            self.preference_order, ModelIdentity, "selector_policy.preference_order"
        )
        seen: set[tuple[str, str, str]] = set()
        for entry in self.preference_order:
            key = (entry.provider, entry.model, entry.variant)
            if key in seen:
                raise SelectionContractValidationError(
                    "selector_policy.preference_order: duplicate entry "
                    + f"{key}"
                )
            seen.add(key)

    @classmethod
    def from_dict(cls, d: object) -> "SelectorPolicy":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "selector_policy")
        raw_preference = dd.get("preference_order")
        preference: tuple[ModelIdentity, ...] = ()
        if raw_preference is not None:
            if not isinstance(raw_preference, list):
                raise SelectionContractValidationError(
                    "selector_policy.preference_order: expected list, got "
                    + f"{type(raw_preference).__name__}"
                )
            preference = tuple(
                ModelIdentity.from_dict(x) for x in cast("list[object]", raw_preference)
            )
        return cls(
            mode=_v_enum(dd["mode"], SELECTOR_MODES, "selector_policy.mode"),
            resource_policy=UserPolicy.from_dict(dd["resource_policy"]),
            preference_order=preference,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "resource_policy": self.resource_policy.to_dict(),
            "preference_order": [entry.to_dict() for entry in self.preference_order],
        }


def neutral_selector_policy() -> SelectorPolicy:
    """The documented neutral default application policy.

    Not a checked-in personal quota policy: no reservation threshold, no
    blackout, no hidden provider preference, no default Sol reservation.
    Unknown capacity is handled ``degraded`` and replenishment visibility is
    ``advisory`` — the two M2d defaults that keep the selector honest without
    encoding any owner-specific preservation rule.
    """
    return SelectorPolicy(
        mode=SELECTOR_MODE_BALANCED,
        resource_policy=UserPolicy(
            policy_version=1,
            unknown_capacity_mode=UNKNOWN_CAPACITY_MODE_DEGRADED,
            replenishment_mode=REPLENISHMENT_MODE_ADVISORY,
            reservations=(),
            blackouts=(),
        ),
        preference_order=(),
    )


# ── Requirement tightening (pure monotone merge) ──────────────────────────────


def _tighten_minimum(
    base: int | None, explicit: int | None, name: str
) -> int | None:
    """Monotone merge for one optional numeric minimum."""
    if explicit is None:
        return base
    if base is None or explicit >= base:
        return explicit
    raise SelectionContractValidationError(
        f"tighten_requirement: explicit {name}={explicit} would loosen the "
        + f"base minimum {base}; relaxation is rejected, never silently "
        + "clamped"
    )


def _tighten_identity(
    base: str | None, explicit: str | None, name: str
) -> str | None:
    """Monotone merge for one optional exact-identity constraint."""
    if explicit is None:
        return base
    if base is None or explicit == base:
        return explicit
    raise SelectionContractValidationError(
        f"tighten_requirement: explicit {name}={explicit!r} contradicts the "
        + f"base constraint {base!r}"
    )


def _tighten_model_ref(
    base: ModelRef | None, explicit: ModelRef | None, name: str
) -> ModelRef | None:
    if explicit is None:
        return base
    if base is None:
        return explicit
    base_key = (base.provider, base.model)
    explicit_key = (explicit.provider, explicit.model)
    if base_key != explicit_key:
        raise SelectionContractValidationError(
            f"tighten_requirement: explicit {name}={explicit_key} contradicts "
            + f"the base constraint {base_key}"
        )
    return base


def tighten_requirement(base: TaskRequirement, explicit: TaskRequirement) -> TaskRequirement:
    """Merge an explicit requirement into a calibrated profile requirement.

    Pure and monotone: the explicit requirement may tighten the base but may
    never loosen it. A lower explicit task level, a lower explicit capability
    minimum or a lower explicit numeric hard minimum is a validation error —
    never a silent ``max()``. Booleans combine monotonically with OR
    (``False`` is a no-op and is never interpreted as "must not support").
    Identity/privacy constraints must agree exactly or be newly supplied.
    Final construction through ``HardConstraints`` still validates
    provider/model contradictions.
    """
    _ = _v_instance_of(base, TaskRequirement, "tighten_requirement.base")
    _ = _v_instance_of(explicit, TaskRequirement, "tighten_requirement.explicit")

    if (
        TASK_LEVELS.index(explicit.task_level) < TASK_LEVELS.index(base.task_level)
    ):
        raise SelectionContractValidationError(
            f"tighten_requirement: explicit task level {explicit.task_level!r} "
            + f"would loosen the base level {base.task_level!r}"
        )

    base_minima = base.capability_minima
    explicit_minima = explicit.capability_minima
    capability_minima = CapabilityMinima(
        reasoning=_tighten_minimum(
            base_minima.reasoning, explicit_minima.reasoning, "capability_minimum.reasoning"
        ),
        coding=_tighten_minimum(
            base_minima.coding, explicit_minima.coding, "capability_minimum.coding"
        ),
        scientific_methodological=_tighten_minimum(
            base_minima.scientific_methodological,
            explicit_minima.scientific_methodological,
            "capability_minimum.scientific_methodological",
        ),
        writing_editorial=_tighten_minimum(
            base_minima.writing_editorial,
            explicit_minima.writing_editorial,
            "capability_minimum.writing_editorial",
        ),
        tool_use=_tighten_minimum(
            base_minima.tool_use, explicit_minima.tool_use, "capability_minimum.tool_use"
        ),
        translation_multilingual=_tighten_minimum(
            base_minima.translation_multilingual,
            explicit_minima.translation_multilingual,
            "capability_minimum.translation_multilingual",
        ),
    )

    base_hard = base.hard_constraints
    explicit_hard = explicit.hard_constraints
    hard_constraints = HardConstraints(
        minimum_input_context_tokens=_tighten_minimum(
            base_hard.minimum_input_context_tokens,
            explicit_hard.minimum_input_context_tokens,
            "hard_constraint.minimum_input_context_tokens",
        ),
        minimum_output_tokens=_tighten_minimum(
            base_hard.minimum_output_tokens,
            explicit_hard.minimum_output_tokens,
            "hard_constraint.minimum_output_tokens",
        ),
        requires_tool_use=base_hard.requires_tool_use or explicit_hard.requires_tool_use,
        requires_vision=base_hard.requires_vision or explicit_hard.requires_vision,
        requires_reasoning_mode=(
            base_hard.requires_reasoning_mode or explicit_hard.requires_reasoning_mode
        ),
        required_provider=_tighten_identity(
            base_hard.required_provider,
            explicit_hard.required_provider,
            "hard_constraint.required_provider",
        ),
        required_model=_tighten_model_ref(
            base_hard.required_model,
            explicit_hard.required_model,
            "hard_constraint.required_model",
        ),
        required_variant=_tighten_identity(
            base_hard.required_variant,
            explicit_hard.required_variant,
            "hard_constraint.required_variant",
        ),
        privacy_constraint=_tighten_identity(
            base_hard.privacy_constraint,
            explicit_hard.privacy_constraint,
            "hard_constraint.privacy_constraint",
        ),
    )

    return TaskRequirement(
        task_level=explicit.task_level,
        capability_minima=capability_minima,
        hard_constraints=hard_constraints,
    )


# ── Hard-constraint evaluation ────────────────────────────────────────────────


@dataclass(frozen=True)
class HardConstraintFailure:
    """One structured hard-constraint failure for one candidate.

    ``reason`` is from the closed vocabulary: ``unknown`` (candidate value
    not known), ``insufficient`` (numeric allowance below the requirement),
    ``unsupported`` (required feature explicitly false), ``mismatch``
    (exact identity mismatch), ``privacy_unknown`` (privacy tags not
    established) or ``privacy_unsatisfied`` (required tag absent). Values are
    deterministic renderings of typed catalog/requirement values — never
    free-form provider payload text.
    """

    constraint: str
    reason: str
    required_value: str
    actual_value: str | None

    def __post_init__(self) -> None:
        _ = _v_enum(self.constraint, _HARD_CONSTRAINT_NAMES, "hard_constraint_failure.constraint")
        _ = _v_enum(self.reason, _HARD_FAILURE_REASONS, "hard_constraint_failure.reason")
        _ = _v_nonempty_str(
            self.required_value, "hard_constraint_failure.required_value"
        )
        if self.actual_value is not None:
            _ = _v_str(self.actual_value, "hard_constraint_failure.actual_value")

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "constraint": self.constraint,
            "reason": self.reason,
            "required_value": self.required_value,
        }
        if self.actual_value is not None:
            out["actual_value"] = self.actual_value
        return out


def evaluate_hard_constraints(
    hard: HardConstraints, entry: ModelCatalogEntry
) -> tuple[HardConstraintFailure, ...]:
    """Evaluate the frozen nine-member hard-constraint vocabulary for one candidate.

    Tri-state hard properties are honest: ``None`` (unknown) never passes a
    required feature and is never read as ``False``; an explicit ``False``
    fails as ``unsupported``. ``False`` requirements pass regardless of the
    candidate property — a requirement is never "must not support". Identity
    constraints match exactly on typed identity, never on display names.
    ``privacy_constraint`` is an exact required privacy tag: unknown tags
    fail as ``privacy_unknown``, a known tag set without the required tag
    fails as ``privacy_unsatisfied``. No privacy hierarchy and no
    cloud/local behavior is invented.
    """
    _ = _v_instance_of(hard, HardConstraints, "evaluate_hard_constraints.hard")
    _ = _v_instance_of(entry, ModelCatalogEntry, "evaluate_hard_constraints.entry")
    failures: list[HardConstraintFailure] = []
    props = entry.hard_properties
    identity = entry.identity

    for name, required, actual in (
        (
            "minimum_input_context_tokens",
            hard.minimum_input_context_tokens,
            props.input_context_tokens,
        ),
        (
            "minimum_output_tokens",
            hard.minimum_output_tokens,
            props.output_tokens,
        ),
    ):
        if required is None:
            continue
        if actual is None:
            failures.append(
                HardConstraintFailure(name, "unknown", str(required), None)
            )
        elif actual < required:
            failures.append(
                HardConstraintFailure(name, "insufficient", str(required), str(actual))
            )

    for name, required, supported in (
        ("requires_tool_use", hard.requires_tool_use, props.supports_tool_use),
        ("requires_vision", hard.requires_vision, props.supports_vision),
        (
            "requires_reasoning_mode",
            hard.requires_reasoning_mode,
            props.supports_reasoning_mode,
        ),
    ):
        if not required:
            continue
        if supported is None:
            failures.append(HardConstraintFailure(name, "unknown", "true", None))
        elif supported is False:
            failures.append(
                HardConstraintFailure(name, "unsupported", "true", "false")
            )

    if (
        hard.required_provider is not None
        and identity.provider != hard.required_provider
    ):
        failures.append(
            HardConstraintFailure(
                "required_provider",
                "mismatch",
                hard.required_provider,
                identity.provider,
            )
        )

    if hard.required_model is not None:
        required_key = (hard.required_model.provider, hard.required_model.model)
        actual_key = (identity.provider, identity.model)
        if actual_key != required_key:
            failures.append(
                HardConstraintFailure(
                    "required_model",
                    "mismatch",
                    "/".join(required_key),
                    "/".join(actual_key),
                )
            )

    if hard.required_variant is not None and identity.variant != hard.required_variant:
        failures.append(
            HardConstraintFailure(
                "required_variant",
                "mismatch",
                hard.required_variant,
                identity.variant,
            )
        )

    if hard.privacy_constraint is not None:
        tags = props.privacy_tags
        if tags is None:
            failures.append(
                HardConstraintFailure(
                    "privacy_constraint", "privacy_unknown", hard.privacy_constraint, None
                )
            )
        elif hard.privacy_constraint not in tags:
            failures.append(
                HardConstraintFailure(
                    "privacy_constraint",
                    "privacy_unsatisfied",
                    hard.privacy_constraint,
                    ",".join(tags),
                )
            )

    return tuple(failures)


# ── Capability sufficiency and margin ─────────────────────────────────────────


@dataclass(frozen=True)
class CapabilityFailure:
    """One structured capability-sufficiency failure for one candidate.

    ``reason`` is ``unknown`` (the required dimension has no known effective
    rating) or ``below_minimum`` (the effective rating is below the required
    minimum). Dimensions are never averaged and never compensate each other.
    """

    dimension: str
    required_rating: int
    actual_rating: int | None
    reason: str

    def __post_init__(self) -> None:
        if self.dimension not in CAPABILITY_DIMENSIONS:
            raise SelectionContractValidationError(
                "capability_failure.dimension: value "
                + f"{self.dimension!r} not in allowed set "
                + f"{list(CAPABILITY_DIMENSIONS)}"
            )
        _ = _v_int(self.required_rating, "capability_failure.required_rating", lo=1)
        if self.actual_rating is not None:
            _ = _v_int(
                self.actual_rating, "capability_failure.actual_rating", lo=1
            )
        _ = _v_enum(
            self.reason, _CAPABILITY_FAILURE_REASONS, "capability_failure.reason"
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "dimension": self.dimension,
            "required_rating": self.required_rating,
            "reason": self.reason,
        }
        if self.actual_rating is not None:
            out["actual_rating"] = self.actual_rating
        return out


def _minima_items(minima: CapabilityMinima) -> tuple[tuple[str, int | None], ...]:
    """The six dimension minima as explicit (name, value) pairs."""
    return (
        ("reasoning", minima.reasoning),
        ("coding", minima.coding),
        ("scientific_methodological", minima.scientific_methodological),
        ("writing_editorial", minima.writing_editorial),
        ("tool_use", minima.tool_use),
        ("translation_multilingual", minima.translation_multilingual),
    )


def _assessment_items(
    capabilities: CapabilityAssessments,
) -> tuple[tuple[str, CapabilityAssessment], ...]:
    """The six dimension assessments as explicit (name, assessment) pairs."""
    return (
        ("reasoning", capabilities.reasoning),
        ("coding", capabilities.coding),
        ("scientific_methodological", capabilities.scientific_methodological),
        ("writing_editorial", capabilities.writing_editorial),
        ("tool_use", capabilities.tool_use),
        ("translation_multilingual", capabilities.translation_multilingual),
    )


def evaluate_capability_sufficiency(
    minima: CapabilityMinima, capabilities: CapabilityAssessments
) -> tuple[CapabilityFailure, ...]:
    """Evaluate every required capability dimension for one candidate.

    Uses ``CapabilityAssessment.effective_rating`` (never the raw rating), so
    a valid ``HumanOverride`` participates without mutating catalog values.
    An unknown effective rating on a required dimension fails as ``unknown``
    — the strict initial M2e policy: an unknown rating never passes.
    """
    _ = _v_instance_of(minima, CapabilityMinima, "evaluate_capability_sufficiency.minima")
    _ = _v_instance_of(
        capabilities,
        CapabilityAssessments,
        "evaluate_capability_sufficiency.capabilities",
    )
    assessments = dict(_assessment_items(capabilities))
    failures: list[CapabilityFailure] = []
    for dimension, required in _minima_items(minima):
        if required is None:
            continue
        effective = assessments[dimension].effective_rating
        if effective is None:
            failures.append(
                CapabilityFailure(dimension, required, None, "unknown")
            )
        elif effective < required:
            failures.append(
                CapabilityFailure(dimension, required, effective, "below_minimum")
            )
    return tuple(failures)


def capability_margin(
    minima: CapabilityMinima, capabilities: CapabilityAssessments
) -> int:
    """The frozen ``balanced`` capability margin for a sufficient candidate.

    Exactly ``SUM(effective_rating - required_minimum)`` over every capability
    dimension that has an explicit minimum; unrequired dimensions do not
    participate. All contributions are nonnegative because the candidate
    already passed sufficiency. No averaging, no weights, no task-level
    bonus, no confidence multiplier, no benchmark score and no hard-property
    margin. A requirement with no capability minima yields 0.
    """
    _ = _v_instance_of(minima, CapabilityMinima, "capability_margin.minima")
    _ = _v_instance_of(
        capabilities, CapabilityAssessments, "capability_margin.capabilities"
    )
    assessments = dict(_assessment_items(capabilities))
    margin = 0
    for dimension, required in _minima_items(minima):
        if required is None:
            continue
        effective = assessments[dimension].effective_rating
        if effective is None:
            raise SelectionContractValidationError(
                "capability_margin: required dimension "
                + f"{dimension!r} has no effective rating; margin is only "
                + "defined for capability-sufficient candidates"
            )
        margin += effective - required
    return margin


# ── Candidate evaluation ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class CandidateEvaluation:
    """The structured deterministic per-candidate selector result.

    ``eligible`` and ``exclusion_stage`` are exact complements: an excluded
    candidate always names its single primary stage from the closed
    ``EXCLUSION_STAGES`` vocabulary and carries at least one normalized
    reason code whose primary code belongs to that stage; an eligible
    candidate carries no reason codes and no failure records. Evaluation
    details are carried only where they were safely computed — later stages
    are not run after a hard/capability failure merely to populate fields.
    ``degraded`` mirrors a ``degraded`` unknown-capacity decision (unknown
    capacity that may remain conditionally usable); it never fabricates a
    numeric scarcity value.
    """

    identity: ModelIdentity
    display_name: str
    eligible: bool
    degraded: bool = False
    exclusion_stage: str | None = None
    hard_constraint_failures: tuple[HardConstraintFailure, ...] = ()
    capability_failures: tuple[CapabilityFailure, ...] = ()
    capability_margin: int | None = None
    blackout_decision: BlackoutDecision | None = None
    scarcity_assessment: ScarcityAssessment | None = None
    unknown_capacity_decision: UnknownCapacityDecision | None = None
    reservation_decisions: tuple[ReservationDecision, ...] = ()
    replenishment_evaluations: tuple[ReplenishmentDecision, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _ = _v_instance_of(self.identity, ModelIdentity, "candidate_evaluation.identity")
        _ = _v_nonempty_str(self.display_name, "candidate_evaluation.display_name")
        _ = _v_bool(self.eligible, "candidate_evaluation.eligible")
        _ = _v_bool(self.degraded, "candidate_evaluation.degraded")
        _ = _v_tuple_of(
            self.hard_constraint_failures,
            HardConstraintFailure,
            "candidate_evaluation.hard_constraint_failures",
        )
        _ = _v_tuple_of(
            self.capability_failures,
            CapabilityFailure,
            "candidate_evaluation.capability_failures",
        )
        if self.capability_margin is not None:
            _ = _v_int(
                self.capability_margin, "candidate_evaluation.capability_margin", lo=0
            )
        if self.blackout_decision is not None:
            _ = _v_instance_of(
                self.blackout_decision,
                BlackoutDecision,
                "candidate_evaluation.blackout_decision",
            )
        if self.scarcity_assessment is not None:
            _ = _v_instance_of(
                self.scarcity_assessment,
                ScarcityAssessment,
                "candidate_evaluation.scarcity_assessment",
            )
        if self.unknown_capacity_decision is not None:
            _ = _v_instance_of(
                self.unknown_capacity_decision,
                UnknownCapacityDecision,
                "candidate_evaluation.unknown_capacity_decision",
            )
        _ = _v_tuple_of(
            self.reservation_decisions,
            ReservationDecision,
            "candidate_evaluation.reservation_decisions",
        )
        _ = _v_tuple_of(
            self.replenishment_evaluations,
            ReplenishmentDecision,
            "candidate_evaluation.replenishment_evaluations",
        )
        if self.degraded:
            decision = self.unknown_capacity_decision
            if decision is None or not decision.degraded:
                raise SelectionContractValidationError(
                    "candidate_evaluation: degraded=True requires a degraded "
                    + "unknown-capacity decision"
                )

        if self.exclusion_stage is None:
            if not self.eligible:
                raise SelectionContractValidationError(
                    "candidate_evaluation: an excluded candidate requires an "
                    + "exclusion_stage"
                )
            if self.reason_codes:
                raise SelectionContractValidationError(
                    "candidate_evaluation: an eligible candidate carries no "
                    + "reason codes"
                )
            if self.hard_constraint_failures or self.capability_failures:
                raise SelectionContractValidationError(
                    "candidate_evaluation: an eligible candidate carries no "
                    + "failure records"
                )
            if self.capability_margin is None:
                raise SelectionContractValidationError(
                    "candidate_evaluation: an eligible candidate carries its "
                    + "capability margin"
                )
            return

        _ = _v_enum(
            self.exclusion_stage,
            frozenset(EXCLUSION_STAGES),
            "candidate_evaluation.exclusion_stage",
        )
        if self.eligible:
            raise SelectionContractValidationError(
                "candidate_evaluation: an eligible candidate must not carry "
                + "an exclusion_stage"
            )
        codes = _v_selection_reason_codes(
            self.reason_codes, "candidate_evaluation.reason_codes"
        )
        object.__setattr__(self, "reason_codes", codes)
        if not codes:
            raise SelectionContractValidationError(
                "candidate_evaluation: an excluded candidate requires at "
                + "least one reason code"
            )
        stage_reasons = _STAGE_REASONS[self.exclusion_stage]
        if not stage_reasons & set(codes):
            raise SelectionContractValidationError(
                "candidate_evaluation: reason codes "
                + f"{list(codes)} carry no primary code for exclusion stage "
                + f"{self.exclusion_stage!r}"
            )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "identity": self.identity.to_dict(),
            "display_name": self.display_name,
            "eligible": self.eligible,
            "degraded": self.degraded,
        }
        if self.exclusion_stage is not None:
            out["exclusion_stage"] = self.exclusion_stage
        if self.hard_constraint_failures:
            out["hard_constraint_failures"] = [
                failure.to_dict() for failure in self.hard_constraint_failures
            ]
        if self.capability_failures:
            out["capability_failures"] = [
                failure.to_dict() for failure in self.capability_failures
            ]
        if self.capability_margin is not None:
            out["capability_margin"] = self.capability_margin
        if self.blackout_decision is not None:
            out["blackout_decision"] = self.blackout_decision.to_dict()
        if self.scarcity_assessment is not None:
            out["scarcity_assessment"] = self.scarcity_assessment.to_dict()
        if self.unknown_capacity_decision is not None:
            out["unknown_capacity_decision"] = self.unknown_capacity_decision.to_dict()
        if self.reservation_decisions:
            out["reservation_decisions"] = [
                decision.to_dict() for decision in self.reservation_decisions
            ]
        if self.replenishment_evaluations:
            out["replenishment_evaluations"] = [
                evaluation.to_dict()
                for evaluation in self.replenishment_evaluations
            ]
        if self.reason_codes:
            out["reason_codes"] = list(self.reason_codes)
        return out


def _identity_key(identity: ModelIdentity) -> tuple[str, str, str]:
    return (identity.provider, identity.model, identity.variant)


def _evaluate_candidate(
    entry: ModelCatalogEntry,
    requirement: TaskRequirement,
    policy: SelectorPolicy,
    snapshots: Sequence[CapacitySnapshot],
    replenishment_states: Sequence[ReplenishmentState],
    evaluated_at: datetime,
) -> CandidateEvaluation:
    """Run the frozen candidate pipeline for one catalog entry.

    Pipeline: blackout -> hard constraints -> capability -> scarcity and
    unknown-capacity policy -> replenishment visibility -> applicable
    reservations -> eligible for ranking. Replenishment is evaluated for
    every candidate that passes capability (including capacity-excluded
    ones) so a currently exhausted candidate can still be explained as
    recoverable; it never changes current eligibility.
    """
    resource_policy = policy.resource_policy
    identity = entry.identity

    blackout = resource_policy.evaluate_blackouts(identity, evaluated_at)
    if blackout.blocked:
        return CandidateEvaluation(
            identity=identity,
            display_name=entry.display_name,
            eligible=False,
            exclusion_stage="policy_blackout",
            blackout_decision=blackout,
            reason_codes=("policy_blocked",),
        )

    hard_failures = evaluate_hard_constraints(requirement.hard_constraints, entry)
    if hard_failures:
        return CandidateEvaluation(
            identity=identity,
            display_name=entry.display_name,
            eligible=False,
            exclusion_stage="hard_constraint",
            hard_constraint_failures=hard_failures,
            reason_codes=("hard_constraint_failed",),
        )

    capability_failures = evaluate_capability_sufficiency(
        requirement.capability_minima, entry.capabilities
    )
    if capability_failures:
        return CandidateEvaluation(
            identity=identity,
            display_name=entry.display_name,
            eligible=False,
            exclusion_stage="capability",
            capability_failures=capability_failures,
            reason_codes=("capability_failed",),
        )
    margin = capability_margin(requirement.capability_minima, entry.capabilities)

    scarcity = assess_scarcity(entry, snapshots)
    unknown_decision = resource_policy.apply_unknown_capacity(scarcity)

    replenishment_evaluations = tuple(
        apply_replenishment_mode(resource_policy.replenishment_mode, state)
        for state in replenishment_states
        if state.provider == identity.provider
    )
    recoverable = any(
        evaluation.recoverable and evaluation.human_action_required
        for evaluation in replenishment_evaluations
    )

    if not unknown_decision.eligible_by_unknown_policy:
        primary = (
            "capacity_unavailable"
            if scarcity.state == "unavailable"
            else "capacity_unknown_blocked"
        )
        codes = [primary]
        if scarcity.state == "unavailable" and recoverable:
            codes.append("replenishment_recoverable")
        return CandidateEvaluation(
            identity=identity,
            display_name=entry.display_name,
            eligible=False,
            exclusion_stage="capacity",
            capability_margin=margin,
            scarcity_assessment=scarcity,
            unknown_capacity_decision=unknown_decision,
            replenishment_evaluations=replenishment_evaluations,
            reason_codes=tuple(sorted(codes)),
        )

    # Applicable reservations are scope-based: only rules whose scope is one
    # of the candidate's known capacity bindings apply. Unknown bindings are
    # never guessed into a reservation match — the candidate is already
    # governed by the unknown-capacity policy above.
    reservation_decisions: tuple[ReservationDecision, ...] = ()
    bindings = entry.capacity_bindings
    if bindings is not None:
        bound = {(scope.provider, scope.scope_id) for scope in bindings}
        applicable = [
            rule
            for rule in resource_policy.reservations
            if (rule.scope.provider, rule.scope.scope_id) in bound
        ]
        reservation_decisions = tuple(
            evaluate_reservation(rule, snapshots, requirement.task_level)
            for rule in applicable
        )
    # Fail closed: an explicit preservation rule whose trigger state cannot
    # be determined excludes the candidate; it is never silently treated as
    # untriggered. Fail-closed dominates an ordinary block in the reason
    # vocabulary; both are stage "reservation".
    reservation_excluded = False
    reservation_primary: str | None = None
    for decision in reservation_decisions:
        if decision.state == "unknown":
            reservation_excluded = True
            reservation_primary = "reservation_unknown"
            break
        if decision.blocked:
            reservation_excluded = True
            reservation_primary = "reservation_blocked"
    if reservation_excluded and reservation_primary is not None:
        return CandidateEvaluation(
            identity=identity,
            display_name=entry.display_name,
            eligible=False,
            exclusion_stage="reservation",
            capability_margin=margin,
            scarcity_assessment=scarcity,
            unknown_capacity_decision=unknown_decision,
            reservation_decisions=reservation_decisions,
            replenishment_evaluations=replenishment_evaluations,
            reason_codes=(reservation_primary,),
        )

    return CandidateEvaluation(
        identity=identity,
        display_name=entry.display_name,
        eligible=True,
        degraded=unknown_decision.degraded,
        capability_margin=margin,
        scarcity_assessment=scarcity,
        unknown_capacity_decision=unknown_decision,
        reservation_decisions=reservation_decisions,
        replenishment_evaluations=replenishment_evaluations,
        reason_codes=(),
    )


# ── Ranking ───────────────────────────────────────────────────────────────────


def _preference_key(
    preference_order: tuple[ModelIdentity, ...], identity: ModelIdentity
) -> tuple[int, int]:
    for index, preferred in enumerate(preference_order):
        if _identity_key(preferred) == _identity_key(identity):
            return (0, index)
    return (1, 0)


def _ranking_key(
    evaluation: CandidateEvaluation, preference_order: tuple[ModelIdentity, ...]
) -> tuple[int, int, int, int, int, str, str, str]:
    """The exact ``balanced`` ranking key (D-027).

    1. capacity knowledge class: known nonzero capacity before unknown /
       degraded capacity (no numeric unknown sentinel exists);
    2. scarcity penalty (integer units) among known-capacity candidates only;
    3. capability margin (lower wins);
    4. explicit preference order (listed before unlisted, then index);
    5. stable ``(provider, model, variant)`` identity.
    """
    scarcity = evaluation.scarcity_assessment
    penalty = 0
    if (
        scarcity is not None
        and scarcity.state == "known"
        and scarcity.penalty_units is not None
    ):
        capacity_class = 0
        penalty = scarcity.penalty_units
    else:
        # Unknown/degraded capacity has no honest numeric comparison against
        # a known percentage; it is separated by the knowledge class first.
        capacity_class = 1
    margin = (
        evaluation.capability_margin
        if evaluation.capability_margin is not None
        else 0
    )
    preference_class, preference_index = _preference_key(
        preference_order, evaluation.identity
    )
    identity = evaluation.identity
    return (
        capacity_class,
        penalty,
        margin,
        preference_class,
        preference_index,
        identity.provider,
        identity.model,
        identity.variant,
    )


# ── SelectionDecision ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SelectionDecision:
    """The structured deterministic selector decision (D-027).

    Carries the selected ``CandidateEvaluation`` (not just an identity), the
    other eligible candidates in exact ranking order, the excluded candidates
    in canonical identity order, the deterministic closest candidates for a
    no-solution result (maximum stage progress, capped at 3) and the
    recoverable candidates (explicitly exhausted candidates whose
    replenishment policy reports ``recoverable`` with
    ``human_action_required``). No raw provider payloads, no credentials, no
    account identifiers and no benchmark evidence dumps are representable.
    Output contracts serialize with ``to_dict``; deserialization is not an
    input boundary in this slice.
    """

    evaluated_at: datetime
    requirement: TaskRequirement
    catalog_version: int
    catalog_updated_on: str
    selector_mode: str
    resource_policy_version: int
    selected: CandidateEvaluation | None
    alternatives: tuple[CandidateEvaluation, ...] = ()
    excluded: tuple[CandidateEvaluation, ...] = ()
    closest_candidates: tuple[CandidateEvaluation, ...] = ()
    recoverable_candidates: tuple[CandidateEvaluation, ...] = ()
    degraded: bool = False
    reason_codes: tuple[str, ...] = ()
    profile_id: str | None = None
    profile_policy_version: int | None = None

    _SELECTED_CODES: ClassVar[frozenset[str]] = frozenset({
        "selected_balanced",
        "selected_degraded_capacity",
    })

    def __post_init__(self) -> None:
        _ = _v_aware_datetime(self.evaluated_at, "selection_decision.evaluated_at")
        _ = _v_instance_of(
            self.requirement, TaskRequirement, "selection_decision.requirement"
        )
        _ = _v_int(
            self.catalog_version, "selection_decision.catalog_version", lo=1
        )
        _ = _v_nonempty_str(
            self.catalog_updated_on, "selection_decision.catalog_updated_on"
        )
        _ = _v_enum(
            self.selector_mode, SELECTOR_MODES, "selection_decision.selector_mode"
        )
        _ = _v_int(
            self.resource_policy_version,
            "selection_decision.resource_policy_version",
            lo=1,
        )
        if self.selected is not None:
            _ = _v_instance_of(
                self.selected, CandidateEvaluation, "selection_decision.selected"
            )
            if not self.selected.eligible:
                raise SelectionContractValidationError(
                    "selection_decision: the selected candidate must be "
                    + "eligible"
                )
        _ = _v_tuple_of(
            self.alternatives, CandidateEvaluation, "selection_decision.alternatives"
        )
        _ = _v_tuple_of(
            self.excluded, CandidateEvaluation, "selection_decision.excluded"
        )
        _ = _v_tuple_of(
            self.closest_candidates,
            CandidateEvaluation,
            "selection_decision.closest_candidates",
        )
        _ = _v_tuple_of(
            self.recoverable_candidates,
            CandidateEvaluation,
            "selection_decision.recoverable_candidates",
        )
        _ = _v_bool(self.degraded, "selection_decision.degraded")
        codes = _v_selection_reason_codes(
            self.reason_codes, "selection_decision.reason_codes"
        )
        object.__setattr__(self, "reason_codes", codes)

        if self.selected is None:
            if self.alternatives:
                raise SelectionContractValidationError(
                    "selection_decision: a no-solution result has no "
                    + "alternatives"
                )
            if self.degraded:
                raise SelectionContractValidationError(
                    "selection_decision: a no-solution result is not degraded"
                )
            if "no_eligible_candidate" not in codes:
                raise SelectionContractValidationError(
                    "selection_decision: a no-solution result requires the "
                    + "'no_eligible_candidate' reason code"
                )
        else:
            if "selected_balanced" not in codes:
                raise SelectionContractValidationError(
                    "selection_decision: a selection requires the "
                    + "'selected_balanced' reason code"
                )
            unexpected = set(codes) - self._SELECTED_CODES
            if unexpected:
                raise SelectionContractValidationError(
                    "selection_decision: a selection carries no exclusion "
                    + f"reason codes, got {sorted(unexpected)}"
                )
            if self.degraded != self.selected.degraded:
                raise SelectionContractValidationError(
                    "selection_decision: degraded must mirror the selected "
                    + "candidate"
                )
            selected_key = _identity_key(self.selected.identity)
            for alternative in self.alternatives:
                if not alternative.eligible:
                    raise SelectionContractValidationError(
                        "selection_decision: alternatives must be eligible"
                    )
                if _identity_key(alternative.identity) == selected_key:
                    raise SelectionContractValidationError(
                        "selection_decision: the selected candidate must not "
                        + "repeat in alternatives"
                    )
        for candidate in self.excluded:
            if candidate.eligible:
                raise SelectionContractValidationError(
                    "selection_decision: excluded candidates must be excluded"
                )
        for candidate in self.closest_candidates:
            if candidate.eligible:
                raise SelectionContractValidationError(
                    "selection_decision: closest candidates are excluded "
                    + "candidates"
                )
        for candidate in self.recoverable_candidates:
            if candidate.eligible:
                raise SelectionContractValidationError(
                    "selection_decision: recoverable candidates are excluded "
                    + "candidates"
                )
        if self.profile_id is None and self.profile_policy_version is not None:
            raise SelectionContractValidationError(
                "selection_decision: profile_policy_version requires a "
                + "profile_id"
            )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "evaluated_at": canonical_instant(self.evaluated_at),
            "requirement": self.requirement.to_dict(),
            "catalog_version": self.catalog_version,
            "catalog_updated_on": self.catalog_updated_on,
            "selector_mode": self.selector_mode,
            "resource_policy_version": self.resource_policy_version,
            "selected": (
                self.selected.to_dict() if self.selected is not None else None
            ),
            "alternatives": [c.to_dict() for c in self.alternatives],
            "excluded": [c.to_dict() for c in self.excluded],
            "closest_candidates": [c.to_dict() for c in self.closest_candidates],
            "recoverable_candidates": [
                c.to_dict() for c in self.recoverable_candidates
            ],
            "degraded": self.degraded,
            "reason_codes": list(self.reason_codes),
        }
        if self.profile_id is not None:
            out["profile_id"] = self.profile_id
        if self.profile_policy_version is not None:
            out["profile_policy_version"] = self.profile_policy_version
        return out


def _closest_candidates(
    excluded: tuple[CandidateEvaluation, ...],
) -> tuple[CandidateEvaluation, ...]:
    """The deterministic no-solution closeness rule (D-027).

    A closer candidate is one that reached a later selector stage before
    exclusion; stage progress only, no second quality score. The closest
    candidates are those at the maximum stage reached by any excluded
    candidate, ordered by stable model identity, capped at 3.
    """
    if not excluded:
        return ()
    max_progress = max(
        _STAGE_PROGRESS[candidate.exclusion_stage or ""] for candidate in excluded
    )
    closest = [
        candidate
        for candidate in excluded
        if _STAGE_PROGRESS[candidate.exclusion_stage or ""] == max_progress
    ]
    return tuple(closest[:3])


def select_model(
    *,
    catalog: ModelCatalog,
    requirement: TaskRequirement,
    policy: SelectorPolicy,
    snapshots: Sequence[CapacitySnapshot],
    evaluated_at: datetime,
    replenishment_states: Sequence[ReplenishmentState] = (),
    profile_id: str | None = None,
    profile_policy_version: int | None = None,
) -> SelectionDecision:
    """The authoritative deterministic ``balanced`` selector.

    Pure: the caller supplies every input, including the single
    timezone-aware evaluation instant used for blackout evaluation. Input
    validation is explicit: at most one snapshot per provider, at most one
    replenishment state per ``(provider, kind)``, timezone-aware instant and
    validated catalog/requirement/policy types. Candidate evaluation always
    proceeds in canonical ``(provider, model, variant)`` order, so catalog
    insertion order never affects the result. A valid no-solution result is a
    legitimate outcome: requirements are never relaxed and no fallback
    bypasses capability.
    """
    _ = _v_aware_datetime(evaluated_at, "select_model.evaluated_at")
    _ = _v_instance_of(catalog, ModelCatalog, "select_model.catalog")
    _ = _v_instance_of(requirement, TaskRequirement, "select_model.requirement")
    _ = _v_instance_of(policy, SelectorPolicy, "select_model.policy")

    snapshot_by_provider: dict[str, CapacitySnapshot] = {}
    for snapshot in snapshots:
        _ = _v_instance_of(snapshot, CapacitySnapshot, "select_model.snapshots")
        if snapshot.provider in snapshot_by_provider:
            raise SelectionContractValidationError(
                "select_model.snapshots: duplicate provider snapshot for "
                + f"{snapshot.provider!r}; at most one snapshot per provider "
                + "is permitted per selection"
            )
        snapshot_by_provider[snapshot.provider] = snapshot
    snapshot_list = list(snapshots)

    state_keys: set[tuple[str, str]] = set()
    state_list: list[ReplenishmentState] = []
    for state in replenishment_states:
        _ = _v_instance_of(state, ReplenishmentState, "select_model.replenishment_states")
        key = (state.provider, state.kind)
        if key in state_keys:
            raise SelectionContractValidationError(
                "select_model.replenishment_states: duplicate replenishment "
                + f"state for (provider, kind) {key}; at most one state per "
                + "(provider, kind) is permitted"
            )
        state_keys.add(key)
        state_list.append(state)

    if profile_id is not None:
        _ = _v_nonempty_str(profile_id, "select_model.profile_id")

    entries = sorted(catalog.entries, key=lambda entry: _identity_key(entry.identity))
    evaluations = [
        _evaluate_candidate(
            entry, requirement, policy, snapshot_list, state_list, evaluated_at
        )
        for entry in entries
    ]
    eligible = [evaluation for evaluation in evaluations if evaluation.eligible]
    ranked = sorted(
        eligible, key=lambda evaluation: _ranking_key(evaluation, policy.preference_order)
    )
    selected = ranked[0] if ranked else None
    alternatives = tuple(ranked[1:])
    excluded = tuple(evaluation for evaluation in evaluations if not evaluation.eligible)

    if selected is None:
        decision_codes: tuple[str, ...] = ("no_eligible_candidate",)
        degraded = False
    else:
        codes = {"selected_balanced"}
        if selected.degraded:
            codes.add("selected_degraded_capacity")
        decision_codes = tuple(sorted(codes))
        degraded = selected.degraded

    return SelectionDecision(
        evaluated_at=evaluated_at,
        requirement=requirement,
        catalog_version=catalog.catalog_version,
        catalog_updated_on=catalog.updated_on,
        selector_mode=policy.mode,
        resource_policy_version=policy.resource_policy.policy_version,
        selected=selected,
        alternatives=alternatives,
        excluded=excluded,
        closest_candidates=(
            () if selected is not None else _closest_candidates(excluded)
        ),
        recoverable_candidates=tuple(
            candidate
            for candidate in excluded
            if candidate.exclusion_stage == "capacity"
            and candidate.scarcity_assessment is not None
            and candidate.scarcity_assessment.state == "unavailable"
            and any(
                evaluation.recoverable and evaluation.human_action_required
                for evaluation in candidate.replenishment_evaluations
            )
        ),
        degraded=degraded,
        reason_codes=decision_codes,
        profile_id=profile_id,
        profile_policy_version=profile_policy_version,
    )


__all__ = [
    "EXCLUSION_STAGES",
    "SELECTION_REASON_CODES",
    "SELECTOR_MODES",
    "SELECTOR_MODE_BALANCED",
    "CapabilityFailure",
    "CandidateEvaluation",
    "HardConstraintFailure",
    "SelectionDecision",
    "SelectorPolicy",
    "capability_margin",
    "canonical_instant",
    "evaluate_capability_sufficiency",
    "evaluate_hard_constraints",
    "neutral_selector_policy",
    "select_model",
    "tighten_requirement",
]
