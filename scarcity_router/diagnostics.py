"""Shared diagnostics for the web UI and the ``doctor`` command (M09, #94).

One diagnostic implementation serves BOTH surfaces: the authenticated
``/control/diagnostics`` endpoint renders it for the administrator in the
web UI, and the additive ``scarcity-router doctor`` CLI command renders
the same report in a terminal. Neither surface re-implements any check.

Realizes the long-deferred ``doctor`` concept (D-016) for the gateway era
without touching any frozen surface.

Discipline:

- **No quota is ever consumed.** Diagnostics read configuration, the
  durable store, the registry's last observations and pairing state.
  They never call a collector, never dispatch an adapter and never issue
  a model request. A real generation test exists only as an explicit,
  separately-confirmed administrator action.
- **The resource ladder.** Every configured resource is reported through
  the closed acceptance ladder of issue #94: ``detected``,
  ``authenticated``, ``protocol_compatible``, ``available``, ``eligible``
  and ``promotion_confirmed`` — each stage strictly a superset of the
  previous one, each computed only from data the server actually holds,
  and the first unmet stage carrying a concrete remediation step.
- **Redacted by construction.** Reports contain configuration facts,
  states, counts and safe identifiers only — never provider credentials,
  key material or hashes, session tokens, prompt/response content or raw
  provider payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .gateway_validation import v_safe_id
from .resource_state import (
    EXECUTION_CHANNELS,
    PromotionObservation,
    RegistrySnapshot,
    ResourceIdentity,
    ResourceRegistryEntry,
)
from .routing_core import AdministratorConstraints
from .server_config import ProviderEndpointConfig, ResourceConfig, ServerConfiguration

# ── Closed vocabularies ───────────────────────────────────────────────────────

CHECK_STATES: frozenset[str] = frozenset({"ok", "warning", "error", "unknown"})

LADDER_STAGES: tuple[str, ...] = (
    "detected",
    "authenticated",
    "protocol_compatible",
    "available",
    "eligible",
    "promotion_confirmed",
)

_DIAGNOSTICS_SCHEMA_VERSION = 1


# ── Report records ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiagnosticCheck:
    """One named diagnostic check with its state and safe detail."""

    check_id: str
    title: str
    state: str
    detail: str
    remediation: str | None = None

    def __post_init__(self) -> None:
        _ = v_safe_id(self.check_id, "diagnostic_check.check_id")
        if self.state not in CHECK_STATES:
            raise ValueError(
                f"diagnostic_check.check_id={self.check_id!r}: "
                + f"unknown state {self.state!r}"
            )
        if len(self.title) > 200 or len(self.detail) > 1000:
            raise ValueError("diagnostic_check: title/detail exceed bounded length")
        if self.remediation is not None and len(self.remediation) > 1000:
            raise ValueError("diagnostic_check: remediation exceeds bounded length")

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "check_id": self.check_id,
            "title": self.title,
            "state": self.state,
            "detail": self.detail,
        }
        if self.remediation is not None:
            out["remediation"] = self.remediation
        return out


@dataclass(frozen=True)
class ResourceLadder:
    """The per-resource acceptance ladder of one configured resource."""

    resource_id: str
    provider: str
    model: str
    channel: str
    enabled: bool
    detected: bool
    authenticated: bool
    protocol_compatible: bool
    available: bool
    eligible: bool
    promotion_confirmed: bool
    first_blocked_stage: str | None
    remediation: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "provider": self.provider,
            "model": self.model,
            "channel": self.channel,
            "enabled": self.enabled,
            "detected": self.detected,
            "authenticated": self.authenticated,
            "protocol_compatible": self.protocol_compatible,
            "available": self.available,
            "eligible": self.eligible,
            "promotion_confirmed": self.promotion_confirmed,
            "first_blocked_stage": self.first_blocked_stage,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class DiagnosticsReport:
    """One complete redacted diagnostics snapshot (UI and doctor share it)."""

    generated_at: str
    mode: str
    checks: tuple[DiagnosticCheck, ...]
    resources: tuple[ResourceLadder, ...]
    workers: tuple[dict[str, object], ...]
    versions: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _DIAGNOSTICS_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "checks": [check.to_dict() for check in self.checks],
            "resources": [resource.to_dict() for resource in self.resources],
            "workers": list(self.workers),
            "versions": dict(self.versions),
        }


# ── Ladder computation ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LadderInputs:
    """Everything the ladder needs, all injectable for deterministic tests.

    ``channels_with_adapters`` names the execution channels for which a
    runnable execution adapter is configured (M04/M05/M06 supply these at
    runtime; M09 alone configures none, and the report says so honestly).
    ``endpoints_with_credentials`` carries the provider ids that HAVE a
    stored credential — a presence flag from the store, never the value.
    """

    configuration: ServerConfiguration
    registry_snapshot: RegistrySnapshot | None
    constraints: AdministratorConstraints
    paired_worker_ids: frozenset[str]
    channels_with_adapters: frozenset[str]
    endpoints_with_credentials: frozenset[str]
    now: datetime


def compute_resource_ladder(inputs: LadderInputs) -> tuple[ResourceLadder, ...]:
    """Compute the acceptance ladder for every configured resource.

    The ladder is computed purely from server-held facts; nothing here
    performs network I/O or consumes inference quota. ``promotion_confirmed``
    means a recorded promotion observation covers the evaluation instant —
    it stays an observation-level state and is never execution proof
    (D-039/D-042).
    """
    entries = _entries_by_id(inputs.registry_snapshot)
    ladders: list[ResourceLadder] = []
    for resource in inputs.configuration.resources:
        ladders.append(_ladder_for(resource, inputs, entries))
    return tuple(ladders)


def _entries_by_id(
    snapshot: RegistrySnapshot | None,
) -> dict[str, ResourceRegistryEntry]:
    if snapshot is None:
        return {}
    return {entry.identity.resource_id: entry for entry in snapshot.entries}


def _ladder_for(
    resource: ResourceConfig,
    inputs: LadderInputs,
    entries: dict[str, ResourceRegistryEntry],
) -> ResourceLadder:
    identity = resource.registration.identity
    resource_id = identity.resource_id

    detected = True
    authenticated = _is_authenticated(resource, inputs)
    protocol_compatible = (
        authenticated
        and identity.channel in inputs.channels_with_adapters
    )
    entry = entries.get(resource_id)
    available = (
        protocol_compatible
        and entry is not None
        and entry.observation is not None
        and entry.freshness == "fresh"
        and entry.observation.health.status == "ok"
    )
    eligible = (
        available
        and resource.enabled
        and _passes_administrator_constraints(identity, inputs.constraints)
    )
    promotion_confirmed = (
        eligible
        and entry is not None
        and entry.observation is not None
        and _promotion_covers_now(entry.observation.promotions, inputs.now)
    )

    blocked, remediation = _first_blocked(
        resource=resource,
        detected=detected,
        authenticated=authenticated,
        protocol_compatible=protocol_compatible,
        available=available,
        eligible=eligible,
        entry=entry,
    )
    return ResourceLadder(
        resource_id=resource_id,
        provider=identity.provider,
        model=identity.model,
        channel=identity.channel,
        enabled=resource.enabled,
        detected=detected,
        authenticated=authenticated,
        protocol_compatible=protocol_compatible,
        available=available,
        eligible=eligible,
        promotion_confirmed=promotion_confirmed,
        first_blocked_stage=blocked,
        remediation=remediation,
    )


def _is_authenticated(resource: ResourceConfig, inputs: LadderInputs) -> bool:
    identity = resource.registration.identity
    if identity.entitlement == "local_ungated":
        return True
    if identity.channel in ("worker_bridged", "local_app_adapter"):
        if resource.worker_id is None:
            return False
        return resource.worker_id in inputs.paired_worker_ids
    # Server-direct HTTP channels authenticate with a stored provider
    # credential (presence flag from the store, never the value).
    if resource.endpoint_id is None:
        return False
    return resource.endpoint_id in inputs.endpoints_with_credentials


def _passes_administrator_constraints(
    identity: ResourceIdentity, constraints: AdministratorConstraints
) -> bool:
    if identity.resource_id in constraints.blocked_resource_ids:
        return False
    if (
        constraints.allowed_providers is not None
        and identity.provider not in constraints.allowed_providers
    ):
        return False
    if (
        constraints.allowed_channels is not None
        and identity.channel not in constraints.allowed_channels
    ):
        return False
    if (
        constraints.allowed_entitlements is not None
        and identity.entitlement not in constraints.allowed_entitlements
    ):
        return False
    return True


def _promotion_covers_now(
    promotions: tuple[PromotionObservation, ...], now: datetime
) -> bool:
    """Whether any recorded promotion observation covers the evaluation instant.

    This stays an observation-level ladder state: a confirmed promotion
    record is never proof that a specific execution qualifies (D-039).
    """
    if not promotions:
        return False
    now_text = (
        now.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    for promotion in promotions:
        starts_ok = promotion.valid_from is None or promotion.valid_from <= now_text
        ends_ok = promotion.valid_until is None or promotion.valid_until > now_text
        if starts_ok and ends_ok:
            return True
    return False


def _first_blocked(
    *,
    resource: ResourceConfig,
    detected: bool,
    authenticated: bool,
    protocol_compatible: bool,
    available: bool,
    eligible: bool,
    entry: ResourceRegistryEntry | None,
) -> tuple[str | None, str | None]:
    identity = resource.registration.identity
    if not detected:  # pragma: no cover - defensive; configured == detected
        return "detected", "resource is not registered; re-apply configuration"
    if not resource.enabled:
        return (
            "detected",
            "resource is disabled: enable it in the resources page "
            + "(POST /control/resources/<id>/enabled with {\"enabled\": true})",
        )
    if not authenticated:
        if identity.channel in ("worker_bridged", "local_app_adapter"):
            return (
                "authenticated",
                "no paired worker for this resource: initiate pairing on the "
                + "workers page and complete it on the worker device",
            )
        if resource.endpoint_id is None:
            return (
                "authenticated",
                "no provider endpoint is bound: add a provider endpoint and "
                + "bind it to this resource",
            )
        return (
            "authenticated",
            "the bound provider endpoint has no stored credential: set the "
            + "provider credential (it is stored in the server's bounded "
            + "credential store and never displayed)",
        )
    if not protocol_compatible:
        if identity.channel not in EXECUTION_CHANNELS:  # pragma: no cover - validated
            return "protocol_compatible", "unknown execution channel"
        return (
            "protocol_compatible",
            "no execution adapter is configured for the "
            + f"'{identity.channel}' channel yet; configure the adapter for "
            + "this channel (generic HTTP adapters and worker transports "
            + "report readiness here once configured)",
        )
    if not available:
        if entry is None:
            return (
                "available",
                "no registry observation is available for this resource: "
                + "verify the registration",
            )
        if entry.observation is None:
            return (
                "available",
                "the resource has never been observed: the server probes "
                + "enabled server-direct resources at their polling cadence "
                + "(default 300 s) and records the connection test's probe; "
                + "run the connection test to probe now",
            )
        if entry.freshness != "fresh":
            return (
                "available",
                "the last observation is "
                + f"{entry.freshness}: the server re-probes at the resource's "
                + "polling cadence, or run the connection test to probe now",
            )
        return (
            "available",
            "last observed health is "
            + f"'{entry.observation.health.status}': address the "
            + "reported health state of the resource",
        )
    if not eligible:
        return (
            "eligible",
            "current administrator constraints exclude this resource: review "
            + "the administrator constraints (allowed providers, channels, "
            + "entitlements and blocked resources)",
        )
    return None, None


# ── Whole-report collection ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ServerDiagnosticsInputs:
    """Inputs for a server-mode report; all injectable for tests.

    ``store_view`` is a narrow read-only view of the durable store (the
    doctor CLI builds one from a path; the control plane passes its open
    store), so diagnostics never need more store access than these reads.
    """

    configuration: ServerConfiguration
    registry_snapshot: RegistrySnapshot | None
    constraints: AdministratorConstraints
    paired_worker_ids: frozenset[str]
    channels_with_adapters: frozenset[str]
    endpoints_with_credentials: frozenset[str]
    pairings: tuple[dict[str, object], ...]
    store_schema_version: int | None
    store_error: str | None
    admin_configured: bool | None
    active_client_keys: int | None
    revoked_client_keys: int | None
    now: datetime
    version: str
    provider_endpoints: tuple[ProviderEndpointConfig, ...] = ()


def collect_server_diagnostics(inputs: ServerDiagnosticsInputs) -> DiagnosticsReport:
    """The server-mode report used by the UI and by ``doctor`` for a server."""
    checks: list[DiagnosticCheck] = []
    if inputs.store_error is not None:
        checks.append(
            DiagnosticCheck(
                check_id="store",
                title="Durable store",
                state="error",
                detail="the server store could not be opened",
                remediation=inputs.store_error,
            )
        )
    elif inputs.store_schema_version is None:
        checks.append(
            DiagnosticCheck(
                check_id="store",
                title="Durable store",
                state="warning",
                detail="no server store found",
                remediation=(
                    "start the server once with a data directory "
                    + "(python -m scarcity_router.control_server --data-dir ...)"
                ),
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                check_id="store",
                title="Durable store",
                state="ok",
                detail=f"store schema version {inputs.store_schema_version} is current",
            )
        )

    if inputs.admin_configured is True:
        checks.append(
            DiagnosticCheck(
                check_id="admin_identity",
                title="Administrator identity",
                state="ok",
                detail="an administrator credential is configured",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                check_id="admin_identity",
                title="Administrator identity",
                state="warning" if inputs.admin_configured is False else "unknown",
                detail="no administrator credential is configured",
                remediation=(
                    "complete first-run onboarding (open the server UI or "
                    + "POST /control/bootstrap/admin once)"
                ),
            )
        )

    checks.append(
        DiagnosticCheck(
            check_id="configuration",
            title="Server configuration",
            state="ok",
            detail=(
                f"{len(inputs.configuration.providers)} provider endpoint(s), "
                + f"{len(inputs.configuration.resources)} resource(s), "
                + f"{len(inputs.configuration.aliases)} routing alias(es) "
                + "validated (fail-closed parse)"
            ),
        )
    )

    if inputs.active_client_keys is not None:
        checks.append(
            DiagnosticCheck(
                check_id="client_keys",
                title="Inference client keys",
                state="ok",
                detail=(
                    f"{inputs.active_client_keys} active, "
                    + f"{inputs.revoked_client_keys or 0} revoked "
                    + "(stored as SHA-256 hashes only)"
                ),
            )
        )

    checks.append(
        DiagnosticCheck(
            check_id="quota_safety",
            title="Quota safety",
            state="ok",
            detail=(
                "diagnostics read stored state only; no collector call, "
                + "adapter dispatch or inference request was made"
            ),
        )
    )

    report_workers = tuple(inputs.pairings)
    if not report_workers:
        checks.append(
            DiagnosticCheck(
                check_id="workers",
                title="Workers",
                state="ok",
                detail="no workers are configured (server-direct operation only)",
            )
        )

    ladders = compute_resource_ladder(
        LadderInputs(
            configuration=inputs.configuration,
            registry_snapshot=inputs.registry_snapshot,
            constraints=inputs.constraints,
            paired_worker_ids=inputs.paired_worker_ids,
            channels_with_adapters=inputs.channels_with_adapters,
            endpoints_with_credentials=inputs.endpoints_with_credentials,
            now=inputs.now,
        )
    )
    return DiagnosticsReport(
        generated_at=_canonical(inputs.now),
        mode="server",
        checks=tuple(checks),
        resources=ladders,
        workers=report_workers,
        versions={
            "scarcity_router": inputs.version,
            "server_store_schema": inputs.store_schema_version,
            "diagnostics_schema": _DIAGNOSTICS_SCHEMA_VERSION,
        },
    )


def collect_local_diagnostics(
    *,
    now: datetime,
    version: str,
    artifact_error: str | None,
    policy_error: str | None,
    policy_configured: bool,
) -> DiagnosticsReport:
    """The recommendation-only report for ``doctor`` without a server.

    Checks the local artifacts and the D-036 default user configuration.
    No collector is invoked: provider telemetry checks would need live
    collection and are deliberately not part of doctor's offline report.
    """
    checks: list[DiagnosticCheck] = []
    if artifact_error is None:
        checks.append(
            DiagnosticCheck(
                check_id="artifacts",
                title="Catalog and policy artifacts",
                state="ok",
                detail="the packaged catalog and task-profile artifacts load",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                check_id="artifacts",
                title="Catalog and policy artifacts",
                state="error",
                detail="the catalog/policy artifacts failed to load",
                remediation=artifact_error,
            )
        )
    if policy_error is None:
        checks.append(
            DiagnosticCheck(
                check_id="user_policy",
                title="Default user configuration",
                state="ok",
                detail=(
                    "the default user selector policy is configured"
                    if policy_configured
                    else "no default user policy file exists (the documented "
                    + "neutral policy applies; run "
                    + "'scarcity-router install-config' to provision one)"
                ),
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                check_id="user_policy",
                title="Default user configuration",
                state="error",
                detail="the default user policy file exists but fails to load",
                remediation=policy_error,
            )
        )
    checks.append(
        DiagnosticCheck(
            check_id="server_mode",
            title="Execution-gateway server",
            state="unknown",
            detail="no server data directory was given; server diagnostics skipped",
            remediation=(
                "pass --server-data-dir to diagnose a deployed server's "
                + "store, configuration, resources and workers"
            ),
        )
    )
    return DiagnosticsReport(
        generated_at=_canonical(now),
        mode="local",
        checks=tuple(checks),
        resources=(),
        workers=(),
        versions={
            "scarcity_router": version,
            "diagnostics_schema": _DIAGNOSTICS_SCHEMA_VERSION,
        },
    )


# ── Rendering (shared by UI and CLI) ──────────────────────────────────────────


def render_report_human(report: DiagnosticsReport) -> str:
    """Deterministic plain-text rendering for the ``doctor`` command."""
    lines: list[str] = [
        f"doctor report ({report.mode} mode) at {report.generated_at}",
    ]
    for check in report.checks:
        marker = {"ok": "[ok]", "warning": "[warn]", "error": "[fail]"}.get(
            check.state, "[ ? ]"
        )
        lines.append(f"{marker} {check.title}: {check.detail}")
        if check.remediation:
            lines.append(f"       remediation: {check.remediation}")
    for worker in report.workers:
        connection = (
            "connected"
            if worker.get("connected") is True
            else "not connected"
            if "connected" in worker
            else "connection state unknown (server not running)"
        )
        lines.append(
            f"[info] worker {worker.get('worker_id', '?')} "
            + f"({worker.get('label', '')}): {worker.get('status', '?')}"
            + f" [{connection}]"
        )
    for resource in report.resources:
        reached = [
            stage
            for stage, value in (
                ("detected", resource.detected),
                ("authenticated", resource.authenticated),
                ("protocol_compatible", resource.protocol_compatible),
                ("available", resource.available),
                ("eligible", resource.eligible),
                ("promotion_confirmed", resource.promotion_confirmed),
            )
            if value
        ]
        state = "eligible" if resource.eligible else (
            f"blocked at {resource.first_blocked_stage}"
            if resource.first_blocked_stage
            else "unknown"
        )
        lines.append(
            f"[info] resource {resource.resource_id} "
            + f"({resource.provider}/{resource.model} via {resource.channel}) "
            + f"{state}; stages passed: {', '.join(reached) or 'none'}"
        )
        if resource.remediation:
            lines.append(f"       remediation: {resource.remediation}")
    return "\n".join(lines) + "\n"


def _canonical(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CHECK_STATES",
    "LADDER_STAGES",
    "DiagnosticCheck",
    "DiagnosticsReport",
    "LadderInputs",
    "ResourceLadder",
    "ServerDiagnosticsInputs",
    "collect_local_diagnostics",
    "collect_server_diagnostics",
    "compute_resource_ladder",
    "render_report_human",
]
