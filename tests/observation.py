"""Shared fake-collector pairing helper for application tests (D-039).

The OpenAI status collector returns one :class:`OpenAICodexObservation`
(snapshot + paired eligibility report). Application-test fakes model the
production pairing conservatively: a snapshot with status ``ok`` pairs with
an ``eligible`` report; any other snapshot status pairs with the matching
fail-closed ``unknown`` report (mirroring the acquisition layer's failure
mapping). Tests that need a specific account classification construct their
own :class:`ExecutionEligibility` explicitly.
"""

from __future__ import annotations

from scarcity_router.capacity import CapacitySnapshot
from scarcity_router.eligibility import ELIGIBILITY_SCHEMA_VERSION, ExecutionEligibility
from scarcity_router.providers.openai_codex_acquisition import OpenAICodexObservation

_FAILURE_REASON_BY_STATUS = {
    "unavailable": "telemetry_unavailable",
    "auth_required": "telemetry_auth_required",
    "unsupported": "telemetry_unsupported",
    "schema_changed": "telemetry_invalid",
    "unknown": "telemetry_invalid",
}


def paired_observation(snapshot: CapacitySnapshot) -> OpenAICodexObservation:
    eligibility = ExecutionEligibility(
        schema_version=ELIGIBILITY_SCHEMA_VERSION,
        provider=snapshot.provider,
        source=snapshot.source,
        retrieved_at=snapshot.retrieved_at,
        state=(
            "eligible"
            if snapshot.status == "ok"
            else "unknown"
        ),
        reason_codes=(
            ()
            if snapshot.status == "ok"
            else (_FAILURE_REASON_BY_STATUS.get(snapshot.status, "telemetry_invalid"),)
        ),
    )
    return OpenAICodexObservation(snapshot=snapshot, eligibility=eligibility)
