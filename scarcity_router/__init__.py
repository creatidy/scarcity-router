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

These types, validation and deterministic serialization only; they carry no
ratings, no profile minima, no profile resolver and no selector (D-024).
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
    "SUPPORTED_PROVIDERS",
    "TASK_LEVELS",
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
    "HardConstraints",
    "HumanOverride",
    "ModelCatalog",
    "ModelCatalogEntry",
    "ModelHardProperties",
    "ModelIdentity",
    "ModelRef",
    "SelectionContractError",
    "SelectionContractValidationError",
    "TaskRequirement",
]

__version__ = "0.0.0"
