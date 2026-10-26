"""Explicit-input MI consumer, not automatic admission or calibration.

Uses the optional, exact-pinned public MI package's own validators/projection.
Only explanation metadata is projected; bootstrap properties/ratings are intact.
No network, producer, credential access, current-pointer polling or disk writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import final

from model_intelligence.contract import (
    Applicability,
    PUBLIC_PAYLOAD_SCHEMAS,
    PublicEvidence,
    decode_public_evidence,
)
from model_intelligence.evidence import Model, NativeLimit, instant
from model_intelligence.projection import effective_at
from model_intelligence.publication import CutReference, decode_cut
from model_intelligence.utilization import decode_decision_utilization, encode_utilization

from .selection_types import EvidenceRef, ModelCatalog, ModelCatalogEntry, ModelIdentity

def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class ModelBinding:
    """Caller-reviewed exact association, never inferred from aliases or variants."""

    identity: ModelIdentity
    subject: Model
    dimension: str
    meaning: str
    basis: str

    def matches(self, entry: ModelCatalogEntry, scope: Applicability) -> bool:
        configuration = self.subject.configuration
        return (
            entry.identity == self.identity
            and entry.identity.provider == self.subject.provider_id
            and entry.identity.model == self.subject.provider_model_id
            and configuration is not None
            and "reasoning_effort" in dict(configuration)
            and dict(configuration)["reasoning_effort"] == entry.reasoning_effort
            and entry.reasoning_effort is not None
            and self.subject.channel is not None
            and self.subject.channel == scope.channel
            and configuration == scope.configuration
        )


@dataclass(frozen=True)
class FrozenCut:
    data: bytes
    reference: CutReference
    public: PublicEvidence


@final
class MIConsumer:
    """Atomic validated input slot. Rejected updates leave the prior cut unchanged.

    The slot is not an admission decision. Every evaluation rechecks time and
    applicability; retaining bytes does not make them fresh for new decisions.
    Retained receipts, not this mutable slot, reproduce earlier decisions.
    """

    def __init__(
        self,
        *,
        expected_scope: Applicability,
        bindings: tuple[ModelBinding, ...],
        max_bytes: int,
        supported_schemas: frozenset[str] = PUBLIC_PAYLOAD_SCHEMAS,
    ) -> None:
        if type(max_bytes) is not int or not 0 < max_bytes <= 16 * 1024 * 1024:
            raise ValueError("mi_invalid_size_policy")
        if len({binding.identity for binding in bindings}) != len(bindings):
            raise ValueError("mi_duplicate_binding")
        self._scope = expected_scope
        self._bindings = bindings
        self._max_bytes = max_bytes
        self._schemas = supported_schemas
        self._current: FrozenCut | None = None
        self._lock = RLock()

    def adopt(self, data: bytes, expected: CutReference) -> None:
        """The expected digest/scope come from caller trust, not this payload."""
        try:
            if type(data) is not bytes:
                raise ValueError("immutable-bytes-required")
            public = decode_public_evidence(
                data, expected, max_bytes=self._max_bytes,
                supported_payload_schemas=self._schemas,
            )
            if public.applicability != self._scope:
                raise ValueError("scope")
            candidate = FrozenCut(data, expected, public)
            with self._lock:
                if self._current is not None:
                    self._current.public.check_update(public)
                self._current = candidate
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
            # Never reflect untrusted fields, decoder errors or filesystem paths.
            raise ValueError("mi_publication_rejected") from None

    def load(self, path: Path, expected: CutReference) -> None:
        try:
            with path.open("rb") as stream:
                data = stream.read(self._max_bytes + 1)
        except OSError:
            raise ValueError("mi_publication_unavailable") from None
        self.adopt(data, expected)

    def capture(self) -> tuple[FrozenCut | None, tuple[ModelBinding, ...]]:
        with self._lock:
            return self._current, self._bindings


def explained_catalog(
    catalog: ModelCatalog,
    captured: tuple[FrozenCut | None, tuple[ModelBinding, ...]],
    at: datetime,
) -> ModelCatalog:
    """Use public evidence BEFORE selection, without changing selection authority.

    The exact existing catalog feeds the existing selector; only bounded display
    annotations change. Public advertised limits are NOT executable ceilings.
    Unknown, conflict, future, stale and withdrawal never supply positive facts.
    """
    cut, bindings = captured
    if cut is None:
        return catalog
    at = instant(at)
    public = cut.public
    view = effective_at(public.evidence, at)
    conflicted = {identity for conflict in view.conflicts for identity in conflict.claim_ids}
    notices = public.active_notices(at)
    withdrawn = {target for notice in notices for target in notice.targets}
    critical = {target for notice in notices if notice.reported_critical is True for target in notice.targets}
    observed = {item.observation_id: item for item in public.evidence.observations}
    frame = decode_cut(
        cut.data, cut.reference, supported_payload_schemas=PUBLIC_PAYLOAD_SCHEMAS,
        max_bytes=len(cut.data),
    )
    entries: list[ModelCatalogEntry] = []
    for entry in catalog.entries:
        binding = next((item for item in bindings if item.identity == entry.identity), None)
        if binding is None:
            entries.append(entry)
            continue
        label = "unsupported"
        if binding.matches(entry, public.applicability):
            claims = tuple(
                item for item in view.models
                if item.subject == binding.subject and isinstance(item.payload, NativeLimit)
                and item.payload.dimension == binding.dimension
                and item.payload.meaning == binding.meaning
                and item.payload.basis == binding.basis
            )
            if frame.produced_at > at:
                label = "future"
            elif any(f"statement:{item.claim_id}" in critical for item in claims):
                label = "critical withdrawal"
            elif any(f"statement:{item.claim_id}" in withdrawn for item in claims):
                label = "withdrawn"
            elif any(item.claim_id in conflicted for item in claims):
                label = "conflict"
            elif not claims:
                label = "unknown"
            else:
                origins = tuple(observed[item.observation_id] for item in claims)
                if any(
                    item.retrieved_at > at
                    or (item.observed_at is not None and item.observed_at > at)
                    or (item.published_at is not None and item.published_at > at)
                    for item in origins
                ):
                    label = "future"
                elif any(item.stale(at) for item in origins):
                    label = "stale"
                elif any(item.fresh_until is None for item in origins):
                    label = "freshness unknown"
                elif any(item.distribution != "public" for item in origins):
                    label = "rights unknown"
                else:
                    values = tuple(item.payload for item in claims if isinstance(item.payload, NativeLimit))
                    if all(
                        value.unit == "tokens"
                        and value.meaning in {"input-allowance", "output-allowance"}
                        and value.assertion != "unknown"
                        and value.amount is not None
                        and Decimal(0) < value.amount <= Decimal(1_000_000_000)
                        and value.amount == value.amount.to_integral_value()
                        for value in values
                    ):
                        amount = values[0].amount
                        assert amount is not None
                        label = f"public {binding.meaning.split('-')[0]} {int(amount)} tokens"
                    else:
                        label = "unknown or unsupported"
        # No source-provided prose/identifiers are rendered or treated as authority.
        entries.append(replace(entry, display_name=f"{entry.display_name[:64]} [MI {label}]"))
    return replace(catalog, entries=tuple(entries))


@dataclass(frozen=True)
class MIUse:
    """Immutable local artifacts; callers retain these bytes, never just a digest."""

    cut: FrozenCut | None
    evaluated_at: datetime
    context: bytes
    decision: bytes
    utilization: bytes
    reference: EvidenceRef

    def envelope(self, decision: dict[str, object]) -> dict[str, object]:
        if self.cut is not None:
            retained = decode_public_evidence(
                self.cut.data, self.cut.reference, max_bytes=len(self.cut.data),
            )
            if retained != self.cut.public:
                raise ValueError("mi_retained_cut_mismatch")
        result: dict[str, object] = {
            "schema_version": 1,
            "decision": decision,
            "mi_utilization": self.reference.to_dict(),
        }
        _ = decode_decision_utilization(
            self.utilization, response=canonical(result),
            expected_cut=self.cut.reference if self.cut is not None else None,
            evaluated_at=self.evaluated_at, decision=self.decision, context=self.context,
            max_bytes=max(len(self.utilization), len(canonical(result)), len(self.decision)),
        )
        return result


def bind_use(
    decision: dict[str, object],
    captured: tuple[FrozenCut | None, tuple[ModelBinding, ...]],
    *,
    at: datetime,
    context: bytes,
) -> MIUse:
    data = canonical(decision)
    cut, _bindings = captured
    manifest, reference = encode_utilization(
        cut.reference if cut is not None else None,
        evaluated_at=at, decision=data, context=context,
    )
    return MIUse(cut, at, context, data, manifest, EvidenceRef.from_dict(reference))
