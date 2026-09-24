"""The authenticated control API and administration plane (M09, issue #94).

One server component serves all four surfaces (D-041): the OpenAI-
compatible execution surface (M03), the authenticated control API (this
module), the lightweight web UI (:mod:`scarcity_router.server_ui`) and
the M05 worker-protocol endpoint. This module owns the control
plane that the execution server dispatches to:

- **Machine-interface control endpoints** ``GET /v1/status``,
  ``POST /v1/select``, ``POST /v1/simulate`` — the exact logical contract
  and paths the M08 remote bridge client
  (:mod:`scarcity_router.remote`) calls, authenticated with the D-044
  inference-client identity (``Authorization: Bearer <client key>``).
  Parsing and envelopes come from the shared
  :mod:`scarcity_router.machine_api` and application behavior from
  :func:`scarcity_router.selection_app.select_from_inputs` /
  ``simulate_from_inputs`` — no selector logic exists here.
- **Administration services and endpoints** — first-run administrator
  onboarding (no default credential exists, D-044), session
  login/logout, provider endpoint and credential administration,
  resource configuration with enable/disable, routing-profile aliases
  (validated against the task-profile catalog, D-042), client key
  issuance/revocation (SHA-256 hash storage, constant-time comparison
  through :class:`~scarcity_router.gateway_contracts.ClientKeyDirectory`),
  worker pairing administration (delegated to the ONE pairing system,
  M05's :class:`~scarcity_router.worker_identity_store.WorkerAdminService`
  and :class:`~scarcity_router.worker_endpoint.WorkerEndpoint`), the
  M04-backed execution-adapter composition
  (:mod:`scarcity_router.server_composition`), diagnostics, and a
  secret-free configuration export.

Security posture (D-044): three separate identity classes (administrator
sessions, inference client keys, worker per-device credentials) never
collapsed; no bearer secret ever appears in a URL; client keys never
authorize administration and administrator sessions never authorize the
machine-interface endpoints; mutations require an authenticated
administrator session plus a per-session CSRF token. Worker pairing
redemption is NOT an HTTP endpoint: the pairing code is redeemed inside
the M05 worker protocol's verified-TLS handshake (the trust bootstrap is
the protocol handshake itself), while code ISSUANCE stays here. Every
response is ``Cache-Control: no-store`` and every error message is a
safe structural message.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl

from .diagnostics import (
    DiagnosticsReport,
    ServerDiagnosticsInputs,
    collect_server_diagnostics,
)
from .control_errors import (
    ADMIN_PREFIX,
    CONTROL_PREFIX,
    CSRF_FORM_FIELD,
    CSRF_HEADER_NAME,
    LIVENESS_PATH,
    ROOT_PATH,
    SESSION_COOKIE_NAME,
    ControlHTTPError,
    MACHINE_SELECT_PATH,
    MACHINE_SIMULATE_PATH,
    MACHINE_STATUS_PATH,
)
from .errors import ApplicationInputError
from .gateway_adapters import AdapterRegistry
from .gateway_audit import AuditRecord, BoundedAuditTrail
from .gateway_contracts import (
    EXECUTION_SURFACE_VERSION,
    ClientKeyDirectory,
    GatewayLimits,
    hash_client_key,
)
from .gateway_coordinator import GatewayApplication, RoutingAliasTable
from .gateway_server import GATEWAY_ORIGIN_HEADER, GatewayRequestHandler
from .gateway_validation import v_safe_id
from .machine_api import (
    parse_selection_document,
    parse_simulation_document,
    selection_envelope,
    simulation_envelope,
    status_envelope,
)
from .capacity import CapacityDiagnostic, CapacitySnapshot
from .eligibility import ExecutionEligibility
from .execution_sources import SourceRegistry, is_source_resource_id
from .model_inventory import ModelInventoryReport
from .model_tracks import load_track_registry
from .resource_state import (
    RESOURCE_STATE_SCHEMA_VERSION,
    ResourceHealth,
    ResourceRegistration,
    ResourceRegistry,
    ResourceStateSnapshot,
    WorkerStateReport,
)
from .routing_core import (
    AdministratorConstraints,
    ClientAuthorization,
    ClientRoutingProfile,
)
from .config import resolve_default_selector_policy
from .selector import SelectorPolicy, neutral_selector_policy
from .selection_app import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_MODEL_POLICY_PATH,
    load_catalog,
    load_configured_artifacts,
    load_model_policy,
    load_strict_json,
    select_from_inputs,
    simulate_from_inputs,
)
from .server_composition import (
    build_adapter_registry,
    build_compatibility_cells,
    validate_execution_configuration,
)
from .server_config import (
    AuditRetention,
    ProviderEndpointConfig,
    SourceConfig,
    ResourceConfig,
    ServerConfigError,
    ServerConfiguration,
)
from .server_store import ServerStore, ServerStoreError
from .providers.openai_http_adapter import (
    HealthProbeResult,
    OpenAICompatibleHttpAdapter,
)
from .status import StatusCollectors, collect_status
from .worker_endpoint import WorkerEndpoint
from .worker_identity_store import (
    WorkerAdminService,
    WorkerIdentityError,
    WorkerIdentityStore,
    default_worker_store_path,
)

#: The injected web-UI dispatcher (see ``scarcity_router.server_ui``).
UiDispatcher = Callable[
    ["ControlPlane", str, str, GatewayRequestHandler], bool
]

_MAX_CONTROL_BODY_BYTES = 1_048_576
_MAX_KEY_LENGTH = 4096
_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 128
_DEFAULT_PBKDF2_ITERATIONS = 240_000

_CLIENT_KEY_PREFIX = "sk-sr-"


# ── Credential primitives ─────────────────────────────────────────────────────


def format_utc(moment: datetime) -> str:
    """Canonical UTC instant in the repository's wire form."""
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def hash_admin_password(password: str, *, iterations: int) -> tuple[str, str, int]:
    """PBKDF2-HMAC-SHA256 verifier for the administrator password.

    Returns ``(salt_hex, hash_hex, iterations)``; only the verifier is
    ever stored, with a fresh per-instance random salt every time.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return salt.hex(), digest.hex(), iterations


def verify_admin_password(
    password: str,
    *,
    salt_hex: str,
    expected_hash_hex: str,
    iterations: int,
) -> bool:
    """Constant-time administrator password verification."""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hash_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The only stored representation of a session/worker token or code."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_client_key() -> str:
    """A fresh inference-client API key (returned exactly once)."""
    return _CLIENT_KEY_PREFIX + secrets.token_urlsafe(24)


def validate_admin_password(password: object) -> str:
    """First-run password policy: bounded length, no trimming ambiguity."""
    if not isinstance(password, str):
        raise ControlHTTPError.invalid_request("password must be a string")
    if len(password) < _MIN_PASSWORD_LENGTH or len(password) > _MAX_PASSWORD_LENGTH:
        raise ControlHTTPError.invalid_request(
            f"password must be between {_MIN_PASSWORD_LENGTH} "
            + f"and {_MAX_PASSWORD_LENGTH} characters"
        )
    if password.strip() != password:
        raise ControlHTTPError.invalid_request(
            "password must not start or end with whitespace"
        )
    return password


def _derive_csrf(session_token: str) -> str:
    """The per-session CSRF token derived from the session token itself.

    Only the CSRF hash is stored; the value is recomputed from the session
    cookie for rendering and comparison, so no second bearer secret is
    persisted anywhere.
    """
    return hmac.new(
        hash_token(session_token).encode("utf-8"), b"csrf", hashlib.sha256
    ).hexdigest()


# ── Durable audit sink ────────────────────────────────────────────────────────


class DurableAuditSink:
    """Audit sink that fans records into memory and the durable store.

    The in-memory :class:`BoundedAuditTrail` keeps the M03 behavior; the
    durable copy gives usage accounting and the audit trail their D-041
    persistence with the configuration's bounded retention. Records are
    the D-043 metadata set only — no prompt/response content is
    representable in an :class:`AuditRecord`.
    """

    _store: ServerStore
    _retention: AuditRetention
    _trail: BoundedAuditTrail

    def __init__(self, store: ServerStore, retention: AuditRetention) -> None:
        self._store = store
        self._retention = retention
        self._trail = BoundedAuditTrail(
            max_records=retention.max_records,
            max_age_seconds=retention.max_age_seconds,
        )

    @property
    def trail(self) -> BoundedAuditTrail:
        return self._trail

    def update_retention(self, retention: AuditRetention) -> None:
        self._retention = retention

    def append(self, record: AuditRecord) -> None:
        self._trail.append(record)
        self._store.append_audit_record(
            payload=record.to_dict(),
            recorded_at=record.ended_at,
            max_records=self._retention.max_records,
            max_age_seconds=self._retention.max_age_seconds,
        )


# ── The control plane ─────────────────────────────────────────────────────────

GenerationTester = Callable[[ResourceConfig], dict[str, object]]


class ControlPlane:
    """The administration and control surface of one server component."""

    _store: ServerStore
    _catalog_path: Path
    _model_policy_path: Path
    _collectors: StatusCollectors | None
    _clock: Callable[[], datetime]
    _adapters: AdapterRegistry
    _own_origins: tuple[str, ...]
    _tls: bool
    _pbkdf2_iterations: int
    _version: str | None
    _observations: dict[str, ResourceStateSnapshot]
    _request_state: threading.local
    _config: ServerConfiguration
    _sink: DurableAuditSink
    _application: GatewayApplication | None
    _ui_dispatcher: UiDispatcher | None
    _generation_tester: GenerationTester | None
    _registry_clock: Callable[[], str]
    _worker_identity_store: WorkerIdentityStore
    _owns_worker_store: bool
    _worker_admin: WorkerAdminService
    _worker_endpoint: WorkerEndpoint

    def __init__(
        self,
        *,
        store: ServerStore,
        catalog_path: Path = DEFAULT_CATALOG_PATH,
        model_policy_path: Path = DEFAULT_MODEL_POLICY_PATH,
        collectors: StatusCollectors | None = None,
        clock: Callable[[], datetime] | None = None,
        adapters: AdapterRegistry | None = None,
        own_origins: tuple[str, ...] = (),
        tls: bool = False,
        pbkdf2_iterations: int = _DEFAULT_PBKDF2_ITERATIONS,
        generation_tester: GenerationTester | None = None,
        ui_dispatcher: UiDispatcher | None = None,
        version: str | None = None,
        worker_identity_store: WorkerIdentityStore | None = None,
    ) -> None:
        self._ui_dispatcher = ui_dispatcher
        self._store = store
        self._catalog_path = catalog_path
        self._model_policy_path = model_policy_path
        self._collectors = collectors
        self._clock = clock if clock is not None else _default_clock
        self._adapters = adapters if adapters is not None else AdapterRegistry()
        self._own_origins = own_origins
        self._tls = tls
        self._pbkdf2_iterations = pbkdf2_iterations
        self._generation_tester = generation_tester
        self._version = version
        self._observations = {}
        # Per-request scratch state (each HTTP request runs on its own
        # thread); the form body is read at most once per request.
        self._request_state = threading.local()
        self._registry_clock = lambda: format_utc(
            datetime.now(timezone.utc)
        )
        # ONE pairing system (M05): the control plane owns the worker
        # identity store (its own permissioned database beside the server
        # store) and the worker endpoint; the administration endpoints
        # below delegate to them. An injected store lets deployments place
        # it explicitly; the default is the canonical server-data location.
        if worker_identity_store is not None:
            self._worker_identity_store = worker_identity_store
            self._owns_worker_store = False
        else:
            self._worker_identity_store = WorkerIdentityStore(
                default_worker_store_path(store.path.parent),
                clock=self._clock,
            )
            self._owns_worker_store = True
        self._worker_admin = WorkerAdminService(self._worker_identity_store)
        # Ownership resolver: a live read over the CURRENT configuration
        # document. The resolver is re-read on every call, so each
        # configuration change (add/remove/enable/unassign) is
        # authoritative immediately — no pushed-copy staleness window.
        # D-053: the server-side execution-source registry (derived
        # resources, track-floor catalog entries, source views). The
        # reviewed track artifact loads with the other calibrated inputs.
        self._source_registry = SourceRegistry(
            track_registry=load_track_registry()
        )
        self._worker_endpoint = WorkerEndpoint(
            identity_store=self._worker_identity_store,
            registry=self,
            configured_owner=self._configured_worker_owner,
        )
        self._worker_endpoint.inventory_sink = self._apply_source_inventory
        document = store.load_configuration_document()
        self._config = (
            ServerConfiguration.from_document(document)
            if document is not None
            else ServerConfiguration.neutral()
        )
        self._sink = DurableAuditSink(store, self._config.audit_retention)
        self._application = None
        self._rebuild_application()

    # ── Server-facing dispatch contract (see gateway_server) ─────────────

    @property
    def store(self) -> ServerStore:
        return self._store

    @property
    def configuration(self) -> ServerConfiguration:
        return self._config

    def admin_configured(self) -> bool:
        return self._store.admin_identity() is not None

    def current_application(self) -> GatewayApplication:
        application = self._application
        if application is None:  # pragma: no cover - construction invariant
            raise RuntimeError("control plane has no application")
        return application

    @property
    def worker_endpoint(self) -> WorkerEndpoint:
        """The M05 worker-protocol endpoint of this process (composition)."""
        return self._worker_endpoint

    @property
    def worker_admin(self) -> WorkerAdminService:
        """The M05 administration seam over the one identity store."""
        return self._worker_admin

    def close_worker_store(self) -> None:
        """Close the worker identity store when this plane created it."""
        if self._owns_worker_store:
            self._worker_identity_store.close()

    def _configured_worker_owner(self, resource_id: str) -> str | None:
        """The configured owner of one resource, or ``None``.

        The ONLY ownership source: the live administrator configuration.
        Unknown, disabled, non-worker-bridged, or unassigned resources
        have no owner — nothing is reportable or executable (D-049
        amendment).
        """
        resource = self._config.resource_by_id(resource_id)
        if resource is not None:
            if not resource.enabled:
                return None
            if resource.registration.identity.channel != "worker_bridged":
                return None
            return resource.worker_id
        # D-053: source-derived resources are owned by their source's
        # configured worker — the administrator granted ownership at
        # source granularity; the report only supplies the inventory.
        derived_owner = self._source_registry.owner_of(resource_id)
        if derived_owner is not None and is_source_resource_id(resource_id):
            return derived_owner
        return None

    def _apply_source_inventory(self, inventory: ModelInventoryReport) -> None:
        """The endpoint's validated inventory sink (D-053).

        Applies the discovery document to the source registry (adopt /
        retire decisions) and rebuilds the routing artifacts so the
        derived registrations, adapter bindings, compatibility cells and
        catalog view all change atomically with the configuration.
        """
        self._source_registry.sync_configuration(self._config.sources)
        _ = self._source_registry.apply_inventory(inventory)
        self._rebuild_application()

    def apply_worker_report(self, report: WorkerStateReport) -> None:
        """The M05 endpoint's report sink (the one normalization path).

        Reports are applied atomically through the live registry (M01
        fail-closed semantics: an unregistered or future-dated entry
        changes nothing) and recorded so configuration rebuilds re-apply
        them — worker-reported state never silently evaporates because an
        administrator edited another resource.
        """
        registry = self.current_application().registry
        registry.apply_worker_report(report)
        for snapshot in report.resources:
            self._observations[snapshot.identity.resource_id] = snapshot

    def handles(self, method: str, path: str) -> bool:
        _ = method
        if path == LIVENESS_PATH:
            return True
        if path in (MACHINE_STATUS_PATH, MACHINE_SELECT_PATH, MACHINE_SIMULATE_PATH):
            return True
        if path == ROOT_PATH or path.startswith(ADMIN_PREFIX):
            return True
        return path.startswith(CONTROL_PREFIX)

    def handle(self, method: str, path: str, handler: GatewayRequestHandler) -> None:
        """Dispatch one request; every failure is a safe response."""
        self._request_state.form = None
        try:
            self._check_router_loop_marker(handler)
            if path == LIVENESS_PATH:
                # Liveness only, exactly like the frozen loopback adapter's
                # /healthz (D-028, GET-only): no collector, no store, no
                # auth data — the one unauthenticated path, so container
                # health probes work without credentials (M10, issue #95).
                if method != "GET":
                    raise ControlHTTPError.method_not_allowed()
                self._send_json(handler, 200, {"status": "ok"})
                return
            if path in (MACHINE_STATUS_PATH, MACHINE_SELECT_PATH, MACHINE_SIMULATE_PATH):
                self._route_machine(method, path, handler)
                return
            if path.startswith(CONTROL_PREFIX):
                self._route_control(method, path, handler)
                return
            if path == ROOT_PATH or path.startswith(ADMIN_PREFIX):
                dispatcher = self._ui_dispatcher
                if dispatcher is not None and dispatcher(self, method, path, handler):
                    return
            raise ControlHTTPError.not_found("unknown request URL")
        except ControlHTTPError as exc:
            self._send_error(handler, exc)
        except ServerConfigError as exc:
            # Fail-closed configuration validation is a client-classifiable
            # administrator error with a safe structural message.
            self._send_error(handler, ControlHTTPError.invalid_request(str(exc)))
        except ServerStoreError:
            self._send_internal_error(handler)
        except Exception:
            self._send_internal_error(handler)

    def _check_router_loop_marker(self, handler: GatewayRequestHandler) -> None:
        if handler.headers.get(GATEWAY_ORIGIN_HEADER) is not None:
            raise ControlHTTPError(
                400,
                "router_loop_detected",
                "refusing gateway-originated traffic: a Scarcity Router "
                + "endpoint must not serve as another router's backend",
            )

    # ── HTTP plumbing ────────────────────────────────────────────────────

    def _send_json(
        self,
        handler: GatewayRequestHandler,
        status: int,
        payload: Mapping[str, object],
        *,
        cookies: tuple[str, ...] = (),
    ) -> None:
        import json

        encoded = json.dumps(
            payload, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(encoded)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            for cookie in cookies:
                handler.send_header("Set-Cookie", cookie)
            handler.end_headers()
            _ = handler.wfile.write(encoded)
        except OSError:
            handler.close_connection = True

    def send_html(
        self,
        handler: GatewayRequestHandler,
        status: int,
        html: str,
        *,
        cookies: tuple[str, ...] = (),
    ) -> None:
        encoded = html.encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(encoded)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            handler.send_header("Referrer-Policy", "no-referrer")
            for cookie in cookies:
                handler.send_header("Set-Cookie", cookie)
            handler.end_headers()
            _ = handler.wfile.write(encoded)
        except OSError:
            handler.close_connection = True

    def send_redirect(self, handler: GatewayRequestHandler, location: str) -> None:
        try:
            handler.send_response(HTTPStatus.SEE_OTHER.value)
            handler.send_header("Location", location)
            handler.send_header("Content-Length", "0")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
        except OSError:
            handler.close_connection = True

    def _send_error(self, handler: GatewayRequestHandler, exc: ControlHTTPError) -> None:
        self._send_json(
            handler, exc.status, {"error": {"code": exc.code, "message": exc.message}}
        )

    def _send_internal_error(self, handler: GatewayRequestHandler) -> None:
        self._send_json(
            handler,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": {"code": "internal_error", "message": "internal server error"}},
        )

    def _read_body(self, handler: GatewayRequestHandler) -> bytes:
        if handler.headers.get_all("Transfer-Encoding"):
            handler.close_connection = True
            raise ControlHTTPError.invalid_request("transfer-encoding is not supported")
        lengths = handler.headers.get_all("Content-Length")
        if lengths is None or len(lengths) != 1:
            handler.close_connection = True
            raise ControlHTTPError.invalid_request(
                "exactly one content-length header is required"
            )
        raw = lengths[0].strip(" \t")
        if not raw or any(character not in "0123456789" for character in raw):
            handler.close_connection = True
            raise ControlHTTPError.invalid_request(
                "content-length must be a decimal integer"
            )
        length = int(raw)
        if length > _MAX_CONTROL_BODY_BYTES:
            handler.close_connection = True
            raise ControlHTTPError.invalid_request("request body is too large")
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = handler.rfile.read(remaining)
            if not chunk:
                handler.close_connection = True
                raise ControlHTTPError.invalid_request("incomplete request body")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _require_json_body(self, handler: GatewayRequestHandler) -> dict[str, object]:
        content_type = (handler.headers.get("Content-Type") or "").split(";")[0]
        if content_type.strip().lower() != "application/json":
            raise ControlHTTPError.invalid_request(
                "content-type application/json is required"
            )
        body = self._read_body(handler)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ControlHTTPError.invalid_request(
                "request body is not valid UTF-8"
            ) from None
        try:
            document = load_strict_json(text, label="request body")
        except ValueError:
            raise ControlHTTPError.invalid_request(
                "request body is not valid JSON"
            ) from None
        if not isinstance(document, dict):
            raise ControlHTTPError.invalid_request("request body must be a JSON object")
        return cast("dict[str, object]", document)

    def read_form(self, handler: GatewayRequestHandler) -> dict[str, str]:
        """Parse a form-encoded body (the web UI's only input channel).

        The body is read exactly once per request and cached: the CSRF
        check and the form handlers share the same parsed value, so the
        socket is never read twice.
        """
        cached = cast(
            "dict[str, str] | None", getattr(self._request_state, "form", None)
        )
        if cached is not None:
            return cached
        content_type = (handler.headers.get("Content-Type") or "").split(";")[0]
        if content_type.strip().lower() != "application/x-www-form-urlencoded":
            raise ControlHTTPError.invalid_request(
                "content-type application/x-www-form-urlencoded is required"
            )
        body = self._read_body(handler)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ControlHTTPError.invalid_request(
                "request body is not valid UTF-8"
            ) from None
        form = dict(parse_qsl(text, keep_blank_values=True, strict_parsing=False))
        self._request_state.form = form
        return form

    # ── Administrator session helpers ────────────────────────────────────

    def session_token(self, handler: GatewayRequestHandler) -> str | None:
        header = handler.headers.get("Cookie")
        if not header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        token: str | None = morsel.value
        if not token or len(token) > _MAX_KEY_LENGTH:
            return None
        return token

    def require_admin_session(
        self, handler: GatewayRequestHandler, *, mutating: bool
    ) -> str:
        """Authenticate the administrator session; enforce CSRF on mutations."""
        token = self.session_token(handler)
        if token is None:
            raise ControlHTTPError.unauthenticated("administrator session required")
        session_hash = hash_token(token)
        stored_csrf_hash = self._store.session_csrf_hash(
            session_hash, now=format_utc(self._clock())
        )
        if stored_csrf_hash is None:
            raise ControlHTTPError.unauthenticated("administrator session expired")
        if mutating:
            presented = self._presented_csrf(handler)
            if presented is None:
                raise ControlHTTPError.forbidden("CSRF token required")
            if not hmac.compare_digest(
                hash_token(presented), stored_csrf_hash
            ):
                raise ControlHTTPError.forbidden("invalid CSRF token")
        return token

    def csrf_token_for(self, handler: GatewayRequestHandler) -> str | None:
        """The session's CSRF value for form rendering, or ``None``."""
        return self.csrf_token_for_cookie(self.session_token(handler))

    def csrf_token_for_cookie(self, cookie_value: str | None) -> str | None:
        """Same as :meth:`csrf_token_for` for an already-read cookie value."""
        if cookie_value is None:
            return None
        if self._store.session_csrf_hash(
            hash_token(cookie_value), now=format_utc(self._clock())
        ) is None:
            return None
        return _derive_csrf(cookie_value)

    def _presented_csrf(self, handler: GatewayRequestHandler) -> str | None:
        header_values = handler.headers.get_all(CSRF_HEADER_NAME)
        if header_values is not None and len(header_values) == 1:
            value = header_values[0]
            if value:
                return value
        content_type = (handler.headers.get("Content-Type") or "").split(";")[0]
        if content_type.strip().lower() == "application/x-www-form-urlencoded":
            form = self.read_form(handler)
            value = form.get(CSRF_FORM_FIELD)
            if value:
                return value
        return None

    def _create_admin_session(self) -> str:
        """Create one session; returns the ``Set-Cookie`` header value."""
        token = new_session_token()
        now = self._clock()
        expires = now + timedelta(seconds=self._config.session_ttl_seconds)
        self._store.create_session(
            session_hash=hash_token(token),
            csrf_hash=hash_token(_derive_csrf(token)),
            created_at=format_utc(now),
            expires_at=format_utc(expires),
        )
        cookie = (
            f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
            + f"Max-Age={self._config.session_ttl_seconds}"
        )
        if self._tls:
            cookie += "; Secure"
        return cookie

    def _clear_session_cookie(self) -> str:
        cookie = (
            f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        )
        if self._tls:
            cookie += "; Secure"
        return cookie

    def _authenticate_client(self, handler: GatewayRequestHandler) -> str:
        """Bearer-key authentication for the machine-interface endpoints."""
        directory = self.current_application().client_key_directory
        if directory is None:
            raise ControlHTTPError.unauthenticated("authentication is unavailable")
        values = handler.headers.get_all("Authorization")
        if values is None or len(values) != 1:
            raise ControlHTTPError.unauthenticated("missing API key")
        header = values[0].strip()
        scheme, _, key = header.partition(" ")
        if scheme.lower() != "bearer" or not key:
            raise ControlHTTPError.unauthenticated("invalid API key")
        key = key.strip()
        if not key or len(key) > _MAX_KEY_LENGTH:
            raise ControlHTTPError.unauthenticated("invalid API key")
        client_id = directory.authenticate(key)
        if client_id is None:
            raise ControlHTTPError.unauthenticated("invalid API key")
        return client_id

    # ── Machine-interface control endpoints (M08 parity) ─────────────────

    def _route_machine(
        self, method: str, path: str, handler: GatewayRequestHandler
    ) -> None:
        client_id = self._authenticate_client(handler)
        if path == MACHINE_STATUS_PATH:
            if method != "GET":
                raise ControlHTTPError.method_not_allowed()
            observation = collect_status(collectors=self._collectors, clock=self._clock)
            self._send_json(
                handler,
                HTTPStatus.OK,
                status_envelope(observation.snapshots, observation.eligibility),
            )
            return
        if method != "POST":
            raise ControlHTTPError.method_not_allowed()
        document = self._require_json_body(handler)
        catalog = load_catalog(self._catalog_path)
        profiles, policy_version = load_model_policy(self._model_policy_path)
        try:
            if path == MACHINE_SELECT_PATH:
                parsed = parse_selection_document(document)
                decision = select_from_inputs(
                    catalog=catalog,
                    profiles=profiles,
                    profile_policy_version=policy_version,
                    profile_id=parsed.profile_id,
                    requirement=parsed.requirement,
                    tightening=parsed.tightening,
                    policy=(
                        parsed.policy
                        if parsed.policy is not None
                        else self._default_policy()
                    ),
                    replenishment_states=parsed.replenishment_states,
                    collectors=self._collectors,
                    clock=self._clock,
                )
                _ = client_id
                self._send_json(handler, HTTPStatus.OK, selection_envelope(decision))
                return
            parsed, overrides = parse_simulation_document(document)
            result = simulate_from_inputs(
                catalog=catalog,
                profiles=profiles,
                profile_policy_version=policy_version,
                profile_id=parsed.profile_id,
                requirement=parsed.requirement,
                tightening=parsed.tightening,
                policy=(
                    parsed.policy
                    if parsed.policy is not None
                    else self._default_policy()
                ),
                replenishment_states=parsed.replenishment_states,
                overrides=overrides,
                collectors=self._collectors,
                clock=self._clock,
            )
            _ = client_id
            self._send_json(handler, HTTPStatus.OK, simulation_envelope(result))
        except ApplicationInputError:
            raise ControlHTTPError.invalid_request() from None

    def _default_policy(self) -> SelectorPolicy:
        """The D-036 server default policy, else the documented neutral."""
        policy = resolve_default_selector_policy()
        return policy if policy is not None else neutral_selector_policy()

    # ── Control endpoint routing ─────────────────────────────────────────

    def _route_control(
        self, method: str, path: str, handler: GatewayRequestHandler
    ) -> None:
        parts = [part for part in path[len(CONTROL_PREFIX) :].split("/") if part]
        # The bootstrap endpoint authenticates with the first-run password;
        # everything else needs a session. Worker pairing redemption is not
        # an HTTP endpoint at all: the M05 worker protocol's TLS handshake
        # is the redemption path.
        if parts == ["bootstrap", "admin"] and method == "POST":
            document = self._require_json_body(handler)
            self.service_bootstrap_admin(
                password=document.get("password"), confirm=document.get("confirm")
            )
            cookie = self._create_admin_session()
            self._send_json(
                handler,
                HTTPStatus.OK,
                {"status": "ok", "administrator": "configured"},
                cookies=(cookie,),
            )
            return
        if parts == ["session"] and method == "POST":
            document = self._require_json_body(handler)
            self.service_login(document)
            cookie = self._create_admin_session()
            self._send_json(
                handler, HTTPStatus.OK, {"status": "ok"}, cookies=(cookie,)
            )
            return
        mutating = method in ("POST", "PUT", "DELETE", "PATCH")
        token = self.require_admin_session(handler, mutating=mutating)
        if parts == ["session"] and method == "DELETE":
            self._store.delete_session(hash_token(token))
            self._send_json(
                handler,
                HTTPStatus.OK,
                {"status": "ok"},
                cookies=(self._clear_session_cookie(),),
            )
            return
        if parts == ["state"] and method == "GET":
            self._send_json(handler, HTTPStatus.OK, self._state_document())
            return
        if parts == ["providers"]:
            if method == "GET":
                self._send_json(
                    handler, HTTPStatus.OK, {"providers": self._providers_view()}
                )
                return
            if method == "POST":
                document = self._require_json_body(handler)
                provider_id = self.service_add_provider(document)
                self._send_json(
                    handler, HTTPStatus.OK, {"status": "ok", "provider_id": provider_id}
                )
                return
        if len(parts) == 2 and parts[0] == "providers" and method == "DELETE":
            self.service_remove_provider(parts[1])
            self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
            return
        if parts == ["resources"] and method == "GET":
            self._send_json(handler, HTTPStatus.OK, {"resources": self._resources_view()})
            return
        if parts == ["resources"] and method == "POST":
            document = self._require_json_body(handler)
            resource_id = self.service_add_resource(document)
            self._send_json(
                handler, HTTPStatus.OK, {"status": "ok", "resource_id": resource_id}
            )
            return
        if len(parts) == 2 and parts[0] == "resources" and method == "DELETE":
            self.service_remove_resource(parts[1])
            self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
            return
        if parts == ["sources"] and method == "GET":
            self._send_json(handler, HTTPStatus.OK, {"sources": self.sources_view()})
            return
        if parts == ["sources"] and method == "POST":
            document = self._require_json_body(handler)
            source_id = self.service_add_source(document)
            self._send_json(
                handler, HTTPStatus.OK, {"status": "ok", "source_id": source_id}
            )
            return
        if len(parts) == 2 and parts[0] == "sources" and method == "DELETE":
            self.service_remove_source(parts[1])
            self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
            return
        if len(parts) == 3 and parts[0] == "resources":
            resource_id = parts[1]
            if parts[2] == "enabled" and method == "POST":
                document = self._require_json_body(handler)
                enabled = document.get("enabled")
                if not isinstance(enabled, bool):
                    raise ControlHTTPError.invalid_request("enabled must be a boolean")
                self.service_set_resource_enabled(resource_id, enabled)
                self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
                return
            if parts[2] == "connection-test" and method == "POST":
                _ = self._require_json_body(handler)
                document = self._connection_test_document(resource_id)
                self._send_json(handler, HTTPStatus.OK, document)
                return
            if parts[2] == "generation-test" and method == "POST":
                document = self._require_json_body(handler)
                if document.get("confirm") is not True:
                    raise ControlHTTPError.invalid_request(
                        "a generation test consumes real inference quota and "
                        + "requires {\"confirm\": true}"
                    )
                self._send_json(
                    handler, HTTPStatus.OK, self._run_generation_test(resource_id)
                )
                return
        if parts == ["aliases"] and method == "GET":
            self._send_json(handler, HTTPStatus.OK, {"aliases": self._aliases_view()})
            return
        if len(parts) == 2 and parts[0] == "aliases":
            if method == "PUT":
                document = self._require_json_body(handler)
                self.service_put_alias(parts[1], document)
                self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
                return
            if method == "DELETE":
                self.service_delete_alias(parts[1])
                self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
                return
        if parts == ["clients"] and method == "GET":
            self._send_json(handler, HTTPStatus.OK, {"clients": self._clients_view()})
            return
        if parts == ["clients"] and method == "POST":
            document = self._require_json_body(handler)
            self._send_json(handler, HTTPStatus.OK, self.service_issue_client_key(document))
            return
        if len(parts) == 2 and parts[0] == "clients" and method == "DELETE":
            self.service_revoke_client_key(parts[1])
            self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
            return
        if parts == ["workers"] and method == "GET":
            self._send_json(handler, HTTPStatus.OK, {"workers": self._workers_view()})
            return
        if parts == ["workers", "pairing-codes"] and method == "POST":
            document = self._require_json_body(handler)
            self._send_json(handler, HTTPStatus.OK, self.service_initiate_pairing(document))
            return
        if (
            len(parts) == 3
            and parts[0] == "workers"
            and parts[2] == "rotate"
            and method == "POST"
        ):
            self._send_json(
                handler,
                HTTPStatus.OK,
                self.service_rotate_worker_credential(parts[1]),
            )
            return
        if len(parts) == 2 and parts[0] == "workers" and method == "DELETE":
            self.service_revoke_worker(parts[1])
            self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
            return
        if parts == ["diagnostics"] and method == "GET":
            self._send_json(handler, HTTPStatus.OK, self.build_diagnostics().to_dict())
            return
        if parts == ["export"] and method == "GET":
            self._send_json(handler, HTTPStatus.OK, self.export_document())
            return
        raise ControlHTTPError.not_found("unknown control endpoint")

    # ── Administration services (shared by the JSON API and the web UI) ──

    def service_bootstrap_admin(self, *, password: object, confirm: object) -> None:
        """First-run administrator setup; fails once an identity exists."""
        if self.admin_configured():
            raise ControlHTTPError.conflict(
                "an administrator credential is already configured; log in instead"
            )
        checked = validate_admin_password(password)
        if confirm is not True:
            raise ControlHTTPError.invalid_request(
                "administrator setup requires \"confirm\": true"
            )
        salt_hex, hash_hex, iterations = hash_admin_password(
            checked, iterations=self._pbkdf2_iterations
        )
        self._store.set_admin_identity(
            password_salt_hex=salt_hex,
            password_hash_hex=hash_hex,
            pbkdf2_iterations=iterations,
            at=format_utc(self._clock()),
        )

    def service_login(self, document: Mapping[str, object]) -> None:
        """Verify the administrator password; raises 401 on mismatch."""
        identity = self._store.admin_identity()
        if identity is None:
            raise ControlHTTPError.conflict(
                "no administrator credential exists; complete onboarding first"
            )
        password = document.get("password")
        if not isinstance(password, str) or not verify_admin_password(
            password,
            salt_hex=identity.password_salt_hex,
            expected_hash_hex=identity.password_hash_hex,
            iterations=identity.pbkdf2_iterations,
        ):
            raise ControlHTTPError.unauthenticated("invalid credentials")

    def service_logout(self, token: str) -> None:
        self._store.delete_session(hash_token(token))

    def service_add_provider(self, document: Mapping[str, object]) -> str:
        try:
            provider = ProviderEndpointConfig.from_dict(
                {
                    key: value
                    for key, value in document.items()
                    if key in ("provider_id", "adapter_id", "base_url", "label")
                }
            )
        except (ValueError, ServerConfigError) as exc:
            raise ControlHTTPError.invalid_request(str(exc)) from None
        if self._config.provider_by_id(provider.provider_id) is not None:
            raise ControlHTTPError.conflict(
                f"provider {provider.provider_id!r} already exists"
            )
        secret = document.get("secret")
        if secret is not None:
            if not isinstance(secret, str) or not secret:
                raise ControlHTTPError.invalid_request(
                    "secret must be a non-empty string"
                )
            if len(secret) > 4096:
                raise ControlHTTPError.invalid_request("secret is too long")
        if secret is not None:
            self._store.set_provider_secret(
                provider_id=provider.provider_id,
                secret=secret,
                at=format_utc(self._clock()),
            )
        try:
            self._save_config(
                self._updated(providers=self._config.providers + (provider,))
            )
        except (ServerConfigError, ServerStoreError):
            raise
        except ValueError as exc:
            raise ControlHTTPError.invalid_request(str(exc)) from None
        return provider.provider_id

    def service_remove_provider(self, provider_id: str) -> None:
        if self._config.provider_by_id(provider_id) is None:
            raise ControlHTTPError.not_found("unknown provider")
        resources = tuple(
            (
                _with_endpoint(resource, None)
                if resource.endpoint_id == provider_id
                else resource
            )
            for resource in self._config.resources
        )
        _ = self._store.delete_provider_secret(provider_id=provider_id)
        self._save_config(
            self._updated(
                providers=_without(self._config.providers, provider_id),
                resources=resources,
            )
        )

    def service_add_resource(self, document: Mapping[str, object]) -> str:
        try:
            resource = ResourceConfig.from_dict(document)
        except (ValueError, ServerConfigError) as exc:
            raise ControlHTTPError.invalid_request(str(exc)) from None
        resource_id = resource.registration.identity.resource_id
        if self._config.resource_by_id(resource_id) is not None:
            raise ControlHTTPError.conflict(f"resource {resource_id!r} already exists")
        if resource.endpoint_id is not None and (
            self._config.provider_by_id(resource.endpoint_id) is None
        ):
            raise ControlHTTPError.invalid_request(
                "resource references an unknown provider endpoint"
            )
        if resource.worker_id is not None:
            worker = self._worker_identity_store.get_identity(resource.worker_id)
            if worker is None:
                raise ControlHTTPError.invalid_request(
                    "resource references an unknown worker; pair the worker "
                    + "first (workers page)"
                )
            if worker.status != "active":
                raise ControlHTTPError.invalid_request(
                    "resource references a revoked worker"
                )
        try:
            self._save_config(
                self._updated(resources=self._config.resources + (resource,))
            )
        except (ServerConfigError, ServerStoreError):
            raise
        except ValueError as exc:
            # Composition failures (including the compatibility matrix's
            # typed conflicting-evidence CompositionError) are
            # client-classifiable administrator errors with a safe
            # remediation-bearing message — never a bare 500.
            raise ControlHTTPError.invalid_request(str(exc)) from None
        return resource_id

    def service_add_source(self, document: Mapping[str, object]) -> str:
        """Add one execution source (D-053). No model slugs here."""
        try:
            source = SourceConfig.from_dict(document)
        except (ValueError, ServerConfigError) as exc:
            raise ControlHTTPError.invalid_request(str(exc)) from None
        if self._config.source_by_id(source.source_id) is not None:
            raise ControlHTTPError.conflict(
                f"source {source.source_id!r} already exists"
            )
        worker = self._worker_identity_store.get_identity(source.worker_id)
        if worker is None:
            raise ControlHTTPError.invalid_request(
                "source references an unknown worker; pair the worker "
                + "first (workers page)"
            )
        if worker.status != "active":
            raise ControlHTTPError.invalid_request(
                "source references a revoked worker"
            )
        try:
            self._save_config(self._updated(sources=self._config.sources + (source,)))
        except (ServerConfigError, ServerStoreError):
            raise
        except ValueError as exc:
            raise ControlHTTPError.invalid_request(str(exc)) from None
        return source.source_id

    def service_remove_source(self, source_id: str) -> None:
        if self._config.source_by_id(source_id) is None:
            raise ControlHTTPError.not_found("unknown source")
        sources = tuple(
            source for source in self._config.sources if source.source_id != source_id
        )
        self._save_config(self._updated(sources=sources))

    def sources_view(self) -> list[dict[str, object]]:
        """The read-only source view (D-053 point 10): no raw payloads."""
        views = {view["source_id"]: view for view in self._source_registry.source_view()}
        out: list[dict[str, object]] = []
        for source in self._config.sources:
            live = views.get(source.source_id, {})
            out.append(
                {
                    "source_id": source.source_id,
                    "label": source.label or source.source_id,
                    "kind": source.kind,
                    "worker_id": source.worker_id,
                    "auto_adopt": source.auto_adopt,
                    "connected": bool(live.get("connected")),
                    "source_authenticated": live.get(
                        "source_authenticated", "unverified"
                    ),
                    "detected_models": live.get("models", []),
                    "routable": live.get("routable", 0),
                    "restricted": live.get("restricted", 0),
                    "errors": live.get("errors", 0),
                    "retired": live.get("retired", []),
                    "observed_at": live.get("observed_at"),
                    # The worker-side login action is the ONE clear
                    # command; it is printed, never derived by the user.
                    "login_command": (
                        "scarcity-router-worker codex-login --source "
                        + source.source_id
                    ),
                }
            )
        return out

    def service_remove_resource(self, resource_id: str) -> None:
        if self._config.resource_by_id(resource_id) is None:
            raise ControlHTTPError.not_found("unknown resource")
        resources = tuple(
            resource
            for resource in self._config.resources
            if resource.registration.identity.resource_id != resource_id
        )
        self._save_config(self._updated(resources=resources))

    def service_set_resource_enabled(self, resource_id: str, enabled: bool) -> None:
        if self._config.resource_by_id(resource_id) is None:
            raise ControlHTTPError.not_found("unknown resource")
        resources = tuple(
            (
                resource
                if resource.registration.identity.resource_id != resource_id
                else _with_enabled(resource, enabled)
            )
            for resource in self._config.resources
        )
        try:
            self._save_config(self._updated(resources=resources))
        except (ServerConfigError, ServerStoreError):
            raise
        except ValueError as exc:
            # Same classification as service_add_resource: a configuration
            # the composition cannot build (e.g. conflicting matrix
            # evidence re-enabled by this change) is a client-classifiable
            # administrator error, and the stored configuration keeps the
            # previously composed state.
            raise ControlHTTPError.invalid_request(str(exc)) from None

    def service_put_alias(self, alias: str, document: Mapping[str, object]) -> None:
        try:
            _ = v_safe_id(alias, "alias")
        except ValueError:
            raise ControlHTTPError.invalid_request("unsafe alias identifier") from None
        try:
            profile = ClientRoutingProfile.from_dict(dict(document))
        except (ValueError, ServerConfigError) as exc:
            raise ControlHTTPError.invalid_request(str(exc)) from None
        catalog_profiles, _version = load_model_policy(self._model_policy_path)
        known = {definition.profile_id for definition in catalog_profiles.definitions}
        if profile.profile_id not in known:
            raise ControlHTTPError.invalid_request(
                "alias references an unknown profile_id"
            )
        aliases = dict(self._config.aliases)
        aliases[alias] = profile
        self._save_config(self._updated(aliases=aliases))

    def service_delete_alias(self, alias: str) -> None:
        if alias not in self._config.aliases:
            raise ControlHTTPError.not_found("unknown alias")
        aliases = {
            name: profile
            for name, profile in self._config.aliases.items()
            if name != alias
        }
        self._save_config(self._updated(aliases=aliases))

    def service_issue_client_key(self, document: Mapping[str, object]) -> dict[str, object]:
        label = document.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ControlHTTPError.invalid_request("label is required")
        label = label.strip()
        if len(label) > 200:
            raise ControlHTTPError.invalid_request("label is too long")
        client_id = document.get("client_id")
        if client_id is None:
            client_id = f"client-{secrets.token_hex(4)}"
        if not isinstance(client_id, str):
            raise ControlHTTPError.invalid_request("client_id must be a string")
        try:
            _ = v_safe_id(client_id, "client_id")
        except ValueError:
            raise ControlHTTPError.invalid_request("unsafe client_id") from None
        if any(
            record.client_id == client_id for record in self._store.list_client_keys()
        ):
            raise ControlHTTPError.conflict(f"client id {client_id!r} already exists")
        authorization = document.get("authorization")
        if authorization is not None:
            try:
                grant = ClientAuthorization.from_dict(authorization)
            except (ValueError, ServerConfigError) as exc:
                raise ControlHTTPError.invalid_request(str(exc)) from None
            authorizations = dict(self._config.client_authorizations)
            authorizations[client_id] = grant
            self._save_config(self._updated(client_authorizations=authorizations))
        key = new_client_key()
        self._store.add_client_key(
            client_id=client_id,
            key_hash=hash_client_key(key),
            label=label,
            created_at=format_utc(self._clock()),
        )
        self._rebuild_application()
        return {
            "status": "ok",
            "client_id": client_id,
            "api_key": key,
            "note": (
                "store this API key now; it is shown once and only its "
                + "SHA-256 hash is kept"
            ),
        }

    def service_revoke_client_key(self, client_id: str) -> None:
        if not self._store.revoke_client_key(
            client_id=client_id, revoked_at=format_utc(self._clock())
        ):
            raise ControlHTTPError.not_found("unknown client id")
        self._rebuild_application()

    def service_initiate_pairing(self, document: Mapping[str, object]) -> dict[str, object]:
        """Issue one pairing code through the ONE pairing system (M05).

        The code is redeemed inside the worker protocol's verified-TLS
        handshake, which is also when the worker's per-device identity
        (``worker_id`` + credential) is created — so this response names
        no worker id; the workers view shows the device once it has
        paired.
        """
        label = document.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ControlHTTPError.invalid_request("label is required")
        try:
            pairing = self._worker_admin.begin_pairing(label=label.strip())
        except WorkerIdentityError as exc:
            if exc.code == "pairing_unavailable":
                raise ControlHTTPError.conflict(exc.message) from None
            raise ControlHTTPError.invalid_request(exc.message) from None
        except ValueError as exc:
            # The store's bounded-label validation: a client-classifiable
            # administrator error, never a bare 500.
            raise ControlHTTPError.invalid_request(str(exc)) from None
        expires_at = format_utc(
            datetime.fromisoformat(pairing.expires_at[:-1] + "+00:00")
        )
        return {
            "status": "ok",
            "pairing_code": pairing.pairing_code,
            "expires_at": expires_at,
            "note": (
                "enter this one-time code on the worker device together with "
                + "the server's worker-protocol URL; the worker pairs over "
                + "the verified-TLS protocol handshake and receives its "
                + "per-device credential there"
            ),
        }

    def service_rotate_worker_credential(self, worker_id: str) -> dict[str, object]:
        """Rotate one worker's per-device credential (shown once, here)."""
        try:
            credential = self._worker_admin.rotate_worker_credential(worker_id)
        except (WorkerIdentityError, ValueError):
            # ValueError: the store rejected the id as malformed (unknown
            # or malformed worker ids are both a plain 404, never a 500).
            raise ControlHTTPError.not_found("unknown worker") from None
        return {
            "status": "ok",
            "worker_id": worker_id,
            "worker_credential": credential,
            "note": (
                "deliver this new credential to the device now; it is shown "
                + "once and only its salted hash is kept"
            ),
        }

    def service_revoke_worker(self, worker_id: str) -> None:
        """Revoke via the M05 endpoint: identity revoked, sessions closed."""
        try:
            self._worker_endpoint.revoke_worker(worker_id)
        except (WorkerIdentityError, ValueError):
            # ValueError: the store rejected the id as malformed (same
            # mapping as rotation — a plain 404, never a 500).
            raise ControlHTTPError.not_found("unknown worker") from None

    # ── Views ────────────────────────────────────────────────────────────

    def _state_document(self) -> dict[str, object]:
        store_records = self._store.list_client_keys()
        active = sum(1 for record in store_records if record.revoked_at is None)
        return {
            "admin_configured": self.admin_configured(),
            "providers": len(self._config.providers),
            "resources_enabled": sum(
                1 for resource in self._config.resources if resource.enabled
            ),
            "resources_disabled": sum(
                1 for resource in self._config.resources if not resource.enabled
            ),
            "aliases": len(self._config.aliases),
            "client_keys_active": active,
            "client_keys_revoked": len(store_records) - active,
            "workers": len(self._worker_admin.list_workers()),
            "adapters_ready": list(self._adapters.registered_channels()),
            "versions": self._versions(),
            "quota_safety": "control and diagnostic reads never consume inference quota",
        }

    def _versions(self) -> dict[str, object]:
        from . import get_version

        return {
            "scarcity_router": self._version if self._version else get_version(),
            "server_store_schema": self._store.schema_version(),
            "execution_surface": EXECUTION_SURFACE_VERSION,
        }

    def _providers_view(self) -> list[dict[str, object]]:
        return [
            {
                "provider_id": provider.provider_id,
                "adapter_id": provider.adapter_id,
                "base_url": provider.base_url,
                "label": provider.label,
                "credential_configured": self._store.has_provider_secret(
                    provider.provider_id
                ),
            }
            for provider in self._config.providers
        ]

    def _resources_view(self) -> list[dict[str, object]]:
        ladders = {
            ladder.resource_id: ladder for ladder in self.build_diagnostics().resources
        }
        view: list[dict[str, object]] = []
        for resource in self._config.resources:
            resource_id = resource.registration.identity.resource_id
            ladder = ladders.get(resource_id)
            view.append(
                {
                    "resource_id": resource_id,
                    "identity": resource.registration.identity.to_dict(),
                    "enabled": resource.enabled,
                    "endpoint_id": resource.endpoint_id,
                    "worker_id": resource.worker_id,
                    "freshness_ttl_seconds": resource.registration.freshness_ttl_seconds,
                    "ladder": ladder.to_dict() if ladder is not None else None,
                }
            )
        return view

    def _aliases_view(self) -> list[dict[str, object]]:
        return [
            {
                "alias": alias,
                "profile_id": profile.profile_id,
                "allowed_providers": (
                    list(profile.allowed_providers)
                    if profile.allowed_providers is not None
                    else None
                ),
            }
            for alias, profile in sorted(self._config.aliases.items())
        ]

    def _clients_view(self) -> list[dict[str, object]]:
        return [
            {
                "client_id": record.client_id,
                "label": record.label,
                "created_at": record.created_at,
                "revoked_at": record.revoked_at,
                "authorization": (
                    self._config.client_authorizations[record.client_id].to_dict()
                    if record.client_id in self._config.client_authorizations
                    else None
                ),
            }
            for record in self._store.list_client_keys()
        ]

    def _workers_view(self) -> list[dict[str, object]]:
        """The M05 pairing store's state plus the endpoint's live state.

        ``status``/``created_at``/``credential_rotated_at`` come from the
        ONE pairing system (M05's identity store); ``connected`` and
        ``last_connected_at`` come from the worker endpoint's real
        session table and authentication liveness stamps — never from a
        second connection bookkeeping.
        """
        endpoint_state = {
            state.worker_id: state
            for state in self._worker_endpoint.worker_connection_state()
        }
        view: list[dict[str, object]] = []
        for record in self._worker_admin.list_workers():
            state = endpoint_state.get(record.worker_id)
            view.append(
                {
                    "worker_id": record.worker_id,
                    "label": record.label,
                    "status": record.status,
                    "created_at": record.created_at,
                    "credential_rotated_at": record.credential_rotated_at,
                    "connected": state.connected if state is not None else False,
                    "last_connected_at": (
                        state.last_connected_at if state is not None else None
                    ),
                }
            )
        return view

    # ── Public accessors used by the web UI ──────────────────────────────

    @property
    def is_tls(self) -> bool:
        return self._tls

    def create_session_cookie(self) -> str:
        return self._create_admin_session()

    def clear_session_cookie(self) -> str:
        return self._clear_session_cookie()

    def state_document(self) -> dict[str, object]:
        return self._state_document()

    def providers_view(self) -> list[dict[str, object]]:
        return self._providers_view()

    def resources_view(self) -> list[dict[str, object]]:
        return self._resources_view()

    def aliases_view(self) -> list[dict[str, object]]:
        return self._aliases_view()

    def clients_view(self) -> list[dict[str, object]]:
        return self._clients_view()

    def workers_view(self) -> list[dict[str, object]]:
        return self._workers_view()

    def connection_test_document(self, resource_id: str) -> dict[str, object]:
        return self._connection_test_document(resource_id)

    def known_profile_ids(self) -> tuple[str, ...]:
        """Profile ids an alias may bind (from the authoritative artifact)."""
        profiles, _version = load_model_policy(self._model_policy_path)
        return tuple(
            sorted({definition.profile_id for definition in profiles.definitions})
        )

    def import_client_key_hash(
        self,
        *,
        client_id: str,
        key_hash: str,
        label: str,
        created_at: str,
    ) -> bool:
        """One-time migration seam (``--import-client-keys``).

        Copies one pre-hashed client key into the store; existing ids are
        kept (``False``) so an import can never overwrite store state.
        """
        if any(
            record.client_id == client_id for record in self._store.list_client_keys()
        ):
            return False
        self._store.add_client_key(
            client_id=client_id,
            key_hash=key_hash,
            label=label,
            created_at=created_at,
        )
        self._rebuild_application()
        return True

    # ── Connection and generation tests ──────────────────────────────────

    def _connection_test_document(self, resource_id: str) -> dict[str, object]:
        """The quota-free connection test over real seam data.

        Configuration checks come from the store and the ONE pairing
        system; when an M04 provider binding exists and its preset
        evidences a health endpoint, the test additionally probes it
        through the adapter's read-only ``probe_health`` (never an
        inference request, never quota). Anything not wired here stays
        honestly absent from the checks rather than reported fake-green.
        """
        resource = self._config.resource_by_id(resource_id)
        if resource is None:
            raise ControlHTTPError.not_found("unknown resource")
        checks: list[dict[str, object]] = []
        identity = resource.registration.identity

        def record(name: str, passed: bool, detail: str, remediation: str) -> None:
            checks.append(
                {
                    "check": name,
                    "passed": passed,
                    "detail": detail,
                    "remediation": None if passed else remediation,
                }
            )

        record(
            "enabled",
            resource.enabled,
            "resource is enabled" if resource.enabled else "resource is disabled",
            "enable the resource before testing",
        )
        probed = False
        if identity.channel in ("worker_bridged", "local_app_adapter"):
            worker = (
                self._worker_identity_store.get_identity(resource.worker_id)
                if resource.worker_id is not None
                else None
            )
            paired = worker is not None and worker.status == "active"
            record(
                "worker_paired",
                paired,
                "an active worker identity is bound"
                if paired
                else "no active worker identity is bound",
                "pair a worker and bind it to this resource",
            )
            connected = resource.worker_id is not None and any(
                state.worker_id == resource.worker_id
                for state in self._worker_endpoint.worker_connection_state()
                if state.connected
            )
            record(
                "worker_connected",
                connected,
                "the bound worker is connected"
                if connected
                else "the bound worker is not connected",
                "start the worker runtime; it connects out to this server "
                + "(worker-protocol listener)",
            )
        else:
            bound = resource.endpoint_id is not None
            record(
                "endpoint_bound",
                bound,
                "a provider endpoint is bound" if bound else "no endpoint is bound",
                "add a provider endpoint and bind it to this resource",
            )
            credential_required = True
            if bound and resource.endpoint_id is not None:
                provider = self._config.provider_by_id(resource.endpoint_id)
                from .providers.openai_http_presets import preset_by_id

                preset = (
                    preset_by_id(provider.adapter_id)
                    if provider is not None
                    else None
                )
                if preset is not None:
                    credential_required = preset.policy.requires_credential
            stored = (
                resource.endpoint_id is not None
                and self._store.has_provider_secret(resource.endpoint_id)
            )
            if credential_required or stored:
                record(
                    "credential_present",
                    stored,
                    "a credential is stored for the endpoint"
                    if stored
                    else "no credential is stored for the endpoint",
                    "set the provider credential through the providers page",
                )
            if bound and identity.channel == "server_direct_http":
                probed, probe_result = self._probe_provider_health(resource, record)
                self._record_probe_observation(resource, probe_result)
        passed = all(bool(check["passed"]) for check in checks)
        return {
            "resource_id": resource_id,
            "result": "passed" if passed else "failed",
            "checks": checks,
            "quota_consumed": False,
            "note": (
                "quota-free test over the configured seams (the M05 worker "
                + "endpoint or the M04 adapter's read-only health probe); "
                + "never an inference request"
                if probed
                else (
                    "quota-free test; no readiness probe ran for this "
                    + "resource (no adapter is composed for its channel), "
                    + "so no provider request was made"
                    if identity.channel == "server_direct_http"
                    and resource.endpoint_id is not None
                    else (
                        "configuration-level test only: no provider request "
                        + "was made and no inference quota was consumed"
                    )
                )
            ),
        }

    def _probe_provider_health(
        self,
        resource: ResourceConfig,
        record: Callable[[str, bool, str, str], None],
    ) -> tuple[bool, "HealthProbeResult | None"]:
        """Run the M04 adapter's quota-free readiness probe when possible.

        Returns ``(ran, result)``: a probe actually ran (its check is
        appended and the outcome is recorded as an observation by the
        caller) versus the states where nothing was probed — unbound,
        no adapter composed for the channel. Those stay honestly absent
        from the check list rather than reported fake-green.
        """
        adapter = self._adapters.resolve("server_direct_http")
        if not isinstance(adapter, OpenAICompatibleHttpAdapter):
            return False, None
        result = adapter.probe_health(
            resource.registration.identity.resource_id
        )
        if result.status == "unsupported_preset":
            return False, None
        if result.status == "refused":
            record(
                "provider_health",
                False,
                result.note or "no provider binding is configured",
                "bind a provider endpoint (with the M04 preset) to this "
                + "resource",
            )
            return True, result
        passed = result.status == "ok"
        record(
            "provider_health",
            passed,
            result.note or f"health probe status: {result.status}",
            "verify the endpoint's origin, credential and reachability",
        )
        return True, result

    def _run_generation_test(self, resource_id: str) -> dict[str, object]:
        resource = self._config.resource_by_id(resource_id)
        if resource is None:
            raise ControlHTTPError.not_found("unknown resource")
        if self._generation_tester is None:
            raise ControlHTTPError.conflict(
                "no execution adapter can serve a generation test for this "
                + "resource yet; configure the resource's execution adapter "
                + "(generic HTTP adapter or worker transport) first"
            )
        return self._generation_tester(resource)

    # ── Diagnostics and export ───────────────────────────────────────────

    def build_diagnostics(self) -> DiagnosticsReport:
        """The shared diagnostics report (UI page and control JSON)."""
        from . import get_version

        active_keys = [
            record
            for record in self._store.list_client_keys()
            if record.revoked_at is None
        ]
        connection_state = {
            state.worker_id: state
            for state in self._worker_endpoint.worker_connection_state()
        }
        return collect_server_diagnostics(
            ServerDiagnosticsInputs(
                configuration=self._config,
                registry_snapshot=(
                    self.current_application().registry.registry_snapshot()
                ),
                constraints=self._config.admin_constraints,
                paired_worker_ids=frozenset(
                    record.worker_id
                    for record in self._worker_admin.list_workers()
                    if record.status == "active"
                ),
                channels_with_adapters=frozenset(self._adapters.registered_channels()),
                endpoints_with_credentials=frozenset(
                    provider.provider_id
                    for provider in self._config.providers
                    if self._store.has_provider_secret(provider.provider_id)
                ),
                pairings=tuple(
                    {
                        "worker_id": record.worker_id,
                        "label": record.label,
                        "status": record.status,
                        "connected": (
                            connection_state[record.worker_id].connected
                            if record.worker_id in connection_state
                            else False
                        ),
                        "last_connected_at": (
                            connection_state[record.worker_id].last_connected_at
                            if record.worker_id in connection_state
                            else None
                        ),
                    }
                    for record in self._worker_admin.list_workers()
                ),
                store_schema_version=self._store.schema_version(),
                store_error=None,
                admin_configured=self.admin_configured(),
                active_client_keys=len(active_keys),
                revoked_client_keys=len(self._store.list_client_keys())
                - len(active_keys),
                now=self._clock(),
                version=self._version if self._version else get_version(),
                provider_endpoints=self._config.providers,
            )
        )

    def export_document(self) -> dict[str, object]:
        """The secret-free configuration export (single source of truth).

        The configuration document never contains secrets, so the export
        is safe by construction: provider credentials live only in the
        store's dedicated table and are referenced by provider id; client
        keys and worker tokens exist only as hashes and are not exported.
        """
        return {
            "schema_version": 1,
            "generated_at": format_utc(self._clock()),
            "configuration": self._config.to_document(),
            "versions": self._versions(),
            "secret_policy": (
                "this export never contains provider credentials, client "
                + "keys or worker tokens"
            ),
        }

    # ── Configuration persistence and application rebuild ────────────────

    def _updated(
        self,
        *,
        providers: tuple[ProviderEndpointConfig, ...] | None = None,
        resources: tuple[ResourceConfig, ...] | None = None,
        sources: tuple[SourceConfig, ...] | None = None,
        aliases: Mapping[str, ClientRoutingProfile] | None = None,
        client_authorizations: Mapping[str, ClientAuthorization] | None = None,
        admin_constraints: AdministratorConstraints | None = None,
        limits: GatewayLimits | None = None,
        audit_retention: AuditRetention | None = None,
        pairing_code_ttl_seconds: int | None = None,
        session_ttl_seconds: int | None = None,
    ) -> ServerConfiguration:
        """One configuration record with the given fields replaced."""
        return ServerConfiguration(
            providers=(
                self._config.providers if providers is None else providers
            ),
            resources=(
                self._config.resources if resources is None else resources
            ),
            sources=(self._config.sources if sources is None else sources),
            aliases=self._config.aliases if aliases is None else aliases,
            client_authorizations=(
                self._config.client_authorizations
                if client_authorizations is None
                else client_authorizations
            ),
            admin_constraints=(
                self._config.admin_constraints
                if admin_constraints is None
                else admin_constraints
            ),
            limits=self._config.limits if limits is None else limits,
            audit_retention=(
                self._config.audit_retention
                if audit_retention is None
                else audit_retention
            ),
            pairing_code_ttl_seconds=(
                self._config.pairing_code_ttl_seconds
                if pairing_code_ttl_seconds is None
                else pairing_code_ttl_seconds
            ),
            session_ttl_seconds=(
                self._config.session_ttl_seconds
                if session_ttl_seconds is None
                else session_ttl_seconds
            ),
        )

    def _save_config(self, config: ServerConfiguration) -> None:
        """Validate, apply, then persist one configuration version.

        Every check runs BEFORE any state mutation so a configuration the
        composition cannot build (unknown preset, unparseable origin,
        unknown worker, conflicting compatibility evidence for one frozen
        D-043 key) is rejected as a client-classifiable error, never
        reaches the durable store and never half-applies in memory: what
        passed composition here will pass again on restart, and the
        previously composed application keeps serving untouched.
        """
        config.validate_no_router_loop(self._own_origins)
        validate_execution_configuration(
            config,
            worker_id_exists=self._worker_id_exists,
        )
        # The compatibility matrix is composed from the candidate
        # configuration ahead of the rebuild: its typed
        # CompositionError (conflicting evidence for one D-043 key)
        # must abort the save before self._config changes, or a
        # rejected configuration would poison every later save.
        _ = build_compatibility_cells(
            config, provider_secret_reader=self._store.get_provider_secret
        )
        self._config = config
        self._sink.update_retention(config.audit_retention)
        self._rebuild_application()
        self._store.save_configuration_document(
            config.to_document(), at=format_utc(self._clock())
        )

    def _worker_id_exists(self, worker_id: str) -> bool:
        """Whether the M05 pairing store knows this worker device."""
        return self._worker_identity_store.get_identity(worker_id) is not None

    def _rebuild_application(self) -> None:
        """Rebuild the GatewayApplication from the current configuration.

        This is the seam between administrator configuration and the M03
        coordinator: routing artifacts load through the shared application
        loader, enabled resources are (re-)registered, observations for
        still-registered resources are re-applied, both authorization
        layers plus the client-key directory come from the authoritative
        configuration, and the execution adapters AND the compatibility
        matrix are composed from that same configuration through
        :mod:`scarcity_router.server_composition` (M04 HTTP adapter from
        provider endpoints plus store-held credentials, with each bound
        resource's evidenced preset cells; M05 worker-bridged adapter
        from resource→worker bindings, with the reviewed M06 Codex
        evidence cells keyed to each Codex resource's physical model). No
        selection or routing logic exists here.
        """
        catalog, profiles, profile_policy_version = load_configured_artifacts(
            self._catalog_path, self._model_policy_path
        )
        # D-053: source-derived state participates like configured
        # registrations — the derived set is recomputed from the latest
        # inventories, then the catalog view gains the conservative
        # track-floor entries for adopted models.
        self._source_registry.sync_configuration(self._config.sources)
        catalog = self._source_registry.derived_catalog_entries(catalog)
        registry = ResourceRegistry(clock=self._registry_clock)
        for registration in self._config.enabled_registrations():
            resource_id = registration.identity.resource_id
            registry.register(registration)
            observation = self._observations.get(resource_id)
            if observation is not None:
                registry.apply_snapshot(observation)
        for registration in self._source_registry.derived_registrations():
            resource_id = registration.identity.resource_id
            registry.register(registration)
            observation = self._observations.get(resource_id)
            if observation is not None:
                registry.apply_snapshot(observation)
        directory = ClientKeyDirectory(self._store.active_client_key_hashes())
        policy = self._default_policy()
        self._adapters = build_adapter_registry(
            self._config,
            provider_secret_reader=self._store.get_provider_secret,
            worker_endpoint=self._worker_endpoint,
            source_registry=self._source_registry,
        )
        self._application = GatewayApplication(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=profile_policy_version,
            policy=policy,
            registry=registry,
            capacity_source=self._capacity_source,
            compatibility_cells=build_compatibility_cells(
                self._config,
                provider_secret_reader=self._store.get_provider_secret,
                source_registry=self._source_registry,
            ),
            admin_constraints=self._config.admin_constraints,
            aliases=RoutingAliasTable(dict(self._config.aliases)),
            adapters=self._adapters,
            audit=self._sink,
            limits=self._config.limits,
            client_key_directory=directory,
            client_authorizations=self._config.client_authorizations,
            clock=self._clock,
        )

    @staticmethod
    def _capacity_source(
        now: str,
    ) -> tuple[tuple[CapacitySnapshot, ...], tuple[ExecutionEligibility, ...]]:
        """Honest empty execution-state source until real collection lands.

        The M03 deployment default: the execution-side registry reports
        what the administrator configured and nothing more; execution
        adapters (M04/M05) supply real observations later. The
        recommendation-side collectors are a separate, independent seam.
        """
        _ = now
        return ((), ())

    # ── Observation seam (collectors / M05 transport) ────────────────────

    def apply_resource_observation(self, snapshot: ResourceStateSnapshot) -> None:
        """Record one normalized observation (collector or M05 transport).

        The observation is retained across configuration rebuilds and
        applied to the live registry. Observations can never redefine a
        resource: the registry fails closed on unregistered or mismatched
        identities.
        """
        self._observations[snapshot.identity.resource_id] = snapshot
        self.current_application().registry.apply_snapshot(snapshot)

    # ── Server-direct readiness observations (M01 refresh seam) ──────────

    #: Probe outcome → resource-health status. ``refused`` and
    #: ``unsupported_preset`` map to nothing: nothing was observed, and
    #: the resource stays honestly unobserved.
    _PROBE_OUTCOME_TO_HEALTH: Mapping[str, str] = {
        "ok": "ok",
        "auth_rejected": "auth_required",
        "unreachable": "unavailable",
        "schema_drift": "schema_changed",
    }

    def _record_probe_observation(
        self,
        resource: ResourceConfig,
        result: "HealthProbeResult | None",
    ) -> None:
        """Record one readiness probe as the resource's observation.

        The M01 contract deliberately carries no background refresh: the
        registry exposes ``refresh_due`` and the server's request loop
        (and the connection test) observe due resources through this
        seam. Non-``ok`` outcomes are recorded honestly — an endpoint
        that rejects the credential, answers 404 on the documented path
        or cannot be reached is EXACTLY the state the acceptance ladder
        and execution admission must see. A probe that could not run
        changes nothing.
        """
        if result is None:
            return
        health_status = self._PROBE_OUTCOME_TO_HEALTH.get(result.status)
        if health_status is None:
            return
        try:
            self.apply_resource_observation(
                ResourceStateSnapshot(
                    schema_version=RESOURCE_STATE_SCHEMA_VERSION,
                    identity=resource.registration.identity,
                    observed_at=format_utc(self._clock()),
                    health=_health_from_probe(health_status),
                )
            )
        except Exception:
            # An unusable probe outcome (store/config race) must never
            # break the surface that ran it; the resource simply stays
            # at its previous observation state.
            return

    def prepare_execution_admission(self) -> None:
        """Observe due server-direct resources before execution admission.

        Called by the execution server's request loop (the M01
        ``refresh_due`` contract): every enabled, bound, composed
        ``server_direct_http`` resource whose polling cadence says it is
        due is probed once through the M04 adapter's quota-free readiness
        probe and the outcome is recorded as its observation. A resource
        that was never observed therefore becomes observable on the
        documented first-run path instead of being permanently
        ``target_unavailable``. Never runs an inference request and never
        raises: probing problems degrade to the previous observation
        state.
        """
        try:
            registry = self.current_application().registry
            now = format_utc(self._clock())
            due = registry.refresh_due(now=now)
        except Exception:
            return
        adapter = self._adapters.resolve("server_direct_http")
        if not isinstance(adapter, OpenAICompatibleHttpAdapter):
            return
        for resource_id in due:
            resource = self._config.resource_by_id(resource_id)
            if (
                resource is None
                or not resource.enabled
                or resource.registration.identity.channel != "server_direct_http"
            ):
                continue
            try:
                result = adapter.probe_health(resource_id)
            except Exception:
                continue
            self._record_probe_observation(resource, result)


#: Non-``ok`` resource-health statuses require their status-level
#: diagnostic code (the capacity v3 mapping); probe-derived observations
#: carry exactly that one diagnostic.
_PROBE_HEALTH_REQUIRED_DIAGNOSTIC: Mapping[str, str] = {
    "unavailable": "source_unavailable",
    "auth_required": "auth_required",
    "schema_changed": "schema_changed",
}


def _health_from_probe(health_status: str) -> ResourceHealth:
    required = _PROBE_HEALTH_REQUIRED_DIAGNOSTIC.get(health_status)
    diagnostics = (
        (CapacityDiagnostic(code=required),) if required is not None else ()
    )
    return ResourceHealth(status=health_status, diagnostics=diagnostics)


# ── Small configuration-record helpers ────────────────────────────────────────

def _with_enabled(resource: ResourceConfig, enabled: bool) -> ResourceConfig:
    return ResourceConfig(
        registration=_registration_copy(resource.registration),
        enabled=enabled,
        endpoint_id=resource.endpoint_id,
        worker_id=resource.worker_id,
    )


def _with_endpoint(resource: ResourceConfig, endpoint_id: str | None) -> ResourceConfig:
    return ResourceConfig(
        registration=_registration_copy(resource.registration),
        enabled=resource.enabled,
        endpoint_id=endpoint_id,
        worker_id=resource.worker_id,
    )


def _registration_copy(
    registration: ResourceRegistration,
) -> ResourceRegistration:
    from .resource_state import ExecutionCapabilities, ResourceCost, ResourceIdentity

    return ResourceRegistration(
        identity=ResourceIdentity(
            resource_id=registration.identity.resource_id,
            channel=registration.identity.channel,
            provider=registration.identity.provider,
            model=registration.identity.model,
            entitlement=registration.identity.entitlement,
            variant=registration.identity.variant,
            quota_pool_ids=registration.identity.quota_pool_ids,
        ),
        freshness_ttl_seconds=registration.freshness_ttl_seconds,
        poll_interval_seconds=registration.poll_interval_seconds,
        capabilities=ExecutionCapabilities(
            streaming=registration.capabilities.streaming,
            tool_calls=registration.capabilities.tool_calls,
            structured_output=registration.capabilities.structured_output,
            reasoning_controls=registration.capabilities.reasoning_controls,
            usage_reporting=registration.capabilities.usage_reporting,
            cancellation=registration.capabilities.cancellation,
            context_limit_tokens=registration.capabilities.context_limit_tokens,
        ),
        cost=(
            ResourceCost(
                observation_class=registration.cost.observation_class,
                input_micro_usd_per_mtoken=(
                    registration.cost.input_micro_usd_per_mtoken
                ),
                output_micro_usd_per_mtoken=(
                    registration.cost.output_micro_usd_per_mtoken
                ),
            )
            if registration.cost is not None
            else None
        ),
    )


def _without(
    providers: tuple[ProviderEndpointConfig, ...], provider_id: str
) -> tuple[ProviderEndpointConfig, ...]:
    return tuple(
        provider for provider in providers if provider.provider_id != provider_id
    )


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ADMIN_PREFIX",
    "CONTROL_PREFIX",
    "CSRF_FORM_FIELD",
    "CSRF_HEADER_NAME",
    "ControlHTTPError",
    "ControlPlane",
    "DurableAuditSink",
    "MACHINE_SELECT_PATH",
    "MACHINE_SIMULATE_PATH",
    "MACHINE_STATUS_PATH",
    "SESSION_COOKIE_NAME",
    "format_utc",
    "hash_admin_password",
    "hash_token",
    "new_client_key",
    "new_session_token",
    "validate_admin_password",
    "verify_admin_password",
]
