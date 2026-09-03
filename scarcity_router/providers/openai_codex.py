"""OpenAI Codex app-server rate-limit normalization.

Pure, deterministic provider-edge parsing for the Codex app-server
``account/rateLimits/read`` JSON-RPC result: an already decoded
JSON-compatible value plus a caller-supplied retrieval timestamp normalize
into the v1 ``CapacitySnapshot`` contract (docs/capacity-model.md).

This parser performs zero I/O. It never reads the clock, filesystem,
environment, network or subprocesses, and never touches credentials. Live
acquisition (binary discovery, process supervision, JSONL transport) is the
separate ``openai_codex_acquisition`` module.

Provider semantics implemented here come only from the validated evidence in
docs/poc-evidence.md ("OpenAI/Codex subscription capacity" and the 2026-09-03
collector reconnaissance) and the redacted fixtures under
``tests/fixtures/openai-codex-appserver/``:

- the successful result is an object containing a ``rateLimits`` object; a
  missing or non-object ``rateLimits`` is structural drift and fails closed
  as ``schema_changed``;
- window objects are the object values inside ``rateLimits`` that carry a
  validated positive integer ``windowDurationMins``. The PoC observed them
  under the ``primary``/``secondary`` keys, but those slot names carry no
  period semantics: classification uses only the validated duration
  (300 minutes -> five-hour, 10080 minutes -> weekly). Any other validated
  duration is an unknown period with its duration preserved;
- an object value inside ``rateLimits`` that lacks a validated
  ``windowDurationMins`` is not a recognizable window under this contract:
  that is structural drift, so the whole response fails closed as
  ``schema_changed`` rather than being partially decoded. Scalar values
  (``limitId``, ``planType``, ``rateLimitReachedType``) are not windows and
  are tolerated;
- ``usedPercent`` is used-oriented for the evidenced schema (the PoC reading
  and the Codex extension's own ``remaining = 100 - used`` derivation agree):
  valid integers 0..100 normalize to a ``used_percent``/``remaining_percent``
  pair; anything else omits the pair;
- ``resetsAt`` is evidenced as a 10-digit epoch-**second** integer; values
  outside that representation (epoch milliseconds, zero, negative, floats,
  strings) are rejected rather than misinterpreted;
- ``plan`` comes only from the adapter evidence allowlist of observed
  ``planType`` values (currently ``{"plus"}``); any other value omits
  ``plan`` even when it matches the safe-ID grammar;
- ``rateLimitReachedType`` has no v1 representation; exhaustion is expressed
  by the validated percentage pair (``(100, 0)``) and the reached flag is
  otherwise dropped, never guessed into a status;
- a ``rateLimits`` object containing no window objects at all is an
  incompatible shape (the evidenced success always carries windows): it
  fails closed as ``schema_changed`` instead of reporting healthy emptiness.

This module also provides ``classify_app_server_message``, the deliberate
structural classifier for decoded JSONL app-server messages (response /
notification / request / invalid) used by the acquisition layer to match the
relevant response by request identity instead of timing. Observed framing
(2026-09-03 reconnaissance): requests carry ``jsonrpc``/``id``/``method``;
responses carry ``id`` with exactly one of ``result``/``error`` and omit the
``jsonrpc`` echo; notifications carry ``method`` without ``id``.

Raw provider text, subprocess output, local paths, credentials and account
data never enter any output this module produces.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Literal, TypeGuard, cast

from ..capacity import CapacityDiagnostic, CapacitySnapshot, CapacityWindow

PROVIDER = "openai"
SOURCE = "codex_app_server"

MessageKind = Literal["request", "notification", "response", "invalid"]

# Validated windowDurationMins -> (kind, fixed duration_seconds) mapping
# (adapter-owned evidence; PoC 2026-09-01 and capacity-model v1 durations).
_KNOWN_DURATIONS_MINS: dict[int, tuple[str, int]] = {
    300: ("five_hour", 18_000),
    10_080: ("weekly", 604_800),
}

# Adapter evidence allowlist of observed plan labels (docs/poc-evidence.md).
# A future evidenced tier extends this set deliberately; arbitrary provider
# text never passes on syntax alone.
_EVIDENCED_PLANS: frozenset[str] = frozenset({"plus"})

# Evidenced ``resetsAt`` representation: 10-digit epoch seconds.
_RESET_S_MIN = 1_000_000_000
_RESET_S_MAX = 9_999_999_999

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# v1 safe identifier grammar (docs/capacity-model.md); slot keys become
# window IDs only when they already satisfy it, unchanged.
_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary value to a ``str``-keyed mapping, or ``None``."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _is_int(value: object) -> TypeGuard[int]:
    """True for a real JSON integer; booleans are not integers."""
    return isinstance(value, int) and not isinstance(value, bool)


def classify_app_server_message(message: object) -> MessageKind:
    """Classify one decoded app-server JSONL message by its structure.

    Deliberate, evidence-based classification (never timing or ordering):

    - ``"notification"``: object with a string ``method`` and no integer
      ``id`` (observed notifications also carry ``params``/``emittedAtMs``,
      which are not part of the classification);
    - ``"request"``: object with a string ``method`` and an integer ``id``
      (a server-initiated request; the collector never answers these);
    - ``"response"``: object without ``method`` that carries an integer
      ``id`` and exactly one of ``result``/``error``;
    - ``"invalid"``: anything else, including non-objects, string ids and
      messages carrying both ``result`` and ``error``.
    """
    envelope = _as_mapping(message)
    if envelope is None:
        return "invalid"
    has_method = isinstance(envelope.get("method"), str)
    has_int_id = _is_int(envelope.get("id"))
    if has_method:
        return "request" if has_int_id else "notification"
    if has_int_id:
        has_result = "result" in envelope
        has_error = "error" in envelope
        if has_result != has_error:
            return "response"
    return "invalid"


def _canonical_from_epoch_s(value: object) -> str | None:
    """Convert a 10-digit epoch-second integer to canonical v1 UTC.

    Only the evidenced representation is accepted: an integer (not bool)
    within the 10-digit epoch-second band. Values that look like epoch
    milliseconds or any other unit are rejected instead of misread. Integer
    arithmetic only (no float precision loss); UTC only.
    """
    if not _is_int(value):
        return None
    if not _RESET_S_MIN <= value <= _RESET_S_MAX:
        return None
    try:
        moment = _EPOCH + timedelta(seconds=value)
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


def _safe_plan(plan_type: object) -> str | None:
    """Accept only evidence-allowlisted plan labels; omit everything else.

    Syntactic safe-ID conformance is not sufficient: arbitrary provider text
    must not become a normalized plan label, enter diagnostics or leak.
    """
    if isinstance(plan_type, str) and plan_type in _EVIDENCED_PLANS:
        return plan_type
    return None


def _slot_window_id(slot: object) -> str | None:
    """A window ID only when the slot key already satisfies the safe-ID
    grammar, unchanged. No raw text is transformed, derived or invented."""
    if isinstance(slot, str) and _SAFE_ID_RE.match(slot):
        return slot
    return None


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


def _parse_window(
    slot: object,
    entry: Mapping[str, object],
) -> tuple[CapacityWindow, list[CapacityDiagnostic]] | None:
    """Normalize one window object from ``rateLimits``.

    Returns ``None`` for structural drift (an object value without a
    validated positive integer ``windowDurationMins``): the caller fails the
    whole response closed instead of partially decoding it.
    """
    mins = entry.get("windowDurationMins")
    if not _is_int(mins) or mins <= 0:
        return None

    kind, duration_seconds = _KNOWN_DURATIONS_MINS.get(
        mins, ("unknown", mins * 60)
    )
    window_id = _slot_window_id(slot)

    diagnostics: list[CapacityDiagnostic] = []
    if kind == "unknown":
        diagnostics.append(
            CapacityDiagnostic(
                code="window_semantics_unknown", window_id=window_id
            )
        )

    used = _used_pair(entry.get("usedPercent"))
    used_percent: int | None = None
    remaining_percent: int | None = None
    if used is None:
        diagnostics.append(
            CapacityDiagnostic(code="percentage_unknown", window_id=window_id)
        )
    else:
        used_percent, remaining_percent = used

    resets_at = _canonical_from_epoch_s(entry.get("resetsAt"))
    if resets_at is None:
        diagnostics.append(
            CapacityDiagnostic(code="reset_unknown", window_id=window_id)
        )

    window = CapacityWindow(
        resource="tokens",
        kind=kind,
        duration_seconds=duration_seconds,
        used_percent=used_percent,
        remaining_percent=remaining_percent,
        resets_at=resets_at,
        window_id=window_id,
    )
    return window, diagnostics


def parse_codex_rate_limits_result(
    result: object,
    *,
    retrieved_at: str,
) -> CapacitySnapshot:
    """Normalize one decoded ``account/rateLimits/read`` result into v1.

    ``result`` must already be decoded from the JSONL response by the
    acquisition layer; this function performs no I/O and does not call the
    clock. ``retrieved_at`` must be the canonical v1 UTC string and is
    validated by the snapshot constructor.

    Expected provider-shape failures degrade safely to documented statuses
    with an empty windows array; they never surface as raw parser exceptions.
    """
    envelope = _as_mapping(result)
    if envelope is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    rate_limits = _as_mapping(envelope.get("rateLimits"))
    if rate_limits is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    windows: list[CapacityWindow] = []
    diagnostics: list[CapacityDiagnostic] = []
    for slot, value in rate_limits.items():
        entry = _as_mapping(value)
        if entry is None:
            # Scalar values (limitId, planType, rateLimitReachedType and
            # additive fields) are not windows and are tolerated.
            continue
        parsed = _parse_window(slot, entry)
        if parsed is None:
            # An object that is not a recognizable window is incompatible
            # with the validated mapping; fail closed without partial
            # decoding of healthy siblings.
            return _failure("schema_changed", "schema_changed", retrieved_at)
        window, window_diagnostics = parsed
        windows.append(window)
        diagnostics.extend(window_diagnostics)

    if not windows:
        # The evidenced success always carries window objects; a windowless
        # rateLimits object is an incompatible shape, not healthy emptiness.
        return _failure("schema_changed", "schema_changed", retrieved_at)

    return CapacitySnapshot(
        schema_version=1,
        provider=PROVIDER,
        source=SOURCE,
        retrieved_at=retrieved_at,
        status="ok",
        windows=tuple(windows),
        diagnostics=tuple(diagnostics),
        plan=_safe_plan(rate_limits.get("planType")),
    )
