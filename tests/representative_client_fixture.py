"""Sanitized representative-client request fixture (program #132, child #133).

Derived from the first real representative-client acceptance capture
(2026-09-26: ZCode on Windows against the Scarcity Router execution
surface on ``gpt-5.6-luna``). No real prompt content, client tool catalog
or captured payload is reproduced: the fixture keeps only the load-bearing
request semantics so gateway tests exercise the shape real coding agents
actually send — a bare logical model id, a client-owned tool array,
streaming with usage, simultaneous reasoning-dialect fields and a
model-default output ceiling.

``tests/test_representative_client.py`` pins the current-state behavior
for each observed acceptance failure; program children #134 (logical
model resolution), #135 (reasoning dialects), #136 (effective limits)
and #137 (client tools) each flip their pin to the target contract.
"""

from __future__ import annotations

REPRESENTATIVE_MODEL = "gpt-5.6-luna"
REPRESENTATIVE_OUTPUT_TOKENS = 128_000

REPRESENTATIVE_SYSTEM_PROMPT = (
    "You are a coding assistant operating inside the user's editor. "
    "Use the provided tools to inspect the workspace before answering."
)
REPRESENTATIVE_USER_PROMPT = "List the files in the current directory."

# The four reasoning representations the real client emitted
# simultaneously on one request, verbatim in shape.
REPRESENTATIVE_REASONING_DIALECTS: dict[str, object] = {
    "thinking": {"type": "enabled"},
    "enable_thinking": True,
    "reasoning_effort": "max",
    "reasoning": {"effort": "max"},
}


def representative_tools() -> list[dict[str, object]]:
    """Two synthetic coding-agent-shaped client-owned function tools.

    Structurally representative of the real client's tool array (function
    definitions with JSON-schema parameters); the names and schemas are
    synthetic and deliberately tiny.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files and directories under a path.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read one UTF-8 text file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
    ]


def representative_request(
    *,
    model: str = REPRESENTATIVE_MODEL,
    include_reasoning_dialects: bool = True,
    include_output_limit: bool = True,
) -> dict[str, object]:
    """The representative client request body.

    ``model`` defaults to the bare logical id the real client sent (no
    alias, no ``sr-pin:``). The reasoning dialects and the output ceiling
    can be dropped independently so tests can isolate each surface gate.
    """
    document: dict[str, object] = {
        "model": model,
        "messages": [
            {"role": "system", "content": REPRESENTATIVE_SYSTEM_PROMPT},
            {"role": "user", "content": REPRESENTATIVE_USER_PROMPT},
        ],
        "tools": representative_tools(),
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if include_reasoning_dialects:
        document.update(REPRESENTATIVE_REASONING_DIALECTS)
    if include_output_limit:
        document["max_completion_tokens"] = REPRESENTATIVE_OUTPUT_TOKENS
    return document


__all__ = [
    "REPRESENTATIVE_MODEL",
    "REPRESENTATIVE_OUTPUT_TOKENS",
    "REPRESENTATIVE_REASONING_DIALECTS",
    "REPRESENTATIVE_SYSTEM_PROMPT",
    "REPRESENTATIVE_USER_PROMPT",
    "representative_request",
    "representative_tools",
]
