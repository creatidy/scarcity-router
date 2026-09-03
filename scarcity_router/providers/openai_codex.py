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
  envelope, and the exact tagged schema *requires* the three members
  ``rateLimits``, ``rateLimitsByLimitId`` and ``rateLimitResetCredits``. A
  missing member is drift and fails closed as ``schema_changed``; an
  explicit ``null`` is a valid absent state for the two optional-valued
  members. Additive envelope members are tolerated when scalar and fail
  closed when structured (they may carry uninterpretable constraining
  data);
- ``rateLimitResetCredits`` is the reset-credit summary (``availableCount``
  plus an opaque ``credits`` collection). A valid present summary has no
  v1 representation: it degrades the snapshot to ``status="unknown"`` and
  withholds the percentage pairs (U-010);
- ``rateLimits`` is the protocol's ``RateLimitSnapshot`` with the evidenced
  nine members: ``limitId``, ``limitName``, ``primary``, ``secondary``,
  ``credits``, ``individualLimit``, ``spendControlReached``, ``planType``
  and ``rateLimitReachedType``. Snapshot members are option-typed: missing
  and ``null`` both mean an absent state; present values are type-validated
  and malformed shapes fail closed:
  - ``limitId`` must be exactly the evidenced quota identity ``"codex"``;
  - ``primary``/``secondary`` are the only window slots, validated as
    windows (positive integer ``windowDurationMins``) or explicit null.
    Slot names carry no period semantics: classification uses only the
    validated duration (300 minutes -> five-hour, 10080 -> weekly);
  - ``credits`` is the evidenced ``CreditsSnapshot``
    (``hasCredits``/``unlimited`` booleans, ``balance`` string-or-null): a
    present valid snapshot is a v1-unrepresentable credit state (unknown +
    withheld pairs), never interpreted;
  - ``individualLimit`` is the evidenced ``SpendControlLimitSnapshot``
    (``limit``, ``used``, ``remainingPercent``, ``resetsAt``): a present
    valid snapshot is a v1-unrepresentable spend state, and an exhausted
    one (``remainingPercent == 0`` or integer ``used >= limit``) is a
    backend blocker;
  - ``spendControlReached`` is the boolean spend-control blocker: absent,
    null or ``false`` means clear, ``true`` blocks, any other shape is
    drift;
  - ``rateLimitReachedType`` is the backend exhaustion flag with the
    evidenced enum members (``rate_limit_reached``,
    ``workspace_member_credits_depleted``,
    ``workspace_owner_credits_depleted``,
    ``workspace_owner_usage_limit_reached``,
    ``workspace_member_usage_limit_reached``, casing-tolerant). Any
    non-null value blocks;
- quota coverage is validated on the main snapshot: lacking either known
  window kind (five-hour or weekly) never reports ``ok``; it degrades to
  ``status="unknown"`` with validated partial windows preserved. Two slot
  windows sharing one known period duplicate the evidenced
  primary/secondary semantics and fail closed as ``schema_changed``;
- ``rateLimitsByLimitId`` carries additional metered buckets, each
  validated as a full quota snapshot with the same schema, identity (the
  map key must equal the bucket's ``limitId`` and must not be the main
  ``"codex"`` identity), window, duplicate-period and nested
  credit/spend-control rules. Any present bucket is additional metering v1
  cannot represent (``unknown``); a bucket-level blocker (reached flag,
  spend control, exhausted individual limit, or a window at
  ``usedPercent == 100``) withholds the main percentage pairs exactly like
  a main-snapshot blocker;
- blocked or v1-unrepresentable states never yield a healthy snapshot:
  ``status="unknown"`` with ``telemetry_unknown``, windows preserved with
  identity/duration/reset facts and percentage pairs withheld
  (``percentage_unknown`` per affected window). Known exhaustion *without*
  any blocker or unrepresentable state stays ``ok`` with the ``(100, 0)``
  pair;
- ``usedPercent`` is used-oriented for the evidenced schema (the PoC
  reading and the Codex extension's own ``remaining = 100 - used``
  derivation agree): valid integers 0..100 normalize to a
  ``used_percent``/``remaining_percent`` pair; anything else omits the
  pair;
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
  verbatim, never rewritten; any other value omits ``plan`` and never
  leaks.

This module also provides ``classify_app_server_message``, the deliberate
structural classifier for decoded JSONL app-server messages (response /
notification / request / invalid) used by the acquisition layer to match the
relevant response by request identity instead of timing. Observed framing
(2026-09-03 reconnaissance): requests carry ``jsonrpc``/``id``/``method``;
responses carry ``id`` with exactly one of ``result``/``error`` and omit the
``jsonrpc`` echo; notifications carry ``method`` without ``id``. A hybrid
message carrying ``method`` together with ``result``/``error`` is invalid:
it is neither a well-formed request nor a well-formed response and is
treated as protocol drift, never silently ignored.

Raw provider text, subprocess output, local paths, credentials and account
data never enter any output this module produces.
"""

from __future__ import annotations

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

# Evidenced members of the GetAccountRateLimitsResponse envelope; the exact
# tagged schema requires all three to be present (null is valid for the two
# optional-valued members; missing is drift).
_KNOWN_ENVELOPE_MEMBERS: frozenset[str] = frozenset(
    {"rateLimits", "rateLimitsByLimitId", "rateLimitResetCredits"}
)

# The evidenced quota identity for this read result.
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

# Evidenced RateLimitReachedType enum members, in both observed casings
# (the binary string tables carry snake_case; the app-server protocol
# renames fields to camelCase, and the value casing is unconfirmed). Public
# so tests build schema-backed reached fixtures from the evidence set.
REACHED_TYPES: frozenset[str] = frozenset(
    {
        "rate_limit_reached",
        "rateLimitReached",
        "workspace_member_credits_depleted",
        "workspaceMemberCreditsDepleted",
        "workspace_owner_credits_depleted",
        "workspaceOwnerCreditsDepleted",
        "workspace_owner_usage_limit_reached",
        "workspaceOwnerUsageLimitReached",
        "workspace_member_usage_limit_reached",
        "workspaceMemberUsageLimitReached",
    }
)

# Evidenced ``resetsAt`` representation: 10-digit epoch seconds.
_RESET_S_MIN = 1_000_000_000
_RESET_S_MAX = 9_999_999_999

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary value to a ``str``-keyed mapping, or ``None``."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _is_int(value: object) -> TypeGuard[int]:
    """True for a real JSON integer; booleans are not integers."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_structured(value: object) -> bool:
    """True for container values (objects/arrays) as opposed to scalars."""
    return isinstance(value, (Mapping, list))


def _membership_valid(
    container: Mapping[str, object],
    known: frozenset[str],
) -> bool:
    """Additive scalars tolerated; additive structured members are drift."""
    for key, value in container.items():
        if key in known:
            continue
        if _is_structured(value):
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

    - ``"notification"``: object with a string ``method`` and no integer
      ``id`` (observed notifications also carry ``params``/``emittedAtMs``,
      which are not part of the classification);
    - ``"request"``: object with a string ``method`` and an integer ``id``
      and no response fields (a server-initiated request; the collector
      never answers these);
    - ``"response"``: object without ``method`` that carries an integer
      ``id`` and exactly one of ``result``/``error``;
    - ``"invalid"``: anything else, including non-objects, string ids,
      messages carrying both ``result`` and ``error``, and hybrids carrying
      ``method`` together with ``result``/``error`` (neither a well-formed
      request nor a well-formed response: protocol drift, never silently
      ignored).
    """
    envelope = _as_mapping(message)
    if envelope is None:
        return "invalid"
    has_method = isinstance(envelope.get("method"), str)
    has_response_fields = "result" in envelope or "error" in envelope
    if has_method and has_response_fields:
        return "invalid"
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
) -> _WindowFacts | None:
    """Normalize one window object from a known window slot.

    Returns the window facts, or ``None`` for structural drift (a slot
    object without a validated positive integer ``windowDurationMins``):
    the caller fails the whole response closed instead of partially
    decoding it.
    """
    mins = entry.get("windowDurationMins")
    if not _is_int(mins) or mins <= 0:
        return None

    kind, duration_seconds = _KNOWN_DURATIONS_MINS.get(
        mins, ("unknown", mins * 60)
    )

    diagnostics: list[CapacityDiagnostic] = []
    if kind == "unknown":
        diagnostics.append(
            CapacityDiagnostic(code="window_semantics_unknown", window_id=slot)
        )

    used = _used_pair(entry.get("usedPercent"))
    used_percent: int | None = None
    remaining_percent: int | None = None
    if used is None:
        diagnostics.append(
            CapacityDiagnostic(code="percentage_unknown", window_id=slot)
        )
    else:
        used_percent, remaining_percent = used

    resets_at = _canonical_from_epoch_s(entry.get("resetsAt"))
    if resets_at is None:
        diagnostics.append(
            CapacityDiagnostic(code="reset_unknown", window_id=slot)
        )

    window = CapacityWindow(
        resource="tokens",
        kind=kind,
        duration_seconds=duration_seconds,
        used_percent=used_percent,
        remaining_percent=remaining_percent,
        resets_at=resets_at,
        window_id=slot,
    )
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
    return stripped, CapacityDiagnostic(
        code="percentage_unknown", window_id=window.window_id
    )


def _credits_state(value: object) -> bool | None:
    """Validate the typed ``CreditsSnapshot``; ``None`` means drift.

    Returns ``True`` when a valid credit state is present (v1-unrepresentable),
    ``False`` when absent (null or missing), ``None`` for malformed shapes.
    ``hasCredits``/``unlimited`` are booleans and ``balance`` is a decimal
    string or null in the evidenced schema; the balance value itself is
    never interpreted or surfaced.
    """
    if value is None:
        return False
    snapshot = _as_mapping(value)
    if snapshot is None:
        return None
    if not _membership_valid(snapshot, frozenset({"hasCredits", "unlimited", "balance"})):
        return None
    has_credits = snapshot.get("hasCredits")
    if has_credits is not None and not isinstance(has_credits, bool):
        return None
    unlimited = snapshot.get("unlimited")
    if unlimited is not None and not isinstance(unlimited, bool):
        return None
    balance = snapshot.get("balance")
    if balance is not None and not isinstance(balance, str):
        return None
    return True


def _individual_limit_state(value: object) -> tuple[bool, bool] | None:
    """Validate the typed ``SpendControlLimitSnapshot``.

    Returns ``(present, exhausted)``; ``None`` means drift. The evidenced
    members are ``limit``, ``used``, ``remainingPercent`` and ``resetsAt``.
    A present valid snapshot is a v1-unrepresentable spend state; an
    exhausted one (integer ``remainingPercent == 0`` or integer
    ``used >= limit``) is a backend blocker. Amounts may be integers or
    decimal strings in the schema; string amounts are never interpreted.
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
    limit = snapshot.get("limit")
    if limit is not None and not _is_int(limit) and not isinstance(limit, str):
        return None
    used = snapshot.get("used")
    if used is not None and not _is_int(used) and not isinstance(used, str):
        return None
    remaining = snapshot.get("remainingPercent")
    if remaining is not None and not _is_int(remaining):
        return None
    resets = snapshot.get("resetsAt")
    if resets is not None and not _is_int(resets):
        return None
    exhausted = remaining == 0
    if _is_int(limit) and _is_int(used) and not exhausted:
        exhausted = used >= limit
    return True, exhausted


def _reset_credits_state(value: object) -> bool | None:
    """Validate the typed reset-credit summary; ``None`` means drift.

    Returns ``True`` when a valid summary is present
    (v1-unrepresentable), ``False`` when absent (null or missing). The
    evidenced members are the integer ``availableCount`` and the opaque
    ``credits`` collection; their values are never interpreted or surfaced.
    """
    if value is None:
        return False
    summary = _as_mapping(value)
    if summary is None:
        return None
    if not _membership_valid(summary, frozenset({"availableCount", "credits"})):
        return None
    available = summary.get("availableCount")
    if available is not None and not _is_int(available):
        return None
    return True


def _quota_snapshot_state(
    snapshot: Mapping[str, object],
    *,
    expected_limit_id: str,
    full_window_blocks: bool,
) -> _SnapshotState | None:
    """Validate one full quota snapshot (main or additional bucket).

    Applies the same conservative rules everywhere: membership, identity,
    typed option members, window-slot structure and duplicate known
    periods. Returns the validated state, or ``None`` for drift. Coverage
    (both known window kinds) is the main snapshot's concern only; buckets
    may legitimately carry a single window. For buckets, a window reporting
    ``usedPercent == 100`` is treated as an enforced block on use; for the
    main snapshot, 100% used without any blocker flag stays a validated
    ``(100, 0)`` fact (U-010).
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
    if plan_type is not None and not isinstance(plan_type, str):
        return None

    reached_raw = snapshot.get("rateLimitReachedType")
    if reached_raw is not None and not isinstance(reached_raw, str):
        return None
    blocked = reached_raw is not None

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
        if not _membership_valid(entry, frozenset({"usedPercent", "windowDurationMins", "resetsAt"})):
            return None
        facts = _parse_window(slot, entry)
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
    # The exact tagged schema requires all three envelope members present;
    # a missing member is drift, an explicit null is a valid absent state.
    for member in ("rateLimits", "rateLimitsByLimitId", "rateLimitResetCredits"):
        if member not in envelope:
            return _failure("schema_changed", "schema_changed", retrieved_at)

    reset_credits_present = _reset_credits_state(
        envelope.get("rateLimitResetCredits")
    )
    if reset_credits_present is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    additional_present = False
    additional_blocked = False
    additional_unrepresentable = False
    buckets = envelope.get("rateLimitsByLimitId")
    if buckets is not None:
        buckets_map = _as_mapping(buckets)
        if buckets_map is None:
            return _failure("schema_changed", "schema_changed", retrieved_at)
        for key, value in buckets_map.items():
            bucket_snapshot = _as_mapping(value)
            if bucket_snapshot is None:
                return _failure("schema_changed", "schema_changed", retrieved_at)
            # The map key must equal the bucket's own quota identity and
            # must not shadow the main codex identity.
            if key == _LIMIT_ID:
                return _failure("schema_changed", "schema_changed", retrieved_at)
            state = _quota_snapshot_state(
                bucket_snapshot,
                expected_limit_id=key,
                full_window_blocks=True,
            )
            if state is None:
                return _failure("schema_changed", "schema_changed", retrieved_at)
            additional_present = True
            additional_blocked = additional_blocked or state.blocked
            additional_unrepresentable = (
                additional_unrepresentable or state.unrepresentable
            )

    rate_limits = _as_mapping(envelope.get("rateLimits"))
    if rate_limits is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)
    main_state = _quota_snapshot_state(
        rate_limits, expected_limit_id=_LIMIT_ID, full_window_blocks=False
    )
    if main_state is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    if not main_state.windows:
        # No window slots at all: the response cannot evidence any quota
        # coverage; that is insufficient evidence, not healthy emptiness.
        return _failure("unknown", "telemetry_unknown", retrieved_at)

    plan = _safe_plan(rate_limits.get("planType"))
    main_kinds = [
        facts.kind for facts in main_state.windows if facts.kind != "unknown"
    ]

    blocked = (
        main_state.blocked
        or additional_blocked
        or main_state.unrepresentable
        or additional_unrepresentable
        or reset_credits_present
    )
    windows: list[CapacityWindow] = [facts.window for facts in main_state.windows]
    diagnostics: list[CapacityDiagnostic] = []
    for facts in main_state.windows:
        diagnostics.extend(facts.diagnostics)

    if blocked:
        # A backend-enforced block or a v1-unrepresentable metering state
        # exists (main or additional): remaining capacity must not be
        # inferred from percentages. Withhold validated pairs with explicit
        # percentage_unknown diagnostics and degrade to unknown (U-010).
        degraded: list[CapacityWindow] = []
        for window in windows:
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
        # an expected window constraint is missing: never healthy, keep the
        # validated facts and degrade the overall status to unknown.
        diagnostics.append(CapacityDiagnostic(code="telemetry_unknown"))
        return CapacitySnapshot(
            schema_version=1,
            provider=PROVIDER,
            source=SOURCE,
            retrieved_at=retrieved_at,
            status="unknown",
            windows=tuple(windows),
            diagnostics=tuple(diagnostics),
            plan=plan,
        )

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
