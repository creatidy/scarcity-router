"""Application composition for select and simulate (M2e, D-027).

Thin application layer: it loads repository and user-supplied JSON artifacts
(strictly: duplicate keys, NaN/Infinity and malformed JSON fail safely),
resolves the task requirement (profile path, explicit-requirement path and
optional monotone tightening), obtains ONE timezone-aware current instant,
collects one current capacity observation through the existing
``collect_status`` path with a fixed clock so both provider snapshots share
it, runs the pure selector or simulation and renders deterministic human or
JSON output. All business logic stays in the pure core (``selector.py``,
``simulation.py``); this module never parses provider payloads and never
issues model requests.

The default artifact paths are the repository-root ``model-catalog.json``
and ``model-policy.json`` of the current source tree — a provisional
source-tree CLI layout only (U-008 remains unresolved; this is not the
final installed-package resource layout).

Strict JSON loading uses only the standard library: ``object_pairs_hook``
rejects duplicate object keys, ``parse_constant`` rejects NaN/Infinity and
malformed JSON raises ``ValueError`` — never a raw payload dump.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .errors import SelectionContractValidationError
from .policy import ReplenishmentDecision, ReplenishmentState
from .selector import (
    EXCLUSION_STAGES,
    CandidateEvaluation,
    SelectionDecision,
    SelectorPolicy,
    neutral_selector_policy,
    select_model,
    tighten_requirement,
)
from .selection_types import (
    ModelCatalog,
    TaskProfileCatalog,
    TaskProfileDefinition,
    TaskRequirement,
)
from .simulation import SimulationOverrides, SimulationResult, simulate_selection
from .status import Clock, StatusCollectors, collect_status

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = REPO_ROOT / "model-catalog.json"
DEFAULT_MODEL_POLICY_PATH = REPO_ROOT / "model-policy.json"


# ── Strict JSON loading ───────────────────────────────────────────────────────


def _as_str_object_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary mapping to ``str`` keys, or return ``None``."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"strict JSON: duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError(f"strict JSON: non-finite constant {name!r} is not accepted")


def load_strict_json(text: str, *, label: str) -> object:
    """Parse JSON strictly; duplicate keys and non-finite constants fail."""
    try:
        return cast(
            object,
            json.loads(
                text,
                object_pairs_hook=_reject_duplicate_key,
                parse_constant=_reject_constant,
            ),
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{label}: malformed JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc


def _load_json_file(path: Path, *, label: str) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{label}: cannot read {path}: {exc.strerror}") from exc
    return load_strict_json(text, label=label)


# ── Artifact loaders ──────────────────────────────────────────────────────────


def load_catalog(path: Path) -> ModelCatalog:
    """Load and validate a model-catalog artifact through ``ModelCatalog``."""
    document = _load_json_file(path, label="model catalog")
    return ModelCatalog.from_dict(document)


def load_model_policy(path: Path) -> tuple[TaskProfileCatalog, int]:
    """Build the ``TaskProfileCatalog`` from the calibrated profile artifact.

    Only ``task_profiles[].id`` and ``task_profiles[].calibrated_requirement``
    feed selection; model classes, workflow exemplars, indicative capability
    needs and descriptive role text are never used. The artifact's
    ``policy_version`` is returned for decision provenance.
    """
    document = _load_json_file(path, label="model policy")
    mapping = cast(Mapping[str, object] | None, document if isinstance(document, Mapping) else None)
    if mapping is None:
        raise SelectionContractValidationError(
            "model policy: expected a JSON object at the top level"
        )
    raw_version = mapping.get("policy_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
        raise SelectionContractValidationError(
            f"model policy: policy_version must be an integer >= 1, got {raw_version!r}"
        )
    raw_profiles = mapping.get("task_profiles")
    if not isinstance(raw_profiles, list):
        raise SelectionContractValidationError(
            "model policy: task_profiles must be a list, got "
            + f"{type(raw_profiles).__name__}"
        )
    definitions: list[TaskProfileDefinition] = []
    for item in cast("list[object]", raw_profiles):
        entry = cast(Mapping[str, object] | None, item if isinstance(item, Mapping) else None)
        if entry is None:
            raise SelectionContractValidationError(
                "model policy: each task_profiles entry must be an object"
            )
        profile_id = entry.get("id")
        if not isinstance(profile_id, str) or not profile_id:
            raise SelectionContractValidationError(
                "model policy: task_profiles[].id must be a non-empty string"
            )
        raw_requirement = _as_str_object_mapping(entry.get("calibrated_requirement"))
        if raw_requirement is None:
            raise SelectionContractValidationError(
                f"model policy: task profile {profile_id!r} has no "
                + "calibrated_requirement object; it is the sole "
                + "selector-facing numeric definition"
            )
        definitions.append(
            TaskProfileDefinition(
                profile_id=profile_id,
                requirement=TaskRequirement.from_dict(raw_requirement),
            )
        )
    return TaskProfileCatalog(definitions=tuple(definitions)), raw_version


def load_selector_policy(path: Path) -> SelectorPolicy:
    """Load a user selector-policy file through ``SelectorPolicy.from_dict``."""
    document = _load_json_file(path, label="selector policy")
    return SelectorPolicy.from_dict(document)


def load_replenishment_states(path: Path) -> tuple[ReplenishmentState, ...]:
    """Load a normalized replenishment list through ``ReplenishmentState``.

    Only the simple normalized JSON list shape is accepted; no
    provider-specific shape is accepted here.
    """
    document = _load_json_file(path, label="replenishment states")
    if not isinstance(document, list):
        raise SelectionContractValidationError(
            "replenishment states: expected a JSON list, got "
            + f"{type(document).__name__}"
        )
    return tuple(
        ReplenishmentState.from_dict(item) for item in cast("list[object]", document)
    )


def load_requirement(path: Path) -> TaskRequirement:
    """Load an explicit full ``TaskRequirement`` JSON file."""
    document = _load_json_file(path, label="task requirement")
    return TaskRequirement.from_dict(document)


def load_simulation_overrides(path: Path) -> SimulationOverrides:
    """Load a simulation-overrides file through ``SimulationOverrides.from_dict``."""
    document = _load_json_file(path, label="simulation overrides")
    return SimulationOverrides.from_dict(document)


# ── Requirement resolution ────────────────────────────────────────────────────


def resolve_requirement(
    *,
    profiles: TaskProfileCatalog,
    profile_id: str | None,
    explicit_requirement: TaskRequirement | None,
    tightening: TaskRequirement | None,
) -> tuple[TaskRequirement, str | None]:
    """Resolve the selector requirement from the two application paths.

    Profile path: ``profile_id`` resolves through ``TaskProfileCatalog``
    (the only production profile resolver) and may optionally be tightened
    by a full ``TaskRequirement`` under the monotone rules. Explicit path: a
    complete ``TaskRequirement`` supplied directly; tightening is not
    permitted with it. Returns the resolved requirement and the profile id
    (``None`` on the explicit path).
    """
    if explicit_requirement is not None:
        if tightening is not None:
            raise SelectionContractValidationError(
                "resolve_requirement: tightening is only permitted with the "
                + "profile path, never with an explicit requirement"
            )
        return explicit_requirement, None
    if profile_id is None:
        raise SelectionContractValidationError(
            "resolve_requirement: exactly one requirement source is required: "
            + "a profile id or an explicit TaskRequirement"
        )
    base = profiles.resolve(profile_id)
    if tightening is None:
        return base, profile_id
    return tighten_requirement(base, tightening), profile_id


# ── Application runner ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SelectionApplication:
    """Injected dependencies with a seam for synthetic application tests."""

    collectors: StatusCollectors | None = None
    clock: Clock | None = None


def _current_instant(clock: Clock | None) -> datetime:
    """Obtain the single timezone-aware evaluation instant for one invocation."""
    instant = clock() if clock is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(
            "selection clock must return a timezone-aware datetime, got a naive datetime"
        )
    return instant


def _load_selection_inputs(
    *,
    catalog_path: Path,
    model_policy_path: Path,
    profile_id: str | None,
    requirement_path: Path | None,
    tighten_path: Path | None,
    selector_policy_path: Path | None,
    replenishment_path: Path | None,
) -> tuple[
    ModelCatalog,
    int,
    TaskRequirement,
    str | None,
    SelectorPolicy,
    tuple[ReplenishmentState, ...],
]:
    catalog = load_catalog(catalog_path)
    profiles, profile_policy_version = load_model_policy(model_policy_path)
    explicit_requirement = (
        load_requirement(requirement_path) if requirement_path is not None else None
    )
    tightening = load_requirement(tighten_path) if tighten_path is not None else None
    requirement, resolved_profile_id = resolve_requirement(
        profiles=profiles,
        profile_id=profile_id,
        explicit_requirement=explicit_requirement,
        tightening=tightening,
    )
    policy = (
        load_selector_policy(selector_policy_path)
        if selector_policy_path is not None
        else neutral_selector_policy()
    )
    states = (
        load_replenishment_states(replenishment_path)
        if replenishment_path is not None
        else ()
    )
    return (
        catalog,
        profile_policy_version,
        requirement,
        resolved_profile_id,
        policy,
        states,
    )


def run_select(
    *,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
    model_policy_path: Path = DEFAULT_MODEL_POLICY_PATH,
    profile_id: str | None = None,
    requirement_path: Path | None = None,
    tighten_path: Path | None = None,
    selector_policy_path: Path | None = None,
    replenishment_path: Path | None = None,
    collectors: StatusCollectors | None = None,
    clock: Clock | None = None,
) -> SelectionDecision:
    """Run one live selection: load artifacts, collect status, select.

    Issues no model prompt and consumes no inference quota; capacity
    collection uses the existing telemetry path and may exercise the bounded
    provider-managed authentication recovery already accepted in D-018.
    """
    (
        catalog,
        profile_policy_version,
        requirement,
        resolved_profile_id,
        policy,
        states,
    ) = _load_selection_inputs(
        catalog_path=catalog_path,
        model_policy_path=model_policy_path,
        profile_id=profile_id,
        requirement_path=requirement_path,
        tighten_path=tighten_path,
        selector_policy_path=selector_policy_path,
        replenishment_path=replenishment_path,
    )
    instant = _current_instant(clock)

    def fixed_clock() -> datetime:
        return instant

    snapshots = collect_status(collectors=collectors, clock=fixed_clock)
    return select_model(
        catalog=catalog,
        requirement=requirement,
        policy=policy,
        snapshots=snapshots,
        evaluated_at=instant,
        replenishment_states=states,
        profile_id=resolved_profile_id,
        profile_policy_version=(
            profile_policy_version if resolved_profile_id is not None else None
        ),
    )


def run_simulate(
    *,
    overrides_path: Path,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
    model_policy_path: Path = DEFAULT_MODEL_POLICY_PATH,
    profile_id: str | None = None,
    requirement_path: Path | None = None,
    tighten_path: Path | None = None,
    selector_policy_path: Path | None = None,
    replenishment_path: Path | None = None,
    collectors: StatusCollectors | None = None,
    clock: Clock | None = None,
) -> SimulationResult:
    """Run one live baseline + simulated selection over the same selector core."""
    (
        catalog,
        profile_policy_version,
        requirement,
        resolved_profile_id,
        policy,
        states,
    ) = _load_selection_inputs(
        catalog_path=catalog_path,
        model_policy_path=model_policy_path,
        profile_id=profile_id,
        requirement_path=requirement_path,
        tighten_path=tighten_path,
        selector_policy_path=selector_policy_path,
        replenishment_path=replenishment_path,
    )
    overrides = load_simulation_overrides(overrides_path)
    instant = _current_instant(clock)

    def fixed_clock() -> datetime:
        return instant

    snapshots = collect_status(collectors=collectors, clock=fixed_clock)
    return simulate_selection(
        catalog=catalog,
        requirement=requirement,
        policy=policy,
        snapshots=snapshots,
        evaluated_at=instant,
        overrides=overrides,
        replenishment_states=states,
        profile_id=resolved_profile_id,
        profile_policy_version=(
            profile_policy_version if resolved_profile_id is not None else None
        ),
    )


# ── Deterministic rendering ───────────────────────────────────────────────────


def _identity_label(candidate: CandidateEvaluation) -> str:
    identity = candidate.identity
    return (
        f"{candidate.display_name} "
        + f"({identity.provider}/{identity.model}/{identity.variant})"
    )


def _requirement_lines(requirement: TaskRequirement) -> list[str]:
    minima_pairs: tuple[tuple[str, int | None], ...] = (
        ("reasoning", requirement.capability_minima.reasoning),
        ("coding", requirement.capability_minima.coding),
        (
            "scientific_methodological",
            requirement.capability_minima.scientific_methodological,
        ),
        ("writing_editorial", requirement.capability_minima.writing_editorial),
        ("tool_use", requirement.capability_minima.tool_use),
        (
            "translation_multilingual",
            requirement.capability_minima.translation_multilingual,
        ),
    )
    minima = [f"{name}>={value}" for name, value in minima_pairs if value is not None]
    hard = requirement.hard_constraints
    constraints: list[str] = []
    if hard.minimum_input_context_tokens is not None:
        constraints.append(
            f"minimum_input_context_tokens={hard.minimum_input_context_tokens}"
        )
    if hard.minimum_output_tokens is not None:
        constraints.append(f"minimum_output_tokens={hard.minimum_output_tokens}")
    for name, value in (
        ("requires_tool_use", hard.requires_tool_use),
        ("requires_vision", hard.requires_vision),
        ("requires_reasoning_mode", hard.requires_reasoning_mode),
    ):
        if value:
            constraints.append(name)
    if hard.required_provider is not None:
        constraints.append(f"required_provider={hard.required_provider}")
    if hard.required_model is not None:
        constraints.append(
            f"required_model={hard.required_model.provider}/{hard.required_model.model}"
        )
    if hard.required_variant is not None:
        constraints.append(f"required_variant={hard.required_variant}")
    if hard.privacy_constraint is not None:
        constraints.append(f"privacy_constraint={hard.privacy_constraint}")
    return [
        f"Task level: {requirement.task_level}",
        "Capability minima: " + (", ".join(minima) if minima else "none"),
        "Hard constraints: " + (", ".join(constraints) if constraints else "none"),
    ]


def _scarcity_line(candidate: CandidateEvaluation) -> str:
    scarcity = candidate.scarcity_assessment
    if scarcity is None:
        return "Scarcity: not assessed"
    if scarcity.state == "known":
        return (
            f"Scarcity: {scarcity.label} — "
            + f"{scarcity.effective_remaining_percent}% remaining, "
            + f"penalty {scarcity.penalty_units}"
        )
    if scarcity.state == "unavailable":
        return "Scarcity: unavailable — capacity exhausted (penalty 10000)"
    return "Scarcity: unknown — " + ",".join(scarcity.reason_codes)


def _failure_lines(candidate: CandidateEvaluation) -> list[str]:
    lines: list[str] = []
    for failure in candidate.hard_constraint_failures:
        detail = f"required {failure.required_value}"
        if failure.actual_value is not None:
            detail += f", actual {failure.actual_value}"
        lines.append(f"{failure.constraint} {failure.reason}: {detail}")
    for failure in candidate.capability_failures:
        detail = f"required >= {failure.required_rating}"
        if failure.actual_rating is not None:
            detail += f", actual {failure.actual_rating}"
        lines.append(f"{failure.dimension} {failure.reason}: {detail}")
    return lines


def _replenishment_label(decision: ReplenishmentDecision) -> str:
    if not decision.visible:
        return "not visible"
    parts = [f"mode {decision.mode}", f"available {decision.available_count}"]
    if decision.recoverable:
        parts.append("human action required")
    if decision.reason_codes:
        parts.append(",".join(decision.reason_codes))
    return ", ".join(parts)


def _short_state(candidate: CandidateEvaluation) -> str:
    scarcity = candidate.scarcity_assessment
    if scarcity is not None and scarcity.state == "known":
        return (
            f"{scarcity.label}, penalty {scarcity.penalty_units}, "
            + f"margin {candidate.capability_margin}"
        )
    return f"degraded unknown capacity, margin {candidate.capability_margin}"


def _explain_sections(decision: SelectionDecision) -> list[str]:
    lines: list[str] = []
    lines.append("Resolved requirement:")
    lines.extend("  " + line for line in _requirement_lines(decision.requirement))

    if decision.selected is not None:
        selected = decision.selected
        scarcity = selected.scarcity_assessment
        capacity_class = (
            "known"
            if scarcity is not None and scarcity.state == "known"
            else "unknown/degraded"
        )
        lines.append("Selected:")
        lines.append(f"  {_identity_label(selected)}")
        lines.append(f"  {_scarcity_line(selected)}")
        lines.append(
            f"  Ranking: capacity={capacity_class}, "
            + f"capability margin={selected.capability_margin}, "
            + f"mode={decision.selector_mode}"
        )
        if selected.replenishment_evaluations:
            lines.append("  Replenishment (advisory, never current capacity):")
            lines.extend(
                "    - " + _replenishment_label(evaluation)
                for evaluation in selected.replenishment_evaluations
            )
    else:
        lines.append("No eligible candidate.")
        if decision.closest_candidates:
            lines.append("Closest candidates (a later selector stage is closer):")
            lines.extend(
                "  - "
                + _identity_label(candidate)
                + f" — excluded at {candidate.exclusion_stage}"
                for candidate in decision.closest_candidates
            )

    if decision.alternatives:
        lines.append("Alternatives (exact ranking order):")
        for index, alternative in enumerate(decision.alternatives, start=1):
            lines.append(
                f"  {index}. {_identity_label(alternative)} — "
                + _short_state(alternative)
            )

    grouped: dict[str, list[CandidateEvaluation]] = {}
    for candidate in decision.excluded:
        if candidate.exclusion_stage is not None:
            grouped.setdefault(candidate.exclusion_stage, []).append(candidate)
    if grouped:
        lines.append("Excluded candidates:")
        for stage in EXCLUSION_STAGES:
            stage_candidates = grouped.get(stage)
            if not stage_candidates:
                continue
            lines.append(f"  {stage}:")
            for candidate in stage_candidates:
                lines.append(f"    - {_identity_label(candidate)}")
                for failure in _failure_lines(candidate):
                    lines.append(f"      {failure}")
                if candidate.exclusion_stage == "policy_blackout":
                    blackout = candidate.blackout_decision
                    if blackout is not None and blackout.rule_id is not None:
                        lines.append(
                            f"      blackout rule {blackout.rule_id} "
                            + f"({blackout.reason_code})"
                        )
                if candidate.exclusion_stage == "capacity":
                    scarcity = candidate.scarcity_assessment
                    if scarcity is not None:
                        lines.append(
                            f"      scarcity state {scarcity.state}, codes: "
                            + ",".join(scarcity.reason_codes)
                        )
                if candidate.exclusion_stage == "reservation":
                    for reservation in candidate.reservation_decisions:
                        lines.append(
                            f"      reservation {reservation.rule_id}: state "
                            + f"{reservation.state}, codes: "
                            + ",".join(reservation.reason_codes)
                        )

    if decision.recoverable_candidates:
        lines.append(
            "Recoverable candidates (replenishment opportunity — NOT current capacity):"
        )
        lines.extend(
            "  - " + _identity_label(candidate) + " — human action required"
            for candidate in decision.recoverable_candidates
        )

    versions = (
        f"Versions: catalog {decision.catalog_version} "
        + f"({decision.catalog_updated_on}); resource policy v"
        + f"{decision.resource_policy_version}; mode {decision.selector_mode}"
    )
    if decision.profile_policy_version is not None:
        versions += f"; profile policy v{decision.profile_policy_version}"
    lines.append(versions)
    return lines


def render_select_human(decision: SelectionDecision, *, explain: bool = False) -> str:
    """Compact deterministic human output; ``--explain`` adds full detail."""
    lines: list[str] = []
    if decision.selected is not None:
        selected = decision.selected
        if decision.degraded:
            lines.append("WARNING: selected with unknown capacity")
            unknown = selected.unknown_capacity_decision
            if unknown is not None and unknown.reason_codes:
                lines.append("Unknown reason: " + ",".join(unknown.reason_codes))
        lines.append(f"Selected: {_identity_label(selected)}")
        lines.append(_scarcity_line(selected))
        lines.append(f"Capability margin: {selected.capability_margin}")
    else:
        lines.append("No eligible candidate")
        if decision.closest_candidates:
            lines.append("Closest candidates:")
            lines.extend(
                "  - "
                + _identity_label(candidate)
                + f" — excluded at {candidate.exclusion_stage}"
                for candidate in decision.closest_candidates
            )
        if decision.recoverable_candidates:
            lines.append(
                "Recoverable candidates (replenishment opportunity — NOT current capacity):"
            )
            lines.extend(
                "  - "
                + _identity_label(candidate)
                + " — human action required"
                for candidate in decision.recoverable_candidates
            )
    if decision.profile_id is not None:
        lines.append(f"Profile: {decision.profile_id}")
    else:
        lines.append("Requirement: explicit")
    lines.append(f"Policy: {decision.selector_mode}")
    lines.append(f"Reason: {','.join(decision.reason_codes)}")
    if explain:
        lines.append("")
        lines.extend(_explain_sections(decision))
    return "\n".join(lines) + "\n"


def render_decision_json(decision: SelectionDecision) -> str:
    """The complete deterministic ``SelectionDecision.to_dict()`` document."""
    return json.dumps(decision.to_dict(), indent=2, sort_keys=True) + "\n"


def render_simulation_human(result: SimulationResult, *, explain: bool = False) -> str:
    """Human output clearly distinguishing CURRENT from SIMULATED."""
    lines: list[str] = ["CURRENT"]
    lines.extend(render_select_human(result.baseline, explain=explain).rstrip("\n").split("\n"))
    lines.append("SIMULATED")
    lines.extend(
        render_select_human(result.simulated, explain=explain).rstrip("\n").split("\n")
    )
    overrides = result.applied_overrides
    lines.append("Overrides applied:")
    if overrides.capacity_percentages:
        lines.extend(
            "  capacity: "
            + f"{override.provider}/{override.scope_id} {override.resource} "
            + f"{override.kind} -> {override.remaining_percent}% remaining"
            + (f" (window {override.window_id})" if override.window_id else "")
            for override in overrides.capacity_percentages
        )
    else:
        lines.append("  capacity: none")
    if overrides.selector_policy is not None:
        lines.append("  selector policy: replaced")
    if overrides.replenishment_states is not None:
        lines.append(
            "  replenishment: replaced ("
            + f"{len(overrides.replenishment_states)} observation(s))"
        )
    if overrides.evaluated_at is not None:
        lines.append("  evaluated_at: moved")
    return "\n".join(lines) + "\n"


def render_simulation_json(result: SimulationResult) -> str:
    """The complete deterministic simulation document."""
    return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"


__all__ = [
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_MODEL_POLICY_PATH",
    "SelectionApplication",
    "load_catalog",
    "load_model_policy",
    "load_replenishment_states",
    "load_requirement",
    "load_selector_policy",
    "load_simulation_overrides",
    "load_strict_json",
    "render_decision_json",
    "render_select_human",
    "render_simulation_human",
    "render_simulation_json",
    "resolve_requirement",
    "run_select",
    "run_simulate",
]
