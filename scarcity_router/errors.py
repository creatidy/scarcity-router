"""Error types for the pure v3 capacity and selection contracts."""

from __future__ import annotations


class CapacityError(Exception):
    """Base class for capacity contract errors."""


class CapacityValidationError(CapacityError):
    """Raised when a snapshot or one of its sub-objects violates a v3 invariant."""


class SelectionContractError(Exception):
    """Base class for task-requirement / model-catalog contract errors."""


class SelectionContractValidationError(SelectionContractError):
    """Raised when a selection-contract object violates a frozen M2b invariant.

    Deliberately separate from :class:`CapacityValidationError`: capacity
    telemetry and selection inputs are different contracts and must not share
    an error type.
    """
