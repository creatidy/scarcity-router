"""Offline, partial Claude Code statusline projection for issue #214.

Documented quota percentages/reset instants do not establish token units, model
scope, billing authority or freshness. This zero-I/O parser never reports healthy
capacity or enables a collector/adapter. See docs/poc-evidence.md (2026-10-24).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import cast

from ..capacity import SCHEMA_VERSION, CapacityDiagnostic, CapacitySnapshot, CapacityWindow

_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_WINDOWS = (("five_hour", "five_hour", 18_000), ("seven_day", "weekly", 604_800))


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def parse_claude_statusline(
    payload: object,
    *,
    retrieved_at: str,
    max_age_seconds: int,
    observed_at: str | None = None,
) -> CapacitySnapshot:
    """Project evidenced fields without attesting a live source.

    ``max_age_seconds`` is explicit caller policy, not a provider guarantee.
    ``observed_at`` means the original quota observation, never file-read/hook
    delivery time; official statusline JSON does not supply it. There is no
    production caller that manufactures this evidence.
    """
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be a positive integer")

    def result(
        status: str,
        windows: tuple[CapacityWindow, ...] = (),
        diagnostics: tuple[CapacityDiagnostic, ...] = (),
    ) -> CapacitySnapshot:
        return CapacitySnapshot(
            schema_version=SCHEMA_VERSION,
            provider="anthropic",
            source="claude_code_statusline",
            retrieved_at=retrieved_at,
            status=status,
            windows=windows,
            diagnostics=(CapacityDiagnostic("schema_changed" if status == "schema_changed" else "telemetry_unknown"), *diagnostics),
        )

    empty = result("unknown")  # Validate the caller's output timestamp first.
    current = _instant(retrieved_at)
    assert current is not None
    observed = _instant(observed_at)
    fresh = observed is not None and 0 <= (current - observed).total_seconds() <= max_age_seconds
    reference = observed if observed is not None and observed <= current else current
    envelope = _mapping(payload)
    if envelope is None:
        return result("schema_changed")
    raw_limits = envelope.get("rate_limits")
    if raw_limits is None:
        return empty
    limits = _mapping(raw_limits)
    if limits is None or any(key not in {"five_hour", "seven_day", "spend_limit"} for key in limits):
        return result("schema_changed")

    windows: list[CapacityWindow] = []
    diagnostics: list[CapacityDiagnostic] = []
    for key, kind, duration in _WINDOWS:
        raw_window = limits.get(key)
        if raw_window is None:
            continue
        window = _mapping(raw_window)
        if window is None or any(field not in {"used_percentage", "resets_at"} for field in window):
            return result("schema_changed")
        reset: datetime | None = None
        raw_reset = window.get("resets_at")
        if isinstance(raw_reset, int) and not isinstance(raw_reset, bool) and raw_reset > 0:
            try:
                reset = _EPOCH + timedelta(seconds=raw_reset)
            except (OverflowError, ValueError):
                pass
        if reset is not None and (reset - reference).total_seconds() > duration + 60:
            reset = None

        used: int | None = None
        percentage = window.get("used_percentage")
        if (
            fresh and reset is not None and reset > current
            and isinstance(percentage, (int, float)) and not isinstance(percentage, bool)
            and 0 <= percentage <= 100 and percentage % 1 == 0
        ):
            used = int(percentage)
        windows.append(CapacityWindow(
            resource="unknown",
            kind=kind,
            duration_seconds=duration,
            used_percent=used,
            remaining_percent=None if used is None else 100 - used,
            resets_at=None if reset is None else reset.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            window_id=key.replace("_", "-"),
        ))
        window_id = windows[-1].window_id
        diagnostics.append(CapacityDiagnostic("window_semantics_unknown", window_id))
        if used is None:
            diagnostics.append(CapacityDiagnostic("percentage_unknown", window_id))
        if reset is None:
            diagnostics.append(CapacityDiagnostic("reset_unknown", window_id))

    # Spend/cost fields do not establish quota units, subscription entitlement,
    # credits or verified no-PAYG; unsupported facts are never emitted.
    return result("unknown", tuple(windows), tuple(diagnostics))
