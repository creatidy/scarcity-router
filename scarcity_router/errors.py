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


class SimulationOverrideApplicationError(SelectionContractValidationError):
    """Raised when a typed simulation override cannot apply to its baseline.

    The override itself has passed the serialized contract boundary, but its
    target is absent, ambiguous, non-usable or otherwise incompatible with
    the supplied baseline snapshots.
    """


class ApplicationInputError(SelectionContractError):
    """Raised when caller-supplied application input is invalid (D-030).

    This is the explicit typed boundary between client-controlled input
    failures and server configuration/internal failures: machine-interface
    adapters classify it as the frozen ``invalid_request`` error class by
    type alone, never by matching exception message text. Deriving from
    :class:`SelectionContractError` keeps the CLI's single invalid-input
    failure class unchanged.
    """


class RouteContractValidationError(SelectionContractError):
    """Raised when a routing-core input violates a route-decision invariant.

    Deliberately separate from :class:`CapacityValidationError` (M01 resource
    state) and from the selector's own
    :class:`SelectionContractValidationError`: the route-decision contract is
    its own versioned family (D-042), so its violations fail as their own
    type while remaining inside the selection-contract error hierarchy that
    application adapters already classify as invalid input.
    """
