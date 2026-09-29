"""Dated ZCode worker-local adapter compatibility-matrix cells (M07, D-063).

The D-043 compatibility-matrix contribution of the M07 ZCode execution
adapter, from the dated D-061 evidence record
(``docs/zcode-adapter-stage2-evidence.md``, CLI v3.14.3 / bundle 0.16.9,
retrieved/probed 2026-09-28) plus the remediation review evidence for
the permission-mode semantics (official source ``permission/service.ts``
at the pinned commit ``29628c9`` and the identical shipped bundle:
``--mode edit`` explicitly allows workspace-scoped file-edit tools,
``mode.edit.fileEdit``; every other side-effecting tool still requires
approval and a headless run cannot answer, so the vendor's default
broker denies it — fail closed). This module is a STATIC, module-level
table — support is never derived at runtime, never inferred from a
parser "happening to work", and never read from request content.
Composed deployments (``server_composition``) key these cells to each
adopted plan-managed lane resource.

Conservative by design: the lane's honest surface is ONE prompt against
an authorized workspace with structured progress events and a terminal
result. ``streaming`` (no evidenced incremental text shape),
``tool_calls``/``tool_results`` (client tools return to clients, D-043;
the adapter rejects them before execution), ``structured_output`` and
``reasoning_controls`` (no supported control exists on the evidenced
headless surface) are ``UNSUPPORTED``. ``usage_reporting`` is absent
entirely (the terminal result's optional usage member has no evidenced
internal field names — nothing is mapped, and an absent cell fails
closed for any request that requires the feature). ``UNKNOWN`` and
``UNSUPPORTED`` fail closed at admission.

Authority model (issue #106): this built-in evidence is the DEFAULT
ceiling for the ZCode worker-local adapter. Nothing here lets
configuration raise a cell above it; administrator narrowing or
elevation with the administrator's own dated evidence remains issue #106
territory and is deliberately not implemented here.
"""

from __future__ import annotations

from collections.abc import Mapping

from .routing_core import COMPAT_FEATURES, CompatibilityCell
from .selection_types import EvidenceRef
from .worker_zcode_adapter import ZCODE_PROVIDER

#: The worker_bridged channel's ZCode adapter identity (D-043 key): the
#: adapter allowlist id is ``zcode``; the matrix ``adapter`` field names
#: the worker-local adapter implementation with its own version.
ZCODE_WORKER_CHANNEL = "worker_bridged"
ZCODE_WORKER_ADAPTER_NAME = "zcode-worker-local"
ZCODE_WORKER_ADAPTER_VERSION = "1.1.0"

#: The evidenced CLI generation and the evidence record (D-061).
_ZCODE_TESTED_VERSION = "zcode 0.16.9 (release v3.14.3, commit 29628c9)"
_EVIDENCE_DATE = "2026-09-28"

#: feature -> (cell value, cell-specific evidence note). Values are the
#: RECORDED evidence — nothing upgraded, nothing derived. The
#: ``usage_reporting`` feature is deliberately ABSENT (no evidenced
#: mapping), which fails closed for any request requiring it.
ZCODE_WORKER_CELL_VALUES: Mapping[str, tuple[str, str]] = {
    "roles_history": (
        "PARTIAL",
        "exactly one non-empty user message maps to the documented "
        + "--prompt data argument; system/developer/history/tool shapes "
        + "are typed rejections BEFORE execution (refuse-not-drop)",
    ),
    "streaming": (
        "UNSUPPORTED",
        "stream-json carries progress events only; the single evidenced "
        + "answer surface is the terminal type:\"result\" line — no "
        + "incremental text shape is evidenced, none is synthesized",
    ),
    "tool_calls": (
        "UNSUPPORTED",
        "client tools return to clients (D-043); the adapter rejects "
        + "tool-bearing requests before any execution; ZCode's internal "
        + "tools run under the explicit --mode edit boundary, never as "
        + "client tool calls",
    ),
    "tool_results": (
        "UNSUPPORTED",
        "tool-result round trips are not representable on the "
        + "single-prompt surface; typed rejection",
    ),
    "structured_output": (
        "UNSUPPORTED",
        "no structured-output control exists on the evidenced headless "
        + "surface; response_format other than text is a typed rejection",
    ),
    "reasoning_controls": (
        "UNSUPPORTED",
        "no supported headless model or effort steering exists (D-061 "
        + "evidence); effort-bearing requests are typed rejections, "
        + "never silently substituted",
    ),
    "context_limits": (
        "PARTIAL",
        "no context ceiling is published on any supported surface "
        + "(UNKNOWN stays UNKNOWN); an over-limit run fails as a typed "
        + "CLI failure, never a fabricated completion",
    ),
    "error_semantics": (
        "PARTIAL",
        "completion requires the terminal result line AND exit 0; every "
        + "other ending is a typed failure or an explicit unknown "
        + "observation; stderr is counted, never read",
    ),
    "cancellation": (
        "PARTIAL",
        "cancel or deadline → bounded process-group terminate, cancelled "
        + "result, never completed after confirmed cancellation; "
        + "deterministic fake-CLI verified, SIGTERM bounded per the "
        + "D-061 live spike",
    ),
}


def zcode_worker_cell_evidence(note: str) -> EvidenceRef:
    """One dated provenance reference for a ZCode cell (bounded)."""
    identifier = (
        "docs/zcode-adapter-stage2-evidence.md + D-063 "
        + f"({_ZCODE_TESTED_VERSION}) | {note}"
    )
    if len(identifier) > 512:
        raise ValueError(
            "zcode compatibility evidence: cell note exceeds the bounded "
            + "identifier length"
        )
    return EvidenceRef(
        source="zcode_adapter_stage2",
        identifier=identifier,
        date=_EVIDENCE_DATE,
    )


def default_zcode_worker_cells(
    *, provider: str, model: str
) -> tuple[CompatibilityCell, ...]:
    """The reviewed M07 cells for one plan-managed lane resource.

    Keyed the way the routing core looks cells up: channel
    ``worker_bridged``, the lane's provider and descriptor slug,
    backend-level (variant ``None`` — the lane resource is variant-less,
    so a variant-qualified cell would never apply). Values are
    backend-level statics from the reviewed table; a feature the table
    does not carry simply stays absent (fail closed).
    """
    if provider != ZCODE_PROVIDER:
        raise ValueError(
            "zcode compatibility evidence: the zcode worker-local adapter "
            + f"only evidences provider {ZCODE_PROVIDER!r}, got {provider!r}"
        )
    cells: list[CompatibilityCell] = []
    for feature in COMPAT_FEATURES:
        entry = ZCODE_WORKER_CELL_VALUES.get(feature)
        if entry is None:
            continue
        value, note = entry
        cells.append(
            CompatibilityCell(
                channel=ZCODE_WORKER_CHANNEL,
                provider=provider,
                model=model,
                feature=feature,
                value=value,
                adapter=ZCODE_WORKER_ADAPTER_NAME,
                adapter_version=ZCODE_WORKER_ADAPTER_VERSION,
                evidence=zcode_worker_cell_evidence(note),
            )
        )
    return tuple(cells)


__all__ = [
    "ZCODE_WORKER_ADAPTER_NAME",
    "ZCODE_WORKER_ADAPTER_VERSION",
    "ZCODE_WORKER_CELL_VALUES",
    "default_zcode_worker_cells",
    "zcode_worker_cell_evidence",
]
