"""Versioned executable demands and consumer preparation, never approval/grants."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import cast

from .errors import SelectionContractError
from .gateway_contracts import GatewayError
from .routing_core import RequestBinding, RouteRequest
from .selection_types import TaskRequirement, REASONING_EFFORTS


def executable_error() -> GatewayError:
    return GatewayError.invalid_request(
        "executable requirements or retained binding are missing, changed or unsupported",
        code="execution_requirements_invalid", param="execution_requirements",
    )


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError
    fields = cast(dict[str, object], value)
    if set(fields) != keys:
        raise ValueError
    return fields


def _version(value: object) -> None:
    if type(value) is not int or value != 1:
        raise ValueError


def merge_binding(retained: RequestBinding, actual: RequestBinding) -> RequestBinding:
    """Combine capability floors; never weaken a retained identity/output ceiling."""
    current = actual.to_dict()
    for key, value in retained.to_dict().items():
        if key.startswith("requires_"):
            current[key] = current.get(key, False) is True or value is True
        elif key == "minimum_input_context_tokens":
            current[key] = max(cast(int, current.get(key, value)), cast(int, value))
        elif key == "maximum_output_tokens":
            supplied = current.get(key)
            if type(supplied) is not int or supplied > cast(int, value):
                raise ValueError
        elif key in current and current[key] != value:
            raise ValueError
        else:
            current[key] = value
    return RequestBinding.from_dict(current)


@dataclass(frozen=True)
class ExecutableRequirements:
    requirement: TaskRequirement
    binding: RequestBinding
    profile_policy_version: int | None = None
    profile_expansion: TaskRequirement | None = None
    reasoning_effort: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> ExecutableRequirements:
        try:
            fields = _object(value, {"schema_version", "requirement", "binding",
                                     "profile_policy_version", "profile_expansion", "reasoning_effort"})
            _version(fields["schema_version"])
            _ = _object(fields["requirement"], {"task_level", "capability_minima", "hard_constraints"})
            requirement = TaskRequirement.from_dict(fields["requirement"])
            binding = RequestBinding.from_dict(fields["binding"])
            version = fields["profile_policy_version"]
            expansion = fields["profile_expansion"]
            effort = fields["reasoning_effort"]
            if effort is not None and (not isinstance(effort, str) or effort not in REASONING_EFFORTS):
                raise ValueError
            if binding.profile_alias is None:
                if version is not None or expansion is not None:
                    raise ValueError
                return cls(requirement, binding, reasoning_effort=effort)
            if type(version) is not int or version < 1 or expansion is None:
                raise ValueError
            return cls(requirement, binding, version, TaskRequirement.from_dict(expansion), effort)
        except (ValueError, SelectionContractError, TypeError, RecursionError):
            raise executable_error() from None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1, "requirement": self.requirement.to_dict(),
            "binding": self.binding.to_dict(),
            "profile_policy_version": self.profile_policy_version,
            "profile_expansion": None if self.profile_expansion is None else self.profile_expansion.to_dict(),
            "reasoning_effort": self.reasoning_effort,
        }

    def fingerprint(self) -> str:
        """In-memory continuation equality, not an authenticity/approval receipt."""
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def bind(self, route: RouteRequest) -> RouteRequest:
        try:
            if self.binding.pinned_target is None or route.request.pinned_target != self.binding.pinned_target:
                raise ValueError
            entry = next((candidate for candidate in route.catalog.entries if candidate.identity == self.binding.pinned_target.model), None)
            if entry is None or entry.reasoning_effort != self.reasoning_effort:
                raise ValueError
            if self.binding.profile_alias is not None:
                profile = route.routing_profile
                if (profile is None or route.profile_policy_version != self.profile_policy_version
                    or route.profiles.resolve(profile.profile_id) != self.profile_expansion):
                    raise ValueError
            return replace(route, request=merge_binding(self.binding, route.request),
                           task_requirement=self.requirement)
        except (ValueError, SelectionContractError):
            raise executable_error() from None


def parse_executable_request(value: object) -> tuple[str, TaskRequirement, RequestBinding]:
    try:
        fields = _object(value, {"schema_version", "model", "requirement", "binding"})
        _version(fields["schema_version"])
        model = fields["model"]
        if not isinstance(model, str) or not model or len(model) > 512:
            raise ValueError
        _ = _object(fields["requirement"], {"task_level", "capability_minima", "hard_constraints"})
        return model, TaskRequirement.from_dict(fields["requirement"]), RequestBinding.from_dict(fields["binding"])
    except (ValueError, SelectionContractError, TypeError, RecursionError):
        raise executable_error() from None
