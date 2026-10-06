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
import shutil
import sys
import textwrap
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


def render_terminal(
    observation: StatusObservation, *, collected_at: str, width: int = 80,
) -> str:
    """A static ASCII view of normalized facts, not a selector or readiness policy."""
    now = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    reports = {
        snapshot.provider: report
        for snapshot in observation.snapshots
        for report in observation.eligibility
        if (report.provider, report.source, report.retrieved_at)
        == (snapshot.provider, snapshot.source, snapshot.retrieved_at)
    }
    ordered = _ordered_snapshots(observation.snapshots)
    if not ordered:
        raise ValueError("status requires at least one provider snapshot")

    def duration(seconds: float) -> str:
        total = max(0, int(seconds))
        parts: list[str] = []
        for unit, size in (("d", 86_400), ("h", 3_600), ("m", 60), ("s", 1)):
            value, total = divmod(total, size)
            if value:
                parts.append(f"{value}{unit}")
            if len(parts) == 2:
                break
        return " ".join(parts) or "0s"

    labels = {"five_hour": "5-hour", "weekly": "Weekly", "unknown": "Unknown window"}
    recovery = {
        "unavailable": "Verify the documented telemetry source and configured binary; for a worker source use scarcity-router-worker service status. Retry status only after restoring that source. Offline scarcity-router doctor checks artifacts/configuration only, not provider/source access.",
        "auth_required": "Configure Z.ai access as documented. Offline scarcity-router doctor checks artifacts/configuration only, not credentials or source access. Do not probe with inference.",
        "unsupported": "This collector/source is unsupported. Check documented source and binary versions. Offline scarcity-router doctor checks artifacts/configuration only, not source availability. Do not infer available quota.",
        "schema_changed": "Collector schema changed: check supported collector versions. Offline scarcity-router doctor checks artifacts/configuration only, not provider schema or access. Missing fields remain unknown.",
        "unknown": "Telemetry is uncertain. Verify documented source selection and configured binary; worker sources can use scarcity-router-worker service status. Retry telemetry-only status; missing evidence stays unknown. Offline scarcity-router doctor checks artifacts/configuration only, not provider/source access. Do not probe with inference.",
    }
    lines = ["Capacity snapshot", f"Collection started: {collected_at}", "", "Overview"]
    for snapshot in ordered:
        known = [window for window in snapshot.windows if (
            window.remaining_percent is not None and window.kind != "unknown"
            and window.resource != "unknown"
        )]
        if known:
            lowest = min(known, key=lambda window: (cast(int, window.remaining_percent), _window_sort_key(window)))
            quota = f"lowest reported {lowest.remaining_percent}% ({labels[lowest.kind]} {lowest.resource})"
        else:
            quota = "quota unknown (no comparable reported windows)"
        uncertain = len(snapshot.windows) - len(known)
        if uncertain:
            quota += f"; {uncertain} unknown/unclassified window(s)"
        report = reports.get(snapshot.provider)
        policy = report.state if report is not None else "not reported (unknown)"
        lines.append(f"{snapshot.provider}: collection {snapshot.status}; {quota}; policy {policy}")
    lines.extend(("", "Details"))
    for snapshot in ordered:
        lines.append(f"Provider {snapshot.provider} | collection: {snapshot.status}")
        lines.append(f"  Source: {snapshot.source}; plan: {snapshot.plan or 'unknown'}")
        age = (now - datetime.fromisoformat(snapshot.retrieved_at.replace("Z", "+00:00"))).total_seconds()
        age_text = duration(age) + " since fetch" if age >= 0 else "ahead of collection clock; timing unknown"
        lines.append(f"  Fetched: {snapshot.retrieved_at} ({age_text})")
        for window in sorted(snapshot.windows, key=_window_sort_key):
            identity = (window.kind, window.resource, window.scope_id)
            ambiguous = sum((other.kind, other.resource, other.scope_id) == identity for other in snapshot.windows) > 1
            label = f"{labels[window.kind]} {window.resource}"
            if ambiguous and window.window_id is not None:
                label += f" [{window.window_id}]"
            lines.append(f"  {label}: remaining {_format_percentage(window.remaining_percent)}, used {_format_percentage(window.used_percent)}")
            if window.resets_at is None:
                reset = "unknown (not immediate)"
            else:
                delta = (datetime.fromisoformat(window.resets_at.replace("Z", "+00:00")) - now).total_seconds()
                relative = "in " + duration(delta) if delta > 0 else "reported time passed; refresh to confirm"
                reset = window.resets_at + " (" + relative + ")"
            lines.append(f"    Reset: {reset}; scope: {window.scope_id or 'unknown applicability'}")
            if (window.remaining_percent == 0 and window.kind != "unknown"
                    and window.resource != "unknown"):
                lines.append("    This reported window is exhausted. Wait for its reset; do not redeem benefits or test with inference.")
        if not snapshot.windows:
            lines.append("  No reported windows: quota unknown, not exhausted or full.")
        report = reports.get(snapshot.provider)
        if report is None:
            lines.append("  Included-allowance policy: not reported; no execution eligibility inferred.")
        elif report.state == "eligible":
            lines.append("  Included-allowance policy: eligible for this observation; not task/model fit or execution readiness.")
        else:
            lines.append(f"  Included-allowance policy: {report.state}; reasons: {', '.join(report.reason_codes)}")
            lines.append("  Keep included-only execution blocked until these conditions are resolved; do not use purchased credits.")
        diagnostic_line = _format_diagnostics(snapshot)
        if diagnostic_line is not None:
            lines.append(diagnostic_line)
        if snapshot.status in recovery:
            guidance = recovery[snapshot.status]
            if snapshot.status == "auth_required" and snapshot.provider == "openai":
                guidance = (
                    "For ordinary Codex telemetry use codex login. For a single installed worker source, "
                    "scarcity-router-worker codex-login discovers its service settings. With multiple sources use "
                    "scarcity-router-worker codex-login --source SOURCE_ID --state-dir STATE_DIR --codex-bin CODEX_BIN. "
                    "Select the same source as SCARCITY_ROUTER_CODEX_SOURCE and matching installed-service state directory "
                    "and binary pin; explicit --source does not discover those settings. Omit --codex-bin only if the "
                    "service has no binary pin. Use existing service configuration, not another default home. "
                    "Placeholders are not private paths or source values. Do not probe with inference."
                )
            lines.append("  Recovery: " + guidance)
        lines.append("")
    lines.extend((
        "Windows: never sum scopes or assume a shared/account pool from this view.",
        "Fetch age is not a freshness verdict or evidence of worker health.",
        "Collection/quota/policy facts are not execution readiness or task fit.",
        "Recommendation-only does not execute; gateway authorization/health are separate.",
        "Pipes keep original text; status --json keeps the canonical snapshot array.",
    ))
    bounded_width = max(12, min(width, 120))
    wrapped: list[str] = []
    for line in lines:
        indent = len(line) - len(line.lstrip())
        wrapped.append(textwrap.fill(
            line, width=bounded_width, subsequent_indent=" " * indent,
            break_long_words=True, break_on_hyphens=False,
        ) if line else "")
    return "\n".join(wrapped) + "\n"


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
    collected_at = observation_timestamp(clock)
    observation = collect_status(
        collectors=collectors,
        clock=lambda: datetime.fromisoformat(collected_at.replace("Z", "+00:00")),
    )
    json_output = arguments.get("json")
    if not isinstance(json_output, bool):
        raise RuntimeError("parser produced an invalid JSON output argument")
    if json_output:
        rendered = render_json(observation.snapshots)
    elif output.isatty():
        rendered = render_terminal(
            observation, collected_at=collected_at, width=shutil.get_terminal_size().columns,
        )
    else:
        rendered = render_human(observation.snapshots)
    _ = output.write(rendered)
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
    "render_terminal",
]
