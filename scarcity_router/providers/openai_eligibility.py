"""OpenAI Codex execution-eligibility normalization (D-039).

Pure, deterministic provider-edge classification over the already decoded
``account/rateLimits/read`` JSON-RPC result — the exact same decoded value
the capacity parser (``openai_codex.parse_codex_rate_limits_result``) reads.
This module performs zero I/O: no clock, filesystem, environment, network or
subprocesses, and no credential access.

Owner policy for unattended autonomy (Infrastructure/creatidy-autonomy#15):

    subscription allowance is allowed;
    purchased-credit usage is forbidden;
    API PAYG is forbidden.

The mandatory pre-execution account conditions are therefore classified as
follows (unknown == unsafe; missing fields are never read as safe):

- ``credits.hasCredits`` must be explicitly ``false``; ``true`` classifies
  ``policy_blocked``/``purchased_credits_present``; absent, null or
  unreadable classifies ``unknown``/``credits_state_unknown``;
- ``ordinaryUsageAllowed`` must be explicitly ``true`` (current response
  generation); ``false`` is ``allowance_unavailable``/
  ``ordinary_usage_not_allowed``; null or a legacy-generation absence is
  ``unknown``/``ordinary_usage_unknown``;
- ``spendControlReached`` must be explicitly ``false``; ``true`` is
  ``allowance_unavailable``/``spend_control_reached``; null or absent is
  ``unknown``/``spend_control_state_unknown``;
- a validated non-null ``rateLimitReachedType``, an exhausted
  ``individualLimit`` (``remainingPercent == 0``), a non-null
  ``rateLimitUpsell`` and any main quota window at 100% used are
  ``allowance_unavailable`` blockers;
- membership drift (additive structured envelope/snapshot members) makes the
  whole observation untrustworthy: ``unknown``/``telemetry_invalid``.

Auth mode is deliberately not classified here: the rate-limits surface
carries no auth-mode member, and auth verification belongs to the execution
side's own bounded pre-call guard. A telemetry read that fails for auth
reasons surfaces through the acquisition layer's failure reports instead.

Balances, account ids and all other credit values are never interpreted
beyond the boolean classification above and never appear in any output.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeGuard, cast

from ..eligibility import ELIGIBILITY_SCHEMA_VERSION, ExecutionEligibility
from .openai_codex import (
    KNOWN_ENVELOPE_MEMBERS,
    KNOWN_SNAPSHOT_MEMBERS,
    REACHED_TYPES,
    membership_valid,
)

PROVIDER = "openai"
SOURCE = "codex_app_server"

_CREDITS_MEMBERS = frozenset({"hasCredits", "unlimited", "balance"})
_INDIVIDUAL_LIMIT_MEMBERS = frozenset(
    {"limit", "used", "remainingPercent", "resetsAt"}
)
_WINDOW_MEMBERS = frozenset({"usedPercent", "windowDurationMins", "resetsAt"})
_WINDOW_SLOTS = ("primary", "secondary")

# State precedence (checked in order): a malformed observation cannot
# support any classification; a forbidden credit state dominates every other
# signal; an unestablishable mandatory field is unknown == unsafe; explicit
# allowance blockers follow.
_UNKNOWN_STATE = "unknown"
_POLICY_BLOCKED_STATE = "policy_blocked"
_ALLOWANCE_STATE = "allowance_unavailable"
_ELIGIBLE_STATE = "eligible"


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in mapping):
            return None
        return cast(Mapping[str, object], value)
    return None


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_codex_execution_eligibility(
    result: object,
    *,
    retrieved_at: str,
) -> ExecutionEligibility:
    """Normalize one decoded rate-limits result into an eligibility report.

    The report always names the same observation identity the capacity
    snapshot carries (``provider``/``source``/``retrieved_at``); no failure
    raises — every untrustworthy or unreadable condition degrades to the
    fail-closed ``unknown`` state with a safe reason code.
    """
    reasons: set[str] = set()
    envelope = _as_mapping(result)
    if envelope is None or not membership_valid(envelope, KNOWN_ENVELOPE_MEMBERS):
        return _unknown_report(retrieved_at, {"telemetry_invalid"})

    rate_limits = _as_mapping(envelope.get("rateLimits"))
    if rate_limits is None or not membership_valid(
        rate_limits, KNOWN_SNAPSHOT_MEMBERS
    ):
        return _unknown_report(retrieved_at, {"telemetry_invalid"})

    # Mandatory credit state: explicitly absent purchased credits only.
    credits = _as_mapping(rate_limits.get("credits"))
    if credits is None:
        reasons.add("credits_state_unknown")
    else:
        has_credits = credits.get("hasCredits")
        unlimited = credits.get("unlimited")
        balance = credits.get("balance")
        structured_drift = not membership_valid(credits, _CREDITS_MEMBERS)
        if (
            structured_drift
            or not isinstance(has_credits, bool)
            or not isinstance(unlimited, bool)
            or (balance is not None and not isinstance(balance, str))
        ):
            reasons.add("credits_state_unknown")
        elif has_credits:
            reasons.add("purchased_credits_present")

    # Mandatory explicit ordinary-usage permission (current generation only).
    if "ordinaryUsageAllowed" not in envelope:
        reasons.add("ordinary_usage_unknown")
    else:
        ordinary = envelope["ordinaryUsageAllowed"]
        if ordinary is True:
            pass
        elif ordinary is False:
            reasons.add("ordinary_usage_not_allowed")
        elif ordinary is None:
            reasons.add("ordinary_usage_unknown")
        else:
            return _unknown_report(retrieved_at, {"telemetry_invalid"})

    # Mandatory explicit no-spend-control-blocker state.
    spend_control = rate_limits.get("spendControlReached")
    if spend_control is True:
        reasons.add("spend_control_reached")
    elif spend_control is False:
        pass
    elif spend_control is None:
        reasons.add("spend_control_state_unknown")
    else:
        return _unknown_report(retrieved_at, {"telemetry_invalid"})

    # Explicit reached/exhausted limit states forbid ordinary included usage.
    reached = rate_limits.get("rateLimitReachedType")
    if reached is not None:
        if not isinstance(reached, str) or reached not in REACHED_TYPES:
            return _unknown_report(retrieved_at, {"telemetry_invalid"})
        reasons.add("rate_limit_reached")

    individual_raw = rate_limits.get("individualLimit")
    if individual_raw is not None:
        individual = _as_mapping(individual_raw)
        if (
            individual is None
            or not membership_valid(individual, _INDIVIDUAL_LIMIT_MEMBERS)
            or any(member not in individual for member in _INDIVIDUAL_LIMIT_MEMBERS)
            or not isinstance(individual["limit"], str)
            or not isinstance(individual["used"], str)
            or not _is_int(individual["remainingPercent"])
            or not _is_int(individual["resetsAt"])
        ):
            return _unknown_report(retrieved_at, {"telemetry_invalid"})
        if individual["remainingPercent"] == 0:
            reasons.add("individual_limit_exhausted")

    # A non-null upsell is a recovery blocker; contents are never inspected.
    if envelope.get("rateLimitUpsell") is not None:
        reasons.add("upsell_present")

    # Any main quota window at 100% used is included-allowance exhaustion.
    for slot in _WINDOW_SLOTS:
        window_raw = rate_limits.get(slot)
        if window_raw is None:
            continue
        window = _as_mapping(window_raw)
        if window is None or not membership_valid(window, _WINDOW_MEMBERS):
            return _unknown_report(retrieved_at, {"telemetry_invalid"})
        used = window.get("usedPercent")
        if used is None:
            continue
        if not _is_int(used) or not 0 <= used <= 100:
            return _unknown_report(retrieved_at, {"telemetry_invalid"})
        if used == 100:
            reasons.add("included_window_exhausted")

    return _report(retrieved_at, reasons)


def _unknown_report(
    retrieved_at: str,
    reasons: set[str],
) -> ExecutionEligibility:
    reasons.add("telemetry_invalid")
    return _report(retrieved_at, reasons)


def _report(retrieved_at: str, reasons: set[str]) -> ExecutionEligibility:
    if not reasons:
        state = _ELIGIBLE_STATE
    elif "telemetry_invalid" in reasons:
        state = _UNKNOWN_STATE
    elif "purchased_credits_present" in reasons:
        state = _POLICY_BLOCKED_STATE
    elif reasons & {
        "credits_state_unknown",
        "ordinary_usage_unknown",
        "spend_control_state_unknown",
    }:
        state = _UNKNOWN_STATE
    else:
        state = _ALLOWANCE_STATE
    return ExecutionEligibility(
        schema_version=ELIGIBILITY_SCHEMA_VERSION,
        provider=PROVIDER,
        source=SOURCE,
        retrieved_at=retrieved_at,
        state=state,
        reason_codes=tuple(sorted(reasons)),
    )
