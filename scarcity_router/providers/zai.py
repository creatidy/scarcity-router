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
- the evidenced successful schema requires ``data.level`` and
  ``data.limits``; a missing ``level`` key or a non-list ``limits`` is
  structural drift and fails closed as ``schema_changed``;
- every ``limits[]`` entry must be an object carrying a string ``type``;
  anything else is structural drift, not an unknown window;
- ``TOKENS_LIMIT`` with ``(unit=3, number=5)`` is the five-hour token window;
  ``(unit=6, number=1)`` is the weekly token window. Any other combination, or
  a missing ``unit``/``number``, is an unknown window, never guessed;
- ``TIME_LIMIT`` is a distinct non-token limit: ``resource="time"`` with
  unknown period semantics;
- a limits object with an unevidenced string ``type`` is preserved as an
  ``unknown`` window without guessing: no raw type text, no derived
  ``window_id``, and its percentage/reset facts are omitted with explicit
  diagnostics because their semantics are unvalidated for that type;
- ``plan`` comes only from the adapter evidence allowlist of observed
  ``level`` values (currently ``{"pro"}``); any other value omits ``plan``
  even when it matches the safe-ID grammar;
- ``percentage`` is used-oriented for the evidenced schema: valid integers
  0..100 normalize to a ``used_percent``/``remaining_percent`` pair; anything
  else omits the pair;
- ``nextResetTime`` is evidenced as a 13-digit epoch-millisecond integer;
  values outside that representation (epoch seconds, zero, negative, other
  digit counts) are rejected rather than misinterpreted, and convert to the
  canonical UTC string only within that validated band.

Structurally incompatible successful responses normalize to
``status="schema_changed"`` with no windows and no partial decoding. An HTTP
401 envelope normalizes to ``status="auth_required"``. Raw provider text
(``msg``), endpoints, credentials and account data never enter the output.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import TypeGuard, cast

from ..capacity import CapacityDiagnostic, CapacitySnapshot, CapacityWindow

PROVIDER = "zai"
SOURCE = "zai_usage_endpoint"

# Validated Z.ai token-window mapping (adapter-owned evidence; U-004).
_KNOWN_TOKEN_WINDOWS: dict[tuple[int, int], tuple[str, int]] = {
    (3, 5): ("five_hour", 18_000),
    (6, 1): ("weekly", 604_800),
}

# Provider type strings validated by evidence; every other string is an
# unknown, preserved window type.
_TOKENS_LIMIT = "TOKENS_LIMIT"
_TIME_LIMIT = "TIME_LIMIT"

# Adapter evidence allowlist of observed plan labels (docs/poc-evidence.md).
# A future evidenced tier extends this mapping deliberately; arbitrary
# provider text never passes on syntax alone.
_EVIDENCED_PLANS: frozenset[str] = frozenset({"pro"})

# Evidenced ``nextResetTime`` representation: 13-digit epoch milliseconds.
_RESET_MS_MIN = 1_000_000_000_000
_RESET_MS_MAX = 9_999_999_999_999

# Window-ID identity components render as decimal digits only when small,
# non-negative integers; everything else degrades to a fixed placeholder.
# This bounds the identifier and keeps generation total: the comparison never
# converts a huge integer to a string, so arbitrary magnitudes cannot raise.
_IDENTITY_PART_MAX = 99_999

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary value to a ``str``-keyed mapping, or ``None``."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _is_int(value: object) -> TypeGuard[int]:
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
    """Accept only evidence-allowlisted plan labels; omit everything else.

    Syntactic safe-ID conformance is not sufficient: arbitrary provider text
    must not become a normalized plan label, enter diagnostics or leak.
    """
    if isinstance(level, str) and level in _EVIDENCED_PLANS:
        return level
    return None


def _identity_part(value: object) -> str:
    """Render one validated identity component for a window ID.

    Small non-negative integers render as decimal digits; missing, boolean,
    negative or oversized components degrade to a placeholder that cannot
    collide with an integer rendering. The magnitude check happens before any
    string conversion, so unbounded provider integers are safe.
    """
    if _is_int(value) and 0 <= value <= _IDENTITY_PART_MAX:
        return str(value)
    return "x"


def _window_identity_id(type_token: str, unit: object, number: object) -> str:
    """Deterministic safe window ID from validated identity fields only.

    The fixed ``type_token`` plus two bounded components always satisfies the
    v1 safe-ID grammar and 64-character limit. No array index, raw JSON or
    provider free-text participates in the ID.
    """
    return f"{type_token}-{_identity_part(unit)}-{_identity_part(number)}"


def _canonical_from_epoch_ms(value: object) -> str | None:
    """Convert a 13-digit epoch-millisecond integer to canonical v1 UTC.

    Only the evidenced representation is accepted: an integer (not bool),
    positive, within the 13-digit millisecond band. Values that look like
    epoch seconds or any other unit are rejected instead of misread. Integer
    arithmetic only (no float precision loss); UTC only.
    """
    if not _is_int(value):
        return None
    if not _RESET_MS_MIN <= value <= _RESET_MS_MAX:
        return None
    seconds, remainder = divmod(value, 1000)
    try:
        moment = _EPOCH + timedelta(seconds=seconds, milliseconds=remainder)
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
    if not 0 <= percentage <= 100:
        return None
    return percentage, 100 - percentage


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


def _parse_limit(
    entry: Mapping[str, object],
) -> tuple[CapacityWindow, list[CapacityDiagnostic]]:
    """Normalize one structurally validated ``data.limits`` object.

    The caller guarantees the entry is a mapping with a string ``type``.
    """
    limit_type = entry.get("type")
    if limit_type == _TOKENS_LIMIT:
        resource = "tokens"
        type_token = "tokens_limit"
    elif limit_type == _TIME_LIMIT:
        resource = "time"
        type_token = "time_limit"
    else:
        # A structurally valid window with an unevidenced type is preserved
        # without guessing semantics. Its percentage/reset semantics are not
        # validated for this type, so both facts are omitted with explicit
        # diagnostics, and no window_id is derived from arbitrary text.
        return (
            CapacityWindow(resource="unknown", kind="unknown"),
            [
                CapacityDiagnostic(code="window_semantics_unknown"),
                CapacityDiagnostic(code="percentage_unknown"),
                CapacityDiagnostic(code="reset_unknown"),
            ],
        )

    unit = entry.get("unit")
    number = entry.get("number")
    window_id = _window_identity_id(type_token, unit, number)

    diagnostics: list[CapacityDiagnostic] = []
    duration_seconds: int | None = None
    kind = "unknown"
    if resource == "tokens" and _is_int(unit) and _is_int(number):
        known = _KNOWN_TOKEN_WINDOWS.get((unit, number))
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
    if data is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    # The evidenced successful schema requires both data.level and
    # data.limits; their disappearance is structural drift, fail closed.
    if "level" not in data:
        return _failure("schema_changed", "schema_changed", retrieved_at)
    limits = data.get("limits")
    if not isinstance(limits, list):
        return _failure("schema_changed", "schema_changed", retrieved_at)

    # Structural validation of every limits entry: a non-object entry, or an
    # object without a usable string type, is schema incompatibility rather
    # than an unknown window. Healthy siblings are not partially preserved.
    entries: list[Mapping[str, object]] = []
    for item in cast("list[object]", limits):
        entry = _as_mapping(item)
        if entry is None or not isinstance(entry.get("type"), str):
            return _failure("schema_changed", "schema_changed", retrieved_at)
        entries.append(entry)

    plan = _safe_plan(data.get("level"))

    windows: list[CapacityWindow] = []
    diagnostics: list[CapacityDiagnostic] = []
    for entry in entries:
        window, window_diagnostics = _parse_limit(entry)
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
