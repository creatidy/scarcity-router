"""Execution audit trail: the minimal D-043 metadata set (M03).

Per executed request the gateway records exactly the frozen D-043 field
set: request id, decision id, client/profile identity, routing-policy
version, state snapshot identity/version, selected target,
actually-executed target, adapter identity/version, start/end time, result
status, provider-reported usage and estimated usage where applicable.

Frozen hygiene rules (D-043/D-044):

- the default audit trail contains NO prompt or response contents, NO
  message text, NO tool arguments, NO credentials and NO raw provider
  payloads — every field is a safe identifier, a closed-vocabulary token,
  a bounded timestamp or a token count;
- retention is bounded: the in-memory trail keeps at most ``max_records``
  records and prunes records older than ``max_age_seconds`` at write
  time; a bounded non-zero default is frozen here and the
  administrator-configurable surface is M09 scope;
- the trail is deliberately IN-MEMORY with an injectable
  :class:`AuditSink` seam (D-041 lets slices that need no durable state
  run in memory); if M09 later needs the embedded durable store, the sink
  seam receives it without touching this contract.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .gateway_contracts import UsageTokens
from .gateway_validation import (
    exact_shape,
    v_canonical_ts,
    v_enum,
    v_instance,
    v_int,
    v_safe_id,
    v_text,
)

# ── Frozen value sets ─────────────────────────────────────────────────────────

AUDIT_SCHEMA_VERSION = 1

# Closed result-status vocabulary. A request that never reached dispatch
# (rejected at admission, limits or validation) is never reported as an
# execution failure: its record carries the rejection class and the reason
# codes, with no executed target and no usage.
RESULT_COMPLETED = "completed"
RESULT_CANCELLED = "cancelled"
RESULT_FAILED = "failed"
RESULT_FAILED_AMBIGUOUS = "failed_ambiguous"
RESULT_TIMED_OUT = "timed_out"
RESULT_REJECTED = "rejected"

RESULT_STATUSES: frozenset[str] = frozenset({
    RESULT_COMPLETED,
    RESULT_CANCELLED,
    RESULT_FAILED,
    RESULT_FAILED_AMBIGUOUS,
    RESULT_TIMED_OUT,
    RESULT_REJECTED,
})

# Result statuses that address a real backend dispatch.
DISPATCHED_STATUSES: frozenset[str] = frozenset({
    RESULT_COMPLETED,
    RESULT_CANCELLED,
    RESULT_TIMED_OUT,
})

_DEFAULT_MAX_RECORDS = 1000
_DEFAULT_MAX_AGE_SECONDS = 86_400


@dataclass(frozen=True)
class ExecutedTarget:
    """The target actually addressed by one execution attempt (audit form).

    A serialization subset of the route target: the executable-target
    reference plus the exact physical model identity. The coordinator never
    substitutes a target, so ``executed_target`` equals ``selected_target``
    whenever dispatch happened at all; both fields stay in the record so a
    future contract extension cannot silently blur the distinction D-043
    freezes.
    """

    resource_id: str
    provider: str
    model: str
    variant: str

    def __post_init__(self) -> None:
        _ = v_safe_id(self.resource_id, "executed_target.resource_id")
        _ = v_safe_id(self.provider, "executed_target.provider")
        _ = v_safe_id(self.model, "executed_target.model")
        _ = v_safe_id(self.variant, "executed_target.variant")

    @classmethod
    def from_dict(cls, d: object) -> "ExecutedTarget":
        dd = exact_shape(
            d,
            ("resource_id", "provider", "model", "variant"),
            (),
            "executed_target",
        )
        return cls(
            resource_id=v_safe_id(dd["resource_id"], "executed_target.resource_id"),
            provider=v_safe_id(dd["provider"], "executed_target.provider"),
            model=v_safe_id(dd["model"], "executed_target.model"),
            variant=v_safe_id(dd["variant"], "executed_target.variant"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "resource_id": self.resource_id,
            "provider": self.provider,
            "model": self.model,
            "variant": self.variant,
        }


@dataclass(frozen=True)
class AuditRecord:
    """One executed (or rejected) request's audit record (D-043 field set).

    ``decision_id`` is the routing provenance: the route decision's id for
    a routed request, the pinned reference's carried decision id for a
    pinned execution, and ``None`` only when admission never produced a
    routing outcome (validation/limit rejection before routing).

    ``registry_revision``/``registry_generated_at`` are the state snapshot
    identity/version at admission time. ``selected_target`` is present
    from admission approval onward; ``executed_target`` only when dispatch
    actually started. ``call_count`` is the number of internal provider
    calls the execution produced (honest multi-call fan-out, D-043).

    There is deliberately no field that could carry prompt/response
    content: free text is not representable in this record.
    """

    request_id: str
    started_at: str
    ended_at: str
    result_status: str
    reason_codes: tuple[str, ...]
    client_id: str
    decision_id: str | None = None
    routing_profile: str | None = None
    routing_policy_version: int | None = None
    registry_revision: int | None = None
    registry_generated_at: str | None = None
    selected_target: ExecutedTarget | None = None
    executed_target: ExecutedTarget | None = None
    adapter_name: str | None = None
    adapter_version: str | None = None
    provider_reported_usage: UsageTokens | None = None
    estimated_usage: UsageTokens | None = None
    call_count: int = 0
    schema_version: int = AUDIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _ = v_safe_id(self.request_id, "audit_record.request_id")
        _ = v_safe_id(self.client_id, "audit_record.client_id")
        _ = v_canonical_ts(self.started_at, "audit_record.started_at")
        _ = v_canonical_ts(self.ended_at, "audit_record.ended_at")
        _ = v_enum(self.result_status, RESULT_STATUSES, "audit_record.result_status")
        codes = self.reason_codes
        normalized = tuple(
            sorted({v_safe_id(code, "audit_record.reason_codes") for code in codes})
        )
        object.__setattr__(self, "reason_codes", normalized)
        if self.decision_id is not None:
            _ = v_safe_id(self.decision_id, "audit_record.decision_id")
        if self.routing_profile is not None:
            _ = v_safe_id(self.routing_profile, "audit_record.routing_profile")
        if self.routing_policy_version is not None:
            _ = v_int(
                self.routing_policy_version,
                "audit_record.routing_policy_version",
                lo=1,
            )
        if self.registry_revision is not None:
            _ = v_int(self.registry_revision, "audit_record.registry_revision", lo=0)
        if self.registry_generated_at is not None:
            _ = v_canonical_ts(
                self.registry_generated_at, "audit_record.registry_generated_at"
            )
        if self.selected_target is not None:
            _ = v_instance(
                self.selected_target, ExecutedTarget, "audit_record.selected_target"
            )
        if self.executed_target is not None:
            _ = v_instance(
                self.executed_target, ExecutedTarget, "audit_record.executed_target"
            )
        if self.adapter_name is not None:
            _ = v_safe_id(self.adapter_name, "audit_record.adapter_name")
        if self.adapter_version is not None:
            _ = v_text(
                self.adapter_version, "audit_record.adapter_version", max_len=128
            )
        if self.provider_reported_usage is not None:
            _ = v_instance(
                self.provider_reported_usage,
                UsageTokens,
                "audit_record.provider_reported_usage",
            )
        if self.estimated_usage is not None:
            _ = v_instance(
                self.estimated_usage,
                UsageTokens,
                "audit_record.estimated_usage",
            )
        _ = v_int(self.call_count, "audit_record.call_count", lo=0)
        _ = v_int(self.schema_version, "audit_record.schema_version", lo=1)
        if self.schema_version != AUDIT_SCHEMA_VERSION:
            raise ValueError(
                f"audit_record.schema_version: expected {AUDIT_SCHEMA_VERSION}, "
                + f"got {self.schema_version!r}"
            )
        # Cross-field honesty invariants.
        if self.executed_target is not None and self.selected_target is None:
            raise ValueError(
                "audit_record: an executed target requires a selected target"
            )
        if self.executed_target is None and self.result_status in (
            RESULT_COMPLETED,
            RESULT_CANCELLED,
            RESULT_TIMED_OUT,
        ):
            raise ValueError(
                f"audit_record: result_status {self.result_status!r} requires "
                + "an executed target"
            )
        if self.result_status == RESULT_COMPLETED and self.call_count == 0:
            raise ValueError(
                "audit_record: a completed execution has at least one call"
            )
        if self.call_count > 0 and self.executed_target is None:
            raise ValueError(
                "audit_record: a call count requires an executed target"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "client_id": self.client_id,
            "decision_id": self.decision_id,
            "routing_profile": self.routing_profile,
            "routing_policy_version": self.routing_policy_version,
            "registry_revision": self.registry_revision,
            "registry_generated_at": self.registry_generated_at,
            "selected_target": (
                None
                if self.selected_target is None
                else self.selected_target.to_dict()
            ),
            "executed_target": (
                None
                if self.executed_target is None
                else self.executed_target.to_dict()
            ),
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "result_status": self.result_status,
            "reason_codes": list(self.reason_codes),
            "provider_reported_usage": (
                None
                if self.provider_reported_usage is None
                else self.provider_reported_usage.to_dict()
            ),
            "estimated_usage": (
                None
                if self.estimated_usage is None
                else self.estimated_usage.to_dict()
            ),
            "call_count": self.call_count,
        }


class AuditSink(Protocol):
    """The injectable destination seam for audit records."""

    def append(self, record: AuditRecord) -> None: ...


class BoundedAuditTrail:
    """Thread-safe bounded in-memory audit trail (default sink).

    Retention is bounded both by count and by age: ``append`` stores the
    record, drops the oldest beyond ``max_records`` and prunes records
    whose ``ended_at`` is older than ``max_age_seconds`` relative to the
    appended record's instant. Both bounds have frozen non-zero defaults;
    nothing here writes to disk (D-041: in-memory is valid for this slice;
    the durable store, if M09 introduces it, arrives through the
    :class:`AuditSink` seam).
    """

    def __init__(
        self,
        *,
        max_records: int = _DEFAULT_MAX_RECORDS,
        max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        _ = v_int(max_records, "bounded_audit_trail.max_records", lo=1)
        _ = v_int(max_age_seconds, "bounded_audit_trail.max_age_seconds", lo=1)
        self._max_records: int = max_records
        self._max_age_seconds: int = max_age_seconds
        self._records: list[AuditRecord] = []
        self._lock: threading.Lock = threading.Lock()

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._max_records:
                del self._records[: len(self._records) - self._max_records]
            self._prune_locked()

    def _prune_locked(self) -> None:
        newest = self._records[-1]
        cutoff = _instant_minus_seconds(newest.ended_at, self._max_age_seconds)
        if cutoff is None:
            return
        self._records = [
            record for record in self._records if record.ended_at >= cutoff
        ]

    def snapshot(self) -> tuple[AuditRecord, ...]:
        """Deterministic oldest-first copy of the retained records."""
        with self._lock:
            return tuple(self._records)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


def _instant_minus_seconds(canonical_ts: str, seconds: int) -> str | None:
    """One canonical timestamp shifted back by ``seconds``, or ``None``.

    A parse failure returns ``None`` so pruning never rejects an
    already-validated record; construction validated the format, so this
    is defensive only.
    """
    try:
        moment = datetime.fromisoformat(canonical_ts[:-1] + "+00:00")
    except ValueError:
        return None
    shifted = moment - timedelta(seconds=seconds)
    return (
        shifted.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "DISPATCHED_STATUSES",
    "RESULT_CANCELLED",
    "RESULT_COMPLETED",
    "RESULT_FAILED",
    "RESULT_FAILED_AMBIGUOUS",
    "RESULT_REJECTED",
    "RESULT_STATUSES",
    "RESULT_TIMED_OUT",
    "AuditRecord",
    "AuditSink",
    "BoundedAuditTrail",
    "ExecutedTarget",
]
