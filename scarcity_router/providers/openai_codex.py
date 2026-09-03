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
  envelope with the evidenced members ``rateLimits``, ``rateLimitsByLimitId``
  and ``rateLimitResetCredits``. ``rateLimits`` is required and must be an
  object; a missing or non-object value is structural drift and fails closed
  as ``schema_changed``. Additive envelope members are tolerated when
  scalar, and fail closed when structured (they may carry uninterpretable
  constraining data);
- ``rateLimitsByLimitId`` carries additional metered buckets keyed by limit
  id. Absent, null or empty means none. A present bucket value must be an
  object (drift otherwise). Any present bucket means the account is metered
  beyond the main quota, which v1 cannot represent: the snapshot degrades to
  ``status="unknown"`` (``telemetry_unknown``). A bucket evidenced as
  exhausted (non-null ``rateLimitReachedType`` or a window slot reporting
  ``usedPercent == 100``) is a backend-enforced blocker and additionally
  withholds the main windows' percentage pairs, because remaining capacity
  must not be inferred while another metered bucket blocks use;
- ``rateLimitResetCredits`` (evidenced ``ResetCreditStatus``: available /
  redeeming) is tolerated as opaque metadata: absent, null or object. It has
  no v1 representation, is never surfaced, and never affects health;
- ``rateLimits`` is the protocol's ``RateLimitSnapshot`` with the evidenced
  nine members: ``limitId``, ``limitName``, ``primary``, ``secondary``,
  ``credits``, ``individualLimit``, ``spendControlReached``, ``planType``
  and ``rateLimitReachedType``. Membership is validated conservatively:
  - ``limitId`` must be exactly the evidenced quota identity ``"codex"``;
    a missing, null or different identity is incompatible drift;
  - ``primary``/``secondary`` are the only window slots. A null or absent
    slot is a structurally valid absent limit; an object slot must be a
    validated window (positive integer ``windowDurationMins``); any other
    slot shape is drift. Slot names carry no period semantics:
    classification uses only the validated duration (300 minutes ->
    five-hour, 10080 minutes -> weekly);
  - ``credits`` (evidenced ``CreditsSnapshot``: ``hasCredits`` /
    ``unlimited`` / ``balance``) and ``individualLimit`` are known non-window
    metadata: absent, null or object values are tolerated and never
    interpreted or surfaced (credit-state semantics are a documented
    residual, not guessed);
  - ``spendControlReached`` is the evidenced boolean spend-control blocker:
    absent, null or ``false`` means not blocked; ``true`` is a
    backend-enforced blocker that degrades the snapshot to
    ``status="unknown"`` with the percentage pairs withheld; any other
    value is drift;
  - ``limitName`` is metadata (absent, null or string, never surfaced);
- quota coverage is validated: a snapshot lacking either known window kind
  (five-hour or weekly) never reports ``ok``; it degrades to
  ``status="unknown"`` with the validated partial windows preserved. Two
  slot windows sharing one known period duplicate the evidenced
  primary/secondary semantics and fail closed as ``schema_changed``;
- ``usedPercent`` is used-oriented for the evidenced schema (the PoC reading
  and the Codex extension's own ``remaining = 100 - used`` derivation agree):
  valid integers 0..100 normalize to a ``used_percent``/``remaining_percent``
  pair; anything else omits the pair;
- ``rateLimitReachedType`` is the backend exhaustion flag with the evidenced
  enum members (``rate_limit_reached``, ``workspace_member_credits_depleted``,
  ``workspace_owner_usage_limit_reached``,
  ``workspace_member_usage_limit_reached``, in either observed casing).
  ``null``/absent means not reached. Any non-null value means the backend
  asserts exhaustion: the snapshot degrades to ``status="unknown"`` and the
  percentage pairs are withheld with ``percentage_unknown`` diagnostics
  (v1 has no reached field; see U-010 in docs/decisions.md). Unknown
  non-null strings take the same degraded path; a non-string value is drift;
- ``resetsAt`` is evidenced as a 10-digit epoch-**second** integer; values
  outside that representation (epoch milliseconds, zero, negative, floats,
  strings) are rejected rather than misinterpreted;
- ``plan`` comes only from the adapter evidence allowlist of validated plan
  enum members that the v1 safe-ID grammar permits as-is: ``free``, ``go``,
  ``plus``, ``pro``, ``prolite``, ``team``, ``edu``, ``edu_pro``,
  ``enterprise``, ``ent26``, ``enterprise_cbp_automation``,
  ``enterprise_cbp_usage_based``, ``self_serve_business_prolite``,
  ``self_serve_business_usage_based`` and ``run`` (underscores are part of
  the v1 grammar). Any other value omits ``plan``; arbitrary provider text
  is never retained.

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

# The two evidenced window slots of the protocol's RateLimitSnapshot. Slot
# names identify position in the provider snapshot only; period semantics
# always come from the validated windowDurationMins.
_WINDOW_SLOTS: tuple[str, str] = ("primary", "secondary")

# Evidenced non-window metadata members of RateLimitSnapshot: absent, null
# or object values are tolerated and never interpreted or surfaced.
_METADATA_OBJECT_KEYS: frozenset[str] = frozenset({"credits", "individualLimit"})

# The complete evidenced member set of RateLimitSnapshot.
_KNOWN_SNAPSHOT_MEMBERS: frozenset[str] = frozenset(
    {
        "limitId",
        "limitName",
        "planType",
        "rateLimitReachedType",
        "spendControlReached",
        *_WINDOW_SLOTS,
        *_METADATA_OBJECT_KEYS,
    }
)

# Evidenced members of the GetAccountRateLimitsResponse envelope.
_KNOWN_ENVELOPE_MEMBERS: frozenset[str] = frozenset(
    {"rateLimits", "rateLimitsByLimitId", "rateLimitResetCredits"}
)

# The evidenced quota identity for this read result.
_LIMIT_ID = "codex"

# Adapter evidence allowlist of validated plan enum members (see
# docs/poc-evidence.md, 2026-09-03 reconnaissance). The v1 safe-ID grammar
# permits underscores, so every evidenced member is preserved as-is.
# Extending this set requires new evidence.
_EVIDENCED_PLANS: frozenset[str] = frozenset(
    {
        "free",
        "go",
        "plus",
        "pro",
        "prolite",
        "team",
        "edu",
        "edu_pro",
        "enterprise",
        "ent26",
        "enterprise_cbp_automation",
        "enterprise_cbp_usage_based",
        "self_serve_business_prolite",
        "self_serve_business_usage_based",
        "run",
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
) -> tuple[CapacityWindow, list[CapacityDiagnostic], str] | None:
    """Normalize one window object from a known window slot.

    Returns the window, its window-scoped diagnostics and the validated
    period kind, or ``None`` for structural drift (a slot object without a
    validated positive integer ``windowDurationMins``): the caller fails
    the whole response closed instead of partially decoding it.
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
    return window, diagnostics, kind


def _without_percentages(
    window: CapacityWindow,
) -> tuple[CapacityWindow, CapacityDiagnostic]:
    """Drop the percentage pair from a validated window (backend blocker).

    Used when the backend asserts an enforced block on use (reached limit,
    spend control, exhausted additional bucket): remaining capacity must not
    be inferred from percentages then, so the pair is withheld and reported
    as ``percentage_unknown``. Window identity, duration and reset facts
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


def _additional_bucket_state(
    buckets: Mapping[str, object],
) -> tuple[bool, bool] | None:
    """Inspect additional ``rateLimitsByLimitId`` buckets conservatively.

    Returns ``(present, exhausted)``, or ``None`` for drift (a non-object
    bucket value). A bucket is exhausted when its own ``rateLimitReachedType``
    is a non-null string or one of its window slots reports the integer
    ``usedPercent == 100``.
    """
    exhausted = False
    for value in buckets.values():
        bucket = _as_mapping(value)
        if bucket is None:
            return None
        reached = bucket.get("rateLimitReachedType")
        if isinstance(reached, str):
            exhausted = True
        for slot in _WINDOW_SLOTS:
            window = _as_mapping(bucket.get(slot))
            if window is None:
                continue
            percentage = window.get("usedPercent")
            if _is_int(percentage) and percentage == 100:
                exhausted = True
    return True, exhausted


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

    # Conservative membership validation of the evidenced envelope shape.
    for key, value in envelope.items():
        if key in _KNOWN_ENVELOPE_MEMBERS:
            continue
        if not _is_structured(value):
            # Additive scalar members cannot carry a constraining window.
            continue
        # An additive structured member (object or array) under an unknown
        # key may carry uninterpretable constraining data: fail closed.
        return _failure("schema_changed", "schema_changed", retrieved_at)

    reset_credits = envelope.get("rateLimitResetCredits")
    if reset_credits is not None and not isinstance(reset_credits, Mapping):
        return _failure("schema_changed", "schema_changed", retrieved_at)

    additional_present = False
    additional_exhausted = False
    buckets = envelope.get("rateLimitsByLimitId")
    if buckets is not None:
        buckets_map = _as_mapping(buckets)
        if buckets_map is None:
            return _failure("schema_changed", "schema_changed", retrieved_at)
        if buckets_map:
            state = _additional_bucket_state(buckets_map)
            if state is None:
                return _failure("schema_changed", "schema_changed", retrieved_at)
            additional_present, additional_exhausted = state

    rate_limits = _as_mapping(envelope.get("rateLimits"))
    if rate_limits is None:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    # Conservative membership validation of the evidenced snapshot shape.
    for key, value in rate_limits.items():
        if key in _KNOWN_SNAPSHOT_MEMBERS:
            continue
        if not _is_structured(value):
            continue
        return _failure("schema_changed", "schema_changed", retrieved_at)

    limit_id = rate_limits.get("limitId")
    if limit_id != _LIMIT_ID:
        # The evidenced quota identity for this read is "codex"; a missing,
        # null or different identity is incompatible drift, never healthy.
        return _failure("schema_changed", "schema_changed", retrieved_at)

    for key in _METADATA_OBJECT_KEYS:
        value = rate_limits.get(key)
        if value is None or isinstance(value, Mapping):
            continue
        return _failure("schema_changed", "schema_changed", retrieved_at)

    limit_name = rate_limits.get("limitName")
    if limit_name is not None and not isinstance(limit_name, str):
        return _failure("schema_changed", "schema_changed", retrieved_at)

    plan_type = rate_limits.get("planType")
    if plan_type is not None and not isinstance(plan_type, str):
        return _failure("schema_changed", "schema_changed", retrieved_at)

    reached_raw = rate_limits.get("rateLimitReachedType")
    if reached_raw is not None and not isinstance(reached_raw, str):
        return _failure("schema_changed", "schema_changed", retrieved_at)
    # Any non-null string (evidenced enum member or not) asserts a backend
    # reached state; the distinction is preserved only through the shared
    # degraded representation below, never by guessing capacity.
    reached = reached_raw is not None

    spend_control = rate_limits.get("spendControlReached")
    if spend_control is None or spend_control is False:
        spend_control_blocked = False
    elif spend_control is True:
        spend_control_blocked = True
    else:
        return _failure("schema_changed", "schema_changed", retrieved_at)

    windows: list[CapacityWindow] = []
    diagnostics: list[CapacityDiagnostic] = []
    kinds: list[str] = []
    for slot in _WINDOW_SLOTS:
        value = rate_limits.get(slot)
        if value is None:
            # A null/absent slot is a structurally valid absent limit; the
            # coverage check below decides how honest that is.
            continue
        entry = _as_mapping(value)
        if entry is None:
            # A known window slot that is not an object is drift.
            return _failure("schema_changed", "schema_changed", retrieved_at)
        parsed = _parse_window(slot, entry)
        if parsed is None:
            return _failure("schema_changed", "schema_changed", retrieved_at)
        window, window_diagnostics, kind = parsed
        windows.append(window)
        diagnostics.extend(window_diagnostics)
        kinds.append(kind)

    # Two slot windows sharing a known period duplicate the evidenced
    # primary/secondary semantics (one five-hour, one weekly): drift.
    known_kinds = [kind for kind in kinds if kind != "unknown"]
    if len(known_kinds) != len(set(known_kinds)):
        return _failure("schema_changed", "schema_changed", retrieved_at)

    if not windows:
        # No window slots at all: the response cannot evidence any quota
        # coverage; that is insufficient evidence, not healthy emptiness.
        return _failure("unknown", "telemetry_unknown", retrieved_at)

    plan = _safe_plan(plan_type)

    blocked = reached or spend_control_blocked or additional_exhausted
    if blocked:
        # The backend enforces a block on use (reached limit, spend control
        # or an exhausted additional bucket). Remaining capacity must not be
        # inferred from percentages, so validated pairs are withheld with
        # explicit percentage_unknown diagnostics and the overall snapshot
        # degrades to unknown (v1 has no blocker field; U-010). Windows
        # whose pair was already unknown keep their existing diagnostic.
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

    if additional_present or "five_hour" not in known_kinds or "weekly" not in known_kinds:
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
