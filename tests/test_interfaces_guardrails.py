"""M08 consolidated guardrail suite for the machine interfaces (issue #93).

This module is the continuous per-module regression gate of the
execution-gateway program (``make guardrails``): every module issue must
keep it green. It locks the frozen machine-interface v1 surfaces
(``docs/machine-interfaces.md``, D-028/D-030/D-031), extends the
``direct == CLI == REST == MCP`` parity rule to the gateway-era additive
fields (D-039 execution eligibility), and pins the explicit-failure
behavior of the optional remote bridge (D-045 topology 4).

Self-contained by design: repository artifacts, synthetic collectors, a
fixed clock, loopback transports only. Deterministic and independent of
live provider quota; no model call is ever issued.
"""

from __future__ import annotations

import ast
import http.client
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast, override
from unittest import mock

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, TextContent

from scarcity_router import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
    SimulationOverrides,
    TaskRequirement,
)
from scarcity_router.cli import main as cli_main
from scarcity_router.eligibility import (
    ELIGIBILITY_SCHEMA_VERSION,
    ExecutionEligibility,
)
from scarcity_router.errors import ApplicationInputError, RemoteBridgeError
from scarcity_router.machine_api import (
    ENVELOPE_SCHEMA_VERSION,
    internal_error_payload,
    invalid_request_payload,
    parse_selection_document,
    parse_simulation_document,
    selection_envelope,
    simulation_envelope,
    status_envelope,
)
from scarcity_router.mcp import MCP_TOOL_NAMES, build_server
from scarcity_router.remote import (
    REMOTE_SELECT_ENDPOINT,
    REMOTE_SIMULATE_ENDPOINT,
    REMOTE_STATUS_ENDPOINT,
    RemoteConfigError,
    RemoteScarcityClient,
    RemoteServerConfig,
)
from scarcity_router.selection_app import (
    ApplicationDependencies,
    load_catalog,
    load_model_policy,
    select_from_inputs,
    simulate_from_inputs,
)
from scarcity_router.server import (
    BIND_HOST,
    RestHTTPServer,
    build_parser as build_server_parser,
    make_server,
)
from scarcity_router.status import StatusCollectors, collect_status
from scarcity_router.providers.openai_codex_acquisition import (
    OpenAICodexObservation,
)
from tests.observation import paired_observation

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"
POLICY_PATH = REPO / "model-policy.json"
FIXED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
RETRIEVED_AT = "2026-09-06T12:00:00.000Z"

#: A conspicuous synthetic client credential for the remote-bridge tests.
_FAKE_CLIENT_KEY = "synthetic-client-key-NOT-A-SECRET-0001"

# D-036 isolation: every adapter run in this module must never read or
# provision the host's ~/.config/scarcity-router (same discipline as
# tests/test_mcp.py and tests/test_server.py).
_TMP_CONFIG_HOME = Path(tempfile.mkdtemp(prefix="scarcity-router-guardrails-"))
os.environ["XDG_CONFIG_HOME"] = str(_TMP_CONFIG_HOME)
_ = (_TMP_CONFIG_HOME / "scarcity-router").mkdir(mode=0o700)
_ = (_TMP_CONFIG_HOME / "scarcity-router" / "selector-policy.json").write_text(
    json.dumps(
        {
            "mode": "balanced",
            "resource_policy": {
                "policy_version": 1,
                "unknown_capacity_mode": "degraded",
                "replenishment_mode": "advisory",
                "reservations": [],
                "blackouts": [],
            },
        }
    ),
    encoding="utf-8",
)

_REQUIREMENT_L3 = {
    "task_level": "L3",
    "capability_minima": {"reasoning": 4, "coding": 5, "tool_use": 4},
    "hard_constraints": {
        "requires_tool_use": True,
        "requires_reasoning_mode": True,
    },
}
_TIGHTENING = {
    "task_level": "L3",
    "capability_minima": {},
    "hard_constraints": {"requires_vision": True},
}
_ZAI_WEEKLY_2 = {
    "capacity_percentages": [
        {
            "provider": "zai",
            "scope_id": "coding_plan",
            "resource": "tokens",
            "kind": "weekly",
            "remaining_percent": 2,
        }
    ]
}


def _clock() -> datetime:
    return FIXED_AT


def _snap(
    provider: str,
    five: int | None,
    weekly: int | None,
    *,
    status: str = "ok",
) -> CapacitySnapshot:
    scope = "codex" if provider == "openai" else "coding_plan"
    windows: list[CapacityWindow] = []
    for kind, remaining, duration in (
        ("five_hour", five, 18_000),
        ("weekly", weekly, 604_800),
    ):
        if remaining is not None:
            windows.append(
                CapacityWindow(
                    resource="tokens",
                    kind=kind,
                    scope_id=scope,
                    duration_seconds=duration,
                    used_percent=100 - remaining,
                    remaining_percent=remaining,
                    window_id=f"{provider}-{kind}",
                )
            )
    diagnostics = (
        ()
        if status == "ok"
        else (CapacityDiagnostic(code="telemetry_unknown"),)
    )
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=RETRIEVED_AT,
        status=status,
        windows=tuple(windows),
        diagnostics=diagnostics,
    )


def _observation(
    snapshot: CapacitySnapshot,
    *,
    state: str | None = None,
    reason_codes: tuple[str, ...] = (),
) -> OpenAICodexObservation:
    """Pair a snapshot with its D-039 report (synthetic production shape)."""
    if state is None:
        return paired_observation(snapshot)
    report = ExecutionEligibility(
        schema_version=ELIGIBILITY_SCHEMA_VERSION,
        provider=snapshot.provider,
        source=snapshot.source,
        retrieved_at=snapshot.retrieved_at,
        state=state,
        reason_codes=reason_codes,
    )
    return OpenAICodexObservation(snapshot=snapshot, eligibility=report)


def _collectors(
    *,
    openai_five: int = 40,
    openai_weekly: int = 40,
    zai_five: int = 80,
    zai_weekly: int = 80,
    openai_status: str = "ok",
    openai_state: str | None = None,
    openai_reason_codes: tuple[str, ...] = (),
    calls: list[tuple[str, str]] | None = None,
) -> StatusCollectors:
    def openai(*, retrieved_at: str) -> OpenAICodexObservation:
        if calls is not None:
            calls.append(("openai", retrieved_at))
        degraded = openai_status != "ok"
        return _observation(
            _snap(
                "openai",
                None if degraded else openai_five,
                None if degraded else openai_weekly,
                status=openai_status,
            ),
            state=openai_state,
            reason_codes=openai_reason_codes,
        )

    def zai(*, retrieved_at: str) -> CapacitySnapshot:
        if calls is not None:
            calls.append(("zai", retrieved_at))
        return _snap("zai", zai_five, zai_weekly)

    return StatusCollectors(openai=openai, zai=zai)


def _application(
    collectors: StatusCollectors | None = None,
) -> ApplicationDependencies:
    return ApplicationDependencies(
        catalog_path=CATALOG_PATH,
        model_policy_path=POLICY_PATH,
        collectors=collectors if collectors is not None else _collectors(),
        clock=_clock,
    )


class _RestHarness:
    """One loopback REST server on a kernel-assigned port for one test."""

    server: RestHTTPServer
    thread: threading.Thread

    def __init__(self, application: ApplicationDependencies) -> None:
        self.server = make_server(application, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    @property
    def port(self) -> int:
        return cast("tuple[str, int]", self.server.server_address)[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _rest_exchange(
    harness: _RestHarness,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=10)
    try:
        connection.request(
            method, path, body=body, headers=dict(headers) if headers else {}
        )
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    finally:
        connection.close()
    parsed: object = None
    if raw:
        parsed = cast(object, json.loads(raw))
    return status, parsed


def _rest_json(
    harness: _RestHarness,
    method: str,
    path: str,
    payload: object | None = None,
) -> tuple[int, object]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    return _rest_exchange(harness, method, path, body=body, headers=headers)


async def _mcp_call_async(
    application: ApplicationDependencies,
    name: str,
    arguments: dict[str, object] | None,
) -> CallToolResult:
    server = build_server(application)
    result: CallToolResult | None = None
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(
                server.run,
                server_streams[0],
                server_streams[1],
                server.create_initialization_options(),
            )
            try:
                with anyio.fail_after(10):
                    async with ClientSession(
                        *client_streams
                    ) as client:
                        _ = await client.initialize()
                        result = await client.call_tool(name, arguments)
            finally:
                tasks.cancel_scope.cancel()
    if result is None:
        raise AssertionError("MCP call did not return")
    return result


def _mcp_payload(result: CallToolResult) -> dict[str, object]:
    return cast("dict[str, object]", result.structured_content)


def _mcp_text_payload(result: CallToolResult) -> dict[str, object]:
    blocks = [
        block.text for block in result.content if isinstance(block, TextContent)
    ]
    if len(blocks) != 1:
        raise AssertionError("expected exactly one MCP text content block")
    return cast("dict[str, object]", json.loads(blocks[0]))


class GuardrailTestCase(unittest.TestCase):
    """Shared harness helpers for the guardrail suite."""

    def _rest(self, application: ApplicationDependencies) -> _RestHarness:
        harness = _RestHarness(application)
        self.addCleanup(harness.close)
        return harness

    def _mcp(
        self,
        application: ApplicationDependencies,
        name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> CallToolResult:
        return anyio.run(
            _mcp_call_async,
            application,
            name,
            None if arguments is None else dict(arguments),
        )

    def _cli(
        self,
        arguments: list[str],
        *,
        collectors: StatusCollectors | None = None,
    ) -> tuple[int, str]:
        output = io.StringIO()
        code = cli_main(
            arguments,
            stdout=output,
            collectors=collectors,
            clock=_clock,
        )
        return code, output.getvalue()


class FrozenSurfaceTests(GuardrailTestCase):
    """The v1 surfaces are exactly the frozen ones — no more, no fewer."""

    def test_rest_route_table_is_exactly_the_frozen_v1_operations(self) -> None:
        """Each frozen operation answers; anything else is 404/405."""
        application = _application()
        harness = self._rest(application)
        code, payload = _rest_json(harness, "GET", "/healthz")
        self.assertEqual((200, {"status": "ok"}), (code, payload))
        code, envelope = _rest_json(harness, "GET", "/v1/status")
        self.assertEqual(200, code)
        self.assertIn("snapshots", cast("dict[str, object]", envelope))
        code, envelope = _rest_json(
            harness, "POST", "/v1/select", {"profile_id": "deep_coding"}
        )
        self.assertEqual(200, code)
        self.assertIn("decision", cast("dict[str, object]", envelope))
        code, envelope = _rest_json(
            harness,
            "POST",
            "/v1/simulate",
            {"profile_id": "deep_coding", "overrides": {}},
        )
        self.assertEqual(200, code)
        self.assertIn("result", cast("dict[str, object]", envelope))

    def test_execution_surface_paths_are_never_served_by_the_loopback_adapter(
        self,
    ) -> None:
        """D-045: the loopback REST v1 adapter is never the execution server."""
        harness = self._rest(_application())
        for path in ("/v1/models", "/v1/chat/completions"):
            post_status, _ = _rest_exchange(
                harness,
                "POST",
                path,
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(404, post_status, path)
            get_status, _ = _rest_exchange(harness, "GET", path)
            self.assertEqual(404, get_status, path)

    def test_unknown_path_and_wrong_method_are_transport_responses(self) -> None:
        harness = self._rest(_application())
        status, _ = _rest_json(harness, "GET", "/v1/unknown")
        self.assertEqual(404, status)
        status, _ = _rest_exchange(harness, "PUT", "/v1/select", body=b"{}")
        self.assertEqual(405, status)
        status, _ = _rest_exchange(harness, "POST", "/v1/status", body=b"{}")
        self.assertEqual(405, status)

    def test_mcp_exposes_exactly_the_three_recommendation_tools(self) -> None:
        self.assertEqual(
            ("scarcity_status", "scarcity_select", "scarcity_simulate"),
            MCP_TOOL_NAMES,
        )

    def test_rest_binding_is_loopback_only_and_not_configurable(self) -> None:
        self.assertEqual("127.0.0.1", BIND_HOST)
        parser = build_server_parser()
        with self.assertRaises(SystemExit):
            _ = parser.parse_args(["--host", "0.0.0.0"])
        with self.assertRaises(SystemExit):
            _ = parser.parse_args(["--bind", "0.0.0.0"])

    def test_healthz_is_exact_liveness_only(self) -> None:
        harness = self._rest(_application())
        status, payload = _rest_json(harness, "GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, payload)


class EnvelopeAndErrorVocabularyTests(GuardrailTestCase):
    """Envelope versioning and the closed error vocabulary are frozen."""

    def test_envelope_version_is_separate_from_nested_domain_versions(self) -> None:
        harness = self._rest(_application())
        status, envelope = _rest_json(harness, "GET", "/v1/status")
        self.assertEqual(200, status)
        document = cast("dict[str, object]", envelope)
        self.assertEqual(ENVELOPE_SCHEMA_VERSION, document["schema_version"])
        self.assertEqual(1, document["schema_version"])
        snapshots = cast("list[object]", document["snapshots"])
        for snapshot in cast("list[dict[str, object]]", snapshots):
            # Capacity contract v3 travels INSIDE the machine-interface v1
            # envelope; the two versions are never collapsed (D-028).
            self.assertEqual(3, snapshot["schema_version"])
        eligibility = cast("list[dict[str, object]]", document["eligibility"])
        for report in eligibility:
            self.assertEqual(1, report["schema_version"])

    def test_rest_error_vocabulary_is_closed_and_safe(self) -> None:
        harness = self._rest(_application())
        invalid_bodies = (
            b'{"profile_id":"a","profile_id":"b"}',
            b'{"profile_id":NaN}',
            b'{"profile_id":Infinity}',
            b'{"profile_id":"deep_coding","unexpected":1}',
            b'{"profile_id":"deep_coding","requirement":{"task_level":"L3"}}',
            b'{"requirement":null}',
            b"not json at all",
        )
        for body in invalid_bodies:
            status, payload = _rest_exchange(
                harness,
                "POST",
                "/v1/select",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(400, status, body)
            error = cast("dict[str, object]", payload)["error"]
            error_map = cast("dict[str, object]", error)
            self.assertEqual({"code", "message"}, set(error_map), body)
            self.assertEqual("invalid_request", error_map["code"], body)

    def test_internal_failure_is_the_fixed_internal_error_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "broken.json"
            _ = catalog_path.write_text("{", encoding="utf-8")
            harness = self._rest(
                ApplicationDependencies(
                    catalog_path=catalog_path,
                    model_policy_path=POLICY_PATH,
                    collectors=_collectors(),
                    clock=_clock,
                )
            )
            status, payload = _rest_json(
                harness, "POST", "/v1/select", {"profile_id": "deep_coding"}
            )
        self.assertEqual(500, status)
        self.assertEqual(internal_error_payload(), payload)
        serialized = json.dumps(payload)
        self.assertNotIn("broken", serialized)
        self.assertNotIn("Traceback", serialized)

    def test_machine_api_envelopes_are_the_frozen_shapes(self) -> None:
        """The shared envelope builders are the only serialization source."""
        harness = self._rest(_application())
        status, payload = _rest_json(harness, "GET", "/v1/status")
        self.assertEqual(200, status)
        application = _application()
        observation = collect_status(
            collectors=application.collectors, clock=application.clock
        )
        self.assertEqual(
            status_envelope(observation.snapshots, observation.eligibility),
            payload,
        )
        self.assertEqual(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "invalid request",
                }
            },
            invalid_request_payload(),
        )
        self.assertEqual(
            {
                "error": {
                    "code": "internal_error",
                    "message": "internal server error",
                }
            },
            internal_error_payload(),
        )


class FrozenSemanticsTests(GuardrailTestCase):
    """Spot checks of the frozen semantics across CLI, REST and MCP."""

    def test_no_solution_is_a_valid_result_everywhere(self) -> None:
        collectors = _collectors(openai_five=0, openai_weekly=0)
        application = _application(collectors)
        harness = self._rest(application)
        status, payload = _rest_json(
            harness, "POST", "/v1/select", {"profile_id": "scientific_review"}
        )
        self.assertEqual(200, status)
        decision = cast(
            "dict[str, object]", cast("dict[str, object]", payload)["decision"]
        )
        self.assertIsNone(decision["selected"])
        self.assertIn(
            "no_eligible_candidate",
            cast("list[object]", decision["reason_codes"]),
        )
        result = self._mcp(
            application, "scarcity_select", {"profile_id": "scientific_review"}
        )
        self.assertFalse(result.is_error)
        self.assertEqual(
            decision,
            cast("dict[str, object]", _mcp_payload(result)["decision"]),
        )
        code, _ = self._cli(
            ["select", "--profile", "scientific_review", "--json"],
            collectors=collectors,
        )
        self.assertEqual(0, code)

    def test_unknown_profile_is_invalid_request_everywhere(self) -> None:
        application = _application()
        harness = self._rest(application)
        status, payload = _rest_json(
            harness, "POST", "/v1/select", {"profile_id": "not-a-profile"}
        )
        self.assertEqual(400, status)
        self.assertEqual(invalid_request_payload(), payload)
        result = self._mcp(
            application, "scarcity_select", {"profile_id": "not-a-profile"}
        )
        self.assertTrue(result.is_error)
        self.assertEqual(invalid_request_payload(), _mcp_payload(result))
        code, _ = self._cli(["select", "--profile", "not-a-profile", "--json"])
        self.assertEqual(1, code)

    def test_provider_degradation_stays_domain_data_everywhere(self) -> None:
        collectors = _collectors(openai_status="unknown")
        application = _application(collectors)
        harness = self._rest(application)
        status, payload = _rest_json(harness, "GET", "/v1/status")
        self.assertEqual(200, status)
        snapshots = cast(
            "list[dict[str, object]]",
            cast("dict[str, object]", payload)["snapshots"],
        )
        self.assertEqual(
            ["openai", "zai"], [item["provider"] for item in snapshots]
        )
        self.assertEqual("unknown", snapshots[0]["status"])
        result = self._mcp(application, "scarcity_status", {})
        self.assertFalse(result.is_error)
        self.assertEqual(payload, _mcp_payload(result))
        code, _ = self._cli(["status", "--json"], collectors=collectors)
        self.assertEqual(0, code)

    def test_select_field_semantics_are_frozen(self) -> None:
        application = _application()
        harness = self._rest(application)
        # replenishment_states: explicit null is invalid at the select
        # boundary; missing and [] mean none.
        status, payload = _rest_json(
            harness,
            "POST",
            "/v1/select",
            {"profile_id": "deep_coding", "replenishment_states": None},
        )
        self.assertEqual(400, status)
        self.assertEqual(invalid_request_payload(), payload)
        replenishment_cases: tuple[object, ...] = (None, [])
        for replenishment in replenishment_cases:
            request: dict[str, object] = {"profile_id": "deep_coding"}
            if replenishment is not None:
                request["replenishment_states"] = replenishment
            status, _ = _rest_json(harness, "POST", "/v1/select", request)
            self.assertEqual(200, status)        # selector_policy: null behaves exactly like missing (server default).
        status_missing, payload_missing = _rest_json(
            harness, "POST", "/v1/select", {"profile_id": "deep_coding"}
        )
        status_null, payload_null = _rest_json(
            harness,
            "POST",
            "/v1/select",
            {"profile_id": "deep_coding", "selector_policy": None},
        )
        self.assertEqual(200, status_missing)
        self.assertEqual(200, status_null)
        self.assertEqual(payload_missing, payload_null)
        # The requirement-source XOR.
        status, _ = _rest_json(
            harness,
            "POST",
            "/v1/select",
            {"profile_id": "routine_coding", "requirement": _REQUIREMENT_L3},
        )
        self.assertEqual(400, status)
        status, _ = _rest_json(harness, "POST", "/v1/select", {})
        self.assertEqual(400, status)
        status, _ = _rest_json(
            harness,
            "POST",
            "/v1/select",
            {"requirement": _REQUIREMENT_L3, "tightening": _TIGHTENING},
        )
        self.assertEqual(400, status)
        # MCP mirrors the same logical semantics.
        result = self._mcp(
            application,
            "scarcity_select",
            {"profile_id": "deep_coding", "replenishment_states": None},
        )
        self.assertTrue(result.is_error)
        self.assertEqual(invalid_request_payload(), _mcp_payload(result))

    def test_simulation_override_tri_state_is_preserved(self) -> None:
        application = _application()
        harness = self._rest(application)
        # overrides is required; the nested replenishment tri-state belongs
        # to SimulationOverrides and is never conflated with select.
        status, _ = _rest_json(
            harness, "POST", "/v1/simulate", {"profile_id": "deep_coding"}
        )
        self.assertEqual(400, status)
        status, _ = _rest_json(
            harness,
            "POST",
            "/v1/simulate",
            {"profile_id": "deep_coding", "overrides": None},
        )
        self.assertEqual(400, status)
        status, _ = _rest_json(
            harness,
            "POST",
            "/v1/simulate",
            {"profile_id": "deep_coding", "overrides": {}},
        )
        self.assertEqual(200, status)


class FreshnessSemanticsTests(GuardrailTestCase):
    """Status freshness semantics are frozen: fresh per request, no cache."""

    def test_one_request_collects_once_with_one_shared_timestamp(self) -> None:
        calls: list[tuple[str, str]] = []
        application = _application(_collectors(calls=calls))
        harness = self._rest(application)
        status, _ = _rest_json(harness, "GET", "/v1/status")
        self.assertEqual(200, status)
        self.assertEqual(["openai", "zai"], [provider for provider, _ in calls])
        self.assertEqual(1, len({stamp for _, stamp in calls}))

    def test_status_is_fresh_on_every_request_without_cache(self) -> None:
        calls: list[tuple[str, str]] = []
        application = _application(_collectors(calls=calls))
        harness = self._rest(application)
        for _ in range(3):
            status, _ = _rest_json(harness, "GET", "/v1/status")
            self.assertEqual(200, status)
        self.assertEqual(6, len(calls))
        _ = self._mcp(application, "scarcity_status", {})
        self.assertEqual(8, len(calls))


class GatewayEraParityTests(GuardrailTestCase):
    """direct == CLI == REST == MCP, extended to gateway-era fields."""

    def _assert_select_parity(
        self,
        collectors: StatusCollectors,
        *,
        profile_id: str | None,
        requirement: TaskRequirement | None = None,
        tightening: TaskRequirement | None = None,
        cli_arguments: list[str],
        rest_request: dict[str, object],
        mcp_arguments: dict[str, object],
    ) -> None:
        application = _application(collectors)
        harness = self._rest(application)
        direct = select_from_inputs(
            catalog=load_catalog(CATALOG_PATH),
            profiles=load_model_policy(POLICY_PATH)[0],
            profile_policy_version=load_model_policy(POLICY_PATH)[1],
            profile_id=profile_id,
            requirement=requirement,
            tightening=tightening,
            policy=None,
            replenishment_states=(),
            collectors=collectors,
            clock=_clock,
        )
        code, output = self._cli(cli_arguments, collectors=collectors)
        self.assertEqual(0, code)
        rest_status, rest_envelope = _rest_json(
            harness, "POST", "/v1/select", rest_request
        )
        self.assertEqual(200, rest_status)
        mcp_result = self._mcp(application, "scarcity_select", mcp_arguments)
        self.assertFalse(mcp_result.is_error)
        expected_envelope = {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "decision": direct.to_dict(),
        }
        self.assertEqual(direct.to_dict(), json.loads(output))
        self.assertEqual(expected_envelope, rest_envelope)
        self.assertEqual(expected_envelope, _mcp_payload(mcp_result))
        self.assertEqual(expected_envelope, _mcp_text_payload(mcp_result))

    def test_select_profile_parity(self) -> None:
        self._assert_select_parity(
            _collectors(),
            profile_id="deep_coding",
            cli_arguments=["select", "--profile", "deep_coding", "--json"],
            rest_request={"profile_id": "deep_coding"},
            mcp_arguments={"profile_id": "deep_coding"},
        )

    def test_select_explicit_requirement_parity(self) -> None:
        self._assert_select_parity(
            _collectors(),
            profile_id=None,
            requirement=TaskRequirement.from_dict(_REQUIREMENT_L3),
            cli_arguments=[
                "select",
                "--requirement",
                str(self._write_fixture("requirement.json", _REQUIREMENT_L3)),
                "--json",
            ],
            rest_request={"requirement": _REQUIREMENT_L3},
            mcp_arguments={"requirement": _REQUIREMENT_L3},
        )

    def test_select_profile_with_tightening_parity(self) -> None:
        self._assert_select_parity(
            _collectors(),
            profile_id="deep_coding",
            tightening=TaskRequirement.from_dict(_TIGHTENING),
            cli_arguments=[
                "select",
                "--profile",
                "deep_coding",
                "--tighten",
                str(self._write_fixture("tightening.json", _TIGHTENING)),
                "--json",
            ],
            rest_request={"profile_id": "deep_coding", "tightening": _TIGHTENING},
            mcp_arguments={"profile_id": "deep_coding", "tightening": _TIGHTENING},
        )

    def _write_fixture(self, name: str, document: object) -> Path:
        directory = tempfile.mkdtemp(prefix="scarcity-guardrail-fixture-")
        self.addCleanup(_rmtree_quiet, directory)
        path = Path(directory) / name
        _ = path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_simulation_parity(self) -> None:
        collectors = _collectors()
        application = _application(collectors)
        harness = self._rest(application)
        request = {"profile_id": "deep_coding", "overrides": _ZAI_WEEKLY_2}
        direct = simulate_from_inputs(
            catalog=load_catalog(CATALOG_PATH),
            profiles=load_model_policy(POLICY_PATH)[0],
            profile_policy_version=load_model_policy(POLICY_PATH)[1],
            profile_id="deep_coding",
            requirement=None,
            tightening=None,
            policy=None,
            replenishment_states=(),
            overrides=SimulationOverrides.from_dict(_ZAI_WEEKLY_2),
            collectors=collectors,
            clock=_clock,
        )
        code, output = self._cli(
            [
                "simulate",
                "--profile",
                "deep_coding",
                "--overrides",
                str(self._write_fixture("overrides.json", _ZAI_WEEKLY_2)),
                "--json",
            ],
            collectors=collectors,
        )
        self.assertEqual(0, code)
        rest_status, rest_envelope = _rest_json(
            harness, "POST", "/v1/simulate", request
        )
        self.assertEqual(200, rest_status)
        mcp_result = self._mcp(application, "scarcity_simulate", request)
        self.assertFalse(mcp_result.is_error)
        expected = {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "result": direct.to_dict(),
        }
        self.assertEqual(direct.to_dict(), json.loads(output))
        self.assertEqual(expected, rest_envelope)
        self.assertEqual(expected, _mcp_payload(mcp_result))

    def test_status_parity_including_gateway_era_eligibility_field(self) -> None:
        """D-039 eligibility rides the v1 envelope additively — everywhere."""
        collectors = _collectors()
        application = _application(collectors)
        harness = self._rest(application)
        observation = collect_status(collectors=collectors, clock=_clock)
        direct_snapshots = [
            snapshot.to_dict() for snapshot in observation.snapshots
        ]
        direct_eligibility = [
            report.to_dict() for report in observation.eligibility
        ]
        code, output = self._cli(["status", "--json"], collectors=collectors)
        self.assertEqual(0, code)
        rest_status, rest_envelope = _rest_json(harness, "GET", "/v1/status")
        self.assertEqual(200, rest_status)
        mcp_result = self._mcp(application, "scarcity_status", {})
        self.assertFalse(mcp_result.is_error)
        rest_document = cast("dict[str, object]", rest_envelope)
        # The CLI keeps its RELEASED snapshot-array shape: the additive
        # eligibility field is an envelope-level v1 addition, never a CLI
        # shape change.
        self.assertEqual(direct_snapshots, json.loads(output))
        # REST and MCP share the same additive envelope field.
        self.assertEqual(1, rest_document["schema_version"])
        self.assertEqual(direct_snapshots, rest_document["snapshots"])
        self.assertEqual(direct_eligibility, rest_document["eligibility"])
        self.assertEqual(rest_document, _mcp_payload(mcp_result))
        self.assertEqual(rest_document, _mcp_text_payload(mcp_result))

    def test_execution_eligibility_exclusion_parity(self) -> None:
        """A blocked provider is excluded BEFORE routing, on every surface."""
        collectors = _collectors(
            openai_state="policy_blocked",
            openai_reason_codes=("purchased_credits_present",),
        )
        application = _application(collectors)
        harness = self._rest(application)
        direct = select_from_inputs(
            catalog=load_catalog(CATALOG_PATH),
            profiles=load_model_policy(POLICY_PATH)[0],
            profile_policy_version=load_model_policy(POLICY_PATH)[1],
            profile_id="routine_coding",
            requirement=None,
            tightening=None,
            policy=None,
            replenishment_states=(),
            collectors=collectors,
            clock=_clock,
        )
        excluded = [
            candidate.to_dict()
            for candidate in direct.excluded
            if candidate.exclusion_stage == "execution"
        ]
        self.assertTrue(excluded, "expected execution-stage exclusions")
        for candidate in excluded:
            report = cast(
                "dict[str, object]", candidate["execution_eligibility"]
            )
            self.assertEqual("policy_blocked", report["state"])
            self.assertEqual(
                ["purchased_credits_present"], report["reason_codes"]
            )
            self.assertIn(
                "execution_policy_blocked",
                cast("list[object]", candidate["reason_codes"]),
            )
        code, output = self._cli(
            ["select", "--profile", "routine_coding", "--json"],
            collectors=collectors,
        )
        self.assertEqual(0, code)
        rest_status, rest_envelope = _rest_json(
            harness, "POST", "/v1/select", {"profile_id": "routine_coding"}
        )
        self.assertEqual(200, rest_status)
        mcp_result = self._mcp(
            application, "scarcity_select", {"profile_id": "routine_coding"}
        )
        self.assertFalse(mcp_result.is_error)
        expected = {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "decision": direct.to_dict(),
        }
        self.assertEqual(direct.to_dict(), json.loads(output))
        self.assertEqual(expected, rest_envelope)
        self.assertEqual(expected, _mcp_payload(mcp_result))


def _rmtree_quiet(directory: str) -> None:
    _ = shutil.rmtree(directory, ignore_errors=True)


# ── Remote bridge (D-045 topology 4) ────────────────────────────────────────


class _ControlStubHandler(BaseHTTPRequestHandler):
    """Synthetic authenticated control server implementing the M08 seam.

    This is the reference stub for the interface-side expectation documented
    in ``scarcity_router/remote.py``: machine-interface v1 envelopes over
    ``GET /v1/status`` / ``POST /v1/select`` / ``POST /v1/simulate`` with
    ``Authorization: Bearer <client key>``. It computes responses through
    the SAME application seam as the local adapters, so a conforming server
    is parity-equivalent by construction.
    """

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        self._handle()

    @override
    def log_message(self, format: str, *args: object) -> None:
        _ = format, args

    def _handle(self) -> None:
        server = cast(_ControlStubHTTPServer, self.server)
        length_header = self.headers.get("Content-Length")
        length = int(length_header) if length_header else 0
        body = self.rfile.read(length) if length else b""
        server.requests.append(
            (self.command, self.path, self.headers.get("Authorization"))
        )
        if server.behavior == "unauthorized":
            self._json(401, invalid_request_payload())
            return
        if server.behavior == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/eavesdrop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if server.behavior == "corrupt":
            self._raw(200, b"not json at all")
            return
        if server.behavior == "duplicate_keys":
            self._raw(
                200,
                b'{"schema_version":1,"schema_version":2,"decision":{}}',
            )
            return
        if (self.command, self.path) == ("GET", REMOTE_STATUS_ENDPOINT):
            self._status(server)
            return
        if (self.command, self.path) == ("POST", REMOTE_SELECT_ENDPOINT):
            self._select(server, body)
            return
        if (self.command, self.path) == ("POST", REMOTE_SIMULATE_ENDPOINT):
            self._simulate(server, body)
            return
        self._json(404, invalid_request_payload())

    def _status(self, server: _ControlStubHTTPServer) -> None:
        if server.behavior == "wrong_envelope":
            self._json(200, {"schema_version": 2, "snapshots": []})
            return
        if server.behavior == "missing_document":
            self._json(200, {"schema_version": 1})
            return
        if server.behavior == "internal_error":
            self._json(500, internal_error_payload())
            return
        application = server.application
        observation = collect_status(
            collectors=application.collectors, clock=application.clock
        )
        self._json(
            200,
            status_envelope(observation.snapshots, observation.eligibility),
        )

    def _select(self, server: _ControlStubHTTPServer, body: bytes) -> None:
        application = server.application
        try:
            document = cast(object, json.loads(body.decode("utf-8")))
            parsed = parse_selection_document(document)
        except (UnicodeDecodeError, ValueError, ApplicationInputError):
            self._json(400, invalid_request_payload())
            return
        catalog = load_catalog(application.catalog_path)
        profiles, version = load_model_policy(application.model_policy_path)
        try:
            decision = select_from_inputs(
                catalog=catalog,
                profiles=profiles,
                profile_policy_version=version,
                profile_id=parsed.profile_id,
                requirement=parsed.requirement,
                tightening=parsed.tightening,
                policy=(
                    parsed.policy
                    if parsed.policy is not None
                    else application.default_policy
                ),
                replenishment_states=parsed.replenishment_states,
                collectors=application.collectors,
                clock=application.clock,
            )
        except ApplicationInputError:
            # The server boundary classifies application-input violations
            # (e.g. an unknown profile id) exactly like the loopback server.
            self._json(400, invalid_request_payload())
            return
        self._json(200, selection_envelope(decision))

    def _simulate(self, server: _ControlStubHTTPServer, body: bytes) -> None:
        application = server.application
        try:
            document = cast(object, json.loads(body.decode("utf-8")))
            parsed, overrides = parse_simulation_document(document)
        except (UnicodeDecodeError, ValueError, ApplicationInputError):
            self._json(400, invalid_request_payload())
            return
        catalog = load_catalog(application.catalog_path)
        profiles, version = load_model_policy(application.model_policy_path)
        try:
            result = simulate_from_inputs(
                catalog=catalog,
                profiles=profiles,
                profile_policy_version=version,
                profile_id=parsed.profile_id,
                requirement=parsed.requirement,
                tightening=parsed.tightening,
                policy=(
                    parsed.policy
                    if parsed.policy is not None
                    else application.default_policy
                ),
                replenishment_states=parsed.replenishment_states,
                overrides=overrides,
                collectors=application.collectors,
                clock=application.clock,
            )
        except ApplicationInputError:
            self._json(400, invalid_request_payload())
            return
        self._json(200, simulation_envelope(result))

    def _json(self, status: int, payload: Mapping[str, object]) -> None:
        self._raw(status, json.dumps(payload).encode("utf-8"))

    def _raw(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)


class _ControlStubHTTPServer(HTTPServer):
    """One synthetic control server instance with switchable behavior."""

    application: ApplicationDependencies
    behavior: str
    requests: list[tuple[str, str, str | None]]

    def __init__(self, application: ApplicationDependencies) -> None:
        super().__init__(("127.0.0.1", 0), _ControlStubHandler)
        self.application = application
        self.behavior = "ok"
        self.requests = []

    @property
    def port(self) -> int:
        return cast("tuple[str, int]", self.server_address)[1]


class RemoteBridgeTests(GuardrailTestCase):
    """Explicit configuration, explicit failure — never a local fallback."""

    def _serve(
        self, collectors: StatusCollectors | None = None
    ) -> _ControlStubHTTPServer:
        server = _ControlStubHTTPServer(_application(collectors))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Cleanup order matters (LIFO): shutdown must ask the serve loop to
        # exit BEFORE the socket is closed and the thread is joined, else
        # server_close orphans a spinning loop and join(5) always burns its
        # full timeout.
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server

    def _client(self, server: _ControlStubHTTPServer) -> RemoteScarcityClient:
        config = RemoteServerConfig(
            base_url=f"http://127.0.0.1:{server.port}",
            api_key=_FAKE_CLIENT_KEY,
        )
        return RemoteScarcityClient(config)

    def test_configuration_requires_https_off_loopback(self) -> None:
        with self.assertRaises(RemoteConfigError):
            _ = RemoteServerConfig(
                base_url="http://server.example.internal:8443",
                api_key=_FAKE_CLIENT_KEY,
            )
        # The explicit bounded loopback exception and verified HTTPS work.
        _ = RemoteServerConfig(
            base_url="http://127.0.0.1:8443", api_key=_FAKE_CLIENT_KEY
        )
        _ = RemoteServerConfig(
            base_url="http://localhost:8443", api_key=_FAKE_CLIENT_KEY
        )
        _ = RemoteServerConfig(
            base_url="https://server.example.internal:8443",
            api_key=_FAKE_CLIENT_KEY,
        )

    def test_configuration_rejects_urls_that_carry_state_or_secrets(self) -> None:
        for base_url in (
            "https://h.example/query?token=x",
            "https://h.example/prefix",
            "https://user:secret@h.example",
            "ftp://h.example",
            "not a url",
            "",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(RemoteConfigError):
                    _ = RemoteServerConfig(
                        base_url=base_url, api_key=_FAKE_CLIENT_KEY
                    )

    def test_configuration_rejects_unusable_credentials_and_timeouts(self) -> None:
        for api_key in ("", "spaced key", "line\nbreak", "  padded  "):
            with self.subTest(api_key=api_key):
                with self.assertRaises(RemoteConfigError):
                    _ = RemoteServerConfig(
                        base_url="https://h.example", api_key=api_key
                    )
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(RemoteConfigError):
                    _ = RemoteServerConfig(
                        base_url="https://h.example",
                        api_key=_FAKE_CLIENT_KEY,
                        timeout_seconds=timeout,
                    )

    def test_credential_is_never_exposed_in_repr(self) -> None:
        config = RemoteServerConfig(
            base_url="https://h.example", api_key=_FAKE_CLIENT_KEY
        )
        self.assertNotIn(_FAKE_CLIENT_KEY, repr(config))
        self.assertNotIn(_FAKE_CLIENT_KEY, str(config))

    def test_remote_round_trip_equals_local_envelopes(self) -> None:
        """Equivalent state/policy ⇒ equivalent remote and local results."""
        collectors = _collectors()
        server = self._serve(collectors)
        client = self._client(server)
        harness = self._rest(_application(collectors))
        remote_status = client.status()
        local_code, local_status = _rest_json(harness, "GET", "/v1/status")
        self.assertEqual(200, local_code)
        self.assertEqual(local_status, remote_status)
        remote_decision = client.select({"profile_id": "deep_coding"})
        local_code, local_decision = _rest_json(
            harness, "POST", "/v1/select", {"profile_id": "deep_coding"}
        )
        self.assertEqual(200, local_code)
        self.assertEqual(local_decision, remote_decision)
        remote_result = client.simulate(
            {"profile_id": "deep_coding", "overrides": _ZAI_WEEKLY_2}
        )
        local_code, local_result = _rest_json(
            harness,
            "POST",
            "/v1/simulate",
            {"profile_id": "deep_coding", "overrides": _ZAI_WEEKLY_2},
        )
        self.assertEqual(200, local_code)
        self.assertEqual(local_result, remote_result)

    def test_credential_travels_in_header_only_never_in_the_url(self) -> None:
        server = self._serve()
        client = self._client(server)
        _ = client.status()
        _ = client.select({"profile_id": "deep_coding"})
        for _method, path, authorization in server.requests:
            self.assertEqual(f"Bearer {_FAKE_CLIENT_KEY}", authorization, path)
            self.assertNotIn(_FAKE_CLIENT_KEY, path)
            self.assertNotIn("?", path)
        self.assertEqual(
            {
                ("GET", REMOTE_STATUS_ENDPOINT),
                ("POST", REMOTE_SELECT_ENDPOINT),
            },
            {(_method, path) for _method, path, _ in server.requests},
        )

    def test_authentication_failure_is_explicit_and_never_falls_back(self) -> None:
        calls: list[tuple[str, str]] = []
        collectors = _collectors(calls=calls)
        server = self._serve(collectors)
        server.behavior = "unauthorized"
        client = RemoteScarcityClient(
            RemoteServerConfig(
                base_url=f"http://127.0.0.1:{server.port}",
                api_key=_FAKE_CLIENT_KEY,
            )
        )
        with self.assertRaises(RemoteBridgeError) as caught:
            _ = client.status()
        self.assertIn("authentication failed", str(caught.exception))
        self.assertNotIn(_FAKE_CLIENT_KEY, str(caught.exception))
        # Explicit failure: zero local collection happened.
        self.assertEqual([], calls)

    def test_unreachable_server_is_an_explicit_error(self) -> None:
        probe = socket.socket()
        _ = probe.bind(("127.0.0.1", 0))
        port = cast("tuple[str, int]", probe.getsockname())[1]
        probe.close()
        client = RemoteScarcityClient(
            RemoteServerConfig(
                base_url=f"http://127.0.0.1:{port}",
                api_key=_FAKE_CLIENT_KEY,
                timeout_seconds=2.0,
            )
        )
        with self.assertRaises(RemoteBridgeError):
            _ = client.select({"profile_id": "deep_coding"})

    def test_server_error_statuses_surface_explicitly(self) -> None:
        server = self._serve()
        server.behavior = "internal_error"
        client = self._client(server)
        with self.assertRaises(RemoteBridgeError) as caught:
            _ = client.status()
        self.assertIn("internal_error", str(caught.exception))
        server.behavior = "redirect"
        with self.assertRaises(RemoteBridgeError) as caught:
            _ = client.status()
        self.assertIn("redirect", str(caught.exception))
        # The redirect was never followed: exactly one new request was made.
        self.assertEqual(2, len(server.requests))

    def test_contract_violating_responses_fail_explicitly(self) -> None:
        for behavior in (
            "corrupt",
            "duplicate_keys",
            "wrong_envelope",
            "missing_document",
        ):
            with self.subTest(behavior=behavior):
                server = self._serve()
                server.behavior = behavior
                client = self._client(server)
                with self.assertRaises(RemoteBridgeError):
                    _ = client.status()

    def test_invalid_request_documents_fail_before_any_network_exchange(
        self,
    ) -> None:
        server = self._serve()
        client = self._client(server)
        with self.assertRaises(ApplicationInputError):
            _ = client.select({"profile_id": "deep_coding", "unexpected": 1})
        with self.assertRaises(ApplicationInputError):
            _ = client.simulate({"profile_id": "deep_coding"})
        # Structural violations never reached the network; the shared
        # logical parser decided client-side.
        self.assertEqual([], server.requests)

    def test_remotely_rejected_profile_surfaces_as_an_explicit_error(
        self,
    ) -> None:
        """The remote catalog is authoritative: an unknown profile id is
        validated by the SERVER and surfaces as an explicit bridge error —
        the client never resolves profiles locally and never falls back."""
        server = self._serve()
        client = self._client(server)
        with self.assertRaises(RemoteBridgeError) as caught:
            _ = client.select({"profile_id": "no-such-profile"})
        self.assertIn("invalid request", str(caught.exception))
        self.assertEqual(
            [("POST", REMOTE_SELECT_ENDPOINT)],
            [(method, path) for method, path, _ in server.requests],
        )

    def test_https_verification_has_no_bypass_option(self) -> None:
        source = (REPO / "scarcity_router" / "remote.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ssl.create_default_context()", source)
        for forbidden in (
            "check_hostname",
            "verify_mode",
            "CERT_NONE",
            "_create_unverified",
        ):
            self.assertNotIn(forbidden, source)


class NoExecutionAndNoIdeMutationTests(GuardrailTestCase):
    """MCP stays recommendation-only; no surface writes IDE configuration."""

    def test_mcp_adapter_is_local_stdio_only_with_no_execution(self) -> None:
        source = (REPO / "scarcity_router" / "mcp.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "import http",
            "import urllib",
            "import socket",
            "import subprocess",
            "import requests",
            "from urllib",
            "from http",
            "from socket",
            "from subprocess",
            "chat/completions",
            "scarcity_router.server",
        ):
            self.assertNotIn(forbidden, source)

    def test_frozen_v1_sources_contain_no_execution_surface(self) -> None:
        for name in ("machine_api.py", "server.py"):
            source = (REPO / "scarcity_router" / name).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("chat/completions", source)
            self.assertNotIn("/v1/models", source)

    def test_package_filesystem_writes_are_confined_to_known_provisioning(
        self,
    ) -> None:
        """Filesystem writes exist only in known bounded provisioning paths.

        AST scan of every package module: filesystem-write operations
        (``write_text``/``write_bytes``/``os.fdopen``/write-mode ``open``/
        write-flag ``os.open``) may exist only in:

        - ``config.py`` — the D-036 selector-policy provisioning (the one
          product-owned write outside the package);
        - ``worker_codex_adapter.py`` — the M06 adapter-owned controlled
          ``CODEX_HOME`` provisioning under the worker's own state directory
          (issue #91 Stage 2): one generated minimal ``config.toml`` inside a
          ``0o700`` adapter-owned tree, never the user's ``~/.codex``. (The
          M05 worker store's sqlite file is created through the sqlite
          library and is confined to the same state directory.)

        This is the structural guarantee behind "Scarcity Router does not
        automatically modify a user's global IDE configuration".
        """
        package_dir = REPO / "scarcity_router"
        provisioning_modules = {"config.py", "worker_codex_adapter.py"}
        offenders: list[str] = []
        write_call_names = {"write_text", "write_bytes", "fdopen"}
        write_flag_markers = ("WRONLY", "RDWR", "CREAT", "TRUNC", "APPEND")
        for path in sorted(package_dir.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else (func.id if isinstance(func, ast.Name) else "")
                )
                if (
                    name in write_call_names
                    and path.name not in provisioning_modules
                ):
                    offenders.append(f"{path.name}: {name} call")
                if name == "open" and path.name not in provisioning_modules:
                    mode = _mode_argument(node)
                    if mode is not None and any(marker in mode for marker in "wax+"):
                        offenders.append(f"{path.name}: write-mode open")
                    if isinstance(func, ast.Attribute):
                        for argument in node.args:
                            for flag_node in ast.walk(argument):
                                if isinstance(flag_node, ast.Attribute) and any(
                                    marker in flag_node.attr
                                    for marker in write_flag_markers
                                ):
                                    offenders.append(
                                        f"{path.name}: write-flag os.open"
                                    )
        self.assertEqual([], offenders)

    def test_cli_surfaces_create_only_the_default_user_config(self) -> None:
        """Behavior check: one full CLI cycle writes exactly one known file."""
        collectors = _collectors()
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as xdg, tempfile.TemporaryDirectory() as work:
            requirement_path = Path(work) / "requirement.json"
            _ = requirement_path.write_text(
                json.dumps(_REQUIREMENT_L3), encoding="utf-8"
            )
            overrides_path = Path(work) / "overrides.json"
            _ = overrides_path.write_text(
                json.dumps(_ZAI_WEEKLY_2), encoding="utf-8"
            )
            isolated = dict(os.environ)
            isolated["HOME"] = home
            isolated["XDG_CONFIG_HOME"] = xdg
            with mock.patch.dict(os.environ, isolated, clear=True):
                for arguments in (
                    ["status", "--json"],
                    [
                        "select",
                        "--requirement",
                        str(requirement_path),
                        "--json",
                    ],
                    [
                        "simulate",
                        "--profile",
                        "deep_coding",
                        "--overrides",
                        str(overrides_path),
                        "--json",
                    ],
                    ["install-config"],
                ):
                    code, _ = self._cli(arguments, collectors=collectors)
                    self.assertEqual(0, code, arguments)
            xdg_files = sorted(
                str(path.relative_to(xdg))
                for path in Path(xdg).rglob("*")
                if path.is_file()
            )
            home_files = sorted(
                str(path.relative_to(home))
                for path in Path(home).rglob("*")
                if path.is_file()
            )
        self.assertEqual(["scarcity-router/selector-policy.json"], xdg_files)
        self.assertEqual([], home_files)


def _mode_argument(node: ast.Call) -> str | None:
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        if isinstance(value, str):
            return value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, str):
                return value
    return None


if __name__ == "__main__":
    _ = unittest.main()
