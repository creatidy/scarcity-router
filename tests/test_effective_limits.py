"""Effective-limits semantics at the execution surface (#136, D-056/D-058).

The effective capability of one route is the intersection of four layers
(D-056): the logical model's hard capabilities, the execution-channel's
evidenced capabilities, the administrator's allowance and the request's
own requirements (which may only narrow). These tests pin the #136
implementation at the public HTTP boundary:

- honest defaults: the generic administrator ceiling no longer sits
  invisibly below evidenced capability (the supported coding-agent
  workload passes);
- rejection semantics are unchanged: every violation is a typed 400,
  never silent clipping; an explicitly lowered administrator ceiling is
  enforced;
- per-route enforcement: a channel below the model's capability exposes
  the weaker effective ceiling for that route, while the advertised
  model-level number stays the strongest bound route's effective ceiling
  with the honest per-route ``routes`` detail beside it;
- the Codex output-limit rule: a channel that evidences NO output-limit
  control normalizes away only a NON-BINDING requested limit (at or
  above the model's proven hard maximum, audited), rejects a binding one
  (``output_limit_unenforceable``) and never normalizes against an
  UNKNOWN hard maximum;
- configuration honesty: non-default limits are exported, defaults are
  not, and an explicitly pinned legacy value stays explicit.

Everything is synthetic: the fixtures' fake backends and identities, no
live provider, no network beyond the loopback test listener.
"""

from __future__ import annotations

import json
import threading
import unittest
from typing import cast

from collections.abc import Callable

from scarcity_router.gateway_adapters import (
    AdapterCall,
    AdapterResult,
    CallObservation,
    ExecutionContext,
)
from scarcity_router.gateway_contracts import GatewayLimits
from scarcity_router.gateway_server import GatewayHTTPServer, make_gateway_server
from scarcity_router.resource_state import (
    ExecutionCapabilities,
    ResourceIdentity,
    ResourceRegistration,
    ResourceRegistry,
)
from scarcity_router.selection_types import (
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
)
from scarcity_router.routing_core import CompatibilityCell
from scarcity_router.selection_types import CapacityScopeRef
from tests.gateway_fixtures import (
    ALL_FEATURES,
    CLIENT_KEY,
    EVIDENCE,
    GatewayApplication,
    ScriptedAdapter,
    ambiguous_failure_behavior,
    audit_records,
    build_catalog,
    build_cells,
    build_registry,
    make_application,
    permanent_failure_behavior,
    timeout_behavior,
)
from tests.gateway_fixtures import (  # noqa: F401 -- fixture privates, single-world helpers
    _capabilities as _capabilities,  # pyright: ignore[reportPrivateUsage]
    _entry as _entry,  # pyright: ignore[reportPrivateUsage]
    _observation as _observation,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_gateway_server import ServerHarness, as_dict, as_list

AUTH = {"Authorization": f"Bearer {CLIENT_KEY}"}


def chat_body(**overrides: object) -> dict[str, object]:
    """A minimal supported-model chat request (logical id, max effort)."""
    body: dict[str, object] = {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


class LimitsHarness(ServerHarness):
    """ServerHarness over an explicitly assembled application world."""

    application: GatewayApplication
    server: GatewayHTTPServer
    thread: threading.Thread
    adapters: dict[str, ScriptedAdapter]

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.adapters = {}

    def make_world(
        self,
        *,
        registry: ResourceRegistry | None = None,
        cells: tuple[CompatibilityCell, ...] | None = None,
        limits: GatewayLimits | None = None,
        catalog: ModelCatalog | None = None,
        channels: tuple[str, ...] = ("server_direct_http", "worker_bridged"),
        behavior: Callable[[AdapterCall, ExecutionContext], AdapterResult] | None = None,
    ) -> int:
        self.adapters = {
            channel: ScriptedAdapter(channel=channel, behavior=behavior)
            for channel in channels
        }
        application = make_application(
            registry=registry,
            cells=cells if cells is not None else build_cells(include_worker=True),
            adapters=[self.adapters[channel] for channel in channels],
            limits=limits,
            catalog=catalog,
        )
        return self._serve(application)

    def adapter_for(self, channel: str) -> ScriptedAdapter:
        return self.adapters[channel]

    def _serve(self, application: GatewayApplication) -> int:
        server = make_gateway_server(application, host="127.0.0.1", port=0)
        self.application = application
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        _, port = self.server.server_address[:2]
        return int(port)


def registry_with(
    *extra: tuple[ResourceIdentity, ExecutionCapabilities],
) -> ResourceRegistry:
    """The default healthy world plus extra (identity, capabilities)."""
    registry = build_registry()
    for identity, capabilities in extra:
        registry.register(
            ResourceRegistration(
                identity=identity,
                freshness_ttl_seconds=300,
                capabilities=capabilities,
            )
        )
        registry.apply_snapshot(_observation(identity))
    return registry


def bare_registry(
    *pairs: tuple[ResourceIdentity, ExecutionCapabilities],
) -> ResourceRegistry:
    """A registry holding ONLY the given (identity, capabilities) routes."""
    registry = ResourceRegistry(clock=lambda: "2026-09-15T12:05:00.000Z")
    for identity, capabilities in pairs:
        registry.register(
            ResourceRegistration(
                identity=identity,
                freshness_ttl_seconds=300,
                capabilities=capabilities,
            )
        )
        registry.apply_snapshot(_observation(identity))
    return registry


def _entry_without_output(provider: str, model: str, variant: str):
    """A calibrated catalog entry whose hard output stays UNKNOWN."""
    base = build_catalog().entries[0]
    return ModelCatalogEntry(
        identity=ModelIdentity(provider=provider, model=model, variant=variant),
        display_name=model,
        hard_properties=ModelHardProperties(
            input_context_tokens=272_000,
            output_tokens=None,
            supports_tool_use=True,
            supports_vision=False,
            supports_reasoning_mode=True,
        ),
        capabilities=_capabilities(4, 4, 4),
        capacity_bindings=base.capacity_bindings,
        reasoning_effort=variant,
    )


class HonestDefaultsTests(LimitsHarness):
    """The #136 defaults admit the supported workload; rejections stand."""

    def test_default_supported_model_request_is_accepted(self) -> None:
        """A normal supported-model request — including an explicit output
        ceiling equal to the model's evidenced hard maximum — is admitted
        end to end, and the limit is carried to the adapter (the channel
        has no contrary evidence)."""
        port = self.make_world()
        response = self.post_chat(port, chat_body(max_completion_tokens=128_000))
        self.assertEqual(response.status, 200)
        server_direct = self.adapter_for("server_direct_http")
        self.assertEqual(server_direct.dispatch_count, 1)
        self.assertEqual(server_direct.dispatches[0].max_output_tokens, 128_000)

    def test_large_but_bounded_request_is_accepted(self) -> None:
        """A real agent-sized conversation (≈100k estimated tokens — far
        beyond the old 131072 input posture) executes under the honest
        defaults."""
        port = self.make_world()
        big = "x" * 400_000  # ~100k tokens at the documented chars/4 floor
        response = self.post_chat(
            port, chat_body(messages=[{"role": "user", "content": big}])
        )
        self.assertEqual(response.status, 200)

    def test_over_admin_output_limit_is_rejected_unchanged(self) -> None:
        """Above the administrator output ceiling the typed rejection is
        unchanged (never clipped, never silently dropped)."""
        port = self.make_world()
        response = self.post_chat(port, chat_body(max_completion_tokens=131_073))
        self.assertEqual(response.status, 400)
        error = as_dict(as_dict(cast("dict[str, object]", json.loads(response.read()))["error"]))
        self.assertEqual(error["code"], "output_limit_exceeded")
        self.assertEqual(error["param"], "max_completion_tokens")
        self.assertEqual(self.adapter_for("server_direct_http").dispatch_count, 0)

    def test_over_admin_context_limit_is_rejected_unchanged(self) -> None:
        port = self.make_world()
        # ~2.13M estimated tokens: above the 2^21 administrator guard but
        # below the 16 MiB body bound, so the typed admin pre-check fires
        # (the per-route channel gate is exercised by the capability
        # tests below).
        huge = "x" * 8_500_000
        response = self.post_chat(
            port, chat_body(messages=[{"role": "user", "content": huge}])
        )
        self.assertEqual(response.status, 400)
        error = as_dict(as_dict(cast("dict[str, object]", json.loads(response.read()))["error"]))
        self.assertEqual(error["code"], "context_length_exceeded")

    def test_explicit_lower_administrator_output_ceiling_is_enforced(self) -> None:
        """An operator who knowingly lowers the ceiling is authoritative:
        requests above it are rejected, requests within it execute — the
        ceiling is visible configuration, not a hidden default."""
        port = self.make_world(limits=GatewayLimits(max_output_tokens=1_000))
        above = self.post_chat(port, chat_body(max_completion_tokens=2_000))
        self.assertEqual(above.status, 400)
        error = as_dict(cast("dict[str, object]", json.loads(above.read()))["error"])
        self.assertEqual(error["code"], "output_limit_exceeded")
        within = self.post_chat(port, chat_body(max_completion_tokens=800))
        self.assertEqual(within.status, 200)

    def test_explicit_lower_administrator_context_ceiling_is_enforced(self) -> None:
        port = self.make_world(limits=GatewayLimits(max_input_context_tokens=64))
        response = self.post_chat(
            port,
            chat_body(messages=[{"role": "user", "content": "x" * 1_000}]),
        )
        self.assertEqual(response.status, 400)
        error = as_dict(as_dict(cast("dict[str, object]", json.loads(response.read()))["error"]))
        self.assertEqual(error["code"], "context_length_exceeded")


class PerRouteIntersectionTests(LimitsHarness):
    """Channel below model capability: enforced per route, advertised
    honestly at model level (D-056 intersection, #136/D-058)."""

    def test_weaker_channel_is_enforced_and_advertised(self) -> None:
        small = ResourceIdentity(
            resource_id="small-output",
            channel="server_direct_http",
            provider="openai",
            model="gpt-5.6-luna",
            entitlement="subscription_included",
        )
        registry = registry_with(
            (
                small,
                ExecutionCapabilities(
                    context_limit_tokens=272_000,
                    output_limit_tokens=32_000,
                ),
            )
        )
        port = self.make_world(registry=registry)
        # The advertised model-level output stays the STRONGEST bound
        # route's effective ceiling; the per-route detail exposes the
        # weaker route beside it — never collapsed in either direction.
        connection = self.client(port)
        connection.request("GET", "/v1/models", headers=dict(AUTH))
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        entries = [as_dict(entry) for entry in as_list(payload["data"])]
        luna = next(entry for entry in entries if entry["id"] == "gpt-5.6-luna")
        metadata = as_dict(luna["x_scarcity_router"])
        self.assertEqual(metadata["max_output_tokens"], 128_000)
        self.assertEqual(metadata["effective_context_limit_tokens"], 272_000)
        routes = [as_dict(route) for route in as_list(metadata["routes"])]
        by_id = {route["resource_id"]: route for route in routes}
        self.assertEqual(
            by_id["openai-http"]["effective_output_limit_tokens"], 128_000
        )
        self.assertEqual(
            by_id["small-output"]["effective_output_limit_tokens"], 32_000
        )
        # A request that fits the weaker route executes through it.
        within = self.post_chat(port, chat_body(max_completion_tokens=32_000))
        self.assertEqual(within.status, 200)
        # A request above the weaker route's effective ceiling, pinned to
        # it, is rejected with the typed over-limit error — admission is
        # route-specific and rejection-based.
        pinned = "sr-pin:small-output/openai/gpt-5.6-luna/max"
        above = self.post_chat(
            port, chat_body(model=pinned, max_completion_tokens=64_000)
        )
        self.assertEqual(above.status, 400)
        error = as_dict(cast("dict[str, object]", json.loads(above.read()))["error"])
        # Rejected at PIN ADMISSION, before ranking could ever prefer it:
        # the route's evidenced output ceiling is below the request.
        self.assertEqual(error["code"], "output_limit_insufficient")

    def test_request_within_advertised_maximum_executes(self) -> None:
        """The advertised headline is executable: a request at the
        strongest route's effective ceiling passes admission."""
        port = self.make_world()
        pinned = "sr-pin:openai-http/openai/gpt-5.6-luna/max"
        response = self.post_chat(
            port, chat_body(model=pinned, max_completion_tokens=128_000)
        )
        self.assertEqual(response.status, 200)


class UnenforceableOutputControlTests(LimitsHarness):
    """A channel that evidences NO output-limit control (the Codex
    execution surface, 2026-09-28 re-evidence): the #136 normalization
    rule."""

    _PIN: str = "sr-pin:codex-luna/openai/gpt-5.6-luna/max"

    def _codex_world(self) -> int:
        codex = ResourceIdentity(
            resource_id="codex-luna",
            channel="worker_bridged",
            provider="openai",
            model="gpt-5.6-luna",
            entitlement="subscription_included",
            variant="max",
        )
        registry = registry_with(
            (
                codex,
                ExecutionCapabilities(
                    context_limit_tokens=272_000,
                    output_limit_control=False,
                ),
            )
        )
        return self.make_world(registry=registry)

    def test_non_binding_limit_is_normalized_and_audited(self) -> None:
        """A requested limit at/above the model's proven hard maximum can
        never bind (the runtime enforces the hard maximum itself), so it
        is normalized away — dispatched WITHOUT the limit — and the
        normalization is audited."""
        port = self._codex_world()
        response = self.post_chat(
            port, chat_body(model=self._PIN, max_completion_tokens=128_000)
        )
        self.assertEqual(response.status, 200)
        bridged = self.adapter_for("worker_bridged")
        self.assertEqual(bridged.dispatch_count, 1)
        self.assertIsNone(bridged.dispatches[0].max_output_tokens)
        record = audit_records(self.application)[-1]
        self.assertIn("output_limit_normalized", record.reason_codes)

    def test_binding_limit_below_hard_max_is_rejected_closed(self) -> None:
        """A smaller (binding) requested limit would fake semantics if
        silently ignored on a channel that cannot enforce it: it is
        rejected before dispatch instead."""
        port = self._codex_world()
        response = self.post_chat(
            port, chat_body(model=self._PIN, max_completion_tokens=64_000)
        )
        self.assertEqual(response.status, 400)
        error = as_dict(as_dict(cast("dict[str, object]", json.loads(response.read()))["error"]))
        # Admission rejections map the routing code verbatim (no param —
        # consistent with the context-ceiling admission mapping).
        self.assertEqual(error["code"], "output_limit_unenforceable")
        self.assertEqual(self.adapter_for("worker_bridged").dispatch_count, 0)
        record = audit_records(self.application)[-1]
        self.assertEqual(record.result_status, "rejected")

    def test_unknown_hard_maximum_is_never_normalized(self) -> None:
        """Without a proven hard maximum there is no non-binding ceiling:
        even a large requested limit is rejected, never normalized against
        a guess."""
        base = build_catalog()
        catalog = ModelCatalog(
            catalog_version=base.catalog_version,
            updated_on=base.updated_on,
            entries=base.entries
            + (_entry_without_output("openai", "gpt-x-unknown", "max"),),
        )
        codex = ResourceIdentity(
            resource_id="codex-unknown",
            channel="worker_bridged",
            provider="openai",
            model="gpt-x-unknown",
            entitlement="subscription_included",
            variant="max",
        )
        registry = registry_with(
            (
                codex,
                ExecutionCapabilities(
                    context_limit_tokens=272_000,
                    output_limit_control=False,
                ),
            )
        )
        # PASS cells for the synthetic unknown-output backend so the
        # matrix gate is not the failure under test here.
        cells = build_cells(include_worker=True) + tuple(
            CompatibilityCell(
                channel="worker_bridged",
                provider="openai",
                model="gpt-x-unknown",
                feature=feature,
                value="PASS",
                adapter="synthetic-http",
                adapter_version="1.2.3",
                evidence=EVIDENCE,
            )
            for feature in (
                "roles_history",
                "streaming",
                "tool_calls",
                "tool_results",
                "structured_output",
                "reasoning_controls",
            )
        )
        port = self.make_world(registry=registry, catalog=catalog, cells=cells)
        pinned = "sr-pin:codex-unknown/openai/gpt-x-unknown/max"
        # Below the administrator ceiling (so the global pre-check passes,
        # it is authoritative regardless of capability knowledge) but with
        # no proven hard maximum on the model: nothing certifies a
        # non-binding level, so the explicit limit is rejected closed.
        response = self.post_chat(
            port, chat_body(model=pinned, max_completion_tokens=100_000)
        )
        self.assertEqual(response.status, 400)
        error = as_dict(as_dict(cast("dict[str, object]", json.loads(response.read()))["error"]))
        # With no evidenced capability input at all the honest rejection is
        # the UNKNOWN code (fail closed), never a guessed normalization.
        self.assertEqual(error["code"], "output_limit_unknown")
        self.assertEqual(self.adapter_for("worker_bridged").dispatch_count, 0)


class OutputSteeringTests(LimitsHarness):
    """Blocker 1 regression: output capability gates routes BEFORE ranking.

    Two routes serve the SAME exact model+effort; the weak route is
    otherwise preferred (the stable resource-id ordering decides without a
    promotion). A request whose output requirement only the strong route
    satisfies MUST execute on the strong route — never select-weak-then-
    reject (D-056: a request routes only to a source whose evidenced
    capability satisfies the full semantic request).
    """

    WEAK: ResourceIdentity = ResourceIdentity(
        resource_id="a-weak-output",
        channel="server_direct_http",
        provider="openai",
        model="gpt-5.6-luna",
        entitlement="subscription_included",
    )
    STRONG: ResourceIdentity = ResourceIdentity(
        resource_id="z-strong-output",
        channel="worker_bridged",
        provider="openai",
        model="gpt-5.6-luna",
        entitlement="subscription_included",
    )

    def _world(self, *, strong_only: bool = False, weak_only: bool = False) -> int:
        pairs: list[tuple[ResourceIdentity, ExecutionCapabilities]] = []
        if not strong_only:
            pairs.append(
                (
                    self.WEAK,
                    ExecutionCapabilities(
                        context_limit_tokens=272_000,
                        output_limit_tokens=32_000,
                        output_limit_control=True,
                    ),
                )
            )
        if not weak_only:
            pairs.append(
                (
                    self.STRONG,
                    ExecutionCapabilities(
                        context_limit_tokens=272_000,
                        output_limit_tokens=128_000,
                        output_limit_control=True,
                    ),
                )
            )
        return self.make_world(registry=bare_registry(*pairs))

    def test_strong_route_executes_when_weak_is_preferred(self) -> None:
        port = self._world()
        response = self.post_chat(port, chat_body(max_completion_tokens=64_000))
        self.assertEqual(response.status, 200)
        record = audit_records(self.application)[-1]
        assert record.selected_target is not None
        # The weak route sorts first and would win the stable-id ordering;
        # pre-ranking output eligibility removes it from the candidate set.
        self.assertEqual(record.selected_target.resource_id, "z-strong-output")
        self.assertEqual(
            self.adapter_for("worker_bridged").dispatch_count, 1
        )
        self.assertEqual(
            self.adapter_for("server_direct_http").dispatch_count, 0
        )

    def test_weak_only_world_is_explicitly_rejected(self) -> None:
        """With no route satisfying the output requirement, the rejection
        is the TYPED output 400 (mapped from the pre-ranking exclusions,
        pinned and unpinned alike) — not a generic 503."""
        port = self._world(weak_only=True)
        response = self.post_chat(port, chat_body(max_completion_tokens=64_000))
        self.assertEqual(response.status, 400)
        error = as_dict(cast("dict[str, object]", json.loads(response.read()))["error"])
        self.assertEqual(error["code"], "output_limit_insufficient")
        self.assertEqual(
            self.adapter_for("server_direct_http").dispatch_count, 0
        )

    def test_pin_to_weak_route_is_explicitly_rejected(self) -> None:
        port = self._world()
        response = self.post_chat(
            port,
            chat_body(
                model="sr-pin:a-weak-output/openai/gpt-5.6-luna/max",
                max_completion_tokens=64_000,
            ),
        )
        self.assertEqual(response.status, 400)
        error = as_dict(cast("dict[str, object]", json.loads(response.read()))["error"])
        self.assertEqual(error["code"], "output_limit_insufficient")
        self.assertEqual(
            self.adapter_for("server_direct_http").dispatch_count, 0
        )

    def test_pin_to_strong_route_executes(self) -> None:
        port = self._world()
        response = self.post_chat(
            port,
            chat_body(
                model="sr-pin:z-strong-output/openai/gpt-5.6-luna/max",
                max_completion_tokens=64_000,
            ),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.adapter_for("worker_bridged").dispatch_count, 1)

    def test_mixed_output_and_availability_stays_503(self) -> None:
        """Review blocker regression: the typed output 400 is reserved for
        decisions caused PURELY by the output dimension. Route A is
        available but its 32k ceiling cannot satisfy 64k; route B could
        satisfy it but is currently UNAVAILABLE. The ordinary
        no-eligible-target 503 stands — the request could succeed
        unchanged when B becomes available — and once B recovers the same
        request executes there unchanged."""
        weak = ResourceIdentity(
            resource_id="a-weak-output",
            channel="server_direct_http",
            provider="openai",
            model="gpt-5.6-luna",
            entitlement="subscription_included",
        )
        strong = ResourceIdentity(
            resource_id="z-strong-output",
            channel="worker_bridged",
            provider="openai",
            model="gpt-5.6-luna",
            entitlement="subscription_included",
        )
        registry = ResourceRegistry(clock=lambda: "2026-09-15T12:05:00.000Z")
        for identity, capabilities in (
            (
                weak,
                ExecutionCapabilities(
                    context_limit_tokens=272_000,
                    output_limit_tokens=32_000,
                    output_limit_control=True,
                ),
            ),
            (
                strong,
                ExecutionCapabilities(
                    context_limit_tokens=272_000,
                    output_limit_tokens=128_000,
                    output_limit_control=True,
                ),
            ),
        ):
            registry.register(
                ResourceRegistration(
                    identity=identity,
                    freshness_ttl_seconds=300,
                    capabilities=capabilities,
                )
            )
        registry.apply_snapshot(_observation(weak))
        # The strong route is registered but NEVER observed: an
        # availability-stage failure, not an output one.
        port = self.make_world(registry=registry)
        response = self.post_chat(port, chat_body(max_completion_tokens=64_000))
        self.assertEqual(response.status, 503)
        error = as_dict(
            cast("dict[str, object]", json.loads(response.read()))["error"]
        )
        self.assertEqual(error["code"], "no_eligible_target")
        self.assertEqual(
            self.adapter_for("server_direct_http").dispatch_count, 0
        )
        # B recovers: the same request now executes on the strong route,
        # unchanged — exactly why the mixed case must not be a 400.
        registry.apply_snapshot(_observation(strong))
        recovered = self.post_chat(
            port, chat_body(max_completion_tokens=64_000)
        )
        self.assertEqual(recovered.status, 200)
        record = audit_records(self.application)[-1]
        assert record.selected_target is not None
        self.assertEqual(record.selected_target.resource_id, "z-strong-output")

    def test_unknown_output_capability_fails_closed(self) -> None:
        """A route with NO evidenced output capability (hard UNKNOWN, channel
        UNKNOWN) cannot prove it satisfies an explicit output requirement:
        fail closed for that route, pinned or unpinned."""
        unknown = ResourceIdentity(
            resource_id="a-unknown-output",
            channel="worker_bridged",
            provider="openai",
            model="gpt-x-unknown",
            entitlement="subscription_included",
            variant="max",
        )
        cells = build_cells(include_worker=True) + tuple(
            CompatibilityCell(
                channel="worker_bridged",
                provider="openai",
                model="gpt-x-unknown",
                feature=feature,
                value="PASS",
                adapter="synthetic-http",
                adapter_version="1.2.3",
                evidence=EVIDENCE,
            )
            for feature in ALL_FEATURES
        )
        catalog = ModelCatalog(
            catalog_version=1,
            updated_on="2026-09-01",
            entries=(build_catalog().entries[0], _entry_without_output(
                "openai", "gpt-x-unknown", "max"
            )),
        )
        port = self.make_world(
            registry=bare_registry(
                (unknown, ExecutionCapabilities(context_limit_tokens=272_000))
            ),
            catalog=catalog,
            cells=cells,
        )
        pinned = "sr-pin:a-unknown-output/openai/gpt-x-unknown/max"
        response = self.post_chat(
            port, chat_body(model=pinned, max_completion_tokens=1_000)
        )
        self.assertEqual(response.status, 400)
        error = as_dict(cast("dict[str, object]", json.loads(response.read()))["error"])
        self.assertEqual(error["code"], "output_limit_unknown")
        self.assertEqual(self.adapter_for("worker_bridged").dispatch_count, 0)


class UnpinnedNoControlChannelTests(LimitsHarness):
    """Exact-head review blocker regression: on the shipped no-control
    channel shape (CODEX_SURFACE_CAPABILITIES), an unpinned request with an
    explicit BINDING sub-maximum limit finds no eligible route pre-ranking
    and is rejected with the TYPED, actionable 400 — never the generic
    retry-suggesting 503 no_eligible_target."""

    _CODEX: ResourceIdentity = ResourceIdentity(
        resource_id="codex-luna",
        channel="worker_bridged",
        provider="openai",
        model="gpt-5.6-luna",
        entitlement="subscription_included",
        variant="max",
    )

    def _world(self) -> int:
        return self.make_world(
            registry=bare_registry(
                (
                    self._CODEX,
                    ExecutionCapabilities(
                        context_limit_tokens=272_000,
                        output_limit_control=False,
                    ),
                )
            ),
            channels=("worker_bridged",),
        )

    def test_binding_limit_without_eligible_route_is_typed(self) -> None:
        port = self._world()
        # 64000 binds against the exact variant's 128000 proven maximum on
        # the only (control=False) route: excluded everywhere pre-ranking.
        response = self.post_chat(
            port, chat_body(max_completion_tokens=64_000)
        )
        self.assertEqual(response.status, 400)
        error = as_dict(
            cast("dict[str, object]", json.loads(response.read()))["error"]
        )
        self.assertEqual(error["code"], "output_limit_unenforceable")
        self.assertEqual(
            self.adapter_for("worker_bridged").dispatch_count, 0
        )

    def test_unpinned_non_binding_limit_still_normalizes(self) -> None:
        """The same unpinned path normalizes a provably non-binding limit
        (at the exact variant's hard maximum) and executes — the honest
        headline boundary: at/above the maximum the request executes."""
        port = self._world()
        response = self.post_chat(
            port, chat_body(max_completion_tokens=128_000)
        )
        self.assertEqual(response.status, 200)
        bridged = self.adapter_for("worker_bridged")
        self.assertEqual(bridged.dispatch_count, 1)
        self.assertIsNone(bridged.dispatches[0].max_output_tokens)
        record = audit_records(self.application)[-1]
        self.assertEqual(
            record.reason_codes, ("completed", "output_limit_normalized")
        )

    def test_omitting_the_limit_executes(self) -> None:
        """The advertised remediation works: a request without an explicit
        output limit routes and executes on the no-control channel."""
        port = self._world()
        response = self.post_chat(port, chat_body())
        self.assertEqual(response.status, 200)
        record = audit_records(self.application)[-1]
        self.assertEqual(record.reason_codes, ("completed",))


class ExactVariantTests(LimitsHarness):
    """Finding 3 regression: hard properties come from the EXACT calibrated
    variant, never a minimum across sibling variants."""

    def _catalog(self) -> ModelCatalog:
        max_entry = _entry(
            "openai", "gpt-5.6-luna", "max", reasoning=4, coding=4, scope="codex"
        )
        medium_entry = ModelCatalogEntry(
            identity=ModelIdentity(provider="openai", model="gpt-5.6-luna", variant="medium"),
            display_name="gpt-5.6-luna medium",
            hard_properties=ModelHardProperties(
                input_context_tokens=131_072,
                output_tokens=64_000,
                supports_tool_use=True,
                supports_vision=False,
                supports_reasoning_mode=True,
            ),
            capabilities=max_entry.capabilities,
            capacity_bindings=max_entry.capacity_bindings,
            reasoning_effort="medium",
        )
        return ModelCatalog(
            catalog_version=1,
            updated_on="2026-09-01",
            entries=(max_entry, medium_entry),
        )

    def _variant_world(self, *, codex: bool) -> int:
        pairs: list[tuple[ResourceIdentity, ExecutionCapabilities]] = []
        if codex:
            pairs.append(
                (
                    ResourceIdentity(
                        resource_id="z-codex-max",
                        channel="worker_bridged",
                        provider="openai",
                        model="gpt-5.6-luna",
                        entitlement="subscription_included",
                        variant="max",
                    ),
                    ExecutionCapabilities(
                        context_limit_tokens=272_000,
                        output_limit_control=False,
                    ),
                )
            )
            pairs.append(
                (
                    ResourceIdentity(
                        resource_id="a-codex-medium",
                        channel="worker_bridged",
                        provider="openai",
                        model="gpt-5.6-luna",
                        entitlement="subscription_included",
                        variant="medium",
                    ),
                    ExecutionCapabilities(
                        context_limit_tokens=272_000,
                        output_limit_control=False,
                    ),
                )
            )
        else:
            pairs.append(
                (
                    ResourceIdentity(
                        resource_id="z-max-route",
                        channel="worker_bridged",
                        provider="openai",
                        model="gpt-5.6-luna",
                        entitlement="subscription_included",
                        variant="max",
                    ),
                    ExecutionCapabilities(context_limit_tokens=272_000),
                )
            )
            pairs.append(
                (
                    ResourceIdentity(
                        resource_id="a-medium-route",
                        channel="worker_bridged",
                        provider="openai",
                        model="gpt-5.6-luna",
                        entitlement="subscription_included",
                        variant="medium",
                    ),
                    ExecutionCapabilities(context_limit_tokens=272_000),
                )
            )
        return self.make_world(registry=bare_registry(*pairs), catalog=self._catalog())

    def test_each_variant_uses_its_own_hard_properties(self) -> None:
        """max route uses max's properties; medium route uses medium's;
        neither inherits min() from the sibling variant."""
        port = self._variant_world(codex=False)
        max_ok = self.post_chat(
            port,
            chat_body(
                model="sr-pin:z-max-route/openai/gpt-5.6-luna/max",
                max_completion_tokens=100_000,
            ),
        )
        self.assertEqual(max_ok.status, 200)  # max hard output 128000 >= 100000
        medium_rejected = self.post_chat(
            port,
            chat_body(
                model="sr-pin:a-medium-route/openai/gpt-5.6-luna/medium",
                max_completion_tokens=100_000,
            ),
        )
        self.assertEqual(medium_rejected.status, 400)  # medium hard 64000
        error = as_dict(
            cast("dict[str, object]", json.loads(medium_rejected.read()))["error"]
        )
        self.assertEqual(error["code"], "output_limit_insufficient")

    def test_discovery_metadata_is_exact_variant_per_route(self) -> None:
        port = self._variant_world(codex=False)
        connection = self.client(port)
        connection.request("GET", "/v1/models", headers=dict(AUTH))
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        entries = [as_dict(e) for e in as_list(payload["data"])]
        luna = next(e for e in entries if e["id"] == "gpt-5.6-luna")
        routes = {
            as_dict(r)["resource_id"]: as_dict(r)
            for r in as_list(as_dict(luna["x_scarcity_router"])["routes"])
        }
        self.assertEqual(
            routes["z-max-route"]["effective_output_limit_tokens"], 128_000
        )
        self.assertEqual(
            routes["a-medium-route"]["effective_output_limit_tokens"], 64_000
        )
        self.assertEqual(
            routes["z-max-route"]["effective_context_limit_tokens"], 272_000
        )
        self.assertEqual(
            routes["a-medium-route"]["effective_context_limit_tokens"], 131_072
        )

    def test_normalization_threshold_is_the_exact_variants_maximum(self) -> None:
        """The discriminating case: on a control=False channel serving the
        max variant (hard output 128000, sibling medium 64000), a request
        for 100000 BINDS against max's own proven maximum and is rejected —
        the old minimum-across-variants reading (64000) would have
        normalized it away as 'non-binding'."""
        port = self._variant_world(codex=True)
        binding = self.post_chat(
            port,
            chat_body(
                model="sr-pin:z-codex-max/openai/gpt-5.6-luna/max",
                max_completion_tokens=100_000,
            ),
        )
        self.assertEqual(binding.status, 400)
        error = as_dict(cast("dict[str, object]", json.loads(binding.read()))["error"])
        self.assertEqual(error["code"], "output_limit_unenforceable")
        self.assertEqual(self.adapter_for("worker_bridged").dispatch_count, 0)
        # Each variant normalizes against its OWN proven maximum.
        for resource, effort, limit in (
            ("z-codex-max", "max", 128_000),
            ("a-codex-medium", "medium", 64_000),
        ):
            response = self.post_chat(
                port,
                chat_body(
                    model=f"sr-pin:{resource}/openai/gpt-5.6-luna/{effort}",
                    reasoning_effort=effort,
                    max_completion_tokens=limit,
                ),
            )
            self.assertEqual(response.status, 200, resource)
            record = audit_records(self.application)[-1]
            self.assertIn("output_limit_normalized", record.reason_codes)


class DefaultCapabilityTests(LimitsHarness):
    """Blocker 2 regression: administrator policy is separated from channel
    capability. The 272000 Codex channel fact must not be the global
    administrator input ceiling, and a >272k request executes on a route
    whose evidence supports it while staying ineligible on the 272k
    channel."""

    LONG_MODEL_CATALOG_CELLS: tuple[CompatibilityCell, ...] | None = None

    def _catalog_with_long_model(self) -> ModelCatalog:
        long_entry = ModelCatalogEntry(
            identity=ModelIdentity(provider="openai", model="gpt-6-long", variant="max"),
            display_name="gpt-6-long max",
            hard_properties=ModelHardProperties(
                input_context_tokens=1_050_000,
                output_tokens=128_000,
                supports_tool_use=True,
                supports_vision=False,
                supports_reasoning_mode=True,
            ),
            capabilities=_entry(
                "openai", "gpt-5.6-luna", "max", reasoning=4, coding=4, scope="codex"
            ).capabilities,
            capacity_bindings=(
                CapacityScopeRef(provider="openai", scope_id="codex"),
            ),
            reasoning_effort="max",
        )
        base = build_catalog()
        return ModelCatalog(
            catalog_version=base.catalog_version,
            updated_on=base.updated_on,
            entries=base.entries + (long_entry,),
        )

    def _cells_for(self, model: str) -> tuple[CompatibilityCell, ...]:
        return build_cells(include_worker=True) + tuple(
            CompatibilityCell(
                channel=channel,
                provider="openai",
                model=model,
                feature=feature,
                value="PASS",
                adapter="synthetic-http",
                adapter_version="1.2.3",
                evidence=EVIDENCE,
            )
            for channel in ("server_direct_http", "worker_bridged")
            for feature in ALL_FEATURES
        )

    def _world(self, *, limits: GatewayLimits | None = None) -> int:
        long_route = ResourceIdentity(
            resource_id="z-long-route",
            channel="server_direct_http",
            provider="openai",
            model="gpt-6-long",
            entitlement="subscription_included",
        )
        codex_route = ResourceIdentity(
            resource_id="a-codex-route",
            channel="worker_bridged",
            provider="openai",
            model="gpt-6-long",
            entitlement="subscription_included",
            variant="max",
        )
        return self.make_world(
            registry=bare_registry(
                (long_route, ExecutionCapabilities(context_limit_tokens=1_050_000)),
                (codex_route, ExecutionCapabilities(context_limit_tokens=272_000)),
            ),
            catalog=self._catalog_with_long_model(),
            cells=self._cells_for("gpt-6-long"),
            limits=limits,
        )

    _LONG_BODY: str = "x" * 1_200_000  # ~300k estimated tokens

    def _long_request(self, **overrides: object) -> dict[str, object]:
        body = chat_body(
            messages=[{"role": "user", "content": self._LONG_BODY}]
        )
        body["model"] = "gpt-6-long"
        body.update(overrides)
        return body

    def test_request_above_272k_executes_on_the_supporting_route(self) -> None:
        """Under DEFAULT administrator limits a >272k input request
        executes on the route whose model+channel evidence supports it —
        the 272k Codex fact is that channel's capability, not gateway
        policy."""
        port = self._world()
        response = self.post_chat(port, self._long_request())
        self.assertEqual(response.status, 200)
        record = audit_records(self.application)[-1]
        assert record.selected_target is not None
        self.assertEqual(record.selected_target.resource_id, "z-long-route")

    def test_same_request_is_ineligible_on_the_272k_channel(self) -> None:
        port = self._world()
        pinned = "sr-pin:a-codex-route/openai/gpt-6-long/max"
        response = self.post_chat(port, self._long_request(model=pinned))
        self.assertEqual(response.status, 400)
        error = as_dict(cast("dict[str, object]", json.loads(response.read()))["error"])
        self.assertEqual(error["code"], "context_limit_insufficient")
        self.assertEqual(self.adapter_for("worker_bridged").dispatch_count, 0)

    def test_explicit_admin_272k_narrows_all_routes(self) -> None:
        """An administrator who explicitly configures
        ``max_input_context_tokens = 272000`` intentionally narrows every
        route: the global pre-check rejects before routing."""
        port = self._world(limits=GatewayLimits(max_input_context_tokens=272_000))
        response = self.post_chat(port, self._long_request())
        self.assertEqual(response.status, 400)
        error = as_dict(cast("dict[str, object]", json.loads(response.read()))["error"])
        self.assertEqual(error["code"], "context_length_exceeded")
        self.assertEqual(
            self.adapter_for("server_direct_http").dispatch_count, 0
        )


class NormalizationProvenanceTests(LimitsHarness):
    """Finding 4 regression: the output_limit_normalized provenance note is
    present on EVERY terminal audit record after normalization, never on
    pre-normalization rejections, and never replaces the primary reason."""

    _PIN: str = "sr-pin:codex-luna/openai/gpt-5.6-luna/max"

    def _world(
        self, behavior: Callable[[AdapterCall, ExecutionContext], AdapterResult] | None
    ) -> int:
        codex = ResourceIdentity(
            resource_id="codex-luna",
            channel="worker_bridged",
            provider="openai",
            model="gpt-5.6-luna",
            entitlement="subscription_included",
            variant="max",
        )
        return self.make_world(
            registry=bare_registry(
                (
                    codex,
                    ExecutionCapabilities(
                        context_limit_tokens=272_000,
                        output_limit_control=False,
                    ),
                )
            ),
            behavior=behavior,
            channels=("worker_bridged",),
        )

    def _normalized_request(self) -> dict[str, object]:
        return chat_body(
            model=self._PIN,
            max_completion_tokens=128_000,  # == the exact variant's hard max
        )

    def test_completed_record_carries_the_note(self) -> None:
        port = self._world(None)
        response = self.post_chat(port, self._normalized_request())
        self.assertEqual(response.status, 200)
        record = audit_records(self.application)[-1]
        self.assertEqual(record.result_status, "completed")
        self.assertIn("output_limit_normalized", record.reason_codes)

    def test_backend_failure_record_carries_the_note(self) -> None:
        port = self._world(permanent_failure_behavior())
        response = self.post_chat(port, self._normalized_request())
        self.assertEqual(response.status, 502)
        record = audit_records(self.application)[-1]
        self.assertEqual(record.result_status, "failed")
        self.assertEqual(
            record.reason_codes, ("backend_failure", "output_limit_normalized")
        )

    def test_timeout_record_carries_the_note(self) -> None:
        port = self._world(timeout_behavior())
        response = self.post_chat(port, self._normalized_request())
        self.assertEqual(response.status, 408)
        record = audit_records(self.application)[-1]
        self.assertEqual(record.result_status, "timed_out")
        self.assertEqual(
            record.reason_codes,
            ("execution_time_limit_exceeded", "output_limit_normalized"),
        )

    def test_ambiguous_record_carries_the_note(self) -> None:
        port = self._world(ambiguous_failure_behavior())
        response = self.post_chat(port, self._normalized_request())
        self.assertEqual(response.status, 500)
        record = audit_records(self.application)[-1]
        self.assertEqual(record.result_status, "failed_ambiguous")
        self.assertEqual(
            record.reason_codes,
            ("ambiguous_execution_state", "output_limit_normalized"),
        )

    def test_cancelled_record_carries_the_note(self) -> None:
        def cancel_and_report(
            call: AdapterCall, context: ExecutionContext
        ) -> AdapterResult:
            _ = call
            context.cancel_event.set()
            return AdapterResult(
                status="cancelled",
                calls=(
                    CallObservation(
                        call_index=0,
                        started_at="2026-09-15T12:05:00.000Z",
                        ended_at="2026-09-15T12:05:01.000Z",
                        status="cancelled",
                    ),
                ),
            )

        port = self._world(cancel_and_report)
        # A client disconnect closes the connection without a status line;
        # the assertion target is the audit provenance.
        try:
            _ = self.post_chat(port, self._normalized_request())
        except OSError:
            pass
        record = audit_records(self.application)[-1]
        self.assertEqual(record.result_status, "cancelled")
        self.assertEqual(
            record.reason_codes, ("client_disconnected", "output_limit_normalized")
        )

    def test_binding_rejection_before_normalization_has_no_note(self) -> None:
        port = self._world(permanent_failure_behavior())
        response = self.post_chat(
            port, chat_body(model=self._PIN, max_completion_tokens=64_000)
        )
        self.assertEqual(response.status, 400)
        record = audit_records(self.application)[-1]
        self.assertEqual(record.result_status, "rejected")
        self.assertNotIn("output_limit_normalized", record.reason_codes)


class ConfigurationExportHonestyTests(unittest.TestCase):
    """Non-default limits are exported; defaults are not (#136)."""

    def test_default_limits_are_not_exported(self) -> None:
        from scarcity_router.server_config import ServerConfiguration

        document = ServerConfiguration().to_document()
        assert isinstance(document, dict)
        self.assertNotIn("limits", document)
        # Round-trip: absence reads back as the (new honest) defaults.
        restored = ServerConfiguration.from_document(
            {"schema_version": document["schema_version"]}
        )
        self.assertEqual(restored.limits, GatewayLimits())

    def test_explicit_values_stay_exported(self) -> None:
        """An operator's explicit value — including a pre-#136 literal
        such as 16384, now non-default — is represented honestly and
        survives a round-trip."""
        from scarcity_router.server_config import ServerConfiguration

        configuration = ServerConfiguration(
            limits=GatewayLimits(max_output_tokens=16_384)
        )
        document = configuration.to_document()
        assert isinstance(document, dict)
        exported = as_dict(document["limits"])
        self.assertEqual(exported["max_output_tokens"], 16_384)
        self.assertEqual(exported["max_input_context_tokens"], 2_097_152)
        restored = ServerConfiguration.from_document(document)
        self.assertEqual(restored.limits, GatewayLimits(max_output_tokens=16_384))

    def test_invalid_limit_values_fail_validation(self) -> None:
        with self.assertRaises(ValueError):
            _ = GatewayLimits(max_output_tokens=0)
        with self.assertRaises(ValueError):
            _ = GatewayLimits.from_dict({"max_output_tokens": -5})


if __name__ == "__main__":  # pragma: no cover
    import unittest

    _ = unittest.main()
