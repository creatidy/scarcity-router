"""Error types for the pure v1 capacity contract."""

from __future__ import annotations


class CapacityError(Exception):
    """Base class for capacity contract errors."""


class CapacityValidationError(CapacityError):
    """Raised when a snapshot or one of its sub-objects violates a v1 invariant."""
