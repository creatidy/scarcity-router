"""One-process composition helpers: configuration → execution adapters.

The integration seam between the three merged workstreams (M04 adapters,
M05 worker transport, M09 configuration): the administrator's typed
:class:`~scarcity_router.server_config.ServerConfiguration` is the ONLY
source from which execution adapters are built. Provider origins and
credentials never come from client requests (D-044); a configuration
that references a preset the program does not evidence, an unparseable
origin, or a worker that does not exist FAILS CLOSED at composition
time instead of half-configuring an adapter.

What this module deliberately does NOT do: no routing, no selection, no
admission, no retries, no provider parsing. It only turns validated
administrator configuration into the M03
:class:`~scarcity_router.gateway_adapters.AdapterRegistry` entries the
coordinator already knows how to resolve — or registers nothing, which
is the honest empty default deployment (no adapters, no worker
listener).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .codex_worker_evidence import default_codex_worker_cells
from .gateway_adapters import AdapterRegistry
from .providers.http_origin import ProviderCredential, ProviderOrigin
from .providers.openai_http_adapter import OpenAICompatibleHttpAdapter, ResourceBinding
from .providers.openai_http_evidence import default_cells_for
from .providers.openai_http_presets import preset_by_id
from .routing_core import CompatibilityCell
from .server_config import ProviderEndpointConfig, ServerConfiguration
from .worker_bridged_adapter import WorkerBridgedAdapter
from .worker_codex_adapter import CODEX_ADAPTER_ID
from .worker_endpoint import WorkerEndpoint

#: The server-direct channel's adapter id family (M04 presets). A provider
#: endpoint's ``adapter_id`` must resolve to one of these presets.
SUPPORTED_ENDPOINT_ADAPTER_IDS: tuple[str, ...] = (
    "openai-api",
    "deepseek",
    "openrouter",
    "zai-coding-plan",
    "ollama",
    "generic-openai",
)

ProviderSecretReader = Callable[[str], "str | None"]


class CompositionError(ValueError):
    """Administrator configuration cannot be composed into adapters.

    Raised fail-closed with a remediation-bearing message; callers map it
    onto the control surface's client-classifiable configuration error.
    """


def _canonical_cells(
    cells: Iterable[CompatibilityCell],
) -> tuple[CompatibilityCell, ...]:
    """Canonicalize per-resource cell emission into the D-043 matrix.

    The frozen D-043 key is ``(channel, provider, model, variant,
    feature)`` — ``resource_id`` is deliberately NOT part of it (D-042):
    multiple resources may execute the same physical model, differing in
    worker, entitlement, pool or availability, and compatibility is
    backend/surface evidence, not resource ownership. The per-resource
    emission is therefore folded onto exactly one cell per key:

    - the first occurrence wins the slot;
    - a later cell with the same key is silently merged when it is
      semantically identical — same value, same adapter, same adapter
      version and the same evidence (the frozen dataclasses' value
      equality over the exact ``EvidenceRef`` fields: source,
      identifier, version, date);
    - otherwise the composition FAILS CLOSED with
      :class:`CompositionError`: the current D-043 matrix cannot
      represent conflicting compatibility evidence for the same backend
      key, so no first/last winner is chosen and the administrator must
      correct the configuration or the evidence source.

    The result is deterministically ordered by the key tuple, with an
    absent variant sorting first — exactly the routing core's own
    ordering of a request's cell tuple.
    """
    cells_by_key: dict[tuple[str, str, str, str | None, str], CompatibilityCell] = {}
    for cell in cells:
        key = (cell.channel, cell.provider, cell.model, cell.variant, cell.feature)
        existing = cells_by_key.get(key)
        if existing is None:
            cells_by_key[key] = cell
            continue
        if (
            existing.value == cell.value
            and existing.adapter == cell.adapter
            and existing.adapter_version == cell.adapter_version
            and existing.evidence == cell.evidence
        ):
            continue
        raise CompositionError(
            "the current D-043 compatibility matrix cannot represent "
            + "conflicting compatibility evidence for the same backend key "
            + f"{key}: one source asserts {existing.value!r} via adapter "
            + f"{existing.adapter!r} (evidence "
            + f"{existing.evidence.identifier!r}), another asserts "
            + f"{cell.value!r} via adapter {cell.adapter!r} (evidence "
            + f"{cell.evidence.identifier!r}); correct the configuration so "
            + "this backend key carries exactly one evidence source"
        )
    return tuple(
        cells_by_key[key]
        for key in sorted(
            cells_by_key,
            key=lambda parts: (parts[0], parts[1], parts[2], parts[3] or "", parts[4]),
        )
    )


def validate_execution_configuration(
    configuration: ServerConfiguration,
    *,
    worker_id_exists: Callable[[str], bool],
) -> None:
    """Fail closed unless every channel binding is composable.

    Checks exactly the fields the execution adapters consume:

    - a provider endpoint's ``adapter_id`` must resolve to an
      evidence-backed preset (:func:`preset_by_id`);
    - its ``base_url`` must parse as an exact
      :class:`~scarcity_router.providers.http_origin.ProviderOrigin`
      (verified HTTPS, or plain HTTP for explicit loopback only);
    - a resource's ``endpoint_id`` reference must exist (validated by the
      configuration model itself) and its ``worker_id`` reference must
      name a known worker device in the ONE pairing system (the M05
      identity store);
    - a ``worker_bridged`` resource's optional ``local_adapter_id`` must
      be a safe identifier (the worker enforces its own allowlist at
      dispatch; the server only needs to name one).
    """
    for provider in configuration.providers:
        _validate_provider_endpoint(provider)
    for resource in configuration.resources:
        resource_id = resource.registration.identity.resource_id
        if resource.worker_id is not None and not worker_id_exists(resource.worker_id):
            raise CompositionError(
                f"resource {resource_id!r} references worker "
                + f"{resource.worker_id!r}, which has no identity in the "
                + "worker pairing store; pair the worker first (workers page)"
            )
        _ = resource_id


def _validate_provider_endpoint(provider: ProviderEndpointConfig) -> None:
    if preset_by_id(provider.adapter_id) is None:
        raise CompositionError(
            f"provider {provider.provider_id!r} names adapter "
            + f"{provider.adapter_id!r}, which is not an evidence-backed "
            + "preset; use one of: " + ", ".join(SUPPORTED_ENDPOINT_ADAPTER_IDS)
        )
    try:
        _ = ProviderOrigin.parse(provider.base_url)
    except ValueError as exc:
        raise CompositionError(
            f"provider {provider.provider_id!r} base_url is not a usable "
            + f"origin: {exc}"
        ) from None


def build_resource_bindings(
    configuration: ServerConfiguration,
    *,
    provider_secret_reader: ProviderSecretReader,
) -> dict[str, ResourceBinding]:
    """Build the M04 resource bindings from administrator configuration.

    One binding per ``server_direct_http`` resource whose endpoint
    resolves: preset from the endpoint's ``adapter_id``, exact origin
    from its ``base_url``, and the credential — when one is stored —
    read through the store's dispatch-only reader (never from request
    content, never logged, never returned). A preset that requires a
    credential with none stored leaves the resource UNBOUND: it is the
    honest "unconfigured" state — diagnostics report it with a
    remediation and dispatch refuses it definitively — never a crash and
    never a fabricated green light.
    """
    bindings: dict[str, ResourceBinding] = {}
    for resource in configuration.resources:
        identity = resource.registration.identity
        if identity.channel != "server_direct_http":
            continue
        if resource.endpoint_id is None:
            continue
        provider = configuration.provider_by_id(resource.endpoint_id)
        if provider is None:  # pragma: no cover - validated at save time
            continue
        preset = preset_by_id(provider.adapter_id)
        if preset is None:  # pragma: no cover - validated at save time
            continue
        origin = ProviderOrigin.parse(provider.base_url)
        secret = provider_secret_reader(provider.provider_id)
        if secret is None and preset.policy.requires_credential:
            # Fail closed honestly: no credential, no binding.
            continue
        credential = ProviderCredential(secret) if secret is not None else None
        bindings[identity.resource_id] = ResourceBinding(
            resource_id=identity.resource_id,
            preset=preset,
            origin=origin,
            credential=credential,
        )
    return bindings


def build_adapter_registry(
    configuration: ServerConfiguration,
    *,
    provider_secret_reader: ProviderSecretReader,
    worker_endpoint: WorkerEndpoint,
) -> AdapterRegistry:
    """Compose the channel-keyed adapter registry from configuration.

    Registers the M04 ``server_direct_http`` adapter when at least one
    configured resource has a resolvable provider binding, and the M05
    ``worker_bridged`` adapter when at least one configured resource is
    bound to a worker with an administrator-declared worker-local
    adapter id. A configuration that needs neither registers nothing —
    the honest empty deployment (the coordinator then answers
    ``adapter_unavailable`` instead of pretending).
    """
    bindings = build_resource_bindings(
        configuration, provider_secret_reader=provider_secret_reader
    )
    worker_adapter_map: dict[str, str] = {}
    for resource in configuration.resources:
        if resource.registration.identity.channel != "worker_bridged":
            continue
        # BOTH dimensions must be administrator-configured: the owning
        # worker AND the worker-local adapter. A resource missing either
        # is never dispatchable — ownership is never invented, and
        # telemetry cannot fill either dimension (D-049 amendment).
        if resource.worker_id is None or resource.local_adapter_id is None:
            continue
        worker_adapter_map[resource.registration.identity.resource_id] = (
            resource.local_adapter_id
        )
    registry = AdapterRegistry()
    if bindings:
        registry.register(OpenAICompatibleHttpAdapter(bindings))
    if worker_adapter_map:
        registry.register(
            WorkerBridgedAdapter(
                worker_endpoint,
                resource_adapter_map=dict(worker_adapter_map),
            )
        )
    return registry


def build_compatibility_cells(
    configuration: ServerConfiguration,
    *,
    provider_secret_reader: ProviderSecretReader,
) -> tuple[CompatibilityCell, ...]:
    """Build the production compatibility matrix from administrator
    configuration and the existing evidence modules.

    Exactly the resources the adapter registry can actually serve get
    cells — no more, no less:

    - every ``server_direct_http`` resource with a resolvable binding
      (the SAME binding set :func:`build_resource_bindings` produces)
      receives the M04 preset's evidenced default cells
      (:func:`~scarcity_router.providers.openai_http_evidence.default_cells_for`)
      keyed to the resource's ``(provider, model)`` — no duplicated
      tables, and the un-evidenced ``generic`` preset stays all-UNKNOWN;
    - every ``worker_bridged`` resource the composition would dispatch
      to the Codex worker-local adapter (``worker_id`` AND
      ``local_adapter_id == "codex"`` configured, provider ``openai``)
      receives the reviewed M06 evidence cells
      (:func:`~scarcity_router.codex_worker_evidence.default_codex_worker_cells`)
      keyed to that resource's physical model slug.

    Because the matrix is keyed by the frozen D-043 backend key and never
    by ``resource_id`` (D-042: several resources may execute the same
    physical model), the per-resource emission is canonicalized onto one
    cell per key (see :func:`_canonical_cells`): two resources serving
    the same backend through the same evidence contribute ONE set of
    cells, while conflicting assertions for one key fail closed with
    :class:`CompositionError`.

    Cells are configuration-derived statics: nothing here derives
    support at runtime or from request content, and nothing elevates a
    cell above the built-in evidence (administrator narrowing/elevation
    remains issue #106 territory). The tuple is rebuilt whenever the
    application is re-composed from configuration, so a configuration
    change replaces the matrix wholesale.
    """
    bindings = build_resource_bindings(
        configuration, provider_secret_reader=provider_secret_reader
    )
    cells: list[CompatibilityCell] = []
    for resource in configuration.resources:
        identity = resource.registration.identity
        binding = bindings.get(identity.resource_id)
        if binding is not None:
            cells.extend(
                default_cells_for(
                    binding.preset, provider=identity.provider, model=identity.model
                )
            )
            continue
        if identity.channel != "worker_bridged":
            continue
        if resource.worker_id is None or resource.local_adapter_id != CODEX_ADAPTER_ID:
            continue
        if identity.provider != "openai":
            # The Codex adapter's reviewed evidence speaks only for the
            # OpenAI provider; anything else simply gets no cells (fail
            # closed) — evidence is never transplanted.
            continue
        cells.extend(
            default_codex_worker_cells(
                provider=identity.provider, model=identity.model
            )
        )
    return _canonical_cells(cells)
