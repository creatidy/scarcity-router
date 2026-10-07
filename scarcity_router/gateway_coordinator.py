"""Execution coordinator: one request's execution lifecycle (M03, D-043).

Owns exactly one lifecycle per external request:

    ingress validation -> admission -> bounded concurrency reservation ->
    dispatch -> streaming lifecycle -> completion/cancellation ->
    observed usage/accounting -> audit record

Frozen rules implemented here (D-043, restated by issue #88):

- **Routing happens once, in the routing core.** A non-pinned request
  calls :func:`~scarcity_router.routing_core.route_request` exactly once
  and dispatches the decision's selected target; a pinned request calls
  :func:`~scarcity_router.routing_core.admit_pinned_target` (admission
  only, never re-ranking). The coordinator never re-ranks, never re-routes
  and never substitutes a target — dispatch failure fails closed with an
  explicit error.
- **The prompt-destination invariant holds from admission**, not from the
  first stream byte: the selected target is final the moment admission
  approves it.
- **Concurrency is bounded and reserved after admission, before
  dispatch.** Exhaustion is an immediate explicit 429, never an unbounded
  wait.
- **No blind retries.** An adapter that reports ambiguous execution state
  produces an explicit error and a ``failed_ambiguous`` audit record;
  nothing re-dispatches (exactly-once is not promised, but no inference
  consumption is ever silently duplicated).
- **Client disconnect propagates** to the selected backend through the
  execution context's cancel event (where the channel supports it — a
  compatibility-matrix fact reported per adapter, never claimed here).
- **Usage/accounting is honest.** One external request may cause several
  internal provider calls; each call is observed separately and
  provider-reported usage stays distinguishable from estimated usage.
- **A synchronous HTTP request is never silently converted into
  undefined-duration background work**: every execution runs inside the
  caller's request, under the admission deadline.

The model field of an OpenAI-compatible request resolves here, exactly
one way: either an administrator-configured routing-profile alias (a
binding into the existing task/profile requirement model — never a second
scoring system, D-042) or an explicit pinned executable-target reference
(``sr-pin:<resource_id>/<provider>/<model>/<variant>`` optionally carrying
``@<decision_id>`` audit provenance). The pinned reference is EXACT: the
gateway builds an :class:`~scarcity_router.routing_core.PinnedTarget`
from it and admission approves exactly that identity — a no-longer-bound
identity is the explicit ``pinned_model_not_bound`` rejection, never a
substitution.

Structural capability requirements the routing core's request binding
does not carry (``roles_history`` for multi-message conversations,
``tool_results`` when tool-result messages are present) are checked at
admission here against the SAME compatibility-matrix cells with the SAME
lookup semantics — admission validation, never ranking, fail-closed on
missing or ``UNKNOWN``/``UNSUPPORTED`` cells.

The coordinator keeps its runtime state IN MEMORY (concurrency counters,
the injectable audit sink) per D-041: no durable state is introduced by
this module; if M09 later introduces the embedded durable store, it
arrives through the audit-sink and identity-directory seams without
changing this lifecycle.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone

from .capacity import CapacitySnapshot
from .eligibility import ExecutionEligibility
from .errors import CapacityValidationError, SelectionContractError
from .gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterPermanentError,
    AdapterRegistry,
    AdapterResult,
    AdapterStreamChunk,
    AdapterTimeoutError,
    AdapterAmbiguousError,
    AdapterToolCall,
    CallObservation,
    ClientDisconnectedError,
    CompletionOutcome,
    ContinuationCapableAdapter,
    ContinuationBindingAdapter,
    ContinuationLostError,
    ExecutionContext,
    FINISH_TOOL_CALLS,
    SuspensionHandle,
    ToolSuspension,
)
from .gateway_audit import (
    RESULT_CANCELLED,
    RESULT_COMPLETED,
    RESULT_FAILED,
    RESULT_FAILED_AMBIGUOUS,
    RESULT_REJECTED,
    RESULT_TIMED_OUT,
    AuditRecord,
    AuditSink,
    ExecutedTarget,
)
from .gateway_continuation import (
    REJECT_ALREADY_RESOLVED,
    REJECT_EXPIRED,
    REJECT_NOT_FOUND,
    CONTINUATION_CANCELLED,
    CONTINUATION_COMPLETED,
    CONTINUATION_EXPIRED,
    CONTINUATION_LOST,
    ContinuationRegistry,
    ContinuationRejected,
    PendingContinuation,
    canonical_json_text,
    message_fingerprint,
    tool_calls_fingerprint,
    tools_fingerprint,
)
from .gateway_contracts import (
    USAGE_SOURCE_ESTIMATED,
    USAGE_SOURCE_MIXED,
    USAGE_SOURCE_PROVIDER_REPORTED,
    USAGE_SOURCE_UNAVAILABLE,
    ClientKeyDirectory,
    GatewayError,
    GatewayLimits,
    UsageAccounting,
    UsageTokens,
)
from .gateway_openai import (
    ChatCompletionRequest,
    LogicalModelInfo,
    RequestCapabilities,
    RouteEffectiveLimits,
)
from .resource_state import (
    OUTPUT_LIMIT_INSUFFICIENT,
    OUTPUT_LIMIT_UNKNOWN,
    OUTPUT_LIMIT_UNENFORCEABLE,
    ExecutionCapabilities,
    RegistrySnapshot,
    ResourceRegistry,
    ResourceRegistryEntry,
    route_output_code,
)
from .gateway_validation import v_instance, v_int, v_safe_id
from .routing_core import (
    AdministratorConstraints,
    AdmissionDecision,
    ClientAuthorization,
    ClientRoutingProfile,
    CompatibilityCell,
    PinnedTarget,
    RequestBinding,
    RouteDecision,
    RouteRequest,
    RouteTarget,
    _lookup_cell,  # pyright: ignore[reportPrivateUsage] -- the M02 matrix lookup is the single authority; reimplementing it here would fork D-043 compatibility semantics
    _authorization_failure_codes,  # pyright: ignore[reportPrivateUsage] -- the M02 authorization stage is the single authority; forking it for the continuation recheck would fork D-042 semantics
    _effective_authorization,  # pyright: ignore[reportPrivateUsage] -- same single-authority rationale
    admit_pinned_target,
    route_request,
)
from .selector import canonical_instant
from .selector import SelectorPolicy
from .selection_types import ModelCatalog, ModelIdentity, ModelRef, TaskProfileCatalog

# ── Model-field resolution ────────────────────────────────────────────────────

PIN_PREFIX = "sr-pin:"

#: D-062: the maximum tool-result text a continuation may carry — the
#: same bound the v3 worker frame enforces (a 4 MiB string stays under
#: the 16 MiB frame bound even in the worst JSON-escaping case). Larger
#: results are a typed pre-delivery rejection, never a truncated frame.
MAX_TOOL_RESULT_TEXT = 4 * 1_048_576

PIN_KIND = "pin"
ALIAS_KIND = "alias"
LOGICAL_KIND = "logical_model"


@dataclass(frozen=True)
class ResolvedModel:
    """The one resolution of a client ``model`` string (D-042/D-055).

    Three kinds: an exact ``sr-pin:`` reference, an administrator routing
    alias, or (D-055) a logical model — an adopted source model requested
    by its bare physical-model id, resolved to the exact
    ``(provider, model)`` identity plus the resolved reasoning-effort
    variant. The logical kind never substitutes another model: the
    routing core narrows to exactly that identity and competes only the
    resources that provide it.
    """

    kind: str
    alias: str | None = None
    profile: ClientRoutingProfile | None = None
    pinned_target: PinnedTarget | None = None
    explicit_model: ModelRef | None = None
    explicit_variant: str | None = None
    available_variants: tuple[str, ...] = ()


class RoutingAliasTable:
    """Administrator-configured routing-profile aliases (D-042 layer 4).

    Each alias is a binding to an EXISTING profile id that the routing
    core resolves through the task-profile catalog; an alias never carries
    its own scoring semantics. Aliases share the safe-id grammar, so the
    ``sr-pin:`` reference syntax can never collide with one.
    """

    def __init__(self, entries: Mapping[str, ClientRoutingProfile]) -> None:
        self._profiles: dict[str, ClientRoutingProfile] = {}
        for alias, profile in entries.items():
            _ = v_safe_id(alias, "routing_alias.alias")
            if alias.startswith(PIN_PREFIX):
                raise ValueError(
                    f"routing_alias: alias {alias!r} collides with the pinned "
                    + "reference syntax"
                )
            _ = v_instance(
                profile, ClientRoutingProfile, f"routing_alias[{alias!r}]"
            )
            self._profiles[alias] = profile

    def __len__(self) -> int:
        return len(self._profiles)

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def resolve(self, alias: str) -> ClientRoutingProfile | None:
        return self._profiles.get(alias)


def parse_pinned_reference(model: str) -> PinnedTarget:
    """Parse ``sr-pin:<resource_id>/<provider>/<model>/<variant>[@<decision>]``.

    The reference is EXACT — all four identifier components are required;
    a reference that omits any component is a request error, never
    repaired by guessing (D-042). The optional ``@decision_id`` suffix is
    audit provenance from a prior recommendation; it is never trusted as a
    reservation (a recommendation is not a reservation).
    """
    invalid = GatewayError.invalid_request(
        "invalid pinned-target reference",
        code="invalid_pin_reference",
        param="model",
    )
    if not model.startswith(PIN_PREFIX):
        raise invalid
    body = model[len(PIN_PREFIX) :]
    decision_id: str | None = None
    if "@" in body:
        body, _, raw_decision = body.rpartition("@")
        try:
            decision_id = v_safe_id(raw_decision, "pin.decision_id")
        except (SelectionContractError, ValueError):
            raise invalid from None
    parts = body.split("/")
    if len(parts) != 4:
        raise invalid
    resource_raw, provider_raw, model_raw, variant_raw = parts
    try:
        resource_id = v_safe_id(resource_raw, "pin.resource_id")
        provider = v_safe_id(provider_raw, "pin.provider")
        model_name = v_safe_id(model_raw, "pin.model")
        variant = v_safe_id(variant_raw, "pin.variant")
    except (SelectionContractError, ValueError):
        raise invalid from None
    return PinnedTarget(
        resource_id=resource_id,
        model=ModelIdentity(provider=provider, model=model_name, variant=variant),
        decision_id=decision_id,
    )


def _logical_matches(
    model: str, catalog: ModelCatalog
) -> tuple[str, tuple[str, ...]]:
    """Resolve a bare model id against the composed catalog (D-055).

    Returns the owning provider and the sorted calibrated variants for
    the exact identity. A slug no catalog entry carries is not exposed
    (the caller rejects with ``model_not_found``); a slug two providers
    carry is ambiguous and fails loudly — never resolved by accidental
    precedence.
    """
    matches = [
        entry.identity
        for entry in catalog.entries
        if entry.identity.model == model and entry.identity.variant
    ]
    if not matches:
        raise GatewayError.not_found(
            "the requested model does not exist on this gateway",
            code="model_not_found",
        )
    providers = sorted({identity.provider for identity in matches})
    if len(providers) > 1:
        raise GatewayError.invalid_request(
            "the requested model id is ambiguous on this gateway",
            code="ambiguous_logical_model",
            param="model",
        )
    return providers[0], tuple(sorted({identity.variant for identity in matches}))


def resolve_model_string(
    model: str,
    aliases: RoutingAliasTable,
    catalog: ModelCatalog | None = None,
) -> ResolvedModel:
    """Resolve the client ``model`` field (D-055 resolution order).

    Exact pinned reference → administrator routing alias → logical
    adopted model → ``model_not_found``. The execution surface invents no
    implicit routing: a logical resolution names the EXACT
    ``(provider, model)`` identity and never another model. Without a
    catalog the logical layer is unavailable and the historical
    alias-or-pin behavior applies unchanged.
    """
    if model.startswith(PIN_PREFIX):
        return ResolvedModel(
            kind=PIN_KIND, pinned_target=parse_pinned_reference(model)
        )
    profile = aliases.resolve(model)
    if profile is not None:
        return ResolvedModel(kind=ALIAS_KIND, alias=model, profile=profile)
    if catalog is None:
        raise GatewayError.not_found(
            "the requested model does not exist on this gateway",
            code="model_not_found",
        )
    provider, variants = _logical_matches(model, catalog)
    return ResolvedModel(
        kind=LOGICAL_KIND,
        explicit_model=ModelRef(provider=provider, model=model),
        available_variants=variants,
    )


def _resolve_logical_effort(
    resolved: ResolvedModel, requested_effort: str | None, catalog: ModelCatalog
) -> ModelCatalog:
    """Intersect exact logical identity with configured effort, never variant text."""
    assert resolved.explicit_model is not None
    entries = tuple(
        entry
        for entry in catalog.entries
        if entry.identity.provider == resolved.explicit_model.provider
        and entry.identity.model == resolved.explicit_model.model
        and entry.identity.variant in resolved.available_variants
    )
    if requested_effort is not None:
        matches = tuple(
            entry for entry in entries if entry.reasoning_effort == requested_effort
        )
        if not matches:
            if any(
                entry.hard_properties.supports_reasoning_mode is None
                for entry in entries
            ):
                raise GatewayError.invalid_request(
                    "the requested reasoning effort has no evidenced configuration",
                    code="compatibility_unknown",
                    param="reasoning_effort",
                )
            raise GatewayError.invalid_request(
                "the requested reasoning effort is not offered by this model",
                code="unsupported_reasoning_effort",
                param="reasoning_effort",
            )
        entries = matches
    elif len({entry.reasoning_effort for entry in entries}) != 1:
        raise GatewayError.invalid_request(
            "this model requires an explicit reasoning_effort",
            code="reasoning_effort_required",
            param="reasoning_effort",
        )
    return ModelCatalog(
        catalog_version=catalog.catalog_version,
        updated_on=catalog.updated_on,
        entries=entries,
    )


def _variant_hard_limits(
    catalog: ModelCatalog, identity: ModelIdentity
) -> tuple[int | None, int | None]:
    """The hard context/output ceilings of ONE exact calibrated variant.

    Exact ``(provider, model, variant)`` match — never a sibling variant's
    calibration, never a minimum across variants (D-058). ``None`` stays
    UNKNOWN, and a variant the catalog does not calibrate yields UNKNOWN
    for both dimensions.
    """
    for entry in catalog.entries:
        if entry.identity == identity:
            return (
                entry.hard_properties.input_context_tokens,
                entry.hard_properties.output_tokens,
            )
    return None, None


def _effective_ceiling(capability_inputs: tuple[int | None, ...], admin: int) -> int | None:
    """The D-056 intersection over one route's inputs (D-058 rule).

    ``min`` over the KNOWN capability inputs (model hard, channel ceiling)
    intersected with the administrator allowance; ``None`` when NO
    capability input is known — an administrator allowance alone never
    certifies a capability the model or the channel have not evidenced,
    so an all-UNKNOWN input set yields an UNKNOWN effective value, never
    a guessed number.
    """
    known = [value for value in capability_inputs if value is not None]
    if not known:
        return None
    return min([*known, admin])


def exposed_logical_models(
    catalog: ModelCatalog,
    registry: ResourceRegistry,
    limits: GatewayLimits,
) -> tuple[LogicalModelInfo, ...]:
    """The logical models ``GET /v1/models`` exposes (D-055, #136/D-058).

    Exposure is resource-aware and adoption-honest: a catalog identity is
    exposed only when at least one registered resource binds that exact
    ``(provider, model)`` — an empty deployment exposes nothing, a
    retired model disappears, and a registered-but-currently-unavailable
    resource still exposes the id (requests then get the explicit
    no-eligible-target outcome, never a misleading ``model_not_found``).
    Restricted and unclassified models never have catalog entries here
    and are never exposed. Ambiguous multi-provider slugs are not
    advertised.

    Effective limits (D-056 intersection, #136/D-058): each route's
    per-variant effective ceiling is ``min(known of: that exact variant's
    hard property, channel ceiling, administrator allowance)`` — never a
    sibling variant's calibration — and stays UNKNOWN when neither the
    variant nor the channel evidences a ceiling; a route binding several
    variants reports the weakest such intersection so the detail never
    over-advertises any variant it serves. The ADVERTISED headline number
    is the strongest bound route's effective ceiling — a request within
    it is executable on this gateway through that route (pre-ranking
    output eligibility routes it there) — and the per-route ``routes``
    detail carries every bound route's own effective ceilings (``None``
    when unknown), so a multi-route spread is never collapsed into
    "every route supports the strongest".
    """
    try:
        snapshot = registry.registry_snapshot()
    except CapacityValidationError:
        return ()
    route_channels: dict[tuple[str, str], list[ResourceRegistryEntry]] = {}
    bound: set[tuple[str, str]] = set()
    for entry in snapshot.entries:
        key = (entry.identity.provider, entry.identity.model)
        bound.add(key)
        route_channels.setdefault(key, []).append(entry)
    by_model: dict[str, list[ModelIdentity]] = {}
    for catalog_entry in catalog.entries:
        identity = catalog_entry.identity
        if (identity.provider, identity.model) not in bound:
            continue
        by_model.setdefault(identity.model, []).append(identity)
    infos: list[LogicalModelInfo] = []
    for model in sorted(by_model):
        identities = by_model[model]
        providers = sorted({identity.provider for identity in identities})
        if len(providers) != 1:
            continue
        model_entries = [
            catalog_entry
            for catalog_entry in catalog.entries
            if catalog_entry.identity.model == model
            and catalog_entry.identity.provider == providers[0]
        ]
        outputs = [
            catalog_entry.hard_properties.output_tokens
            for catalog_entry in model_entries
            if catalog_entry.hard_properties.output_tokens is not None
        ]
        if not outputs:
            continue
        entries = route_channels.get((providers[0], model), ())
        routes: list[RouteEffectiveLimits] = []
        for entry in sorted(entries, key=lambda item: item.identity.resource_id):
            # Exact-variant intersections (D-058): each route's detail is
            # computed per calibrated variant it actually binds, then the
            # route reports the WEAKEST such variant — the detail never
            # over-advertises any variant the route serves. A
            # variant-qualified route (one bound variant) reports that
            # variant's own exact intersection.
            context_values: list[int] = []
            output_values: list[int] = []
            for catalog_entry in model_entries:
                if (
                    entry.identity.variant is not None
                    and catalog_entry.identity.variant != entry.identity.variant
                ):
                    continue
                route_context = _effective_ceiling(
                    (
                        catalog_entry.hard_properties.input_context_tokens,
                        entry.capabilities.context_limit_tokens,
                    ),
                    limits.max_input_context_tokens,
                )
                route_output = _effective_ceiling(
                    (
                        catalog_entry.hard_properties.output_tokens,
                        entry.capabilities.output_limit_tokens,
                    ),
                    limits.max_output_tokens,
                )
                if route_context is not None:
                    context_values.append(route_context)
                if route_output is not None:
                    output_values.append(route_output)
            routes.append(
                RouteEffectiveLimits(
                    resource_id=entry.identity.resource_id,
                    effective_context_limit_tokens=(
                        min(context_values) if context_values else None
                    ),
                    effective_output_limit_tokens=(
                        min(output_values) if output_values else None
                    ),
                )
            )
        contexts = [route.effective_context_limit_tokens for route in routes]
        known_contexts = [value for value in contexts if value is not None]
        known_outputs = [
            route.effective_output_limit_tokens
            for route in routes
            if route.effective_output_limit_tokens is not None
        ]
        if not known_outputs:
            # Reachable corner case (deliberate, documented skip): exposure
            # requires SOME variant of the model to carry a calibrated hard
            # output, but every BOUND route may still bind only variants
            # whose own hard output is UNKNOWN (e.g. a floor-derived effort
            # on a control=False channel). D-055's `max_output_tokens` is a
            # hard number — the model stays unadvertised rather than
            # guessing one, even though it remains routable; the metadata
            # contract never advertises what no bound route evidences.
            continue
        efforts = tuple(
            sorted({
                catalog_entry.reasoning_effort
                for catalog_entry in model_entries
                if catalog_entry.reasoning_effort is not None
                and any(
                    entry.identity.variant is None
                    or entry.identity.variant == catalog_entry.identity.variant
                    for entry in entries
                )
            })
        )
        infos.append(
            LogicalModelInfo(
                model=model,
                provider=providers[0],
                reasoning_efforts=efforts,
                effective_context_limit_tokens=(
                    max(known_contexts) if known_contexts else None
                ),
                max_output_tokens=max(known_outputs),
                routes=tuple(routes),
            )
        )
    return tuple(infos)


# ── The application (server configuration + runtime state) ────────────────────

CapacitySource = Callable[
    [str], tuple[tuple[CapacitySnapshot, ...], tuple[ExecutionEligibility, ...]]
]
RequestFactory = Callable[[], str]
StreamEmitter = Callable[[AdapterStreamChunk], None]


def _carry_concurrency_state(
    replaced: "GatewayApplication | None", limits: GatewayLimits
) -> tuple[threading.BoundedSemaphore, threading.Lock, dict[str, int]]:
    """Carry admission accounting across application rebuilds (Daybreak
    finding 5) when the limits are unchanged; otherwise fresh state."""
    if (
        isinstance(replaced, GatewayApplication)
        and replaced.limits.max_concurrent_executions
        == limits.max_concurrent_executions
        and replaced.limits.max_concurrent_executions_per_client
        == limits.max_concurrent_executions_per_client
    ):
        return (
            replaced._global_slots,  # pyright: ignore[reportPrivateUsage] - same-module carry
            replaced._client_lock,  # pyright: ignore[reportPrivateUsage] - same-module carry
            replaced._client_active,  # pyright: ignore[reportPrivateUsage] - same-module carry
        )
    return (
        threading.BoundedSemaphore(limits.max_concurrent_executions),
        threading.Lock(),
        {},
    )


class GatewayApplication:
    """Process-configured dependencies and runtime state of the gateway.

    Configuration here is SERVER configuration (administrator-owned,
    never sourced from client request content, D-042): the routing
    artifacts, the M01 registry, the capacity/eligibility source, the
    compatibility-matrix cells, both authorization layers, the client
    routing aliases, the admission limits, the adapter registry, the
    audit sink, the clock and — when this process also terminates
    ingress — the client-key directory (the directory type itself lives
    in :mod:`scarcity_router.gateway_contracts`).
    ``client_authorizations`` may omit a client
    id; a missing entry is an unrestricted client grant (the
    administrator layers still gate every request) — per-client narrowing
    is optional configuration.

    Runtime state (the concurrency slots) is created per instance and is
    deliberately not configuration.
    """

    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        profiles: TaskProfileCatalog,
        profile_policy_version: int,
        policy: SelectorPolicy,
        registry: ResourceRegistry,
        capacity_source: CapacitySource,
        compatibility_cells: tuple[CompatibilityCell, ...],
        admin_constraints: AdministratorConstraints,
        aliases: RoutingAliasTable,
        adapters: AdapterRegistry,
        audit: AuditSink,
        limits: GatewayLimits | None = None,
        client_key_directory: ClientKeyDirectory | None = None,
        client_authorizations: Mapping[str, ClientAuthorization] | None = None,
        authority_source: Callable[[str], tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]] | None = None,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: RequestFactory | None = None,
        continuations: ContinuationRegistry | None = None,
        continuation_capability_source: "Callable[[], frozenset[str]] | None" = None,
        replaced_application: "GatewayApplication | None" = None,
    ) -> None:
        _ = v_instance(catalog, ModelCatalog, "gateway_application.catalog")
        _ = v_instance(profiles, TaskProfileCatalog, "gateway_application.profiles")
        _ = v_int(
            profile_policy_version,
            "gateway_application.profile_policy_version",
            lo=1,
        )
        _ = v_instance(policy, SelectorPolicy, "gateway_application.policy")
        _ = v_instance(registry, ResourceRegistry, "gateway_application.registry")
        for cell in compatibility_cells:
            _ = v_instance(
                cell, CompatibilityCell, "gateway_application.compatibility_cells"
            )
        _ = v_instance(
            admin_constraints,
            AdministratorConstraints,
            "gateway_application.admin_constraints",
        )
        _ = v_instance(aliases, RoutingAliasTable, "gateway_application.aliases")
        _ = v_instance(adapters, AdapterRegistry, "gateway_application.adapters")
        if limits is not None:
            # ``None`` selects the documented safe-default limits (D-044).
            _ = v_instance(limits, GatewayLimits, "gateway_application.limits")
        if client_authorizations is not None:
            for client_id, grant in client_authorizations.items():
                _ = v_safe_id(client_id, "gateway_application.client_authorizations")
                _ = v_instance(
                    grant,
                    ClientAuthorization,
                    "gateway_application.client_authorizations",
                )
        if not callable(getattr(audit, "append", None)):
            raise ValueError("gateway_application.audit: expected an AuditSink")
        if client_key_directory is not None:
            _ = v_instance(
                client_key_directory,
                ClientKeyDirectory,
                "gateway_application.client_key_directory",
            )
        self.catalog: ModelCatalog = catalog
        self.profiles: TaskProfileCatalog = profiles
        self.profile_policy_version: int = profile_policy_version
        self.policy: SelectorPolicy = policy
        self.registry: ResourceRegistry = registry
        self.capacity_source: CapacitySource = capacity_source
        self.compatibility_cells: tuple[CompatibilityCell, ...] = compatibility_cells
        self.admin_constraints: AdministratorConstraints = admin_constraints
        self.aliases: RoutingAliasTable = aliases
        self.adapters: AdapterRegistry = adapters
        self.audit: AuditSink = audit
        self.limits: GatewayLimits = (
            limits if limits is not None else GatewayLimits()
        )
        self.client_key_directory: ClientKeyDirectory | None = client_key_directory
        self.client_authorizations: Mapping[str, ClientAuthorization] | None = (
            client_authorizations
        )
        self.authority_source: Callable[[str], tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]] | None = authority_source
        self.clock: Callable[[], datetime] | None = clock
        self.request_id_factory: RequestFactory | None = request_id_factory
        #: D-062: the shared continuation registry (one instance per
        #: server process, owned by the composition so application
        #: rebuilds never orphan a pending continuation). ``None``
        #: (the default) means the deployment has no client-tool
        #: continuation surface — suspensions then fail closed instead
        #: of registering.
        self.continuations: ContinuationRegistry | None = continuations
        #: D-062 (review round 2, finding 3): the LIVE worker-continuation
        #: capability source — the worker_bridged resource ids whose owning
        #: worker session has negotiated protocol v3 right now. ``None``
        #: means no live fact source: tool-bearing worker_bridged requests
        #: fail closed at pre-ranking availability. Wired by the composed
        #: server over the worker endpoint; never derived from request
        #: content, never a matrix mutation.
        self.continuation_capability_source: (
            "Callable[[], frozenset[str]] | None"
        ) = continuation_capability_source
        # Daybreak finding 5: concurrency enforcement must SURVIVE
        # application rebuilds (a state-report adoption rebuilds the
        # application while executions are in flight). The reservation
        # state is carried over from the replaced application whenever
        # the limits are unchanged, so in-flight executions keep their
        # accounting and limits cannot be reset by a rebuild.
        carry = _carry_concurrency_state(replaced_application, self.limits)
        self._global_slots: threading.BoundedSemaphore = carry[0]
        self._client_lock: threading.Lock = carry[1]
        self._client_active: dict[str, int] = carry[2]

    # ── Shared seams ─────────────────────────────────────────────────────

    def _now(self) -> datetime:
        if self.clock is not None:
            moment = self.clock()
        else:
            moment = datetime.now(timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError(
                "gateway clock must return a timezone-aware datetime, got a "
                + "naive datetime"
            )
        return moment

    def _acquire_reservation(self, client_id: str) -> _ConcurrencyReservation:
        """The bounded post-admission execution reservation (D-043)."""
        return _ConcurrencyReservation.acquire(
            semaphore=self._global_slots,
            client_lock=self._client_lock,
            client_active=self._client_active,
            per_client_limit=self.limits.max_concurrent_executions_per_client,
            client_id=client_id,
        )

    def _client_grant(self, client_id: str) -> ClientAuthorization:
        return self._current_authority(client_id)[2]

    def _current_authority(
        self, client_id: str,
    ) -> tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]:
        if self.authority_source is not None:
            try:
                registry, administrator, grant, adapters = self.authority_source(client_id)
                _ = v_instance(registry, ResourceRegistry, "current_resource_registry")
                _ = v_instance(administrator, AdministratorConstraints, "current_administrator_constraints")
                _ = v_instance(grant, ClientAuthorization, "current_client_authorization")
                _ = v_instance(adapters, AdapterRegistry, "current_adapter_registry")
                return registry, administrator, grant, adapters
            except GatewayError:
                raise
            except Exception:  # A failed live authority read never restores a legacy grant.
                raise GatewayError.api(
                    "the client's current authorization is unavailable",
                    code="state_unavailable", http_status=503,
                ) from None
        grants = self.client_authorizations
        grant = ClientAuthorization() if grants is None else grants.get(client_id, ClientAuthorization())
        return self.registry, self.admin_constraints, grant, self.adapters

    # ── The lifecycle ────────────────────────────────────────────────────

    def execute(
        self,
        *,
        client_id: str,
        request: ChatCompletionRequest,
        emit_chunk: StreamEmitter | None = None,
        request_id: str | None = None,
    ) -> CompletionOutcome:
        """Run one execution lifecycle; returns the completion outcome.

        Raises :class:`GatewayError` for every client-visible failure
        (each outcome is audited before the error propagates) and
        :class:`ClientDisconnectedError` when the client connection dropped
        mid-execution (audited as ``cancelled``). Streaming chunks are
        delivered through ``emit_chunk`` while the dispatch runs; the
        outcome is still returned for a stream that completes.
        ``request_id`` lets a transport supply the response identifier it
        already announced on a stream; it must be a safe identifier and is
        otherwise generated here.
        """
        started = self._now()
        resolved_request_id = (
            request_id if request_id is not None else _request_id(self.request_id_factory)
        )
        state = _LifecycleState(
            request_id=resolved_request_id,
            client_id=client_id,
            started_at=canonical_instant(started),
        )
        try:
            continuation_record = self._detect_continuation(request, client_id)
            if continuation_record is not None:
                # D-062: this request IS the harness's tool result for a
                # suspended backend turn. It never routes, never
                # re-ranks and never re-admits: it resolves into the
                # exact continuation (sticky to its original target).
                outcome = self._run_continuation(
                    record=continuation_record,
                    request=request,
                    client_id=client_id,
                    emit_chunk=emit_chunk,
                    started=started,
                    state=state,
                )
            else:
                outcome = self._run_lifecycle(
                    started=started, request=request, client_id=client_id,
                    state=state, emit_chunk=emit_chunk,
                )
            self._write_audit(state, RESULT_COMPLETED, ("completed",))
            return outcome
        except ClientDisconnectedError:
            if state.context is not None:
                state.context.cancel_event.set()
            self._write_audit(state, RESULT_CANCELLED, ("client_disconnected",))
            raise
        except AdapterTimeoutError:
            self._write_audit(
                state, RESULT_TIMED_OUT, ("execution_time_limit_exceeded",)
            )
            raise GatewayError.timeout(
                "the execution exceeded its time limit",
                code="execution_time_limit_exceeded",
            ) from None
        except AdapterAmbiguousError:
            self._write_audit(
                state, RESULT_FAILED_AMBIGUOUS, ("ambiguous_execution_state",)
            )
            raise GatewayError.api(
                "the execution ended in an ambiguous state and was not "
                + "retried; backend usage may have been consumed",
                code="ambiguous_execution_state",
                http_status=500,
            ) from None
        except AdapterPermanentError:
            self._write_audit(state, RESULT_FAILED, ("backend_failure",))
            raise GatewayError.api(
                "the selected backend failed to execute the request",
                code="backend_failure",
                http_status=502,
            ) from None
        except GatewayError as exc:
            if state.executed_target is None:
                self._write_audit(state, RESULT_REJECTED, (exc.code or "rejected",))
            else:
                self._write_audit(state, RESULT_FAILED, (exc.code or "failed",))
            raise
        except Exception:
            # Fail closed on audit completeness: an unexpected internal
            # error on an executed request still writes its record (safe
            # reason code, no exception details) before propagating.
            status = RESULT_FAILED if state.executed_target is not None else RESULT_REJECTED
            self._write_audit(state, status, ("unexpected_error",))
            raise

    # ── Lifecycle phases ─────────────────────────────────────────────────

    def _run_lifecycle(
        self,
        *,
        started: datetime,
        request: ChatCompletionRequest,
        client_id: str,
        state: _LifecycleState,
        emit_chunk: StreamEmitter | None,
    ) -> CompletionOutcome:
        self._enforce_request_limits(request.capabilities)
        resolved = resolve_model_string(request.model, self.aliases, self.catalog)
        self._admit(started=started, request=request, resolved=resolved, state=state)
        target = state.target
        if target is None:  # pragma: no cover - admission sets it or raises
            raise GatewayError.api(
                "admission produced no target", code="invalid_state"
            )
        self._supplemental_capability_gate(target, request.capabilities)
        state.normalized_output_limit = self._enforce_effective_limits(
            target=target, caps=request.capabilities, state=state
        )
        reservation = self._acquire_reservation(client_id)
        try:
            return self._dispatch(
                request=request, target=target, state=state,
                emit_chunk=emit_chunk, started=started, resolved=resolved,
            )
        finally:
            reservation.release()

    # ── D-062 client-tool continuation ───────────────────────────────────

    _CONTINUATION_TOKEN_PREFIX: str = "srct-"

    def _detect_continuation(
        self, request: ChatCompletionRequest, client_id: str
    ) -> PendingContinuation | None:
        """Whether this request IS a harness tool result for a suspension.

        Detection is SHAPE-AWARE: only a request whose FINAL message is
        a ``role: "tool"`` result can be attempting a continuation, so
        ordinary full-history follow-up turns (which necessarily still
        carry earlier tool messages) are never mistaken for replays —
        a completed round never poisons the conversation. Within that
        delivery shape:

        - a live record for the addressed token → the continuation
          (exact-shape validation follows in
          :meth:`_validate_continuation_request`);
        - a terminally resolved token (client-scoped tombstone) → the
          explicit ``409`` conflict, never a silent re-interpretation;
        - any other gateway-issued ``srct-`` token (a lost record:
          gateway restart, registry bound) → the typed ``404``
          continuation_not_found — the gateway issued that id, so a
          result addressed to it must never be fed to a fresh model
          selection;
        - any non-gateway id → a normal request (full-history paths on
          other channels stay untouched).

        A live record cited WITHOUT the delivery shape (mid-history)
        falls through to normal flow, where the codex adapter's own
        rejection applies.
        """
        registry = self.continuations
        if registry is None:
            return None
        messages = request.messages
        if not messages:
            return None
        final = messages[-1]
        if final.role != "tool" or final.tool_call_id is None:
            return None
        token = final.tool_call_id
        candidate = registry.detect(token)
        if candidate is not None:
            return candidate
        if registry.was_terminal_for(token, client_id):
            # A replayed/duplicate tool result after the continuation
            # already resolved: the explicit conflict. A foreign
            # client's replay stays not-found.
            raise GatewayError.conflict(
                "this continuation was already resolved by one tool result",
                code="continuation_already_resolved",
            )
        if token.startswith(self._CONTINUATION_TOKEN_PREFIX):
            # A result addressed to a gateway-issued id with no registry
            # state: the suspension was lost (restart, bound) — the
            # documented typed failure, never a fresh completion.
            raise GatewayError.not_found(
                "no pending continuation exists for this tool result",
                code="continuation_not_found",
            )
        return None

    def _continuation_error(self, reason: str) -> GatewayError:
        """The typed failure for a tool result that cannot claim its
        continuation (D-062: expired/not-found are distinct, replay and
        double-delivery conflict)."""
        if reason == REJECT_EXPIRED:
            return GatewayError.not_found(
                "the continuation's lifetime has expired; restart the task "
                + "with a fresh request",
                code="continuation_expired",
            )
        if reason == REJECT_ALREADY_RESOLVED:
            return GatewayError.conflict(
                "this continuation was already resolved by one tool result",
                code="continuation_already_resolved",
            )
        return GatewayError.not_found(
            "no pending continuation exists for this tool result",
            code="continuation_not_found",
        )

    def _validate_continuation_request(
        self,
        *,
        record: PendingContinuation,
        request: ChatCompletionRequest,
    ) -> str:
        """Validate the exact continuation shape; returns the tool content.

        Fails closed on ANY drift: a changed model, effort, tool set or
        tool_choice; a rewritten conversation prefix; a substituted
        assistant tool_calls entry; more than the one expected new
        tool-result message; or an over-bound result text. Fingerprints
        are bounded digests — no prompt content is stored or compared in
        the clear.
        """
        messages = request.messages
        if (
            record.client_id  # client equality checked by the caller
            and request.model != record.model_echo
        ):
            raise GatewayError.invalid_request(
                "a continuation must repeat the original model",
                code="continuation_mismatch",
                param="model",
            )
        if request.reasoning_effort != record.reasoning_effort:
            raise GatewayError.invalid_request(
                "a continuation must repeat the original reasoning effort",
                code="continuation_mismatch",
                param="reasoning_effort",
            )
        request_tools_fp = tools_fingerprint(request.tools)
        if request_tools_fp != record.tools_fingerprint and request.tools:
            # Normal harnesses resend the identical tools[]; an absent
            # declaration means the unchanged thread-scoped set (the
            # #137 evidence), never a substitution.
            raise GatewayError.invalid_request(
                "a continuation's tool declarations must match the "
                + "suspended turn's declared set",
                code="continuation_mismatch",
                param="tools",
            )
        if canonical_json_text(request.tool_choice) != record.tool_choice_json:
            raise GatewayError.invalid_request(
                "a continuation must repeat the original tool_choice",
                code="continuation_mismatch",
                param="tool_choice",
            )
        if len(messages) < 3:
            raise GatewayError.invalid_request(
                "a continuation carries the conversation, the assistant "
                + "tool_calls and the tool result",
                code="continuation_mismatch",
                param="messages",
            )
        final = messages[-1]
        assistant = messages[-2]
        if (
            final.role != "tool"
            or final.tool_call_id != record.continuation_token
            or final.content is None
            or assistant.role != "assistant"
            or tool_calls_fingerprint(assistant.tool_calls)
            != record.assistant_tool_calls_digest
        ):
            raise GatewayError.invalid_request(
                "the continuation's final messages must echo the returned "
                + "tool_calls and address this tool result to them",
                code="continuation_mismatch",
                param="messages",
            )
        if message_fingerprint(messages[:-2]) != record.prefix_fingerprint:
            raise GatewayError.invalid_request(
                "the continued conversation prefix has changed since the "
                + "model suspended",
                code="continuation_mismatch",
                param="messages",
            )
        if len(final.content) > MAX_TOOL_RESULT_TEXT:
            raise GatewayError.invalid_request(
                "the tool result exceeds the maximum continuation size",
                code="tool_result_too_large",
                param="messages",
            )
        return final.content

    def _run_continuation(
        self,
        *,
        record: PendingContinuation,
        request: ChatCompletionRequest,
        client_id: str,
        emit_chunk: StreamEmitter | None,
        started: datetime,
        state: _LifecycleState,
    ) -> CompletionOutcome:
        """Resolve one suspended backend turn with the harness's result.

        Sticky by construction: no competitive routing runs, the D-059
        policy and scarcity state are never re-read for selection, and
        the original selected/executed targets are the audit identity.
        Only the hard gates recheck: the record's client, the absolute
        lifetime, and the live worker session (revocation or session
        loss fails closed — never a substitute execution).
        """
        registry = self.continuations
        assert registry is not None
        if record.client_id != client_id:
            # Indistinguishable from an unknown id: a token never grants
            # another client access to a pending execution.
            raise self._continuation_error(REJECT_NOT_FOUND)
        content = self._validate_continuation_request(
            record=record, request=request
        )
        try:
            record = registry.claim(record.continuation_token, client_id)
        except ContinuationRejected as exc:
            raise self._continuation_error(exc.reason) from None
        remaining = (record.deadline - self._now()).total_seconds()
        if remaining <= 0.0:
            self._close_continuation(record, CONTINUATION_EXPIRED)
            raise self._continuation_error(REJECT_EXPIRED)
        # Audit provenance repeats the ORIGINAL dispatch identity: the
        # continuation is the same logical backend execution, so its one
        # provider call is counted exactly once — on the terminal record.
        state.decision_id = record.decision_id
        state.selected_target = record.selected_target
        state.executed_target = record.executed_target
        state.adapter_name = record.adapter_name
        state.adapter_version = record.adapter_version
        state.registry_revision = record.registry_revision
        state.registry_generated_at = record.registry_generated_at
        adapter = record.adapter if record.adapter is not None else self.adapters.resolve(record.channel)
        if adapter is None or not isinstance(adapter, ContinuationCapableAdapter):
            self._close_continuation(record, CONTINUATION_LOST)
            raise GatewayError.api(
                "no continuation-capable adapter is configured for the "
                + "suspended execution's channel",
                code="adapter_unavailable",
                http_status=503,
            )
        context = self._build_context(
            state,
            emit_chunk,
            record.deadline,
            register_continuation=self._continuation_registrar(
                request=request, state=state, deadline=record.deadline,
                channel=record.channel, adapter=adapter,
                adapter_binding=record.adapter_binding,
            ),
        )
        state.context = context
        state.flow_notes = (*state.flow_notes, "continuation_resumed")
        if record.handle is None:  # pragma: no cover - registration invariant
            self._close_continuation(record, CONTINUATION_LOST)
            raise GatewayError.api(
                "the continuation record is unusable",
                code="invalid_state",
                http_status=500,
            )
        # Hard-authority recheck (review round 2, finding 6): sticky
        # routing never bypasses CURRENT authorization. The exact
        # original target's registry entry is re-read and the client's
        # CURRENT grant is evaluated through the SAME M02 authorization
        # stage admission used — scarcity/policy ranking is never run,
        # and a revoked grant cancels the suspended turn instead of
        # delivering into it.
        # Every post-claim exit reaches a terminal registry state
        # (review round 2, finding 5): nothing may occupy the pending
        # table past this point regardless of outcome.
        try:
            self._recheck_continuation_authority(record, adapter)
            outcome = adapter.deliver_tool_result(record.handle, content, context)
        except ClientDisconnectedError:
            self._close_continuation(record, CONTINUATION_CANCELLED)
            raise
        except ContinuationLostError:
            self._close_continuation(record, CONTINUATION_LOST)
            raise GatewayError.not_found(
                "the suspended execution was lost before the tool result "
                + "was delivered; nothing was consumed",
                code="continuation_not_found",
            ) from None
        except AdapterAmbiguousError:
            self._close_continuation(record, CONTINUATION_LOST)
            raise
        except AdapterTimeoutError:
            self._close_continuation(record, CONTINUATION_EXPIRED)
            raise
        except AdapterPermanentError:
            # The backend definitively failed the resumed turn.
            self._close_continuation(record, CONTINUATION_LOST)
            raise
        except GatewayError:
            self._close_continuation(record, CONTINUATION_CANCELLED)
            raise
        except Exception:
            # Unexpected internal error: the record must still reach a
            # terminal state (honest ambiguity about the delivery).
            self._close_continuation(record, CONTINUATION_LOST)
            raise
        if isinstance(outcome, ToolSuspension):
            # A sequential tool round on the SAME turn: the previous
            # token's result was consumed by THIS delivery (terminal for
            # that token — a later replay gets the explicit conflict).
            # The NEXT continuation was already registered by the adapter
            # through this leg's registrar BEFORE the new token became
            # observable (review round 2, finding 4).
            self._close_continuation(record, CONTINUATION_COMPLETED)
            state.calls = (
                CallObservation(
                    call_index=0,
                    started_at=state.started_at,
                    ended_at=canonical_instant(self._now()),
                    status="unknown",
                    note=(
                        "the backend turn is suspended for a client-owned "
                        + "tool call"
                    ),
                ),
            )
            message = AdapterMessage(
                role="assistant",
                content=outcome.content,
                tool_calls=(
                    AdapterToolCall(
                        id=outcome.continuation_token,
                        name=outcome.tool_name,
                        arguments=outcome.arguments,
                    ),
                ),
            )
            accounting = _unavailable_usage()
            return CompletionOutcome(
                request_id=state.request_id,
                created=int(started.timestamp()),
                model_echo=request.model,
                message=message,
                finish_reason=FINISH_TOOL_CALLS,
                usage=accounting,
                decision_id=record.decision_id,
            )
        result = outcome
        state.calls = result.calls
        if result.status == "cancelled":
            self._close_continuation(record, CONTINUATION_CANCELLED)
            if context.cancelled:
                raise ClientDisconnectedError()
            raise AdapterTimeoutError(result_note(result))
        if result.status == "failed":
            self._close_continuation(record, CONTINUATION_LOST)
            raise AdapterPermanentError(result_note(result))
        self._close_continuation(record, CONTINUATION_COMPLETED)
        accounting = _aggregate_usage(result)
        message = result.message
        finish_reason = result.finish_reason
        if message is None or finish_reason is None:  # pragma: no cover - contract
            raise AdapterPermanentError("adapter returned an incomplete result")
        return CompletionOutcome(
            request_id=state.request_id,
            created=int(started.timestamp()),
            model_echo=request.model,
            message=message,
            finish_reason=finish_reason,
            usage=accounting,
            decision_id=record.decision_id,
        )

    def _continuation_registrar(
        self,
        *,
        request: ChatCompletionRequest,
        state: _LifecycleState,
        deadline: datetime,
        channel: str,
        adapter: ContinuationCapableAdapter,
        adapter_binding: tuple[str, ...] | None = None,
    ) -> "Callable[[SuspensionHandle, str], bool]":
        """The dispatch's continuation registration closure (D-062).

        The ADAPTER invokes this at the suspension point — strictly
        before the tool_call id becomes observable (streamed frame or
        return value), so the invariant "observable ⇒ registered" holds
        for streaming and non-streaming clients alike (review round 2,
        finding 4). The record retains only identifiers and bounded
        digests: the echoed assistant tool_calls are stored as their
        fingerprint, never as raw arguments (finding 2).
        """
        registry = self.continuations

        def register(handle: SuspensionHandle, arguments: str) -> bool:
            if registry is None:  # pragma: no cover - dispatch gate
                return False
            if isinstance(adapter, ContinuationBindingAdapter):
                try:
                    if adapter_binding is None or adapter.continuation_binding(handle.resource_id) != adapter_binding:
                        return False
                except Exception:
                    return False
            record = PendingContinuation(
                continuation_token=handle.continuation_token,
                attempt_id=handle.attempt_id,
                resource_id=handle.resource_id,
                channel=channel,
                call_id=handle.call_id,
                deadline=deadline,
                created_at=canonical_instant(self._now()),
                client_id=state.client_id,
                model_echo=request.model,
                reasoning_effort=request.reasoning_effort,
                tools_fingerprint=tools_fingerprint(request.tools),
                tool_choice_json=canonical_json_text(request.tool_choice),
                prefix_fingerprint=message_fingerprint(request.messages),
                assistant_tool_calls_digest=tool_calls_fingerprint(
                    (
                        AdapterToolCall(
                            id=handle.continuation_token,
                            name=handle.tool_name,
                            arguments=arguments,
                        ),
                    )
                ),
                decision_id=state.decision_id,
                adapter_name=state.adapter_name,
                adapter_version=state.adapter_version,
                registry_revision=state.registry_revision,
                registry_generated_at=state.registry_generated_at,
                selected_target=state.selected_target,
                executed_target=state.executed_target,
                handle=handle,
                adapter=adapter,
                adapter_binding=adapter_binding,
                cancel_callback=lambda _record: adapter.cancel_suspension(handle),
            )
            registered = registry.register(record)
            if registered:
                state.flow_notes = (
                    *state.flow_notes,
                    "suspended_for_client_tool",
                )
            return registered

        return register

    def _close_continuation(self, record: PendingContinuation, state: str) -> None:
        """Terminal-close a claimed continuation (registry removal +
        tombstone) and cancel the suspended worker turn whenever the
        backend may still be waiting (expiry/authority revocation)."""
        registry = self.continuations
        assert registry is not None
        registry.close(
            record.continuation_token, state,
            cancel_backend=state in (CONTINUATION_CANCELLED, CONTINUATION_EXPIRED),
        )

    def _recheck_continuation_authority(
        self, record: PendingContinuation, adapter: ContinuationCapableAdapter,
    ) -> None:
        """The CURRENT hard authority of the ORIGINAL client against the
        EXACT original target (review round 2, finding 6).

        Runs the SAME M02 authorization stage admission runs (provider /
        channel / entitlement allow-lists, blocked resources, spending
        limits) over the client's CURRENT grant — never scarcity or
        campaign ranking, which stay non-authoritative for an existing
        turn (D-059). On any failure the continuation is closed and the
        suspended backend turn is cancelled; the typed permission
        failure never reroutes.
        """
        registry = self.continuations
        assert registry is not None
        try:
            current_registry, administrator, client, current_adapters = self._current_authority(record.client_id)
            snapshot = current_registry.registry_snapshot(
                now=canonical_instant(self._now())
            )
        except (CapacityValidationError, ValueError):
            raise GatewayError.api(
                "the gateway's resource state is unavailable",
                code="state_unavailable",
                http_status=503,
            ) from None
        entry = next(
            (
                candidate
                for candidate in snapshot.entries
                if candidate.identity.resource_id == record.resource_id
            ),
            None,
        )
        if entry is None:
            # The exact target's registration disappeared: hard
            # authority/availability loss — fail closed, cancel, never
            # reroute.
            raise GatewayError.not_found(
                "the suspended execution's target no longer exists",
                code="continuation_not_found",
            )
        original = record.executed_target
        if (
            original is None or entry.identity.channel != record.channel
            or entry.identity.provider != original.provider
            or entry.identity.model != original.model
            or (entry.identity.variant is not None and entry.identity.variant != original.variant)
        ):
            raise GatewayError.permission(
                "the suspended execution's original target binding has changed",
                code="unauthorized_target",
            )
        effective = _effective_authorization(
            administrator,
            client,
            None,
        )
        codes = _authorization_failure_codes(entry, effective)
        if codes:
            raise GatewayError.permission(
                "the client's current authorization no longer covers the "
                + "suspended execution's target",
                code="unauthorized_target",
            )
        current_adapter = current_adapters.resolve(record.channel)
        try:
            same_binding = (
                (isinstance(current_adapter, ContinuationCapableAdapter)
                 and not isinstance(current_adapter, ContinuationBindingAdapter)
                 and current_adapter is adapter)
                if record.adapter_binding is None else
                isinstance(current_adapter, ContinuationCapableAdapter)
                and isinstance(current_adapter, ContinuationBindingAdapter)
                and current_adapter.continuation_binding(record.resource_id) == record.adapter_binding
            )
        except Exception:
            raise GatewayError.api(
                "the suspended execution's current binding is unavailable",
                code="state_unavailable", http_status=503,
            ) from None
        if not same_binding:
            raise GatewayError.permission(
                "the suspended execution's adapter/worker binding is no longer authorized",
                code="unauthorized_target",
            )

    def expire_continuations(self) -> tuple[str, ...]:
        """Expire due continuations (the server's reaper loop calls this).

        Each expiry cancels the worker-side turn best-effort and drops
        the record, so later tool results receive the typed expired/
        not-found response and no Codex process outlives its budget.
        """
        if self.continuations is None:
            return ()
        return self.continuations.expire_due(self._now())

    def _enforce_request_limits(self, caps: RequestCapabilities) -> None:
        """Body/context/output admission limits, before any routing I/O."""
        limits = self.limits
        if (
            caps.estimated_input_tokens is not None
            and caps.estimated_input_tokens > limits.max_input_context_tokens
        ):
            raise GatewayError.invalid_request(
                "the request exceeds the maximum input context",
                code="context_length_exceeded",
            )
        if (
            caps.requested_output_tokens is not None
            and caps.requested_output_tokens > limits.max_output_tokens
        ):
            raise GatewayError.invalid_request(
                "the requested output exceeds the maximum output limit",
                code="output_limit_exceeded",
                param="max_completion_tokens",
            )

    def _admit(
        self,
        *,
        started: datetime,
        request: ChatCompletionRequest,
        resolved: ResolvedModel,
        state: _LifecycleState,
    ) -> None:
        """Admission: exactly one routing-core call, or a typed rejection."""
        now_ts = canonical_instant(started)
        try:
            registry_snapshot = self.registry.registry_snapshot(now=now_ts)
            capacity_snapshots, eligibility_reports = self.capacity_source(now_ts)
        except (CapacityValidationError, ValueError):
            raise GatewayError.api(
                "the gateway's resource state is unavailable",
                code="state_unavailable",
                http_status=503,
            ) from None
        state.registry_revision = registry_snapshot.revision
        state.registry_generated_at = registry_snapshot.generated_at
        explicit_model = None
        explicit_variant = None
        admission_catalog = self.catalog
        candidate_identities = None
        if resolved.kind == LOGICAL_KIND:
            # D-071: narrow by catalog effort before the authoritative core.
            explicit_model = resolved.explicit_model
            admission_catalog = _resolve_logical_effort(
                resolved, request.reasoning_effort, self.catalog
            )
            if len(admission_catalog.entries) == 1:
                explicit_variant = admission_catalog.entries[0].identity.variant
            candidate_identities = tuple(entry.identity for entry in admission_catalog.entries)
        if resolved.profile is not None:
            state.routing_profile = resolved.profile.profile_id
            state.routing_policy_version = self.profile_policy_version
        caps = request.capabilities
        binding = RequestBinding(
            requires_tool_calls=caps.requires_tool_calls,
            requires_structured_output=caps.requires_structured_output,
            requires_streaming=caps.requires_streaming,
            requires_reasoning_controls=caps.requires_reasoning_controls,
            minimum_input_context_tokens=caps.estimated_input_tokens,
            maximum_output_tokens=caps.requested_output_tokens,
            profile_alias=resolved.alias,
            explicit_model=explicit_model,
            explicit_variant=explicit_variant,
            pinned_target=resolved.pinned_target,
        )
        routing_profile = resolved.profile
        continuation_capable: frozenset[str] | None = None
        if self.continuation_capability_source is not None:
            try:
                continuation_capable = self.continuation_capability_source()
            except Exception:  # noqa: BLE001 - a live-fact failure fails
                # CLOSED (None), never open: the availability stage then
                # excludes worker_bridged candidates for tool requests.
                continuation_capable = None
        try:
            request_obj = RouteRequest(
                catalog=self.catalog,
                profiles=self.profiles,
                registry_snapshot=registry_snapshot,
                policy=self.policy,
                evaluated_at=started,
                capacity_snapshots=capacity_snapshots,
                eligibility_reports=eligibility_reports,
                compatibility_cells=self.compatibility_cells,
                admin_constraints=self.admin_constraints,
                client_authorization=self._client_grant(state.client_id),
                routing_profile=routing_profile,
                request=binding,
                profile_policy_version=(
                    self.profile_policy_version if routing_profile is not None else None
                ),
                continuation_capable_resource_ids=continuation_capable,
                candidate_identities=candidate_identities,
            )
        except (CapacityValidationError, SelectionContractError, ValueError):
            raise GatewayError.api(
                "the gateway's routing configuration is invalid",
                code="invalid_state",
                http_status=500,
            ) from None
        if resolved.pinned_target is not None:
            admission = admit_pinned_target(
                request_obj, pinned_target=resolved.pinned_target
            )
            if not admission.approved:
                raise _admission_rejection(admission)
            assert admission.target is not None
            state.decision_id = admission.bound_decision_id
            state.target = admission.target
            state.selected_target = _audit_target(admission.target)
            state.target_capabilities = _target_capabilities(
                registry_snapshot, admission.target
            )
            return
        decision = route_request(request_obj)
        state.decision_id = decision.decision_id
        if decision.status != "selected" or decision.target is None:
            raise _no_eligible_target_error(decision)
        state.target = decision.target
        state.selected_target = _audit_target(decision.target)
        state.target_capabilities = _target_capabilities(
            registry_snapshot, decision.target
        )

    def _supplemental_capability_gate(
        self, target: RouteTarget, caps: RequestCapabilities
    ) -> None:
        """Admission-gate the structural features M02's binding does not carry.

        ``roles_history`` (multi-message conversations) and
        ``tool_results`` (tool-result messages present) are request
        features with their own matrix dimensions; the routing core's
        request binding does not express them, so admission checks them
        here against the SAME cells with the SAME lookup semantics.
        Missing or ``UNKNOWN``/``UNSUPPORTED`` fails closed (D-043) with
        an explicit pre-inference rejection — never a ranking change.
        """
        required: tuple[str, ...] = ()
        if caps.requires_roles_history:
            required = (*required, "roles_history")
        if caps.requires_tool_results:
            required = (*required, "tool_results")
        identity = target.resource
        for feature in required:
            cell = _lookup_cell(self.compatibility_cells, identity, feature)
            if cell is None or cell.value == "UNKNOWN":
                raise GatewayError.invalid_request(
                    "the selected backend has no evidenced support for a "
                    + "capability this request requires",
                    code="compatibility_unknown",
                    param=feature,
                )
            if cell.value == "UNSUPPORTED":
                raise GatewayError.invalid_request(
                    "the selected backend does not support a capability this "
                    + "request requires",
                    code="compatibility_unsupported",
                    param=feature,
                )

    def _enforce_effective_limits(
        self, *, target: RouteTarget, caps: RequestCapabilities, state: _LifecycleState
    ) -> bool:
        """The D-056 effective-capability intersection, per route (#136/D-058).

        Runs AFTER admission selected the exact target, so enforcement is
        route-specific: the request must fit the SELECTED route's
        ``effective = exact-variant hard capability ∩ execution-channel
        capability ∩ administrator allowance``. Administrator ceilings
        stay authoritative (the global pre-check plus their place in the
        intersection); no client request may increase any limit; every
        violation is a typed rejection, never silent clipping.

        The output dimension applies the SAME shared rule the routing core
        applies pre-ranking (:func:`route_output_code`) over the EXACT
        selected variant's proven hard maximum — so enforcement here is
        defense in depth for the composed path, not a divergent rule.
        When the selected channel evidences NO output-limit control
        (``output_limit_control is False`` — today the Codex execution
        surface), an explicitly requested output limit may be NORMALIZED
        AWAY only when it is provably non-binding — at or above that exact
        variant's proven hard maximum — and the normalization is audited
        on every terminal record; a binding request, an unresolved variant
        or an UNKNOWN hard maximum is rejected ``output_limit_unenforceable``
        rather than silently ignored. Channels that map explicit limits or
        whose control fact is UNKNOWN keep the adapters' own
        refuse-not-drop backstops.

        Returns ``True`` when a requested output limit was normalized
        away (the caller dispatches without it and the audit carries the
        ``output_limit_normalized`` note).
        """
        entry = state.target_capabilities
        channel = (
            entry.capabilities if entry is not None else ExecutionCapabilities()
        )
        hard_context, hard_output = _variant_hard_limits(self.catalog, target.model)
        effective_context = _effective_ceiling(
            (hard_context, channel.context_limit_tokens),
            self.limits.max_input_context_tokens,
        )
        if (
            caps.estimated_input_tokens is not None
            and effective_context is not None
            and caps.estimated_input_tokens > effective_context
        ):
            raise GatewayError.invalid_request(
                "the request exceeds the effective input context of the "
                + "selected route",
                code="context_length_exceeded",
            )
        requested = caps.requested_output_tokens
        if requested is None:
            return False
        output_code = route_output_code(
            channel,
            hard_output_tokens=hard_output,
            requested_output_tokens=requested,
            variant_resolved=True,
        )
        if output_code == OUTPUT_LIMIT_INSUFFICIENT:
            raise GatewayError.invalid_request(
                "the requested output exceeds the effective output limit of "
                + "the selected route",
                code="output_limit_exceeded",
                param="max_completion_tokens",
            )
        if output_code == OUTPUT_LIMIT_UNKNOWN:
            raise GatewayError.invalid_request(
                "the selected route has no evidenced output capability to "
                + "satisfy the request's output requirement",
                code="output_limit_unknown",
                param="max_completion_tokens",
            )
        if output_code == OUTPUT_LIMIT_UNENFORCEABLE:
            raise GatewayError.invalid_request(
                "the selected execution channel cannot enforce an output limit; "
                + "a limit below the model's proven maximum is rejected instead "
                + "of being silently ignored",
                code="output_limit_unenforceable",
                param="max_completion_tokens",
            )
        if (
            channel.output_limit_control is False
            and hard_output is not None
            and requested >= hard_output
        ):
            state.limit_notes = (*state.limit_notes, "output_limit_normalized")
            return True
        return False

    def _dispatch(
        self,
        *,
        request: ChatCompletionRequest,
        target: RouteTarget,
        state: _LifecycleState,
        emit_chunk: StreamEmitter | None,
        started: datetime,
        resolved: ResolvedModel,
    ) -> CompletionOutcome:
        try:
            current_registry, administrator, client, current_adapters = self._current_authority(state.client_id)
            snapshot = current_registry.registry_snapshot(now=canonical_instant(self._now()))
        except (CapacityValidationError, ValueError):
            raise GatewayError.api(
                "the gateway's current resource state is unavailable",
                code="state_unavailable", http_status=503,
            ) from None
        current_entry = next((
            entry for entry in snapshot.entries
            if entry.identity.resource_id == target.resource.resource_id
        ), None)
        effective = _effective_authorization(
            administrator, client, resolved.profile,
        )
        if (
            current_entry is None or current_entry.identity != target.resource
            or _authorization_failure_codes(current_entry, effective)
        ):
            raise GatewayError.permission(
                "the client's current authorization or registration no longer covers "
                + "the selected target; inference-only clients require an authorized "
                + "server-direct HTTP resource",
                code="unauthorized_target",
            )
        adapter = current_adapters.resolve(target.resource.channel)
        if adapter is None:
            raise GatewayError.api(
                "no execution adapter is configured for the selected channel",
                code="adapter_unavailable",
                http_status=503,
            )
        try:
            adapter_binding = (
                adapter.continuation_binding(target.resource.resource_id)
                if isinstance(adapter, ContinuationBindingAdapter) else None
            )
        except Exception:
            raise GatewayError.api(
                "the selected resource's current adapter binding is unavailable",
                code="state_unavailable", http_status=503,
            ) from None
        state.adapter_name = adapter.adapter_name
        state.adapter_version = adapter.adapter_version
        deadline = started + timedelta(seconds=self.limits.execution_time_limit_seconds)
        context = self._build_context(
            state,
            emit_chunk,
            deadline,
            register_continuation=(
                self._continuation_registrar(
                    request=request,
                    state=state,
                    deadline=deadline,
                    channel=target.resource.channel,
                    adapter=adapter,
                    adapter_binding=adapter_binding,
                )
                if self.continuations is not None
                and isinstance(adapter, ContinuationCapableAdapter)
                else None
            ),
        )
        state.context = context
        configured_effort = next(
            entry.reasoning_effort for entry in self.catalog.entries
            if entry.identity == target.model
        )
        dispatched_effort = request.reasoning_effort
        controls = _lookup_cell(
            self.compatibility_cells, target.resource, "reasoning_controls"
        )
        native_default = (
            target.resource.channel == "worker_bridged"
            and controls is not None and controls.value in ("PASS", "PARTIAL")
        )
        if (
            (resolved.pinned_target is not None or (native_default and resolved.kind == ALIAS_KIND))
            and dispatched_effort is not None
            and dispatched_effort != configured_effort
        ):
            raise GatewayError.invalid_request(
                "the requested reasoning effort "
                + f"{dispatched_effort!r} conflicts with the selected "
                + f"target's configured effort {configured_effort!r}",
                code="effort_conflicts_with_target",
            )
        if (
            (resolved.kind in (PIN_KIND, LOGICAL_KIND) or target.resource.channel == "worker_bridged")
            and dispatched_effort is None
            and (target.resource.variant is not None or native_default)
        ):
            # Qualified source resources carry catalog effort, not variant text.
            # Native worker calls carry catalog effort independently of opaque
            # configuration; plain HTTP resources keep omitted wire controls.
            dispatched_effort = configured_effort
        state.executed_target = state.selected_target
        call = AdapterCall(
            resource=target.resource,
            model=target.model,
            messages=request.messages,
            stream=request.stream,
            tools=request.tools,
            tool_choice=request.tool_choice,
            response_format=request.response_format,
            reasoning_effort=dispatched_effort,
            # A normalized output limit dispatches WITHOUT the limit (the
            # channel cannot honor it and it was non-binding); the audit
            # carries the output_limit_normalized note.
            max_output_tokens=(
                None
                if state.normalized_output_limit
                else request.capabilities.requested_output_tokens
            ),
            generation_params=request.generation_params or {},
        )
        result = adapter.execute(call, context)
        # D-062: a continuation-capable channel that suspended for a
        # client tool registered the continuation through the context's
        # registrar BEFORE the tool_call id became observable (review
        # round 2, finding 4); a bound failure surfaced as a typed
        # dispatch failure instead. Server-direct adapters' own
        # tool_calls legs never register (they replay history
        # statelessly).
        state.calls = result.calls
        if result.status == "cancelled":
            if context.cancelled:
                raise ClientDisconnectedError()
            raise AdapterTimeoutError(result_note(result))
        if result.status == "failed":
            raise AdapterPermanentError(result_note(result))
        accounting = _aggregate_usage(result)
        message = result.message
        finish_reason = result.finish_reason
        if message is None or finish_reason is None:  # pragma: no cover - contract
            raise AdapterPermanentError("adapter returned an incomplete result")
        return CompletionOutcome(
            request_id=state.request_id,
            created=int(started.timestamp()),
            model_echo=request.model,
            message=message,
            finish_reason=finish_reason,
            usage=accounting,
            decision_id=state.decision_id,
        )

    def _build_context(
        self,
        state: _LifecycleState,
        emit_chunk: StreamEmitter | None,
        deadline: datetime,
        register_continuation: "Callable[[SuspensionHandle, str], bool] | None" = None,
    ) -> ExecutionContext:
        """Build the dispatch context; the emitter propagates disconnects.

        ``register_continuation`` is the D-062 seam (review round 2,
        finding 4): the continuation-capable adapter invokes it at the
        suspension point, BEFORE the tool_call id becomes observable
        anywhere.
        """
        if emit_chunk is None:
            return ExecutionContext(
                request_id=state.request_id,
                deadline=canonical_instant(deadline),
                register_continuation=register_continuation,
            )

        def emit(chunk: AdapterStreamChunk) -> None:
            assert state.context is not None
            if state.context.cancelled:
                raise ClientDisconnectedError()
            try:
                emit_chunk(chunk)
            except ClientDisconnectedError:
                state.context.cancel_event.set()
                raise

        return ExecutionContext(
            request_id=state.request_id,
            deadline=canonical_instant(deadline),
            emit_chunk=emit,
            register_continuation=register_continuation,
        )

    # ── Audit ────────────────────────────────────────────────────────────

    def _write_audit(
        self,
        state: _LifecycleState,
        result_status: str,
        reason_codes: tuple[str, ...],
    ) -> None:
        reported, estimated = _usage_totals(state.calls)
        record = AuditRecord(
            request_id=state.request_id,
            client_id=state.client_id,
            decision_id=state.decision_id,
            routing_profile=state.routing_profile,
            routing_policy_version=state.routing_policy_version,
            registry_revision=state.registry_revision,
            registry_generated_at=state.registry_generated_at,
            selected_target=state.selected_target,
            executed_target=state.executed_target,
            adapter_name=state.adapter_name,
            adapter_version=state.adapter_version,
            started_at=state.started_at,
            ended_at=canonical_instant(self._now()),
            result_status=result_status,
            # Terminal provenance for pre-dispatch transformations (D-058):
            # ``state.limit_notes`` carries e.g. ``output_limit_normalized``
            # on EVERY terminal record written after the normalization
            # happened (completed, failed, failed-ambiguous, timed out,
            # cancelled, unexpected failure after dispatch). Notes are
            # appended only once normalization has actually occurred, so
            # requests rejected BEFORE it are never tagged; the note is an
            # additional reason code and never replaces the primary
            # failure reason.
            reason_codes=tuple(reason_codes)
            + tuple(state.limit_notes)
            + tuple(state.flow_notes),
            provider_reported_usage=reported,
            estimated_usage=estimated,
            call_count=len(state.calls),
        )
        self.audit.append(record)


# ── Lifecycle bookkeeping ─────────────────────────────────────────────────────


class _LifecycleState:
    """Mutable per-execution bookkeeping for the audit record."""

    __slots__: tuple[str, ...] = (
        "request_id",
        "client_id",
        "started_at",
        "decision_id",
        "routing_profile",
        "routing_policy_version",
        "registry_revision",
        "registry_generated_at",
        "selected_target",
        "executed_target",
        "adapter_name",
        "adapter_version",
        "context",
        "calls",
        "target",
        "target_capabilities",
        "limit_notes",
        "normalized_output_limit",
        "flow_notes",
    )

    def __init__(self, *, request_id: str, client_id: str, started_at: str) -> None:
        self.request_id: str = request_id
        self.client_id: str = client_id
        self.started_at: str = started_at
        self.decision_id: str | None = None
        self.routing_profile: str | None = None
        self.routing_policy_version: int | None = None
        self.registry_revision: int | None = None
        self.registry_generated_at: str | None = None
        self.selected_target: ExecutedTarget | None = None
        self.executed_target: ExecutedTarget | None = None
        self.adapter_name: str | None = None
        self.adapter_version: str | None = None
        self.context: ExecutionContext | None = None
        self.calls: tuple[CallObservation, ...] = ()
        self.target: RouteTarget | None = None
        self.target_capabilities: ResourceRegistryEntry | None = None
        self.limit_notes: tuple[str, ...] = ()
        self.normalized_output_limit: bool = False
        #: D-062 flow provenance notes (appended to the audit reason
        #: codes): ``suspended_for_client_tool``,
        #: ``continuation_resumed``, ``continuation_unavailable``.
        self.flow_notes: tuple[str, ...] = ()


def _request_id(factory: RequestFactory | None) -> str:
    if factory is not None:
        return v_safe_id(factory(), "request_id")
    return f"chatcmpl-{uuid.uuid4().hex}"


def _unavailable_usage() -> UsageAccounting:
    """The honest accounting of a leg whose provider call is still open.

    A tool_calls leg ends one HTTP response but NOT the backend turn:
    no call observation exists yet (the single observation arrives with
    the turn's terminal result), so the usage source is ``unavailable``
    — structural absence, never a fabricated zero.
    """
    return UsageAccounting(usage_source=USAGE_SOURCE_UNAVAILABLE)


def _audit_target(target: RouteTarget) -> ExecutedTarget:
    return ExecutedTarget(
        resource_id=target.resource.resource_id,
        provider=target.resource.provider,
        model=target.model.model,
        variant=target.model.variant,
    )


def _target_capabilities(
    snapshot: RegistrySnapshot, target: RouteTarget
) -> ResourceRegistryEntry | None:
    """The registry entry of the selected target, from admission's snapshot.

    Admission reads exactly one snapshot; the effective-limits gate uses
    the SAME snapshot's registration-owned capability facts (never a
    second, later read).
    """
    for entry in snapshot.entries:
        if entry.identity == target.resource:
            return entry
    return None


def _no_eligible_target_error(decision: RouteDecision) -> GatewayError:
    """The typed failure for an unpinned decision with no selected target.

    The output 400 is reserved for decisions caused PURELY by the output
    dimension (D-058): every target exclusion in the decision must be a
    compatibility-stage exclusion whose reason codes are exactly output
    codes, AND the embedded selection decision must show no candidate
    excluded for a non-output reason — concretely, no D-059 policy-blackout
    exclusion (``exclusion_stage == "policy_blackout"`` / a blocked
    ``blackout_decision``), which lives at the selector level and never
    appears in the gate-level exclusion records. If any route was excluded
    for any OTHER reason — availability, authorization, binding, a
    different compatibility feature, or D-059 campaign policy — the
    request could succeed unchanged once that other cause clears, so the
    honest surface is the ordinary 503 ``no_eligible_target`` with its
    availability-style semantics, never an actionable "change your output
    request" 400. Priority within the pure case matches
    :func:`_admission_rejection` (insufficient > unenforceable > unknown).
    """
    output_codes: set[str] = {
        "output_limit_unknown",
        "output_limit_insufficient",
        "output_limit_unenforceable",
    }
    exclusions = decision.target_exclusions
    purely_output = bool(exclusions) and all(
        exclusion.reason_codes
        and set(exclusion.reason_codes) <= output_codes
        for exclusion in exclusions
    )
    if purely_output:
        # D-059 interplay: a selector-level policy blackout never reaches
        # the gate exclusions, so a pure-output gate set can still be a
        # mixed cause overall. Any blocked candidate in the embedded
        # selection decision downgrades the classification to the generic
        # 503 — the request may simply need the campaign to end.
        selection = decision.selection
        candidates = tuple(selection.excluded) + tuple(
            selection.closest_candidates
        )
        if any(
            candidate.blackout_decision is not None
            and candidate.blackout_decision.blocked
            for candidate in candidates
        ):
            purely_output = False
    if purely_output:
        seen: set[str] = set()
        for exclusion in exclusions:
            seen |= set(exclusion.reason_codes)
        if "output_limit_insufficient" in seen:
            code = "output_limit_insufficient"
        elif "output_limit_unenforceable" in seen:
            code = "output_limit_unenforceable"
        else:
            code = "output_limit_unknown"
        return GatewayError.invalid_request(
            "no execution route can satisfy this request's output "
            + "requirements",
            code=code,
        )
    return GatewayError.api(
        "no authorized execution target satisfies this request",
        code="no_eligible_target",
        http_status=503,
    )


def _admission_rejection(admission: AdmissionDecision) -> GatewayError:
    """Map an admission rejection to its explicit execution-surface error."""
    codes = set(admission.reason_codes)
    if "pin_target_not_found" in codes:
        return GatewayError.not_found(
            "the pinned execution target does not exist",
            code="pin_target_not_found",
        )
    if "pinned_model_not_bound" in codes:
        return GatewayError.invalid_request(
            "the pinned model identity is no longer bound by the pinned "
            + "resource; obtain a fresh recommendation instead",
            code="pinned_model_not_bound",
        )
    if "capability_unassessed" in codes:
        return GatewayError.not_found(
            "the pinned target has no calibrated capability binding",
            code="capability_unassessed",
        )
    if codes & {
        "unauthorized_provider",
        "unauthorized_channel",
        "unauthorized_entitlement",
        "resource_blocked",
    }:
        return GatewayError.permission(
            "the pinned target is not authorized for this client",
            code="unauthorized_target",
        )
    spend_codes = codes & {"spend_limit_exceeded", "spend_limit_unverifiable"}
    if spend_codes:
        return GatewayError.permission(
            "the pinned target violates the applicable spending limit",
            code="spend_limit_exceeded"
            if "spend_limit_exceeded" in spend_codes
            else "spend_limit_unverifiable",
        )
    availability = codes & {
        "resource_stale",
        "resource_never_observed",
        "resource_unhealthy",
        "execution_ineligible",
    }
    if availability:
        return GatewayError.api(
            "the pinned target is not currently available",
            code="target_unavailable",
            http_status=503,
        )
    if "compatibility_unsupported" in codes:
        return GatewayError.invalid_request(
            "the pinned target does not support a capability this request "
            + "requires",
            code="compatibility_unsupported",
        )
    if "compatibility_unknown" in codes:
        return GatewayError.invalid_request(
            "the pinned target has no evidenced support for a capability "
            + "this request requires",
            code="compatibility_unknown",
        )
    context_codes = codes & {"context_limit_unknown", "context_limit_insufficient"}
    if context_codes:
        return GatewayError.invalid_request(
            "the pinned target cannot satisfy the request's context "
            + "requirements",
            code="context_limit_insufficient"
            if "context_limit_insufficient" in context_codes
            else "context_limit_unknown",
        )
    output_codes = codes & {
        "output_limit_unknown",
        "output_limit_insufficient",
        "output_limit_unenforceable",
    }
    if output_codes:
        return GatewayError.invalid_request(
            "the pinned target cannot satisfy the request's output "
            + "requirements",
            code="output_limit_insufficient"
            if "output_limit_insufficient" in output_codes
            else (
                "output_limit_unenforceable"
                if "output_limit_unenforceable" in output_codes
                else "output_limit_unknown"
            ),
        )
    return GatewayError.api(
        "the pinned target was rejected at admission",
        code="admission_rejected",
        http_status=503,
    )


def result_note(result: AdapterResult) -> str:
    """A safe one-line note from a failed result, bounded."""
    for call in reversed(result.calls):
        if call.note is not None:
            return call.note[:200]
    return "the backend reported a failure"


def _usage_totals(
    calls: tuple[CallObservation, ...],
) -> tuple[UsageTokens | None, UsageTokens | None]:
    reported: UsageTokens | None = None
    estimated: UsageTokens | None = None
    for call in calls:
        if call.provider_reported_usage is not None:
            reported = (
                call.provider_reported_usage
                if reported is None
                else reported + call.provider_reported_usage
            )
        if call.estimated_usage is not None:
            estimated = (
                call.estimated_usage
                if estimated is None
                else estimated + call.estimated_usage
            )
    return reported, estimated


def _aggregate_usage(result: AdapterResult) -> UsageAccounting:
    reported, estimated = _usage_totals(result.calls)
    if reported is not None and estimated is not None:
        source = USAGE_SOURCE_MIXED
    elif reported is not None:
        source = USAGE_SOURCE_PROVIDER_REPORTED
    elif estimated is not None:
        source = USAGE_SOURCE_ESTIMATED
    else:
        source = USAGE_SOURCE_UNAVAILABLE
    return UsageAccounting(
        usage_source=source,
        provider_reported_usage=reported,
        estimated_usage=estimated,
    )


class _ConcurrencyReservation:
    """The bounded post-admission concurrency reservation (D-043).

    Acquires the global execution slot and the per-client slot
    non-blockingly; exhaustion is an immediate explicit 429 — a gateway
    never queues unbounded work behind a saturated backend. ``release``
    runs exactly once, in the lifecycle's ``finally``.
    """

    def __init__(
        self,
        *,
        semaphore: threading.BoundedSemaphore,
        client_lock: threading.Lock,
        client_active: dict[str, int],
        client_id: str,
    ) -> None:
        self._semaphore: threading.BoundedSemaphore = semaphore
        self._client_lock: threading.Lock = client_lock
        self._client_active: dict[str, int] = client_active
        self._client_id: str = client_id
        self._global_acquired: bool = False
        self._client_acquired: bool = False

    @classmethod
    def acquire(
        cls,
        *,
        semaphore: threading.BoundedSemaphore,
        client_lock: threading.Lock,
        client_active: dict[str, int],
        per_client_limit: int,
        client_id: str,
    ) -> "_ConcurrencyReservation":
        reservation = cls(
            semaphore=semaphore,
            client_lock=client_lock,
            client_active=client_active,
            client_id=client_id,
        )
        if not semaphore.acquire(blocking=False):
            raise GatewayError.rate_limit(
                "the gateway is at its concurrent-execution limit",
                code="concurrency_limit_reached",
            )
        reservation._global_acquired = True
        with client_lock:
            active = client_active.get(client_id, 0)
            if active >= per_client_limit:
                semaphore.release()
                reservation._global_acquired = False
                raise GatewayError.rate_limit(
                    "the client is at its concurrent-execution limit",
                    code="concurrency_limit_reached",
                )
            client_active[client_id] = active + 1
            reservation._client_acquired = True
        return reservation

    def release(self) -> None:
        if self._client_acquired:
            with self._client_lock:
                active = self._client_active.get(self._client_id, 0)
                if active <= 1:
                    _ = self._client_active.pop(self._client_id, None)
                else:
                    self._client_active[self._client_id] = active - 1
                self._client_acquired = False
        if self._global_acquired:
            self._semaphore.release()
            self._global_acquired = False


__all__ = [
    "ALIAS_KIND",
    "PIN_KIND",
    "PIN_PREFIX",
    "CapacitySource",
    "GatewayApplication",
    "ResolvedModel",
    "RoutingAliasTable",
    "parse_pinned_reference",
    "resolve_model_string",
]
