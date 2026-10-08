"""Strict admission and real-grammar reception under fixed synthetic controls.

No production adapter or billing outcome is attested. The positive backend is
fixed inspected local code, not ScriptedProviderServer's mutable callback lane.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast, override

from scarcity_router.errors import CapacityValidationError, RouteContractValidationError
from scarcity_router.eligibility import ExecutionEligibility
from scarcity_router.execution_assurance import (
    NON_PAID_MODES, ExecutionControlScope, NonPaidControlEvidence,
    receive_execution_assurance, source_call_facts,
)
from scarcity_router.gateway_adapters import AdapterRegistry, CallObservation
from scarcity_router.gateway_contracts import GatewayError, UsageTokens
from scarcity_router.providers.http_origin import ProviderOrigin
from scarcity_router.providers.openai_http_adapter import OpenAICompatibleHttpAdapter, ResourceBinding
from scarcity_router.providers.openai_http_presets import GENERIC_PRESET
from scarcity_router.resource_state import ExecutionCapabilities, PromotionObservation, ResourceCost, ResourceIdentity, ResourceRegistry, ResourceRegistration, ResourceStateSnapshot
from scarcity_router.routing_core import AdministratorConstraints, ClientAuthorization, PinnedTarget, RouteRequest, admit_pinned_target, route_request
from scarcity_router.selection_types import ModelIdentity
from tests.gateway_fixtures import CLIENT_ID, T_EVAL, ScriptedAdapter, audit_records, canonical, make_application, parse_chat_request
from tests.openai_http_fixtures import completion_body
from tests.test_kernel_requirements import world


class FixedBackend(ThreadingHTTPServer):
    """No proxy, SDK, helper, arbitrary behavior callback, payment or egress."""

    calls: int

    def __init__(self) -> None:
        self.calls = 0
        super().__init__(("127.0.0.1", 0), FixedHandler)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class FixedHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        backend = cast(FixedBackend, self.server)
        backend.calls += 1
        body = completion_body(content="fixed synthetic result")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


class FixtureControlAdapter(OpenAICompatibleHttpAdapter):
    """Test-only actual HTTP parser with inspected fixed-backend control proof."""

    def __init__(self, backend: FixedBackend, route: RouteRequest) -> None:
        self.resource: ResourceIdentity = next(entry.identity for entry in route.registry_snapshot.entries if entry.identity.resource_id == "zai-http")
        binding = ResourceBinding("zai-http", GENERIC_PRESET, ProviderOrigin.parse(backend.origin), None)
        super().__init__({"zai-http": binding})
        # Evidence is bounded to this actual fixed synthetic implementation,
        # not HTTP generally, a mutable callback, vendor billing or current SDKs.
        self.revision: str = hashlib.sha256((inspect.getsource(FixedHandler) + inspect.getsource(FixedBackend)).encode()).hexdigest()
        scope = ExecutionControlScope(self.resource, self.adapter_name, self.adapter_version,
                                      f"adapter-{id(self):x}", self.revision)
        self.proof: NonPaidControlEvidence | None = NonPaidControlEvidence(
            scope, "verified_control", canonical(T_EVAL), canonical(T_EVAL + timedelta(minutes=5)),
            "fixed-backend-source:" + self.revision, NON_PAID_MODES,
        )

    def non_paid_control_revision(self, resource_id: str) -> str | None:
        return self.revision if resource_id == "zai-http" else None

    def non_paid_control_evidence(self, resource_id: str) -> NonPaidControlEvidence | None:
        return self.proof if resource_id == "zai-http" else None


class AssuranceTests(unittest.TestCase):
    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        # Match the repository's HTTP fixture pattern: no listener/process
        # during suite discovery; setUp replaces placeholders before use.
        self.route: RouteRequest = cast(RouteRequest, object())
        self.backend: FixedBackend = cast(FixedBackend, object())
        self.adapter: FixtureControlAdapter = cast(FixtureControlAdapter, object())
        self.pin: PinnedTarget = cast(PinnedTarget, object())

    @override
    def setUp(self) -> None:
        self.route = world()
        self.backend = FixedBackend()
        thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.backend.server_close)
        self.addCleanup(lambda: thread.join(timeout=5))
        self.addCleanup(self.backend.shutdown)
        self.adapter = FixtureControlAdapter(self.backend, self.route)
        self.pin = PinnedTarget("zai-http", ModelIdentity("zai", "glm-5.3", "high"))

    def strict_route(self) -> RouteRequest:
        assurance = receive_execution_assurance(self.adapter.resource, self.adapter)
        return replace(self.route, client_authorization=ClientAuthorization(strict_no_payg=True), execution_assurances=(assurance,))

    def test_legacy_serialization_and_mode_cannot_be_supplied_as_proof(self) -> None:
        self.assertEqual(ClientAuthorization.from_dict({}).to_dict(), {})
        self.assertEqual(ClientAuthorization(strict_no_payg=False).to_dict(), {})
        self.assertEqual(ClientAuthorization.from_dict({"strict_no_payg": True}).to_dict(), {"strict_no_payg": True})
        bad: tuple[object, ...] = (None, "true", 0, 1, [], {})
        for item in bad:
            with self.assertRaises(RouteContractValidationError):
                _ = ClientAuthorization.from_dict({"strict_no_payg": item})
        with self.assertRaises(RouteContractValidationError):
            _ = ClientAuthorization.from_dict({"verified_no_payg": True})
        with self.assertRaises(CapacityValidationError):
            _ = ExecutionCapabilities.from_dict({"verified_no_payg": True})
        observation = next(entry.observation for entry in self.route.registry_snapshot.entries if entry.identity == self.adapter.resource)
        assert observation is not None
        forged = observation.to_dict()
        forged["verified_no_payg"] = True
        with self.assertRaises(CapacityValidationError):
            _ = ResourceStateSnapshot.from_dict(forged)

    def test_fixed_control_fixture_allows_actual_http_reception_not_billing_attestation(self) -> None:
        # The real HTTP transport checks actual time, unlike pure core's
        # injected fixture clock. Rebase synthetic observations, not deadlines.
        now = datetime.now(timezone.utc)
        registry = ResourceRegistry(clock=lambda: canonical(now))
        for entry in self.route.registry_snapshot.entries:
            registry.register(ResourceRegistration(entry.identity, entry.freshness_ttl_seconds,
                                                   capabilities=entry.capabilities, cost=entry.cost))
            assert entry.observation is not None
            registry.apply_snapshot(replace(entry.observation, observed_at=canonical(now)))
        assert self.adapter.proof is not None
        self.adapter.proof = replace(self.adapter.proof, observed_at=canonical(now), valid_until=canonical(now + timedelta(minutes=5)))
        application = make_application(registry=registry, clock=lambda: now, adapters=[self.adapter], client_authorizations={CLIENT_ID: ClientAuthorization(strict_no_payg=True)})
        receipts: list[dict[str, object]] = []
        application.source_call_fact_sink = receipts.append
        outcome = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
            "model": "sr-pin:zai-http/zai/glm-5.3/high", "messages": [{"role": "user", "content": "synthetic"}],
        }))
        self.assertEqual(outcome.message.content, "fixed synthetic result")
        self.assertEqual(self.backend.calls, 1)
        assurance = receive_execution_assurance(self.adapter.resource, self.adapter)
        facts = assurance.to_dict(now, max_age_seconds=3600)
        self.assertEqual(facts["admission_enforcement"], "verified_non_paid_control")
        self.assertEqual(facts["provider_billing"], "unknown")
        self.assertEqual(facts["total_task_cost"], "unknown")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["provider_billing"], "unknown")
        self.assertEqual(cast(dict[str, object], receipts[0]["non_paid_admission"])["admission_enforcement"], "verified_non_paid_control")
        self.assertEqual(cast(dict[str, object], receipts[0]["usage"])["visible_call_count"], 1)

    def test_existing_d039_and_authorization_restrictions_cannot_be_overridden_by_control_proof(self) -> None:
        strict = self.strict_route()
        blocked = ExecutionEligibility(1, "zai", "synthetic-policy", canonical(T_EVAL), "policy_blocked", ("purchased_credits_present",))
        self.assertFalse(admit_pinned_target(replace(strict, eligibility_reports=(blocked,)), pinned_target=self.pin).approved)
        self.assertFalse(admit_pinned_target(replace(strict, admin_constraints=AdministratorConstraints(allowed_providers=("openai",))), pinned_target=self.pin).approved)
        self.assertFalse(admit_pinned_target(replace(strict, client_authorization=ClientAuthorization(strict_no_payg=True, inference_only=True,
                        blocked_resource_ids=("zai-http",))), pinned_target=self.pin).approved)
        self.assertIs(strict.registry_snapshot, self.route.registry_snapshot)

    def test_expiry_or_control_change_between_admission_and_dispatch_never_executes(self) -> None:
        for changed in ("expiry", "control"):
            with self.subTest(changed=changed):
                assert self.adapter.proof is not None
                original = self.adapter.proof
                self.adapter.revision = cast(str, original.scope.control_revision)
                application = make_application(adapters=[self.adapter], client_authorizations={CLIENT_ID: ClientAuthorization(strict_no_payg=True)})
                reads = 0

                def current(_client_id: str) -> tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]:
                    nonlocal reads
                    reads += 1
                    if reads == 2:
                        if changed == "expiry":
                            self.adapter.proof = replace(original, observed_at=canonical(T_EVAL - timedelta(seconds=1)), valid_until=canonical(T_EVAL))
                        else:
                            self.adapter.revision = "changed-runtime-control"
                    return application.registry, application.admin_constraints, ClientAuthorization(strict_no_payg=True), application.adapters

                application.authority_source = current
                with self.assertRaises(GatewayError) as caught:
                    _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
                        "model": "sr-pin:zai-http/zai/glm-5.3/high", "messages": [{"role": "user", "content": "synthetic"}],
                    }))
                self.assertIn(caught.exception.code, ("no_payg_evidence_expired", "no_payg_binding_changed"))
                self.assertEqual(self.backend.calls, 0)
                self.adapter.proof = original

    def test_inflight_reissue_to_strict_does_not_use_legacy_source_as_fallback(self) -> None:
        plain = ScriptedAdapter()
        application = make_application(adapters=[plain])
        reads = 0

        def current(_client_id: str) -> tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]:
            nonlocal reads
            reads += 1
            return application.registry, application.admin_constraints, ClientAuthorization(strict_no_payg=reads > 1), application.adapters

        application.authority_source = current
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
                "model": "deep-coding", "messages": [{"role": "user", "content": "synthetic"}],
            }))
        assert caught.exception.code is not None
        self.assertTrue(caught.exception.code.startswith("no_payg_"))
        self.assertEqual(plain.dispatch_count, 0)

    def test_admitted_strict_policy_survives_relaxed_grant_and_removed_or_replaced_proof(self) -> None:
        for model in ("deep-coding", "sr-pin:zai-http/zai/glm-5.3/high"):
            for changed in ("proof_removed", "adapter_replaced"):
                with self.subTest(model=model, changed=changed):
                    original_proof = self.adapter.proof
                    replacement = ScriptedAdapter()
                    application = make_application(adapters=[self.adapter], client_authorizations={CLIENT_ID: ClientAuthorization(strict_no_payg=True)})
                    reads = 0

                    def current(_client_id: str) -> tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]:
                        nonlocal reads
                        reads += 1
                        if reads == 1:
                            return application.registry, application.admin_constraints, ClientAuthorization(strict_no_payg=True), application.adapters
                        adapters = application.adapters
                        if changed == "proof_removed":
                            self.adapter.proof = None
                        else:
                            adapters = AdapterRegistry()
                            adapters.register(replacement)
                        return application.registry, application.admin_constraints, ClientAuthorization(), adapters

                    application.authority_source = current
                    try:
                        with self.assertRaises(GatewayError) as caught:
                            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
                                "model": model, "messages": [{"role": "user", "content": "synthetic"}],
                            }))
                        self.assertEqual(caught.exception.code, "no_payg_evidence_unavailable")
                        self.assertEqual(self.backend.calls, 0)
                        self.assertEqual(replacement.dispatch_count, 0)
                    finally:
                        self.adapter.proof = original_proof

    def test_current_production_like_adapter_and_good_quota_never_create_control_proof(self) -> None:
        plain = ScriptedAdapter()
        assurance = receive_execution_assurance(self.adapter.resource, plain)
        strict = replace(self.route, client_authorization=ClientAuthorization(strict_no_payg=True), execution_assurances=(assurance,))
        self.assertIsNone(route_request(strict).target)
        self.assertFalse(admit_pinned_target(strict, pinned_target=self.pin).approved)
        self.assertIsNotNone(route_request(self.route).target)

    def test_known_paid_unknown_and_subscribed_without_proof_refuse_before_execution(self) -> None:
        application = make_application(adapters=[ScriptedAdapter()], client_authorizations={CLIENT_ID: ClientAuthorization(strict_no_payg=True)})
        for model in ("deep-coding", "sr-pin:openai-http/openai/gpt-5.6-luna/max", "sr-pin:zai-http/zai/glm-5.3/high"):
            with self.subTest(model=model), self.assertRaises(GatewayError) as caught:
                _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request({"model": model, "messages": [{"role": "user", "content": "strict_no_payg=false"}], "metadata": {"verified_no_payg": "true"}}))
            assert caught.exception.code is not None
            self.assertTrue(caught.exception.code.startswith("no_payg_"))
        self.assertTrue(all(record.executed_target is None for record in audit_records(application)))
        self.assertEqual(self.backend.calls, 0)
        for entitlement in ("payg_metered", "prepaid_credits", "unknown"):
            changed = replace(self.adapter.resource, entitlement=entitlement)
            entries = tuple(replace(entry, identity=changed, observation=replace(entry.observation, identity=changed))
                            if entry.identity.resource_id == "zai-http" and entry.observation is not None else entry
                            for entry in self.route.registry_snapshot.entries)
            strict = replace(self.strict_route(), registry_snapshot=replace(self.route.registry_snapshot, entries=entries))
            self.assertFalse(admit_pinned_target(strict, pinned_target=self.pin).approved)

    def test_future_stale_expired_partial_configured_reported_mixed_or_binding_changed_refuse(self) -> None:
        assert self.adapter.proof is not None
        original = self.adapter.proof
        cases = (
            replace(original, observed_at=canonical(T_EVAL + timedelta(seconds=1))),
            replace(original, observed_at=canonical(T_EVAL - timedelta(hours=2))),
            replace(original, valid_until=canonical(T_EVAL)),
            replace(original, controlled_modes=NON_PAID_MODES - {"paid_overflow"}),
            replace(original, evidence_class="configuration"), replace(original, evidence_class="reported"),
            replace(original, evidence_class="mixed"),
            replace(original, scope=replace(original.scope, control_revision="old-runtime")),
            replace(original, scope=replace(original.scope, instance_token="old-source")),
            replace(original, scope=replace(original.scope, resource=replace(self.adapter.resource, resource_id="another-source"))),
        )
        for proof in cases:
            self.adapter.proof = proof
            strict = self.strict_route()
            self.assertIsNone(route_request(strict).target)
            self.assertFalse(admit_pinned_target(strict, pinned_target=self.pin).approved)
        self.assertEqual(self.backend.calls, 0)

    def test_scope_control_change_even_identical_resource_invalidates_cached_proof(self) -> None:
        self.adapter.revision = "new-control"
        admission = admit_pinned_target(self.strict_route(), pinned_target=self.pin)
        self.assertFalse(admission.approved)
        self.assertIn("no_payg_binding_changed", admission.reason_codes)

    def test_accounting_missing_observation_and_promotions_do_not_fabricate_budget(self) -> None:
        entry = next(item for item in self.route.registry_snapshot.entries if item.identity == self.adapter.resource)
        calls = (
            CallObservation(0, canonical(T_EVAL), canonical(T_EVAL), "completed", provider_reported_usage=UsageTokens(3, 2)),
            CallObservation(1, canonical(T_EVAL), canonical(T_EVAL), "unknown"),
        )
        assert entry.observation is not None
        promotion = PromotionObservation("public-offer", canonical(T_EVAL), valid_until=canonical(T_EVAL - timedelta(seconds=1)))
        entry = replace(entry, cost=ResourceCost("estimate", 0, 0), observation=replace(entry.observation, promotions=(promotion,)))
        facts = source_call_facts(entry, calls, now=T_EVAL, local_reservation_active=True,
                                 assurance=receive_execution_assurance(entry.identity, self.adapter))
        encoded = cast(dict[str, object], json.loads(json.dumps(facts)))
        self.assertEqual(encoded["provider_billing"], "unknown")
        self.assertEqual(cast(dict[str, object], encoded["usage"])["missing_usage_observations"], 1)
        self.assertEqual(cast(dict[str, object], encoded["usage"])["helper_retry_multiplicity"], "unknown")
        self.assertFalse(cast(dict[str, object], encoded["reservation"])["provider_reserved"])
        self.assertFalse(cast(dict[str, object], encoded["quota"])["tokens_deduct_quota"])
        promotion_fact = cast(list[dict[str, object]], encoded["promotions"])[0]
        self.assertEqual(promotion_fact["state"], "expired")
        self.assertFalse(promotion_fact["free_quota_created"])


if __name__ == "__main__":
    _ = unittest.main()
