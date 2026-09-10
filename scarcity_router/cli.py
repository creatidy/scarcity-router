"""Top-level provisional CLI dispatcher for Scarcity Router.

Composes the read-only ``status`` surface (M1) with the M2e ``select`` and
``simulate`` commands. Argparse handlers only parse arguments and delegate:
selection logic stays in the pure core (``selector.py``, ``simulation.py``)
and the application layer (``selection_app.py``). The existing ``status``
module entry point (``status.main``) is preserved unchanged for direct
callers; the dispatcher re-uses the same collect/render primitives so the
status output contract is identical.

Default artifact paths resolve through ``selection_app.resolve_default_artifact``:
the repository-root ``model-catalog.json`` and ``model-policy.json`` in a
source tree, the packaged resource copies in an installed package; explicit
``--catalog`` / ``--model-policy`` overrides always win.

Valid no-solution decisions are legitimate selector results and exit 0 for
both ``select`` and ``simulate``; non-zero exit is reserved for invalid
input, configuration or application failure. Shell exit status is never a
second selection contract.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO, cast

from .errors import CapacityError, SelectionContractError
from .selection_app import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_MODEL_POLICY_PATH,
    render_decision_json,
    render_select_human,
    render_simulation_human,
    render_simulation_json,
    run_select,
    run_simulate,
)
from .status import (
    Clock,
    StatusCollectors,
    collect_status,
    render_human,
    render_json,
)


def _default_prog() -> str:
    """Show the installed script name for script runs, the module form else."""
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return invoked if invoked == "scarcity-router" else "python -m scarcity_router"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``status`` / ``select`` / ``simulate`` parser."""
    parser = argparse.ArgumentParser(
        prog=_default_prog(),
        description=(
            "Read-only normalized AI provider capacity status and "
            "deterministic least-scarce model selection."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status_parser = commands.add_parser(
        "status",
        help="collect current OpenAI and Z.ai status",
        description="Collect read-only normalized status from all supported providers.",
    )
    _ = status_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the ordered normalized snapshot list as JSON",
    )

    select_parser = commands.add_parser(
        "select",
        help="recommend the least scarce model that is capable enough now",
        description=(
            "Deterministically recommend the least scarce capable model for a "
            "resolved task requirement under the active resource policy."
        ),
    )
    _fill_requirement_arguments(select_parser)
    _ = select_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete deterministic SelectionDecision as JSON",
    )
    _ = select_parser.add_argument(
        "--explain",
        action="store_true",
        help="append the readable decision explanation (does not change --json)",
    )

    simulate_parser = commands.add_parser(
        "simulate",
        help="run baseline and simulated selections through the same selector",
        description=(
            "Apply typed overrides to copies of the current inputs and show "
            "the CURRENT and SIMULATED decisions side by side."
        ),
    )
    _fill_requirement_arguments(simulate_parser)
    _ = simulate_parser.add_argument(
        "--overrides",
        metavar="FILE",
        required=True,
        help=(
            "simulation overrides JSON (capacity percentages, policy, "
            "replenishment, evaluated_at)"
        ),
    )
    _ = simulate_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete deterministic simulation result as JSON",
    )
    _ = simulate_parser.add_argument(
        "--explain",
        action="store_true",
        help="append the readable explanation for both decisions",
    )
    return parser


def _fill_requirement_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    _ = group.add_argument(
        "--profile",
        metavar="PROFILE",
        help="calibrated task profile id from model-policy.json",
    )
    _ = group.add_argument(
        "--requirement",
        metavar="FILE",
        help="explicit full TaskRequirement JSON file",
    )
    _ = parser.add_argument(
        "--tighten",
        metavar="FILE",
        help=(
            "full TaskRequirement JSON that monotonically tightens the "
            + "profile (only valid with --profile)"
        ),
    )
    _ = parser.add_argument(
        "--selector-policy",
        metavar="FILE",
        help="selector policy JSON (defaults to the documented neutral policy)",
    )
    _ = parser.add_argument(
        "--replenishment",
        metavar="FILE",
        help="normalized replenishment observations JSON list",
    )
    _ = parser.add_argument(
        "--catalog",
        metavar="FILE",
        default=str(DEFAULT_CATALOG_PATH),
        help="model catalog JSON (default: the source-tree or packaged default catalog)",
    )
    _ = parser.add_argument(
        "--model-policy",
        metavar="FILE",
        default=str(DEFAULT_MODEL_POLICY_PATH),
        help="model policy JSON (default: the source-tree or packaged default policy)",
    )


def _optional_path(value: object) -> Path | None:
    text = value if isinstance(value, str) else None
    return Path(text) if text is not None else None


def _run_status(
    args: dict[str, object],
    output: TextIO,
    collectors: StatusCollectors | None,
    clock: Clock | None,
) -> int:
    json_output = args.get("json")
    if not isinstance(json_output, bool):
        raise RuntimeError("parser produced an invalid JSON output argument")
    snapshots = collect_status(collectors=collectors, clock=clock)
    _ = output.write(render_json(snapshots) if json_output else render_human(snapshots))
    return 0


def _resolve_requirement_paths(
    args: dict[str, object],
) -> tuple[str | None, Path | None, Path | None]:
    profile_id = args.get("profile")
    requirement = _optional_path(args.get("requirement"))
    tighten = _optional_path(args.get("tighten"))
    if tighten is not None and requirement is not None:
        raise ValueError(
            "tightening is only permitted with --profile, never with --requirement"
        )
    checked_profile = profile_id if isinstance(profile_id, str) else None
    return checked_profile, requirement, tighten


def _run_select(
    args: dict[str, object],
    output: TextIO,
    collectors: StatusCollectors | None,
    clock: Clock | None,
) -> int:
    profile_id, requirement_path, tighten_path = _resolve_requirement_paths(args)
    json_output = args.get("json")
    explain = args.get("explain")
    if not isinstance(json_output, bool) or not isinstance(explain, bool):
        raise RuntimeError("parser produced an invalid output argument")
    decision = run_select(
        profile_id=profile_id,
        requirement_path=requirement_path,
        tighten_path=tighten_path,
        selector_policy_path=_optional_path(args.get("selector_policy")),
        replenishment_path=_optional_path(args.get("replenishment")),
        catalog_path=Path(cast(str, args.get("catalog"))),
        model_policy_path=Path(cast(str, args.get("model_policy"))),
        collectors=collectors,
        clock=clock,
    )
    if json_output:
        _ = output.write(render_decision_json(decision))
    else:
        _ = output.write(render_select_human(decision, explain=explain))
    return 0


def _run_simulate(
    args: dict[str, object],
    output: TextIO,
    collectors: StatusCollectors | None,
    clock: Clock | None,
) -> int:
    profile_id, requirement_path, tighten_path = _resolve_requirement_paths(args)
    overrides_path = _optional_path(args.get("overrides"))
    if overrides_path is None:
        raise RuntimeError("parser produced an invalid overrides argument")
    json_output = args.get("json")
    explain = args.get("explain")
    if not isinstance(json_output, bool) or not isinstance(explain, bool):
        raise RuntimeError("parser produced an invalid output argument")
    result = run_simulate(
        overrides_path=overrides_path,
        profile_id=profile_id,
        requirement_path=requirement_path,
        tighten_path=tighten_path,
        selector_policy_path=_optional_path(args.get("selector_policy")),
        replenishment_path=_optional_path(args.get("replenishment")),
        catalog_path=Path(cast(str, args.get("catalog"))),
        model_policy_path=Path(cast(str, args.get("model_policy"))),
        collectors=collectors,
        clock=clock,
    )
    if json_output:
        _ = output.write(render_simulation_json(result))
    else:
        _ = output.write(render_simulation_human(result, explain=explain))
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    collectors: StatusCollectors | None = None,
    clock: Clock | None = None,
) -> int:
    """Run the provisional module CLI and return its process exit code."""
    parser = build_parser()
    arguments = cast(dict[str, object], vars(parser.parse_args(argv)))
    output = sys.stdout if stdout is None else stdout
    try:
        command = arguments.get("command")
        if command == "status":
            return _run_status(arguments, output, collectors, clock)
        if command == "select":
            return _run_select(arguments, output, collectors, clock)
        if command == "simulate":
            return _run_simulate(arguments, output, collectors, clock)
    except (ValueError, OSError, SelectionContractError, CapacityError) as exc:
        # Fail safely: a concise structural message only — never a raw
        # provider payload, credential or traceback dump.
        _ = output.flush()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise RuntimeError(f"parser produced an unknown command {command!r}")


__all__ = ["build_parser", "main"]
