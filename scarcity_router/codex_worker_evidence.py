"""Dated Codex worker-local adapter compatibility-matrix cells (M06).

The D-043 compatibility-matrix contribution of the M06 Codex execution
adapter, originally as reviewed and recorded in
``docs/codex-adapter-stage1-evidence.md`` (Stage-2 matrix, 2026-09-20;
tested ``codex-cli 0.154.0-alpha.6.2`` against the version-pinned
``rust-v0.155.1`` schemas). This module is a STATIC, module-level table —
support is never derived dynamically at runtime, never inferred from a
parser "happening to work", and never read from inference request
content. Composed deployments (``server_composition``) key these cells
to each configured ``worker_bridged`` Codex resource's physical model so
the production path serves evidence-backed Codex traffic without
synthetic or administrator-invented entries.

Conservative by design: every turn-level cell stays ``PARTIAL`` because
the live half (confirmation against a signed-in subscription home) is
still pending behind the recorded ``LIVE_CODEX_SUBSCRIPTION`` gate — no
cell is upgraded above the reviewed matrix. ``tool_calls`` and
``tool_results`` moved from ``UNSUPPORTED`` to ``PARTIAL`` with the
D-060 client-tool bridge (#137): the mechanism is evidenced at
mechanism level on the current runtime (2026-09-28 probe of
``codex-cli 0.155.0-alpha.16.3`` + official docs + binary-pinned
schemas, re-pinned upstream ``rust-v0.157.1`` by #150), the
deterministic round trip is test-verified end to end, and the live
signed-in round trip stays behind the same M10-class gate — hence
PARTIAL, never PASS. An OLD worker that cannot negotiate protocol
version 3 is never tool-continuation-capable regardless of these cells
(the dispatch path fails closed on the live worker session). ``UNKNOWN``
and ``UNSUPPORTED`` fail closed at admission.

Authority model (issue #106): this built-in evidence is the DEFAULT
ceiling for the Codex worker-local adapter. Nothing here lets
configuration raise a cell above it; administrator narrowing or
elevation with the administrator's own dated evidence remains issue #106
territory and is deliberately not implemented here.
"""

from __future__ import annotations

from collections.abc import Mapping

from .routing_core import COMPAT_FEATURES, CompatibilityCell
from .selection_types import EvidenceRef
from .worker_codex_adapter import CODEX_PROVIDER

#: The worker_bridged channel's Codex adapter identity (D-043 key): the
#: adapter allowlist id is ``codex``; the matrix ``adapter`` field names
#: the worker-local adapter implementation with its own version.
CODEX_WORKER_CHANNEL = "worker_bridged"
CODEX_WORKER_ADAPTER_NAME = "codex-worker-local"
CODEX_WORKER_ADAPTER_VERSION = "1.1.0"

#: The tested Codex generation and the evidence record, as reviewed
#: (docs/codex-adapter-stage1-evidence.md, Stage-2 matrix, 2026-09-20).
_CODEX_TESTED_VERSION = "codex-cli 0.154.0-alpha.6.2 / schemas rust-v0.155.1"
_EVIDENCE_DATE = "2026-09-20"
#: The D-060 tool-bridge evidence date (#137): the 2026-09-28 mechanism
#: re-evidence on the CURRENT runtime plus the deterministic
#: implementation acceptance.
_TOOL_BRIDGE_EVIDENCE_DATE = "2026-09-28"

#: feature -> (cell value, cell-specific evidence note, evidence date).
#: Values are the RECORDED matrix values — nothing upgraded beyond what
#: the dated evidence supports.
CODEX_WORKER_CELL_VALUES: Mapping[str, tuple[str, str, str]] = {
    "roles_history": (
        "PARTIAL",
        "mapping test-verified (system→baseInstructions, "
        + "developer→developerInstructions, prior turns via inject_items, "
        + "final user input); live fidelity pending",
        _EVIDENCE_DATE,
    ),
    "streaming": (
        "PARTIAL",
        "item/agentMessage/delta → text_delta mapping test-verified "
        + "(bounded per delta and cumulative); live delta semantics pending",
        _EVIDENCE_DATE,
    ),
    "tool_calls": (
        "PARTIAL",
        "D-060 bridge: experimentalApi-gated dynamicTools declarations "
        + "and item/tool/call suspensions relayed to the harness; "
        + "mechanism evidenced on codex-cli 0.155.0-alpha.16.3, round "
        + "trip test-verified on the deterministic App Server; live "
        + "signed-in acceptance pending; v3-worker-gated",
        _TOOL_BRIDGE_EVIDENCE_DATE,
    ),
    "tool_results": (
        "PARTIAL",
        "D-060 bridge: the harness's role:tool result answers the SAME "
        + "suspended turn (evidenced success/contentItems response, "
        + "text-only); round trip test-verified; live signed-in "
        + "acceptance pending; v3-worker-gated",
        _TOOL_BRIDGE_EVIDENCE_DATE,
    ),
    "structured_output": (
        "PARTIAL",
        "json_schema → outputSchema mapping test-verified (object/size/"
        + "depth validated); json_object rejected; live enforcement pending",
        _EVIDENCE_DATE,
    ),
    "reasoning_controls": (
        "PARTIAL",
        "exact binding test-verified (slug listed, effort in "
        + "supportedReasoningEfforts, pinned per turn); live acceptance "
        + "of pinned turns pending",
        _EVIDENCE_DATE,
    ),
    "context_limits": (
        "PARTIAL",
        "contextWindowExceeded maps to a safe failure note; no per-model "
        + "context-window discovery implemented",
        _EVIDENCE_DATE,
    ),
    "error_semantics": (
        "PARTIAL",
        "failed turns map to the closed codexErrorInfo vocabulary only; "
        + "free-text error bodies never read; exact client error-code "
        + "parity pending",
        _EVIDENCE_DATE,
    ),
    "usage_reporting": (
        "PARTIAL",
        "tokenUsage last/total → prompt/completion tokens mapping "
        + "test-verified; absent usage stays absent; cached/reasoning "
        + "components not represented",
        _EVIDENCE_DATE,
    ),
    "cancellation": (
        "PARTIAL",
        "cancel or deadline → exactly one bounded turn/interrupt, "
        + "cancelled result, never completed after confirmed cancellation; "
        + "live propagation timing pending",
        _EVIDENCE_DATE,
    ),
}


def codex_worker_cell_evidence(note: str, *, date: str = _EVIDENCE_DATE) -> EvidenceRef:
    """One dated provenance reference for a Codex cell (bounded)."""
    identifier = (
        "docs/codex-adapter-stage1-evidence.md Stage-2 matrix "
        + f"({_CODEX_TESTED_VERSION}) | {note}"
    )
    if len(identifier) > 512:
        raise ValueError(
            "codex compatibility evidence: cell note exceeds the bounded "
            + "identifier length"
        )
    return EvidenceRef(
        source="codex_adapter_stage2",
        identifier=identifier,
        date=date,
    )


def default_codex_worker_cells(
    *, provider: str, model: str
) -> tuple[CompatibilityCell, ...]:
    """The reviewed M06 cells for one Codex resource's physical model.

    Keyed to the resource identity the way the routing core looks cells
    up: channel ``worker_bridged``, the resource's provider and physical
    model slug, backend-level (variant ``None`` — the resource-level
    identity is variant-less, so a variant-qualified cell would never
    apply). Values are backend-level statics from the reviewed table; a
    feature the table does not carry simply stays absent (fail closed).
    Called per configured resource at composition time — never per
    request, never from request content.
    """
    if provider != CODEX_PROVIDER:
        raise ValueError(
            "codex compatibility evidence: the codex worker-local adapter "
            + f"only evidences provider {CODEX_PROVIDER!r}, got {provider!r}"
        )
    cells: list[CompatibilityCell] = []
    for feature in COMPAT_FEATURES:
        entry = CODEX_WORKER_CELL_VALUES.get(feature)
        if entry is None:
            continue
        value, note, date = entry
        cells.append(
            CompatibilityCell(
                channel=CODEX_WORKER_CHANNEL,
                provider=provider,
                model=model,
                feature=feature,
                value=value,
                adapter=CODEX_WORKER_ADAPTER_NAME,
                adapter_version=CODEX_WORKER_ADAPTER_VERSION,
                evidence=codex_worker_cell_evidence(note, date=date),
            )
        )
    return tuple(cells)


__all__ = [
    "CODEX_WORKER_ADAPTER_NAME",
    "CODEX_WORKER_ADAPTER_VERSION",
    "CODEX_WORKER_CELL_VALUES",
    "codex_worker_cell_evidence",
    "default_codex_worker_cells",
]
