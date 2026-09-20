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

The user selector policy resolves through ``config.py`` (D-036): an explicit
``--selector-policy`` file wins; otherwise the default user configuration
``~/.config/scarcity-router/selector-policy.json`` (XDG-aware) is
provisioned from the checked-in example when missing and used when present;
``--neutral-policy`` ignores the user default for one run. The
``install-config`` command provisions the default configuration explicitly.

Valid no-solution decisions are legitimate selector results and exit 0 for
both ``select`` and ``simulate``; non-zero exit is reserved for invalid
input, configuration or application failure. Shell exit status is never a
second selection contract.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, cast

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from .diagnostics import DiagnosticsReport

from .config import ensure_default_user_config
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

    install_parser = commands.add_parser(
        "install-config",
        help=(
            "provision the default user configuration "
            + "(~/.config/scarcity-router, XDG-aware)"
        ),
        description=(
            "Create the default selector-policy.json from the checked-in "
            + "owner example. An existing file is never overwritten unless "
            + "--force is given."
        ),
    )
    _ = install_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing selector-policy.json with the shipped defaults",
    )

    doctor_parser = commands.add_parser(
        "doctor",
        help=(
            "run redacted diagnostics (local mode, or a server's data "
            + "directory with --server-data-dir)"
        ),
        description=(
            "Shared diagnostic report (issue #94, realizing the deferred "
            + "D-016 doctor concept): checks configuration, artifacts, the "
            + "durable store, resource states and workers. Reads stored "
            + "state only — no collector call, adapter dispatch or "
            + "inference request is ever made."
        ),
    )
    _ = doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the diagnostics report as JSON",
    )
    _ = doctor_parser.add_argument(
        "--server-data-dir",
        metavar="DIR",
        default=None,
        help=(
            "diagnose a deployed server component's durable store and "
            + "configuration instead of the local recommendation setup"
        ),
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
    policy_group = parser.add_mutually_exclusive_group()
    _ = policy_group.add_argument(
        "--selector-policy",
        metavar="FILE",
        help=(
            "selector policy JSON (default: the user default config "
            + "~/.config/scarcity-router/selector-policy.json when present, "
            + "else the documented neutral policy)"
        ),
    )
    _ = policy_group.add_argument(
        "--neutral-policy",
        action="store_true",
        help="run the documented neutral policy, ignoring the user default config",
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


def _resolve_selector_policy_path(args: dict[str, object]) -> Path | None:
    """Resolve the user selector policy path for one run (D-036).

    An explicit ``--selector-policy`` file always wins. Otherwise the
    default user configuration is provisioned from the checked-in example
    when missing and used when present; ``--neutral-policy`` skips it. The
    run that first provisions reports the created file once on stderr; a
    provisioning failure degrades to the neutral policy with one concise
    warning — it never blocks a selection.
    """
    explicit = _optional_path(args.get("selector_policy"))
    if explicit is not None or args.get("neutral_policy"):
        return explicit
    try:
        config_path, wrote = ensure_default_user_config(env=os.environ)
    except OSError as exc:
        print(
            f"warning: default selector policy unavailable: {exc.strerror}",
            file=sys.stderr,
        )
        return None
    if wrote:
        print(
            f"note: provisioned default user config: {config_path}",
            file=sys.stderr,
        )
    return config_path if config_path.is_file() else None


def _run_install_config(args: dict[str, object], output: TextIO) -> int:
    force = args.get("force")
    if not isinstance(force, bool):
        raise RuntimeError("parser produced an invalid force argument")
    try:
        path, wrote = ensure_default_user_config(force=force, env=os.environ)
    except OSError as exc:
        print(
            f"error: cannot provision the default configuration: "
            + f"{exc.strerror}",
            file=sys.stderr,
        )
        return 1
    action = "wrote" if wrote else "kept existing"
    _ = output.write(f"{action}: {path}\n")
    return 0


def _run_doctor(args: dict[str, object], output: TextIO) -> int:
    """The additive ``doctor`` command (issue #94; shared diagnostics).

    Renders the same report the server UI shows: local artifact/policy
    checks by default, or a deployed server's store, configuration,
    resource ladder and pairing state with ``--server-data-dir``. Reads
    stored state only — never a collector, adapter dispatch or inference
    request — and never prints secrets.
    """
    from datetime import timezone

    from . import get_version
    from .diagnostics import (
        ServerDiagnosticsInputs,
        collect_local_diagnostics,
        collect_server_diagnostics,
        render_report_human,
    )
    from .selection_app import load_configured_artifacts
    from .server_config import ServerConfiguration
    from .server_store import ServerStore, ServerStoreError

    json_output = args.get("json")
    if not isinstance(json_output, bool):
        raise RuntimeError("parser produced an invalid JSON output argument")
    now = datetime.now(timezone.utc)
    version = get_version()
    report: DiagnosticsReport

    server_dir_value = args.get("server_data_dir")
    if isinstance(server_dir_value, str) and server_dir_value:
        from pathlib import Path as _Path

        store_error: str | None = None
        store: ServerStore | None = None
        try:
            store = ServerStore.open(_Path(server_dir_value))
            _ = store.require_current_schema()
        except (ServerStoreError, OSError) as exc:
            store_error = str(exc)
        document: dict[str, object] | None = None
        config_error: str | None = None
        if store is not None:
            try:
                document = store.load_configuration_document()
            except ServerStoreError as exc:
                config_error = str(exc)
        configuration: ServerConfiguration | None = None
        if document is not None:
            try:
                configuration = ServerConfiguration.from_document(document)
            except ValueError as exc:
                config_error = str(exc)
        if store is None or configuration is None:
            report = _doctor_server_failure_report(
                now=now,
                version=version,
                detail=store_error or config_error or "server store unreadable",
            )
        else:
            active = [
                record
                for record in store.list_client_keys()
                if record.revoked_at is None
            ]
            total = len(store.list_client_keys())
            # Worker pairing state lives in the ONE pairing system (M05's
            # identity store beside the server store); the server's live
            # connection state is in-process and therefore not visible to
            # an offline doctor — rows honestly carry no connection claim.
            worker_records: tuple[WorkerIdentityRecord, ...] = ()
            from .worker_identity_store import (
                WorkerIdentityError,
                WorkerIdentityRecord,
                WorkerIdentityStore,
                default_worker_store_path,
            )

            worker_path = _Path(default_worker_store_path(server_dir_value))
            if worker_path.is_file():
                try:
                    worker_store = WorkerIdentityStore(worker_path)
                    try:
                        worker_records = worker_store.list_identities()
                    finally:
                        worker_store.close()
                except (WorkerIdentityError, OSError, ValueError):
                    worker_records = ()
            report = collect_server_diagnostics(
                ServerDiagnosticsInputs(
                    configuration=configuration,
                    registry_snapshot=None,
                    constraints=configuration.admin_constraints,
                    paired_worker_ids=frozenset(
                        record.worker_id
                        for record in worker_records
                        if record.status == "active"
                    ),
                    channels_with_adapters=frozenset(),
                    endpoints_with_credentials=frozenset(
                        provider.provider_id
                        for provider in configuration.providers
                        if store.has_provider_secret(provider.provider_id)
                    ),
                    pairings=tuple(
                        {
                            "worker_id": record.worker_id,
                            "label": record.label,
                            "status": record.status,
                        }
                        for record in worker_records
                    ),
                    store_schema_version=store.schema_version(),
                    store_error=None,
                    admin_configured=store.admin_identity() is not None,
                    active_client_keys=len(active),
                    revoked_client_keys=total - len(active),
                    now=now,
                    version=version,
                    provider_endpoints=configuration.providers,
                )
            )
        if store is not None:
            store.close()
    else:
        artifact_error: str | None = None
        try:
            _ = load_configured_artifacts(
                DEFAULT_CATALOG_PATH, DEFAULT_MODEL_POLICY_PATH
            )
        except (ValueError, OSError, SelectionContractError) as exc:
            artifact_error = str(exc)
        policy_error: str | None = None
        policy_configured = False
        from .config import default_selector_policy_path, load_default_selector_policy

        if default_selector_policy_path(env=os.environ).is_file():
            policy_configured = True
            try:
                _ = load_default_selector_policy(env=os.environ)
            except (ValueError, OSError, SelectionContractError) as exc:
                policy_error = str(exc)
        report = collect_local_diagnostics(
            now=now,
            version=version,
            artifact_error=artifact_error,
            policy_error=policy_error,
            policy_configured=policy_configured,
        )
    if json_output:
        import json

        _ = output.write(
            json.dumps(report.to_dict(), sort_keys=True, indent=2) + "\n"
        )
    else:
        _ = output.write(render_report_human(report))
    return 0


def _doctor_server_failure_report(
    *, now: datetime, version: str, detail: str
) -> DiagnosticsReport:
    """A one-check report when the server store cannot be diagnosed."""
    from datetime import timezone

    from .diagnostics import DiagnosticCheck, DiagnosticsReport

    return DiagnosticsReport(
        generated_at=(
            now.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        ),
        mode="server",
        checks=(
            DiagnosticCheck(
                check_id="store",
                title="Durable store",
                state="error",
                detail="the server store or its configuration failed validation",
                remediation=detail,
            ),
        ),
        resources=(),
        workers=(),
        versions={"scarcity_router": version},
    )


def _run_status(
    args: dict[str, object],
    output: TextIO,
    collectors: StatusCollectors | None,
    clock: Clock | None,
) -> int:
    json_output = args.get("json")
    if not isinstance(json_output, bool):
        raise RuntimeError("parser produced an invalid JSON output argument")
    observation = collect_status(collectors=collectors, clock=clock)
    _ = output.write(
        render_json(observation.snapshots)
        if json_output
        else render_human(observation.snapshots)
    )
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
        selector_policy_path=_resolve_selector_policy_path(args),
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
        selector_policy_path=_resolve_selector_policy_path(args),
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
        if command == "install-config":
            return _run_install_config(arguments, output)
        if command == "doctor":
            return _run_doctor(arguments, output)
    except (ValueError, OSError, SelectionContractError, CapacityError) as exc:
        # Fail safely: a concise structural message only — never a raw
        # provider payload, credential or traceback dump.
        _ = output.flush()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise RuntimeError(f"parser produced an unknown command {command!r}")


__all__ = ["build_parser", "main"]
