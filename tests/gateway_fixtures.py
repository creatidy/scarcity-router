"""Shared deterministic fixtures for the M03 gateway tests.

Everything here is synthetic and self-contained: no live providers, no
network beyond the loopback test listener, no subprocess, no wall-clock
reads (the clock is injected). The synthetic backend model identities are
fake but structurally valid; no real provider credential, account or
payload appears anywhere.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import override

from scarcity_router.capacity import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
)
from scarcity_router.eligibility import ExecutionEligibility
from scarcity_router.gateway_adapters import (
    CHUNK_FINISH,
    CHUNK_TEXT_DELTA,
    CHUNK_USAGE,
    AdapterAmbiguousError,
    AdapterCall,
    AdapterMessage,
    AdapterPermanentError,
    AdapterRegistry,
    AdapterResult,
    AdapterStreamChunk,
    AdapterTimeoutError,
    AdapterToolCall,
    CallObservation,
    ExecutionContext,
)
from scarcity_router.gateway_audit import AuditRecord, BoundedAuditTrail
from scarcity_router.gateway_contracts import (
    ClientKeyDirectory,
    GatewayLimits,
    UsageTokens,
)
from scarcity_router.gateway_coordinator import GatewayApplication, RoutingAliasTable
from scarcity_router.gateway_openai import ChatCompletionRequest
from scarcity_router.resource_state import (
    EXECUTION_CHANNELS,
    ExecutionCapabilities,
    QuotaFact,
    ResourceHealth,
    ResourceIdentity,
    ResourceRegistration,
    ResourceRegistry,
    ResourceStateSnapshot,
)
from scarcity_router.routing_core import (
    AdministratorConstraints,
    ClientAuthorization,
    ClientRoutingProfile,
    CompatibilityCell,
)
from scarcity_router.selector import SelectorPolicy, neutral_selector_policy
from scarcity_router.selection_types import (
    CapabilityAssessment,
    CapabilityAssessments,
    CapabilityMinima,
    CapacityScopeRef,
    EvidenceRef,
    HardConstraints,
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
    TaskProfileCatalog,
    TaskProfileDefinition,
    TaskRequirement,
)

# ── Deterministic instants ────────────────────────────────────────────────────

T_EVAL = datetime(2026, 9, 15, 12, 5, tzinfo=timezone.utc)
T_NOW = "2026-09-15T12:05:00.000Z"
T_OBS = "2026-09-15T12:04:00.000Z"

EVIDENCE = EvidenceRef(
    source="official_docs", identifier="synthetic://gateway-evidence", date="2026-09-01"
)

CLIENT_ID = "client-a"
CLIENT_KEY = "sk-sr-synthetic-client-key-000"


def fixed_clock() -> Callable[[], datetime]:
    """One fixed timezone-aware evaluation instant."""
    return lambda: T_EVAL


def canonical(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ── Catalog and profiles ──────────────────────────────────────────────────────


def _known(rating: int) -> CapabilityAssessment:
    return CapabilityAssessment(
        rating=rating,
        evidence=(EVIDENCE,),
        confidence="medium",
        assessed_on="2026-09-01",
        rationale="synthetic calibrated rating",
    )


def _capabilities(reasoning: int, coding: int, tool_use: int) -> CapabilityAssessments:
    unknown = CapabilityAssessment(rating=None)
    return CapabilityAssessments(
        reasoning=_known(reasoning),
        coding=_known(coding),
        scientific_methodological=unknown,
        writing_editorial=unknown,
        tool_use=_known(tool_use),
        translation_multilingual=unknown,
    )


def _entry(
    provider: str,
    model: str,
    variant: str,
    *,
    reasoning: int,
    coding: int,
    tool_use: int = 4,
    scope: str,
) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        identity=ModelIdentity(provider=provider, model=model, variant=variant),
        display_name=f"{model} {variant}",
        hard_properties=ModelHardProperties(
            input_context_tokens=272_000,
            output_tokens=128_000,
            supports_tool_use=True,
            supports_vision=False,
            supports_reasoning_mode=True,
        ),
        capabilities=_capabilities(reasoning, coding, tool_use),
        capacity_bindings=(CapacityScopeRef(provider=provider, scope_id=scope),),
        reasoning_effort=variant,
    )


def build_catalog() -> ModelCatalog:
    return ModelCatalog(
        catalog_version=1,
        updated_on="2026-09-01",
        entries=(
            _entry(
                "openai", "gpt-5.6-luna", "max", reasoning=4, coding=4, scope="codex"
            ),
            _entry(
                "openai",
                "gpt-5.6-luna",
                "medium",
                reasoning=3,
                coding=3,
                scope="codex",
            ),
            _entry(
                "zai", "glm-5.3", "high", reasoning=4, coding=4, scope="coding_plan"
            ),
        ),
    )


def build_profiles() -> TaskProfileCatalog:
    return TaskProfileCatalog(
        definitions=(
            TaskProfileDefinition(
                profile_id="gateway-core",
                requirement=TaskRequirement(
                    task_level="L2",
                    capability_minima=CapabilityMinima(reasoning=3, coding=3),
                    hard_constraints=HardConstraints(),
                ),
            ),
        )
    )


# ── Registry world ────────────────────────────────────────────────────────────

TTL = 300


def _identity(
    resource_id: str,
    channel: str,
    provider: str,
    model: str,
    *,
    variant: str | None = None,
    pools: tuple[str, ...] = (),
) -> ResourceIdentity:
    return ResourceIdentity(
        resource_id=resource_id,
        channel=channel,
        provider=provider,
        model=model,
        entitlement="subscription_included",
        variant=variant,
        quota_pool_ids=pools,
    )


def _observation(
    identity: ResourceIdentity, *, observed_at: str = T_OBS, status: str = "ok"
) -> ResourceStateSnapshot:
    diagnostics: tuple[CapacityDiagnostic, ...] = ()
    required = {
        "unavailable": "source_unavailable",
        "auth_required": "auth_required",
        "unsupported": "unsupported_source",
        "schema_changed": "schema_changed",
        "unknown": "telemetry_unknown",
    }
    if status != "ok":
        diagnostics = (CapacityDiagnostic(code=required[status]),)
    return ResourceStateSnapshot(
        schema_version=1,
        identity=identity,
        observed_at=observed_at,
        health=ResourceHealth(status=status, diagnostics=diagnostics),
        quota_facts=(
            QuotaFact(
                observation_class="provider_telemetry",
                window=CapacityWindow(
                    resource="tokens",
                    kind="five_hour",
                    scope_id=(
                        "codex" if identity.provider == "openai" else "coding_plan"
                    ),
                    duration_seconds=18_000,
                    used_percent=50,
                    remaining_percent=50,
                ),
            ),
        ),
        promotions=(),
    )


def _registration(
    identity: ResourceIdentity, *, context_limit: int | None = 272_000
) -> ResourceRegistration:
    return ResourceRegistration(
        identity=identity,
        freshness_ttl_seconds=TTL,
        capabilities=ExecutionCapabilities(context_limit_tokens=context_limit),
    )


def build_registry(*, with_worker: bool = False) -> ResourceRegistry:
    """The default healthy world: two server-direct surfaces, plus an
    optional worker-bridged surface bound to the same openai identity."""
    identities = [
        _identity("openai-http", "server_direct_http", "openai", "gpt-5.6-luna"),
        _identity(
            "zai-http", "server_direct_http", "zai", "glm-5.3", pools=("zai-shared",)
        ),
    ]
    if with_worker:
        identities.append(
            _identity("openai-worker", "worker_bridged", "openai", "gpt-5.6-luna")
        )
    registry = ResourceRegistry(clock=lambda: T_NOW)
    for identity in identities:
        registry.register(_registration(identity))
        registry.apply_snapshot(_observation(identity))
    return registry


# ── Capacity v3 snapshots and eligibility ─────────────────────────────────────


def build_capacity_snapshots() -> tuple[CapacitySnapshot, ...]:
    snapshots: list[CapacitySnapshot] = []
    for provider, scope in (("openai", "codex"), ("zai", "coding_plan")):
        snapshots.append(
            CapacitySnapshot(
                schema_version=3,
                provider=provider,
                source="synthetic_test",
                retrieved_at=T_NOW,
                status="ok",
                windows=(
                    CapacityWindow(
                        resource="tokens",
                        kind="five_hour",
                        scope_id=scope,
                        duration_seconds=18_000,
                        used_percent=50,
                        remaining_percent=50,
                        window_id=f"{provider}-five",
                    ),
                    CapacityWindow(
                        resource="tokens",
                        kind="weekly",
                        scope_id=scope,
                        duration_seconds=604_800,
                        used_percent=50,
                        remaining_percent=50,
                        window_id=f"{provider}-weekly",
                    ),
                ),
                diagnostics=(),
            )
        )
    return tuple(snapshots)


def build_eligibility_reports() -> tuple[ExecutionEligibility, ...]:
    return ()


# ── Compatibility matrix cells ────────────────────────────────────────────────

ALL_FEATURES = (
    "roles_history",
    "streaming",
    "tool_calls",
    "tool_results",
    "structured_output",
    "reasoning_controls",
)

DEFAULT_BACKENDS = (
    ("server_direct_http", "openai", "gpt-5.6-luna"),
    ("server_direct_http", "zai", "glm-5.3"),
)


def build_cells(
    overrides: Mapping[tuple[str, str, str, str], str] | None = None,
    *,
    include_worker: bool = False,
) -> tuple[CompatibilityCell, ...]:
    """PASS cells for every default backend x feature, with overrides.

    ``overrides`` keys are ``(channel, provider, model, feature)``; values
    are cell values (``PASS``/``PARTIAL``/``UNSUPPORTED``/``UNKNOWN``) or
    the ``MISS`` sentinel that removes the cell entirely (the fail-closed
    missing-cell case).
    """
    overrides = overrides or {}
    backends = list(DEFAULT_BACKENDS)
    if include_worker:
        backends.append(("worker_bridged", "openai", "gpt-5.6-luna"))
    cells: list[CompatibilityCell] = []
    for channel, provider, model in backends:
        for feature in ALL_FEATURES:
            value = overrides.get((channel, provider, model, feature), "PASS")
            if value == "MISS":
                continue
            cells.append(
                CompatibilityCell(
                    channel=channel,
                    provider=provider,
                    model=model,
                    feature=feature,
                    value=value,
                    adapter="synthetic-http",
                    adapter_version="1.2.3",
                    evidence=EVIDENCE,
                )
            )
    return tuple(cells)


# ── The deterministic synthetic adapter ───────────────────────────────────────


class ScriptedAdapter:
    """A deterministic in-memory execution adapter (test fixture only).

    NOT a production adapter and never labelled as one: it implements the
    M03 adapter seam so the coordinator, the wire mapping and the HTTP
    server can be exercised end to end without any live provider. The
    configured ``behavior`` callable decides each dispatch's outcome; the
    adapter records every dispatch for assertions.
    """

    def __init__(
        self,
        *,
        channel: str = "server_direct_http",
        adapter_name: str = "synthetic-http",
        adapter_version: str = "1.2.3",
        behavior: Callable[[AdapterCall, ExecutionContext], AdapterResult] | None = None,
    ) -> None:
        assert channel in EXECUTION_CHANNELS
        self.channel: str = channel
        self.adapter_name: str = adapter_name
        self.adapter_version: str = adapter_version
        self.behavior: Callable[[AdapterCall, ExecutionContext], AdapterResult] = (
            behavior if behavior is not None else echo_behavior()
        )
        self.dispatches: list[AdapterCall] = []
        self.contexts: list[ExecutionContext] = []
        self._lock: threading.Lock = threading.Lock()

    def execute(self, call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        with self._lock:
            self.dispatches.append(call)
            self.contexts.append(context)
        return self.behavior(call, context)

    @property
    def dispatch_count(self) -> int:
        return len(self.dispatches)


def _ts(offset_seconds: int) -> str:
    return canonical(T_EVAL + timedelta(seconds=offset_seconds))


def echo_behavior(
    *,
    content: str = "synthetic reply",
    reported_usage: tuple[int, int] | None = (11, 7),
    honor_cancellation: bool = True,
) -> Callable[[AdapterCall, ExecutionContext], AdapterResult]:
    """Default behavior: echo a fixed reply; stream when asked."""

    def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        observation = CallObservation(
            call_index=0,
            started_at=_ts(0),
            ended_at=_ts(1),
            status="completed",
            provider_reported_usage=(
                None if reported_usage is None else UsageTokens(*reported_usage)
            ),
        )
        if call.stream and context.emit_chunk is not None:
            if context.cancelled:
                return AdapterResult(status="cancelled", calls=(observation,))
            for piece in (content[:5], content[5:]):
                if honor_cancellation and context.cancelled:
                    return AdapterResult(status="cancelled", calls=(observation,))
                context.emit_chunk(
                    AdapterStreamChunk(kind=CHUNK_TEXT_DELTA, text=piece)
                )
            context.emit_chunk(
                AdapterStreamChunk(kind=CHUNK_FINISH, finish_reason="stop")
            )
            if reported_usage is not None:
                context.emit_chunk(
                    AdapterStreamChunk(
                        kind=CHUNK_USAGE, usage=UsageTokens(*reported_usage)
                    )
                )
        return AdapterResult(
            status="completed",
            calls=(observation,),
            message=AdapterMessage(role="assistant", content=content),
            finish_reason="stop",
        )

    return behavior


def permanent_failure_behavior(
    note: str = "synthetic backend refused",
) -> Callable[[AdapterCall, ExecutionContext], AdapterResult]:
    def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        _ = call, context
        raise AdapterPermanentError(note)

    return behavior


def ambiguous_failure_behavior() -> (
    Callable[[AdapterCall, ExecutionContext], AdapterResult]
):
    def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        _ = call, context
        raise AdapterAmbiguousError("synthetic ambiguous outcome")

    return behavior


def timeout_behavior() -> Callable[[AdapterCall, ExecutionContext], AdapterResult]:
    def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        _ = call, context
        raise AdapterTimeoutError("synthetic deadline exceeded")

    return behavior


def multi_call_behavior(
    first: tuple[int, int] | None = (120, 30),
    second: tuple[int, int] | None = (10, 500),
) -> Callable[[AdapterCall, ExecutionContext], AdapterResult]:
    """A multi-call fan-out: one provider-reported call, one estimated call."""
    def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        _ = call, context
        calls: list[CallObservation] = [
            CallObservation(
                call_index=0,
                started_at=_ts(0),
                ended_at=_ts(1),
                status="completed",
                provider_reported_usage=(
                    None if first is None else UsageTokens(*first)
                ),
            ),
            CallObservation(
                call_index=1,
                started_at=_ts(1),
                ended_at=_ts(2),
                status="completed",
                estimated_usage=None if second is None else UsageTokens(*second),
            ),
        ]
        return AdapterResult(
            status="completed",
            calls=tuple(calls),
            message=AdapterMessage(role="assistant", content="fan-out reply"),
            finish_reason="stop",
        )

    return behavior


def tool_call_behavior(
    tool_name: str = "list_files",
    arguments: str = '{"path": "."}',
) -> Callable[[AdapterCall, ExecutionContext], AdapterResult]:
    def behavior(call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        _ = call, context
        observation = CallObservation(
            call_index=0,
            started_at=_ts(0),
            ended_at=_ts(1),
            status="completed",
            provider_reported_usage=UsageTokens(9, 4),
        )
        if not call.tools:
            return AdapterResult(
                status="completed",
                calls=(observation,),
                message=AdapterMessage(role="assistant", content="no tools given"),
                finish_reason="stop",
            )
        return AdapterResult(
            status="completed",
            calls=(observation,),
            message=AdapterMessage(
                role="assistant",
                content=None,
                tool_calls=(
                    AdapterToolCall(
                        id="call-synthetic-1", name=tool_name, arguments=arguments
                    ),
                ),
            ),
            finish_reason="tool_calls",
        )

    return behavior


class BlockingAdapter(ScriptedAdapter):
    """An adapter whose dispatch blocks until released (concurrency tests)."""

    def __init__(
        self,
        *,
        channel: str = "server_direct_http",
        adapter_name: str = "synthetic-http",
        adapter_version: str = "1.2.3",
    ) -> None:
        super().__init__(
            channel=channel, adapter_name=adapter_name, adapter_version=adapter_version
        )
        self.release_event: threading.Event = threading.Event()
        self.started_event: threading.Event = threading.Event()

    @override
    def execute(self, call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        with self._lock:
            self.dispatches.append(call)
            self.contexts.append(context)
        self.started_event.set()
        while not self.release_event.is_set() and not context.cancelled:
            time.sleep(0.005)
        if context.cancelled:
            return AdapterResult(status="cancelled", calls=())
        return echo_behavior()(call, context)


# ── Application assembly ──────────────────────────────────────────────────────


def build_aliases(
    entries: Mapping[str, ClientRoutingProfile] | None = None,
) -> RoutingAliasTable:
    return RoutingAliasTable(
        entries
        if entries is not None
        else {
            "deep-coding": ClientRoutingProfile(profile_id="gateway-core"),
            "zai-only": ClientRoutingProfile(
                profile_id="gateway-core", allowed_providers=("zai",)
            ),
        }
    )


def make_application(
    *,
    registry: ResourceRegistry | None = None,
    cells: tuple[CompatibilityCell, ...] | None = None,
    aliases: RoutingAliasTable | None = None,
    adapters: list[ScriptedAdapter] | None = None,
    limits: GatewayLimits | None = None,
    clock: Callable[[], datetime] | None = None,
    request_id_factory: Callable[[], str] | None = None,
    audit: BoundedAuditTrail | None = None,
    admin_constraints: AdministratorConstraints | None = None,
    client_authorizations: Mapping[str, ClientAuthorization] | None = None,
    capacity_snapshots: tuple[CapacitySnapshot, ...] | None = None,
    client_key_directory: ClientKeyDirectory | None = None,
) -> GatewayApplication:
    """Assemble a fully injected GatewayApplication for tests."""
    adapter_registry = AdapterRegistry()
    for adapter in adapters if adapters is not None else [ScriptedAdapter()]:
        adapter_registry.register(adapter)
    snapshots = (
        capacity_snapshots
        if capacity_snapshots is not None
        else build_capacity_snapshots()
    )

    def capacity_source(
        now: str,
    ) -> tuple[tuple[CapacitySnapshot, ...], tuple[ExecutionEligibility, ...]]:
        _ = now
        return (snapshots, ())

    policy: SelectorPolicy = neutral_selector_policy()
    return GatewayApplication(
        catalog=build_catalog(),
        profiles=build_profiles(),
        profile_policy_version=1,
        policy=policy,
        registry=registry if registry is not None else build_registry(),
        capacity_source=capacity_source,
        compatibility_cells=cells if cells is not None else build_cells(),
        admin_constraints=(
            admin_constraints
            if admin_constraints is not None
            else AdministratorConstraints()
        ),
        aliases=aliases if aliases is not None else build_aliases(),
        adapters=adapter_registry,
        audit=audit if audit is not None else BoundedAuditTrail(),
        limits=limits if limits is not None else GatewayLimits(),
        client_key_directory=(
            client_key_directory
            if client_key_directory is not None
            else ClientKeyDirectory.from_secrets({CLIENT_ID: CLIENT_KEY})
        ),
        client_authorizations=client_authorizations,
        clock=clock if clock is not None else fixed_clock(),
        request_id_factory=(
            request_id_factory
            if request_id_factory is not None
            else make_sequential_request_ids()
        ),
    )


def make_sequential_request_ids() -> Callable[[], str]:
    counter = {"n": 0}

    def next_id() -> str:
        counter["n"] += 1
        return f"chatcmpl-{counter['n']:04d}"

    return next_id


def audit_records(application: GatewayApplication) -> tuple[AuditRecord, ...]:
    """The bounded trail's records for one injected application."""
    trail = application.audit
    if not isinstance(trail, BoundedAuditTrail):
        raise TypeError("audit fixture expected a BoundedAuditTrail")
    return trail.snapshot()


def parse_chat_request(document: dict[str, object]) -> ChatCompletionRequest:
    """Parse one chat-completion body via the strict surface parser."""
    from scarcity_router.gateway_openai import parse_chat_completion_request

    return parse_chat_completion_request(document)


__all__ = [
    "ALL_FEATURES",
    "BlockingAdapter",
    "CLIENT_ID",
    "CLIENT_KEY",
    "DEFAULT_BACKENDS",
    "EVIDENCE",
    "GatewayApplication",
    "ScriptedAdapter",
    "T_EVAL",
    "T_NOW",
    "T_OBS",
    "TTL",
    "ambiguous_failure_behavior",
    "build_aliases",
    "build_capacity_snapshots",
    "build_catalog",
    "build_cells",
    "build_eligibility_reports",
    "build_profiles",
    "audit_records",
    "build_registry",
    "canonical",
    "echo_behavior",
    "fixed_clock",
    "make_application",
    "make_sequential_request_ids",
    "multi_call_behavior",
    "parse_chat_request",
    "permanent_failure_behavior",
    "timeout_behavior",
    "tool_call_behavior",
]
