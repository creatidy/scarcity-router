"""Evidence-based provider presets for the generic OpenAI-compatible HTTP
adapter (M04, issue #89).

ONE generic adapter serves every preset; a preset is pure configuration:
the evidenced endpoint path, the translation policy knobs (see
:class:`~scarcity_router.providers.openai_http_core.TranslationPolicy`),
the suggested entitlement class (kept DISTINCT per D-042 — a preset
suggests; the resource registration decides), and the dated evidence
reference. Presets are not provider gateways: there is exactly one wire
implementation and zero provider-specific code paths.

Evidence discipline (issue #89 / ``docs/providers.md``): every knob below
is backed by the dated retrieval of the provider's official documentation
listed on the preset. Where today's automated retrieval could not verify a
fact, the fact is NOT asserted: the knob stays conservative (explicit
rejection, documented omission) and the compatibility-matrix cell stays
``PARTIAL`` or ``UNKNOWN`` (fail closed) — see
:mod:`scarcity_router.providers.openai_http_evidence`.

OpenAI note: ``platform.openai.com`` blocks automated retrieval (HTTP 403
on 2026-09-20, and the reference moved to ``developers.openai.com``); the
OpenAI preset's wire facts are evidenced by this repository's own
execution-surface v1 contract (``docs/execution-surface.md``, the M03
OpenAI-compatible surface) plus the provider documents below that describe
themselves explicitly against the OpenAI API. Cells that would need the
provider reference itself carry that limitation in the evidence module.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..selection_types import EvidenceRef
from .openai_http_core import TranslationPolicy

# ── Dated evidence references (retrieved 2026-09-20) ──────────────────────────

EVIDENCE_DATE = "2026-09-20"

OPENAI_EVIDENCE = EvidenceRef(
    source="official_docs",
    identifier=(
        "https://platform.openai.com/docs/api-reference/chat (automated "
        + "retrieval blocked, HTTP 403, 2026-09-20; wire conventions "
        + "corroborated by docs/execution-surface.md and the provider "
        + "documents below, retrieved 2026-09-20)"
    ),
    date=EVIDENCE_DATE,
)

DEEPSEEK_EVIDENCE = EvidenceRef(
    source="official_docs",
    identifier=(
        "https://api-docs.deepseek.com/ and "
        + "https://api-docs.deepseek.com/api/create-chat-completion and "
        + "https://api-docs.deepseek.com/quick_start/error_codes"
    ),
    date=EVIDENCE_DATE,
)

OPENROUTER_EVIDENCE = EvidenceRef(
    source="official_docs",
    identifier=(
        "https://openrouter.ai/docs/api-reference/overview and "
        + "https://openrouter.ai/docs/use-cases/reasoning-tokens"
    ),
    date=EVIDENCE_DATE,
)

ZAI_CODING_EVIDENCE = EvidenceRef(
    source="official_docs",
    identifier=(
        "https://docs.z.ai/devpack/quick-start (Coding Plan OpenAI "
        + "Chat Completions base URL https://api.z.ai/api/coding/paas/v4) and "
        + "https://docs.z.ai/api-reference/llm/chat-completion and "
        + "https://docs.z.ai/guides/llm/glm-4.6"
    ),
    date=EVIDENCE_DATE,
)

OLLAMA_EVIDENCE = EvidenceRef(
    source="official_docs",
    identifier=(
        "https://docs.ollama.com/api/openai-compatibility and "
        + "https://docs.ollama.com/api (version/tags/ps endpoints) and "
        + "https://docs.ollama.com/api/chat (native tool-role schema)"
    ),
    date=EVIDENCE_DATE,
)

OLLAMA_VERSION_NOTE = (
    "Ollama docs state a version only for one endpoint addition "
    + "(Responses API added in v0.13.3); the compatibility page itself is "
    + "unversioned, so the tested version is whatever instance the "
    + "administrator points the resource at and MUST be re-verified per "
    + "deployment (M09 diagnostics surface)."
)


# ── The preset record ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderPreset:
    """One evidence-backed preset of the generic HTTP adapter.

    ``suggested_entitlement`` is a D-042 entitlement-class SUGGESTION for
    the administrator's resource registration — never an entitlement
    decision by the adapter, and never silently changed by a model name.
    The Z.ai Coding Plan preset is subscription-backed
    (``subscription_included``); the same vendor's PAYG platform API is a
    DIFFERENT preset (the generic one, ``payg_metered``) — matching model
    names never merge the two.
    """

    preset_id: str
    display_name: str
    provider: str
    policy: TranslationPolicy
    evidence: EvidenceRef
    suggested_entitlement: str
    preset_note: str


# ── The presets ───────────────────────────────────────────────────────────────

#: OpenAI API: the reference OpenAI-compatible surface. Endpoint
#: ``/v1/chat/completions``; ``max_completion_tokens``; ``developer`` role;
#: ``reasoning_effort`` (minimal/low/medium/high).
OPENAI_API_PRESET = ProviderPreset(
    preset_id="openai-api",
    display_name="OpenAI API",
    provider="openai",
    suggested_entitlement="payg_metered",
    evidence=OPENAI_EVIDENCE,
    preset_note=(
        "The reference surface. Uses the modern max_completion_tokens "
        + "field; streaming includes the final usage chunk when "
        + "stream_options.include_usage is requested."
    ),
    policy=TranslationPolicy(
        preset_id="openai-api",
        endpoint_path="/v1/chat/completions",
        max_tokens_field="max_completion_tokens",
        developer_role="pass",
        tool_choice_policy="pass",
        response_format_policy="pass",
        reasoning_policy="reasoning_effort",
        reasoning_value_map={
            "minimal": "minimal",
            "low": "low",
            "medium": "medium",
            "high": "high",
        },
        unevidenced_generation_params=frozenset(),
        omitted_generation_params=frozenset(),
        stream_options="request_include_usage",
        requires_credential=True,
        discovery_path=None,
        health_path=None,
    ),
)

#: DeepSeek API: OpenAI-compatible endpoint ``/chat/completions`` on
#: ``https://api.deepseek.com`` (documented with and without ``/v1``;
#: the preset pins the documented bare path). Evidenced today:
#: ``max_tokens`` (1..393216), ``response_format`` text/json_object only
#: (no json_schema documented -> refused), function ``tools`` with
#: ``tool_choice`` (none/auto/required/named; required+named are refused
#: by the backend in thinking mode -> mapped honestly at runtime),
#: ``thinking: {type, reasoning_effort}`` with DeepSeek's own documented
#: OpenAI-style value mapping, SSE with ``data: [DONE]`` and usage riding
#: on the last content chunk, finish reasons including provider-specific
#: tokens (mapped conservatively), and error codes 400/401/402/422/429/
#: 500/503. ``frequency_penalty``/``presence_penalty`` are documented as
#: no longer supported -> documented omission. ``stop``/``seed`` and
#: ``parallel_tool_calls`` were NOT documented on the retrieved reference
#: page -> unevidenced -> explicit refusal when a client sends them.
DEEPSEEK_PRESET = ProviderPreset(
    preset_id="deepseek",
    display_name="DeepSeek API",
    provider="deepseek",
    suggested_entitlement="payg_metered",
    evidence=DEEPSEEK_EVIDENCE,
    preset_note=(
        "OpenAI-compatible API at https://api.deepseek.com/chat/completions. "
        + "JSON-mode structured output only (json_schema refused); "
        + "reasoning via the documented thinking object; penalties omitted "
        + "(documented as no longer supported); stop/seed/"
        + "parallel_tool_calls unevidenced on the retrieved reference and "
        + "therefore refused rather than forwarded."
    ),
    policy=TranslationPolicy(
        preset_id="deepseek",
        endpoint_path="/chat/completions",
        max_tokens_field="max_tokens",
        developer_role="reject",
        tool_choice_policy="pass",
        response_format_policy="json_object_only",
        reasoning_policy="thinking_deepseek",
        reasoning_value_map={
            "minimal": "low",  # DeepSeek's own documented mapping
            "low": "low",
            "medium": "high",  # DeepSeek's own documented mapping
            "high": "high",
        },
        unevidenced_generation_params=frozenset({
            "stop",
            "seed",
            "parallel_tool_calls",
        }),
        omitted_generation_params=frozenset({
            "frequency_penalty",
            "presence_penalty",
        }),
        stream_options="request_include_usage",
        requires_credential=True,
        discovery_path=None,
        health_path=None,
    ),
)

#: OpenRouter: OpenAI-compatible router at ``https://openrouter.ai/api/v1``
#: with ``/chat/completions``. Evidenced today: tools/tool_choice
#: passthrough, response_format json_object/json_schema (with strict), the
#: unified ``reasoning`` object whose ``effort`` is documented as
#: OpenAI-style, SSE streaming with usage exactly once in the final chunk
#: BEFORE [DONE] where that chunk carries a NON-EMPTY choices array (the
#: parser tolerates both shapes), ``native_finish_reason`` alongside the
#: normalized ``finish_reason`` (values include content_filter and error,
#: which have no normalized mapping and fail closed), and per-choice
#: error objects. ``stream_options`` as a REQUEST parameter was not
#: documented on the retrieved pages -> omitted (usage arrives regardless).
#: Non-evidenced surface parameters (repetition_penalty, top_k, plugins,
#: models, route, provider, ...) are surface-accepted values the adapter
#: never originates; only the surface's own closed parameter set is sent.
OPENROUTER_PRESET = ProviderPreset(
    preset_id="openrouter",
    display_name="OpenRouter",
    provider="openrouter",
    suggested_entitlement="payg_metered",
    evidence=OPENROUTER_EVIDENCE,
    preset_note=(
        "OpenAI-compatible router; usage arrives in the final stream chunk "
        + "(non-empty choices shape tolerated); reasoning via the unified "
        + "reasoning object; stream_options is not a documented request "
        + "parameter and is omitted."
    ),
    policy=TranslationPolicy(
        preset_id="openrouter",
        endpoint_path="/api/v1/chat/completions",
        max_tokens_field="max_tokens",
        developer_role="reject",
        tool_choice_policy="pass",
        response_format_policy="pass",
        reasoning_policy="openrouter_reasoning",
        reasoning_value_map={
            "minimal": "minimal",
            "low": "low",
            "medium": "medium",
            "high": "high",
        },
        unevidenced_generation_params=frozenset({"parallel_tool_calls"}),
        omitted_generation_params=frozenset(),
        stream_options="omit",
        requires_credential=True,
        discovery_path=None,
        health_path=None,
    ),
)

#: Z.ai Coding Plan: the vendor-documented OpenAI Chat Completions
#: endpoint for Coding Plan subscribers, ``https://api.z.ai/api/coding/paas/v4``
#: (docs.z.ai/devpack/quick-start), authenticated with a CODING PLAN key
#: (distinct from platform keys; not interchangeable per the docs). The
#: chat-completion reference (retrieved 2026-09-20) evidences: function
#: tools (max 128) with tool_choice restricted to ``auto``, response_format
#: text/json_object only, ``thinking: {type: enabled}`` plus a top-level
#: ``reasoning_effort`` (GLM-5.3: low/high/max), SSE ending with
#: ``data: [DONE]``, usage with prompt/completion/total tokens,
#: finish reasons including provider-specific tokens (sensitive,
#: model_context_window_exceeded, network_error -> no normalized mapping,
#: fail closed), and an error shape {code, message} that is NOT the OpenAI
#: envelope (mapped conservatively by status). stream_options is NOT
#: documented -> omitted (stream usage presence is then unevidenced and
#: reported honestly when absent). The coding endpoint's parity with the
#: documented platform reference for reasoning/thinking is not separately
#: evidenced -> the reasoning cell stays PARTIAL.
ZAI_CODING_PLAN_PRESET = ProviderPreset(
    preset_id="zai-coding-plan",
    display_name="Z.ai Coding Plan",
    provider="zai",
    suggested_entitlement="subscription_included",
    evidence=ZAI_CODING_EVIDENCE,
    preset_note=(
        "Subscription-backed Coding Plan endpoint; NEVER the PAYG platform "
        + "API (that is the generic preset with entitlement payg_metered). "
        + "Tool choice restricted to auto; JSON-mode structured output "
        + "only; reasoning via thinking + reasoning_effort (evidenced on "
        + "the platform reference; coding-endpoint parity unevidenced -> "
        + "PARTIAL)."
    ),
    policy=TranslationPolicy(
        preset_id="zai-coding-plan",
        endpoint_path="/chat/completions",
        max_tokens_field="max_tokens",
        developer_role="reject",
        tool_choice_policy="auto_only",
        response_format_policy="json_object_only",
        reasoning_policy="thinking_zai",
        reasoning_value_map={
            "minimal": "low",
            "low": "low",
            "medium": "high",
            "high": "high",
        },
        unevidenced_generation_params=frozenset({
            "seed",
            "parallel_tool_calls",
        }),
        omitted_generation_params=frozenset(),
        stream_options="omit",
        requires_credential=True,
        discovery_path=None,
        health_path=None,
    ),
)

#: Ollama (network-reachable): OpenAI-compatible surface
#: ``/v1/chat/completions`` (docs.ollama.com/api/openai-compatibility,
#: retrieved 2026-09-20). Evidenced: model, messages, tools; ``max_tokens``
#: (max_completion_tokens NOT documented); ``tool_choice`` NOT supported
#: (explicit ``auto`` omitted as the de-facto default; anything else
#: refused); response_format JSON mode (json_schema not documented on the
#: page -> refused); reasoning_effort AND reasoning.effort (high/medium/
#: low/max/none); stream + stream_options.include_usage; temperature/
#: top_p/seed/stop/frequency_penalty/presence_penalty. ``n``, ``user``,
#: logprobs, logit_bias are documented unsupported (the surface already
#: rejects them at ingress). Model discovery via the native
#: ``GET /api/tags`` ("Fetch a list of models and their details"); health
#: via ``GET /api/version`` ("Retrieve the version of the Ollama") — no
#: dedicated health endpoint is documented, and both reads consume no
#: inference quota. Local requests need no API key, so the credential is
#: optional for this preset; when an administrator configures one (e.g. a
#: proxy in front of Ollama), it is sent as a normal Bearer header bound
#: to the configured origin. Plain HTTP is valid ONLY for the explicit
#: loopback/localhost exception (D-044) enforced by the origin type.
OLLAMA_PRESET = ProviderPreset(
    preset_id="ollama",
    display_name="Ollama (network-reachable)",
    provider="ollama",
    suggested_entitlement="local_ungated",
    evidence=OLLAMA_EVIDENCE,
    preset_note=(
        "One OpenAI-compatible implementation reached server-direct when "
        + "network-accessible; a localhost-only instance is reached through "
        + "the worker (M05) invoking the SAME translation core. tool_choice "
        + "is unsupported and omitted/refused; json_schema response_format "
        + "is unevidenced and refused; discovery/health are native "
        + "read-only endpoints that never consume inference quota. "
        + OLLAMA_VERSION_NOTE
    ),
    policy=TranslationPolicy(
        preset_id="ollama",
        endpoint_path="/v1/chat/completions",
        max_tokens_field="max_tokens",
        developer_role="reject",
        tool_choice_policy="omit_auto",
        response_format_policy="json_object_only",
        reasoning_policy="reasoning_effort",
        reasoning_value_map={
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
        },
        unevidenced_generation_params=frozenset({"parallel_tool_calls"}),
        omitted_generation_params=frozenset(),
        stream_options="request_include_usage",
        requires_credential=False,
        discovery_path="/api/tags",
        health_path="/api/version",
    ),
)

#: Generic administrator-configured OpenAI-compatible endpoint. NO provider
#: evidence exists, so the policy asserts only the wire minimum and every
#: compatibility-matrix cell defaults to UNKNOWN (fail closed): the
#: administrator (M09) must supply their own dated evidence cells before
#: admission lets feature-bearing requests through.
GENERIC_PRESET = ProviderPreset(
    preset_id="generic-openai",
    display_name="Generic OpenAI-compatible endpoint",
    provider="generic",
    suggested_entitlement="unknown",
    evidence=EvidenceRef(
        source="administrator_configuration",
        identifier=(
            "No vendor evidence: an administrator-configured endpoint; "
            + "compatibility evidence is the administrator's responsibility "
            + "(M09) and every default matrix cell is UNKNOWN (fail closed)"
        ),
        date=EVIDENCE_DATE,
    ),
    preset_note=(
        "The escape hatch for self-hosted or undocumented OpenAI-compatible "
        + "servers. Nothing is claimed on the vendor's behalf; unevidenced "
        + "parameters are refused, and all default matrix cells are UNKNOWN."
    ),
    policy=TranslationPolicy(
        preset_id="generic-openai",
        endpoint_path="/v1/chat/completions",
        max_tokens_field="max_tokens",
        developer_role="reject",
        tool_choice_policy="pass",
        response_format_policy="pass",
        reasoning_policy="reasoning_effort",
        reasoning_value_map={
            "minimal": "minimal",
            "low": "low",
            "medium": "medium",
            "high": "high",
        },
        unevidenced_generation_params=frozenset(),
        omitted_generation_params=frozenset(),
        stream_options="request_include_usage",
        requires_credential=False,
        discovery_path=None,
        health_path=None,
    ),
)

PRESETS: tuple[ProviderPreset, ...] = (
    OPENAI_API_PRESET,
    DEEPSEEK_PRESET,
    OPENROUTER_PRESET,
    ZAI_CODING_PLAN_PRESET,
    OLLAMA_PRESET,
    GENERIC_PRESET,
)

_PRESET_INDEX: dict[str, ProviderPreset] = {
    preset.preset_id: preset for preset in PRESETS
}


def preset_by_id(preset_id: str) -> ProviderPreset | None:
    """The preset with this id, or ``None``."""
    return _PRESET_INDEX.get(preset_id)


__all__ = [
    "DEEPSEEK_EVIDENCE",
    "DEEPSEEK_PRESET",
    "EVIDENCE_DATE",
    "GENERIC_PRESET",
    "OLLAMA_EVIDENCE",
    "OLLAMA_PRESET",
    "OPENAI_API_PRESET",
    "OPENAI_EVIDENCE",
    "OPENROUTER_EVIDENCE",
    "OPENROUTER_PRESET",
    "PRESETS",
    "ProviderPreset",
    "ZAI_CODING_EVIDENCE",
    "ZAI_CODING_PLAN_PRESET",
    "preset_by_id",
]
