#!/usr/bin/env python3
"""Deterministic evaluation of the dated delegated-model governance policy.

The development/agent-orchestration layer (which models may be delegated to
subagents, workflow workers, independent reviewers and confirmers) is
governed by the ``delegated_model_policy`` section of the repository-root
``model-policy.json`` (decision D-060; the authoritative prose lives in
``docs/llm-operating-policy.md``). This tool is that section's single
deterministic evaluator:

- ``standing_rules`` name models that must not be delegated (and the
  substitute to route to instead, with an explicit disclosure requirement —
  substitution is never silent);
- ``temporary_overrides`` suspend one standing rule for an inclusive local
  calendar window (``YYYY-MM-DD`` in the section calendar), mirroring the
  D-035/D-059 selector-policy date-bound convention. When the window ends
  the standing rule returns automatically — no manual revert.

The section is deliberately NOT selector-facing: it changes no runtime
routing, catalog rating, capacity applicability or quota calculation, and it
does not duplicate the runtime pricing calendar (campaign economics are
referenced by decision id, not re-encoded).

The clock is injectable through ``--at`` (any ISO-8601 instant with a UTC
offset; naive instants are rejected), so no invocation needs to depend on
the current date and boundary instants can be pinned exactly.

Usage:
  uv run python tools/model_governance.py --at 2026-09-30T15:00:00+08:00 \
      --model zai/glm-5.3
  uv run python tools/model_governance.py --at 2026-10-08T14:00:00+08:00 \
      --model zai/glm-5.3 --json

Exit codes: 0 the queried model is allowed, 3 it is restricted by an
in-force standing rule, 2 usage or policy-artifact error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = REPO_ROOT / "model-policy.json"
STRICT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

EXIT_ALLOWED = 0
EXIT_RESTRICTED = 3
EXIT_ERROR = 2


# ── artifact loading ──────────────────────────────────────────────────────────


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON object key: {key}")
        seen.add(key)
    result: dict[str, object] = dict(pairs)
    return result


def load_policy_document(text: str, *, label: str) -> dict[str, object]:
    """Strictly parse the policy artifact (no duplicate keys, no NaN)."""
    value = cast(object, json.loads(text, object_pairs_hook=_reject_duplicate_keys))
    if not isinstance(value, dict):
        raise ValueError(f"{label}: top level must be a JSON object")
    return cast("dict[str, object]", value)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return cast("dict[str, object]", value)


def _string(container: Mapping[str, object], key: str, label: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


def _object_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty JSON array")
    entries = cast("list[object]", value)
    return [
        _mapping(item, f"{label}[{index}]") for index, item in enumerate(entries)
    ]


def _model_ref(value: object, label: str) -> "ModelRef":
    entry = _mapping(value, label)
    return ModelRef(
        provider=_string(entry, "provider", label),
        model=_string(entry, "model", label),
    )


# ── validated policy view ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model: str

    def label(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class StandingRule:
    rule_id: str
    statement: str
    restricted_models: tuple[ModelRef, ...]
    substitute_model: ModelRef

    def restricts(self, candidate: ModelRef) -> bool:
        return any(
            ref.provider == candidate.provider and ref.model == candidate.model
            for ref in self.restricted_models
        )


@dataclass(frozen=True)
class TemporaryOverride:
    override_id: str
    suspends: str
    effective_from: date
    effective_until: date

    def covers(self, local_date: date) -> bool:
        return self.effective_from <= local_date <= self.effective_until


@dataclass(frozen=True)
class DelegatedModelPolicy:
    calendar: str
    preference_default: ModelRef
    standing_rules: tuple[StandingRule, ...]
    temporary_overrides: tuple[TemporaryOverride, ...]


def _strict_date(value: str, label: str) -> date:
    if not STRICT_DATE.match(value):
        raise ValueError(f"{label} must be formatted YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid calendar date: {value!r}") from exc


def validate_delegated_model_policy(section: object) -> DelegatedModelPolicy:
    """Validate the ``delegated_model_policy`` section and narrow its types."""
    root = _mapping(section, "delegated_model_policy")
    if root.get("selector_facing") is not False:
        raise ValueError(
            "delegated_model_policy.selector_facing must be false: the section "
            + "is governance metadata and never a selector input"
        )
    calendar = _string(root, "calendar", "delegated_model_policy")
    try:
        _ = ZoneInfo(calendar)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(
            f"delegated_model_policy.calendar is not an IANA zone: {calendar!r}"
        ) from exc

    preference = _model_ref(
        root.get("preference_default"),
        "delegated_model_policy.preference_default",
    )

    rules: list[StandingRule] = []
    rule_ids: set[str] = set()
    for index, entry in enumerate(
        _object_list(
            root.get("standing_rules"), "delegated_model_policy.standing_rules"
        )
    ):
        label = f"delegated_model_policy.standing_rules[{index}]"
        rule_id = _string(entry, "rule_id", label)
        if rule_id in rule_ids:
            raise ValueError(f"duplicate standing rule id: {rule_id}")
        rule_ids.add(rule_id)
        restricted_raw = entry.get("restricted_models")
        if not isinstance(restricted_raw, list) or not restricted_raw:
            raise ValueError(f"{label}.restricted_models must be a non-empty array")
        restricted_items = cast("list[object]", restricted_raw)
        restricted = tuple(
            _model_ref(item, f"{label}.restricted_models[{pos}]")
            for pos, item in enumerate(restricted_items)
        )
        rules.append(
            StandingRule(
                rule_id=rule_id,
                statement=_string(entry, "statement", label),
                restricted_models=restricted,
                substitute_model=_model_ref(
                    entry.get("substitute_model"), f"{label}.substitute_model"
                ),
            )
        )

    overrides: list[TemporaryOverride] = []
    override_ids: set[str] = set()
    for index, entry in enumerate(
        _object_list(
            root.get("temporary_overrides"),
            "delegated_model_policy.temporary_overrides",
        )
    ):
        label = f"delegated_model_policy.temporary_overrides[{index}]"
        override_id = _string(entry, "override_id", label)
        if override_id in override_ids:
            raise ValueError(f"duplicate temporary override id: {override_id}")
        override_ids.add(override_id)
        suspends = _string(entry, "suspends", label)
        if suspends not in rule_ids:
            raise ValueError(
                f"{label}.suspends references unknown standing rule: {suspends!r}"
            )
        effective_from = _strict_date(
            _string(entry, "effective_from", label), f"{label}.effective_from"
        )
        effective_until = _strict_date(
            _string(entry, "effective_until", label), f"{label}.effective_until"
        )
        if effective_from > effective_until:
            raise ValueError(
                f"{label}: effective_from {effective_from} is after "
                + f"effective_until {effective_until}"
            )
        overrides.append(
            TemporaryOverride(
                override_id=override_id,
                suspends=suspends,
                effective_from=effective_from,
                effective_until=effective_until,
            )
        )

    return DelegatedModelPolicy(
        calendar=calendar,
        preference_default=preference,
        standing_rules=tuple(rules),
        temporary_overrides=tuple(overrides),
    )


# ── evaluation ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelVerdict:
    requested: ModelRef
    allowed: bool
    restricted_by_rule_ids: tuple[str, ...]
    active_override_ids: tuple[str, ...]
    allowed_by_override_ids: tuple[str, ...]
    substitute: ModelRef | None
    disclosure_required: bool
    notes: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "requested": self.requested.label(),
            "allowed": self.allowed,
            "restricted_by_rule_ids": list(self.restricted_by_rule_ids),
            "active_override_ids": list(self.active_override_ids),
            "allowed_by_override_ids": list(self.allowed_by_override_ids),
            "substitute": self.substitute.label() if self.substitute else None,
            "disclosure_required": self.disclosure_required,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class GovernanceEvaluation:
    evaluated_at: datetime
    local_date: date
    calendar: str
    standing_rule_ids_in_force: tuple[str, ...]
    suspended_rule_ids: tuple[str, ...]
    active_override_ids: tuple[str, ...]
    preference_default: ModelRef

    def to_json(self) -> dict[str, object]:
        return {
            "evaluated_at": self.evaluated_at.isoformat(),
            "local_date": self.local_date.isoformat(),
            "calendar": self.calendar,
            "standing_rule_ids_in_force": list(self.standing_rule_ids_in_force),
            "suspended_rule_ids": list(self.suspended_rule_ids),
            "active_override_ids": list(self.active_override_ids),
            "preference_default": self.preference_default.label(),
        }


def evaluate_delegated_model_policy(
    policy: DelegatedModelPolicy, *, instant: datetime
) -> GovernanceEvaluation:
    """Evaluate which governance rules are in force at one aware instant."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(
            "timezone-aware instant required: naive instants are rejected "
            + "(the local calendar date would be ambiguous)"
        )
    local_date = instant.astimezone(ZoneInfo(policy.calendar)).date()
    active = tuple(
        override
        for override in policy.temporary_overrides
        if override.covers(local_date)
    )
    suspended = {override.suspends for override in active}
    in_force = tuple(
        rule for rule in policy.standing_rules if rule.rule_id not in suspended
    )
    return GovernanceEvaluation(
        evaluated_at=instant,
        local_date=local_date,
        calendar=policy.calendar,
        standing_rule_ids_in_force=tuple(rule.rule_id for rule in in_force),
        suspended_rule_ids=tuple(sorted(suspended)),
        active_override_ids=tuple(
            sorted(override.override_id for override in active)
        ),
        preference_default=policy.preference_default,
    )


def verdict_for(
    policy: DelegatedModelPolicy,
    evaluation: GovernanceEvaluation,
    requested: ModelRef,
) -> ModelVerdict:
    """One model's delegated-use verdict under the evaluated phase.

    Restriction is never silently substituted: a disallowed model carries the
    governing rule ids, the policy-directed substitute and a disclosure
    requirement, so an orchestrator applying it stays auditable. A model
    allowed only because an override suspended its restricting rule is
    explicitly labeled not-a-preference-default — the override restores
    eligibility and choice, never a cost ranking.
    """
    suspended_by_override = {
        override.suspends: override
        for override in policy.temporary_overrides
        if override.override_id in evaluation.active_override_ids
    }
    restricting = [
        rule
        for rule in policy.standing_rules
        if rule.rule_id in evaluation.standing_rule_ids_in_force
        and rule.restricts(requested)
    ]
    if restricting:
        substitute = restricting[0].substitute_model
        return ModelVerdict(
            requested=requested,
            allowed=False,
            restricted_by_rule_ids=tuple(rule.rule_id for rule in restricting),
            active_override_ids=evaluation.active_override_ids,
            allowed_by_override_ids=(),
            substitute=substitute,
            disclosure_required=True,
            notes=(
                "restricted by standing rule "
                + ", ".join(rule.rule_id for rule in restricting)
                + ": route to "
                + substitute.label()
                + " and state the substitution once (never silently)",
            ),
        )
    allowed_by = sorted(
        suspended_by_override[rule.rule_id].override_id
        for rule in policy.standing_rules
        if rule.restricts(requested) and rule.rule_id in suspended_by_override
    )
    notes: list[str] = []
    if allowed_by:
        notes.append(
            "allowed by temporary override "
            + ", ".join(allowed_by)
            + "; eligibility and choice, not preference"
        )
    if requested != evaluation.preference_default:
        notes.append(
            "not a preference default: "
            + evaluation.preference_default.label()
            + " remains the default when its capability is adequate"
        )
    return ModelVerdict(
        requested=requested,
        allowed=True,
        restricted_by_rule_ids=(),
        active_override_ids=evaluation.active_override_ids,
        allowed_by_override_ids=tuple(allowed_by),
        substitute=None,
        disclosure_required=False,
        notes=tuple(notes),
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_instant(value: str) -> datetime:
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--at must be an ISO-8601 instant, got {value!r}") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(
            f"--at must carry a UTC offset (naive instant rejected): {value!r}"
        )
    return instant


def _parse_model(value: str) -> ModelRef:
    provider, separator, model = value.partition("/")
    if not separator or not provider or not model:
        raise ValueError(f"--model must be formatted provider/model, got {value!r}")
    return ModelRef(provider=provider, model=model)


def _render(
    evaluation: GovernanceEvaluation,
    verdict: ModelVerdict | None,
    *,
    as_json: bool,
) -> str:
    if as_json:
        payload: dict[str, object] = {"evaluation": evaluation.to_json()}
        if verdict is not None:
            payload["verdict"] = verdict.to_json()
        return json.dumps(payload, indent=2, sort_keys=True)
    lines = [
        "delegated model governance @ " + evaluation.evaluated_at.isoformat()
        + f" ({evaluation.calendar} {evaluation.local_date.isoformat()})"
    ]
    in_force = ", ".join(evaluation.standing_rule_ids_in_force) or "(none)"
    if evaluation.suspended_rule_ids:
        lines.append(
            "standing rules in force: "
            + in_force
            + " (suspended: "
            + ", ".join(evaluation.suspended_rule_ids)
            + ")"
        )
    else:
        lines.append("standing rules in force: " + in_force)
    if evaluation.active_override_ids:
        lines.append(
            "active overrides: " + ", ".join(evaluation.active_override_ids)
        )
    lines.append(f"preference default: {evaluation.preference_default.label()}")
    if verdict is not None:
        lines.append(
            f"{verdict.requested.label()}: "
            + ("ALLOWED" if verdict.allowed else "RESTRICTED")
        )
        lines.extend(f"  - {note}" for note in verdict.notes)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the dated delegated-model governance policy "
            "(model-policy.json delegated_model_policy, D-060)."
        )
    )
    _ = parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY_PATH,
        help="model-policy.json path (default: the repository-root artifact)",
    )
    _ = parser.add_argument(
        "--at",
        help=(
            "ISO-8601 instant with UTC offset at which to evaluate the policy "
            "(default: now; tests and boundary checks always pin this)"
        ),
    )
    _ = parser.add_argument(
        "--model",
        help="query one model as provider/model (e.g. zai/glm-5.3)",
    )
    _ = parser.add_argument(
        "--json",
        action="store_true",
        help="emit the machine-readable evaluation instead of the summary",
    )
    args = parser.parse_args(argv)
    policy_path = cast(Path, args.policy)
    at_argument = cast("str | None", args.at)
    model_argument = cast("str | None", args.model)
    as_json = cast(bool, args.json)

    try:
        document = load_policy_document(
            policy_path.read_text(encoding="utf-8"), label=str(policy_path)
        )
        policy = validate_delegated_model_policy(
            document.get("delegated_model_policy")
        )
        instant = (
            _parse_instant(at_argument)
            if at_argument is not None
            else datetime.now().astimezone()
        )
        evaluation = evaluate_delegated_model_policy(policy, instant=instant)
        verdict = (
            verdict_for(policy, evaluation, _parse_model(model_argument))
            if model_argument is not None
            else None
        )
    except (OSError, ValueError) as exc:
        print(f"model_governance: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(_render(evaluation, verdict, as_json=as_json))
    if verdict is not None and not verdict.allowed:
        return EXIT_RESTRICTED
    return EXIT_ALLOWED


if __name__ == "__main__":
    raise SystemExit(main())
