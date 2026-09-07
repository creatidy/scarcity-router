"""Shared logical machine-interface parsing and envelopes (M3c, D-031).

REST and MCP own only their transport framing. This module owns the logical
request shapes, missing/null semantics and machine-interface v1 envelopes so
both adapters call the same typed application boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar, cast

from .capacity import CapacitySnapshot
from .errors import ApplicationInputError, SelectionContractError
from .policy import ReplenishmentState
from .selection_types import TaskRequirement
from .simulation import SimulationOverrides, SimulationResult
from .status import canonical_snapshot_documents
from .selector import SelectionDecision, SelectorPolicy

ENVELOPE_SCHEMA_VERSION = 1

_SELECT_REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "profile_id",
        "requirement",
        "tightening",
        "selector_policy",
        "replenishment_states",
    }
)
_SIMULATE_REQUEST_KEYS: frozenset[str] = _SELECT_REQUEST_KEYS | {"overrides"}

_T = TypeVar("_T")


@dataclass(frozen=True)
class ParsedSelectionInputs:
    """Typed logical select inputs after shared boundary validation."""

    profile_id: str | None
    requirement: TaskRequirement | None
    tightening: TaskRequirement | None
    policy: SelectorPolicy | None
    replenishment_states: tuple[ReplenishmentState, ...]


def _as_object_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ApplicationInputError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _parse_client_object(
    from_dict: Callable[[object], _T], value: object, field: str
) -> _T:
    """Parse one typed client object without reflecting its contents."""
    try:
        return from_dict(value)
    except (ValueError, SelectionContractError) as exc:
        _ = exc
        raise ApplicationInputError(f"invalid {field}") from None


def _reject_unknown_keys(
    document: Mapping[str, object], allowed: frozenset[str]
) -> None:
    if set(document) - allowed:
        raise ApplicationInputError("request contains unknown fields")


def _parse_common_selection_fields(
    document: Mapping[str, object],
) -> ParsedSelectionInputs:
    """Apply the frozen select/simulate missing and null semantics."""
    raw_profile = document.get("profile_id")
    if raw_profile is not None and not isinstance(raw_profile, str):
        raise ApplicationInputError("profile_id must be a string or null")
    raw_requirement = document.get("requirement")
    raw_tightening = document.get("tightening")
    raw_policy = document.get("selector_policy")
    return ParsedSelectionInputs(
        profile_id=raw_profile,
        requirement=(
            _parse_client_object(TaskRequirement.from_dict, raw_requirement, "requirement")
            if raw_requirement is not None
            else None
        ),
        tightening=(
            _parse_client_object(TaskRequirement.from_dict, raw_tightening, "tightening")
            if raw_tightening is not None
            else None
        ),
        policy=(
            _parse_client_object(SelectorPolicy.from_dict, raw_policy, "selector_policy")
            if raw_policy is not None
            else None
        ),
        replenishment_states=_parse_replenishment_states(document),
    )


def parse_selection_document(document: object) -> ParsedSelectionInputs:
    """Parse one logical ``/v1/select`` or ``scarcity_select`` document."""
    selection = _as_object_mapping(document, "selection request")
    _reject_unknown_keys(selection, _SELECT_REQUEST_KEYS)
    return _parse_common_selection_fields(selection)


def _parse_replenishment_states(
    document: Mapping[str, object],
) -> tuple[ReplenishmentState, ...]:
    if "replenishment_states" not in document:
        return ()
    raw = document["replenishment_states"]
    if raw is None:
        raise ApplicationInputError("replenishment_states must be an array, not null")
    if not isinstance(raw, list):
        raise ApplicationInputError("replenishment_states must be an array")
    states: list[ReplenishmentState] = []
    seen: set[tuple[str, str]] = set()
    for item in cast("list[object]", raw):
        state = _parse_client_object(
            ReplenishmentState.from_dict, item, "replenishment_states entry"
        )
        key = (state.provider, state.kind)
        if key in seen:
            raise ApplicationInputError("duplicate replenishment state")
        seen.add(key)
        states.append(state)
    return tuple(states)


def _parse_overrides(document: Mapping[str, object]) -> SimulationOverrides:
    if "overrides" not in document:
        raise ApplicationInputError("overrides is required")
    raw = document["overrides"]
    if not isinstance(raw, Mapping):
        raise ApplicationInputError("overrides must be a JSON object")
    return _parse_client_object(
        SimulationOverrides.from_dict,
        cast(Mapping[str, object], raw),
        "overrides",
    )


def parse_simulation_document(
    document: object,
) -> tuple[ParsedSelectionInputs, SimulationOverrides]:
    """Parse one logical ``/v1/simulate`` or ``scarcity_simulate`` document."""
    simulation = _as_object_mapping(document, "simulation request")
    _reject_unknown_keys(simulation, _SIMULATE_REQUEST_KEYS)
    return _parse_common_selection_fields(simulation), _parse_overrides(simulation)


def parse_status_arguments(document: object) -> None:
    """Validate that the status tool received no logical input fields."""
    arguments = _as_object_mapping(document, "status arguments")
    if arguments:
        raise ApplicationInputError("scarcity_status accepts no input fields")


def status_envelope(
    snapshots: Sequence[CapacitySnapshot],
) -> dict[str, object]:
    """Build the exact machine-interface v1 status envelope."""
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "snapshots": canonical_snapshot_documents(snapshots),
    }


def selection_envelope(decision: SelectionDecision) -> dict[str, object]:
    """Build the exact machine-interface v1 selection envelope."""
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "decision": decision.to_dict(),
    }


def simulation_envelope(result: SimulationResult) -> dict[str, object]:
    """Build the exact machine-interface v1 simulation envelope."""
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "result": result.to_dict(),
    }


def invalid_request_payload() -> dict[str, object]:
    """Return the fixed safe logical invalid-request payload."""
    return {
        "error": {
            "code": "invalid_request",
            "message": "invalid request",
        }
    }


def internal_error_payload() -> dict[str, object]:
    """Return the fixed safe logical internal-error payload."""
    return {
        "error": {
            "code": "internal_error",
            "message": "internal server error",
        }
    }


__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "ParsedSelectionInputs",
    "invalid_request_payload",
    "internal_error_payload",
    "parse_selection_document",
    "parse_simulation_document",
    "parse_status_arguments",
    "selection_envelope",
    "simulation_envelope",
    "status_envelope",
]
