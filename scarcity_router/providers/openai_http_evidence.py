"""Dated OpenAI compatibility-matrix evidence for the HTTP presets (M04).

The D-043 compatibility matrix is keyed per (adapter, adapter version,
model/backend); cell values here are the EVIDENCE-BASED DEFAULTS each
preset asserts for a registered backend model — the administrator's
resource/matrix configuration (M09) applies them per concrete model and
may only NARROW them silently; raising a cell above the evidenced default
requires the administrator's own dated evidence (issue #89: "resources
without verified capability ratings remain unrated/unknown rather than
automatically sufficient").

Every cell carries the preset's dated evidence reference plus a
cell-specific note. Where today's retrieval could not verify a fact, the
cell is ``PARTIAL`` or ``UNKNOWN`` — never a guess (``UNKNOWN`` fails
closed at admission, D-043). The most important fail-closed example is the
generic preset: with NO vendor evidence, every cell is ``UNKNOWN`` and
nothing passes the capability gates until the administrator supplies
evidence.

The ``cancellation`` dimension is ``PARTIAL`` for every HTTP preset by
construction: the adapter's evidenced behavior is closing the connection
on client disconnect; whether each upstream provider aborts its
generation is not evidenced by their compatibility documentation.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..routing_core import COMPAT_FEATURES, CompatibilityCell
from ..selection_types import EvidenceRef
from .openai_http_adapter import ADAPTER_NAME, ADAPTER_VERSION
from .openai_http_presets import (
    DEEPSEEK_PRESET,
    GENERIC_PRESET,
    OLLAMA_PRESET,
    OPENAI_API_PRESET,
    OPENROUTER_PRESET,
    ProviderPreset,
    ZAI_CODING_PLAN_PRESET,
)

_CHANNEL = "server_direct_http"

_COMMON_CANCELLATION_NOTE = (
    "gateway closes the connection on client disconnect; upstream abort "
    + "behavior unevidenced"
)

#: preset_id -> feature -> (cell value, cell-specific evidence note).
PRESET_CELL_VALUES: Mapping[str, Mapping[str, tuple[str, str]]] = {
    OPENAI_API_PRESET.preset_id: {
        "roles_history": ("PASS", "multi-role conversations; developer role documented"),
        "streaming": ("PASS", "SSE chat.completion.chunk surface"),
        "tool_calls": ("PASS", "function tools and tool_choice documented"),
        "tool_results": ("PASS", "tool role documented"),
        "structured_output": ("PASS", "response_format json_object/json_schema"),
        "reasoning_controls": (
            "PASS",
            "reasoning_effort minimal/low/medium/high",
        ),
        "context_limits": (
            "PARTIAL",
            "per-model; only the admission floor is enforced here",
        ),
        "error_semantics": (
            "PASS",
            "OpenAI error envelope; corroborated wire conventions",
        ),
        "usage_reporting": (
            "PASS",
            "usage object on completions; final stream chunk with "
            + "stream_options.include_usage",
        ),
        "cancellation": ("PARTIAL", _COMMON_CANCELLATION_NOTE),
    },
    DEEPSEEK_PRESET.preset_id: {
        "roles_history": ("PASS", "system/user/assistant/tool documented"),
        "streaming": ("PASS", "SSE terminated by data: [DONE]"),
        "tool_calls": (
            "PASS",
            "function tools; tool_choice documented (thinking-mode "
            + "restrictions surface as runtime 400s and map honestly)",
        ),
        "tool_results": ("PASS", "tool role documented"),
        "structured_output": (
            "PARTIAL",
            "json_object documented; json_schema not documented and refused "
            + "by the preset",
        ),
        "reasoning_controls": (
            "PASS",
            "thinking.reasoning_effort with the provider's documented "
            + "OpenAI-style value mapping (minimal->low, medium->high)",
        ),
        "context_limits": ("PARTIAL", "max_tokens 1..393216; model-dependent"),
        "error_semantics": (
            "PARTIAL",
            "error codes 400/401/402/422/429/500/503 documented; body shape "
            + "not documented; mapped conservatively by status",
        ),
        "usage_reporting": (
            "PASS",
            "prompt/completion/total plus cache details; stream usage rides "
            + "on the last content chunk (shape tolerated)",
        ),
        "cancellation": ("PARTIAL", _COMMON_CANCELLATION_NOTE),
    },
    OPENROUTER_PRESET.preset_id: {
        "roles_history": ("PASS", "messages passthrough documented"),
        "streaming": (
            "PASS",
            "SSE; usage exactly once in the final chunk before [DONE] "
            + "(non-empty choices shape tolerated)",
        ),
        "tool_calls": (
            "PASS",
            "tools/tool_choice passthrough documented; providers may "
            + "transform tools (tool_calls still arrive on the wire)",
        ),
        "tool_results": (
            "PARTIAL",
            "carried by the documented messages passthrough; per-role "
            + "semantics not separately evidenced",
        ),
        "structured_output": (
            "PASS",
            "response_format json_object and json_schema (strict) documented",
        ),
        "reasoning_controls": (
            "PASS",
            "reasoning.effort documented as OpenAI-style effort values",
        ),
        "context_limits": (
            "PARTIAL",
            "max_tokens within the per-model context length",
        ),
        "error_semantics": (
            "PARTIAL",
            "per-choice error objects and finish reasons including "
            + "content_filter/error documented; unmapped reasons fail closed",
        ),
        "usage_reporting": (
            "PASS",
            "usage always returned with detailed breakdowns (extra fields "
            + "tolerated)",
        ),
        "cancellation": ("PARTIAL", _COMMON_CANCELLATION_NOTE),
    },
    ZAI_CODING_PLAN_PRESET.preset_id: {
        "roles_history": (
            "PASS",
            "user/system/assistant/tool documented; not system/assistant-only",
        ),
        "streaming": ("PASS", "SSE ending with data: [DONE]"),
        "tool_calls": (
            "PARTIAL",
            "function tools (max 128) documented; tool_choice only auto "
            + "(other values refused by the preset)",
        ),
        "tool_results": ("PASS", "tool role documented"),
        "structured_output": (
            "PARTIAL",
            "text/json_object documented; json_schema not documented and "
            + "refused by the preset",
        ),
        "reasoning_controls": (
            "PARTIAL",
            "thinking + reasoning_effort (GLM-5.3: low/high/max) evidenced "
            + "on the platform reference; coding-endpoint parity not "
            + "separately evidenced",
        ),
        "context_limits": (
            "PARTIAL",
            "max_tokens 1..131072 (128K output on GLM-5.x/4.7/4.6)",
        ),
        "error_semantics": (
            "PARTIAL",
            "documented {code, message} shape is NOT the OpenAI envelope; "
            + "mapped conservatively by status",
        ),
        "usage_reporting": (
            "PARTIAL",
            "usage documented on completions; stream_options not documented "
            + "(omitted), so stream usage presence is unevidenced",
        ),
        "cancellation": ("PARTIAL", _COMMON_CANCELLATION_NOTE),
    },
    OLLAMA_PRESET.preset_id: {
        "roles_history": (
            "PASS",
            "messages supported on the compatibility surface; roles "
            + "system/user/assistant/tool evidenced on the native chat schema",
        ),
        "streaming": (
            "PASS",
            "streaming supported; stream_options.include_usage documented",
        ),
        "tool_calls": (
            "PASS",
            "tools documented as supported; tool_choice unsupported "
            + "(explicit auto omitted as the documented mapping; other "
            + "values refused)",
        ),
        "tool_results": (
            "PARTIAL",
            "tool roles evidenced on the native /api/chat schema; "
            + "OpenAI-compat surface tool-role handling not separately "
            + "evidenced",
        ),
        "structured_output": (
            "PARTIAL",
            "response_format JSON mode supported; json_schema not "
            + "documented on the compatibility page and refused",
        ),
        "reasoning_controls": (
            "PASS",
            "reasoning_effort and reasoning.effort documented "
            + "(high/medium/low/max/none)",
        ),
        "context_limits": (
            "PARTIAL",
            "per-model context; not evidenced per model here",
        ),
        "error_semantics": (
            "UNKNOWN",
            "the compatibility page documents no error schema; failures "
            + "are mapped conservatively by status only",
        ),
        "usage_reporting": (
            "PARTIAL",
            "stream_options.include_usage documented; the non-streaming "
            + "usage shape is not separately documented",
        ),
        "cancellation": ("PARTIAL", _COMMON_CANCELLATION_NOTE),
    },
    GENERIC_PRESET.preset_id: {
        feature: (
            "UNKNOWN",
            "no vendor evidence; the administrator must supply dated "
            + "evidence cells (M09) before these gates pass",
        )
        for feature in COMPAT_FEATURES
    },
}


def _cell_evidence(preset: ProviderPreset, note: str) -> EvidenceRef:
    identifier = f"{preset.evidence.identifier} | {note}"
    if len(identifier) > 512:
        raise ValueError(
            "compatibility evidence: cell note exceeds the bounded "
            + "identifier length"
        )
    return EvidenceRef(
        source=preset.evidence.source,
        identifier=identifier,
        date=preset.evidence.date,
    )


def default_cells_for(
    preset: ProviderPreset, *, provider: str, model: str
) -> tuple[CompatibilityCell, ...]:
    """The evidenced default cells for one registered backend model.

    Called per (provider, model) at resource/matrix configuration time
    (M09); values are backend-level (variant ``None``) and fail closed:
    any feature the preset table does not evidence simply stays absent
    from the matrix, which the routing core treats as fail-closed too.
    """
    table = PRESET_CELL_VALUES.get(preset.preset_id)
    if table is None:
        raise ValueError(
            f"compatibility evidence: no cell table for preset "
            + f"'{preset.preset_id}'"
        )
    cells: list[CompatibilityCell] = []
    for feature in COMPAT_FEATURES:
        entry = table.get(feature)
        if entry is None:
            continue
        value, note = entry
        cells.append(
            CompatibilityCell(
                channel=_CHANNEL,
                provider=provider,
                model=model,
                feature=feature,
                value=value,
                adapter=ADAPTER_NAME,
                adapter_version=ADAPTER_VERSION,
                evidence=_cell_evidence(preset, note),
            )
        )
    return tuple(cells)


def all_preset_cell_tables() -> tuple[str, ...]:
    """The preset ids with an evidenced default cell table."""
    return tuple(sorted(PRESET_CELL_VALUES))


__all__ = [
    "PRESET_CELL_VALUES",
    "all_preset_cell_tables",
    "default_cells_for",
]
