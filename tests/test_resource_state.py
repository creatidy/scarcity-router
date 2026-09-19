"""Contract tests for the M01 resource-state contract, registry and worker report.

These tests construct the normalized resource-state objects directly (the
contract in ``scarcity_router/resource_state.py``, sibling to capacity v3)
and assert the invariants issue #86 requires: distinguished unknown/stale/
zero-exhausted/unavailable/unsupported states, collector failure isolation,
telemetry never changing capability facts, no secrets or account identity in
snapshots, explicit confirmed-only quota pools, and fail-closed handling of
wrong-version or malformed documents. They also pin the bounded corrections
of the contract: administrator registration policy is authoritative over
observations, registry read state is cross-resource consistent (an entry's
observation must carry the entry's identity), and future-dated observations
are never evaluated as fresh. They do NOT touch live providers, the network
or credentials; all fixtures are synthetic.

Run with either:

    python -m unittest discover -s tests -v
    python -m unittest tests.test_resource_state -v
"""

from __future__ import annotations

import json
import unittest
from typing import cast

from scarcity_router.capacity import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
)
from scarcity_router.errors import CapacityValidationError
from scarcity_router.resource_state import (
    ENTITLEMENT_CLASSES,
    EXECUTION_CHANNELS,
    REGISTRY_SCHEMA_VERSION,
    RESOURCE_STATE_SCHEMA_VERSION,
    WORKER_REPORT_SCHEMA_VERSION,
    ExecutionCapabilities,
    PromotionObservation,
    QuotaFact,
    QuotaPoolGroup,
    ResourceCost,
    ResourceHealth,
    ResourceIdentity,
    ResourceRegistration,
    ResourceRegistry,
    ResourceRegistryEntry,
    ResourceStateSnapshot,
    RegistrySnapshot,
    WorkerStateReport,
    classify_freshness,
    resource_snapshot_from_capacity,
)

# ── synthetic clocks (canonical UTC millisecond timestamps) ───────────────────

T0 = "2026-09-15T12:00:00.000Z"
T0_PLUS_59 = "2026-09-15T12:00:59.000Z"
T0_PLUS_300 = "2026-09-15T12:05:00.000Z"
T0_PLUS_301 = "2026-09-15T12:05:01.000Z"
T_MINUS_1 = "2026-09-15T11:59:59.000Z"

TTL = 300
POLL = 60

_HEALTH_REQUIRED_CODE: dict[str, str] = {
    "unavailable": "source_unavailable",
    "auth_required": "auth_required",
    "unsupported": "unsupported_source",
    "schema_changed": "schema_changed",
    "unknown": "telemetry_unknown",
}


# ── builders ──────────────────────────────────────────────────────────────────

def _identity(fields: dict[str, object]) -> ResourceIdentity:
    return ResourceIdentity(
        resource_id=cast(str, fields["resource_id"]),
        channel=cast(str, fields["channel"]),
        provider=cast(str, fields["provider"]),
        model=cast(str, fields["model"]),
        entitlement=cast(str, fields["entitlement"]),
        variant=cast("str | None", fields.get("variant")),
        quota_pool_ids=cast("tuple[str, ...]", fields.get("quota_pool_ids", ())),
    )


def codex_identity(**overrides: object) -> ResourceIdentity:
    """The OpenAI Codex subscription surface (local app adapter channel)."""
    fields: dict[str, object] = {
        "resource_id": "openai-codex-sub",
        "channel": "local_app_adapter",
        "provider": "openai",
        "model": "gpt-6-codex",
        "entitlement": "subscription_included",
    }
    fields.update(overrides)
    return _identity(fields)


def ollama_identity(**overrides: object) -> ResourceIdentity:
    """A localhost Ollama surface behind a worker bridge."""
    fields: dict[str, object] = {
        "resource_id": "lab-worker-ollama-qwen3",
        "channel": "worker_bridged",
        "provider": "ollama",
        "model": "qwen3-coder",
        "entitlement": "local_ungated",
    }
    fields.update(overrides)
    return _identity(fields)


def window(
    *,
    resource: str = "tokens",
    kind: str = "five_hour",
    scope_id: str | None = "codex",
    used: int | None = 6,
    remaining: int | None = 94,
    duration_seconds: int | None = 18_000,
) -> dict[str, object]:
    d: dict[str, object] = {"resource": resource, "kind": kind}
    if scope_id is not None:
        d["scope_id"] = scope_id
    if duration_seconds is not None:
        d["duration_seconds"] = duration_seconds
    if used is not None:
        d["used_percent"] = used
        d["remaining_percent"] = remaining
    return d


def capacity_payload(
    *,
    provider: str = "openai",
    status: str = "ok",
    windows: list[dict[str, object]] | None = None,
    diagnostics: list[dict[str, object]] | None = None,
    plan: str | None = "plus",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 3,
        "provider": provider,
        "source": "codex_app_server" if provider == "openai" else "zai_usage_endpoint",
        "retrieved_at": T0,
        "status": status,
        "windows": [] if windows is None else windows,
        "diagnostics": [] if diagnostics is None else diagnostics,
    }
    if plan is not None:
        payload["plan"] = plan
    return payload


def failed_capacity(status: str, diagnostic: str) -> dict[str, object]:
    """A capacity v3 failure observation: empty windows plus its status code."""
    return capacity_payload(status=status, diagnostics=[{"code": diagnostic}])


def resource_snapshot(
    identity: ResourceIdentity | None = None,
    *,
    observed_at: str = T0,
    status: str = "ok",
    quota_facts: tuple[QuotaFact, ...] = (),
    promotions: tuple[PromotionObservation, ...] = (),
) -> ResourceStateSnapshot:
    """An observation record: identity, health and observed facts only."""
    diagnostics: tuple[CapacityDiagnostic, ...] = ()
    if status != "ok":
        diagnostics = (CapacityDiagnostic(code=_HEALTH_REQUIRED_CODE[status]),)
    return ResourceStateSnapshot(
        schema_version=RESOURCE_STATE_SCHEMA_VERSION,
        identity=identity or codex_identity(),
        observed_at=observed_at,
        health=ResourceHealth(status=status, diagnostics=diagnostics),
        quota_facts=quota_facts,
        promotions=promotions,
    )


def registration(
    identity: ResourceIdentity | None = None,
    *,
    capabilities: ExecutionCapabilities | None = None,
    cost: ResourceCost | None = None,
    poll: int | None = POLL,
    ttl: int = TTL,
) -> ResourceRegistration:
    """Administrator-owned policy and configured facts for one resource."""
    return ResourceRegistration(
        identity=identity or codex_identity(),
        freshness_ttl_seconds=ttl,
        poll_interval_seconds=poll,
        capabilities=capabilities if capabilities is not None else ExecutionCapabilities(),
        cost=cost,
    )


# ── identity ──────────────────────────────────────────────────────────────────


class TestResourceIdentity(unittest.TestCase):
    """Identity separates model, surface, entitlement and pools (D-042)."""

    def test_accepts_full_identity(self) -> None:
        identity = codex_identity(
            variant="high",
            quota_pool_ids=("family-pool",),
        )
        self.assertEqual(identity.variant, "high")
        self.assertEqual(identity.quota_pool_ids, ("family-pool",))

    def test_rejects_unknown_channel(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = codex_identity(channel="carrier_pigeon")

    def test_channel_vocabulary_is_exactly_the_a0_members(self) -> None:
        self.assertEqual(
            EXECUTION_CHANNELS,
            {"server_direct_http", "worker_bridged", "local_app_adapter"},
        )

    def test_rejects_unknown_entitlement(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = codex_identity(entitlement="enterprise_unlimited")

    def test_entitlement_vocabulary_matches_d042_plus_unknown(self) -> None:
        self.assertEqual(
            ENTITLEMENT_CLASSES,
            {
                "subscription_included",
                "promotional",
                "payg_metered",
                "prepaid_credits",
                "local_ungated",
                "unknown",
            },
        )

    def test_rejects_credential_shaped_identifier(self) -> None:
        # Uppercase and over-long tokens cannot be smuggled into identity.
        with self.assertRaises(CapacityValidationError):
            _ = codex_identity(resource_id="sk-LIVE-TOKEN-0123456789ABCDEF0123456789")

    def test_rejects_unsorted_quota_pool_ids(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = codex_identity(quota_pool_ids=("pool-b", "pool-a"))

    def test_rejects_duplicate_quota_pool_ids(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = codex_identity(quota_pool_ids=("pool-a", "pool-a"))

    def test_round_trips_serialization(self) -> None:
        identity = codex_identity(variant="high", quota_pool_ids=("pool-a",))
        self.assertEqual(ResourceIdentity.from_dict(identity.to_dict()), identity)

    def test_empty_pools_mean_no_confirmed_sharing(self) -> None:
        # The empty tuple serializes as an absent field and never as a
        # claim of independence or of sharing.
        self.assertNotIn("quota_pool_ids", codex_identity().to_dict())


# ── quota facts, cost, capabilities, promotions ───────────────────────────────


class TestQuotaFact(unittest.TestCase):
    def test_accepts_window_with_observation_class(self) -> None:
        fact = QuotaFact(
            observation_class="provider_telemetry",
            window=CapacityWindow.from_dict(window()),
        )
        self.assertEqual(fact.observation_class, "provider_telemetry")

    def test_rejects_unknown_observation_class(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = QuotaFact(
                observation_class="rumor",
                window=CapacityWindow.from_dict(window()),
            )

    def test_rejects_invalid_window(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = QuotaFact.from_dict(
                {
                    "observation_class": "provider_telemetry",
                    "window": {"resource": "tokens", "kind": "nope"},
                }
            )


class TestResourceCost(unittest.TestCase):
    def test_accepts_known_amounts(self) -> None:
        cost = ResourceCost(
            observation_class="provider_telemetry",
            input_micro_usd_per_mtoken=2_000_000,
            output_micro_usd_per_mtoken=8_000_000,
        )
        self.assertEqual(
            cost.to_dict(),
            {
                "observation_class": "provider_telemetry",
                "input_micro_usd_per_mtoken": 2_000_000,
                "output_micro_usd_per_mtoken": 8_000_000,
            },
        )

    def test_requires_at_least_one_amount(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = ResourceCost(observation_class="estimate")

    def test_rejects_negative_amount(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = ResourceCost(
                observation_class="estimate",
                input_micro_usd_per_mtoken=-1,
            )

    def test_zero_is_known_free_not_unknown(self) -> None:
        cost = ResourceCost(
            observation_class="provider_telemetry",
            input_micro_usd_per_mtoken=0,
        )
        self.assertEqual(cost.input_micro_usd_per_mtoken, 0)


class TestExecutionCapabilities(unittest.TestCase):
    def test_default_is_all_unknown(self) -> None:
        capabilities = ExecutionCapabilities()
        self.assertIsNone(capabilities.tool_calls)
        self.assertIsNone(capabilities.context_limit_tokens)
        self.assertEqual(capabilities.to_dict(), {})

    def test_accepts_known_facts(self) -> None:
        capabilities = ExecutionCapabilities(
            streaming=True,
            tool_calls=False,
            context_limit_tokens=128_000,
        )
        self.assertEqual(
            capabilities.to_dict(),
            {
                "streaming": True,
                "tool_calls": False,
                "context_limit_tokens": 128_000,
            },
        )

    def test_rejects_non_bool_fact_at_the_serialized_boundary(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = ExecutionCapabilities.from_dict({"streaming": "yes"})

    def test_rejects_nonpositive_context_limit(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = ExecutionCapabilities(context_limit_tokens=0)


class TestPromotionObservation(unittest.TestCase):
    def test_accepts_fully_scoped_promotion(self) -> None:
        promotion = PromotionObservation(
            source="owner-evidence",
            observed_at=T0,
            channel="local_app_adapter",
            provider="openai",
            model="gpt-6-codex",
            plan="plus",
            valid_from=T0,
            valid_until=T0_PLUS_300,
            timezone="utc",
        )
        self.assertEqual(
            PromotionObservation.from_dict(promotion.to_dict()), promotion
        )

    def test_unknown_scopes_are_omitted_not_everywhere(self) -> None:
        promotion = PromotionObservation(source="owner-evidence", observed_at=T0)
        serialized = promotion.to_dict()
        for absent in ("channel", "provider", "model", "plan"):
            self.assertNotIn(absent, serialized)

    def test_rejects_inverted_validity_period(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = PromotionObservation(
                source="owner-evidence",
                observed_at=T0,
                valid_from=T0_PLUS_301,
                valid_until=T0,
            )

    def test_rejects_unknown_channel(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = PromotionObservation(
                source="owner-evidence", observed_at=T0, channel="magic"
            )


# ── health ────────────────────────────────────────────────────────────────────


class TestResourceHealth(unittest.TestCase):
    def test_ok_requires_no_diagnostic(self) -> None:
        self.assertEqual(ResourceHealth(status="ok").to_dict(), {"status": "ok"})

    def test_non_ok_requires_status_level_diagnostic(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = ResourceHealth(status="unavailable")
        health = ResourceHealth(
            status="unavailable",
            diagnostics=(CapacityDiagnostic(code="source_unavailable"),),
        )
        self.assertEqual(health.status, "unavailable")

    def test_rejects_stale_as_a_stored_health_status(self) -> None:
        # Staleness is evaluated freshness, never a stored health state.
        with self.assertRaises(CapacityValidationError):
            _ = ResourceHealth(status="stale")

    def test_rejects_unsorted_diagnostics(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = ResourceHealth(
                status="unknown",
                diagnostics=(
                    CapacityDiagnostic(code="telemetry_unknown"),
                    CapacityDiagnostic(code="percentage_unknown", window_id="codex:0"),
                ),
            )


# ── snapshot contract ─────────────────────────────────────────────────────────


class TestResourceStateSnapshot(unittest.TestCase):
    def test_round_trips_serialization(self) -> None:
        snapshot = resource_snapshot(
            quota_facts=(
                QuotaFact(
                    observation_class="provider_telemetry",
                    window=CapacityWindow.from_dict(window()),
                ),
            ),
            promotions=(
                PromotionObservation(
                    source="owner-evidence", observed_at=T0, plan="plus"
                ),
            ),
        )
        self.assertEqual(
            ResourceStateSnapshot.from_dict(snapshot.to_dict()), snapshot
        )

    def test_rejects_wrong_schema_version(self) -> None:
        payload = resource_snapshot().to_dict()
        payload["schema_version"] = 2
        with self.assertRaises(CapacityValidationError):
            _ = ResourceStateSnapshot.from_dict(payload)

    def test_rejects_unknown_keys(self) -> None:
        payload = resource_snapshot().to_dict()
        payload["plan"] = "plus"
        with self.assertRaises(CapacityValidationError):
            _ = ResourceStateSnapshot.from_dict(payload)

    def test_observation_documents_cannot_carry_server_policy(self) -> None:
        # The single-canonical-truth rule: policy and configured facts are
        # registration-owned. An observation document carrying any of them
        # is malformed and fails closed, so no reporting side (including a
        # worker) can even express a TTL/poll/capability/cost override.
        payload = resource_snapshot().to_dict()
        for forbidden in (
            "freshness_ttl_seconds",
            "poll_interval_seconds",
            "capabilities",
            "cost",
        ):
            with self.subTest(forbidden=forbidden):
                tampered = dict(payload)
                tampered[forbidden] = 1 if forbidden.endswith("seconds") else {}
                with self.assertRaises(CapacityValidationError):
                    _ = ResourceStateSnapshot.from_dict(tampered)

    def test_rejects_unsorted_quota_facts(self) -> None:
        late = QuotaFact(
            observation_class="provider_telemetry",
            window=CapacityWindow.from_dict(
                window(kind="weekly", duration_seconds=604_800)
            ),
        )
        early = QuotaFact(
            observation_class="provider_telemetry",
            window=CapacityWindow.from_dict(window()),
        )
        with self.assertRaises(CapacityValidationError):
            _ = resource_snapshot(quota_facts=(late, early))

    def test_never_carries_capacity_plan_label(self) -> None:
        snapshot = resource_snapshot_from_capacity(
            CapacitySnapshot.from_dict(
                capacity_payload(plan="plus", windows=[window()])
            ),
            identity=codex_identity(),
        )
        serialized = json.dumps(snapshot.to_dict())
        self.assertNotIn("plan", serialized)
        self.assertNotIn("plus", serialized)

    def test_validate_revalidates(self) -> None:
        snapshot = resource_snapshot()
        self.assertEqual(snapshot.validate(), snapshot)


# ── shared normalization from capacity v3 ─────────────────────────────────────


class TestCapacityNormalization(unittest.TestCase):
    """The one shared path from provider telemetry to resource state."""

    def test_healthy_telemetry_maps_identity_and_windows(self) -> None:
        snapshot = resource_snapshot_from_capacity(
            CapacitySnapshot.from_dict(
                capacity_payload(windows=[window(used=6, remaining=94)])
            ),
            identity=codex_identity(),
        )
        self.assertEqual(snapshot.health.status, "ok")
        self.assertEqual(snapshot.observed_at, T0)
        self.assertEqual(len(snapshot.quota_facts), 1)
        fact = snapshot.quota_facts[0]
        self.assertEqual(fact.observation_class, "provider_telemetry")
        self.assertEqual(fact.window.scope_id, "codex")

    def test_all_six_capacity_statuses_map_one_to_one(self) -> None:
        cases: list[tuple[str, list[str]]] = [
            ("ok", []),
            ("unavailable", ["source_unavailable"]),
            ("auth_required", ["auth_required"]),
            ("unsupported", ["unsupported_source"]),
            ("schema_changed", ["schema_changed"]),
            ("unknown", ["telemetry_unknown"]),
        ]
        for capacity_status, codes in cases:
            with self.subTest(capacity_status=capacity_status):
                payload = (
                    failed_capacity(capacity_status, codes[0])
                    if codes
                    else capacity_payload(status=capacity_status)
                )
                snapshot = resource_snapshot_from_capacity(
                    CapacitySnapshot.from_dict(payload),
                    identity=codex_identity(),
                )
                self.assertEqual(snapshot.health.status, capacity_status)
                self.assertEqual(
                    [diagnostic.code for diagnostic in snapshot.health.diagnostics],
                    codes,
                )

    def test_failure_telemetry_yields_no_quota_facts(self) -> None:
        snapshot = resource_snapshot_from_capacity(
            CapacitySnapshot.from_dict(
                failed_capacity("auth_required", "auth_required")
            ),
            identity=codex_identity(),
        )
        self.assertEqual(snapshot.quota_facts, ())

    def test_zero_remaining_is_not_unhealthy(self) -> None:
        # Known exhausted quota stays status "ok"; exhaustion is a quota
        # fact, never a health state.
        snapshot = resource_snapshot_from_capacity(
            CapacitySnapshot.from_dict(
                capacity_payload(windows=[window(used=100, remaining=0)])
            ),
            identity=codex_identity(),
        )
        self.assertEqual(snapshot.health.status, "ok")
        self.assertEqual(snapshot.quota_facts[0].window.remaining_percent, 0)

    def test_provider_mismatch_fails_closed(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = resource_snapshot_from_capacity(
                CapacitySnapshot.from_dict(capacity_payload(provider="zai")),
                identity=codex_identity(),
            )


# ── worker state reports ──────────────────────────────────────────────────────


class TestWorkerStateReport(unittest.TestCase):
    def test_round_trips_serialization(self) -> None:
        report = WorkerStateReport(
            schema_version=WORKER_REPORT_SCHEMA_VERSION,
            worker_id="lab-worker-01",
            reported_at=T0,
            resources=(resource_snapshot(ollama_identity(), status="ok"),),
        )
        self.assertEqual(WorkerStateReport.from_dict(report.to_dict()), report)

    def test_rejects_wrong_report_version(self) -> None:
        payload = WorkerStateReport(
            schema_version=WORKER_REPORT_SCHEMA_VERSION,
            worker_id="lab-worker-01",
            reported_at=T0,
            resources=(),
        ).to_dict()
        payload["schema_version"] = 2
        with self.assertRaises(CapacityValidationError):
            _ = WorkerStateReport.from_dict(payload)

    def test_rejects_server_direct_channel(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = WorkerStateReport(
                schema_version=WORKER_REPORT_SCHEMA_VERSION,
                worker_id="lab-worker-01",
                reported_at=T0,
                resources=(
                    resource_snapshot(codex_identity(channel="server_direct_http")),
                ),
            )

    def test_rejects_duplicate_resource_ids(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = WorkerStateReport(
                schema_version=WORKER_REPORT_SCHEMA_VERSION,
                worker_id="lab-worker-01",
                reported_at=T0,
                resources=(
                    resource_snapshot(ollama_identity(), observed_at=T0),
                    resource_snapshot(
                        ollama_identity(model="qwen3-coder-2"),
                        observed_at=T0,
                    ),
                ),
            )

    def test_rejects_observation_in_the_future_of_the_report(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = WorkerStateReport(
                schema_version=WORKER_REPORT_SCHEMA_VERSION,
                worker_id="lab-worker-01",
                reported_at=T0,
                resources=(
                    resource_snapshot(ollama_identity(), observed_at=T0_PLUS_301),
                ),
            )

    def test_rejects_unsorted_resources(self) -> None:
        first = resource_snapshot(codex_identity(resource_id="aaa-first"))
        second = resource_snapshot(ollama_identity())
        with self.assertRaises(CapacityValidationError):
            _ = WorkerStateReport(
                schema_version=WORKER_REPORT_SCHEMA_VERSION,
                worker_id="lab-worker-01",
                reported_at=T0,
                resources=(second, first),
            )


# ── registry ──────────────────────────────────────────────────────────────────


class TestResourceRegistry(unittest.TestCase):
    def test_register_and_read_never_observed(self) -> None:
        registry = ResourceRegistry()
        configured = registration(
            capabilities=ExecutionCapabilities(streaming=True),
            cost=ResourceCost(
                observation_class="estimate", input_micro_usd_per_mtoken=1
            ),
            poll=POLL,
        )
        registry.register(configured)
        read = registry.registry_snapshot(now=T0)
        self.assertEqual(read.revision, 1)
        self.assertEqual(len(read.entries), 1)
        entry = read.entries[0]
        self.assertIsNone(entry.observation)
        self.assertEqual(entry.freshness, "never_observed")
        self.assertTrue(entry.refresh_due)
        # The read model composes the registration's authoritative policy
        # and configured facts.
        self.assertEqual(entry.freshness_ttl_seconds, TTL)
        self.assertEqual(entry.poll_interval_seconds, POLL)
        self.assertEqual(entry.capabilities, configured.capabilities)
        self.assertEqual(entry.cost, configured.cost)

    def test_register_rejects_duplicates(self) -> None:
        registry = ResourceRegistry()
        registry.register(registration())
        with self.assertRaises(CapacityValidationError):
            registry.register(registration())

    def test_apply_rejects_unregistered_resource(self) -> None:
        registry = ResourceRegistry()
        with self.assertRaises(CapacityValidationError):
            registry.apply_snapshot(resource_snapshot())

    def test_apply_rejects_identity_mismatch(self) -> None:
        registry = ResourceRegistry()
        registry.register(registration())
        drifted = resource_snapshot(codex_identity(entitlement="payg_metered"))
        with self.assertRaises(CapacityValidationError):
            registry.apply_snapshot(drifted)

    def test_registration_policy_is_authoritative_for_freshness(self) -> None:
        # The TTL comes from the administrator registration. Observations
        # carry no TTL at all, so no reporting side can enlarge how long
        # its state is treated as fresh.
        clock_state = {"now": T0}
        registry = ResourceRegistry(clock=lambda: clock_state["now"])
        registry.register(registration(ttl=TTL))
        registry.apply_snapshot(resource_snapshot())
        self.assertEqual(
            registry.registry_snapshot(now=T0).entries[0].freshness, "fresh"
        )
        # Age exactly at the registration TTL is still fresh; one second
        # beyond is stale.
        self.assertEqual(
            registry.registry_snapshot(now=T0_PLUS_300).entries[0].freshness,
            "fresh",
        )
        stale_read = registry.registry_snapshot(now=T0_PLUS_301)
        stale_entry = stale_read.entries[0]
        assert stale_entry.observation is not None
        self.assertEqual(stale_entry.freshness, "stale")
        self.assertEqual(stale_entry.freshness_ttl_seconds, TTL)
        # Staleness never rewrites the observation's own health.
        self.assertEqual(stale_entry.observation.health.status, "ok")
        # A new observation restores freshness once the server clock has
        # reached its observation time.
        clock_state["now"] = T0_PLUS_301
        registry.apply_snapshot(resource_snapshot(observed_at=T0_PLUS_301))
        self.assertEqual(
            registry.registry_snapshot(now=T0_PLUS_301).entries[0].freshness,
            "fresh",
        )

    def test_refresh_due_follows_registered_polling_cadence(self) -> None:
        registry = ResourceRegistry()
        registry.register(registration(poll=POLL))
        registry.apply_snapshot(resource_snapshot())
        self.assertEqual(registry.refresh_due(now=T0_PLUS_59), ())
        self.assertEqual(
            registry.refresh_due(now=T0_PLUS_300), ("openai-codex-sub",)
        )

    def test_refresh_due_never_for_poll_free_resources(self) -> None:
        registry = ResourceRegistry()
        registry.register(registration(poll=None))
        self.assertEqual(registry.refresh_due(now=T0), ())

    def test_stale_observation_is_never_due_without_polling(self) -> None:
        registry = ResourceRegistry()
        registry.register(registration(poll=None))
        registry.apply_snapshot(resource_snapshot())
        read = registry.registry_snapshot(now=T0_PLUS_301)
        self.assertEqual(read.entries[0].freshness, "stale")
        self.assertFalse(read.entries[0].refresh_due)
        self.assertIsNone(read.entries[0].poll_interval_seconds)

    def test_confirmed_pool_groups_members_and_excludes_unrelated(self) -> None:
        registry = ResourceRegistry()
        registry.register(
            registration(
                ollama_identity(
                    resource_id="wsl-ollama",
                    quota_pool_ids=("lab-gpu-pool",),
                )
            )
        )
        registry.register(
            registration(
                ollama_identity(
                    resource_id="windows-ollama",
                    model="qwen3-coder",
                    quota_pool_ids=("lab-gpu-pool",),
                )
            )
        )
        read = registry.registry_snapshot(now=T0)
        self.assertEqual(
            read.quota_pools,
            (
                QuotaPoolGroup(
                    pool_id="lab-gpu-pool",
                    resource_ids=("windows-ollama", "wsl-ollama"),
                ),
            ),
        )

    def test_unconfirmed_sharing_is_never_assumed_in_either_direction(self) -> None:
        # Two surfaces of the same provider/model/entitlement do NOT become
        # one pool, and one surface does not become an explicitly
        # independent pool either: with no confirmed pool ids there is
        # simply no pool fact at all.
        registry = ResourceRegistry()
        registry.register(registration(ollama_identity(resource_id="surface-a")))
        registry.register(registration(ollama_identity(resource_id="surface-b")))
        read = registry.registry_snapshot(now=T0)
        self.assertEqual(read.quota_pools, ())

    def test_entries_are_ordered_by_resource_id(self) -> None:
        registry = ResourceRegistry()
        registry.register(registration(ollama_identity()))
        registry.register(registration(codex_identity()))
        read = registry.registry_snapshot(now=T0)
        self.assertEqual(
            [entry.identity.resource_id for entry in read.entries],
            ["lab-worker-ollama-qwen3", "openai-codex-sub"],
        )

    def test_one_resource_failure_does_not_invalidate_another(self) -> None:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration(codex_identity()))
        registry.register(registration(ollama_identity()))
        registry.apply_snapshot(resource_snapshot(codex_identity()))
        registry.apply_snapshot(
            resource_snapshot(ollama_identity(), status="unavailable")
        )
        read = registry.registry_snapshot(now=T0)
        by_id = {entry.identity.resource_id: entry for entry in read.entries}
        codex_entry = by_id["openai-codex-sub"]
        assert codex_entry.observation is not None
        ollama_entry = by_id["lab-worker-ollama-qwen3"]
        assert ollama_entry.observation is not None
        self.assertEqual(codex_entry.observation.health.status, "ok")
        self.assertEqual(codex_entry.freshness, "fresh")
        self.assertEqual(ollama_entry.observation.health.status, "unavailable")
        # The unavailable resource is honestly unavailable, and the healthy
        # one is unaffected; the unavailable one is not "stale" either.
        self.assertEqual(ollama_entry.freshness, "fresh")

    def test_registry_read_round_trips_serialization(self) -> None:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration())
        registry.apply_snapshot(resource_snapshot())
        read = registry.registry_snapshot(now=T0)
        self.assertEqual(RegistrySnapshot.from_dict(read.to_dict()), read)

    def test_registry_read_rejects_wrong_version(self) -> None:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration())
        payload = registry.registry_snapshot(now=T0).to_dict()
        payload["schema_version"] = 2
        with self.assertRaises(CapacityValidationError):
            _ = RegistrySnapshot.from_dict(payload)

    def test_registry_read_version_is_frozen_at_one(self) -> None:
        self.assertEqual(REGISTRY_SCHEMA_VERSION, 1)


# ── cross-resource registry-state invariants ──────────────────────────────────


class TestRegistryCrossResourceInvariants(unittest.TestCase):
    """Registry read state must never mix one resource with another's data."""

    def test_entry_rejects_mismatched_observation_identity(self) -> None:
        entry_identity = codex_identity()
        foreign_observation = resource_snapshot(ollama_identity())
        with self.assertRaises(CapacityValidationError):
            _ = ResourceRegistryEntry(
                identity=entry_identity,
                freshness_ttl_seconds=TTL,
                poll_interval_seconds=None,
                capabilities=ExecutionCapabilities(),
                cost=None,
                observation=foreign_observation,
                freshness="fresh",
                refresh_due=False,
            )

    def test_entry_from_dict_rejects_mismatched_observation_identity(self) -> None:
        payload = ResourceRegistryEntry(
            identity=codex_identity(),
            freshness_ttl_seconds=TTL,
            poll_interval_seconds=POLL,
            capabilities=ExecutionCapabilities(),
            cost=None,
            observation=resource_snapshot(codex_identity()),
            freshness="fresh",
            refresh_due=False,
        ).to_dict()
        # Swap in another resource's observation under this entry's read.
        payload["observation"] = resource_snapshot(ollama_identity()).to_dict()
        with self.assertRaises(CapacityValidationError):
            _ = ResourceRegistryEntry.from_dict(payload)

    def test_registry_snapshot_from_dict_rejects_cross_resource_state(self) -> None:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration(codex_identity()))
        registry.register(registration(ollama_identity()))
        registry.apply_snapshot(resource_snapshot(codex_identity()))
        read_payload = registry.registry_snapshot(now=T0).to_dict()
        entries = cast("list[dict[str, object]]", read_payload["entries"])
        codex_entry = next(
            entry
            for entry in entries
            if cast("dict[str, object]", entry["identity"])["resource_id"]
            == "openai-codex-sub"
        )
        # Tamper: the codex entry now carries the (absent) ollama slot's
        # observation identity — a different resource's state.
        codex_entry["observation"] = resource_snapshot(
            ollama_identity(), status="unavailable"
        ).to_dict()
        with self.assertRaises(CapacityValidationError):
            _ = RegistrySnapshot.from_dict(read_payload)

    def test_matching_identity_still_round_trips(self) -> None:
        entry = ResourceRegistryEntry(
            identity=codex_identity(),
            freshness_ttl_seconds=TTL,
            poll_interval_seconds=POLL,
            capabilities=ExecutionCapabilities(streaming=True),
            cost=ResourceCost(observation_class="estimate", input_micro_usd_per_mtoken=1),
            observation=resource_snapshot(codex_identity()),
            freshness="fresh",
            refresh_due=False,
        )
        self.assertEqual(ResourceRegistryEntry.from_dict(entry.to_dict()), entry)


# ── future timestamps ─────────────────────────────────────────────────────────


class TestFutureTimestampSemantics(unittest.TestCase):
    """A future observation is never silently treated as fresh."""

    def test_classify_freshness_rejects_future_observation(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = classify_freshness(
                observed_at=T0_PLUS_301, now=T0, freshness_ttl_seconds=TTL
            )

    def test_apply_rejects_future_dated_observation(self) -> None:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration())
        with self.assertRaises(CapacityValidationError):
            registry.apply_snapshot(
                resource_snapshot(observed_at=T0_PLUS_301)
            )
        # Nothing was stored: the resource stays never-observed and the
        # revision is unchanged.
        read = registry.registry_snapshot(now=T0)
        self.assertEqual(read.revision, 1)
        self.assertIsNone(read.entries[0].observation)
        self.assertEqual(read.entries[0].freshness, "never_observed")

    def test_worker_report_with_future_dated_entry_is_rejected_atomically(
        self,
    ) -> None:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration(codex_identity()))
        registry.register(registration(ollama_identity()))
        registry.apply_snapshot(resource_snapshot(codex_identity()))
        baseline = registry.registry_snapshot(now=T0)
        bad_report = WorkerStateReport(
            schema_version=WORKER_REPORT_SCHEMA_VERSION,
            worker_id="lab-worker-01",
            reported_at=T0_PLUS_301,
            resources=(
                resource_snapshot(ollama_identity(), observed_at=T0_PLUS_301),
            ),
        )
        with self.assertRaises(CapacityValidationError):
            registry.apply_worker_report(bad_report)
        after = registry.registry_snapshot(now=T0)
        self.assertEqual(after.revision, baseline.revision)
        by_id = {entry.identity.resource_id: entry for entry in after.entries}
        self.assertEqual(by_id["lab-worker-ollama-qwen3"].freshness, "never_observed")

    def test_backwards_evaluation_instant_fails_closed(self) -> None:
        # Applied against a clock at T0, a read evaluated before T0 refuses
        # to classify the stored observation instead of calling it fresh.
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration())
        registry.apply_snapshot(resource_snapshot(observed_at=T0))
        with self.assertRaises(CapacityValidationError):
            _ = registry.registry_snapshot(now=T_MINUS_1)

    def test_refresh_due_rejects_future_observation(self) -> None:
        # Same fail-closed time seam as freshness evaluation: an
        # observation dated after the evaluation instant is rejected, not
        # silently treated as "not due" via a negative age.
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration(poll=POLL))
        registry.apply_snapshot(resource_snapshot(observed_at=T0))
        with self.assertRaises(CapacityValidationError):
            _ = registry.refresh_due(now=T_MINUS_1)

    def test_refresh_due_boundary_behavior_is_unchanged(self) -> None:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration(poll=POLL))
        registry.apply_snapshot(resource_snapshot(observed_at=T0))
        # Age 0 is a valid, non-negative age: not due, never an error.
        self.assertEqual(registry.refresh_due(now=T0), ())
        self.assertEqual(registry.refresh_due(now=T0_PLUS_300), ("openai-codex-sub",))
        # A never-observed resource compares no timestamps, so it stays
        # due at any evaluation instant.
        fresh_registry = ResourceRegistry()
        fresh_registry.register(registration(ollama_identity(), poll=POLL))
        self.assertEqual(
            fresh_registry.refresh_due(now=T_MINUS_1),
            ("lab-worker-ollama-qwen3",),
        )

    def test_boundary_now_equal_to_observed_at_is_fresh(self) -> None:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration())
        registry.apply_snapshot(resource_snapshot(observed_at=T0))
        self.assertEqual(
            registry.registry_snapshot(now=T0).entries[0].freshness, "fresh"
        )


# ── worker report application ─────────────────────────────────────────────────


class TestWorkerReportApplication(unittest.TestCase):
    """Worker reports apply atomically or not at all — and never touch policy."""

    def _registry_with_baseline(self) -> tuple[ResourceRegistry, int]:
        registry = ResourceRegistry(clock=lambda: T0)
        registry.register(registration(codex_identity()))
        registry.register(registration(ollama_identity()))
        registry.apply_snapshot(resource_snapshot(codex_identity()))
        baseline_revision = registry.registry_snapshot(now=T0).revision
        return registry, baseline_revision

    def _report(
        self, resources: tuple[ResourceStateSnapshot, ...]
    ) -> WorkerStateReport:
        return WorkerStateReport(
            schema_version=WORKER_REPORT_SCHEMA_VERSION,
            worker_id="lab-worker-01",
            reported_at=T0,
            resources=resources,
        )

    def test_valid_report_updates_its_resources_only(self) -> None:
        registry, _ = self._registry_with_baseline()
        registry.apply_worker_report(
            self._report(
                (
                    resource_snapshot(
                        ollama_identity(),
                        status="ok",
                        quota_facts=(
                            QuotaFact(
                                observation_class="local_limit",
                                window=CapacityWindow.from_dict(
                                    window(
                                        scope_id=None,
                                        used=10,
                                        remaining=90,
                                        duration_seconds=None,
                                    )
                                ),
                            ),
                        ),
                    ),
                )
            )
        )
        read = registry.registry_snapshot(now=T0)
        by_id = {entry.identity.resource_id: entry for entry in read.entries}
        ollama_entry = by_id["lab-worker-ollama-qwen3"]
        assert ollama_entry.observation is not None
        codex_entry = by_id["openai-codex-sub"]
        assert codex_entry.observation is not None
        self.assertEqual(
            ollama_entry.observation.quota_facts[0].observation_class,
            "local_limit",
        )
        # The server-observed resource is untouched by the worker report.
        self.assertEqual(codex_entry.observation.health.status, "ok")

    def test_report_cannot_change_registered_policy_or_configured_facts(
        self,
    ) -> None:
        registry, _ = self._registry_with_baseline()
        before = registry.registry_snapshot(now=T0)
        registry.apply_worker_report(
            self._report((resource_snapshot(ollama_identity()),))
        )
        after = registry.registry_snapshot(now=T0)
        for before_entry, after_entry in zip(before.entries, after.entries):
            self.assertEqual(
                before_entry.freshness_ttl_seconds,
                after_entry.freshness_ttl_seconds,
            )
            self.assertEqual(
                before_entry.poll_interval_seconds,
                after_entry.poll_interval_seconds,
            )
            self.assertEqual(before_entry.capabilities, after_entry.capabilities)
            self.assertEqual(before_entry.cost, after_entry.cost)

    def test_report_with_one_bad_entry_changes_nothing(self) -> None:
        registry, baseline_revision = self._registry_with_baseline()
        bad_report = self._report(
            (
                resource_snapshot(
                    ollama_identity(resource_id="unregistered-resource")
                ),
            )
        )
        with self.assertRaises(CapacityValidationError):
            registry.apply_worker_report(bad_report)
        read = registry.registry_snapshot(now=T0)
        self.assertEqual(read.revision, baseline_revision)
        by_id = {entry.identity.resource_id: entry for entry in read.entries}
        self.assertNotIn("unregistered-resource", by_id)
        self.assertEqual(
            by_id["lab-worker-ollama-qwen3"].freshness, "never_observed"
        )

    def test_mismatched_identity_in_report_changes_nothing(self) -> None:
        registry, baseline_revision = self._registry_with_baseline()
        bad_report = self._report(
            (resource_snapshot(ollama_identity(entitlement="payg_metered")),)
        )
        with self.assertRaises(CapacityValidationError):
            registry.apply_worker_report(bad_report)
        self.assertEqual(
            registry.registry_snapshot(now=T0).revision,
            baseline_revision,
        )


# ── U-003 freshness rule ──────────────────────────────────────────────────────


class TestClassifyFreshness(unittest.TestCase):
    def test_boundary_is_inclusive(self) -> None:
        self.assertEqual(
            classify_freshness(
                observed_at=T0, now=T0_PLUS_300, freshness_ttl_seconds=TTL
            ),
            "fresh",
        )
        self.assertEqual(
            classify_freshness(
                observed_at=T0, now=T0_PLUS_301, freshness_ttl_seconds=TTL
            ),
            "stale",
        )

    def test_rejects_nonpositive_ttl(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = classify_freshness(observed_at=T0, now=T0, freshness_ttl_seconds=0)

    def test_rejects_noncanonical_timestamps(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = classify_freshness(
                observed_at="2026-09-15T12:00:00Z",
                now=T0,
                freshness_ttl_seconds=TTL,
            )


# ── registration ──────────────────────────────────────────────────────────────


class TestResourceRegistration(unittest.TestCase):
    def test_requires_positive_ttl(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = ResourceRegistration(
                identity=codex_identity(),
                freshness_ttl_seconds=0,
            )

    def test_carries_configuration_facts(self) -> None:
        cost = ResourceCost(
            observation_class="estimate", input_micro_usd_per_mtoken=1
        )
        config = registration(
            capabilities=ExecutionCapabilities(streaming=True),
            cost=cost,
            poll=None,
        )
        self.assertEqual(config.capabilities.streaming, True)
        self.assertEqual(config.cost, cost)
        self.assertIsNone(config.poll_interval_seconds)


if __name__ == "__main__":
    _ = unittest.main()
