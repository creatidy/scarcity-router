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
from .model_tracks import TrackRegistry, validate_catalog_effort_restriction
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

#: Lifetime cap on retained retirement history per source (Daybreak
#: finding 4): retired slugs beyond the cap drop the OLDEST entry first.
#: Audit records are immutable and are NOT the retention mechanism here —
#: this bounds only the in-memory bookkeeping.
MAX_RETAINED_RETIRED = 64

#: The catalog variant id of a plan-managed lane's single floor entry
#: (D-063). It is a LANE qualifier, never a reasoning effort: the entry
#: carries ``reasoning_effort=None`` and ``supports_reasoning_mode=False``,
#: so an effort-bearing request can never resolve onto the lane (the
#: gateway rejects it as an unsupported effort — exact-identity preserved).
PLAN_LANE_VARIANT = "plan"

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
#: what the reviewed M06 evidence supports and nothing about any specific
#: model. The D-043 matrix remains the per-request admission authority.
#:
#: - ``context_limit_tokens`` — 272000 is the owner-reviewed registration
#:   fact of the 2026-09-24 sources program (D-053). Re-evidenced
#:   2026-09-28 (issue #136): the CURRENT runtime
#:   (``codex-cli 0.155.0-alpha.16.3``) exposes no context-window
#:   discovery on ``model/list`` or ``modelProvider/capabilities/read``
#:   and current official documentation publishes no context-window
#:   number, so the figure could NOT be re-established from provider
#:   surfaces; it is retained exactly because it is the administrator's
#:   registered fact, not because current docs confirm it. The runtime's
#:   own ``contextWindowExceeded`` failure signal stays the enforcement
#:   backstop. (The original M06 evidence doc never recorded the number —
#:   a provenance gap now recorded here and in the evidence doc.)
#: - ``output_limit_control: False`` — re-evidenced 2026-09-28 (issue
#:   #136): the current app-server turn contract carries NO output-token
#:   control (``TurnStartParams`` has no such field; candidate fields are
#:   silently ignored — the protocol parses unknown fields leniently — and
#:   no experimental gate names one). An explicit client output limit can
#:   never be enforced on this channel; the #136 coordinator normalization
#:   rule governs.
#: - ``tool_calls: False`` — client tools return to clients (D-043); the
#:   experimental dynamic-tools round trip is #137's investigation scope
#:   and stays unaudited here.
CODEX_SURFACE_CAPABILITIES: dict[str, object] = {
    "context_limit_tokens": 272_000,
    "streaming": True,
    "tool_calls": False,
    "structured_output": True,
    "reasoning_controls": True,
    "usage_reporting": True,
    "cancellation": True,
    "output_limit_control": False,
}

#: Registration-owned capability facts of the ZCODE EXECUTION SURFACE
#: (D-061, evidence 2026-09-28, CLI v3.14.3 / bundle 0.16.9): derived
#: resources served through this surface inherit exactly what the dated
#: evidence supports and nothing about any specific model. The D-043
#: matrix remains the per-request admission authority.
#:
#: - ``context_limit_tokens: 1_000_000`` — the OWNER-REVIEWED
#:   registration fact of the 2026-09-29 remediation (D-063): no
#:   context ceiling is published on any supported ZCode surface, so
#:   the lane carries the reviewed plan-family calibration minimum
#:   (model-catalog.json: glm-5.3 and glm-5.3-flash both calibrate
#:   1_000_000 input tokens) exactly the way the codex surface carries
#:   its owner-reviewed 272_000 registration fact — a bounded
#:   admission ceiling, never a per-model claim; the runtime remains
#:   the enforcement backstop. ``output_limit_tokens: 128_000`` — the
#:   same reviewed family output calibration; ``output_limit_control``
#:   stays False (no honored explicit-limit control exists, so the
#:   #136 normalization governs).
#: - ``streaming: False`` — the stream carries progress events only; the
#:   single evidenced answer surface is the terminal result line, so no
#:   incremental text delivery is evidenced.
#: - ``tool_calls: False`` — client tools return to clients (D-043); the
#:   ZCode agent's internal tools are its own under the explicit safe
#:   permission mode, never client tool calls.
#: - ``structured_output: False`` / ``reasoning_controls: False`` /
#:   ``output_limit_control: False`` — no supported control exists on the
#:   evidenced headless surface (no model/effort steering, no output
#:   schema, no output-token control).
#: - ``usage_reporting: None`` — the terminal result carries an OPTIONAL
#:   usage member whose internal field names are NOT evidenced; nothing
#:   is mapped, and unknown stays unknown (never invented telemetry).
ZCODE_SURFACE_CAPABILITIES: dict[str, object] = {
    "context_limit_tokens": 1_000_000,
    "streaming": False,
    "tool_calls": False,
    "structured_output": False,
    "reasoning_controls": False,
    "usage_reporting": None,
    "cancellation": True,
    "output_limit_tokens": 128_000,
    "output_limit_control": False,
}

#: Per-kind registration-owned capability facts (D-053 sources; D-061
#: added the zcode kind).
_SURFACE_CAPABILITIES_BY_KIND: dict[str, dict[str, object]] = {
    "codex_subscription": CODEX_SURFACE_CAPABILITIES,
    "zcode_subscription": ZCODE_SURFACE_CAPABILITIES,
}

#: The worker-local adapter id prefix each source kind's instances use
#: (``<prefix>:<source_id>``; the worker registers exactly these ids).
_ADAPTER_PREFIX_BY_KIND: dict[str, str] = {
    "codex_subscription": "codex",
    "zcode_subscription": "zcode",
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
        # D-063: a plan-managed lane adopts from a healthy-but-unverified
        # observation too. "unverified" is the honest steady state for a
        # source whose runtime exposes no non-inference auth probe (the
        # ZCode CLI): the probes prove the runtime, the vendor manages
        # sign-in visibility, and an actual sign-in failure surfaces as a
        # typed execution failure. Physical-model sources (codex) keep
        # the strict authenticated gate unchanged.
        healthy = source_inventory.auth_state in ("authenticated", "unverified")
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
            if track.plan_managed:
                if not healthy:
                    decisions.append(
                        AdoptionDecision(slug, "classified", track.track_id(), "source_not_authenticated", None, model.reasoning_efforts)
                    )
                    continue
            elif not authenticated:
                decisions.append(
                    AdoptionDecision(slug, "classified", track.track_id(), "source_not_authenticated", None, model.reasoning_efforts)
                )
                continue
            if track.floor is None:  # structural: standard tracks carry floors
                decisions.append(
                    AdoptionDecision(slug, "classified", track.track_id(), "unclassified_track", None, model.reasoning_efforts)
                )
                continue
            if not track.plan_managed and not any(
                effort in REASONING_EFFORTS for effort in model.reasoning_efforts
            ):
                # A physical-model source must advertise at least one
                # policy effort. A plan-managed lane is the deliberate
                # exception (D-063): its efforts are vendor-managed and
                # the effort-less report IS the honest lane.
                decisions.append(
                    AdoptionDecision(slug, "classified", track.track_id(), "effort_not_reported", None, model.reasoning_efforts)
                )
                continue
            routable_now.add(slug)
            if track.plan_managed:
                # One effort-less lane adoption: the derived resource
                # carries NO variant (there is no effort to bind) and the
                # resource id has no effort suffix.
                _ = _resource_id(config.source_id, slug)
                state.adopted[slug] = (track.track_id(), ())
                decisions.append(
                    AdoptionDecision(
                        slug,
                        "routable",
                        track.track_id(),
                        None,
                        _resource_id(config.source_id, slug),
                        model.reasoning_efforts,
                    )
                )
                continue
            policy_efforts = sorted(
                e for e in model.reasoning_efforts if e in REASONING_EFFORTS
            )
            # Daybreak blocker 2: validate EVERY per-effort derived id
            # BEFORE any adoption state is mutated — an over-long combo
            # fails the whole model closed instead of poisoning partial
            # state.
            for effort in policy_efforts:
                _ = _resource_id(config.source_id, slug, effort)
            state.adopted[slug] = (track.track_id(), model.reasoning_efforts)
            decisions.append(
                AdoptionDecision(
                    slug,
                    "routable",
                    track.track_id(),
                    None,
                    _resource_id(config.source_id, slug, policy_efforts[0]),
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
                    state.retired[slug] = track_id
                    _ = state.miss_counts.pop(slug, None)
                    # Bounded history (Daybreak finding 4): drop the OLDEST
                    # retired entry beyond the cap.
                    while len(state.retired) > MAX_RETAINED_RETIRED:
                        _ = state.retired.pop(next(iter(state.retired)))
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
                _track_id, efforts = state.adopted[slug]
                if not efforts:
                    # D-063: an effort-less adopted model is a plan-managed
                    # lane — ONE variant-less exact resource. No effort or
                    # physical-model qualifier is ever bound to it; a
                    # variant-qualified request can never select it
                    # (routing_core: a variant-less resource binds the
                    # model's calibrated variants, and the lane's single
                    # catalog entry is effort-less — an effort-bearing
                    # request is rejected before admission).
                    identity = ResourceIdentity(
                        resource_id=_resource_id(source_id, slug),
                        channel="worker_bridged",
                        provider=state.config.provider(),
                        model=slug,
                        variant=None,
                        entitlement=state.config.entitlement,
                        quota_pool_ids=(state.config.pool_id(),),
                    )
                    registrations.append(
                        ResourceRegistration(
                            identity=identity,
                            freshness_ttl_seconds=self._ttl,
                            capabilities=ExecutionCapabilities.from_dict(
                                dict(
                                    _SURFACE_CAPABILITIES_BY_KIND.get(
                                        state.config.kind,
                                        CODEX_SURFACE_CAPABILITIES,
                                    )
                                )
                            ),
                        )
                    )
                    continue
                # Daybreak finding 3: one exact resource per ADVERTISED
                # effort, variant-qualified, so a resource can never bind
                # a variant its own runtime did not advertise.
                for effort in sorted(e for e in efforts if e in REASONING_EFFORTS):
                    identity = ResourceIdentity(
                        resource_id=_resource_id(source_id, slug, effort),
                        channel="worker_bridged",
                        provider=state.config.provider(),
                        model=slug,
                        variant=effort,
                        entitlement=state.config.entitlement,
                        quota_pool_ids=(state.config.pool_id(),),
                    )
                    registrations.append(
                        ResourceRegistration(
                            identity=identity,
                            freshness_ttl_seconds=self._ttl,
                            # Registration-owned capability facts of THIS
                            # source kind's execution surface (evidenced,
                            # model-independent): the D-043 matrix stays
                            # the per-request authority.
                            capabilities=ExecutionCapabilities.from_dict(
                                dict(
                                    _SURFACE_CAPABILITIES_BY_KIND.get(
                                        state.config.kind,
                                        CODEX_SURFACE_CAPABILITIES,
                                    )
                                )
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
        context limits or quota consumption. D-054: a track marked
        ``max_only`` (light family) expands at ``max`` only, and only when
        the runtime itself reports a ``max`` effort — never invented.
        """
        validate_catalog_effort_restriction(base, self._tracks)
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
                if not efforts:
                    # D-063: a plan-managed lane gets ONE effort-less
                    # floor entry. Its variant is the lane qualifier
                    # (PLAN_LANE_VARIANT), its effort is None, and it
                    # never claims reasoning support — so an
                    # effort-bearing request can never resolve onto the
                    # lane, while a plain "model" request resolves to the
                    # single legal variant through the normal D-055
                    # resolution.
                    key = (state.config.provider(), slug, PLAN_LANE_VARIANT)
                    if key not in existing:
                        existing.add(key)
                        extra.append(
                            _lane_floor_entry(
                                track.track_id(), track.floor, slug, state.config
                            )
                        )
                    continue
                allowed = track.restrict_floor_efforts(tuple(efforts))
                for effort in [e for e in allowed if e in REASONING_EFFORTS]:
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
        state = self._sources.get(source_id)
        if state is None:
            return None
        prefix = _ADAPTER_PREFIX_BY_KIND.get(state.config.kind)
        if prefix is None:
            return None
        return f"{prefix}:{source_id}"

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


def _resource_id(source_id: str, slug: str, effort: str | None = None) -> str:
    if len(slug) > _MAX_SLUG_LENGTH:
        # Deterministic and honest: an over-long slug stays undiscovered
        # rather than being mangled into a different identity.
        raise SourceAdoptionError(
            f"discovered slug {slug!r} exceeds {_MAX_SLUG_LENGTH} chars"
        )
    return source_resource_id(source_id, slug, effort)


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
    continuity = floor.hard_properties_continuity or {}
    return ModelCatalogEntry(
        # The floor entry serves THIS source kind's provider — never a
        # hard-coded one (a zai-kind source's floor entry must be a zai
        # identity, exactly like the codex kind's openai one).
        identity=ModelIdentity(
            provider=config.provider(), model=slug, variant=effort
        ),
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


def _lane_floor_entry(
    track_id: str,
    floor: object,
    slug: str,
    config: SourceConfig,
) -> ModelCatalogEntry:
    """One effort-less plan-managed lane floor entry (D-063).

    The variant is the LANE qualifier (``PLAN_LANE_VARIANT``), never an
    effort: ``reasoning_effort`` stays None and reasoning support is
    evidenced-absent (the vendor manages model selection headless), so
    an effort-bearing request can never resolve onto the lane while a
    plain model request resolves to the single legal variant. Hard
    properties stay honestly UNKNOWN (no continuity assumption exists
    for an unverifiable physical model) and the floor ratings are the
    reviewed lane floor — the minimum of the scale, never a model claim.
    """
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
            confidence=floor.confidence,
            assessed_on=floor.assessed_on,
            rationale=(
                f"plan-managed lane floor for {track_id}; vendor-managed "
                + "physical model, never an exact-model claim (D-063)"
            ),
        )
        for dim in CAPABILITY_DIMENSIONS
    }
    continuity = floor.hard_properties_continuity or {}
    return ModelCatalogEntry(
        identity=ModelIdentity(
            provider=config.provider(), model=slug, variant=PLAN_LANE_VARIANT
        ),
        display_name=slug,
        # Context/output come from the floor's OWNER-REVIEWED
        # family-continuity assumption (the reviewed plan-family
        # calibration; the runtime is the enforcement boundary); reasoning
        # support stays evidenced-absent (no headless steering).
        hard_properties=ModelHardProperties(
            supports_reasoning_mode=False,
            input_context_tokens=continuity.get("input_context_tokens"),
            output_tokens=continuity.get("output_tokens"),
        ),
        capabilities=CapabilityAssessments(**assessments),
        capacity_bindings=None,
        reasoning_effort=None,
        model_version_date=None,
        last_reviewed_on=floor.assessed_on,
    )


__all__ = [
    "ADOPTION_EXCLUSION_CODES",
    "is_source_resource_id",
    "ADOPTION_STATES",
    "AdoptionDecision",
    "PLAN_LANE_VARIANT",
    "RETIRE_AFTER_MISSES",
    "SourceAdoptionError",
    "SourceRegistry",
]
