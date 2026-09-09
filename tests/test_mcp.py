"""M3c official-SDK MCP adapter, parity and transport-boundary tests.

All application tests use the repository artifacts with synthetic collectors
and a fixed clock. The only subprocess test starts this repository's MCP
module and performs discovery; it does not invoke a provider tool.
"""

from __future__ import annotations

import http.client
import io
import json
import math
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import anyio
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, JSONRPCRequest, TextContent, Tool

from scarcity_router import CapacityDiagnostic, CapacitySnapshot, CapacityWindow
from scarcity_router.cli import main as cli_main
from scarcity_router.machine_api import (
    invalid_request_payload,
    internal_error_payload,
)
from scarcity_router.mcp import MCP_TOOL_NAMES, build_server
from scarcity_router.selection_app import (
    ApplicationDependencies,
    load_configured_artifacts,
    select_from_inputs,
    simulate_from_inputs,
)
from scarcity_router.server import RestHTTPServer, make_server
from scarcity_router.status import StatusCollectors, collect_status
from scarcity_router.simulation import SimulationOverrides

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"
POLICY_PATH = REPO / "model-policy.json"
FIXED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
BASE_OVERRIDES: dict[str, object] = {
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


def _synthetic_snap(
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
        retrieved_at="2026-09-06T12:00:00.000Z",
        status=status,
        windows=tuple(windows),
        diagnostics=diagnostics,
    )


def _unknown_snap(provider: str) -> CapacitySnapshot:
    return _synthetic_snap(provider, None, None, status="unknown")


def _collectors(
    *,
    openai_five: int = 40,
    openai_weekly: int = 40,
    zai_five: int = 80,
    zai_weekly: int = 80,
) -> StatusCollectors:
    def openai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        return _synthetic_snap("openai", openai_five, openai_weekly)

    def zai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        return _synthetic_snap("zai", zai_five, zai_weekly)

    return StatusCollectors(openai=openai, zai=zai)


def _error_payload(result: CallToolResult) -> dict[str, object]:
    return cast(dict[str, object], result.structured_content)


def _text_payload(result: CallToolResult) -> dict[str, object]:
    text_blocks = [
        block.text for block in result.content if isinstance(block, TextContent)
    ]
    if len(text_blocks) != 1:
        raise AssertionError("expected exactly one MCP text content block")
    return cast(dict[str, object], json.loads(text_blocks[0]))


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
                        *client_streams, read_timeout_seconds=5
                    ) as client:
                        _ = await client.initialize()
                        result = await client.call_tool(name, arguments)
            finally:
                tasks.cancel_scope.cancel()
    if result is None:
        raise AssertionError("MCP call did not return")
    return result


async def _mcp_discover_async(
    application: ApplicationDependencies | None = None,
) -> tuple[list[str], dict[str, object]]:
    server = build_server(application)
    result: tuple[list[str], dict[str, object]] | None = None
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
                        *client_streams, read_timeout_seconds=5
                    ) as client:
                        initialization = await client.initialize()
                        tools = await client.list_tools()
                        names = [tool.name for tool in tools.tools]
                        capabilities = cast(
                            dict[str, object],
                            initialization.capabilities.model_dump(exclude_none=True),
                        )
                        result = names, capabilities
            finally:
                tasks.cancel_scope.cancel()
    if result is None:
        raise AssertionError("MCP discovery did not return")
    return result


async def _stdio_subprocess_discover_async() -> list[str]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "scarcity_router.mcp"],
        cwd=REPO,
    )
    names: list[str] = []
    with anyio.fail_after(15):
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams, read_timeout_seconds=5) as client:
                _ = await client.initialize()
                tools = await client.list_tools()
                names = [tool.name for tool in tools.tools]
    return names


async def _mcp_tools_async(application: ApplicationDependencies) -> list[Tool]:
    server = build_server(application)
    result: list[Tool] | None = None
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
                async with ClientSession(
                    *client_streams, read_timeout_seconds=5
                ) as client:
                    _ = await client.initialize()
                    tools = await client.list_tools()
                    result = list(tools.tools)
            finally:
                tasks.cancel_scope.cancel()
    if result is None:
        raise AssertionError("MCP tool discovery did not return")
    return result


class _RestHarness:
    thread: threading.Thread

    def __init__(self, application: ApplicationDependencies) -> None:
        self.server: RestHTTPServer = make_server(application, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return cast("tuple[str, int]", self.server.server_address)[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _rest_request(
    harness: _RestHarness,
    method: str,
    path: str,
    payload: object | None = None,
) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=10)
    try:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class McpTestCase(unittest.TestCase):
    def _application(
        self,
        *,
        collectors: StatusCollectors | None = None,
        catalog_path: Path = CATALOG_PATH,
        model_policy_path: Path = POLICY_PATH,
    ) -> ApplicationDependencies:
        return ApplicationDependencies(
            catalog_path=catalog_path,
            model_policy_path=model_policy_path,
            collectors=collectors if collectors is not None else _collectors(),
            clock=_clock,
        )

    def _call(
        self,
        application: ApplicationDependencies,
        name: str,
        arguments: Mapping[str, object] | None,
    ) -> CallToolResult:
        return anyio.run(
            _mcp_call_async,
            application,
            name,
            None if arguments is None else dict(arguments),
        )

    def _discover(
        self,
        application: ApplicationDependencies | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        return anyio.run(_mcp_discover_async, application)

    def _cli_json(
        self, application: ApplicationDependencies, arguments: list[str]
    ) -> object:
        output = io.StringIO()
        exit_code = cli_main(
            arguments,
            stdout=output,
            collectors=application.collectors,
            clock=application.clock,
        )
        self.assertEqual(0, exit_code)
        return cast(object, json.loads(output.getvalue()))

    def _rest(self, application: ApplicationDependencies) -> _RestHarness:
        harness = _RestHarness(application)
        self.addCleanup(harness.close)
        return harness


class DiscoveryTests(McpTestCase):
    def test_exact_tools_and_no_extra_capabilities(self) -> None:
        names, capabilities = self._discover(self._application())
        self.assertEqual(list(MCP_TOOL_NAMES), names)
        self.assertIsNone(capabilities.get("resources"))
        self.assertIsNone(capabilities.get("prompts"))
        self.assertEqual({"list_changed": False}, capabilities["tools"])

    def test_tool_descriptions_and_advertised_fields(self) -> None:
        tools = anyio.run(_mcp_tools_async, self._application())
        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(set(MCP_TOOL_NAMES), set(by_name))
        for tool in by_name.values():
            description = cast(str, tool.description)
            self.assertIn("Scarcity Router machine-interface contract v1", description)
            self.assertIn("never executes model inference", description)
        status_schema = cast(
            dict[str, object], by_name["scarcity_status"].input_schema
        )
        self.assertFalse(status_schema["additionalProperties"])
        self.assertEqual(
            {}, cast(dict[str, object], status_schema["properties"])
        )
        select_schema = cast(
            dict[str, object], by_name["scarcity_select"].input_schema
        )
        self.assertFalse(select_schema["additionalProperties"])
        select_properties = cast(dict[str, object], select_schema["properties"])
        self.assertEqual(
            {
                "profile_id",
                "requirement",
                "tightening",
                "selector_policy",
                "replenishment_states",
            },
            set(select_properties),
        )
        replenishment_schema = cast(
            dict[str, object], select_properties["replenishment_states"]
        )
        self.assertEqual("array", replenishment_schema["type"])
        simulate_schema = cast(
            dict[str, object], by_name["scarcity_simulate"].input_schema
        )
        self.assertFalse(simulate_schema["additionalProperties"])
        simulate_properties = cast(
            dict[str, object], simulate_schema["properties"]
        )
        self.assertEqual(
            set(select_properties) | {"overrides"},
            set(simulate_properties),
        )
        overrides_schema = cast(dict[str, object], simulate_properties["overrides"])
        self.assertEqual("object", overrides_schema["type"])
        self.assertEqual(["overrides"], simulate_schema["required"])

    def test_mcp_module_does_not_import_rest_runtime(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scarcity_router" / "mcp.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("scarcity_router.server", text)
        self.assertNotIn("RestApplication", text)


class StatusTests(McpTestCase):
    def test_status_matches_rest_and_cli_and_direct(self) -> None:
        application = self._application()
        direct = [
            snapshot.to_dict()
            for snapshot in collect_status(
                collectors=application.collectors, clock=application.clock
            )
        ]
        cli = cast(list[object], self._cli_json(application, ["status", "--json"]))
        harness = self._rest(application)
        status, rest = _rest_request(harness, "GET", "/v1/status")
        self.assertEqual(200, status)
        rest_envelope = cast(dict[str, object], rest)
        mcp = self._call(application, "scarcity_status", None)
        self.assertFalse(mcp.is_error)
        mcp_envelope = _error_payload(mcp)
        self.assertEqual(direct, cli)
        self.assertEqual(direct, rest_envelope["snapshots"])
        self.assertEqual(rest_envelope, mcp_envelope)
        self.assertEqual(mcp_envelope, _text_payload(mcp))
        self.assertEqual(["openai", "zai"], [item["provider"] for item in direct])

    def test_status_collects_once_with_one_timestamp_and_degraded_is_success(self) -> None:
        calls: list[tuple[str, str]] = []

        def openai(*, retrieved_at: str) -> CapacitySnapshot:
            calls.append(("openai", retrieved_at))
            return _unknown_snap("openai")

        def zai(*, retrieved_at: str) -> CapacitySnapshot:
            calls.append(("zai", retrieved_at))
            return _synthetic_snap("zai", 80, 80)

        application = self._application(
            collectors=StatusCollectors(openai=openai, zai=zai)
        )
        result = self._call(application, "scarcity_status", {})
        self.assertFalse(result.is_error)
        self.assertEqual(["openai", "zai"], [provider for provider, _ in calls])
        self.assertEqual(1, len({retrieved_at for _, retrieved_at in calls}))
        snapshots = cast(dict[str, object], result.structured_content)["snapshots"]
        first_snapshot = cast(dict[str, object], cast(list[object], snapshots)[0])
        self.assertEqual("unknown", first_snapshot["status"])


class SelectTests(McpTestCase):
    def test_five_effort_profiles_match_direct_cli_rest_and_mcp_v1(self) -> None:
        application = self._application(collectors=_collectors(zai_weekly=0))
        catalog, profiles, version = load_configured_artifacts(CATALOG_PATH, POLICY_PATH)
        harness = self._rest(application)
        expected = {
            "routine_coding": ("gpt-5.6-luna", "medium"),
            "deep_coding": ("gpt-5.6-terra", "medium"),
            "scientific_review": ("gpt-5.6-sol", "high"),
            "orchestration": ("gpt-5.6-luna", "max"),
            "translation": ("gpt-5.6-sol", "high"),
        }
        for profile_id, (model, variant) in expected.items():
            with self.subTest(profile_id=profile_id):
                direct = select_from_inputs(
                    catalog=catalog, profiles=profiles, profile_policy_version=version,
                    profile_id=profile_id, requirement=None, tightening=None, policy=None,
                    replenishment_states=(), collectors=application.collectors,
                    clock=application.clock,
                )
                assert direct.selected is not None
                self.assertEqual(direct.selected.identity.to_dict(), {
                    "provider": "openai", "model": model, "variant": variant,
                })
                cli = self._cli_json(application, ["select", "--profile", profile_id, "--json"])
                status, rest = _rest_request(harness, "POST", "/v1/select", {"profile_id": profile_id})
                self.assertEqual(200, status)
                result = self._call(application, "scarcity_select", {"profile_id": profile_id})
                self.assertFalse(result.is_error)
                envelope = {"schema_version": 1, "decision": direct.to_dict()}
                self.assertEqual(direct.to_dict(), cli)
                self.assertEqual(envelope, rest)
                self.assertEqual(envelope, cast(dict[str, object], result.structured_content))
                self.assertEqual(envelope, _text_payload(result))

    def test_profile_and_explicit_requirement_paths(self) -> None:
        application = self._application()
        profile_result = self._call(
            application, "scarcity_select", {"profile_id": "deep_coding"}
        )
        self.assertFalse(profile_result.is_error)
        catalog, profiles, version = load_configured_artifacts(
            CATALOG_PATH, POLICY_PATH
        )
        profile_direct = select_from_inputs(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=version,
            profile_id="deep_coding",
            requirement=None,
            tightening=None,
            policy=None,
            replenishment_states=(),
            collectors=application.collectors,
            clock=application.clock,
        )
        cli = self._cli_json(
            application, ["select", "--profile", "deep_coding", "--json"]
        )
        harness = self._rest(application)
        status, rest = _rest_request(
            harness, "POST", "/v1/select", {"profile_id": "deep_coding"}
        )
        self.assertEqual(200, status)
        rest_decision = cast(dict[str, object], rest)["decision"]
        self.assertEqual(profile_direct.to_dict(), cli)
        self.assertEqual(profile_direct.to_dict(), rest_decision)
        self.assertEqual(
            profile_direct.to_dict(),
            cast(dict[str, object], profile_result.structured_content)["decision"],
        )

        explicit = profiles.resolve("deep_coding").to_dict()
        explicit_result = self._call(
            application, "scarcity_select", {"requirement": explicit}
        )
        self.assertFalse(explicit_result.is_error)
        explicit_direct = select_from_inputs(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=version,
            profile_id=None,
            requirement=profiles.resolve("deep_coding"),
            tightening=None,
            policy=None,
            replenishment_states=(),
            collectors=application.collectors,
            clock=application.clock,
        )
        self.assertEqual(
            explicit_direct.to_dict(),
            cast(dict[str, object], explicit_result.structured_content)["decision"],
        )

    def test_valid_no_solution_is_successful(self) -> None:
        application = self._application(
            collectors=_collectors(openai_five=0, openai_weekly=0)
        )
        result = self._call(
            application, "scarcity_select", {"profile_id": "scientific_review"}
        )
        self.assertFalse(result.is_error)
        decision = cast(dict[str, object], result.structured_content)["decision"]
        decision_document = cast(dict[str, object], decision)
        self.assertIsNone(decision_document["selected"])
        self.assertIn(
            "no_eligible_candidate",
            cast(list[object], decision_document["reason_codes"]),
        )

    def test_invalid_logical_inputs_are_structured_and_safe(self) -> None:
        application = self._application()
        _, profiles, _ = load_configured_artifacts(CATALOG_PATH, POLICY_PATH)
        requirement = profiles.resolve("deep_coding").to_dict()
        cases: tuple[Mapping[str, object], ...] = (
            {"profile_id": "unknown-profile"},
            {"profile_id": "deep_coding", "requirement": requirement},
            {},
            {"replenishment_states": None, "profile_id": "deep_coding"},
            {"profile_id": "deep_coding", "unexpected": "do-not-reflect"},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self._call(application, "scarcity_select", arguments)
                self.assertTrue(result.is_error)
                self.assertEqual(invalid_request_payload(), _error_payload(result))
                self.assertEqual(_error_payload(result), _text_payload(result))
                self.assertNotIn("do-not-reflect", json.dumps(_error_payload(result)))


class SimulationTests(McpTestCase):
    def test_empty_and_98_2_simulations_match_direct_cli_rest_and_mcp(self) -> None:
        application = self._application()
        empty = self._call(
            application,
            "scarcity_simulate",
            {"profile_id": "deep_coding", "overrides": {}},
        )
        self.assertFalse(empty.is_error)

        with tempfile.TemporaryDirectory() as directory:
            override_path = Path(directory) / "overrides.json"
            _ = override_path.write_text(
                json.dumps(BASE_OVERRIDES), encoding="utf-8"
            )
            cli = self._cli_json(
                application,
                [
                    "simulate",
                    "--profile",
                    "deep_coding",
                    "--overrides",
                    str(override_path),
                    "--json",
                ],
            )

        catalog, profiles, version = load_configured_artifacts(
            CATALOG_PATH, POLICY_PATH
        )
        direct = simulate_from_inputs(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=version,
            profile_id="deep_coding",
            requirement=None,
            tightening=None,
            policy=None,
            replenishment_states=(),
            overrides=SimulationOverrides.from_dict(BASE_OVERRIDES),
            collectors=application.collectors,
            clock=application.clock,
        )
        harness = self._rest(application)
        status, rest = _rest_request(
            harness,
            "POST",
            "/v1/simulate",
            {"profile_id": "deep_coding", "overrides": BASE_OVERRIDES},
        )
        self.assertEqual(200, status)
        mcp = self._call(
            application,
            "scarcity_simulate",
            {"profile_id": "deep_coding", "overrides": BASE_OVERRIDES},
        )
        self.assertFalse(mcp.is_error)
        rest_result = cast(dict[str, object], rest)["result"]
        mcp_result = cast(dict[str, object], mcp.structured_content)["result"]
        self.assertEqual(direct.to_dict(), cli)
        self.assertEqual(direct.to_dict(), rest_result)
        self.assertEqual(direct.to_dict(), mcp_result)
        self.assertEqual(cast(dict[str, object], mcp.structured_content), _text_payload(mcp))

    def test_semantic_and_schema_override_errors_are_invalid_requests(self) -> None:
        application = self._application()
        nonexistent = {
            "profile_id": "deep_coding",
            "overrides": {
                "capacity_percentages": [
                    {
                        "provider": "zai",
                        "scope_id": "not-a-real-scope",
                        "resource": "tokens",
                        "kind": "weekly",
                        "remaining_percent": 2,
                    }
                ]
            },
        }
        invalid_schema = {
            "profile_id": "deep_coding",
            "overrides": {"capacity_percentages": [{"provider": "zai"}]},
        }
        for arguments in (nonexistent, invalid_schema):
            with self.subTest(arguments=arguments):
                result = self._call(application, "scarcity_simulate", arguments)
                self.assertTrue(result.is_error)
                self.assertEqual(invalid_request_payload(), _error_payload(result))

        def failing_openai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            return _unknown_snap("openai")

        non_ok_application = self._application(
            collectors=StatusCollectors(
                openai=failing_openai,
                zai=_collectors().zai,
            )
        )
        non_ok = {
            "profile_id": "deep_coding",
            "overrides": {
                "capacity_percentages": [
                    {
                        "provider": "openai",
                        "scope_id": "codex",
                        "resource": "tokens",
                        "kind": "weekly",
                        "remaining_percent": 2,
                    }
                ]
            },
        }
        result = self._call(non_ok_application, "scarcity_simulate", non_ok)
        self.assertTrue(result.is_error)
        self.assertEqual(invalid_request_payload(), _error_payload(result))

    def test_tighter_advertised_schema_does_not_replace_handler_validation(self) -> None:
        application = self._application()
        cases: tuple[tuple[str, Mapping[str, object]], ...] = (
            (
                "unknown field",
                {"profile_id": "deep_coding", "unexpected": "field"},
            ),
            ("missing overrides", {"profile_id": "deep_coding"}),
            (
                "null overrides",
                {"profile_id": "deep_coding", "overrides": None},
            ),
            (
                "null top-level replenishment",
                {
                    "profile_id": "deep_coding",
                    "overrides": {},
                    "replenishment_states": None,
                },
            ),
        )
        for label, arguments in cases:
            with self.subTest(label=label):
                result = self._call(application, "scarcity_simulate", arguments)
                self.assertTrue(result.is_error)
                self.assertEqual(invalid_request_payload(), _error_payload(result))


class InternalFailureTests(McpTestCase):
    def test_malformed_catalog_is_fixed_internal_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "malformed-catalog.json"
            _ = catalog_path.write_text("{", encoding="utf-8")
            result = self._call(
                self._application(catalog_path=catalog_path),
                "scarcity_select",
                {"profile_id": "deep_coding"},
            )
        self.assertTrue(result.is_error)
        self.assertEqual(internal_error_payload(), _error_payload(result))
        serialized = json.dumps(_error_payload(result))
        self.assertNotIn("malformed-catalog", serialized)
        self.assertNotIn("Traceback", serialized)
        self.assertNotIn("ValueError", serialized)

    def test_unexpected_collector_failure_is_fixed_internal_error(self) -> None:
        def failing_openai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            raise RuntimeError("synthetic provider payload SECRET_MARKER")

        result = self._call(
            self._application(
                collectors=StatusCollectors(
                    openai=failing_openai,
                    zai=_collectors().zai,
                )
            ),
            "scarcity_status",
            {},
        )
        self.assertTrue(result.is_error)
        self.assertEqual(internal_error_payload(), _error_payload(result))
        serialized = json.dumps(_error_payload(result))
        self.assertNotIn("SECRET_MARKER", serialized)
        self.assertNotIn("RuntimeError", serialized)


class RawSdkCompatibilityTests(unittest.TestCase):
    """Pin the observed official SDK decoder boundary, not app semantics."""

    @staticmethod
    def _arguments(raw: str) -> dict[str, object]:
        message = mcp_types.jsonrpc_message_adapter.validate_json(raw, by_name=False)
        request = cast(JSONRPCRequest, message)
        params = cast(dict[str, object] | None, request.params)
        if params is None:
            raise AssertionError("decoded request has no params")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            raise AssertionError("decoded request has no argument object")
        return cast(dict[str, object], arguments)

    def test_duplicate_object_keys_are_accepted_and_last_value_wins(self) -> None:
        arguments = self._arguments(
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            + '"params":{"name":"scarcity_select",'
            + '"arguments":{"profile_id":"first","profile_id":"last"}}}'
        )
        self.assertEqual("last", arguments["profile_id"])

    def test_nonfinite_numbers_reach_decoded_arguments_as_floats(self) -> None:
        predicates: tuple[tuple[str, Callable[[float], bool]], ...] = (
            ("NaN", math.isnan),
            ("Infinity", lambda value: value == float("inf")),
            ("-Infinity", lambda value: value == float("-inf")),
        )
        for token, predicate in predicates:
            with self.subTest(token=token):
                arguments = self._arguments(
                    '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                    + '"params":{"name":"scarcity_select",'
                    + '"arguments":{"profile_id":%s}}}' % token
                )
                value = arguments["profile_id"]
                self.assertIsInstance(value, float)
                self.assertTrue(predicate(cast(float, value)))

    def test_malformed_json_and_jsonrpc_shape_are_rejected_before_handler(self) -> None:
        malformed = (
            '{"jsonrpc":"2.0","id":1,"method":"initialize"',
            '{"jsonrpc":"2.0","id":1}',
            '{"jsonrpc":"1.0","id":1,"method":"initialize"}',
        )
        for raw in malformed:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    _ = mcp_types.jsonrpc_message_adapter.validate_json(
                        raw, by_name=False
                    )


class StdioLifecycleTests(unittest.TestCase):
    def test_real_stdio_subprocess_discovery_and_cleanup(self) -> None:
        names = anyio.run(_stdio_subprocess_discover_async)
        self.assertEqual(list(MCP_TOOL_NAMES), names)


if __name__ == "__main__":
    _ = unittest.main()
