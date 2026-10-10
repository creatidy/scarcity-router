"""Inert producer-response preparation, not a Kernel/harness implementation."""
from __future__ import annotations

from dataclasses import replace
from typing import cast

from .errors import SelectionContractError
from .executable_contract import ExecutableRequirements, executable_error, merge_binding, parse_executable_request
from .gateway_coordinator import parse_pinned_reference
from .gateway_openai import parse_chat_completion_request
from .routing_core import PinnedTarget, RequestBinding
from .selector import tighten_requirement
from .selection_types import CapabilityMinima, HardConstraints, TaskRequirement


def prepare_executable_completion(
    response: object, *, expected_request: object, completion: dict[str, object],
    require_bound: bool = False,
) -> dict[str, object]:
    """Validate retained demands/identity and prepare a request without I/O.

    Kernel owns approval, Attempt persistence and actual harness fidelity.
    This checks a received artifact's relationships, not producer authenticity.
    """
    try:
        _model, expected, expected_binding = parse_executable_request(expected_request)
        if not isinstance(response, dict):
            raise ValueError
        envelope = cast(dict[str, object], response)
        if (set(envelope) != {"schema_version", "route", "execution"}
            or type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1):
            raise ValueError
        if not isinstance(envelope["route"], dict) or not isinstance(envelope["execution"], dict):
            raise ValueError
        route = cast(dict[str, object], envelope["route"])
        if (route.get("status") != "selected" or type(route.get("schema_version")) is not int
            or route["schema_version"] != 1):
            raise ValueError
        execution = cast(dict[str, object], envelope["execution"])
        if set(execution) != {"model", "reasoning_effort", "execution_requirements"}:
            raise ValueError
        pin = parse_pinned_reference(cast(str, execution["model"]))
        target = PinnedTarget.from_route_target_dict(route["target"], decision_id=cast(str, route["decision_id"]))
        if pin != target:
            raise ValueError
        context = ExecutableRequirements.from_dict(execution["execution_requirements"])
        if context.binding.profile_alias is not None:
            if context.binding.profile_alias != _model:
                raise ValueError
        elif _model.startswith("sr-pin:"):
            expected_pin = parse_pinned_reference(_model)
            if expected_pin.resource_id != pin.resource_id or expected_pin.model != pin.model:
                raise ValueError
        elif context.binding.explicit_model is None or context.binding.explicit_model.model != _model:
            raise ValueError
        if context.binding.pinned_target != pin or tighten_requirement(expected, context.requirement) != context.requirement:
            raise ValueError
        if expected_binding.pinned_target is not None:
            original = expected_binding.pinned_target
            if original.resource_id != pin.resource_id or original.model != pin.model:
                raise ValueError
            expected_binding = replace(expected_binding, pinned_target=pin)
        if merge_binding(expected_binding, context.binding) != context.binding:
            raise ValueError
        selection = cast(dict[str, object], route["selection"])
        selected = cast(dict[str, object], selection["selected"])
        selected_requirement = tighten_requirement(context.requirement, TaskRequirement(
            task_level=context.requirement.task_level, capability_minima=CapabilityMinima(),
            hard_constraints=HardConstraints(minimum_output_tokens=context.binding.maximum_output_tokens)))
        if (selected["identity"] != cast(dict[str, object], route["target"])["model"]
            or selected["reasoning_effort"] != execution["reasoning_effort"]
            or selection["requirement"] != selected_requirement.to_dict()):
            raise ValueError
        out = dict(completion)
        already_bound = any(key in out for key in ("model", "reasoning_effort", "execution_requirements"))
        if require_bound or already_bound:
            required = {"model", "execution_requirements"}
            if execution["reasoning_effort"] is not None:
                required.add("reasoning_effort")
            if not required.issubset(out):
                raise ValueError
        for key in ("model", "reasoning_effort", "execution_requirements"):
            if key in out and out[key] != execution[key]:
                raise ValueError
            if key != "reasoning_effort" or execution[key] is not None:
                out[key] = execution[key]
        if context.binding.maximum_output_tokens is not None:
            if "max_tokens" not in out and "max_completion_tokens" not in out:
                if require_bound or already_bound:
                    raise ValueError
                out["max_completion_tokens"] = context.binding.maximum_output_tokens
        parsed = parse_chat_completion_request(out)
        caps = parsed.capabilities
        actual = RequestBinding(
            requires_tool_calls=caps.requires_tool_calls, requires_streaming=caps.requires_streaming,
            requires_structured_output=caps.requires_structured_output,
            requires_reasoning_controls=caps.requires_reasoning_controls,
            minimum_input_context_tokens=caps.estimated_input_tokens,
            maximum_output_tokens=caps.requested_output_tokens, pinned_target=pin,
        )
        _ = merge_binding(context.binding, actual)
        return out
    except (ValueError, SelectionContractError, TypeError, KeyError, AttributeError, RecursionError):
        raise executable_error() from None
