"""Unified read-only status collection and presentation.

This module composes the OpenAI and Z.ai provider collectors without
interpreting provider payloads. Each invocation creates one observation
timestamp, calls the collectors in a fixed order, and exposes either a compact
human view or the existing v3 snapshot dictionaries plus — additively (D-039)
— the OpenAI execution-eligibility report produced by the same observation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TextIO, cast

from .capacity import CapacityDiagnostic, CapacitySnapshot, CapacityWindow
from .eligibility import ExecutionEligibility
from .providers.openai_codex_acquisition import (
    OpenAICodexObservation,
    collect_openai_codex_capacity,
)
from .providers.zai_acquisition import collect_zai_capacity

_PROVIDER_ORDER = {"openai": 0, "zai": 1}
_WINDOW_KIND_ORDER = {"five_hour": 0, "weekly": 1, "unknown": 2}
_WINDOW_RESOURCE_ORDER = {"tokens": 0, "time": 1, "unknown": 2}

# D-039: explicit standalone Codex binary for controlled production
# configurations. When set, the default OpenAI collector skips VS Code
# extension discovery and uses exactly this executable. The value is a path,
# never a credential; it is read per invocation and never logged.
CODEX_BINARY_PATH_ENV = "SCARCITY_ROUTER_CODEX_BIN"
CODEX_SOURCE_ENV = "SCARCITY_ROUTER_CODEX_SOURCE"


class OpenAIObservationCollector(Protocol):
    def __call__(self, *, retrieved_at: str) -> OpenAICodexObservation: ...


class ZaiCapacityCollector(Protocol):
    def __call__(self, *, retrieved_at: str) -> CapacitySnapshot: ...


Clock = Callable[[], datetime]


def _default_openai_collector(*, retrieved_at: str) -> OpenAICodexObservation:
    explicit_binary = os.environ.get(CODEX_BINARY_PATH_ENV, "").strip()
    source = os.environ.get(CODEX_SOURCE_ENV, "").strip()
    codex_home: Path | None = None
    if source != "@local":
        from .gateway_validation import v_safe_id
        from .codex_home import ControlledCodexHome
        from .model_inventory import SOURCE_ID_MAX_LENGTH
        from .providers.openai_codex_acquisition import discover_codex_binary
        from .worker_service import (
            ServiceUnitNotFoundError,
            ServiceUnitReadError,
            read_installed_service_configuration,
        )

        try:
            installed = read_installed_service_configuration()
        except ServiceUnitNotFoundError:
            if source:
                raise ValueError(
                    f"{CODEX_SOURCE_ENV} requires an installed worker service; "
                    + "install it or use @local for ordinary Codex telemetry"
                ) from None
        except (ServiceUnitReadError, ValueError):
            raise ValueError(
                "cannot resolve the installed worker's Codex configuration; "
                + "repair the worker service configuration or set "
                + f"{CODEX_SOURCE_ENV}=@local for ordinary Codex telemetry"
            ) from None
        else:
            sources = installed.selection.codex_sources
            if source and source not in sources:
                raise ValueError(
                    f"{CODEX_SOURCE_ENV} must name a configured --codex-source "
                    + "or be @local for ordinary Codex telemetry"
                )
            if not source and len(sources) > 1:
                raise ValueError(
                    "the worker service has multiple Codex sources; set "
                    + f"{CODEX_SOURCE_ENV} to the desired --codex-source "
                    + "(or @local for ordinary Codex telemetry)"
                )
            if sources:
                selected = source or sources[0]
                # Never interpolate an unvalidated unit value into a home path.
                if len(selected) > SOURCE_ID_MAX_LENGTH:
                    raise ValueError("configured Codex source id is too long")
                try:
                    selected = v_safe_id(selected, "codex_source")
                except ValueError:
                    raise ValueError("configured Codex source id is invalid") from None
                home = ControlledCodexHome(
                    installed.state_dir, name=f"codex-sources/{selected}"
                )
                if home.path.resolve() != home.path or home.validate() is not None:
                    raise ValueError(
                        "the configured Codex source home is missing or unsafe; "
                        + "run make codex-login to initialize/sign in to the source; "
                        + "if it remains unsafe, repair its isolation configuration"
                    )
                codex_home = home.path
                if not explicit_binary and installed.selection.codex_bin:
                    binary, _ = discover_codex_binary(
                        pinned_binary=Path(installed.selection.codex_bin)
                    )
                    if binary is None:
                        raise ValueError(
                            "the worker service's --codex-bin must be an existing "
                            + "non-symlink regular executable; repair the service "
                            + "binary pin or explicitly set SCARCITY_ROUTER_CODEX_BIN"
                        )
                    explicit_binary = str(binary.path)
    return collect_openai_codex_capacity(
        retrieved_at=retrieved_at,
        binary_path=Path(explicit_binary) if explicit_binary else None,
        codex_home=codex_home,
    )


@dataclass(frozen=True)
class StatusCollectors:
    """Collector dependencies, with a seam for synthetic application tests."""

    openai: OpenAIObservationCollector = _default_openai_collector
    zai: ZaiCapacityCollector = collect_zai_capacity


@dataclass(frozen=True)
class StatusObservation:
    """One unified observation: provider snapshots plus paired eligibility.

    ``snapshots`` keeps the fixed provider order (openai, zai);
    ``eligibility`` carries one report per provider that produces one (only
    OpenAI today, D-039). Both collections share the single observation
    timestamp.
    """

    snapshots: tuple[CapacitySnapshot, ...]
    eligibility: tuple[ExecutionEligibility, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def observation_timestamp(clock: Clock | None = None) -> str:
    """Return one canonical UTC millisecond timestamp for an invocation."""
    current = (clock or _utc_now)()
    if current.tzinfo is None:
        raise ValueError("observation clock must return a timezone-aware datetime")
    return (
        current.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def collect_status(
    *,
    collectors: StatusCollectors | None = None,
    clock: Clock | None = None,
) -> StatusObservation:
    """Collect OpenAI and Z.ai observations for one shared instant."""
    retrieved_at = observation_timestamp(clock)
    selected = StatusCollectors() if collectors is None else collectors
    openai_observation = selected.openai(retrieved_at=retrieved_at)
    zai_snapshot = selected.zai(retrieved_at=retrieved_at)
    return StatusObservation(
        snapshots=(openai_observation.snapshot, zai_snapshot),
        eligibility=(openai_observation.eligibility,),
    )


def _ordered_snapshots(
    snapshots: Sequence[CapacitySnapshot],
) -> list[CapacitySnapshot]:
    return sorted(
        snapshots,
        key=lambda snapshot: (
            _PROVIDER_ORDER.get(snapshot.provider, len(_PROVIDER_ORDER)),
            snapshot.provider,
        ),
    )


def _window_sort_key(window: CapacityWindow) -> tuple[object, ...]:
    return (
        _WINDOW_KIND_ORDER.get(window.kind, len(_WINDOW_KIND_ORDER)),
        _WINDOW_RESOURCE_ORDER.get(window.resource, len(_WINDOW_RESOURCE_ORDER)),
        window.window_id or "",
        window.duration_seconds if window.duration_seconds is not None else -1,
        window.resets_at or "",
        window.used_percent if window.used_percent is not None else -1,
        window.remaining_percent if window.remaining_percent is not None else -1,
    )


def _format_percentage(value: int | None) -> str:
    return "unknown" if value is None else f"{value}%"


def _format_window(window: CapacityWindow) -> str:
    fields = [
        f"kind={window.kind}",
        f"resource={window.resource}",
        f"used={_format_percentage(window.used_percent)}",
        f"remaining={_format_percentage(window.remaining_percent)}",
        f"reset={window.resets_at or 'unknown'}",
    ]
    if window.scope_id is not None:
        fields.append(f"scope={window.scope_id}")
    if window.window_id is not None:
        fields.append(f"id={window.window_id}")
    return "  window " + " ".join(fields)


def _diagnostic_sort_key(diagnostic: CapacityDiagnostic) -> tuple[str, str]:
    return diagnostic.code, diagnostic.window_id or ""


def _format_diagnostics(snapshot: CapacitySnapshot) -> str | None:
    if not snapshot.diagnostics:
        return None
    diagnostics = sorted(snapshot.diagnostics, key=_diagnostic_sort_key)
    values = [
        diagnostic.code
        + (f"[{diagnostic.window_id}]" if diagnostic.window_id is not None else "")
        for diagnostic in diagnostics
    ]
    return "  diagnostics=" + ",".join(values)


def _canonical_snapshot_dict(snapshot: CapacitySnapshot) -> dict[str, object]:
    """Serialize one snapshot with canonical ordering for unordered arrays."""
    payload = snapshot.to_dict()
    payload["windows"] = [
        window.to_dict() for window in sorted(snapshot.windows, key=_window_sort_key)
    ]
    payload["diagnostics"] = [
        diagnostic.to_dict()
        for diagnostic in sorted(snapshot.diagnostics, key=_diagnostic_sort_key)
    ]
    return payload


def canonical_snapshot_documents(
    snapshots: Sequence[CapacitySnapshot],
) -> list[dict[str, object]]:
    """Ordered canonical snapshot dictionaries — the shared JSON contract.

    The CLI ``status --json`` output and the REST ``/v1/status`` envelope
    (D-028) both use this exact serialization: canonical provider ordering
    and canonically sorted windows and diagnostics. Since M3b (D-030) no
    interface maintains its own snapshot serialization.
    """
    return [
        _canonical_snapshot_dict(snapshot)
        for snapshot in _ordered_snapshots(snapshots)
    ]


def canonical_eligibility_documents(
    eligibility: Sequence[ExecutionEligibility],
) -> list[dict[str, object]]:
    """Ordered canonical eligibility documents (D-039).

    Canonical provider ordering, shared by the REST ``/v1/status`` envelope
    and the MCP status tool. The CLI ``status --json`` surface keeps its
    released snapshot-array shape and does not include these documents.
    """
    return [
        report.to_dict()
        for report in sorted(eligibility, key=lambda report: report.provider)
    ]


def render_human(snapshots: Sequence[CapacitySnapshot]) -> str:
    """Render snapshots using only safe normalized contract fields."""
    ordered = _ordered_snapshots(snapshots)
    if not ordered:
        raise ValueError("status requires at least one provider snapshot")
    lines = [f"Observed at {ordered[0].retrieved_at}"]
    for snapshot in ordered:
        header = [f"Provider {snapshot.provider}", f"status={snapshot.status}"]
        if snapshot.plan is not None:
            header.append(f"plan={snapshot.plan}")
        lines.append(" ".join(header))
        for window in sorted(snapshot.windows, key=_window_sort_key):
            lines.append(_format_window(window))
        if not snapshot.windows:
            lines.append("  windows=none")
        diagnostic_line = _format_diagnostics(snapshot)
        if diagnostic_line is not None:
            lines.append(diagnostic_line)
    return "\n".join(lines) + "\n"


def render_json(snapshots: Sequence[CapacitySnapshot]) -> str:
    """Render the ordered snapshots with their existing v3 serialization."""
    payload = canonical_snapshot_documents(snapshots)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scarcity_router",
        description="Read-only normalized AI provider capacity status.",
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
    return parser


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
    observation = collect_status(collectors=collectors, clock=clock)
    json_output = arguments.get("json")
    if not isinstance(json_output, bool):
        raise RuntimeError("parser produced an invalid JSON output argument")
    _ = output.write(
        render_json(observation.snapshots)
        if json_output
        else render_human(observation.snapshots)
    )
    return 0


__all__ = [
    "CODEX_BINARY_PATH_ENV",
    "CODEX_SOURCE_ENV",
    "StatusCollectors",
    "StatusObservation",
    "build_parser",
    "canonical_eligibility_documents",
    "canonical_snapshot_documents",
    "collect_status",
    "main",
    "observation_timestamp",
    "render_human",
    "render_json",
]
