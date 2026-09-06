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
    SelectionContractValidationError, SelectionContractError

Public API — scarcity and resource-policy primitives (M2d, D-026):
    scarcity penalty and labels:
        SCARCITY_PENALTY_SCALE, SCARCITY_LABELS, SCARCITY_STATES,
        SCARCITY_REASON_CODES, scarcity_penalty_units, scarcity_label
    assessment:
        GoverningWindowEvidence, ScarcityAssessment, assess_scarcity
    policy:
        UNKNOWN_CAPACITY_MODES, REPLENISHMENT_MODES, WEEKDAYS,
        POLICY_REASON_CODES, UnknownCapacityDecision,
        apply_unknown_capacity_mode, ReservationRule, ReservationDecision,
        evaluate_reservation, AvailabilityTarget, WeeklyBlackoutRule,
        BlackoutDecision, evaluate_blackouts, ReplenishmentState,
        ReplenishmentDecision, apply_replenishment_mode, UserPolicy

These types, validation and deterministic serialization only; they carry no
ratings, no ranking modes, no candidate ordering and no selector (D-024,
D-026). M2d assesses scarcity and evaluates resource policy for single
candidates; M2e owns ranking and ``select``.
"""

from __future__ import annotations

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
)
from .policy import (
    POLICY_REASON_CODES,
    REPLENISHMENT_MODES,
    UNKNOWN_CAPACITY_MODES,
    WEEKDAYS,
    AvailabilityTarget,
    BlackoutDecision,
    ReplenishmentDecision,
    ReplenishmentState,
    ReservationDecision,
    ReservationRule,
    UnknownCapacityDecision,
    UserPolicy,
    WeeklyBlackoutRule,
    apply_replenishment_mode,
    apply_unknown_capacity_mode,
    evaluate_blackouts,
    evaluate_reservation,
)
from .scarcity import (
    SCARCITY_LABELS,
    SCARCITY_PENALTY_SCALE,
    SCARCITY_REASON_CODES,
    SCARCITY_STATES,
    GoverningWindowEvidence,
    ScarcityAssessment,
    assess_scarcity,
    scarcity_label,
    scarcity_penalty_units,
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
    "CAPABILITY_DIMENSIONS",
    "CONFIDENCE_VALUES",
    "MAX_RATING",
    "MIN_RATING",
    "POLICY_REASON_CODES",
    "REPLENISHMENT_MODES",
    "SCARCITY_LABELS",
    "SCARCITY_PENALTY_SCALE",
    "SCARCITY_REASON_CODES",
    "SCARCITY_STATES",
    "SUPPORTED_PROVIDERS",
    "TASK_LEVELS",
    "UNKNOWN_CAPACITY_MODES",
    "WEEKDAYS",
    "AvailabilityTarget",
    "BlackoutDecision",
    "CapabilityAssessment",
    "CapabilityAssessments",
    "CapacityDiagnostic",
    "CapacityError",
    "CapabilityMinima",
    "CapacityScopeRef",
    "CapacitySnapshot",
    "CapacityValidationError",
    "CapacityWindow",
    "EvidenceRef",
    "GoverningWindowEvidence",
    "HardConstraints",
    "HumanOverride",
    "ModelCatalog",
    "ModelCatalogEntry",
    "ModelHardProperties",
    "ModelIdentity",
    "ModelRef",
    "ReplenishmentDecision",
    "ReplenishmentState",
    "ReservationDecision",
    "ReservationRule",
    "ScarcityAssessment",
    "SelectionContractError",
    "SelectionContractValidationError",
    "TaskRequirement",
    "UnknownCapacityDecision",
    "UserPolicy",
    "WeeklyBlackoutRule",
    "apply_replenishment_mode",
    "apply_unknown_capacity_mode",
    "assess_scarcity",
    "evaluate_blackouts",
    "evaluate_reservation",
    "scarcity_label",
    "scarcity_penalty_units",
]

__version__ = "0.0.0"
