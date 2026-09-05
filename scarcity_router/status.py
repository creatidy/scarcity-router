"""Unified read-only status collection and presentation.

This module composes the OpenAI and Z.ai provider collectors without
interpreting provider payloads. Each invocation creates one observation
timestamp, calls the collectors in a fixed order, and exposes either a compact
human view or the existing v2 snapshot dictionaries.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, TextIO, cast

from .capacity import CapacityDiagnostic, CapacitySnapshot, CapacityWindow
from .providers.openai_codex_acquisition import collect_openai_codex_capacity
from .providers.zai_acquisition import collect_zai_capacity

_PROVIDER_ORDER = {"openai": 0, "zai": 1}
_WINDOW_KIND_ORDER = {"five_hour": 0, "weekly": 1, "unknown": 2}
_WINDOW_RESOURCE_ORDER = {"tokens": 0, "time": 1, "unknown": 2}


class OpenAICapacityCollector(Protocol):
    def __call__(self, *, retrieved_at: str) -> CapacitySnapshot: ...


class ZaiCapacityCollector(Protocol):
    def __call__(self, *, retrieved_at: str) -> CapacitySnapshot: ...


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class StatusCollectors:
    """Collector dependencies, with a seam for synthetic application tests."""

    openai: OpenAICapacityCollector = collect_openai_codex_capacity
    zai: ZaiCapacityCollector = collect_zai_capacity


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
) -> tuple[CapacitySnapshot, CapacitySnapshot]:
    """Collect OpenAI and Z.ai snapshots for one observation."""
    retrieved_at = observation_timestamp(clock)
    selected = StatusCollectors() if collectors is None else collectors
    return (
        selected.openai(retrieved_at=retrieved_at),
        selected.zai(retrieved_at=retrieved_at),
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
    """Render the ordered snapshots with their existing v2 serialization."""
    payload = [
        _canonical_snapshot_dict(snapshot)
        for snapshot in _ordered_snapshots(snapshots)
    ]
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
    snapshots = collect_status(collectors=collectors, clock=clock)
    json_output = arguments.get("json")
    if not isinstance(json_output, bool):
        raise RuntimeError("parser produced an invalid JSON output argument")
    _ = output.write(render_json(snapshots) if json_output else render_human(snapshots))
    return 0


__all__ = [
    "StatusCollectors",
    "build_parser",
    "collect_status",
    "main",
    "observation_timestamp",
    "render_human",
    "render_json",
]
