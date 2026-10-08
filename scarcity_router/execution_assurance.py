"""Internal source/control reception, separate from configuration and billing.

No client, configuration or worker decoder accepts these proof records. Only a
Router-owned reviewed control producer can supply positive evidence; current
production adapters do not implement that producer and remain unsupported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .resource_state import ResourceIdentity

if TYPE_CHECKING:
    from .gateway_adapters import CallObservation, ExecutionAdapter
    from .resource_state import ResourceRegistryEntry

NON_PAID_MODES = frozenset({"payg", "purchased_credit", "paid_overflow", "unaccounted_paid_helper"})
NON_PAID_REFUSAL_CODES = frozenset({
    "no_payg_source_mismatch", "no_payg_evidence_unavailable", "no_payg_binding_changed",
    "no_payg_path_unverified", "no_payg_path_incomplete", "no_payg_evidence_invalid",
    "no_payg_evidence_expired", "no_payg_access_mode_forbidden",
    "no_payg_evidence_stale",
})


def _utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("control evidence requires UTC")
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class ExecutionControlScope:
    """Exact source/resource and adapter/control incarnation, not account IDs."""

    resource: ResourceIdentity
    adapter_name: str
    adapter_version: str
    instance_token: str
    control_revision: str | None


@dataclass(frozen=True)
class NonPaidControlEvidence:
    """Conclusion from inspected control code, never a billing attestation.

    ``controlled_modes`` describes controls covering the complete admitted
    context (including helpers), not a current account balance. Freshness is
    admission evidence validity; the controls must hold for the entire bound
    execution, not merely until this observation expires. No default validity
    duration, amount, quota conversion or task budget is invented.
    """

    scope: ExecutionControlScope
    evidence_class: str
    observed_at: str
    valid_until: str
    control_reference: str = field(repr=False)
    controlled_modes: frozenset[str]


@runtime_checkable
class NonPaidControlProducer(Protocol):
    """Trusted pure metadata/control producer, NOT an execution capability flag.

    It cannot run discovery/model/auth probes while producing evidence. A
    reported runtime or configured label cannot implement verification. No
    current production adapter claims this interface's full closure property.
    """

    def non_paid_control_revision(self, resource_id: str) -> str | None: ...

    def non_paid_control_evidence(self, resource_id: str) -> NonPaidControlEvidence | None: ...


@dataclass(frozen=True)
class ExecutionAssurance:
    current_scope: ExecutionControlScope
    evidence: NonPaidControlEvidence | None

    def refusal_codes(self, resource: ResourceIdentity, now: datetime, *, max_age_seconds: int) -> tuple[str, ...]:
        if self.current_scope.resource != resource:
            return ("no_payg_source_mismatch",)
        proof = self.evidence
        if self.current_scope.control_revision is None or proof is None:
            return ("no_payg_evidence_unavailable",)
        if proof.scope != self.current_scope:
            return ("no_payg_binding_changed",)
        if proof.evidence_class != "verified_control":
            return ("no_payg_path_unverified",)
        if not proof.control_reference or not proof.scope.control_revision:
            return ("no_payg_evidence_unavailable",)
        if type(proof.controlled_modes) is not frozenset or not proof.controlled_modes.issubset(NON_PAID_MODES):
            return ("no_payg_evidence_invalid",)
        if proof.controlled_modes != NON_PAID_MODES:
            return ("no_payg_path_incomplete",)
        try:
            observed = _utc(proof.observed_at)
            expires = _utc(proof.valid_until)
        except Exception:
            return ("no_payg_evidence_invalid",)
        if observed > now or expires <= observed:
            return ("no_payg_evidence_invalid",)
        if now >= expires:
            return ("no_payg_evidence_expired",)
        if (now - observed).total_seconds() > max_age_seconds:
            return ("no_payg_evidence_stale",)
        return ()

    def to_dict(self, now: datetime, *, max_age_seconds: int) -> dict[str, object]:
        codes = self.refusal_codes(self.current_scope.resource, now, max_age_seconds=max_age_seconds)
        return {
            "schema_version": 1,
            "resource_id": self.current_scope.resource.resource_id,
            "admission_enforcement": "verified_non_paid_control" if not codes else "unverified",
            "provider_billing": "unknown",
            "total_task_cost": "unknown",
            "reason_codes": list(codes),
        }


def receive_execution_assurance(resource: ResourceIdentity, adapter: ExecutionAdapter) -> ExecutionAssurance:
    """Receive only a trusted reviewed adapter-control producer, never raw JSON.

    Current scope comes from the actual adapter incarnation and control revision,
    independently of its cached evidence. Invalid/erroring producers fail closed
    without diagnostic interpolation. All shipped production adapters currently
    lack complete-path controls and therefore return an unsupported assessment.
    """
    revision: str | None = None
    evidence: NonPaidControlEvidence | None = None
    if isinstance(adapter, NonPaidControlProducer):
        try:
            revision = adapter.non_paid_control_revision(resource.resource_id)
            evidence = adapter.non_paid_control_evidence(resource.resource_id)
            if type(revision) is not str or not revision or not isinstance(evidence, NonPaidControlEvidence):
                revision, evidence = None, None
        except Exception:
            revision, evidence = None, None
    scope = ExecutionControlScope(
        resource, adapter.adapter_name, adapter.adapter_version,
        f"adapter-{id(adapter):x}", revision,
    )
    return ExecutionAssurance(scope, evidence)


def source_call_facts(
    entry: ResourceRegistryEntry, calls: tuple[CallObservation, ...], *, now: datetime,
    requested_output_ceiling: int | None = None, local_reservation_active: bool = False,
    assurance: ExecutionAssurance | None = None,
    assurance_checked_at: datetime | None = None,
) -> dict[str, object]:
    """Local versioned receipt for Kernel accounting, not a billing calculator.

    Reuses exact observed calls and configured rate facts; it never recomputes
    charges, converts tokens to quota, infers accounts or deduplicates a task.
    Kernel retains call/Attempt identity and whole-path receipt reconciliation.
    No existing worker/audit/usage or recommendation wire schema is changed.
    """
    reported = sum(call.provider_reported_usage is not None for call in calls)
    estimated = sum(call.estimated_usage is not None for call in calls)
    covered = sum(call.provider_reported_usage is not None or call.estimated_usage is not None for call in calls)
    promotions: list[dict[str, object]] = []
    if entry.observation is not None:
        for promotion in entry.observation.promotions:
            state = "unknown_validity"
            if promotion.valid_until is not None:
                state = "expired" if now >= _utc(promotion.valid_until) else "reported_conditional"
            if promotion.valid_from is not None and now < _utc(promotion.valid_from):
                state = "not_started"
            promotions.append({
                "state": state,
                "execution_qualification": "unverified", "free_quota_created": False,
            })
    observations: list[dict[str, object]] = []
    for call in calls:
        item = call.to_dict()
        _ = item.pop("note", None)
        observations.append(item)
    return {
        "schema_version": 1, "resource_id": entry.identity.resource_id,
        "access_mode": {"configured_entitlement": entry.identity.entitlement, "basis": "configuration"},
        "price_rate": {"unit": "micro_usd_per_million_tokens", "basis": "administrator_registration",
                       "facts": entry.cost.to_dict() if entry.cost is not None else None},
        "output": {"requested_ceiling": requested_output_ceiling,
                   "configured_channel_allowance": entry.capabilities.output_limit_tokens,
                   "configured_cap_control": entry.capabilities.output_limit_control,
                   "hard_spend_guarantee": "unestablished"},
        "quota": {"bound_pool_ids": list(entry.identity.quota_pool_ids),
                  "independent_account_relationship": "unknown", "tokens_deduct_quota": False},
        "reservation": {"scope": "router_local", "router_local_held_for_attempt": local_reservation_active, "provider_reserved": False},
        "usage": {"visible_call_count": len(calls), "visible_usage_coverage": "reported_or_estimated" if calls and covered == len(calls) else "incomplete_or_unknown",
                  "provider_reported_observations": reported, "estimated_observations": estimated,
                  "missing_usage_observations": len(calls) - covered,
                  "helper_retry_multiplicity": "unknown", "observations": observations},
        "promotions": promotions,
        "non_paid_admission": assurance.to_dict(assurance_checked_at or now, max_age_seconds=entry.freshness_ttl_seconds) if assurance is not None else {"admission_enforcement": "unverified"},
        "provider_billing": "unknown", "total_task_cost": "unknown", "provider_settlement": "unobserved",
    }
