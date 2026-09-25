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
    AdapterPermanentError,
    AdapterRegistry,
    AdapterResult,
    AdapterStreamChunk,
    AdapterTimeoutError,
    AdapterAmbiguousError,
    CallObservation,
    ClientDisconnectedError,
    CompletionOutcome,
    ExecutionContext,
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
from .gateway_openai import ChatCompletionRequest, RequestCapabilities
from .resource_state import ResourceRegistry
from .gateway_validation import v_instance, v_int, v_safe_id
from .routing_core import (
    AdministratorConstraints,
    AdmissionDecision,
    ClientAuthorization,
    ClientRoutingProfile,
    CompatibilityCell,
    PinnedTarget,
    RequestBinding,
    RouteRequest,
    RouteTarget,
    _lookup_cell,  # pyright: ignore[reportPrivateUsage] -- the M02 matrix lookup is the single authority; reimplementing it here would fork D-043 compatibility semantics
    admit_pinned_target,
    route_request,
)
from .selector import canonical_instant
from .selector import SelectorPolicy
from .selection_types import ModelCatalog, ModelIdentity, TaskProfileCatalog

# ── Model-field resolution ────────────────────────────────────────────────────

PIN_PREFIX = "sr-pin:"

PIN_KIND = "pin"
ALIAS_KIND = "alias"


@dataclass(frozen=True)
class ResolvedModel:
    """The one resolution of a client ``model`` string (D-042 layers 4/5)."""

    kind: str
    alias: str | None = None
    profile: ClientRoutingProfile | None = None
    pinned_target: PinnedTarget | None = None


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


def resolve_model_string(model: str, aliases: RoutingAliasTable) -> ResolvedModel:
    """Resolve the client ``model`` field: alias or exact pinned reference.

    Anything else is ``model_not_found``: the execution surface invents no
    implicit routing and never guesses a bare model name into a target.
    """
    if model.startswith(PIN_PREFIX):
        return ResolvedModel(
            kind=PIN_KIND, pinned_target=parse_pinned_reference(model)
        )
    profile = aliases.resolve(model)
    if profile is None:
        raise GatewayError.not_found(
            "the requested model does not exist on this gateway",
            code="model_not_found",
        )
    return ResolvedModel(kind=ALIAS_KIND, alias=model, profile=profile)


# ── The application (server configuration + runtime state) ────────────────────

CapacitySource = Callable[
    [str], tuple[tuple[CapacitySnapshot, ...], tuple[ExecutionEligibility, ...]]
]
RequestFactory = Callable[[], str]
StreamEmitter = Callable[[AdapterStreamChunk], None]


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
        clock: Callable[[], datetime] | None = None,
        request_id_factory: RequestFactory | None = None,
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
        self.clock: Callable[[], datetime] | None = clock
        self.request_id_factory: RequestFactory | None = request_id_factory
        # Daybreak finding 5: concurrency enforcement must SURVIVE
        # application rebuilds (a state-report adoption rebuilds the
        # application while executions are in flight). The reservation
        # state is carried over from the replaced application whenever
        # the limits are unchanged, so in-flight executions keep their
        # accounting and limits cannot be reset by a rebuild.
        previous = (
            replaced_application
            if isinstance(replaced_application, GatewayApplication)
            else None
        )
        if (
            previous is not None
            and previous.limits.max_concurrent_executions
            == self.limits.max_concurrent_executions
            and previous.limits.max_concurrent_executions_per_client
            == self.limits.max_concurrent_executions_per_client
        ):
            self._global_slots: threading.BoundedSemaphore = (
                previous._global_slots  # pyright: ignore[reportPrivateUsage] - deliberate carry-over
            )
            self._client_lock: threading.Lock = previous._client_lock  # pyright: ignore[reportPrivateUsage] - deliberate carry-over
            self._client_active: dict[str, int] = previous._client_active  # pyright: ignore[reportPrivateUsage] - deliberate carry-over
        else:
            self._global_slots: threading.BoundedSemaphore = threading.BoundedSemaphore(
                self.limits.max_concurrent_executions
            )
            self._client_lock: threading.Lock = threading.Lock()
            self._client_active: dict[str, int] = {}

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
        if self.client_authorizations is None:
            return ClientAuthorization()
        return self.client_authorizations.get(client_id, ClientAuthorization())

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
        resolved = resolve_model_string(request.model, self.aliases)
        self._admit(started=started, request=request, resolved=resolved, state=state)
        target = state.target
        if target is None:  # pragma: no cover - admission sets it or raises
            raise GatewayError.api(
                "admission produced no target", code="invalid_state"
            )
        self._supplemental_capability_gate(target, request.capabilities)
        reservation = self._acquire_reservation(client_id)
        try:
            return self._dispatch(
                request=request, target=target, state=state,
                emit_chunk=emit_chunk, started=started, resolved=resolved,
            )
        finally:
            reservation.release()

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
            pinned_target=resolved.pinned_target,
        )
        routing_profile = resolved.profile
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
            return
        decision = route_request(request_obj)
        state.decision_id = decision.decision_id
        if decision.status != "selected" or decision.target is None:
            raise GatewayError.api(
                "no authorized execution target satisfies this request",
                code="no_eligible_target",
                http_status=503,
            )
        state.target = decision.target
        state.selected_target = _audit_target(decision.target)

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
        adapter = self.adapters.resolve(target.resource.channel)
        if adapter is None:
            raise GatewayError.api(
                "no execution adapter is configured for the selected channel",
                code="adapter_unavailable",
                http_status=503,
            )
        state.adapter_name = adapter.adapter_name
        state.adapter_version = adapter.adapter_version
        deadline = started + timedelta(seconds=self.limits.execution_time_limit_seconds)
        context = self._build_context(state, emit_chunk, deadline)
        state.context = context
        # Exact-execution discipline (D-042/D-053, Daybreak finding 2):
        # a PINNED request is admission-only — the pinned variant is part
        # of the execution contract, so a conflicting request effort is a
        # typed rejection before dispatch, never a silent downgrade while
        # the audit records the selected variant. Non-pinned profile
        # requests keep the carried-control semantics pinned by the
        # existing contract (the effort is a request control; the variant
        # is the catalog configuration identity).
        selected_variant = target.model.variant
        dispatched_effort = request.reasoning_effort
        if (
            resolved.pinned_target is not None
            and selected_variant is not None
        ):
            if (
                dispatched_effort is not None
                and dispatched_effort != selected_variant
            ):
                # Before executed_target is recorded: nothing dispatched,
                # so the audit stays a REJECTION with no executed target.
                raise GatewayError.invalid_request(
                    "the requested reasoning effort "
                    + f"{dispatched_effort!r} conflicts with the pinned "
                    + f"target's effort {selected_variant!r}",
                    code="effort_conflicts_with_target",
                )
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
            max_output_tokens=request.capabilities.requested_output_tokens,
            generation_params=request.generation_params or {},
        )
        result = adapter.execute(call, context)
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
    ) -> ExecutionContext:
        """Build the dispatch context; the emitter propagates disconnects."""
        if emit_chunk is None:
            return ExecutionContext(
                request_id=state.request_id,
                deadline=canonical_instant(deadline),
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
            reason_codes=reason_codes,
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


def _request_id(factory: RequestFactory | None) -> str:
    if factory is not None:
        return v_safe_id(factory(), "request_id")
    return f"chatcmpl-{uuid.uuid4().hex}"


def _audit_target(target: RouteTarget) -> ExecutedTarget:
    return ExecutedTarget(
        resource_id=target.resource.resource_id,
        provider=target.resource.provider,
        model=target.model.model,
        variant=target.model.variant,
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
