"""Bounded local interpretation of Kernel ordinary Declaration v1.

No transport, authentication, approval, task classification or model inventory.
Only explicit data in the approved subject supplies model/channel requirements.
The caller must retain Kernel's current approval and Router's authorization gates.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import urlsplit

from .errors import ApplicationInputError, SelectionContractError
from .routing_core import RequestBinding, RouteRequest
from .selection_app import load_strict_json, resolve_requirement
from .selection_types import TaskProfileCatalog, TaskRequirement
from .selector import tighten_requirement
from .gateway_validation import v_instance

QUALITY_PREFIX = "scarcity-router.requirement.v1:"
INTERFACE_PREFIX = "scarcity-router.request.v1:"
KERNEL_PRODUCER_REVISION = "a9a65006cc8cc5b5e4468746a18ff51daf742e6b"
_FIELDS = frozenset({"version", "outcome", "criteria", "quality", "interface", "context",
                     "unknowns", "paths", "network", "effects", "recipes", "provenance"})


@dataclass(frozen=True)
class KernelRequirementInterpretation:
    """Non-authorizing projection; original content is represented by its digest.

    Binding rechecks the actual profile contents as well as its version, so
    cached interpretations cannot silently inherit changed calibration.
    """

    declaration_digest: str
    requirement: TaskRequirement | None = field(repr=False)
    request: RequestBinding | None = field(repr=False)
    problems: tuple[str, ...] = ()
    profile_id: str | None = field(default=None, repr=False)
    profile_policy_version: int | None = None
    profile_expansion: TaskRequirement | None = None

    def to_dict(self) -> dict[str, object]:
        """Local diagnostic/projection format, not a recommendation-v1 response.

        Evidence pin identifies the inspected schema, not the origin of arbitrary
        input. Kernel retains the full draft/decision/evidence/Program handoff;
        this projection never replaces that subject or attests its approval.
        """
        out: dict[str, object] = {
            "interpretation_version": 1,
            "producer_schema_evidence_revision": KERNEL_PRODUCER_REVISION,
            "declaration_digest": self.declaration_digest,
            "problems": list(self.problems),
            "accounted_fields": {
                "router_interpretation": ["version", "quality", "interface", "context", "unknowns"],
                "kernel_acceptance": ["outcome", "criteria", "recipes"],
                "kernel_scoped_requests": ["paths", "network", "effects"],
                "untrusted_provenance": ["provenance"],
            },
        }
        if self.requirement is not None:
            out["requirement"] = self.requirement.to_dict()
        if self.request is not None:
            out["request"] = self.request.to_dict()
        if self.profile_id is not None and self.profile_expansion is not None:
            out["profile"] = {
                "profile_id": self.profile_id, "policy_version": self.profile_policy_version,
                "expansion": self.profile_expansion.to_dict(),
            }
        return out

    def bind(self, route: RouteRequest, *, current_declaration_digest: str) -> RouteRequest:
        if self.problems or self.requirement is None or self.request is None:
            raise ApplicationInputError("Kernel requirements refused: " + ", ".join(self.problems))
        if current_declaration_digest != self.declaration_digest:
            raise ApplicationInputError("Kernel requirement subject changed; obtain a fresh interpretation")
        if self.profile_id is not None:
            try:
                same_profile = (
                    route.profile_policy_version == self.profile_policy_version
                    and route.profiles.resolve(self.profile_id) == self.profile_expansion
                )
            except SelectionContractError:
                same_profile = False
            if not same_profile:
                raise ApplicationInputError("Kernel requirement profile changed; obtain a fresh interpretation")
        requirement = self.requirement
        if route.task_requirement is not None:
            try:
                requirement = tighten_requirement(route.task_requirement, requirement)
            except SelectionContractError:
                raise ApplicationInputError("Kernel requirements contradict existing requirement constraints") from None
        current, incoming = route.request.to_dict(), self.request.to_dict()
        for key, value in incoming.items():
            if key.startswith("requires_"):
                current[key] = current.get(key, False) is True or value is True
            elif key == "minimum_input_context_tokens":
                current[key] = max(cast(int, current.get(key, value)), cast(int, value))
            elif key == "maximum_output_tokens" and key in current and current[key] != value:
                raise ApplicationInputError("Kernel output ceiling conflicts with the existing request")
            elif key in current and current[key] != value:
                raise ApplicationInputError("Kernel request conflicts with existing identity binding")
            else:
                current[key] = value
        try:
            binding = RequestBinding.from_dict(current)
        except SelectionContractError:
            raise ApplicationInputError("Kernel request conflicts with the configured alias/identity binding") from None
        try:
            return replace(route, task_requirement=requirement, request=binding)
        except SelectionContractError:
            raise ApplicationInputError("Kernel requirements conflict with the current profile/channel/output contract") from None


def interpret_kernel_declaration(
    declaration: bytes, *, profiles: TaskProfileCatalog, profile_policy_version: int | None = None,
) -> KernelRequirementInterpretation:
    """Validate the actual producer shape and account for every declared field.

    Caller result/recipe/scope obligations remain bound by the original digest,
    not converted to grants. Textual model/interface/context meaning is never
    guessed. Diagnostics contain fixed field codes, never submitted content.
    """
    _ = v_instance(declaration, bytes, "kernel_declaration")
    if len(declaration) > 262144:
        raise ApplicationInputError("Kernel declaration exceeds the producer byte bound")
    digest = hashlib.sha256(declaration).hexdigest()
    problems: list[str] = []
    requirement: TaskRequirement | None = None
    binding: RequestBinding | None = None
    profile_id: str | None = None
    expansion: TaskRequirement | None = None
    try:
        value = load_strict_json(declaration.decode("utf-8"), label="kernel_declaration")
        if not isinstance(value, dict):
            raise ValueError
        fields = cast(dict[str, object], value)
        if frozenset(fields) != _FIELDS or type(fields["version"]) is not int or fields["version"] != 1:
            raise ValueError
        if not isinstance(fields["outcome"], str) or not fields["outcome"].strip():
            raise ValueError
        arrays: dict[str, list[str]] = {}
        for name in _FIELDS - {"version", "outcome"}:
            items = fields[name]
            if not isinstance(items, list):
                raise ValueError
            strings: list[str] = []
            seen: set[str] = set()
            for item in cast(list[object], items):
                if not isinstance(item, str) or not item.strip() or item in seen:
                    raise ValueError
                seen.add(item)
                strings.append(item)
            if name in ("criteria", "recipes", "provenance") and not strings:
                raise ValueError
            arrays[name] = strings
        for path in arrays["paths"]:
            parsed_path = PurePosixPath(path)
            if parsed_path.is_absolute() or ".." in parsed_path.parts or path in (".", "") or "\\" in path:
                raise ValueError
        for origin in arrays["network"]:
            parsed_origin = urlsplit(origin)
            if (parsed_origin.scheme != "https" or not parsed_origin.hostname or parsed_origin.username
                or parsed_origin.password or parsed_origin.path or parsed_origin.query or parsed_origin.fragment):
                raise ValueError
    except (ValueError, UnicodeError, RecursionError):
        return KernelRequirementInterpretation(digest, None, None, ("declaration.malformed",))

    if arrays["unknowns"]:
        problems.append("unknowns.unresolved")
    if arrays["context"]:
        problems.append("context.unsupported")
    quality = arrays["quality"]
    if not quality:
        problems.append("quality.missing_requirement")
    elif len(quality) != 1 or not quality[0].startswith(QUALITY_PREFIX):
        problems.append("quality.unsupported")
    else:
        try:
            payload = load_strict_json(quality[0][len(QUALITY_PREFIX):], label="kernel_quality")
            if not isinstance(payload, dict):
                raise ValueError
            mapping = cast(dict[str, object], payload)
            if "requirement" not in mapping or not frozenset(mapping).issubset({"requirement", "profile_id"}):
                raise ValueError
            supplied = TaskRequirement.from_dict(mapping["requirement"])
            if "profile_id" in mapping:
                candidate = mapping["profile_id"]
                if not isinstance(candidate, str) or not candidate or type(profile_policy_version) is not int or profile_policy_version < 1:
                    raise ValueError
                profile_id = candidate
                expansion = profiles.resolve(profile_id)
                requirement, _profile = resolve_requirement(
                    profiles=profiles, profile_id=profile_id, explicit_requirement=None, tightening=supplied,
                )
                if requirement != supplied:
                    raise ValueError  # The approved bytes must include the actual expanded floor.
            else:
                requirement, _profile = resolve_requirement(
                    profiles=profiles, profile_id=None, explicit_requirement=supplied, tightening=None,
                )
            if requirement.hard_constraints.requires_vision:
                problems.append("quality.vision_channel_unsupported")
        except (ValueError, SelectionContractError, RecursionError):
            problems.append("quality.invalid_requirement")
    interface = arrays["interface"]
    if not interface:
        binding = RequestBinding()
    elif len(interface) != 1 or not interface[0].startswith(INTERFACE_PREFIX):
        problems.append("interface.unsupported")
    else:
        try:
            binding = RequestBinding.from_dict(load_strict_json(interface[0][len(INTERFACE_PREFIX):], label="kernel_interface"))
            if binding.profile_alias is not None:
                problems.append("interface.profile_alias_unsupported")
        except (ValueError, SelectionContractError, RecursionError):
            problems.append("interface.invalid_binding")
    if problems:
        return KernelRequirementInterpretation(digest, None, None, tuple(problems))
    return KernelRequirementInterpretation(
        digest, requirement, binding, (), profile_id,
        profile_policy_version if profile_id is not None else None, expansion,
    )
