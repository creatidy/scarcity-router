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
from tests.gateway_fixtures import (
    CLIENT_KEY,
    EVIDENCE,
    GatewayApplication,
    ScriptedAdapter,
    audit_records,
    build_catalog,
    build_cells,
    build_registry,
    make_application,
)
from tests.gateway_fixtures import (  # noqa: F401 -- fixture privates, single-world helpers
    _capabilities as _capabilities,  # pyright: ignore[reportPrivateUsage]
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
    ) -> int:
        self.adapters = {channel: ScriptedAdapter(channel=channel) for channel in channels}
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
        huge = "x" * 1_100_000  # ~275k estimated tokens > 272000
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
        self.assertEqual(error["code"], "output_limit_exceeded")
        self.assertEqual(error["param"], "max_completion_tokens")

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
        self.assertEqual(error["code"], "output_limit_unenforceable")
        self.assertEqual(error["param"], "max_completion_tokens")
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
        self.assertEqual(error["code"], "output_limit_unenforceable")
        self.assertEqual(self.adapter_for("worker_bridged").dispatch_count, 0)


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
        self.assertEqual(exported["max_input_context_tokens"], 272_000)
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
