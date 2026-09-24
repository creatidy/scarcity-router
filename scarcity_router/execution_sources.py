"""Execution sources: server-side adoption of discovered inventory (D-053).

The SOURCE is the administrator's grant: one configured, independently
authenticated execution source bound to one worker. The worker discovers
what its runtime can execute and reports a bounded ModelInventory; this
module owns the SERVER half of that contract:

- **Classification** — each discovered slug is classified against the
  reviewed track registry. Unclassified slugs stay DISCOVERED (visible,
  never routable); restricted tracks (Daybreak Blue) stay RESTRICTED
  (visible, never materialized as resources).
- **Conservative adoption** — an adopted model becomes an exact derived
  resource (``<source_id>:<slug>``) only through the D-053 gates: known
  standard track, approved floor, source authenticated, administrator
  adoption policy permitting. Capability inheritance is ONLY the track
  floor, expressed as derived catalog entries whose hard properties and
  capacity applicability stay honestly UNKNOWN.
- **Deterministic lifecycle** — the source owns it: a model absent from
  an authenticated inventory marks its resource unavailable; after
  ``RETIRE_AFTER_MISSES`` consecutive authenticated observations without
  it, the resource is retired (deregistered). Audit history is never
  rewritten; a reappearing model re-materializes.

No credentials ever appear here: the inventory contract cannot carry
them and the source configuration references only ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model_inventory import (
    SOURCE_ID_MAX_LENGTH,
    ModelInventoryReport,
    SourceInventory,
    is_source_resource_id,
    source_resource_id,
)
from .model_tracks import TrackRegistry
from .resource_state import (
    ExecutionCapabilities,
    ResourceRegistration,
    ResourceIdentity,
)
from .selection_types import (
    CAPABILITY_DIMENSIONS,
    CapabilityAssessment,
    CapabilityAssessments,
    EvidenceRef,
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
    REASONING_EFFORTS,
)
from .server_config import SourceConfig


#: A derived resource is retired after this many consecutive
#: AUTHENTICATED inventory observations without its model. One miss is
#: not enough (a provider-side listing glitch must not retire capacity);
#: the counter is deterministic and bounded.
RETIRE_AFTER_MISSES = 3

#: Closed adoption states for the source view.
ADOPTION_STATES: tuple[str, ...] = (
    "discovered",
    "classified",
    "routable",
    "restricted",
    "retired",
)

#: Registration-owned capability facts of the CODEX EXECUTION SURFACE
#: (D-053): derived resources served through this surface inherit exactly
#: what the reviewed M06 evidence supports (docs/codex-adapter-stage1-
#: evidence.md) — the 272k thread context limit and reasoning controls —
#: and nothing about any specific model. Tool calls stay false on the
#: stable surface (client tools return to clients, D-043); the D-043
#: matrix remains the per-request admission authority.
CODEX_SURFACE_CAPABILITIES: dict[str, object] = {
    "context_limit_tokens": 272_000,
    "streaming": True,
    "tool_calls": False,
    "structured_output": True,
    "reasoning_controls": True,
    "usage_reporting": True,
    "cancellation": True,
}

#: Closed reason codes for non-adopted models (audit + UX remediations).
ADOPTION_EXCLUSION_CODES: frozenset[str] = frozenset(
    {
        "unclassified_track",
        "restricted_track",
        "track_not_allowed",
        "adoption_disabled",
        "source_not_authenticated",
        "effort_not_reported",
    }
)

_MAX_SLUG_LENGTH = 40


class SourceAdoptionError(ValueError):
    """An inventory document could not be adopted (fail closed)."""


#: Mirrors the M01 registration contract's required TTL for
#: worker-reported resources: bounded and honest (a source that stops
#: reporting goes stale on the normal registry path).
_DEFAULT_FRESHNESS_SECONDS = 900


@dataclass(frozen=True)
class AdoptionDecision:
    """One discovered model's classification/adoption outcome (auditable)."""

    slug: str
    state: str
    track_id: str | None
    reason: str | None  # ADOPTION_EXCLUSION_CODES member when not routable
    resource_id: str | None
    efforts: tuple[str, ...] = ()


@dataclass
class _SourceState:
    config: SourceConfig
    inventory: SourceInventory | None = None
    #: Slugs adopted routable as of the last authenticated observation.
    routable: set[str] = field(default_factory=set)
    #: Adopted models still held registered: slug -> (track_id, efforts).
    #: An adopted model absent from the LATEST authenticated observation
    #: stays here (routing sees it as stale/unavailable — never usable)
    #: until the miss counter retires it; audit identity is never
    #: erased early and a reappearing model re-materializes in place.
    adopted: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    miss_counts: dict[str, int] = field(default_factory=dict)
    retired: dict[str, str] = field(default_factory=dict)  # slug -> track_id
    decisions: tuple[AdoptionDecision, ...] = ()


class SourceRegistry:
    """The server's derived-resource state for every configured source.

    In-memory derived state (D-041/D-053): the source itself is the
    origin of truth and a restart re-derives from the next inventory
    report. All mutations flow through :meth:`apply_inventory`; reads
    are the projections the rest of the server consumes (registrations,
    catalog entries, ownership, adapter ids, the source view).
    """

    def __init__(
        self,
        *,
        track_registry: TrackRegistry,
        freshness_ttl_seconds: int = _DEFAULT_FRESHNESS_SECONDS,
    ) -> None:
        self._tracks: TrackRegistry = track_registry
        self._ttl: int = freshness_ttl_seconds
        self._sources: dict[str, _SourceState] = {}

    # ── configuration coupling ────────────────────────────────────────

    def sync_configuration(self, sources: tuple[SourceConfig, ...]) -> None:
        """Adopt the current administrator source configuration.

        Sources removed from configuration lose their derived state
        immediately (the grant is gone); surviving sources keep their
        inventory/retirement history.
        """
        kept: dict[str, _SourceState] = {}
        for config in sources:
            existing = self._sources.get(config.source_id)
            kept[config.source_id] = (
                existing if existing is not None else _SourceState(config=config)
            )
            kept[config.source_id].config = config
        self._sources = kept

    def has_source(self, source_id: str) -> bool:
        return source_id in self._sources

    # ── the adoption path ─────────────────────────────────────────────

    def apply_inventory(self, inventory: ModelInventoryReport) -> tuple[AdoptionDecision, ...]:
        """Adopt one worker's inventory report (validated by the caller).

        Every configured source named by the report is updated; unknown
        source ids in the report are ignored (a worker reporting sources
        this deployment does not configure has no grant here — the
        ownership check makes them unroutable regardless).
        """
        decisions: list[AdoptionDecision] = []
        for source_inventory in inventory.sources:
            state = self._sources.get(source_inventory.source_id)
            if state is None:
                continue
            state.inventory = source_inventory
            decisions.extend(self._decide(state, source_inventory))
        return tuple(decisions)

    def _decide(
        self, state: _SourceState, source_inventory: SourceInventory
    ) -> tuple[AdoptionDecision, ...]:
        config = state.config
        decisions: list[AdoptionDecision] = []
        present: set[str] = set()
        authenticated = source_inventory.auth_state == "authenticated"
        routable_now: set[str] = set()
        for model in source_inventory.models:
            slug = model.slug
            present.add(slug)
            track = self._tracks.classify(config.provider(), slug)
            if track is None:
                decisions.append(
                    AdoptionDecision(slug, "discovered", None, "unclassified_track", None, model.reasoning_efforts)
                )
                continue
            if track.classification == "restricted":
                decisions.append(
                    AdoptionDecision(slug, "restricted", track.track_id(), "restricted_track", None, model.reasoning_efforts)
                )
                continue
            if not config.auto_adopt:
                decisions.append(
                    AdoptionDecision(slug, "classified", track.track_id(), "adoption_disabled", None, model.reasoning_efforts)
                )
                continue
            if config.allowed_tracks and track.track_id() not in config.allowed_tracks:
                decisions.append(
                    AdoptionDecision(slug, "classified", track.track_id(), "track_not_allowed", None, model.reasoning_efforts)
                )
                continue
            if not authenticated:
                decisions.append(
                    AdoptionDecision(slug, "classified", track.track_id(), "source_not_authenticated", None, model.reasoning_efforts)
                )
                continue
            if track.floor is None:  # structural: standard tracks carry floors
                decisions.append(
                    AdoptionDecision(slug, "classified", track.track_id(), "unclassified_track", None, model.reasoning_efforts)
                )
                continue
            if not any(effort in REASONING_EFFORTS for effort in model.reasoning_efforts):
                decisions.append(
                    AdoptionDecision(slug, "classified", track.track_id(), "effort_not_reported", None, model.reasoning_efforts)
                )
                continue
            routable_now.add(slug)
            derived_id = _resource_id(config.source_id, slug)
            state.adopted[slug] = (track.track_id(), model.reasoning_efforts)
            decisions.append(
                AdoptionDecision(
                    slug,
                    "routable",
                    track.track_id(),
                    None,
                    derived_id,
                    model.reasoning_efforts,
                )
            )
        # Deterministic retirement (D-053 point 6): a previously routable
        # model absent from THIS authenticated observation accrues a miss;
        # RETIRE_AFTER_MISSES consecutive misses retire it. Unauthenticated
        # observations change nothing (the source cannot see the runtime).
        if authenticated:
            for slug in sorted(set(state.adopted) - present):
                misses = state.miss_counts.get(slug, 0) + 1
                if misses >= RETIRE_AFTER_MISSES:
                    track_id, _efforts = state.adopted.pop(slug, ("", ()))
                    _ = state.retired.setdefault(slug, track_id)
                    _ = state.miss_counts.pop(slug, None)
                else:
                    state.miss_counts[slug] = misses
            for slug in present & set(state.adopted):
                _ = state.miss_counts.pop(slug, None)
                _ = state.retired.pop(slug, None)
            state.routable = routable_now
        state.decisions = tuple(decisions)
        return tuple(decisions)

    # ── projections consumed by composition ───────────────────────────

    def derived_registrations(self) -> tuple[ResourceRegistration, ...]:
        """The adopted exact resources (deterministic order).

        Registration follows ADOPTION, not the latest listing: a model
        absent from the newest authenticated inventory stays registered
        (its observation goes stale on the normal M01 path, so routing
        treats it as unavailable — never usable, never substituted) until
        the deterministic retirement counter removes it.
        """
        registrations: list[ResourceRegistration] = []
        for source_id in sorted(self._sources):
            state = self._sources[source_id]
            if state.inventory is None:
                continue
            for slug in sorted(state.adopted):
                identity = ResourceIdentity(
                    resource_id=_resource_id(source_id, slug),
                    channel="worker_bridged",
                    provider=state.config.provider(),
                    model=slug,
                    entitlement=state.config.entitlement,
                    quota_pool_ids=(state.config.pool_id(),),
                )
                registrations.append(
                    ResourceRegistration(
                        identity=identity,
                        freshness_ttl_seconds=self._ttl,
                        # Registration-owned capability facts of the CODEX
                        # EXECUTION SURFACE (evidenced, model-independent):
                        # the D-043 matrix stays the per-request authority.
                        capabilities=ExecutionCapabilities.from_dict(
                            dict(CODEX_SURFACE_CAPABILITIES)
                        ),
                    )
                )
        return tuple(registrations)

    def derived_catalog_entries(self, base: ModelCatalog) -> ModelCatalog:
        """The catalog view with track-floor entries for adopted models.

        Exact reviewed catalog entries always win; the floor-derived
        entries fill ONLY the (model, effort) pairs the built-in catalog
        does not calibrate. Hard properties and capacity applicability
        stay UNKNOWN — a floor is a capability floor, not evidence about
        context limits or quota consumption.
        """
        extra: list[ModelCatalogEntry] = []
        existing = {
            (entry.identity.provider, entry.identity.model, entry.identity.variant)
            for entry in base.entries
        }
        for source_id in sorted(self._sources):
            state = self._sources[source_id]
            if state.inventory is None:
                continue
            for slug in sorted(state.adopted):
                track_id, efforts = state.adopted[slug]
                track = self._tracks.track_by_id(
                    state.config.provider(), track_id.split("/", 1)[1]
                )
                if track is None or track.floor is None:
                    continue
                for effort in [e for e in efforts if e in REASONING_EFFORTS]:
                    key = (state.config.provider(), slug, effort)
                    if key in existing:
                        continue
                    existing.add(key)
                    extra.append(
                        _floor_entry(track.track_id(), track.floor, slug, effort, state.config)
                    )
        if not extra:
            return base
        return ModelCatalog(
            catalog_version=base.catalog_version,
            updated_on=base.updated_on,
            entries=base.entries + tuple(extra),
        )

    def owner_of(self, resource_id: str) -> str | None:
        """The owning worker of a derived resource id, or None."""
        parsed = _split_resource_id(resource_id)
        if parsed is None:
            return None
        source_id, _slug = parsed
        state = self._sources.get(source_id)
        if state is None:
            return None
        return state.config.worker_id

    def adapter_of(self, resource_id: str) -> str | None:
        """The worker-local adapter INSTANCE id serving a derived resource."""
        parsed = _split_resource_id(resource_id)
        if parsed is None:
            return None
        source_id, _slug = parsed
        if source_id not in self._sources:
            return None
        return f"codex:{source_id}"

    def source_view(self) -> tuple[dict[str, object], ...]:
        """The read-only per-source view (UI/diagnostics; no raw payloads)."""
        views: list[dict[str, object]] = []
        for source_id in sorted(self._sources):
            state = self._sources[source_id]
            inventory = state.inventory
            routable = restricted = errors = 0
            models: list[dict[str, object]] = []
            if inventory is not None:
                for decision in state.decisions:
                    if decision.state == "routable":
                        routable += 1
                    elif decision.state == "restricted":
                        restricted += 1
                    elif decision.state == "discovered":
                        errors += 1
                for model in inventory.models:
                    models.append(
                        {
                            "slug": model.slug,
                            "state": _state_for(state, model.slug),
                            "reasoning_efforts": list(model.reasoning_efforts),
                        }
                    )
            views.append(
                {
                    "source_id": source_id,
                    "label": state.config.label or source_id,
                    "kind": state.config.kind,
                    "worker_id": state.config.worker_id,
                    "connected": inventory is not None,
                    "source_authenticated": (
                        inventory.auth_state if inventory is not None else "unverified"
                    ),
                    "observed_at": inventory.observed_at if inventory is not None else None,
                    "runtime_version": inventory.runtime_version if inventory is not None else None,
                    "models": models,
                    "routable": routable,
                    "restricted": restricted,
                    "errors": errors,
                    "retired": sorted(state.retired),
                }
            )
        return tuple(views)


def _state_for(state: _SourceState, slug: str) -> str:
    decision = _decision_for(state.decisions, slug)
    if decision is not None:
        return decision.state
    if slug in state.retired:
        return "retired"
    return "discovered"


def _decision_for(
    decisions: tuple[AdoptionDecision, ...], slug: str
) -> AdoptionDecision | None:
    for decision in decisions:
        if decision.slug == slug:
            return decision
    return None


def _resource_id(source_id: str, slug: str) -> str:
    if len(slug) > _MAX_SLUG_LENGTH:
        # Deterministic and honest: an over-long slug stays undiscovered
        # rather than being mangled into a different identity.
        raise SourceAdoptionError(
            f"discovered slug {slug!r} exceeds {_MAX_SLUG_LENGTH} chars"
        )
    return source_resource_id(source_id, slug)


def _split_resource_id(resource_id: str) -> tuple[str, str] | None:
    head, sep, tail = resource_id.partition(":")
    if not sep or not head or not tail or len(head) > SOURCE_ID_MAX_LENGTH:
        return None
    return head, tail


def _floor_entry(
    track_id: str,
    floor: object,
    slug: str,
    effort: str,
    config: SourceConfig,
) -> ModelCatalogEntry:
    """One conservative track-floor catalog entry (D-053 point 5)."""
    from .model_tracks import TrackFloor

    assert isinstance(floor, TrackFloor)
    evidence = (
        EvidenceRef(
            source="model-tracks.json",
            identifier=track_id,
            version="1",
            date=floor.assessed_on,
        ),
    )
    assessments: dict[str, CapabilityAssessment] = {
        dim: CapabilityAssessment(
            rating=floor.ratings[dim],
            evidence=evidence,
            confidence="low",
            assessed_on=floor.assessed_on,
            rationale=f"track floor for {track_id}; derived, not exact-model evidence",
        )
        for dim in CAPABILITY_DIMENSIONS
    }
    _ = config
    continuity = floor.hard_properties_continuity or {}
    return ModelCatalogEntry(
        identity=ModelIdentity(provider="openai", model=slug, variant=effort),
        display_name=slug,
        # supports_reasoning_mode is EVIDENCED, not inherited: the entry
        # exists because the runtime itself advertises this reasoning
        # effort for this slug. Context/output tokens come from the
        # floor's OWNER-REVIEWED family-continuity assumption (the true
        # enforcement boundary is the runtime itself); vision and tool
        # support stay honestly unknown.
        hard_properties=ModelHardProperties(
            supports_reasoning_mode=True,
            input_context_tokens=continuity.get("input_context_tokens"),
            output_tokens=continuity.get("output_tokens"),
        ),
        capabilities=CapabilityAssessments(**assessments),
        capacity_bindings=None,
        reasoning_effort=effort,
        model_version_date=None,
        last_reviewed_on=floor.assessed_on,
    )


__all__ = [
    "ADOPTION_EXCLUSION_CODES",
    "is_source_resource_id",
    "ADOPTION_STATES",
    "AdoptionDecision",
    "RETIRE_AFTER_MISSES",
    "SourceAdoptionError",
    "SourceRegistry",
]
