"""Logical model resolution + discovery (D-055, issue #134, program #132).

A representative OpenAI-compatible client selects a real model by its
bare id: resolution order ``sr-pin:`` → alias → logical adopted model →
``model_not_found``; a logical request narrows routing to the EXACT
``(provider, model, effort)`` identity and never substitutes another
model; discovery exposes adopted logical models honestly (restricted and
unclassified never appear; an empty deployment exposes nothing; the
effective context ceiling is the model/channel intersection or UNKNOWN).

The composed world here is the D-053 unit path: a real ``SourceRegistry``
classifies a real inventory against the reviewed track artifact, the
derived registrations materialize into a ``ResourceRegistry``, and the
merged (base + floor) catalog drives the gateway — dispatch through a
deterministic ``worker_bridged`` adapter. The full composed-stack
acceptance (real worker protocol) remains in
``tests/test_e2e_sources_acceptance.py``.
"""

from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from typing import cast, override

from scarcity_router.capacity import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
)
from scarcity_router.eligibility import ExecutionEligibility
from scarcity_router.execution_sources import SourceRegistry
from scarcity_router.gateway_adapters import AdapterRegistry
from scarcity_router.gateway_audit import BoundedAuditTrail
from scarcity_router.gateway_audit import ExecutedTarget
from scarcity_router.gateway_contracts import ClientKeyDirectory, GatewayError, GatewayLimits
from scarcity_router.gateway_coordinator import (
    GatewayApplication,
    RoutingAliasTable,
    exposed_logical_models,
    resolve_model_string,
)
from scarcity_router.gateway_server import GatewayHTTPServer, make_gateway_server
from scarcity_router.model_inventory import (
    DiscoveredModel,
    ModelInventoryReport,
    SourceInventory,
)
from scarcity_router.model_tracks import load_track_registry
from scarcity_router.resource_state import (
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
    ClientRoutingProfile,
    CompatibilityCell,
)
from scarcity_router.selector import neutral_selector_policy
from scarcity_router.selection_app import DEFAULT_CATALOG_PATH, load_catalog
from scarcity_router.selection_types import (
    CapabilityAssessment,
    CapabilityAssessments,
    CapacityScopeRef,
    EvidenceRef,
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
)
from scarcity_router.providers.openai_http_core import build_chat_completion_request
from scarcity_router.providers.openai_http_presets import OPENAI_API_PRESET, ZAI_CODING_PLAN_PRESET
from scarcity_router.server_config import ServerConfiguration, SourceConfig
from scarcity_router.server_composition import build_compatibility_cells
from tests.gateway_fixtures import (
    CLIENT_ID,
    CLIENT_KEY,
    ScriptedAdapter,
    audit_records,
    build_aliases,
    build_profiles,
    make_application,
    parse_chat_request,
    make_sequential_request_ids,
)
from tests.test_gateway_server import ServerHarness, as_dict, as_list

OBSERVED = "2026-09-24T12:00:00.000Z"
DATE = "2026-09-24"
EVALUATED = datetime(2026, 9, 24, 12, 5, tzinfo=timezone.utc)
EVIDENCE = EvidenceRef(
    source="official_docs", identifier="synthetic://logical-model", date="2026-09-24"
)
FEATURES = (
    "roles_history",
    "streaming",
    "tool_calls",
    "tool_results",
    "structured_output",
    "reasoning_controls",
)


# ── Derived-source world ──────────────────────────────────────────────────────


def _source_config(source_id: str) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        kind="codex_subscription",
        label=f"Source {source_id}",
        worker_id="worker-1",
    )


def _inventory(
    models: tuple[DiscoveredModel, ...],
    *,
    source_id: str = "personal-openai",
) -> ModelInventoryReport:
    return ModelInventoryReport(
        worker_id="worker-1",
        sources=(
            SourceInventory(
                source_id=source_id,
                adapter_id=f"codex:{source_id}",
                kind="codex_subscription",
                observed_at=OBSERVED,
                auth_state="authenticated",
                runtime_name="codex",
                runtime_version="0.155.0",
                models=models,
            ),
        ),
    )


def _healthy_snapshot(
    identity: ResourceIdentity, *, status: str = "ok"
) -> ResourceStateSnapshot:
    diagnostics: tuple[CapacityDiagnostic, ...] = ()
    if status != "ok":
        diagnostics = (CapacityDiagnostic(code="source_unavailable"),)
    return ResourceStateSnapshot(
        schema_version=1,
        identity=identity,
        observed_at=OBSERVED,
        health=ResourceHealth(status=status, diagnostics=diagnostics),
        quota_facts=(
            QuotaFact(
                observation_class="provider_telemetry",
                window=CapacityWindow(
                    resource="tokens",
                    kind="five_hour",
                    scope_id="codex",
                    duration_seconds=18_000,
                    used_percent=50,
                    remaining_percent=50,
                ),
            ),
        ),
        promotions=(),
    )


def _pass_cells(provider: str, model: str) -> tuple[CompatibilityCell, ...]:
    return tuple(
        CompatibilityCell(
            channel="worker_bridged",
            provider=provider,
            model=model,
            feature=feature,
            value="PASS",
            adapter="synthetic-worker",
            adapter_version="1.2.3",
            evidence=EVIDENCE,
        )
        for feature in FEATURES
    )


def derived_world(
    models: tuple[DiscoveredModel, ...],
    *,
    source_ids: tuple[str, ...] = ("personal-openai",),
) -> tuple[ResourceRegistry, ModelCatalog]:
    """A real D-053 adoption world: classified inventory → derived
    registrations → healthy registry, plus the merged floor catalog."""
    sources = SourceRegistry(track_registry=load_track_registry())
    sources.sync_configuration(tuple(_source_config(sid) for sid in source_ids))
    for source_id in source_ids:
        _ = sources.apply_inventory(_inventory(models, source_id=source_id))
    registry = ResourceRegistry(clock=lambda: OBSERVED)
    for registration in sources.derived_registrations():
        registry.register(registration)
        _ = registry.apply_snapshot(_healthy_snapshot(registration.identity))
    return registry, sources.derived_catalog_entries(load_catalog(DEFAULT_CATALOG_PATH))


def make_derived_application(
    registry: ResourceRegistry,
    catalog: ModelCatalog,
    *,
    aliases: dict[str, ClientRoutingProfile] | None = None,
    channel: str = "worker_bridged",
) -> GatewayApplication:
    """One derived-world application: a ``worker_bridged`` echo adapter,
    PASS cells for every registered identity, the given alias table."""
    adapter_registry = AdapterRegistry()
    adapter_registry.register(ScriptedAdapter(channel=channel))
    cells: list[CompatibilityCell] = []
    seen_pairs: set[tuple[str, str]] = set()
    for entry in registry.registry_snapshot().entries:
        pair = (entry.identity.provider, entry.identity.model)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        cells.extend(replace(cell, channel=channel) for cell in _pass_cells(*pair))
    snapshots = _openai_capacity_snapshots()

    def capacity_source(
        now: str,
    ) -> tuple[tuple[CapacitySnapshot, ...], tuple[ExecutionEligibility, ...]]:
        _ = now
        return (snapshots, ())

    return GatewayApplication(
        catalog=catalog,
        profiles=build_profiles(),
        profile_policy_version=1,
        policy=neutral_selector_policy(),
        registry=registry,
        capacity_source=capacity_source,
        compatibility_cells=tuple(cells),
        admin_constraints=AdministratorConstraints(),
        aliases=RoutingAliasTable(aliases or {}),
        adapters=adapter_registry,
        audit=BoundedAuditTrail(),
        client_key_directory=ClientKeyDirectory.from_secrets({CLIENT_ID: CLIENT_KEY}),
        clock=lambda: EVALUATED,
        request_id_factory=make_sequential_request_ids(),
    )


def _openai_capacity_snapshots() -> tuple[CapacitySnapshot, ...]:
    """The synthetic openai/codex capacity world for the derived identities."""
    from tests.gateway_fixtures import build_capacity_snapshots

    return tuple(
        snapshot
        for snapshot in build_capacity_snapshots()
        if snapshot.provider == "openai"
    )


def _catalog_entry(provider: str, model: str, variant: str) -> ModelCatalogEntry:
    known = CapabilityAssessment(
        rating=3,
        evidence=(EVIDENCE,),
        confidence="medium",
        assessed_on="2026-09-24",
        rationale="synthetic calibrated rating",
    )
    unknown = CapabilityAssessment(rating=None)
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
        capabilities=CapabilityAssessments(
            reasoning=known,
            coding=known,
            scientific_methodological=unknown,
            writing_editorial=unknown,
            tool_use=known,
            translation_multilingual=unknown,
        ),
        capacity_bindings=(CapacityScopeRef(provider=provider, scope_id="codex"),),
        reasoning_effort=variant,
    )


def _registration_for(identity: ResourceIdentity) -> ResourceRegistration:
    return ResourceRegistration(
        identity=identity,
        freshness_ttl_seconds=300,
        capabilities=ExecutionCapabilities(context_limit_tokens=272_000),
    )


class DerivedHarness(ServerHarness):
    """ServerHarness over a derived-source application."""

    application: GatewayApplication
    server: GatewayHTTPServer
    thread: threading.Thread

    def make_derived_server(self, application: GatewayApplication) -> int:
        server = make_gateway_server(application, host="127.0.0.1", port=0)
        self.application = application
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        _, port = server.server_address[:2]
        return int(port)

    def models_ids(self, port: int) -> list[str]:
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        return [str(as_dict(entry)["id"]) for entry in as_list(payload["data"])]

    def models_entry(self, port: int, model_id: str) -> dict[str, object]:
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        for entry in as_list(payload["data"]):
            entry_dict = as_dict(entry)
            if entry_dict["id"] == model_id:
                return entry_dict
        raise AssertionError(f"no models entry for {model_id!r}")

def _selected_target(
    application: GatewayApplication,
) -> tuple[ExecutedTarget, ExecutedTarget]:
    """The last audit record's selected/executed target pair, narrowed."""
    audit = audit_records(application)[-1]
    selected = audit.selected_target
    executed = audit.executed_target
    assert selected is not None, "expected a selected target"
    assert executed is not None, "expected an executed target"
    return selected, executed


SOL = (DiscoveredModel("gpt-6-sol", ("high", "max")),)


class LogicalResolutionTests(DerivedHarness):
    """Bare logical ids route to the EXACT identity — never substituted."""

    def test_bare_logical_model_routes_to_the_exact_identity(self) -> None:
        registry, catalog = derived_world(SOL)
        port = self.make_derived_server(make_derived_application(registry, catalog))
        response = self.post_chat(
            port,
            {
                "model": "gpt-6-sol",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(response.status, 200)
        audit = audit_records(self.application)[-1]
        self.assertEqual(
            audit.selected_target,
            audit.executed_target,
            "selected target must equal the executed target",
        )
        selected, _ = _selected_target(self.application)
        self.assertEqual(selected.model, "gpt-6-sol")
        self.assertEqual(selected.variant, "high")
        self.assertEqual(selected.provider, "openai")

    def test_two_sources_with_the_same_identity_compete_exactly(self) -> None:
        registry, catalog = derived_world(
            SOL, source_ids=("personal-openai", "second-openai")
        )
        port = self.make_derived_server(make_derived_application(registry, catalog))
        response = self.post_chat(
            port,
            {
                "model": "gpt-6-sol",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(response.status, 200)
        audit = audit_records(self.application)[-1]
        self.assertEqual(audit.selected_target, audit.executed_target)
        selected, _ = _selected_target(self.application)
        self.assertIn(
            selected.resource_id,
            ("personal-openai:gpt-6-sol:high", "second-openai:gpt-6-sol:high"),
        )
        self.assertEqual(selected.model, "gpt-6-sol")
        self.assertEqual(selected.variant, "high")

    def test_requested_effort_the_identity_lacks_is_typed_rejection(self) -> None:
        """No downgrade, no substitution: an effort the exact model does
        not offer fails explicitly before any dispatch."""
        registry, catalog = derived_world(SOL)
        application = make_derived_application(registry, catalog)
        port = self.make_derived_server(application)
        response = self.post_chat(
            port,
            {
                "model": "gpt-6-sol",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "ultra",
            },
        )
        self.assertEqual(response.status, 400)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(
            as_dict(payload["error"])["code"], "unsupported_reasoning_effort"
        )
        # D-043: rejected requests are audited — with no dispatch, no
        # target and the explicit reason code.
        records = audit_records(self.application)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[-1].result_status, "rejected")
        self.assertEqual(records[-1].reason_codes, ("unsupported_reasoning_effort",))
        self.assertIsNone(records[-1].executed_target)


class EffortRuleTests(DerivedHarness):
    """Omitted effort: single legal variant uses it; several fail."""

    def test_max_only_family_uses_max_when_omitted(self) -> None:
        """D-054 consistency: a max-only light family's only calibrated
        effort is the one used when the client omits the field."""
        luna_max_only = (DiscoveredModel("gpt-6.5-luna", ("low", "max")),)
        registry, catalog = derived_world(luna_max_only)
        port = self.make_derived_server(make_derived_application(registry, catalog))
        response = self.post_chat(
            port,
            {"model": "gpt-6.5-luna", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status, 200)
        selected, _ = _selected_target(self.application)
        self.assertEqual(selected.model, "gpt-6.5-luna")
        self.assertEqual(selected.variant, "max")

    def test_multi_variant_model_requires_an_explicit_effort(self) -> None:
        registry, catalog = derived_world(SOL)
        port = self.make_derived_server(make_derived_application(registry, catalog))
        response = self.post_chat(
            port,
            {"model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status, 400)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(
            as_dict(payload["error"])["code"], "reasoning_effort_required"
        )


class CatalogEffortTests(DerivedHarness):
    _http_started: bool = False

    @override
    def tearDown(self) -> None:
        if self._http_started:
            super().tearDown()

    def _world(
        self, configurations: tuple[tuple[str, str | None, bool | None], ...],
        *, qualified: bool = True,
    ) -> tuple[GatewayApplication, ScriptedAdapter]:
        base = _catalog_entry("openai", "gpt-6-sol", "high")
        entries = tuple(
            replace(
                base,
                identity=ModelIdentity(provider="openai", model="gpt-6-sol", variant=variant),
                reasoning_effort=effort,
                hard_properties=replace(
                    base.hard_properties,
                    supports_reasoning_mode=support,
                ),
            ) for variant, effort, support in configurations
        )
        catalog = ModelCatalog(catalog_version=1, updated_on=DATE, entries=entries)
        registry = ResourceRegistry(clock=lambda: OBSERVED)
        for index, entry in enumerate(entries):
            identity = ResourceIdentity(
                resource_id=f"configuration-{index}",
                channel="worker_bridged" if qualified else "server_direct_http",
                provider="openai", model="gpt-6-sol", entitlement="subscription_included",
                variant=entry.identity.variant if qualified else None,
                quota_pool_ids=(),
            )
            registry.register(_registration_for(identity))
            _ = registry.apply_snapshot(_healthy_snapshot(identity))
        application = make_derived_application(
            registry, catalog, channel="worker_bridged" if qualified else "server_direct_http"
        )
        adapter = application.adapters.resolve("worker_bridged" if qualified else "server_direct_http")
        assert isinstance(adapter, ScriptedAdapter)
        return application, adapter

    def test_http_discovery_and_logical_ingress_use_catalog_effort(self) -> None:
        application, adapter = self._world((
            ("opaque-high", "high", True), ("high", "none", True),
        ))
        port = self.make_derived_server(application)
        self._http_started = True
        metadata = as_dict(self.models_entry(port, "gpt-6-sol")["x_scarcity_router"])
        self.assertEqual(metadata["reasoning_efforts"], ["high", "none"])
        response = self.post_chat(port, {
            "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high", "reasoning": {"effort": "high"},
        })
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(payload["model"], "gpt-6-sol", "response model echoes requested identity")
        self.assertEqual(adapter.dispatches[0].model.variant, "opaque-high")
        self.assertEqual(adapter.dispatches[0].reasoning_effort, "high")
        wire = build_chat_completion_request(adapter.dispatches[0], OPENAI_API_PRESET.policy)
        self.assertEqual(wire["reasoning_effort"], "high")
        self.assertEqual(wire["model"], "gpt-6-sol")
        audit = audit_records(application)[-1]
        self.assertEqual(audit.selected_target, audit.executed_target)
        assert audit.executed_target is not None
        self.assertEqual(audit.executed_target.variant, "opaque-high")
        self.assertEqual(audit.adapter_version, adapter.adapter_version)
        self.assertNotIn("observed_model", audit.to_dict())
        self.assertNotIn("runtime_verified", audit.to_dict())
        pin = "sr-pin:configuration-0/openai/gpt-6-sol/opaque-high"
        response = self.post_chat(port, {
            "model": pin, "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        })
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(payload["model"], pin, "pin echo is not physical-model observation")
        self.assertEqual(adapter.dispatches[-1].reasoning_effort, "high")
        before = adapter.dispatch_count
        response = self.post_chat(port, {
            "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high", "reasoning": {"effort": "low"},
        })
        self.assertEqual(response.status, 400)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(payload["error"])["code"],
                         "conflicting_reasoning_parameters")
        self.assertEqual(adapter.dispatch_count, before)

    def test_null_none_and_unknown_are_distinct(self) -> None:
        for variant, effort, support, explicit_error in (
            ("high", None, False, "unsupported_reasoning_effort"),
            ("unknown", None, None, "compatibility_unknown"),
            ("opaque-none", "none", True, "unsupported_reasoning_effort"),
        ):
            with self.subTest(effort=effort, support=support):
                application, adapter = self._world(((variant, effort, support),))
                infos = exposed_logical_models(application.catalog, application.registry, application.limits)
                self.assertEqual(infos[0].reasoning_efforts, () if effort is None else (effort,))
                _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
                    "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
                }))
                self.assertEqual(adapter.dispatches[0].reasoning_effort, effort)
                with self.assertRaises(GatewayError) as caught:
                    _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
                        "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
                        "reasoning_effort": "high",
                    }))
                self.assertEqual(caught.exception.code, explicit_error)
                self.assertEqual(adapter.dispatch_count, 1)

    def test_same_effort_configurations_compete_without_inventing_effort(self) -> None:
        application, adapter = self._world((
            ("opaque-a", "high", True), ("opaque-b", "high", True),
        ))
        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
            "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
        }))
        self.assertIn(adapter.dispatches[0].model.variant, ("opaque-a", "opaque-b"))
        self.assertEqual(adapter.dispatches[0].reasoning_effort, "high")
        weak = application.catalog.entries[0]
        application.catalog = replace(application.catalog, entries=(
            replace(weak, hard_properties=replace(weak.hard_properties, output_tokens=512)),
            application.catalog.entries[1],
        ))
        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
            "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 128_000,
        }))
        self.assertEqual(adapter.dispatches[-1].model.variant, "opaque-b")
        self.assertEqual(adapter.dispatches[-1].reasoning_effort, "high")
        application, adapter = self._world((
            ("opaque-a", "high", True), ("opaque-b", "low", True),
        ))
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
                "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
            }))
        self.assertEqual(caught.exception.code, "reasoning_effort_required")
        self.assertEqual(adapter.dispatch_count, 0)

    def test_mixed_null_and_literal_none_require_an_explicit_configured_choice(self) -> None:
        application, adapter = self._world((
            ("opaque-null", None, False), ("opaque-none", "none", True),
        ))
        body: dict[str, object] = {
            "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
        }
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(body))
        self.assertEqual(caught.exception.code, "reasoning_effort_required")
        self.assertEqual(adapter.dispatch_count, 0)
        body["reasoning_effort"] = "none"
        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(body))
        self.assertEqual(adapter.dispatches[0].model.variant, "opaque-none")
        self.assertEqual(adapter.dispatches[0].reasoning_effort, "none")

    def test_omitted_logical_effort_is_carried_and_still_requires_channel_support(self) -> None:
        application, adapter = self._world((("opaque-high", "high", True),))
        request = parse_chat_request({
            "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}],
        })
        _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(adapter.dispatches[0].reasoning_effort, "high")
        application.compatibility_cells = tuple(
            replace(cell, value="UNSUPPORTED") if cell.feature == "reasoning_controls" else cell
            for cell in application.compatibility_cells
        )
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(caught.exception.code, "no_eligible_target")
        self.assertEqual(adapter.dispatch_count, 1)
        self.assertIsNone(audit_records(application)[-1].executed_target)

    def test_pin_uses_exact_catalog_effort_and_preserves_omitted_wire_control(self) -> None:
        for qualified in (False, True):
            with self.subTest(qualified=qualified):
                application, adapter = self._world((("opaque-high", "high", True),), qualified=qualified)
                pin = "sr-pin:configuration-0/openai/gpt-6-sol/opaque-high"
                for effort in ("high", None):
                    body: dict[str, object] = {
                        "model": pin, "messages": [{"role": "user", "content": "hi"}],
                    }
                    if effort is not None:
                        body["reasoning_effort"] = effort
                    _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(body))
                    self.assertEqual(adapter.dispatches[-1].model.variant, "opaque-high")
                    self.assertEqual(adapter.dispatches[-1].reasoning_effort,
                                     "high" if qualified or effort is not None else None)
                with self.assertRaises(GatewayError) as caught:
                    _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
                        "model": pin, "messages": [{"role": "user", "content": "hi"}],
                        "reasoning_effort": "low",
                    }))
                self.assertEqual(caught.exception.code, "effort_conflicts_with_target")
                self.assertEqual(adapter.dispatch_count, 2)
                self.assertIsNone(audit_records(application)[-1].executed_target)

    def test_discovery_omits_efforts_without_a_matching_bound_configuration(self) -> None:
        application, _ = self._world((("opaque-high", "high", True),))
        other = replace(application.catalog.entries[0],
                        identity=ModelIdentity(provider="openai", model="gpt-6-sol", variant="max"),
                        reasoning_effort="max")
        other_provider = replace(other,
            identity=ModelIdentity(provider="zai", model="gpt-6-sol", variant="opaque-high"),
            capacity_bindings=(CapacityScopeRef(provider="zai", scope_id="coding-plan"),),
            reasoning_effort="low")
        catalog = replace(application.catalog, entries=(*application.catalog.entries, other, other_provider))
        infos = exposed_logical_models(catalog, application.registry, application.limits)
        self.assertEqual(infos[0].reasoning_efforts, ("high",))

    def test_shipped_max_only_http_logical_request_preserves_real_preset_omission(self) -> None:
        entry = next(
            entry for entry in load_catalog(DEFAULT_CATALOG_PATH).entries
            if entry.identity.provider == "zai" and entry.identity.model == "glm-5.3-flash"
        )
        self.assertEqual(entry.reasoning_effort, "max")
        identity = ResourceIdentity(
            resource_id="zai-http", channel="server_direct_http", provider="zai",
            model=entry.identity.model, entitlement="subscription_included",
        )
        registry = ResourceRegistry(clock=lambda: OBSERVED)
        registry.register(_registration_for(identity))
        snapshot = _healthy_snapshot(identity)
        _ = registry.apply_snapshot(replace(snapshot, quota_facts=tuple(
            replace(fact, window=replace(fact.window, scope_id="coding-plan"))
            for fact in snapshot.quota_facts
        )))
        catalog = ModelCatalog(catalog_version=1, updated_on=DATE, entries=(entry,))
        application = make_derived_application(registry, catalog, channel="server_direct_http")
        adapter = application.adapters.resolve("server_direct_http")
        assert isinstance(adapter, ScriptedAdapter)
        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
            "model": entry.identity.model, "messages": [{"role": "user", "content": "hi"}],
        }))
        call = adapter.dispatches[0]
        self.assertEqual(call.model, entry.identity)
        self.assertIsNone(call.reasoning_effort)
        wire = build_chat_completion_request(call, ZAI_CODING_PLAN_PRESET.policy)
        self.assertEqual(wire["model"], entry.identity.model)
        self.assertNotIn("reasoning_effort", wire)
        self.assertNotIn("thinking", wire)

    def test_actual_plan_lane_has_no_effort_or_physical_model_attestation(self) -> None:
        sources = SourceRegistry(track_registry=load_track_registry())
        config = SourceConfig(source_id="zai-plan-1", kind="zcode_subscription",
                              label="synthetic plan", worker_id="worker-1")
        sources.sync_configuration((config,))
        _ = sources.apply_inventory(ModelInventoryReport(worker_id="worker-1", sources=(
            SourceInventory(source_id=config.source_id, adapter_id="zcode:zai-plan-1",
                            kind=config.kind, observed_at=OBSERVED, auth_state="unverified",
                            runtime_name="zcode", runtime_version="0.16.9",
                            models=(DiscoveredModel("plan-managed", ()),)),
        )))
        registry = ResourceRegistry(clock=lambda: OBSERVED)
        for registration in sources.derived_registrations():
            registry.register(registration)
            _ = registry.apply_snapshot(replace(
                _healthy_snapshot(registration.identity), quota_facts=(),
            ))
        adapter = ScriptedAdapter(channel="worker_bridged", adapter_name="synthetic-plan")
        application = make_application(
            registry=registry,
            clock=lambda: EVALUATED,
            catalog=sources.derived_catalog_entries(load_catalog(DEFAULT_CATALOG_PATH)),
            adapters=(adapter,), aliases=build_aliases({}),
            cells=build_compatibility_cells(ServerConfiguration(sources=(config,)),
                                          provider_secret_reader=lambda _provider: "SYNTHETIC",
                                          source_registry=sources),
        )
        port = self.make_derived_server(application)
        self._http_started = True
        metadata = as_dict(self.models_entry(port, "plan-managed")["x_scarcity_router"])
        self.assertEqual(metadata["reasoning_efforts"], [])
        for model in ("plan-managed", "sr-pin:zai-plan-1:plan-managed/zai/plan-managed/plan"):
            response = self.post_chat(port, {
                "model": model, "messages": [{"role": "user", "content": "hi"}],
            })
            payload = cast("dict[str, object]", json.loads(response.read()))
            self.assertEqual(response.status, 200, payload)
            self.assertEqual(adapter.dispatches[-1].model.model, "plan-managed")
            self.assertEqual(adapter.dispatches[-1].model.variant, "plan")
            self.assertIsNone(adapter.dispatches[-1].reasoning_effort)
        self.assertEqual(audit_records(application)[-1].selected_target,
                         audit_records(application)[-1].executed_target)
        before = adapter.dispatch_count
        for model, effort in (("plan-managed", "high"), ("plan-managed", "none"),
                              ("glm-5.3", "high")):
            body: dict[str, object] = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
            body["reasoning_effort"] = effort
            response = self.post_chat(port, body)
            self.assertNotEqual(response.status, 200)
            _ = response.read()
            self.assertIsNone(audit_records(application)[-1].executed_target)
        self.assertEqual(adapter.dispatch_count, before)


class ResolutionBoundaries(DerivedHarness):
    """Not-found stays not-found; availability is availability."""

    def test_unknown_restricted_and_unclassified_stay_model_not_found(
        self,
    ) -> None:
        world = (
            DiscoveredModel("gpt-6-sol", ("high",)),
            DiscoveredModel("gpt-daybreak-blue-latest", ("high",)),
            DiscoveredModel("totally-unclassified-slug", ("high",)),
        )
        registry, catalog = derived_world(world)
        port = self.make_derived_server(make_derived_application(registry, catalog))
        for slug in (
            "gpt-daybreak-blue-latest",
            "totally-unclassified-slug",
            "never-heard-of-it",
        ):
            response = self.post_chat(
                port,
                {
                    "model": slug,
                    "messages": [{"role": "user", "content": "hi"}],
                    "reasoning_effort": "high",
                },
            )
            self.assertEqual(response.status, 404, slug)
            payload = cast("dict[str, object]", json.loads(response.read()))
            self.assertEqual(as_dict(payload["error"])["code"], "model_not_found")
        ids = self.models_ids(port)
        self.assertIn("gpt-6-sol", ids)
        self.assertNotIn("gpt-daybreak-blue-latest", ids)
        self.assertNotIn("totally-unclassified-slug", ids)

    def test_unavailable_resources_are_availability_not_not_found(self) -> None:
        registry, catalog = derived_world(SOL)
        for entry in registry.registry_snapshot().entries:
            _ = registry.apply_snapshot(
                _healthy_snapshot(entry.identity, status="unavailable")
            )
        port = self.make_derived_server(make_derived_application(registry, catalog))
        response = self.post_chat(
            port,
            {
                "model": "gpt-6-sol",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(response.status, 503)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(payload["error"])["code"], "no_eligible_target")

    def test_genuinely_empty_deployment_exposes_and_resolves_nothing(self) -> None:
        application = make_derived_application(
            ResourceRegistry(clock=lambda: OBSERVED),
            ModelCatalog(catalog_version=1, updated_on=DATE, entries=()),
        )
        port = self.make_derived_server(application)
        self.assertEqual(self.models_ids(port), [])
        response = self.post_chat(
            port,
            {
                "model": "gpt-6-sol",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(response.status, 404)


class AliasCollisionTests(DerivedHarness):
    """Administrator aliases take precedence — visibly, never accidentally."""

    def test_alias_precedence_routes_through_the_profile(self) -> None:
        registry, catalog = derived_world(SOL)
        application = make_derived_application(
            registry,
            catalog,
            aliases={
                "gpt-6-sol": ClientRoutingProfile(
                    profile_id="gateway-core", allowed_providers=("zai",)
                )
            },
        )
        port = self.make_derived_server(application)
        response = self.post_chat(
            port,
            {
                "model": "gpt-6-sol",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        # The alias narrows to zai; the derived world has no zai resource —
        # and the audit proves the ALIAS path (routing profile set), not a
        # silent fall-through to the logical model.
        self.assertEqual(response.status, 503)
        audit = audit_records(self.application)[-1]
        self.assertEqual(audit.routing_profile, "gateway-core")

    def test_models_payload_shows_the_shadowing_explicitly(self) -> None:
        registry, catalog = derived_world(SOL)
        application = make_derived_application(
            registry,
            catalog,
            aliases={"gpt-6-sol": ClientRoutingProfile(profile_id="gateway-core")},
        )
        port = self.make_derived_server(application)
        entries = [
            as_dict(entry)
            for entry in as_list(
                cast(
                    "dict[str, object]",
                    json.loads(self._raw_models(port)),
                )["data"]
            )
        ]
        sol_entries = [e for e in entries if e["id"] == "gpt-6-sol"]
        self.assertEqual(len(sol_entries), 1, "no duplicate ids")
        metadata = as_dict(sol_entries[0]["x_scarcity_router"])
        self.assertEqual(metadata["kind"], "routing_alias")
        self.assertIs(metadata["logical_model_shadowed"], True)

    def _raw_models(self, port: int) -> str:
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        return response.read().decode("utf-8")


class AmbiguousSlugTests(unittest.TestCase):
    def test_multi_provider_slug_fails_loudly_on_request(self) -> None:
        catalog = ModelCatalog(
            catalog_version=1,
            updated_on=DATE,
            entries=(
                _catalog_entry("openai", "shared-slug", "max"),
                _catalog_entry("zai", "shared-slug", "max"),
            ),
        )
        with self.assertRaises(GatewayError) as caught:
            _ = resolve_model_string("shared-slug", build_aliases({}), catalog)
        self.assertEqual(caught.exception.code, "ambiguous_logical_model")

    def test_multi_provider_slug_is_never_advertised(self) -> None:
        registry = ResourceRegistry(clock=lambda: OBSERVED)
        for provider in ("openai", "zai"):
            identity = ResourceIdentity(
                resource_id=f"{provider}-x",
                channel="worker_bridged",
                provider=provider,
                model="shared-slug",
                entitlement="subscription_included",
                variant="max",
                quota_pool_ids=(),
            )
            registry.register(_registration_for(identity))
            _ = registry.apply_snapshot(_healthy_snapshot(identity))
        catalog = ModelCatalog(
            catalog_version=1,
            updated_on=DATE,
            entries=(
                _catalog_entry("openai", "shared-slug", "max"),
                _catalog_entry("zai", "shared-slug", "max"),
            ),
        )
        self.assertEqual(exposed_logical_models(catalog, registry, GatewayLimits()), ())


class DiscoveryMetadataTests(DerivedHarness):
    def test_logical_entry_metadata_is_the_honest_intersection(self) -> None:
        luna_max_only = (DiscoveredModel("gpt-6.5-luna", ("low", "max")),)
        registry, catalog = derived_world(luna_max_only)
        port = self.make_derived_server(make_derived_application(registry, catalog))
        entry = self.models_entry(port, "gpt-6.5-luna")
        metadata = as_dict(entry["x_scarcity_router"])
        self.assertEqual(metadata["kind"], "logical_model")
        self.assertEqual(metadata["provider"], "openai")
        # Only the calibrated floor entry's effort — the runtime-reported
        # non-max efforts have no catalog entry and are not advertised.
        self.assertEqual(metadata["reasoning_efforts"], ["max"])
        # Model hard context (1.05M) intersected with the known channel
        # ceiling (272k) — the channel-bound honest number, never 1.05M.
        self.assertEqual(metadata["effective_context_limit_tokens"], 272_000)
        self.assertEqual(metadata["max_output_tokens"], 128_000)

    def test_alias_entry_metadata_marks_kind(self) -> None:
        registry, catalog = derived_world(SOL)
        application = make_derived_application(
            registry,
            catalog,
            aliases={"fast-coding": ClientRoutingProfile(profile_id="gateway-core")},
        )
        port = self.make_derived_server(application)
        entry = self.models_entry(port, "fast-coding")
        self.assertEqual(
            as_dict(entry["x_scarcity_router"])["kind"], "routing_alias"
        )
        ids = self.models_ids(port)
        self.assertEqual(
            ids, ["fast-coding", "gpt-6-sol"], "aliases first, then logical"
        )


class ResolutionOrderUnitTests(unittest.TestCase):
    def test_resolution_without_catalog_keeps_historical_behavior(self) -> None:
        with self.assertRaises(GatewayError) as caught:
            _ = resolve_model_string("gpt-6-sol", build_aliases({}))
        self.assertEqual(caught.exception.code, "model_not_found")

    def test_unknown_slug_with_catalog_stays_model_not_found(self) -> None:
        _registry, catalog = derived_world(SOL)
        with self.assertRaises(GatewayError) as caught:
            _ = resolve_model_string("never-heard-of-it", build_aliases({}), catalog)
        self.assertEqual(caught.exception.code, "model_not_found")


if __name__ == "__main__":
    _ = unittest.main()
