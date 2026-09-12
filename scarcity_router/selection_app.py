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

Since M3b (D-030) the application layer also exposes a typed in-memory seam
(``select_from_inputs`` / ``simulate_from_inputs``) that takes already-typed
inputs instead of file paths. The file-based CLI runners and the
machine-interface adapters (REST and MCP) call the same seam, so no
transport owns requirement resolution or application semantics.
Caller-supplied input violations raise ``ApplicationInputError`` so adapters
can classify client errors by type (D-030).

The default artifact paths resolve through ``resolve_default_artifact``:
the repository-root ``model-catalog.json`` and ``model-policy.json`` win in
a source tree (the single committed authoritative copies), and the packaged
resource copies inside ``scarcity_router`` are used by an installed
package (D-034). Explicit path overrides always replace the defaults.

Strict JSON loading uses only the standard library: ``object_pairs_hook``
rejects duplicate object keys, ``parse_constant`` rejects NaN/Infinity and
malformed JSON raises ``ValueError`` — never a raw payload dump.
"""

from __future__ import annotations

import importlib.resources
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .errors import (
    ApplicationInputError,
    SelectionContractValidationError,
    SimulationOverrideApplicationError,
)
from .policy import ReplenishmentState, ReservationDecision
from .selector import (
    EXCLUSION_STAGES,
    CandidateEvaluation,
    ReplenishmentEvaluation,
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


def resolve_default_artifact(name: str, *, source_root: Path = REPO_ROOT) -> Path:
    """Resolve one default artifact path for source-tree and installed runs.

    Source tree: the repository-root authoritative copy wins, so a checkout
    always uses the single committed root artifact and never depends on
    packaging state. Installed package: the packaged resource copy inside
    ``scarcity_router`` (mapped into the wheel at build time; no committed
    duplicate) is used. Resolution depends only on the module location,
    never on the working directory or environment variables. Explicit
    ``--catalog`` / ``--model-policy`` overrides bypass it entirely.
    """
    source_copy = source_root / name
    if source_copy.is_file():
        return source_copy
    # Regular (wheel) installs resolve to a real filesystem path; the
    # context manager only matters for archive-based imports.
    with importlib.resources.as_file(
        importlib.resources.files("scarcity_router").joinpath(name)
    ) as resource_path:
        return resource_path


DEFAULT_CATALOG_PATH = resolve_default_artifact("model-catalog.json")
DEFAULT_MODEL_POLICY_PATH = resolve_default_artifact("model-policy.json")


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

    Caller-supplied input violations — the exclusivity rules, an unknown
    profile id and a non-monotone tightening — raise
    :class:`ApplicationInputError` so machine-interface adapters can classify
    them as client errors by type (D-030).
    """
    if explicit_requirement is not None:
        if profile_id is not None:
            raise ApplicationInputError(
                "resolve_requirement: exactly one requirement source is "
                + "permitted: a profile id or an explicit TaskRequirement, "
                + "never both"
            )
        if tightening is not None:
            raise ApplicationInputError(
                "resolve_requirement: tightening is only permitted with the "
                + "profile path, never with an explicit requirement"
            )
        return explicit_requirement, None
    if profile_id is None:
        raise ApplicationInputError(
            "resolve_requirement: exactly one requirement source is required: "
            + "a profile id or an explicit TaskRequirement"
        )
    try:
        base = profiles.resolve(profile_id)
    except SelectionContractValidationError as exc:
        raise ApplicationInputError(
            f"resolve_requirement: unknown profile id {profile_id!r}"
        ) from exc
    if tightening is None:
        return base, profile_id
    try:
        return tighten_requirement(base, tightening), profile_id
    except SelectionContractValidationError as exc:
        raise ApplicationInputError(
            f"resolve_requirement: invalid tightening: {exc}"
        ) from exc


# ── Application runner ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApplicationDependencies:
    """Process-configured dependencies shared by machine transports.

    Artifact paths and the default selector policy are server
    configuration, never client-controlled request data. Collectors and
    the clock remain injectable for deterministic tests; production callers
    leave them as ``None``. ``default_policy`` (D-036) is the server-side
    policy loaded from the default user configuration; a request that
    supplies its own ``selector_policy`` always wins over it, and a missing
    default keeps the documented neutral policy.
    """

    catalog_path: Path = DEFAULT_CATALOG_PATH
    model_policy_path: Path = DEFAULT_MODEL_POLICY_PATH
    default_policy: SelectorPolicy | None = None
    collectors: StatusCollectors | None = None
    clock: Clock | None = None


# Retain the earlier development-only name while transports use the neutral
# dependency record. The alias has identical construction and field semantics.
SelectionApplication = ApplicationDependencies


def _current_instant(clock: Clock | None) -> datetime:
    """Obtain the single timezone-aware evaluation instant for one invocation."""
    instant = clock() if clock is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(
            "selection clock must return a timezone-aware datetime, got a naive datetime"
        )
    return instant


def load_configured_artifacts(
    catalog_path: Path,
    model_policy_path: Path,
) -> tuple[ModelCatalog, TaskProfileCatalog, int]:
    """Load the two process-configured application artifacts.

    Catalog and profile artifacts are server configuration, never
    per-request inputs (D-028): every caller — CLI runner or machine
    adapter — loads them through this one authoritative loader. A loading
    or validation failure is a server configuration failure, not a
    client-input error.
    """
    catalog = load_catalog(catalog_path)
    profiles, profile_policy_version = load_model_policy(model_policy_path)
    return catalog, profiles, profile_policy_version


# ── Typed in-memory application seam (M3b, D-030) ────────────────────────────


def select_from_inputs(
    *,
    catalog: ModelCatalog,
    profiles: TaskProfileCatalog,
    profile_policy_version: int,
    profile_id: str | None,
    requirement: TaskRequirement | None,
    tightening: TaskRequirement | None,
    policy: SelectorPolicy | None,
    replenishment_states: tuple[ReplenishmentState, ...] = (),
    collectors: StatusCollectors | None = None,
    clock: Clock | None = None,
) -> SelectionDecision:
    """Run one selection from typed in-memory inputs — the shared seam.

    This is the single application path behind the file-based CLI runners
    and the machine-interface adapters: requirement resolution through the
    existing authoritative mechanism (profile XOR explicit requirement,
    optional monotone tightening applied exactly once), ONE aware
    evaluation instant, one ``collect_status`` observation with a fixed
    clock so both provider snapshots share it, then the pure
    ``select_model`` core. A missing policy means the documented neutral
    policy. Caller-supplied input violations raise
    :class:`ApplicationInputError`; other failures are server failures and
    are not classified here.
    """
    resolved_requirement, resolved_profile_id = resolve_requirement(
        profiles=profiles,
        profile_id=profile_id,
        explicit_requirement=requirement,
        tightening=tightening,
    )
    instant = _current_instant(clock)

    def fixed_clock() -> datetime:
        return instant

    snapshots = collect_status(collectors=collectors, clock=fixed_clock)
    return select_model(
        catalog=catalog,
        requirement=resolved_requirement,
        policy=policy if policy is not None else neutral_selector_policy(),
        snapshots=snapshots,
        evaluated_at=instant,
        replenishment_states=replenishment_states,
        profile_id=resolved_profile_id,
        profile_policy_version=(
            profile_policy_version if resolved_profile_id is not None else None
        ),
    )


def simulate_from_inputs(
    *,
    catalog: ModelCatalog,
    profiles: TaskProfileCatalog,
    profile_policy_version: int,
    profile_id: str | None,
    requirement: TaskRequirement | None,
    tightening: TaskRequirement | None,
    policy: SelectorPolicy | None,
    replenishment_states: tuple[ReplenishmentState, ...] = (),
    overrides: SimulationOverrides,
    collectors: StatusCollectors | None = None,
    clock: Clock | None = None,
) -> SimulationResult:
    """Run one baseline + simulated selection from typed in-memory inputs.

    The baseline and simulated decisions are both produced by the SAME
    ``select_model`` core through ``simulate_selection``; the seam adds no
    simulation semantics of its own. See :func:`select_from_inputs` for the
    shared selection-input handling.
    """
    resolved_requirement, resolved_profile_id = resolve_requirement(
        profiles=profiles,
        profile_id=profile_id,
        explicit_requirement=requirement,
        tightening=tightening,
    )
    instant = _current_instant(clock)

    def fixed_clock() -> datetime:
        return instant

    snapshots = collect_status(collectors=collectors, clock=fixed_clock)
    try:
        return simulate_selection(
            catalog=catalog,
            requirement=resolved_requirement,
            policy=policy if policy is not None else neutral_selector_policy(),
            snapshots=snapshots,
            evaluated_at=instant,
            overrides=overrides,
            replenishment_states=replenishment_states,
            profile_id=resolved_profile_id,
            profile_policy_version=(
                profile_policy_version if resolved_profile_id is not None else None
            ),
        )
    except SimulationOverrideApplicationError as exc:
        raise ApplicationInputError(
            f"simulate_from_inputs: invalid simulation override: {exc}"
        ) from exc


# ── File-based application runners ───────────────────────────────────────────


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

    Selection issues no model prompt and does not intentionally consume
    inference quota. Capacity collection uses the existing telemetry path
    and may exercise the bounded provider-managed authentication recovery
    already accepted in D-018. Delegates to the typed
    :func:`select_from_inputs` seam after loading the JSON files.
    """
    catalog, profiles, profile_policy_version = load_configured_artifacts(
        catalog_path, model_policy_path
    )
    return select_from_inputs(
        catalog=catalog,
        profiles=profiles,
        profile_policy_version=profile_policy_version,
        profile_id=profile_id,
        requirement=(
            load_requirement(requirement_path)
            if requirement_path is not None
            else None
        ),
        tightening=(
            load_requirement(tighten_path) if tighten_path is not None else None
        ),
        policy=(
            load_selector_policy(selector_policy_path)
            if selector_policy_path is not None
            else None
        ),
        replenishment_states=(
            load_replenishment_states(replenishment_path)
            if replenishment_path is not None
            else ()
        ),
        collectors=collectors,
        clock=clock,
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
    """Run one live baseline + simulated selection over the same selector core.

    Delegates to the typed :func:`simulate_from_inputs` seam after loading
    the JSON files.
    """
    catalog, profiles, profile_policy_version = load_configured_artifacts(
        catalog_path, model_policy_path
    )
    return simulate_from_inputs(
        catalog=catalog,
        profiles=profiles,
        profile_policy_version=profile_policy_version,
        profile_id=profile_id,
        requirement=(
            load_requirement(requirement_path)
            if requirement_path is not None
            else None
        ),
        tightening=(
            load_requirement(tighten_path) if tighten_path is not None else None
        ),
        policy=(
            load_selector_policy(selector_policy_path)
            if selector_policy_path is not None
            else None
        ),
        replenishment_states=(
            load_replenishment_states(replenishment_path)
            if replenishment_path is not None
            else ()
        ),
        overrides=load_simulation_overrides(overrides_path),
        collectors=collectors,
        clock=clock,
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


def _governing_line(candidate: CandidateEvaluation) -> str | None:
    """One-line governing capacity evidence, or None when it does not exist.

    Unknown scarcity has no governing window; none is ever invented. The
    diagnostic ``window_id`` is rendered verbatim and never parsed.
    """
    scarcity = candidate.scarcity_assessment
    if scarcity is None or scarcity.governing_window is None:
        return None
    window = scarcity.governing_window
    scope = window.scope
    detail = (
        f"Governing capacity: {scope.provider}/{scope.scope_id} "
        + f"{window.resource} {window.kind} — "
        + f"{window.remaining_percent}% remaining"
    )
    if window.window_id is not None:
        detail += f" (window {window.window_id})"
    return detail


def _reservation_detail_lines(decisions: tuple[ReservationDecision, ...]) -> list[str]:
    """Deterministic detail lines for one candidate's reservation decisions."""
    lines: list[str] = []
    for decision in decisions:
        parts = [f"state={decision.state}"]
        if decision.triggered is not None:
            parts.append(f"triggered={'true' if decision.triggered else 'false'}")
        if decision.blocked is not None:
            parts.append(f"blocked={'true' if decision.blocked else 'false'}")
        if decision.evidence_remaining_percent is not None:
            parts.append(
                f"remaining={decision.evidence_remaining_percent}%"
            )
        if decision.reason_codes:
            parts.append("codes: " + ",".join(decision.reason_codes))
        lines.append(f"- {decision.rule_id}: " + ", ".join(parts))
    return lines


def _replenishment_label(evaluation: ReplenishmentEvaluation) -> str:
    """One observation's provenance next to its policy decision."""
    state = evaluation.state
    decision = evaluation.decision
    parts = [
        f"{state.provider}/{state.kind}",
        f"available {state.available_count}",
        f"retrieved {state.retrieved_at}",
    ]
    if state.earliest_expiry is not None:
        parts.append(f"earliest expiry {state.earliest_expiry}")
    parts.append(f"mode={decision.mode}")
    if decision.visible:
        if decision.recoverable:
            parts.append("human action required")
        if decision.reason_codes:
            parts.append("codes: " + ",".join(decision.reason_codes))
    else:
        parts.append("not visible")
    return ", ".join(parts)


def _short_state(candidate: CandidateEvaluation) -> str:
    scarcity = candidate.scarcity_assessment
    if scarcity is not None and scarcity.state == "known":
        return (
            f"{scarcity.label}, penalty {scarcity.penalty_units}, "
            + f"margin {candidate.capability_margin}"
        )
    return f"degraded unknown capacity, margin {candidate.capability_margin}"


def _happy_hour_lines(candidate: CandidateEvaluation) -> list[str]:
    """Explanation lines for a candidate's happy-hour quota preference."""
    decision = candidate.happy_hour_decision
    if decision is None:
        return []
    return [
        f"Happy hour: rule {decision.rule_id} ({decision.reason_code}) — "
        + "quota-preference window active (ranking preference only)"
    ]


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
        lines.extend("  " + line for line in _happy_hour_lines(selected))
        governing = _governing_line(selected)
        if governing is not None:
            lines.append(f"  {governing}")
        if selected.reservation_decisions:
            lines.append("  Reservations:")
            lines.extend(
                "    " + line
                for line in _reservation_detail_lines(
                    selected.reservation_decisions
                )
            )
        if selected.replenishment_evaluations:
            lines.append("  Replenishment (visibility only, never current capacity):")
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

    if decision.preference_order:
        lines.append("Preference order:")
        lines.extend(
            f"  {index}. {entry.provider}/{entry.model}/{entry.variant}"
            for index, entry in enumerate(decision.preference_order, start=1)
        )
    else:
        lines.append("Preference order: none")

    if decision.alternatives:
        lines.append("Alternatives (exact ranking order):")
        for index, alternative in enumerate(decision.alternatives, start=1):
            lines.append(
                f"  {index}. {_identity_label(alternative)} — "
                + _short_state(alternative)
            )
            lines.extend("     " + line for line in _happy_hour_lines(alternative))
            governing = _governing_line(alternative)
            if governing is not None:
                lines.append(f"     {governing}")
            if alternative.reservation_decisions:
                lines.append("     Reservations:")
                lines.extend(
                    "       " + line
                    for line in _reservation_detail_lines(
                        alternative.reservation_decisions
                    )
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
                    governing = _governing_line(candidate)
                    if governing is not None:
                        lines.append(f"      {governing}")
                if candidate.exclusion_stage == "reservation":
                    for line in _reservation_detail_lines(
                        candidate.reservation_decisions
                    ):
                        lines.append(f"      {line}")

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
    "ApplicationDependencies",
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_MODEL_POLICY_PATH",
    "SelectionApplication",
    "load_catalog",
    "load_configured_artifacts",
    "load_model_policy",
    "load_replenishment_states",
    "load_requirement",
    "resolve_default_artifact",
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
    "select_from_inputs",
    "simulate_from_inputs",
]
