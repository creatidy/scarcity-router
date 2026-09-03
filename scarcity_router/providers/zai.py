"""Z.ai Coding Plan quota-response parser.

Pure, deterministic provider-edge parsing: an already decoded JSON-compatible
payload plus a caller-supplied retrieval timestamp are normalized into the v1
``CapacitySnapshot`` contract (docs/capacity-model.md).

This parser performs zero I/O. It never reads the clock, filesystem,
environment or network, and never touches credentials. Live acquisition is a
separate, unimplemented concern.

Provider semantics implemented here come only from the validated evidence in
docs/poc-evidence.md ("2026-09-01 M1 reconnaissance") and the redacted
fixtures under tests/fixtures/zai-coding-plan/:

- envelope: ``code`` (int), ``msg`` (str), ``success`` (bool), ``data`` (object);
- ``data.level`` is the plan tier; ``data.limits`` is a non-semantic array;
- ``TOKENS_LIMIT`` with ``(unit=3, number=5)`` is the five-hour token window;
  ``(unit=6, number=1)`` is the weekly token window. Any other combination, or
  a missing ``unit``/``number``, is an unknown window, never guessed;
- ``TIME_LIMIT`` is a distinct non-token limit: ``resource="time"`` with
  unknown period semantics;
- ``percentage`` is used-oriented: valid integers 0..100 normalize to a
  ``used_percent``/``remaining_percent`` pair; anything else omits the pair;
- ``nextResetTime`` is epoch milliseconds, converted to canonical UTC.

Structurally incompatible successful responses normalize to
``status="schema_changed"`` with no windows and no partial decoding. An HTTP
401 envelope normalizes to ``status="auth_required"``. Raw provider text
(``msg``), endpoints, credentials and account data never enter the output.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import cast

from ..capacity import CapacityDiagnostic, CapacitySnapshot, CapacityWindow

PROVIDER = "zai"
SOURCE = "zai_usage_endpoint"

# Validated Z.ai token-window mapping (adapter-owned evidence; U-004).
_KNOWN_TOKEN_WINDOWS: dict[tuple[int, int], tuple[str, int]] = {
    (3, 5): ("five_hour", 18_000),
    (6, 1): ("weekly", 604_800),
}

# Provider type strings validated by evidence; everything else is unknown.
_TOKENS_LIMIT = "TOKENS_LIMIT"
_TIME_LIMIT = "TIME_LIMIT"

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary value to a ``str``-keyed mapping, or ``None``."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _is_int(value: object) -> bool:
    """True for a real JSON integer; booleans are not integers."""
    return isinstance(value, int) and not isinstance(value, bool)


def _envelope_valid(envelope: Mapping[str, object]) -> bool:
    """Deliberate validation of the evidenced response envelope shape."""
    if not _is_int(envelope.get("code")):
        return False
    if not isinstance(envelope.get("success"), bool):
        return False
    if not isinstance(envelope.get("msg"), str):
        return False
    return "data" in envelope


def _safe_plan(level: object) -> str | None:
    """Accept a provider plan level only when it is already a safe v1 ID.

    Arbitrary or unsafe values are omitted, never sanitized into a
    misleading identifier and never copied into diagnostics.
    """
    if isinstance(level, str) and _SAFE_ID_RE.match(level):
        return level
    return None


def _window_identity_id(type_token: str, unit: object, number: object) -> str:
    """Deterministic safe window ID from validated identity fields only.

    Missing or non-integer identity parts become a fixed placeholder that
    cannot collide with an integer rendering. No array index, raw JSON or
    provider free-text participates in the ID.
    """
    def part(value: object) -> str:
        return str(value) if _is_int(value) else "x"

    return f"{type_token}-{part(unit)}-{part(number)}".lower()


def _canonical_from_epoch_ms(value: object) -> str | None:
    """Convert an epoch-millisecond integer to the canonical v1 UTC string.

    Integer arithmetic only (no float precision loss); UTC only; returns
    ``None`` for non-integers and unrepresentable instants.
    """
    if not _is_int(value):
        return None
    seconds, millis = divmod(cast(int, value), 1000)
    try:
        moment = _EPOCH + timedelta(seconds=seconds, milliseconds=millis)
    except (OverflowError, ValueError):
        return None
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        + f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        + f".{moment.microsecond // 1000:03d}Z"
    )


def _used_pair(percentage: object) -> tuple[int, int] | None:
    """Validate the used-oriented provider percentage; ``None`` if unusable."""
    if not _is_int(percentage):
        return None
    used = cast(int, percentage)
    if not 0 <= used <= 100:
        return None
    return used, 100 - used


def _failure(
    status: str,
    diagnostic_code: str,
    retrieved_at: str,
) -> CapacitySnapshot:
    return CapacitySnapshot(
        schema_version=1,
        provider=PROVIDER,
        source=SOURCE,
        retrieved_at=retrieved_at,
        status=status,
        windows=(),
        diagnostics=(CapacityDiagnostic(code=diagnostic_code),),
        plan=None,
    )


def _parse_limit(item: object) -> tuple[CapacityWindow, list[CapacityDiagnostic]]:
    """Normalize one ``data.limits`` entry into a window plus diagnostics."""
    entry = _as_mapping(item)
    if entry is None:
        # A non-object limit is preserved as a fully unknown window; no raw
        # provider content is injected into an ID or diagnostic.
        return (
            CapacityWindow(resource="unknown", kind="unknown"),
            [CapacityDiagnostic(code="window_semantics_unknown")],
        )

    limit_type = entry.get("type")
    if limit_type == _TOKENS_LIMIT:
        resource = "tokens"
        type_token = "tokens_limit"
    elif limit_type == _TIME_LIMIT:
        resource = "time"
        type_token = "time_limit"
    else:
        return (
            CapacityWindow(resource="unknown", kind="unknown"),
            [CapacityDiagnostic(code="window_semantics_unknown")],
        )

    unit = entry.get("unit")
    number = entry.get("number")
    window_id = _window_identity_id(type_token, unit, number)

    diagnostics: list[CapacityDiagnostic] = []
    duration_seconds: int | None = None
    kind = "unknown"
    if resource == "tokens" and _is_int(unit) and _is_int(number):
        known = _KNOWN_TOKEN_WINDOWS.get(
            (cast(int, unit), cast(int, number))
        )
        if known is not None:
            kind, duration_seconds = known
    if kind == "unknown":
        diagnostics.append(
            CapacityDiagnostic(
                code="window_semantics_unknown", window_id=window_id
            )
        )

    used = _used_pair(entry.get("percentage"))
    used_percent: int | None = None
    remaining_percent: int | None = None
    if used is None:
        diagnostics.append(
            CapacityDiagnostic(code="percentage_unknown", window_id=window_id)
        )
    else:
        used_percent, remaining_percent = used

    resets_at = _canonical_from_epoch_ms(entry.get("nextResetTime"))
    if resets_at is None:
        diagnostics.append(
            CapacityDiagnostic(code="reset_unknown", window_id=window_id)
        )

    window = CapacityWindow(
        resource=resource,
        kind=kind,
        duration_seconds=duration_seconds,
        used_percent=used_percent,
        remaining_percent=remaining_percent,
        resets_at=resets_at,
        window_id=window_id,
    )
    return window, diagnostics


def parse_zai_quota_response(
    payload: object,
    *,
    retrieved_at: str,
) -> CapacitySnapshot:
    """Normalize one decoded Z.ai quota response into a v1 snapshot.

    ``payload`` must already be decoded (e.g. by the caller's HTTP layer);
    this function performs no I/O and does not call the clock. ``retrieved_at``
    must be the canonical v1 UTC string and is validated by the snapshot
    constructor.

    Failures degrade safely to a documented status with an empty windows
    array; expected provider-shape problems never surface as raw parser
    exceptions.
    """
    envelope = _as_mapping(payload)
    if envelope is None or not _envelope_valid(envelope):
        return _failure("schema_changed", "schema_changed", retrieved_at)

    code = cast(int, envelope["code"])
    if code == 401:
        return _failure("auth_required", "auth_required", retrieved_at)
    if code != 200 or envelope["success"] is not True:
        return _failure("unknown", "telemetry_unknown", retrieved_at)

    data = _as_mapping(envelope["data"])
    limits = data.get("limits") if data is not None else None
    if not isinstance(limits, list):
        return _failure("schema_changed", "schema_changed", retrieved_at)

    plan = _safe_plan(data.get("level")) if data is not None else None

    windows: list[CapacityWindow] = []
    diagnostics: list[CapacityDiagnostic] = []
    for item in cast("list[object]", limits):
        window, window_diagnostics = _parse_limit(item)
        windows.append(window)
        diagnostics.extend(window_diagnostics)

    return CapacitySnapshot(
        schema_version=1,
        provider=PROVIDER,
        source=SOURCE,
        retrieved_at=retrieved_at,
        status="ok",
        windows=tuple(windows),
        diagnostics=tuple(diagnostics),
        plan=plan,
    )
