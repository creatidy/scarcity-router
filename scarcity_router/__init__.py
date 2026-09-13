"""Pure normalized contracts for Scarcity Router.

Provider-independent, standard-library only. No provider parsing, credential
access, network or subprocess behavior.

Public API — capacity (v3):
    CapacitySnapshot    -- a v3 normalized capacity observation
    CapacityWindow      -- one normalized quota/limit window (optional
                           semantic scope_id)
    CapacityDiagnostic  -- one allowlisted diagnostic record
    CapacityValidationError
    CapacityError

Validate and serialize deterministically::

    snap = CapacitySnapshot.from_dict(payload)      # raises on violation
    payload2 = snap.to_dict()                        # canonical JSON dict
    CapacitySnapshot.from_dict(payload2)             # round-trips

Public API — selection inputs (M2b task/catalog core contracts):
    ModelRef, ModelIdentity, CapacityScopeRef,
    CapabilityMinima, HardConstraints, TaskRequirement,
    ModelHardProperties, EvidenceRef, HumanOverride,
    CapabilityAssessment, CapabilityAssessments,
    ModelCatalogEntry, ModelCatalog,
    SelectionContractValidationError, SimulationOverrideApplicationError,
    SelectionContractError

Public API — scarcity and resource-policy primitives (M2d, D-026; blend D-037):
    scarcity penalty and labels:
        SCARCITY_PENALTY_SCALE, SCARCITY_LABELS, SCARCITY_STATES,
        SCARCITY_REASON_CODES, scarcity_penalty_units, scarcity_label
    window roles and blend (D-037):
        WINDOW_ROLES, WEEKLY_TO_FIVE_HOUR_RATIO, BLEND_UNIT_COUNT,
        window_role, blended_effective_remaining_percent
    assessment:
        GoverningWindowEvidence, ScarcityAssessment, assess_scarcity
    policy:
        UNKNOWN_CAPACITY_MODES, REPLENISHMENT_MODES, WEEKDAYS,
        POLICY_REASON_CODES, UnknownCapacityDecision,
        apply_unknown_capacity_mode, ReservationRule, ReservationDecision,
        evaluate_reservation, AvailabilityTarget, WeeklyBlackoutRule,
        BlackoutDecision, evaluate_blackouts, WeeklyHappyHourRule,
        HappyHourDecision, evaluate_happy_hours, ReplenishmentState,
        ReplenishmentDecision, apply_replenishment_mode, UserPolicy

Public API — deterministic selector (M2e, D-027):
    SelectorPolicy, neutral_selector_policy, tighten_requirement,
    HardConstraintFailure, CapabilityFailure, CandidateEvaluation,
    ReplenishmentEvaluation, SelectionDecision, select_model,
    capability_margin,
    evaluate_hard_constraints, evaluate_capability_sufficiency,
    SELECTOR_MODE_BALANCED, SELECTOR_MODES, EXCLUSION_STAGES,
    SELECTION_REASON_CODES

Public API — simulation over the same selector (M2e, D-027):
    CapacityPercentageOverride, SimulationOverrides, SimulationResult,
    apply_capacity_overrides, simulate_selection

The selector and simulation are pure: callers supply the catalog, the
resolved requirement, the policy, the current normalized snapshots, the
replenishment states and one timezone-aware evaluation instant. Application
composition (artifact loading, status collection, rendering) lives in
``selection_app``/``cli``.
"""

from __future__ import annotations

from importlib import metadata as _metadata

from .capacity import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
)
from .errors import (
    CapacityError,
    CapacityValidationError,
    SelectionContractError,
    SelectionContractValidationError,
    SimulationOverrideApplicationError,
)
from .policy import (
    POLICY_REASON_CODES,
    REPLENISHMENT_MODES,
    UNKNOWN_CAPACITY_MODES,
    WEEKDAYS,
    AvailabilityTarget,
    BlackoutDecision,
    HappyHourDecision,
    ReplenishmentDecision,
    ReplenishmentState,
    ReservationDecision,
    ReservationRule,
    UnknownCapacityDecision,
    UserPolicy,
    WeeklyBlackoutRule,
    WeeklyHappyHourRule,
    apply_replenishment_mode,
    apply_unknown_capacity_mode,
    evaluate_blackouts,
    evaluate_happy_hours,
    evaluate_reservation,
)
from .scarcity import (
    BLEND_UNIT_COUNT,
    SCARCITY_LABELS,
    SCARCITY_PENALTY_SCALE,
    SCARCITY_REASON_CODES,
    SCARCITY_STATES,
    WEEKLY_TO_FIVE_HOUR_RATIO,
    WINDOW_ROLES,
    WINDOW_ROLE_STRATEGIC,
    WINDOW_ROLE_TACTICAL,
    GoverningWindowEvidence,
    ScarcityAssessment,
    assess_scarcity,
    blended_effective_remaining_percent,
    scarcity_label,
    scarcity_penalty_units,
    window_role,
)
from .selector import (
    DEFAULT_SHORT_WINDOW_FLOOR_PERCENT,
    EXCLUSION_STAGES,
    SELECTION_REASON_CODES,
    SELECTOR_MODES,
    SELECTOR_MODE_BALANCED,
    CapabilityFailure,
    CandidateEvaluation,
    HardConstraintFailure,
    ReplenishmentEvaluation,
    SelectionDecision,
    SelectorPolicy,
    capability_margin,
    evaluate_capability_sufficiency,
    evaluate_hard_constraints,
    neutral_selector_policy,
    select_model,
    tighten_requirement,
)
from .simulation import (
    CapacityPercentageOverride,
    SimulationOverrides,
    SimulationResult,
    apply_capacity_overrides,
    simulate_selection,
)
from .selection_types import (
    CAPABILITY_DIMENSIONS,
    CONFIDENCE_VALUES,
    MAX_RATING,
    MIN_RATING,
    SUPPORTED_PROVIDERS,
    TASK_LEVELS,
    CapabilityAssessment,
    CapabilityAssessments,
    CapabilityMinima,
    CapacityScopeRef,
    EvidenceRef,
    HardConstraints,
    HumanOverride,
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
    ModelRef,
    TaskRequirement,
)

__all__ = [
    "BLEND_UNIT_COUNT",
    "CAPABILITY_DIMENSIONS",
    "CONFIDENCE_VALUES",
    "DEFAULT_SHORT_WINDOW_FLOOR_PERCENT",
    "EXCLUSION_STAGES",
    "MAX_RATING",
    "MIN_RATING",
    "POLICY_REASON_CODES",
    "REPLENISHMENT_MODES",
    "SCARCITY_LABELS",
    "SCARCITY_PENALTY_SCALE",
    "SCARCITY_REASON_CODES",
    "SCARCITY_STATES",
    "SELECTION_REASON_CODES",
    "SELECTOR_MODES",
    "SELECTOR_MODE_BALANCED",
    "SUPPORTED_PROVIDERS",
    "TASK_LEVELS",
    "UNKNOWN_CAPACITY_MODES",
    "WEEKDAYS",
    "WEEKLY_TO_FIVE_HOUR_RATIO",
    "WINDOW_ROLES",
    "WINDOW_ROLE_STRATEGIC",
    "WINDOW_ROLE_TACTICAL",
    "AvailabilityTarget",
    "BlackoutDecision",
    "CapabilityAssessment",
    "CapabilityAssessments",
    "CapabilityFailure",
    "CapacityDiagnostic",
    "CapacityError",
    "CapabilityMinima",
    "CapacityPercentageOverride",
    "CapacityScopeRef",
    "CapacitySnapshot",
    "CapacityValidationError",
    "CapacityWindow",
    "CandidateEvaluation",
    "EvidenceRef",
    "GoverningWindowEvidence",
    "HardConstraintFailure",
    "HardConstraints",
    "HappyHourDecision",
    "HumanOverride",
    "ModelCatalog",
    "ModelCatalogEntry",
    "ModelHardProperties",
    "ModelIdentity",
    "ModelRef",
    "ReplenishmentDecision",
    "ReplenishmentEvaluation",
    "ReplenishmentState",
    "ReservationDecision",
    "ReservationRule",
    "ScarcityAssessment",
    "SelectionContractError",
    "SelectionContractValidationError",
    "SimulationOverrideApplicationError",
    "SelectionDecision",
    "SelectorPolicy",
    "SimulationOverrides",
    "SimulationResult",
    "TaskRequirement",
    "UnknownCapacityDecision",
    "UserPolicy",
    "WeeklyBlackoutRule",
    "WeeklyHappyHourRule",
    "apply_replenishment_mode",
    "apply_unknown_capacity_mode",
    "apply_capacity_overrides",
    "assess_scarcity",
    "blended_effective_remaining_percent",
    "capability_margin",
    "evaluate_blackouts",
    "evaluate_capability_sufficiency",
    "evaluate_happy_hours",
    "evaluate_hard_constraints",
    "evaluate_reservation",
    "get_version",
    "neutral_selector_policy",
    "scarcity_label",
    "scarcity_penalty_units",
    "select_model",
    "simulate_selection",
    "tighten_requirement",
    "window_role",
]

# ── Distribution version ─────────────────────────────────────────────────────
#
# ``__version__`` is the single authoritative source literal; the build
# backend reads it for distribution metadata (no second committed copy).
# Runtime callers use ``get_version()``, which prefers the installed
# distribution metadata and falls back to this deterministic literal when
# the package runs from a source tree without an installed distribution.

__version__ = "0.1.0"


def get_version() -> str:
    """Return the installed distribution version or the source fallback."""
    try:
        return _metadata.version("scarcity-router")
    except _metadata.PackageNotFoundError:
        return __version__
