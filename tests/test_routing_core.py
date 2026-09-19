"""Route-decision contract tests for executable targets (M02, issue #87).

These tests construct the typed route-decision world directly (the contract
in ``scarcity_router/routing_core.py``, D-042) over a synthetic catalog,
synthetic v3 capacity and a synthetic M01 registry, and pin the invariants
issue #87 requires: deterministic decisions and decision ids, the five
D-042 target dimensions, the frozen authorization precedence (client
options can never expand authorization or spending limits), explicit
model/effort/target pins that are honored or explicitly failed, fail-closed
stale/never-observed/unhealthy worker state, D-039 eligibility gating with
absence-never-means-eligible semantics, confirmed shared quota pools that
are never independent capacity, expired promotions that never contribute
preference, tool-calling compatibility that fails closed, the
recommendation-to-execution admission binding without re-ranking, and
explicit no-solution results. Deterministic and self-contained: no live
providers, no network, no subprocess, no clock access; all fixtures are
synthetic.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import cast

from scarcity_router.capacity import CapacityDiagnostic, CapacitySnapshot, CapacityWindow
from scarcity_router.eligibility import ExecutionEligibility
from scarcity_router.errors import (
    RouteContractValidationError,
    SelectionContractValidationError,
)
from scarcity_router.resource_state import (
    RESOURCE_STATE_SCHEMA_VERSION,
    ExecutionCapabilities,
    PromotionObservation,
    QuotaFact,
    ResourceCost,
    ResourceHealth,
    ResourceIdentity,
    ResourceRegistration,
    ResourceRegistry,
    ResourceStateSnapshot,
    RegistrySnapshot,
)
from scarcity_router.routing_core import (
    ROUTE_STATUS_NO_SOLUTION,
    ROUTE_STATUS_SELECTED,
    AdministratorConstraints,
    ClientAuthorization,
    ClientRoutingProfile,
    CompatibilityCell,
    PinnedTarget,
    RequestBinding,
    RouteDecision,
    RouteRequest,
    SpendingLimit,
    TargetExclusion,
    admit_pinned_target,
    route_request,
)
from scarcity_router.selector import neutral_selector_policy
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
    ModelRef,
    TaskProfileCatalog,
    TaskProfileDefinition,
    TaskRequirement,
)

# ── synthetic instants ────────────────────────────────────────────────────────

T_OBS = "2026-09-15T12:04:00.000Z"
T_EVAL = "2026-09-15T12:05:00.000Z"
T_STALE_NOW = "2026-09-15T12:10:00.000Z"
EVAL_AT = datetime(2026, 9, 15, 12, 5, tzinfo=timezone.utc)
STALE_AT = datetime(2026, 9, 15, 12, 10, tzinfo=timezone.utc)
PROMO_ACTIVE_UNTIL = "2026-09-15T13:05:00.000Z"
PROMO_EXPIRED_UNTIL = "2026-09-15T12:04:59.000Z"
PROMO_VALID_FROM = "2026-09-01T00:00:00.000Z"

TTL = 300


def now_minus_60(canonical_now: str) -> str:
    """One minute before a canonical instant, in the same canonical shape."""
    moment = datetime.strptime(canonical_now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    return (
        (moment - timedelta(seconds=60))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


EVIDENCE = EvidenceRef(
    source="official_docs", identifier="synthetic://cell-evidence", date="2026-09-01"
)


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
    tool_use: int = 3,
    scope: str,
    effort: str | None = None,
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
        reasoning_effort=effort,
    )


def _catalog() -> ModelCatalog:
    return ModelCatalog(
        catalog_version=1,
        updated_on="2026-09-01",
        entries=(
            _entry(
                "openai",
                "gpt-5.6-luna",
                "max",
                reasoning=4,
                coding=4,
                scope="codex",
                effort="max",
            ),
            _entry(
                "openai",
                "gpt-5.6-luna",
                "medium",
                reasoning=3,
                coding=3,
                scope="codex",
                effort="medium",
            ),
            _entry(
                "openai",
                "gpt-5.6-terra",
                "medium",
                reasoning=2,
                coding=2,
                scope="terra_scope",
                effort="medium",
            ),
            _entry(
                "zai",
                "glm-5.3",
                "high",
                reasoning=4,
                coding=4,
                scope="coding_plan",
                effort="high",
            ),
        ),
    )


def _profiles() -> TaskProfileCatalog:
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
            TaskProfileDefinition(
                profile_id="openai-only",
                requirement=TaskRequirement(
                    task_level="L2",
                    capability_minima=CapabilityMinima(reasoning=3, coding=3),
                    hard_constraints=HardConstraints(required_provider="openai"),
                ),
            ),
        )
    )


CORE_PROFILE = ClientRoutingProfile(profile_id="gateway-core")


# ── capacity v3 snapshots ─────────────────────────────────────────────────────


def _snap(
    provider: str,
    *,
    five: int = 50,
    weekly: int = 50,
    extra_scopes: dict[str, int] | None = None,
) -> CapacitySnapshot:
    scope = "codex" if provider == "openai" else "coding_plan"
    windows: list[CapacityWindow] = [
        CapacityWindow(
            resource="tokens",
            kind="five_hour",
            scope_id=scope,
            duration_seconds=18_000,
            used_percent=100 - five,
            remaining_percent=five,
            window_id=f"{provider}-five",
        ),
        CapacityWindow(
            resource="tokens",
            kind="weekly",
            scope_id=scope,
            duration_seconds=604_800,
            used_percent=100 - weekly,
            remaining_percent=weekly,
            window_id=f"{provider}-weekly",
        ),
    ]
    for extra_scope, remaining in (extra_scopes or {}).items():
        windows.append(
            CapacityWindow(
                resource="tokens",
                kind="weekly",
                scope_id=extra_scope,
                duration_seconds=604_800,
                used_percent=100 - remaining,
                remaining_percent=remaining,
                window_id=f"{provider}-{extra_scope}",
            )
        )
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=T_EVAL,
        status="ok",
        windows=tuple(windows),
        diagnostics=(),
    )


def _exhausted_snap(provider: str, scope: str) -> CapacitySnapshot:
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=T_EVAL,
        status="ok",
        windows=(
            CapacityWindow(
                resource="tokens",
                kind="five_hour",
                scope_id=scope,
                duration_seconds=18_000,
                used_percent=100,
                remaining_percent=0,
                window_id=f"{provider}-five",
            ),
        ),
        diagnostics=(),
    )


# ── registry snapshot ─────────────────────────────────────────────────────────


def _identity(
    resource_id: str,
    channel: str,
    provider: str,
    model: str,
    entitlement: str,
    *,
    variant: str | None = None,
    pools: tuple[str, ...] = (),
) -> ResourceIdentity:
    return ResourceIdentity(
        resource_id=resource_id,
        channel=channel,
        provider=provider,
        model=model,
        entitlement=entitlement,
        variant=variant,
        quota_pool_ids=pools,
    )


def _observation(
    identity: ResourceIdentity,
    *,
    observed_at: str = T_OBS,
    status: str = "ok",
    promotions: tuple[PromotionObservation, ...] = (),
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
        schema_version=RESOURCE_STATE_SCHEMA_VERSION,
        identity=identity,
        observed_at=observed_at,
        health=ResourceHealth(status=status, diagnostics=diagnostics),
        quota_facts=(
            QuotaFact(
                observation_class="provider_telemetry",
                window=CapacityWindow(
                    resource="tokens",
                    kind="five_hour",
                    scope_id="codex",
                    duration_seconds=18_000,
                    used_percent=6,
                    remaining_percent=94,
                ),
            ),
        ),
        promotions=promotions,
    )


def _registration(
    identity: ResourceIdentity,
    *,
    context_limit: int | None = 272_000,
    cost: ResourceCost | None = None,
) -> ResourceRegistration:
    return ResourceRegistration(
        identity=identity,
        freshness_ttl_seconds=TTL,
        capabilities=ExecutionCapabilities(context_limit_tokens=context_limit),
        cost=cost,
    )


def _registry_snapshot(
    registrations: list[ResourceRegistration],
    observations: dict[str, ResourceStateSnapshot],
    *,
    now: str = T_EVAL,
) -> RegistrySnapshot:
    registry = ResourceRegistry(clock=lambda: now)
    for registration in registrations:
        registry.register(registration)
    for snapshot in observations.values():
        registry.apply_snapshot(snapshot)
    return registry.registry_snapshot(now=now)


def _default_registry(*, now: str = T_EVAL) -> RegistrySnapshot:
    """The default healthy world: two openai surfaces, two zai surfaces,
    one worker-bridged openai surface and one uncalibrated ollama surface."""
    openai_sub = _identity(
        "openai-sub", "local_app_adapter", "openai", "gpt-5.6-luna", "subscription_included"
    )
    openai_payg = _identity(
        "openai-payg", "server_direct_http", "openai", "gpt-5.6-luna", "payg_metered"
    )
    openai_worker = _identity(
        "openai-worker", "worker_bridged", "openai", "gpt-5.6-luna", "subscription_included"
    )
    zai_alt = _identity(
        "zai-alt",
        "server_direct_http",
        "zai",
        "glm-5.3",
        "subscription_included",
        pools=("zai-shared",),
    )
    zai_sub = _identity(
        "zai-sub",
        "server_direct_http",
        "zai",
        "glm-5.3",
        "subscription_included",
        pools=("zai-shared",),
    )
    ollama = _identity(
        "lab-ollama", "worker_bridged", "ollama", "qwen3-coder", "local_ungated"
    )
    identities = [openai_sub, openai_payg, openai_worker, zai_alt, zai_sub, ollama]
    return _registry_snapshot(
        [_registration(identity) for identity in identities],
        {identity.resource_id: _observation(identity) for identity in identities},
        now=now,
    )


PAYG_COST = ResourceCost(
    observation_class="provider_telemetry",
    input_micro_usd_per_mtoken=5_000_000,
    output_micro_usd_per_mtoken=20_000_000,
)


def _pass_cell(channel: str, provider: str, model: str, feature: str) -> CompatibilityCell:
    return CompatibilityCell(
        channel=channel,
        provider=provider,
        model=model,
        feature=feature,
        value="PASS",
        adapter="synthetic-adapter",
        adapter_version="1.0.0",
        evidence=EVIDENCE,
    )


def _tool_cells(
    *,
    server_direct: str | None = "PASS",
    local_app: str | None = "PASS",
    worker: str | None = "PASS",
    zai_direct: str | None = "PASS",
) -> tuple[CompatibilityCell, ...]:
    def cell(
        channel: str, provider: str, model: str, value: str | None
    ) -> CompatibilityCell | None:
        if value is None:
            return None
        return CompatibilityCell(
            channel=channel,
            provider=provider,
            model=model,
            feature="tool_calls",
            value=value,
            adapter="synthetic-adapter",
            adapter_version="1.0.0",
            evidence=EVIDENCE,
        )

    return tuple(
        cell
        for cell in (
            cell("server_direct_http", "openai", "gpt-5.6-luna", server_direct),
            cell("local_app_adapter", "openai", "gpt-5.6-luna", local_app),
            cell("worker_bridged", "openai", "gpt-5.6-luna", worker),
            cell("server_direct_http", "zai", "glm-5.3", zai_direct),
        )
        if cell is not None
    )


def _request(
    *,
    registry: RegistrySnapshot | None = None,
    snapshots: tuple[CapacitySnapshot, ...] | None = None,
    evaluated_at: datetime = EVAL_AT,
    profiles: TaskProfileCatalog | None = None,
    routing_profile: ClientRoutingProfile | None = CORE_PROFILE,
    request: RequestBinding | None = None,
    admin: AdministratorConstraints | None = None,
    client: ClientAuthorization | None = None,
    cells: tuple[CompatibilityCell, ...] = (),
    reports: tuple[ExecutionEligibility, ...] = (),
) -> RouteRequest:
    return RouteRequest(
        catalog=_catalog(),
        profiles=profiles if profiles is not None else _profiles(),
        registry_snapshot=registry if registry is not None else _default_registry(),
        policy=neutral_selector_policy(),
        evaluated_at=evaluated_at,
        capacity_snapshots=snapshots
        if snapshots is not None
        else (_snap("openai"), _snap("zai")),
        eligibility_reports=reports,
        compatibility_cells=cells,
        admin_constraints=admin if admin is not None else AdministratorConstraints(),
        client_authorization=client if client is not None else ClientAuthorization(),
        routing_profile=routing_profile,
        request=request if request is not None else RequestBinding(),
    )


def _exclusion_by_id(
    decision: RouteDecision,
) -> dict[str, TargetExclusion]:
    return {exclusion.resource_id: exclusion for exclusion in decision.target_exclusions}


# ── determinism and decision identity ─────────────────────────────────────────


class DeterminismTests(unittest.TestCase):
    def test_identical_inputs_produce_identical_decision_and_id(self) -> None:
        first = route_request(_request())
        second = route_request(_request())
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.decision_id, second.decision_id)
        decision_id = cast(str, first.decision_id)
        self.assertTrue(decision_id.startswith("rd-"))
        self.assertEqual(len(decision_id), 35)

    def test_different_evaluation_instant_changes_the_decision_id(self) -> None:
        first = route_request(_request())
        moved = route_request(
            _request(
                registry=_default_registry(now="2026-09-15T12:05:01.000Z"),
                evaluated_at=EVAL_AT + timedelta(seconds=1),
            )
        )
        self.assertNotEqual(first.decision_id, moved.decision_id)

    def test_caller_supplied_decision_id_is_preserved(self) -> None:
        decision = route_request(_request())
        supplied = RouteDecision(
            status=decision.status,
            selection=decision.selection,
            registry_revision=decision.registry_revision,
            registry_generated_at=decision.registry_generated_at,
            target=decision.target,
            target_alternatives=decision.target_alternatives,
            unroutable_identities=decision.unroutable_identities,
            target_exclusions=decision.target_exclusions,
            expired_promotions=decision.expired_promotions,
            pinned=decision.pinned,
            reason_codes=decision.reason_codes,
            decision_id="custom-decision-id",
        )
        self.assertEqual(supplied.decision_id, "custom-decision-id")
        # The derived id is content-addressed: identical content derives the
        # identical id regardless of construction path.
        self.assertEqual(
            RouteDecision(
                status=decision.status,
                selection=decision.selection,
                registry_revision=decision.registry_revision,
                registry_generated_at=decision.registry_generated_at,
                target=decision.target,
                target_alternatives=decision.target_alternatives,
                unroutable_identities=decision.unroutable_identities,
                target_exclusions=decision.target_exclusions,
                expired_promotions=decision.expired_promotions,
                pinned=decision.pinned,
                reason_codes=decision.reason_codes,
            ).decision_id,
            decision.decision_id,
        )

    def test_serialization_is_json_clean_and_typed_inputs_round_trip(self) -> None:
        decision = route_request(
            _request(request=RequestBinding(requires_tool_calls=True), cells=_tool_cells())
        )
        rendered = json.dumps(decision.to_dict(), sort_keys=True)
        self.assertIn(decision.decision_id, rendered)
        typed_inputs = (
            AdministratorConstraints(
                allowed_providers=("openai",),
                allowed_channels=("server_direct_http",),
                allowed_entitlements=("payg_metered",),
                blocked_resource_ids=("openai-worker",),
                spend_limit=SpendingLimit(micro_usd_per_mtoken_max=1),
            ),
            ClientAuthorization(
                allowed_providers=("openai",),
                blocked_resource_ids=("openai-sub",),
                spend_limit=SpendingLimit(micro_usd_per_mtoken_max=2),
            ),
            ClientRoutingProfile(profile_id="gateway-core", allowed_providers=("openai",)),
            RequestBinding(
                requires_tool_calls=True,
                minimum_input_context_tokens=1000,
                maximum_output_tokens=2000,
                explicit_model=ModelRef(provider="openai", model="gpt-5.6-luna"),
                explicit_variant="max",
                pinned_target=PinnedTarget(resource_id="openai-sub"),
            ),
            PinnedTarget(resource_id="openai-sub", decision_id="rd-" + "0" * 32),
            SpendingLimit(micro_usd_per_mtoken_max=10),
            _pass_cell("server_direct_http", "openai", "gpt-5.6-luna", "tool_calls"),
        )
        for value in typed_inputs:
            self.assertEqual(value.from_dict(value.to_dict()), value)
        for exclusion in decision.target_exclusions:
            self.assertEqual(TargetExclusion.from_dict(exclusion.to_dict()), exclusion)


# ── the five D-042 target dimensions and provenance ──────────────────────────


class DecisionShapeTests(unittest.TestCase):
    def test_decision_separates_the_five_target_dimensions(self) -> None:
        decision = route_request(
            _request(snapshots=(_snap("openai"), _snap("zai", five=90, weekly=90)))
        )
        self.assertEqual(decision.status, ROUTE_STATUS_SELECTED)
        target = decision.target
        assert target is not None
        # Physical model/variant: the calibrated catalog identity.
        self.assertEqual(
            (target.model.provider, target.model.model, target.model.variant),
            ("zai", "glm-5.3", "high"),
        )
        # Execution channel/surface + executable-target reference.
        self.assertEqual(target.resource.channel, "server_direct_http")
        self.assertEqual(target.resource.resource_id, "zai-alt")
        # Entitlement.
        self.assertEqual(target.resource.entitlement, "subscription_included")
        # Confirmed quota pools.
        self.assertEqual(target.quota_pools, ("zai-shared",))
        # Client routing profile.
        self.assertEqual(target.routing_profile, "gateway-core")
        self.assertEqual(
            decision.registry_generated_at, "2026-09-15T12:05:00.000Z"
        )
        self.assertGreaterEqual(decision.registry_revision, 1)

    def test_embedded_selection_matches_the_route_target(self) -> None:
        decision = route_request(_request())
        assert decision.target is not None
        selected = decision.selection.selected
        assert selected is not None
        self.assertEqual(selected.identity, decision.target.model)
        self.assertEqual(
            decision.selection.requirement.capability_minima.reasoning, 3
        )
        self.assertEqual(
            decision.selection.requirement.capability_minima.coding, 3
        )
        self.assertEqual(decision.selection.profile_id, "gateway-core")

    def test_unroutable_identities_report_target_narrowing(self) -> None:
        admin = AdministratorConstraints(allowed_providers=("zai",))
        decision = route_request(_request(admin=admin))
        unroutable = {
            (identity.provider, identity.model, identity.variant)
            for identity in decision.unroutable_identities
        }
        self.assertIn(("openai", "gpt-5.6-luna", "max"), unroutable)
        self.assertIn(("openai", "gpt-5.6-terra", "medium"), unroutable)
        self.assertNotIn(("zai", "glm-5.3", "high"), unroutable)
        ollama_exclusion = _exclusion_by_id(decision)["lab-ollama"]
        self.assertEqual(ollama_exclusion.stage, "binding")
        self.assertEqual(ollama_exclusion.reason_codes, ("capability_unassessed",))

    def test_unbound_resource_is_never_routed(self) -> None:
        decision = route_request(_request())
        self.assertIsNotNone(decision.target)
        excluded = _exclusion_by_id(decision)
        self.assertIn("lab-ollama", excluded)
        self.assertEqual(excluded["lab-ollama"].stage, "binding")


# ── authorization precedence ──────────────────────────────────────────────────


class AuthorizationTests(unittest.TestCase):
    def test_client_authorization_cannot_expand_administrator_constraints(self) -> None:
        admin = AdministratorConstraints(allowed_providers=("openai",))
        client = ClientAuthorization(allowed_providers=("openai", "zai"))
        decision = route_request(_request(admin=admin, client=client))
        assert decision.target is not None
        self.assertEqual(decision.target.resource.provider, "openai")
        excluded = _exclusion_by_id(decision)
        for resource_id in ("zai-alt", "zai-sub"):
            self.assertIn("unauthorized_provider", excluded[resource_id].reason_codes)
            self.assertEqual(excluded[resource_id].stage, "authorization")

    def test_blocked_resource_ids_union_across_layers(self) -> None:
        admin = AdministratorConstraints(blocked_resource_ids=("openai-sub",))
        client = ClientAuthorization(blocked_resource_ids=("openai-payg",))
        decision = route_request(_request(admin=admin, client=client))
        excluded = _exclusion_by_id(decision)
        self.assertEqual(
            excluded["openai-sub"].reason_codes, ("resource_blocked",)
        )
        self.assertEqual(
            excluded["openai-payg"].reason_codes, ("resource_blocked",)
        )

    def test_channel_and_entitlement_narrowing(self) -> None:
        admin = AdministratorConstraints(
            allowed_channels=("server_direct_http",),
            allowed_entitlements=("subscription_included",),
        )
        decision = route_request(_request(admin=admin))
        excluded = _exclusion_by_id(decision)
        self.assertIn(
            "unauthorized_channel", excluded["openai-sub"].reason_codes
        )
        self.assertIn(
            "unauthorized_entitlement", excluded["openai-payg"].reason_codes
        )

    def test_spending_limit_gates_metered_surfaces_fail_closed(self) -> None:
        admin = AdministratorConstraints(
            spend_limit=SpendingLimit(micro_usd_per_mtoken_max=0)
        )
        registry = _registry_snapshot(
            [
                _registration(
                    _identity(
                        "openai-payg",
                        "server_direct_http",
                        "openai",
                        "gpt-5.6-luna",
                        "payg_metered",
                    ),
                    cost=PAYG_COST,
                ),
                _registration(
                    _identity(
                        "lab-box",
                        "worker_bridged",
                        "ollama",
                        "qwen3-coder",
                        "local_ungated",
                    ),
                    cost=None,
                ),
            ],
            {},
            now=T_EVAL,
        )
        decision = route_request(_request(registry=registry, admin=admin))
        excluded = _exclusion_by_id(decision)
        self.assertEqual(
            excluded["openai-payg"].reason_codes, ("spend_limit_exceeded",)
        )
        # The local/ungated surface has no marginal monetary spend: a
        # spending limit cannot exclude it (it fails the binding stage
        # first — uncalibrated backends are excluded honestly).
        self.assertEqual(excluded["lab-box"].stage, "binding")

    def test_metered_surface_without_cost_record_is_unverifiable(self) -> None:
        admin = AdministratorConstraints(
            spend_limit=SpendingLimit(micro_usd_per_mtoken_max=10_000_000)
        )
        decision = route_request(_request(admin=admin))
        excluded = _exclusion_by_id(decision)
        self.assertEqual(
            excluded["openai-payg"].reason_codes, ("spend_limit_unverifiable",)
        )

    def test_client_limit_cannot_loosen_the_administrator_limit(self) -> None:
        payg = _identity(
            "openai-payg", "server_direct_http", "openai", "gpt-5.6-luna", "payg_metered"
        )
        registry = _registry_snapshot(
            [_registration(payg, cost=PAYG_COST)],
            {"openai-payg": _observation(payg)},
            now=T_EVAL,
        )
        admin = AdministratorConstraints(
            spend_limit=SpendingLimit(micro_usd_per_mtoken_max=0)
        )
        client = ClientAuthorization(
            spend_limit=SpendingLimit(micro_usd_per_mtoken_max=99_000_000)
        )
        decision = route_request(
            _request(
                registry=registry,
                snapshots=(_snap("openai"),),
                admin=admin,
                client=client,
            )
        )
        excluded = _exclusion_by_id(decision)
        self.assertEqual(
            excluded["openai-payg"].reason_codes, ("spend_limit_exceeded",)
        )

    def test_profile_alias_requires_the_configured_routing_profile(self) -> None:
        with self.assertRaises(RouteContractValidationError):
            _ = _request(
                routing_profile=None,
                request=RequestBinding(profile_alias="gateway-core"),
            )

    def test_unknown_profile_fails_explicitly(self) -> None:
        with self.assertRaises(SelectionContractValidationError):
            _ = route_request(
                _request(routing_profile=ClientRoutingProfile(profile_id="missing"))
            )

    def test_request_tightens_but_never_loosens_the_profile_requirement(self) -> None:
        decision = route_request(
            _request(
                request=RequestBinding(
                    requires_tool_calls=True,
                    minimum_input_context_tokens=150_000,
                    maximum_output_tokens=8_000,
                )
            )
        )
        requirement = decision.selection.requirement
        self.assertEqual(requirement.capability_minima.reasoning, 3)
        self.assertEqual(requirement.hard_constraints.requires_tool_use, True)
        self.assertEqual(
            requirement.hard_constraints.minimum_input_context_tokens, 150_000
        )
        self.assertEqual(requirement.hard_constraints.minimum_output_tokens, 8_000)
        self.assertEqual(requirement.task_level, "L2")


# ── execution-surface availability: fail closed ───────────────────────────────


def _world_with_worker(
    *, status: str = "ok", now: str = T_EVAL, observe: bool = True
) -> tuple[RegistrySnapshot, ResourceIdentity]:
    """A two-resource world: one worker-bridged surface plus one healthy
    local-app surface, so worker-state tests can prove fail-closed routing
    to the healthy alternative (or the explicit no-solution)."""
    worker = _identity(
            "openai-worker",
            "worker_bridged",
            "openai",
            "gpt-5.6-luna",
            "subscription_included",
        )
    others = [
        _identity(
            "openai-sub",
            "local_app_adapter",
            "openai",
            "gpt-5.6-luna",
            "subscription_included",
        )
    ]
    registrations = [_registration(identity) for identity in [worker, *others]]
    observations: dict[str, ResourceStateSnapshot] = {}
    if observe:
        observations[worker.resource_id] = _observation(worker, status=status)
    for identity in others:
        # Observed shortly before the evaluation instant even when that
        # instant is far enough ahead to stale the worker's observation.
        observations[identity.resource_id] = _observation(
            identity, observed_at=now_minus_60(now)
        )
    return _registry_snapshot(registrations, observations, now=now), worker


class AvailabilityTests(unittest.TestCase):
    def test_stale_worker_is_blocked_and_never_read_as_healthy(self) -> None:
        registry, _worker = _world_with_worker(now=T_STALE_NOW)
        decision = route_request(
            _request(registry=registry, evaluated_at=STALE_AT)
        )
        excluded = _exclusion_by_id(decision)
        self.assertEqual(excluded["openai-worker"].reason_codes, ("resource_stale",))
        self.assertEqual(excluded["openai-worker"].freshness, "stale")
        self.assertEqual(excluded["openai-worker"].stage, "availability")
        assert decision.target is not None
        self.assertEqual(decision.target.resource.resource_id, "openai-sub")

    def test_unhealthy_worker_is_blocked_with_its_health_status(self) -> None:
        registry, _worker = _world_with_worker(status="schema_changed")
        decision = route_request(_request(registry=registry))
        excluded = _exclusion_by_id(decision)
        worker_exclusion = excluded["openai-worker"]
        self.assertIn("resource_unhealthy", worker_exclusion.reason_codes)
        self.assertEqual(worker_exclusion.health_status, "schema_changed")

    def test_never_observed_worker_is_an_honest_unknown(self) -> None:
        registry, _worker = _world_with_worker(observe=False)
        decision = route_request(_request(registry=registry))
        excluded = _exclusion_by_id(decision)
        self.assertEqual(
            excluded["openai-worker"].reason_codes, ("resource_never_observed",)
        )

    def test_only_unavailable_worker_is_an_explicit_no_solution(self) -> None:
        worker = _identity(
            "openai-worker",
            "worker_bridged",
            "openai",
            "gpt-5.6-luna",
            "subscription_included",
        )
        registry = _registry_snapshot(
            [_registration(worker)],
            {worker.resource_id: _observation(worker, status="unavailable")},
            now=T_EVAL,
        )
        decision = route_request(
            _request(registry=registry, snapshots=(_snap("openai"),))
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        self.assertIsNone(decision.target)
        self.assertEqual(decision.reason_codes, ("no_eligible_target",))
        self.assertEqual(
            _exclusion_by_id(decision)["openai-worker"].reason_codes,
            ("resource_unhealthy",),
        )
        self.assertEqual(
            decision.selection.reason_codes, ("no_eligible_candidate",)
        )

    def test_d039_report_blocks_its_provider_and_absence_applies_no_stage(self) -> None:
        report = ExecutionEligibility(
            schema_version=1,
            provider="openai",
            source="codex_app_server",
            retrieved_at=T_OBS,
            state="policy_blocked",
            reason_codes=("purchased_credits_present",),
        )
        decision = route_request(_request(reports=(report,)))
        excluded = _exclusion_by_id(decision)
        for resource_id in ("openai-sub", "openai-payg", "openai-worker"):
            self.assertEqual(excluded[resource_id].stage, "availability")
            self.assertIn("execution_ineligible", excluded[resource_id].reason_codes)
            paired_report = excluded[resource_id].execution_eligibility
            assert paired_report is not None
            self.assertEqual(
                paired_report.reason_codes,
                ("purchased_credits_present",),
            )
        # zai has NO report: the execution stage never applies to it — it is
        # never read as "eligible" and never read as blocked.
        assert decision.target is not None
        self.assertEqual(decision.target.resource.provider, "zai")

    def test_decision_never_precedes_its_state_snapshot(self) -> None:
        with self.assertRaises(RouteContractValidationError):
            _ = _request(
                registry=_default_registry(now=T_EVAL),
                evaluated_at=EVAL_AT - timedelta(seconds=1),
            )


# ── request compatibility: tool calling and context ──────────────────────────


class CompatibilityTests(unittest.TestCase):
    def test_tool_calling_requirement_routes_to_a_compatible_surface(self) -> None:
        decision = route_request(
            _request(
                request=RequestBinding(requires_tool_calls=True),
                cells=_tool_cells(
                    server_direct="PASS", local_app="UNSUPPORTED", worker=None
                ),
            )
        )
        assert decision.target is not None
        # openai-payg sorts first by id and passes; the subscription surface
        # explicitly does not support tool calls and is never silently used.
        self.assertEqual(decision.target.resource.resource_id, "openai-payg")
        excluded = _exclusion_by_id(decision)
        sub = excluded["openai-sub"]
        self.assertEqual(sub.stage, "compatibility")
        self.assertEqual(sub.reason_codes, ("compatibility_unsupported",))
        self.assertEqual(sub.compatibility_feature, "tool_calls")
        self.assertEqual(sub.compatibility_value, "UNSUPPORTED")
        worker = excluded["openai-worker"]
        self.assertEqual(worker.reason_codes, ("compatibility_unknown",))
        self.assertIsNone(worker.compatibility_value)

    def test_missing_cells_fail_closed_to_an_explicit_no_solution(self) -> None:
        decision = route_request(
            _request(request=RequestBinding(requires_tool_calls=True), cells=())
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        excluded = _exclusion_by_id(decision)
        for resource_id in ("openai-sub", "openai-payg", "zai-alt", "zai-sub"):
            self.assertEqual(
                excluded[resource_id].reason_codes, ("compatibility_unknown",)
            )
        self.assertIn("no_eligible_target", decision.reason_codes)

    def test_unknown_cell_fails_closed_like_a_missing_cell(self) -> None:
        decision = route_request(
            _request(
                request=RequestBinding(requires_tool_calls=True),
                cells=_tool_cells(zai_direct="UNKNOWN"),
            )
        )
        excluded = _exclusion_by_id(decision)
        self.assertEqual(
            excluded["zai-alt"].reason_codes, ("compatibility_unknown",)
        )
        assert decision.target is not None
        self.assertNotEqual(decision.target.resource.provider, "zai")

    def test_context_minimum_uses_the_registration_fact_fail_closed(self) -> None:
        zai_identity = _identity(
            "zai-sub", "server_direct_http", "zai", "glm-5.3", "subscription_included"
        )
        zai_only = _registry_snapshot(
            [_registration(zai_identity, context_limit=128_000)],
            {"zai-sub": _observation(zai_identity)},
            now=T_EVAL,
        )
        decision = route_request(
            _request(
                registry=zai_only,
                snapshots=(_snap("zai"),),
                request=RequestBinding(minimum_input_context_tokens=150_000),
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        excluded = _exclusion_by_id(decision)
        self.assertEqual(
            excluded["zai-sub"].reason_codes, ("context_limit_insufficient",)
        )

    def test_unknown_context_limit_fails_closed(self) -> None:
        zai_identity = _identity(
            "zai-sub", "server_direct_http", "zai", "glm-5.3", "subscription_included"
        )
        zai_only = _registry_snapshot(
            [_registration(zai_identity, context_limit=None)],
            {"zai-sub": _observation(zai_identity)},
            now=T_EVAL,
        )
        decision = route_request(
            _request(
                registry=zai_only,
                snapshots=(_snap("zai"),),
                request=RequestBinding(minimum_input_context_tokens=150_000),
            )
        )
        excluded = _exclusion_by_id(decision)
        self.assertEqual(
            excluded["zai-sub"].reason_codes, ("context_limit_unknown",)
        )


# ── quota pools and promotions ────────────────────────────────────────────────


class QuotaPoolAndPromotionTests(unittest.TestCase):
    def test_shared_quota_pool_is_not_independent_capacity(self) -> None:
        decision = route_request(
            _request(snapshots=(_exhausted_snap("openai", "codex"), _snap("zai")))
        )
        assert decision.target is not None
        self.assertEqual(decision.target.resource.resource_id, "zai-alt")
        self.assertEqual(decision.target.quota_pools, ("zai-shared",))
        self.assertEqual(len(decision.target_alternatives), 1)
        alternative = decision.target_alternatives[0]
        self.assertEqual(alternative.resource.resource_id, "zai-sub")
        self.assertEqual(alternative.quota_pools, ("zai-shared",))
        self.assertTrue(alternative.shares_quota_pool_with_selected)
        # A pool-mate draws one budget with the selected target: the decision
        # never presents it as independent fallback capacity.
        self.assertIn("shares_quota_pool_with_selected", alternative.to_dict())

    def test_unshared_alternative_is_not_marked_as_sharing(self) -> None:
        decision = route_request(_request())
        assert decision.target is not None
        # The selected zai target's alternatives are zai pool-mates only;
        # verify with openai surfaces instead where no pools exist.
        openai_decision = route_request(
            _request(
                snapshots=(_snap("openai"), _exhausted_snap("zai", "coding_plan")),
                admin=AdministratorConstraints(allowed_providers=("openai", "zai")),
            )
        )
        assert openai_decision.target is not None
        self.assertEqual(
            openai_decision.target.resource.resource_id, "openai-payg"
        )
        for alternative in openai_decision.target_alternatives:
            self.assertFalse(alternative.shares_quota_pool_with_selected)

    def test_active_promotion_contributes_target_preference(self) -> None:
        zai_a = _identity(
            "zai-a", "server_direct_http", "zai", "glm-5.3", "subscription_included"
        )
        zai_b = _identity(
            "zai-b", "server_direct_http", "zai", "glm-5.3", "subscription_included"
        )
        promotion = PromotionObservation(
            source="zai-summer-promo",
            observed_at=T_OBS,
            channel="server_direct_http",
            provider="zai",
            model="glm-5.3",
            valid_from=PROMO_VALID_FROM,
            valid_until=PROMO_ACTIVE_UNTIL,
        )
        registry = _registry_snapshot(
            [_registration(zai_a), _registration(zai_b)],
            {
                "zai-a": _observation(zai_a),
                "zai-b": _observation(zai_b, promotions=(promotion,)),
            },
            now=T_EVAL,
        )
        decision = route_request(
            _request(registry=registry, snapshots=(_snap("zai"),))
        )
        assert decision.target is not None
        self.assertEqual(decision.target.resource.resource_id, "zai-b")
        self.assertEqual(decision.target.promotion_sources, ("zai-summer-promo",))
        self.assertEqual(decision.expired_promotions, ())
        self.assertEqual(len(decision.target_alternatives), 1)
        self.assertEqual(
            decision.target_alternatives[0].resource.resource_id, "zai-a"
        )

    def test_expired_promotion_never_contributes_preference(self) -> None:
        zai_a = _identity(
            "zai-a", "server_direct_http", "zai", "glm-5.3", "subscription_included"
        )
        zai_b = _identity(
            "zai-b", "server_direct_http", "zai", "glm-5.3", "subscription_included"
        )
        expired = PromotionObservation(
            source="zai-summer-promo",
            observed_at=T_OBS,
            channel="server_direct_http",
            provider="zai",
            model="glm-5.3",
            valid_from=PROMO_VALID_FROM,
            valid_until=PROMO_EXPIRED_UNTIL,
        )
        registry = _registry_snapshot(
            [_registration(zai_a), _registration(zai_b)],
            {
                "zai-a": _observation(zai_a),
                "zai-b": _observation(zai_b, promotions=(expired,)),
            },
            now=T_EVAL,
        )
        decision = route_request(
            _request(registry=registry, snapshots=(_snap("zai"),))
        )
        assert decision.target is not None
        # Without an active preference the stable resource id decides.
        self.assertEqual(decision.target.resource.resource_id, "zai-a")
        self.assertEqual(decision.target.promotion_sources, ())
        self.assertEqual(decision.expired_promotions, ("zai-summer-promo",))

    def test_plan_scoped_promotion_is_never_matched(self) -> None:
        zai_a = _identity(
            "zai-a", "server_direct_http", "zai", "glm-5.3", "subscription_included"
        )
        zai_b = _identity(
            "zai-b", "server_direct_http", "zai", "glm-5.3", "subscription_included"
        )
        unverifiable = PromotionObservation(
            source="zai-plan-promo",
            observed_at=T_OBS,
            plan="coding_plan",
            valid_until=PROMO_ACTIVE_UNTIL,
        )
        registry = _registry_snapshot(
            [_registration(zai_a), _registration(zai_b)],
            {
                "zai-a": _observation(zai_a),
                "zai-b": _observation(zai_b, promotions=(unverifiable,)),
            },
            now=T_EVAL,
        )
        decision = route_request(
            _request(registry=registry, snapshots=(_snap("zai"),))
        )
        assert decision.target is not None
        self.assertEqual(decision.target.resource.resource_id, "zai-a")
        self.assertEqual(decision.target.promotion_sources, ())


# ── pins: honored or explicitly failed ────────────────────────────────────────


class PinTests(unittest.TestCase):
    def _first_decision_id(self) -> str:
        return cast(str, route_request(_request()).decision_id)

    def test_explicit_target_pin_is_honored_exactly(self) -> None:
        prior = self._first_decision_id()
        decision = route_request(
            _request(
                request=RequestBinding(
                    pinned_target=PinnedTarget(
                        resource_id="openai-worker", decision_id=prior
                    )
                )
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_SELECTED)
        self.assertTrue(decision.pinned)
        assert decision.target is not None
        self.assertEqual(decision.target.resource.resource_id, "openai-worker")
        self.assertEqual(decision.target.resource.channel, "worker_bridged")
        self.assertEqual(decision.target_alternatives, ())
        self.assertEqual(decision.reason_codes, ("route_selected",))

    def test_violating_target_pin_fails_explicitly_without_substitution(self) -> None:
        # The pinned worker is stale; the healthy surfaces must NOT be
        # silently selected instead.
        registry, _worker = _world_with_worker(now=T_STALE_NOW)
        decision = route_request(
            _request(
                registry=registry,
                evaluated_at=STALE_AT,
                request=RequestBinding(
                    pinned_target=PinnedTarget(resource_id="openai-worker")
                ),
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        self.assertIsNone(decision.target)
        self.assertTrue(decision.pinned)
        self.assertIn("pinned_request_failed", decision.reason_codes)
        self.assertEqual(
            _exclusion_by_id(decision)["openai-worker"].reason_codes,
            ("resource_stale",),
        )

    def test_unknown_target_pin_fails_explicitly(self) -> None:
        decision = route_request(
            _request(
                request=RequestBinding(
                    pinned_target=PinnedTarget(resource_id="no-such-resource")
                )
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        self.assertIn("pin_target_not_found", decision.reason_codes)
        self.assertIn("pinned_request_failed", decision.reason_codes)

    def test_unauthorized_target_pin_fails_explicitly(self) -> None:
        decision = route_request(
            _request(
                admin=AdministratorConstraints(
                    blocked_resource_ids=("openai-sub",)
                ),
                request=RequestBinding(
                    pinned_target=PinnedTarget(resource_id="openai-sub")
                ),
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        self.assertIn("pinned_request_failed", decision.reason_codes)
        self.assertEqual(
            _exclusion_by_id(decision)["openai-sub"].reason_codes,
            ("resource_blocked",),
        )

    def test_model_pin_is_honored_with_target_level_choice(self) -> None:
        decision = route_request(
            _request(
                request=RequestBinding(
                    explicit_model=ModelRef(provider="openai", model="gpt-5.6-luna")
                )
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_SELECTED)
        assert decision.target is not None
        self.assertEqual(
            (decision.target.model.provider, decision.target.model.model),
            ("openai", "gpt-5.6-luna"),
        )
        # No minima advantage between the two variants under the profile, so
        # the margin tie-break prefers the smaller configuration; the pin is
        # honored either way and the target level stays competitive.
        self.assertEqual(decision.target.model.variant, "medium")

    def test_effort_pin_is_honored(self) -> None:
        decision = route_request(
            _request(
                request=RequestBinding(
                    explicit_model=ModelRef(
                        provider="openai", model="gpt-5.6-luna"
                    ),
                    explicit_variant="max",
                )
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_SELECTED)
        assert decision.target is not None
        self.assertEqual(decision.target.model.variant, "max")

    def test_pin_contradicting_the_profile_fails_explicitly(self) -> None:
        decision = route_request(
            _request(
                routing_profile=ClientRoutingProfile(profile_id="openai-only"),
                request=RequestBinding(
                    explicit_model=ModelRef(provider="zai", model="glm-5.3")
                ),
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        self.assertIn("pinned_request_failed", decision.reason_codes)
        # The failure is explained at the model level: the pinned identity
        # cannot satisfy the profile's provider constraint.
        excluded = {
            (c.identity.provider, c.identity.model, c.identity.variant): c
            for c in decision.selection.excluded
        }
        terra_failure = excluded[("zai", "glm-5.3", "high")]
        self.assertEqual(terra_failure.exclusion_stage, "hard_constraint")
        self.assertEqual(
            terra_failure.hard_constraint_failures[0].constraint,
            "required_provider",
        )

    def test_unknown_model_pin_fails_explicitly(self) -> None:
        decision = route_request(
            _request(
                request=RequestBinding(
                    explicit_model=ModelRef(provider="zai", model="no-such-model")
                )
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        self.assertIn("pin_model_not_found", decision.reason_codes)


# ── capability/scarcity discipline preserved ─────────────────────────────────


class ScoringDisciplineTests(unittest.TestCase):
    def test_quota_state_never_raises_capability(self) -> None:
        # Terra has plentiful dedicated capacity and a healthy compatible
        # surface; Luna is scarce. The capability minima still exclude Terra.
        luna = _identity(
            "openai-sub", "local_app_adapter", "openai", "gpt-5.6-luna", "subscription_included"
        )
        terra = _identity(
            "terra-box", "server_direct_http", "openai", "gpt-5.6-terra", "subscription_included"
        )
        registry = _registry_snapshot(
            [_registration(luna), _registration(terra)],
            {"openai-sub": _observation(luna), "terra-box": _observation(terra)},
            now=T_EVAL,
        )
        decision = route_request(
            _request(
                registry=registry,
                snapshots=(
                    _snap("openai", five=5, weekly=5, extra_scopes={"terra_scope": 100}),
                    _exhausted_snap("zai", "coding_plan"),
                ),
            )
        )
        assert decision.target is not None
        self.assertEqual(decision.target.model.model, "gpt-5.6-luna")
        excluded = {
            (c.identity.provider, c.identity.model, c.identity.variant): c
            for c in decision.selection.excluded
        }
        terra = excluded[("openai", "gpt-5.6-terra", "medium")]
        self.assertEqual(terra.exclusion_stage, "capability")
        self.assertEqual(terra.capability_failures[0].reason, "below_minimum")

    def test_no_solution_is_explicit_with_closest_candidates(self) -> None:
        hard_profile = TaskProfileCatalog(
            definitions=(
                TaskProfileDefinition(
                    profile_id="impossible",
                    requirement=TaskRequirement(
                        task_level="L5",
                        capability_minima=CapabilityMinima(
                            reasoning=5,
                            coding=5,
                            scientific_methodological=5,
                            writing_editorial=5,
                            tool_use=5,
                            translation_multilingual=5,
                        ),
                        hard_constraints=HardConstraints(),
                    ),
                ),
            )
        )
        decision = route_request(
            _request(
                routing_profile=ClientRoutingProfile(profile_id="impossible"),
                profiles=hard_profile,
            )
        )
        self.assertEqual(decision.status, ROUTE_STATUS_NO_SOLUTION)
        self.assertIsNone(decision.target)
        self.assertEqual(decision.selection.reason_codes, ("no_eligible_candidate",))
        self.assertTrue(decision.selection.closest_candidates)
        self.assertTrue(decision.target_exclusions)


# ── recommendation-to-execution binding (admission only) ─────────────────────


class AdmissionBindingTests(unittest.TestCase):
    def test_admission_approves_a_non_winner_without_re_ranking(self) -> None:
        decision = route_request(_request())
        assert decision.target is not None
        winner = decision.target.resource.resource_id
        # zai-sub is not the ranking winner (zai-alt sorts first) but passes
        # every gate: admission approves it — no second routing decision.
        loser = "zai-sub"
        self.assertNotEqual(winner, loser)
        admission = admit_pinned_target(
            _request(),
            resource_id=loser,
            decision_id=cast(str, decision.decision_id),
        )
        self.assertTrue(admission.approved)
        assert admission.target is not None
        self.assertEqual(admission.target.resource.resource_id, loser)
        self.assertEqual(
            admission.target.model,
            ModelIdentity(provider="zai", model="glm-5.3", variant="high"),
        )
        self.assertEqual(
            admission.bound_decision_id, decision.decision_id
        )
        self.assertEqual(admission.reason_codes, ("admission_approved",))

    def test_admission_rejects_a_stale_target_fail_closed(self) -> None:
        registry, _worker = _world_with_worker(now=T_STALE_NOW)
        admission = admit_pinned_target(
            _request(registry=registry, evaluated_at=STALE_AT),
            resource_id="openai-worker",
        )
        self.assertFalse(admission.approved)
        self.assertIsNone(admission.target)
        assert admission.exclusion is not None
        self.assertEqual(
            admission.exclusion.reason_codes, ("resource_stale",)
        )
        self.assertEqual(
            admission.reason_codes, ("admission_rejected", "resource_stale")
        )

    def test_admission_rejects_an_unknown_reference(self) -> None:
        admission = admit_pinned_target(_request(), resource_id="no-such-resource")
        self.assertFalse(admission.approved)
        self.assertIn("pin_target_not_found", admission.reason_codes)
        self.assertIsNone(admission.exclusion)

    def test_admission_enforces_authorization_and_limits(self) -> None:
        admission = admit_pinned_target(
            _request(admin=AdministratorConstraints(allowed_providers=("openai",))),
            resource_id="zai-sub",
        )
        self.assertFalse(admission.approved)
        assert admission.exclusion is not None
        self.assertEqual(admission.exclusion.stage, "authorization")
        self.assertIn("unauthorized_provider", admission.reason_codes)

    def test_admission_is_deterministic(self) -> None:
        first = admit_pinned_target(_request(), resource_id="openai-sub")
        second = admit_pinned_target(_request(), resource_id="openai-sub")
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_admission_serialization_round_trips_through_json(self) -> None:
        approved = admit_pinned_target(_request(), resource_id="openai-sub")
        rendered = json.dumps(approved.to_dict(), sort_keys=True)
        self.assertIn("admission_approved", rendered)
        rejected = admit_pinned_target(_request(), resource_id="no-such-resource")
        self.assertIn(
            "pin_target_not_found",
            json.dumps(rejected.to_dict(), sort_keys=True),
        )


if __name__ == "__main__":
    _ = unittest.main()
