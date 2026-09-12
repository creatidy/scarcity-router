"""Minimal stdio MCP adapter for machine-interface contract v1 (M3c).

The official MCP SDK owns protocol framing and decoding. This module only
maps MCP tool arguments to the shared logical machine boundary, invokes the
typed application layer in-process and returns the existing machine envelopes.
It never starts or contacts the REST server and never executes model calls.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import cast

import anyio
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from . import get_version
from .errors import ApplicationInputError
from .machine_api import (
    internal_error_payload,
    invalid_request_payload,
    parse_selection_document,
    parse_simulation_document,
    parse_status_arguments,
    selection_envelope,
    simulation_envelope,
    status_envelope,
)
from .selection_app import (
    ApplicationDependencies,
    load_configured_artifacts,
    select_from_inputs,
    simulate_from_inputs,
)
from .config import resolve_default_selector_policy
from .selection_types import ModelCatalog, TaskProfileCatalog
from .status import collect_status

MCP_TOOL_NAMES: tuple[str, str, str] = (
    "scarcity_status",
    "scarcity_select",
    "scarcity_simulate",
)

_TOOL_DESCRIPTION = (
    "Scarcity Router machine-interface contract v1. "
    "This tool reports or recommends only; it never executes model inference."
)


def _object_schema(
    properties: Mapping[str, object],
    *,
    required: list[str] | None = None,
) -> dict[str, object]:
    """Advertise structural constraints; the shared parser owns semantics."""
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required is not None:
        schema["required"] = required
    return schema


_SELECT_PROPERTIES: dict[str, object] = {
    "profile_id": {
        "type": ["string", "null"],
        "description": "Calibrated task profile id.",
    },
    "requirement": {
        "type": ["object", "null"],
        "description": "Serialized TaskRequirement.",
    },
    "tightening": {
        "type": ["object", "null"],
        "description": "Serialized monotone profile tightening.",
    },
    "selector_policy": {
        "type": ["object", "null"],
        "description": "Serialized SelectorPolicy.",
    },
    "replenishment_states": {
        "type": "array",
        "description": "Normalized replenishment observations.",
    }
}
_SELECT_INPUT_SCHEMA = _object_schema(_SELECT_PROPERTIES)

_SIMULATE_INPUT_SCHEMA = _object_schema(
    {
        **_SELECT_PROPERTIES,
        "overrides": {
            "type": "object",
            "description": "Serialized SimulationOverrides; required.",
        },
    },
    required=["overrides"],
)

_TOOLS: tuple[Tool, Tool, Tool] = (
    Tool(
        name="scarcity_status",
        description=_TOOL_DESCRIPTION + " It accepts no logical input fields.",
        input_schema=_object_schema({}),
    ),
    Tool(
        name="scarcity_select",
        description=(
            _TOOL_DESCRIPTION
            + " Inputs mirror the REST /v1/select logical contract."
        ),
        input_schema=_SELECT_INPUT_SCHEMA,
    ),
    Tool(
        name="scarcity_simulate",
        description=(
            _TOOL_DESCRIPTION
            + " Inputs mirror the REST /v1/simulate logical contract."
        ),
        input_schema=_SIMULATE_INPUT_SCHEMA,
    ),
)


def _text_result(payload: dict[str, object], *, is_error: bool) -> CallToolResult:
    """Return structured content plus deterministic text for SDK clients."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return CallToolResult(
        content=[TextContent(text=text)],
        structured_content=payload,
        is_error=is_error,
    )


def _success_result(payload: dict[str, object]) -> CallToolResult:
    return _text_result(payload, is_error=False)


def _error_result() -> CallToolResult:
    return _text_result(invalid_request_payload(), is_error=True)


def _internal_result() -> CallToolResult:
    return _text_result(internal_error_payload(), is_error=True)


def _arguments(params: CallToolRequestParams) -> dict[str, object]:
    raw = params.arguments
    if raw is None:
        return {}
    return cast(dict[str, object], raw)


def _configured_artifacts(
    application: ApplicationDependencies,
) -> tuple[ModelCatalog, TaskProfileCatalog, int]:
    """Load process-configured artifacts without exposing their paths to MCP."""
    return load_configured_artifacts(
        application.catalog_path, application.model_policy_path
    )


async def _call_tool(
    application: ApplicationDependencies,
    params: CallToolRequestParams,
) -> CallToolResult:
    if params.name not in MCP_TOOL_NAMES:
        # Unknown tools are protocol routing, not Scarcity Router domain input.
        raise ValueError("unknown MCP tool")

    try:
        arguments = _arguments(params)
        if params.name == "scarcity_status":
            parse_status_arguments(arguments)
            snapshots = collect_status(
                collectors=application.collectors, clock=application.clock
            )
            return _success_result(status_envelope(snapshots))

        if params.name == "scarcity_select":
            parsed = parse_selection_document(arguments)
            catalog, profiles, profile_policy_version = _configured_artifacts(
                application
            )
            decision = select_from_inputs(
                catalog=catalog,
                profiles=profiles,
                profile_policy_version=profile_policy_version,
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
            return _success_result(selection_envelope(decision))

        parsed, overrides = parse_simulation_document(arguments)
        catalog, profiles, profile_policy_version = _configured_artifacts(application)
        result = simulate_from_inputs(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=profile_policy_version,
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
        return _success_result(simulation_envelope(result))
    except ApplicationInputError:
        return _error_result()
    except Exception:
        # Application and configuration failures are deliberately opaque.
        return _internal_result()


def build_server(
    application: ApplicationDependencies | None = None,
) -> Server[None]:
    """Build the low-level official MCP server with injected dependencies."""
    dependencies = application or ApplicationDependencies()

    @asynccontextmanager
    async def empty_lifespan(_server: Server[None]) -> AsyncGenerator[None, None]:
        yield None

    async def on_list_tools(
        _context: ServerRequestContext[None],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=list(_TOOLS))

    async def on_call_tool(
        _context: ServerRequestContext[None],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        return await _call_tool(dependencies, params)

    return Server(
        "scarcity-router",
        version=get_version(),
        lifespan=empty_lifespan,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def run_stdio(
    application: ApplicationDependencies | None = None,
) -> None:
    """Run one official SDK stdio connection until the MCP client closes it."""
    server = build_server(application)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Run the stdio-only MCP adapter; stdout is reserved for MCP framing.

    The default user selector policy (D-036) is provisioned and loaded once
    at process start; requests that omit ``selector_policy`` run under it,
    requests that supply one override it, and a broken or missing default
    degrades to the neutral policy with a stderr warning.
    """
    application = ApplicationDependencies(
        default_policy=resolve_default_selector_policy()
    )
    anyio.run(run_stdio, application)


__all__ = ["MCP_TOOL_NAMES", "build_server", "main", "run_stdio"]


if __name__ == "__main__":
    main()
