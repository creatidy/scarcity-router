"""The server component's administrator configuration model (M09, #94).

One typed, fail-closed configuration document is the SINGLE source of
truth for every administrator-configured domain of the server component:
provider endpoints, execution resources (with their enable state and
channel bindings), routing-profile aliases (D-042 layer 4), per-client
authorization grants, administrator routing constraints, admission
limits, pairing/session/audit bounds. The document lives in exactly one
place — the durable store's ``server_configuration`` row — and every
server surface (control API, web UI, doctor) reads it from there.

Discipline:

- **No secrets, ever.** Provider credentials are NOT part of this
  document; they live only in the store's dedicated bounded table
  (D-044) and are referenced by provider id. The document is therefore
  secret-free by construction, which is what makes export, support
  bundles and diagnostics safe without filtering.
- **Neutral default.** The neutral configuration is empty in every
  preference-bearing domain: no providers, no resources, no aliases, no
  author-private schedules, campaigns, provider or model preferences are
  ever shipped as defaults (issue #94 non-goal).
- **Fail closed.** Parsing rejects unknown keys, wrong types, unsafe
  identifiers and unknown enum values; a document that does not parse is
  a hard error, never a partial load.
- **Administrator-owned.** Nothing in a client request can write any
  field of this document (D-042); mutations arrive only through the
  authenticated administrator control surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit

from .gateway_contracts import GatewayLimits
from .gateway_validation import (
    exact_shape,
    v_bool,
    v_int,
    v_safe_id,
    v_str,
    v_str_object_mapping,
)
from .resource_state import ResourceRegistration
from .routing_core import (
    AdministratorConstraints,
    ClientAuthorization,
    ClientRoutingProfile,
)

#: The configuration document's own version (server-internal, like the
#: store schema; changes only through an explicit migration).
#: Version 2 (D-053) adds the additive ``sources`` domain; a version-1
#: document remains valid and parses as zero sources.
CONFIG_SCHEMA_VERSION = 2

#: Closed execution-source kinds (D-053). ``codex_subscription`` is the
#: first evidenced kind; new kinds arrive only through an explicit
#: decision with their own provider/adapter evidence.
SOURCE_KINDS: tuple[str, ...] = ("codex_subscription",)

#: The provider each source kind executes through.
_SOURCE_KIND_PROVIDER: dict[str, str] = {"codex_subscription": "openai"}

#: Upper bound for source ids: the derived resource id
#: ``<source_id>:<slug>`` must stay inside the safe-id contract.
SOURCE_ID_MAX_LENGTH = 20

#: Configuration document versions this build parses: v2 (current) and
#: v1 (pre-sources documents remain valid and mean zero sources).
_SUPPORTED_CONFIG_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

_DEFAULT_PAIRING_CODE_TTL_SECONDS = 900
_DEFAULT_SESSION_TTL_SECONDS = 8 * 3600
_DEFAULT_AUDIT_MAX_RECORDS = 10_000
_DEFAULT_AUDIT_MAX_AGE_SECONDS = 30 * 24 * 3600
#: Default readiness-probe cadence for server-direct HTTP resources when
#: the administrator does not set one (server-side policy is
#: authoritative, M01/U-003): the server observes each such resource at
#: most every five minutes, and never more often than its polling
#: cadence requires.
SERVER_DIRECT_DEFAULT_POLL_INTERVAL_SECONDS = 300


class ServerConfigError(ValueError):
    """Raised when an administrator configuration document fails validation."""


@dataclass(frozen=True)
class ProviderEndpointConfig:
    """One administrator-configured provider endpoint (no credential).

    ``adapter_id`` names the execution adapter preset that serves this
    endpoint (the generic OpenAI-compatible HTTP adapter family is M04
    work; this model only carries the typed configuration M04 consumes).
    ``base_url`` must be verified-HTTPS for non-loopback origins; plain
    HTTP is accepted only for explicit loopback origins (the bounded
    D-044 exception). The credential, if any, is referenced implicitly by
    ``provider_id`` and never appears here.
    """

    provider_id: str
    adapter_id: str
    base_url: str
    label: str = ""

    def __post_init__(self) -> None:
        _ = v_safe_id(self.provider_id, "provider.provider_id")
        _ = v_safe_id(self.adapter_id, "provider.adapter_id")
        _validate_endpoint_origin(self.base_url, "provider.base_url")
        if len(self.label) > 200:
            raise ServerConfigError("provider.label exceeds 200 characters")

    @classmethod
    def from_dict(cls, d: object) -> "ProviderEndpointConfig":
        dd = exact_shape(
            d,
            ("provider_id", "adapter_id", "base_url"),
            ("label",),
            "provider_endpoint",
        )
        return cls(
            provider_id=v_str(dd["provider_id"], "provider.provider_id"),
            adapter_id=v_str(dd["adapter_id"], "provider.adapter_id"),
            base_url=v_str(dd["base_url"], "provider.base_url"),
            label=(v_str(dd["label"], "provider.label") if "label" in dd else ""),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "base_url": self.base_url,
        }
        if self.label:
            out["label"] = self.label
        return out


def _validate_endpoint_origin(base_url: object, field: str) -> None:
    """Scheme/host discipline for administrator-configured provider origins."""
    text = v_str(base_url, field)
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ServerConfigError(f"{field}: not a valid URL") from exc
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not hostname:
        raise ServerConfigError(
            f"{field}: must be an absolute http(s) URL with a hostname"
        )
    if parts.username is not None or parts.password is not None:
        raise ServerConfigError(
            f"{field}: credentials never travel in URLs (D-044)"
        )
    if parts.query or parts.fragment:
        raise ServerConfigError(f"{field}: query and fragment are not allowed")
    if scheme == "http" and hostname not in _LOOPBACK_HOSTS:
        raise ServerConfigError(
            f"{field}: plain HTTP is permitted only for explicit loopback "
            + "origins; non-local provider endpoints require https"
        )


@dataclass(frozen=True)
class ResourceConfig:
    """One execution resource's administrator configuration.

    ``registration`` is the M01 :class:`ResourceRegistration` (validated
    by its own contract). ``enabled`` is M09's operational switch: a
    disabled resource stays configured but is never registered into the
    routing registry. ``endpoint_id``/``worker_id`` are the channel
    bindings consumed by the execution path (M04 adapter / M05 worker
    transport respectively); both are optional so a resource can be
    reviewed and prepared before its channel exists.
    ``local_adapter_id`` names the worker-local adapter (the worker's own
    allowlist key, e.g. ``ollama``) that a ``worker_bridged`` resource's
    executions are dispatched to; the worker still enforces its own
    allowlist against the name at dispatch, so this can only ever name an
    adapter the worker itself configured.
    """

    registration: ResourceRegistration
    enabled: bool = True
    endpoint_id: str | None = None
    worker_id: str | None = None
    local_adapter_id: str | None = None

    @classmethod
    def from_dict(cls, d: object) -> "ResourceConfig":
        dd = exact_shape(
            d,
            ("registration",),
            ("enabled", "endpoint_id", "worker_id", "local_adapter_id"),
            "resource_config",
        )
        registration_document = v_str_object_mapping(
            dd["registration"], "resource_config.registration"
        )
        registration = _registration_from_document(registration_document)
        endpoint_id = (
            v_safe_id(dd["endpoint_id"], "resource_config.endpoint_id")
            if "endpoint_id" in dd
            else None
        )
        worker_id = (
            v_safe_id(dd["worker_id"], "resource_config.worker_id")
            if "worker_id" in dd
            else None
        )
        local_adapter_id = (
            v_safe_id(dd["local_adapter_id"], "resource_config.local_adapter_id")
            if "local_adapter_id" in dd
            else None
        )
        return cls(
            registration=registration,
            enabled=(v_bool(dd["enabled"], "resource_config.enabled") if "enabled" in dd else True),
            endpoint_id=endpoint_id,
            worker_id=worker_id,
            local_adapter_id=local_adapter_id,
        )

    def to_dict(self) -> dict[str, object]:
        registration = self.registration
        document: dict[str, object] = {
            "identity": registration.identity.to_dict(),
            "freshness_ttl_seconds": registration.freshness_ttl_seconds,
        }
        if registration.poll_interval_seconds is not None:
            document["poll_interval_seconds"] = registration.poll_interval_seconds
        if registration.capabilities.to_dict():
            document["capabilities"] = registration.capabilities.to_dict()
        if registration.cost is not None:
            document["cost"] = registration.cost.to_dict()
        out: dict[str, object] = {"registration": document, "enabled": self.enabled}
        if self.endpoint_id is not None:
            out["endpoint_id"] = self.endpoint_id
        if self.worker_id is not None:
            out["worker_id"] = self.worker_id
        if self.local_adapter_id is not None:
            out["local_adapter_id"] = self.local_adapter_id
        return out


def _registration_from_document(d: object) -> ResourceRegistration:
    """Rebuild one ``ResourceRegistration`` from its composite document.

    M01's ``ResourceRegistration`` has no serializer of its own; this
    composite form reuses the identity/capabilities/cost serializers of
    the owning records and validates through the registration
    constructor. Violations surface as the uniform configuration error
    so the configuration boundary keeps one fail-closed error type.
    """
    from .errors import CapacityError
    from .resource_state import (
        ExecutionCapabilities,
        ResourceCost,
        ResourceIdentity,
    )

    dd = exact_shape(
        d,
        ("identity", "freshness_ttl_seconds"),
        ("poll_interval_seconds", "capabilities", "cost"),
        "resource_registration",
    )
    try:
        identity = ResourceIdentity.from_dict(dd["identity"])
        # Server-side availability policy is AUTHORITATIVE (M01/U-003):
        # server-direct HTTP resources get a bounded default polling
        # cadence so the server's readiness probes can observe them and
        # pinned execution is reachable on the documented first-run
        # path. Workers report their own observations (M05 transport);
        # their cadence stays unset unless the administrator sets one.
        default_poll = (
            SERVER_DIRECT_DEFAULT_POLL_INTERVAL_SECONDS
            if identity.channel == "server_direct_http"
            else None
        )
        return ResourceRegistration(
            identity=identity,
            freshness_ttl_seconds=v_int(
                dd["freshness_ttl_seconds"],
                "resource_registration.freshness_ttl_seconds",
                lo=1,
            ),
            poll_interval_seconds=(
                v_int(
                    dd["poll_interval_seconds"],
                    "resource_registration.poll_interval_seconds",
                    lo=1,
                )
                if "poll_interval_seconds" in dd
                else default_poll
            ),
            capabilities=(
                ExecutionCapabilities.from_dict(dd["capabilities"])
                if "capabilities" in dd
                else ExecutionCapabilities()
            ),
            cost=ResourceCost.from_dict(dd["cost"]) if "cost" in dd else None,
        )
    except CapacityError as exc:
        raise ServerConfigError(f"resource_registration: {exc}") from None


@dataclass(frozen=True)
class SourceConfig:
    """One execution source's administrator configuration (D-053).

    One source = one configured, independently authenticated source of
    executable model capacity: ``source_id`` (the identity), ``kind``
    (closed vocabulary; determines the provider and worker adapter
    kind), a human ``label``, the owning ``worker_id`` (the M05-paired
    device whose controlled homes serve this source), the
    ``entitlement`` its capacity draws from, and the adoption policy.
    Physical models are NOT configured here — they are discovered and
    adopted per D-053; an exact model pin stays an ADVANCED routing
    concern, never a source-setup requirement.

    The optional explicit ``quota_pool_id`` is the ONLY way two sources
    ever share a quota pool: absent it, every source derives its own
    pool (``pool-<source_id>``) and unconfirmed sharing is never
    assumed (D-042).
    """

    source_id: str
    kind: str
    label: str
    worker_id: str
    entitlement: str = "subscription_included"
    quota_pool_id: str | None = None
    auto_adopt: bool = True
    allowed_tracks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.source_id) > SOURCE_ID_MAX_LENGTH:
            raise ServerConfigError(
                f"source.source_id: longer than {SOURCE_ID_MAX_LENGTH} "
                + "characters (derived resource ids must stay safe ids)"
            )
        _ = v_safe_id(self.source_id, "source.source_id")
        if self.kind not in SOURCE_KINDS:
            raise ServerConfigError(f"source.kind: unknown kind {self.kind!r}")
        if not self.label or len(self.label) > 200:
            raise ServerConfigError("source.label: required, <= 200 chars")
        _ = v_safe_id(self.worker_id, "source.worker_id")
        from .resource_state import ENTITLEMENT_CLASSES

        if self.entitlement not in ENTITLEMENT_CLASSES:
            raise ServerConfigError(
                f"source.entitlement: unknown entitlement {self.entitlement!r}"
            )
        if self.quota_pool_id is not None:
            _ = v_safe_id(self.quota_pool_id, "source.quota_pool_id")
        for track_id in self.allowed_tracks:
            provider, sep, track = track_id.partition("/")
            if not sep:
                raise ServerConfigError(
                    f"source.allowed_tracks: track ids look like "
                    + f"'provider/track', got {track_id!r}"
                )
            _ = v_safe_id(provider, "source.allowed_tracks.provider")
            _ = v_safe_id(track, "source.allowed_tracks.track")

    def provider(self) -> str:
        """The provider this source kind executes through."""
        return _SOURCE_KIND_PROVIDER[self.kind]

    def pool_id(self) -> str:
        """The quota pool this source's capacity draws from (D-042)."""
        if self.quota_pool_id is not None:
            return self.quota_pool_id
        return f"pool-{self.source_id}"

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "source_id": self.source_id,
            "kind": self.kind,
            "label": self.label,
            "worker_id": self.worker_id,
            "entitlement": self.entitlement,
            "auto_adopt": self.auto_adopt,
        }
        if self.quota_pool_id is not None:
            out["quota_pool_id"] = self.quota_pool_id
        if self.allowed_tracks:
            out["allowed_tracks"] = list(self.allowed_tracks)
        return out

    @classmethod
    def from_dict(cls, d: object) -> "SourceConfig":
        dd = exact_shape(
            d,
            ("source_id", "kind", "label", "worker_id"),
            (
                "entitlement",
                "quota_pool_id",
                "auto_adopt",
                "allowed_tracks",
            ),
            "source",
        )
        allowed_raw = dd.get("allowed_tracks")
        allowed: tuple[str, ...] = ()
        if allowed_raw is not None:
            if not isinstance(allowed_raw, list):
                raise ServerConfigError("source.allowed_tracks must be an array")
            allowed = tuple(
                v_str(item, "source.allowed_tracks")
                for item in cast("list[object]", allowed_raw)
            )
        try:
            return cls(
                source_id=v_str(dd["source_id"], "source.source_id"),
                kind=v_str(dd["kind"], "source.kind"),
                label=v_str(dd["label"], "source.label"),
                worker_id=v_str(dd["worker_id"], "source.worker_id"),
                entitlement=(
                    v_str(dd["entitlement"], "source.entitlement")
                    if "entitlement" in dd
                    else "subscription_included"
                ),
                quota_pool_id=(
                    v_str(dd["quota_pool_id"], "source.quota_pool_id")
                    if "quota_pool_id" in dd
                    else None
                ),
                auto_adopt=(
                    v_bool(dd["auto_adopt"], "source.auto_adopt")
                    if "auto_adopt" in dd
                    else True
                ),
                allowed_tracks=allowed,
            )
        except ServerConfigError:
            raise
        except ValueError as exc:
            raise ServerConfigError(f"source: {exc}") from None


@dataclass(frozen=True)
class AuditRetention:
    """Bounded audit retention (D-043/D-044): count and age, both bounded."""

    max_records: int = _DEFAULT_AUDIT_MAX_RECORDS
    max_age_seconds: int = _DEFAULT_AUDIT_MAX_AGE_SECONDS

    def __post_init__(self) -> None:
        _ = v_int(self.max_records, "audit_retention.max_records", lo=1)
        _ = v_int(self.max_age_seconds, "audit_retention.max_age_seconds", lo=1)

    @classmethod
    def from_dict(cls, d: object) -> "AuditRetention":
        dd = exact_shape(
            d, ("max_records", "max_age_seconds"), (), "audit_retention"
        )
        return cls(
            max_records=v_int(dd["max_records"], "audit_retention.max_records", lo=1),
            max_age_seconds=v_int(
                dd["max_age_seconds"], "audit_retention.max_age_seconds", lo=1
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_records": self.max_records,
            "max_age_seconds": self.max_age_seconds,
        }


_EMPTY_ALIAS_BINDINGS: Mapping[str, ClientRoutingProfile] = MappingProxyType({})
_EMPTY_CLIENT_GRANTS: Mapping[str, ClientAuthorization] = MappingProxyType({})


@dataclass(frozen=True)
class ServerConfiguration:
    """The typed administrator configuration (one authoritative document)."""

    providers: tuple[ProviderEndpointConfig, ...] = ()
    resources: tuple[ResourceConfig, ...] = ()
    sources: tuple[SourceConfig, ...] = ()
    aliases: Mapping[str, ClientRoutingProfile] = field(
        default=_EMPTY_ALIAS_BINDINGS
    )
    client_authorizations: Mapping[str, ClientAuthorization] = field(
        default=_EMPTY_CLIENT_GRANTS
    )
    admin_constraints: AdministratorConstraints = field(
        default_factory=AdministratorConstraints
    )
    limits: GatewayLimits = field(default_factory=GatewayLimits)
    audit_retention: AuditRetention = field(default_factory=AuditRetention)
    pairing_code_ttl_seconds: int = _DEFAULT_PAIRING_CODE_TTL_SECONDS
    session_ttl_seconds: int = _DEFAULT_SESSION_TTL_SECONDS

    def __post_init__(self) -> None:
        if self.pairing_code_ttl_seconds < 1:
            raise ServerConfigError("pairing_code_ttl_seconds must be positive")
        if self.session_ttl_seconds < 1:
            raise ServerConfigError("session_ttl_seconds must be positive")
        _instance_of(self.admin_constraints, AdministratorConstraints)
        _instance_of(self.limits, GatewayLimits)
        _instance_of(self.audit_retention, AuditRetention)
        provider_ids = {provider.provider_id for provider in self.providers}
        if len(provider_ids) != len(self.providers):
            raise ServerConfigError("duplicate provider_id in configuration")
        resource_ids = {
            resource.registration.identity.resource_id for resource in self.resources
        }
        if len(resource_ids) != len(self.resources):
            raise ServerConfigError("duplicate resource_id in configuration")
        source_ids = {source.source_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ServerConfigError("duplicate source_id in configuration")
        worker_bound_resources = {
            resource.registration.identity.resource_id: resource.worker_id
            for resource in self.resources
            if resource.worker_id is not None
        }
        for derived in worker_bound_resources:
            from .model_inventory import is_source_resource_id

            if is_source_resource_id(derived):
                raise ServerConfigError(
                    f"resource {derived!r}: '<source_id>:<slug>' ids are "
                    + "derived from execution sources and must never be "
                    + "configured by hand (D-053)"
                )
        for resource in self.resources:
            if resource.endpoint_id is not None and (
                resource.endpoint_id not in provider_ids
            ):
                raise ServerConfigError(
                    f"resource {resource.registration.identity.resource_id!r} "
                    + f"references unknown endpoint {resource.endpoint_id!r}"
                )
        for alias, profile in self.aliases.items():
            _ = v_safe_id(alias, "server_config.alias")
            _instance_of(profile, ClientRoutingProfile)
        for client_id, grant in self.client_authorizations.items():
            _ = v_safe_id(client_id, "server_config.client_authorization")
            _instance_of(grant, ClientAuthorization)

    @classmethod
    def neutral(cls) -> "ServerConfiguration":
        """The neutral default: empty in every preference-bearing domain."""
        return cls()

    @classmethod
    def from_document(cls, document: object) -> "ServerConfiguration":
        """Parse and validate one configuration document (fail closed)."""
        dd = exact_shape(
            document,
            ("schema_version",),
            (
                "providers",
                "resources",
                "sources",
                "aliases",
                "client_authorizations",
                "admin_constraints",
                "limits",
                "audit_retention",
                "pairing_code_ttl_seconds",
                "session_ttl_seconds",
            ),
            "server_configuration",
        )
        version = v_int(dd["schema_version"], "server_configuration.schema_version")
        if version not in _SUPPORTED_CONFIG_SCHEMA_VERSIONS:
            raise ServerConfigError(
                f"server_configuration.schema_version {version} is not "
                + f"supported (expected {CONFIG_SCHEMA_VERSION})"
            )
        providers = tuple(
            ProviderEndpointConfig.from_dict(item)
            for item in _object_list(dd, "providers")
        )
        resources = tuple(
            ResourceConfig.from_dict(item) for item in _object_list(dd, "resources")
        )
        sources = tuple(
            SourceConfig.from_dict(item) for item in _object_list(dd, "sources")
        )
        if version < 2 and sources:
            raise ServerConfigError(
                "server_configuration.sources requires schema_version 2"
            )
        aliases: dict[str, ClientRoutingProfile] = {}
        raw_aliases: Mapping[str, object] = (
            cast("Mapping[str, object]", dd.get("aliases")) or {}
        )
        for alias, raw in v_str_object_mapping(
            raw_aliases, "server_configuration.aliases"
        ).items():
            aliases[v_safe_id(alias, "server_configuration.alias")] = (
                ClientRoutingProfile.from_dict(raw)
            )
        authorizations: dict[str, ClientAuthorization] = {}
        raw_authorizations: Mapping[str, object] = (
            cast("Mapping[str, object]", dd.get("client_authorizations")) or {}
        )
        for client_id, raw in v_str_object_mapping(
            raw_authorizations,
            "server_configuration.client_authorizations",
        ).items():
            authorizations[v_safe_id(client_id, "server_configuration.client_id")] = (
                ClientAuthorization.from_dict(raw)
            )
        return cls(
            providers=providers,
            resources=resources,
            sources=sources,
            aliases=aliases,
            client_authorizations=authorizations,
            admin_constraints=(
                AdministratorConstraints.from_dict(dd["admin_constraints"])
                if "admin_constraints" in dd
                else AdministratorConstraints()
            ),
            limits=(
                GatewayLimits.from_dict(dd["limits"]) if "limits" in dd else GatewayLimits()
            ),
            audit_retention=(
                AuditRetention.from_dict(dd["audit_retention"])
                if "audit_retention" in dd
                else AuditRetention()
            ),
            pairing_code_ttl_seconds=(
                v_int(
                    dd["pairing_code_ttl_seconds"],
                    "server_configuration.pairing_code_ttl_seconds",
                    lo=1,
                )
                if "pairing_code_ttl_seconds" in dd
                else _DEFAULT_PAIRING_CODE_TTL_SECONDS
            ),
            session_ttl_seconds=(
                v_int(
                    dd["session_ttl_seconds"],
                    "server_configuration.session_ttl_seconds",
                    lo=1,
                )
                if "session_ttl_seconds" in dd
                else _DEFAULT_SESSION_TTL_SECONDS
            ),
        )

    def to_document(self) -> dict[str, object]:
        """The secret-free authoritative document (the store's only copy)."""
        document: dict[str, object] = {"schema_version": CONFIG_SCHEMA_VERSION}
        if self.providers:
            document["providers"] = [provider.to_dict() for provider in self.providers]
        if self.resources:
            document["resources"] = [
                resource.to_dict() for resource in self.resources
            ]
        if self.sources:
            document["sources"] = [source.to_dict() for source in self.sources]
        if self.aliases:
            document["aliases"] = {
                alias: profile.to_dict() for alias, profile in self.aliases.items()
            }
        if self.client_authorizations:
            document["client_authorizations"] = {
                client_id: grant.to_dict()
                for client_id, grant in self.client_authorizations.items()
            }
        if self.admin_constraints.to_dict():
            document["admin_constraints"] = self.admin_constraints.to_dict()
        if self.limits.to_dict() != GatewayLimits().to_dict():
            document["limits"] = self.limits.to_dict()
        if self.audit_retention.to_dict() != AuditRetention().to_dict():
            document["audit_retention"] = self.audit_retention.to_dict()
        if self.pairing_code_ttl_seconds != _DEFAULT_PAIRING_CODE_TTL_SECONDS:
            document["pairing_code_ttl_seconds"] = self.pairing_code_ttl_seconds
        if self.session_ttl_seconds != _DEFAULT_SESSION_TTL_SECONDS:
            document["session_ttl_seconds"] = self.session_ttl_seconds
        return document

    def provider_by_id(self, provider_id: str) -> ProviderEndpointConfig | None:
        for provider in self.providers:
            if provider.provider_id == provider_id:
                return provider
        return None

    def resource_by_id(self, resource_id: str) -> ResourceConfig | None:
        for resource in self.resources:
            if resource.registration.identity.resource_id == resource_id:
                return resource
        return None

    def source_by_id(self, source_id: str) -> SourceConfig | None:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        return None

    def enabled_registrations(self) -> tuple[ResourceRegistration, ...]:
        """Registrations of enabled resources, in configuration order."""
        return tuple(
            resource.registration
            for resource in self.resources
            if resource.enabled
        )

    def validate_no_router_loop(self, own_origins: tuple[str, ...]) -> None:
        """Refuse the server's own origin (or another router's) as a backend.

        D-044 router-loop protection: a provider endpoint whose origin
        equals one of the given router origins is a configuration error —
        router -> router chains must fail loudly at configuration time,
        never silently loop at request time. Origins compare by
        ``(scheme, host, port)``.
        """
        own = {_origin_key(origin) for origin in own_origins}
        for provider in self.providers:
            if _origin_key(provider.base_url) in own:
                raise ServerConfigError(
                    f"provider {provider.provider_id!r} base_url identifies a "
                    + "Scarcity Router endpoint; a router must not serve as "
                    + "another router's provider backend (D-044)"
                )


def _origin_key(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, hostname, port)


def _object_list(document: Mapping[str, object], field: str) -> list[object]:
    if field not in document:
        return []
    raw = document[field]
    if not isinstance(raw, list):
        raise ServerConfigError(f"server_configuration.{field} must be an array")
    return cast("list[object]", raw)


def _instance_of(value: object, expected: type) -> None:
    from typing import cast as _cast

    from .gateway_validation import v_instance

    _ = v_instance(value, _cast("type[object]", expected), "server_config value")


__all__ = [
    "AuditRetention",
    "CONFIG_SCHEMA_VERSION",
    "SOURCE_ID_MAX_LENGTH",
    "SOURCE_KINDS",
    "SourceConfig",
    "ProviderEndpointConfig",
    "SERVER_DIRECT_DEFAULT_POLL_INTERVAL_SECONDS",
    "ResourceConfig",
    "ServerConfigError",
    "ServerConfiguration",
]
