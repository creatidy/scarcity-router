"""OpenAI Codex app-server rate-limit normalization.

Pure, deterministic provider-edge parsing for the Codex app-server
``account/rateLimits/read`` JSON-RPC result: an already decoded
JSON-compatible value plus a caller-supplied retrieval timestamp normalize
into the v1 ``CapacitySnapshot`` contract (docs/capacity-model.md).

This parser performs zero I/O. It never reads the clock, filesystem,
environment, network or subprocesses, and never touches credentials. Live
acquisition (binary discovery, process supervision, JSONL transport) is the
separate ``openai_codex_acquisition`` module.

Provider semantics implemented here come only from validated evidence in
docs/poc-evidence.md ("OpenAI/Codex subscription capacity" and the
2026-09-03 collector reconnaissance) and the redacted fixtures under
``tests/fixtures/openai-codex-appserver/``:

- the successful result is the protocol's ``GetAccountRateLimitsResponse``
  envelope. Per the exact tagged generated JSON schema, only ``rateLimits``
  is required; ``rateLimitsByLimitId`` and ``rateLimitResetCredits`` are
  nullable optional members (missing and explicit null are both valid
  absent states). Additive envelope members are tolerated when scalar and
  fail closed when structured (they may carry uninterpretable constraining
  data);
- ``rateLimits`` is the protocol's ``RateLimitSnapshot`` with the evidenced
  nine members: ``limitId``, ``limitName``, ``primary``, ``secondary``,
  ``credits``, ``individualLimit``, ``spendControlReached``, ``planType``
  and ``rateLimitReachedType`` (option-typed: missing and null both mean an
  absent state). Present values validate strictly:
  - ``limitId`` must be exactly the evidenced quota identity ``"codex"``;
  - ``primary``/``secondary`` are the only window slots. A valid window
    requires i32 ``usedPercent``. ``windowDurationMins`` is an optional i64:
    positive values classify the period (300 minutes -> five-hour, 10080 ->
    weekly, other validated values -> unknown with duration preserved), while
    null or absence gives an unknown-duration window. ``resetsAt`` is an
    optional i64. Slot names carry no period semantics;
  - ``credits`` is the evidenced ``CreditsSnapshot``: ``hasCredits`` and
    ``unlimited`` are required booleans, while optional ``balance`` is a
    decimal string or null. A present valid snapshot is a
    v1-unrepresentable credit state (unknown + withheld pairs), never
    interpreted;
  - ``individualLimit`` is the evidenced ``SpendControlLimitSnapshot``:
    ``limit`` and ``used`` are required strings, while
    ``remainingPercent`` and ``resetsAt`` are required integers. A present
    valid snapshot is a
    v1-unrepresentable spend state, and ``remainingPercent == 0`` is a
    backend blocker. Amounts are never interpreted or compared;
  - ``spendControlReached`` is the boolean spend-control blocker: absent,
    null or ``false`` means clear, ``true`` blocks, any other shape is
    drift;
  - ``rateLimitReachedType`` accepts exactly the evidenced snake_case enum
    members (``rate_limit_reached``, ``workspace_member_credits_depleted``,
    ``workspace_owner_credits_depleted``,
    ``workspace_owner_usage_limit_reached``,
    ``workspace_member_usage_limit_reached``); camelCase or arbitrary
    strings are drift, not degraded facts;
- ``rateLimitsByLimitId`` carries additional metered buckets keyed by limit
   id. If the map is present, it must contain the exact tagged success mirror
   under the ``"codex"`` key: that entry is accepted, fully validated, and
   checked for consistency with the top-level ``rateLimits`` (a missing or
   diverging mirror is drift). Every other bucket validates as a full quota
   snapshot with the same membership, identity (the map key must equal the
   bucket's ``limitId``), window and nested credit/spend-control rules, and its
   validated windows are emitted with a safe distinct identity
   (``"<limitId>:<slot>"``) without merging equal periods across buckets.
   Buckets are ordered deterministically: main slots first, then buckets by
   key, each primary then secondary;
- ``rateLimitResetCredits`` is the reset-credit summary: a valid present
   summary requires the integer ``availableCount``; its optional ``credits``
   rows are typed objects requiring ``id``, ``resetType``, ``status`` and
   ``grantedAt``; nullable ``expiresAt``, ``title`` and ``description`` are
   optional. Empty arrays are valid, but empty or malformed rows are drift. A
   present valid summary is v1-unrepresentable (unknown + withheld pairs);
- backend blockers and v1-unrepresentable states never yield a healthy
  snapshot: ``status="unknown"`` with ``telemetry_unknown``, all validated
  windows (main and additional) preserved with identity/duration/reset
  facts, and every percentage pair withheld (``percentage_unknown`` per
  window). A present additional bucket without blockers also degrades to
  ``unknown`` (v1 cannot represent capacity metered across buckets) but
  keeps validated pairs. Known exhaustion of the main quota *without* any
  blocker stays ``ok`` with the ``(100, 0)`` pair;
- main quota coverage is validated: a main snapshot lacking either known
  window kind (five-hour or weekly) never reports ``ok``; it degrades to
  ``unknown`` with the validated partial windows preserved. Two slot
  windows sharing one known period duplicate the evidenced
  primary/secondary semantics and fail closed as ``schema_changed``;
 - ``usedPercent`` is a required i32 and is used-oriented for the evidenced
   schema (the PoC reading and the Codex extension's own
   ``remaining = 100 - used`` derivation agree): valid values 0..100
   normalize to a ``used_percent``/``remaining_percent`` pair; other values
   within i32 omit the pair, while missing, null, non-integers or out-of-width
   values are schema drift;
- ``resetsAt`` is evidenced as a 10-digit epoch-**second** integer; values
  outside that representation (epoch milliseconds, zero, negative, floats,
  strings) are rejected rather than misinterpreted;
- ``plan`` comes only from the adapter evidence allowlist of validated
  ``PlanType`` enum members that the v1 safe-ID grammar permits as-is
  (underscores included): ``free``, ``go``, ``plus``, ``pro``, ``prolite``,
  ``team``, ``business``, ``edu``, ``edu_plus``, ``edu_pro``,
  ``enterprise``, ``ent26``, ``enterprise_cbp_automation``,
  ``enterprise_cbp_usage_based``, ``self_serve_business_prolite``,
   ``self_serve_business_usage_based`` and ``unknown``. Values are preserved
   verbatim, never rewritten; any other present value is schema drift and is
   never leaked.

This module also provides ``classify_app_server_message``, the deliberate
structural classifier for decoded JSONL app-server messages (response /
notification / request / invalid) used by the acquisition layer to match the
relevant response by request identity instead of timing. Observed framing
(2026-09-03 reconnaissance): requests carry ``id``/``method`` and optional
``params``; responses carry ``id`` with exactly one of ``result``/``error`` and
omit ``jsonrpc``; notifications carry ``method`` without ``id``. A hybrid
message carrying a ``method`` key — whatever its value type — together with
``result``/``error`` is invalid drift, and so is a message whose ID is neither
a string nor a bounded integer: neither is silently ignored.

Raw provider text, subprocess output, local paths, credentials and account
data never enter any output this module produces.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
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

# The two evidenced window slots of the protocol's RateLimitSnapshot. Slot
# names identify position in the provider snapshot only; period semantics
# always come from the validated windowDurationMins.
_WINDOW_SLOTS: tuple[str, str] = ("primary", "secondary")

# The complete evidenced member set of RateLimitSnapshot (option-typed
# members: missing and null both mean an absent state).
_KNOWN_SNAPSHOT_MEMBERS: frozenset[str] = frozenset(
    {
        "limitId",
        "limitName",
        "planType",
        "rateLimitReachedType",
        "spendControlReached",
        "credits",
        "individualLimit",
        *_WINDOW_SLOTS,
    }
)

# Evidenced members of the GetAccountRateLimitsResponse envelope. Only
# `rateLimits` is required by the exact tagged generated schema; the other
# two are nullable optional members.
_KNOWN_ENVELOPE_MEMBERS: frozenset[str] = frozenset(
    {"rateLimits", "rateLimitsByLimitId", "rateLimitResetCredits"}
)

# The evidenced quota identity for this read result, and the bucket key
# under which the exact tagged success response mirrors the main snapshot.
_LIMIT_ID = "codex"

# Adapter evidence allowlist of validated PlanType enum members (see
# docs/poc-evidence.md, 2026-09-03 reconnaissance). The v1 safe-ID grammar
# permits underscores, so every retained member is preserved verbatim.
# Extending or shrinking this set requires new schema evidence.
_EVIDENCED_PLANS: frozenset[str] = frozenset(
    {
        "free",
        "go",
        "plus",
        "pro",
        "prolite",
        "team",
        "business",
        "edu",
        "edu_plus",
        "edu_pro",
        "enterprise",
        "ent26",
        "enterprise_cbp_automation",
        "enterprise_cbp_usage_based",
        "self_serve_business_prolite",
        "self_serve_business_usage_based",
        "unknown",
    }
)

# The exact evidenced RateLimitReachedType enum members. Snake_case only:
# camelCase or arbitrary strings are drift, not degraded facts.
REACHED_TYPES: frozenset[str] = frozenset(
    {
        "rate_limit_reached",
        "workspace_member_credits_depleted",
        "workspace_owner_credits_depleted",
        "workspace_owner_usage_limit_reached",
        "workspace_member_usage_limit_reached",
    }
)

# The evidenced reset-credit row status enum.
_RESET_CREDIT_STATUSES: frozenset[str] = frozenset(
    {"available", "redeeming", "redeemed", "unknown"}
)

_RESET_CREDIT_TYPES: frozenset[str] = frozenset({"codexRateLimits", "unknown"})

# Evidenced ``resetsAt`` representation: 10-digit epoch seconds.
_RESET_S_MIN = 1_000_000_000
_RESET_S_MAX = 9_999_999_999
_I32_MIN = -(2**31)
_I32_MAX = 2**31 - 1
_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# v1 safe identifier grammar (docs/capacity-model.md); used to compose
# additional-bucket window identities without inventing unsafe text.
_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary value to a ``str``-keyed mapping, or ``None``."""
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in mapping):
            return None
        return cast(Mapping[str, object], value)
    return None


def _strict_equal(left: object, right: object) -> bool:
    """Compare decoded JSON without Python bool/int or int/float coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left_mapping = cast(Mapping[object, object], left)
        right_mapping = cast(Mapping[object, object], right)
        if set(left_mapping) != set(right_mapping):
            return False
        return all(
            _strict_equal(left_mapping[key], right_mapping[key])
            for key in left_mapping
        )
    if isinstance(left, list) and isinstance(right, list):
        left_list = cast(list[object], left)
        right_list = cast(list[object], right)
        return len(left_list) == len(right_list) and all(
            _strict_equal(left_item, right_item)
            for left_item, right_item in zip(left_list, right_list)
        )
    return left == right


def _is_int(value: object) -> TypeGuard[int]:
    """True for a real JSON integer; booleans are not integers."""
    return isinstance(value, int) and not isinstance(value, bool)


def _fits_i32(value: object) -> bool:
    return _is_int(value) and _I32_MIN <= value <= _I32_MAX


def is_signed_i64(value: object) -> bool:
    """Validate the tagged protocol's signed i64 integer representation."""
    return _is_int(value) and _I64_MIN <= value <= _I64_MAX


def _fits_i64(value: object) -> bool:
    return is_signed_i64(value)


def _is_request_id(value: object) -> bool:
    return isinstance(value, str) or _fits_i64(value)


def _membership_valid(
    container: Mapping[str, object],
    known: frozenset[str],
) -> bool:
    """Additive scalars tolerated; additive structured members are drift."""
    for _key, value in container.items():
        if _key in known:
            continue
        if isinstance(value, (Mapping, list)):
            return False
    return True


@dataclass(frozen=True)
class _WindowFacts:
    window: CapacityWindow
    diagnostics: tuple[CapacityDiagnostic, ...] = field(default=())
    kind: str = ""


@dataclass(frozen=True)
class _SnapshotState:
    """Validated state of one quota snapshot (main or additional bucket)."""

    windows: tuple[_WindowFacts, ...]
    blocked: bool
    unrepresentable: bool


def classify_app_server_message(message: object) -> MessageKind:
    """Classify one decoded app-server JSONL message by its structure.

    Deliberate, evidence-based classification (never timing or ordering):

    - ``"notification"``: object with a string ``method`` and no ``id`` key;
    - ``"request"``: object with a string ``method`` and an integer ``id``
      and no response fields (a server-initiated request; the collector
      never answers these);
    - ``"response"``: object without a ``method`` key that carries an
      integer ``id`` and exactly one of ``result``/``error``;
    - ``"invalid"``: anything else. This includes non-objects, string or
      boolean ids, messages carrying both ``result`` and ``error``, any
      hybrid carrying a ``method`` key — string, null, number or boolean —
      together with ``result``/``error`` (neither a well-formed request nor
      a well-formed response), and notifications whose ``id`` key is
      present but malformed (not an integer). Invalid messages are
      protocol drift, never silently ignored.
    """
    envelope = _as_mapping(message)
    if envelope is None:
        return "invalid"
    if "method" in envelope:
        if "result" in envelope or "error" in envelope:
            return "invalid"
        if not isinstance(envelope["method"], str):
            return "invalid"
        if "id" in envelope:
            return "request" if _is_request_id(envelope["id"]) else "invalid"
        return "notification"
    message_id = envelope.get("id")
    if _is_int(message_id) and not _fits_i64(message_id):
        return "invalid"
    if _is_request_id(message_id):
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


def _bucket_window_id(limit_id: object, slot: str) -> str | None:
    """Compose a safe additional-window identity from validated parts only.

    ``"<limitId>:<slot>"`` when both halves satisfy the v1 safe-ID grammar.
    An unsafe or overlong composition returns ``None`` so the caller can fail
    closed rather than emit a colliding or invented identity.
    """
    if isinstance(limit_id, str) and _SAFE_ID_RE.match(limit_id):
        composed = f"{limit_id}:{slot}"
        if _SAFE_ID_RE.match(composed):
            return composed
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
    slot: str,
    entry: Mapping[str, object],
    *,
    window_id: str | None,
) -> _WindowFacts | None:
    """Normalize one window object from a known window slot.

    Returns the window facts, or ``None`` for structural drift. A validated
    positive integer ``windowDurationMins`` classifies the period; an
    explicit null (or absent) duration is the evidenced unknown-duration
    window with no duration asserted. The exact schema requires
    ``usedPercent`` as an i32. ``window_id`` is the already
    decided identity (main slot name, composed bucket identity, or ``None``
    for additional buckets, the identity must be safe and distinct).
    """
    if "usedPercent" not in entry:
        return None
    if not _membership_valid(
        entry, frozenset({"usedPercent", "windowDurationMins", "resetsAt"})
    ):
        return None

    used_raw = entry["usedPercent"]
    if not _fits_i32(used_raw):
        return None
    duration_raw = entry.get("windowDurationMins")
    if duration_raw is not None and not _fits_i64(duration_raw):
        return None
    reset_raw = entry.get("resetsAt")
    if _is_int(reset_raw) and not _fits_i64(reset_raw):
        return None

    mins = entry.get("windowDurationMins")
    if mins is None:
        kind = "unknown"
        duration_seconds: int | None = None
    elif _is_int(mins) and mins > 0:
        kind, duration_seconds = _KNOWN_DURATIONS_MINS.get(
            mins, ("unknown", mins * 60)
        )
    else:
        return None

    diagnostics: list[CapacityDiagnostic] = []
    if kind == "unknown":
        diagnostics.append(
            CapacityDiagnostic(code="window_semantics_unknown", window_id=window_id)
        )

    used = _used_pair(used_raw)
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
    _ = slot  # slot names carry no normalized semantics
    return _WindowFacts(
        window=window, diagnostics=tuple(diagnostics), kind=kind
    )


def _without_percentages(
    window: CapacityWindow,
) -> tuple[CapacityWindow, CapacityDiagnostic]:
    """Drop the percentage pair from a validated window (backend blocker).

    Used when the backend enforces a block on use or a metering state has
    no v1 representation: remaining capacity must not be inferred from
    percentages then, so the pair is withheld and reported as
    ``percentage_unknown``. Window identity, duration and reset facts
    remain validated and preserved.
    """
    stripped = CapacityWindow(
        resource=window.resource,
        kind=window.kind,
        duration_seconds=window.duration_seconds,
        used_percent=None,
        remaining_percent=None,
        resets_at=window.resets_at,
        window_id=window.window_id,
    )
    diagnostic_window_id = window.window_id
    return stripped, CapacityDiagnostic(
        code="percentage_unknown", window_id=diagnostic_window_id
    )


def _credits_state(value: object) -> bool | None:
    """Validate the typed ``CreditsSnapshot``; ``None`` means drift.

    Returns ``True`` when a valid credit state is present
    (v1-unrepresentable), ``False`` when absent (null or missing). The
    exact tagged schema requires the booleans ``hasCredits`` and
    ``unlimited``; an optional ``balance`` must be a string or null. The
    balance is never interpreted or surfaced.
    """
    if value is None:
        return False
    snapshot = _as_mapping(value)
    if snapshot is None:
        return None
    if not _membership_valid(
        snapshot, frozenset({"hasCredits", "unlimited", "balance"})
    ):
        return None
    if (
        "hasCredits" not in snapshot
        or "unlimited" not in snapshot
    ):
        return None
    if not isinstance(snapshot["hasCredits"], bool):
        return None
    if not isinstance(snapshot["unlimited"], bool):
        return None
    balance = snapshot.get("balance")
    if balance is not None and not isinstance(balance, str):
        return None
    return True


def _individual_limit_state(value: object) -> tuple[bool, bool] | None:
    """Validate the typed ``SpendControlLimitSnapshot``.

    Returns ``(present, exhausted)``; ``None`` means drift. The exact
    tagged schema requires all four members: ``limit`` and ``used`` as
    strings, ``remainingPercent`` as an integer and ``resetsAt`` as an
    integer. Amount strings are never interpreted or compared;
    ``remainingPercent == 0`` is the evidenced exhausted state.
    """
    if value is None:
        return False, False
    snapshot = _as_mapping(value)
    if snapshot is None:
        return None
    if not _membership_valid(
        snapshot, frozenset({"limit", "used", "remainingPercent", "resetsAt"})
    ):
        return None
    for required in ("limit", "used", "remainingPercent", "resetsAt"):
        if required not in snapshot:
            return None
    if not isinstance(snapshot["limit"], str):
        return None
    if not isinstance(snapshot["used"], str):
        return None
    remaining = snapshot["remainingPercent"]
    if not _fits_i32(remaining):
        return None
    resets = snapshot["resetsAt"]
    if not _fits_i64(resets):
        return None
    return True, remaining == 0


def _reset_credits_state(value: object) -> bool | None:
    """Validate the typed reset-credit summary; ``None`` means drift.

    Returns ``True`` when a valid summary is present
    (v1-unrepresentable), ``False`` when absent (null or missing). The
    exact tagged schema requires the integer ``availableCount``; each
    optional ``credits`` row is a typed object requiring ``id``, ``resetType``,
    ``status`` and ``grantedAt``. ``expiresAt``, ``title`` and ``description``
    are optional and nullable. Empty arrays are valid, but empty or malformed
    rows are drift.
    Values are never interpreted or surfaced.
    """
    if value is None:
        return False
    summary = _as_mapping(value)
    if summary is None:
        return None
    if not _membership_valid(summary, frozenset({"availableCount", "credits"})):
        return None
    if "availableCount" not in summary or not _fits_i64(summary["availableCount"]):
        return None
    rows = summary.get("credits")
    if rows is None:
        return True
    if not isinstance(rows, list):
        return None
    for row in cast(list[object], rows):
        row_map = _as_mapping(row)
        if row_map is None:
            return None
        required = ("id", "resetType", "status", "grantedAt")
        if any(field not in row_map for field in required):
            return None
        if not isinstance(row_map["id"], str):
            return None
        reset_type = row_map["resetType"]
        if not isinstance(reset_type, str) or reset_type not in _RESET_CREDIT_TYPES:
            return None
        status = row_map["status"]
        if not isinstance(status, str) or status not in _RESET_CREDIT_STATUSES:
            return None
        if not _fits_i64(row_map["grantedAt"]):
            return None
        for field in ("expiresAt", "title", "description"):
            if field not in row_map:
                continue
            value = row_map[field]
            if field == "expiresAt":
                if value is not None and not _fits_i64(value):
                    return None
            elif value is not None and not isinstance(value, str):
                return None
        if not _membership_valid(row_map, frozenset(required)):
            return None
    return True


def _quota_snapshot_state(
    snapshot: Mapping[str, object],
    *,
    expected_limit_id: str,
    full_window_blocks: bool,
    window_id_prefix: object = None,
) -> _SnapshotState | None:
    """Validate one full quota snapshot (main or additional bucket).

    Applies the same conservative rules everywhere: membership, identity,
    typed option members, window-slot structure and duplicate known
    periods. Returns the validated state, or ``None`` for drift. Coverage
    (both known window kinds) is the main snapshot's concern only; buckets
    may legitimately carry a single window. For buckets, a window reporting
    ``usedPercent == 100`` is treated as an enforced block on use; for the
    main snapshot, 100% used without any blocker flag stays a validated
    ``(100, 0)`` fact (U-010). ``window_id_prefix`` (the bucket's limit
    id) composes additional-window identities.
    """
    if not _membership_valid(snapshot, _KNOWN_SNAPSHOT_MEMBERS):
        return None

    limit_id = snapshot.get("limitId")
    if limit_id != expected_limit_id:
        return None

    limit_name = snapshot.get("limitName")
    if limit_name is not None and not isinstance(limit_name, str):
        return None

    plan_type = snapshot.get("planType")
    if plan_type is not None and (
        not isinstance(plan_type, str) or plan_type not in _EVIDENCED_PLANS
    ):
        return None

    reached_raw = snapshot.get("rateLimitReachedType")
    blocked = False
    if reached_raw is not None:
        if not isinstance(reached_raw, str) or reached_raw not in REACHED_TYPES:
            return None
        blocked = True

    spend_control = snapshot.get("spendControlReached")
    if spend_control is None or spend_control is False:
        pass
    elif spend_control is True:
        blocked = True
    else:
        return None

    credits_present = _credits_state(snapshot.get("credits"))
    if credits_present is None:
        return None

    individual = _individual_limit_state(snapshot.get("individualLimit"))
    if individual is None:
        return None
    individual_present, individual_exhausted = individual
    if individual_exhausted:
        blocked = True

    windows: list[_WindowFacts] = []
    for slot in _WINDOW_SLOTS:
        value = snapshot.get(slot)
        if value is None:
            continue
        entry = _as_mapping(value)
        if entry is None:
            return None
        window_id = (
            _bucket_window_id(window_id_prefix, slot)
            if window_id_prefix is not None
            else slot
        )
        if window_id is None:
            return None
        facts = _parse_window(slot, entry, window_id=window_id)
        if facts is None:
            return None
        windows.append(facts)
        if full_window_blocks and facts.window.used_percent == 100:
            blocked = True

    known_kinds = [facts.kind for facts in windows if facts.kind != "unknown"]
    if len(known_kinds) != len(set(known_kinds)):
        return None

    return _SnapshotState(
        windows=tuple(windows),
        blocked=blocked,
        unrepresentable=credits_present or individual_present,
    )


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
    with an empty windows array or explicitly degraded partial windows;
    they never surface as raw parser exceptions.
    """
    envelope = _as_mapping(result)
    if envelope is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)
    if not _membership_valid(envelope, _KNOWN_ENVELOPE_MEMBERS):
        return _failure("schema_changed", "schema_changed", retrieved_at)

    reset_credits_present = _reset_credits_state(
        envelope.get("rateLimitResetCredits")
    )
    if reset_credits_present is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    additional_present = False
    additional_blocked = False
    additional_unrepresentable = False
    bucket_states: list[tuple[str, _SnapshotState]] = []
    buckets = envelope.get("rateLimitsByLimitId")
    buckets_map: Mapping[str, object] | None = None
    if buckets is not None:
        buckets_map = _as_mapping(buckets)
        if buckets_map is None:
            return _failure("schema_changed", "schema_changed", retrieved_at)

    rate_limits = _as_mapping(envelope.get("rateLimits"))
    if rate_limits is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    if buckets_map is not None:
        mirror = _as_mapping(buckets_map.get(_LIMIT_ID))
        if mirror is None:
            return _failure("schema_changed", "schema_changed", retrieved_at)
        mirror_state = _quota_snapshot_state(
            mirror, expected_limit_id=_LIMIT_ID, full_window_blocks=False
        )
        if mirror_state is None or not _strict_equal(mirror, rate_limits):
            return _failure("schema_changed", "schema_changed", retrieved_at)
        for key in sorted(buckets_map):
            value = buckets_map[key]
            bucket_snapshot = _as_mapping(value)
            if bucket_snapshot is None:
                return _failure("schema_changed", "schema_changed", retrieved_at)
            if key == _LIMIT_ID:
                # The exact tagged success response mirrors the main codex
                # snapshot here: accept the entry only when it is fully
                # valid and consistent with the top-level snapshot.
                if not _strict_equal(bucket_snapshot, rate_limits):
                    return _failure(
                        "schema_changed", "schema_changed", retrieved_at
                    )
                continue
            state = _quota_snapshot_state(
                bucket_snapshot,
                expected_limit_id=key,
                full_window_blocks=True,
                window_id_prefix=key,
            )
            if state is None:
                return _failure("schema_changed", "schema_changed", retrieved_at)
            additional_present = True
            additional_blocked = additional_blocked or state.blocked
            additional_unrepresentable = (
                additional_unrepresentable or state.unrepresentable
            )
            bucket_states.append((key, state))

    main_state = _quota_snapshot_state(
        rate_limits, expected_limit_id=_LIMIT_ID, full_window_blocks=False
    )
    if main_state is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    plan = _safe_plan(rate_limits.get("planType"))
    main_kinds = [
        facts.kind for facts in main_state.windows if facts.kind != "unknown"
    ]

    # Deterministic emission: main slots, then additional buckets by key.
    emitted: list[CapacityWindow] = [f.window for f in main_state.windows]
    diagnostics: list[CapacityDiagnostic] = []
    for facts in main_state.windows:
        diagnostics.extend(facts.diagnostics)
    for _key, state in bucket_states:
        for facts in state.windows:
            emitted.append(facts.window)
            diagnostics.extend(facts.diagnostics)

    blocked = (
        main_state.blocked
        or additional_blocked
        or main_state.unrepresentable
        or additional_unrepresentable
        or reset_credits_present
    )

    if not emitted:
        # No window slots anywhere: the response cannot evidence any quota
        # coverage; that is insufficient evidence, not healthy emptiness.
        return _failure("unknown", "telemetry_unknown", retrieved_at)

    if blocked:
        # A backend-enforced block or a v1-unrepresentable metering state
        # exists: remaining capacity must not be inferred from percentages.
        # Withhold every validated pair (main and additional) with explicit
        # percentage_unknown diagnostics and degrade to unknown (U-010).
        degraded: list[CapacityWindow] = []
        for window in emitted:
            if window.used_percent is None:
                degraded.append(window)
                continue
            stripped, diagnostic = _without_percentages(window)
            degraded.append(stripped)
            diagnostics.append(diagnostic)
        diagnostics.append(CapacityDiagnostic(code="telemetry_unknown"))
        return CapacitySnapshot(
            schema_version=1,
            provider=PROVIDER,
            source=SOURCE,
            retrieved_at=retrieved_at,
            status="unknown",
            windows=tuple(degraded),
            diagnostics=tuple(diagnostics),
            plan=plan,
        )

    if (
        additional_present
        or "five_hour" not in main_kinds
        or "weekly" not in main_kinds
    ):
        # Additional metered buckets beyond the main quota are present, or
        # an expected main window constraint is missing: never healthy,
        # keep the validated facts and degrade the overall status to
        # unknown.
        diagnostics.append(CapacityDiagnostic(code="telemetry_unknown"))
        return CapacitySnapshot(
            schema_version=1,
            provider=PROVIDER,
            source=SOURCE,
            retrieved_at=retrieved_at,
            status="unknown",
            windows=tuple(emitted),
            diagnostics=tuple(diagnostics),
            plan=plan,
        )

    return CapacitySnapshot(
        schema_version=1,
        provider=PROVIDER,
        source=SOURCE,
        retrieved_at=retrieved_at,
        status="ok",
        windows=tuple(emitted),
        diagnostics=tuple(diagnostics),
        plan=plan,
    )
