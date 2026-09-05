"""Pure v2 normalized capacity contract for Scarcity Router.

Provider-independent, standard-library only. No provider parsing, credential
access, network or subprocess behavior.

Public API:
    CapacitySnapshot    -- a v2 normalized capacity observation
    CapacityWindow      -- one normalized quota/limit window
    CapacityDiagnostic  -- one allowlisted diagnostic record
    CapacityValidationError
    CapacityError

Validate and serialize deterministically::

    snap = CapacitySnapshot.from_dict(payload)      # raises on violation
    payload2 = snap.to_dict()                        # canonical JSON dict
    CapacitySnapshot.from_dict(payload2)             # round-trips
"""

from __future__ import annotations

from .capacity import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
)
from .errors import CapacityError, CapacityValidationError

__all__ = [
    "CapacityDiagnostic",
    "CapacityError",
    "CapacitySnapshot",
    "CapacityValidationError",
    "CapacityWindow",
]

__version__ = "0.0.0"
