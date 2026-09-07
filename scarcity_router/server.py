"""Minimal local REST adapter (M3b, D-028/D-029).

A thin, loopback-only HTTP transport over the same application/core the CLI
uses. The adapter owns no selection, scarcity, provider, capacity or policy
semantics: it parses transport input, calls the shared typed application
seam (``selection_app.select_from_inputs`` / ``simulate_from_inputs`` /
``collect_status``) and serializes the existing typed results inside the
frozen machine-interface envelopes of ``docs/machine-interfaces.md``.

Implementation choice (D-029): the Python standard library
``HTTPServer``/``BaseHTTPRequestHandler`` only. ``HTTPServer`` is
deliberately single-threaded, so requests are serialized by construction —
one request, one application invocation, the existing synchronous provider
collection — which satisfies the D-028 concurrency requirement without
locks. No framework, no authentication, no non-loopback bind option, no
chunked request bodies, no request-body logging and no runtime dependency.

Frozen surface: exactly ``GET /healthz``, ``GET /v1/status``,
``POST /v1/select``, ``POST /v1/simulate``. Client-input failures are
classified by the typed ``ApplicationInputError`` boundary (never by
matching exception message text) and become HTTP 400 ``invalid_request``;
application/internal failures become HTTP 500 ``internal_error`` with a
fixed safe message; unknown paths and unsupported methods are transport
routing responses (404/405). Provider degradation stays normalized domain
data in HTTP 200 responses. Valid no-solution decisions are HTTP 200.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar, TypeVar, cast, override

from .errors import ApplicationInputError, SelectionContractError
from .policy import ReplenishmentState
from .selector import SelectorPolicy
from .selection_app import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_MODEL_POLICY_PATH,
    load_configured_artifacts,
    load_strict_json,
    select_from_inputs,
    simulate_from_inputs,
)
from .selection_types import TaskRequirement
from .simulation import SimulationOverrides
from .status import (
    Clock,
    StatusCollectors,
    canonical_snapshot_documents,
    collect_status,
)

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ENVELOPE_SCHEMA_VERSION = 1
MAX_REQUEST_BODY_BYTES = 1_048_576


@dataclass(frozen=True)
class RestApplication:
    """Server-side application dependencies; never client-supplied.

    Catalog and model-policy paths are process configuration (D-028):
    clients never supply artifact paths, provider endpoints or credentials.
    The collector and clock seams exist for deterministic tests only; in
    production they stay ``None`` so the real collectors and clock are used.
    """

    catalog_path: Path = DEFAULT_CATALOG_PATH
    model_policy_path: Path = DEFAULT_MODEL_POLICY_PATH
    collectors: StatusCollectors | None = None
    clock: Clock | None = None


type _Response = tuple[HTTPStatus, dict[str, object]]


class RestHTTPServer(HTTPServer):
    """Single-threaded loopback-only server (D-029).

    ``HTTPServer`` handles one request at a time, so application and
    provider collection are serialized by construction. The bind address is
    fixed to :data:`BIND_HOST` and is not configurable.
    """

    application: RestApplication

    def __init__(self, *, port: int, application: RestApplication) -> None:
        super().__init__((BIND_HOST, port), RestRequestHandler)
        self.application = application

    @override
    def handle_error(self, request: object, client_address: object) -> None:
        """Stay quiet on broken client connections; never dump tracebacks."""
        _ = request, client_address
        print("server: request handling failed", file=sys.stderr)


_PATH_METHODS: Mapping[str, str] = {
    "/healthz": "GET",
    "/v1/status": "GET",
    "/v1/select": "POST",
    "/v1/simulate": "POST",
}


class RestRequestHandler(BaseHTTPRequestHandler):
    """Dispatches the frozen D-028 surface; no framework routing magic."""

    # Do not disclose interpreter versions on a local machine interface.
    server_version: str = "scarcity-router"
    sys_version: str = ""
    # Bound a stalled client so the single-threaded server cannot hang
    # forever on a socket read; provider collection time is unaffected.
    timeout: ClassVar[float | None] = 60
    close_connection: bool = True

    def _rest_application(self) -> RestApplication:
        server = cast(RestHTTPServer, self.server)
        return server.application

    # ── Transport dispatch ────────────────────────────────────────────────

    def _dispatch(self) -> None:
        """Route one request with the frozen 400/500 error boundary."""
        try:
            self._process()
        except ApplicationInputError as exc:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST, "invalid_request", str(exc)
            )
        except Exception:
            # Fail closed: a fixed structural message only — never exception
            # details, local paths, provider payloads or a traceback (D-028).
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "internal server error",
            )

    def _process(self) -> None:
        route = _ROUTES.get((self.command, self.path))
        if route is not None:
            status, payload = route(self)
            self._send_json(status, payload)
            return
        allow = _PATH_METHODS.get(self.path)
        if allow is not None:
            self._send_empty(HTTPStatus.METHOD_NOT_ALLOWED, allow=allow)
            return
        # Exact-match routing: query or fragment components are not part of
        # the frozen v1 surface, so such paths match nothing and 404.
        self._send_empty(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    @override
    def send_error(
        self,
        code: int | HTTPStatus,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace framework HTML error pages with safe empty responses.

        Framework-generated error bodies reflect request material (method,
        path) as HTML; this surface prefers a body-less status response.
        """
        _ = message, explain
        self.close_connection = True
        self._send_empty(HTTPStatus(int(code)))

    @override
    def log_message(self, format: str, *args: object) -> None:
        """Quiet by design: no request logging, hence no body logging."""
        _ = format, args

    # ── Response serialization ────────────────────────────────────────────

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        _ = self.wfile.write(encoded)

    def _send_error_json(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_empty(self, status: HTTPStatus, *, allow: str | None = None) -> None:
        self.send_response(status.value)
        if allow is not None:
            self.send_header("Allow", allow)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── Request body handling (PHASE 7) ───────────────────────────────────

    def _read_json_object(self) -> dict[str, object]:
        document = self._read_json_document()
        if not isinstance(document, dict):
            raise ApplicationInputError("request body must be a JSON object")
        return cast("dict[str, object]", document)

    def _read_json_document(self) -> object:
        # Read the declared body first so a rejected request leaves no
        # unread bytes behind; the client can always read the 400.
        body = self._read_body()
        self._require_json_content_type()
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ApplicationInputError(
                "request body is not valid UTF-8"
            ) from None
        try:
            return load_strict_json(text, label="request body")
        except ValueError as exc:
            raise ApplicationInputError(str(exc)) from exc

    def _read_body(self) -> bytes:
        if self.headers.get_all("Transfer-Encoding"):
            raise ApplicationInputError("transfer-encoding is not supported")
        lengths = self.headers.get_all("Content-Length")
        if lengths is None or len(lengths) != 1:
            raise ApplicationInputError(
                "exactly one content-length header is required"
            )
        raw_length = lengths[0]
        if not _is_decimal(raw_length):
            raise ApplicationInputError("content-length must be a decimal integer")
        length = int(raw_length.strip())
        if length > MAX_REQUEST_BODY_BYTES:
            # Bounded best-effort drain of the oversized declared body so
            # the connection closes without an unread-data TCP reset and
            # the client can still read the 400. Never stored or logged.
            self._drain_bounded(length)
            raise ApplicationInputError(
                "request body exceeds the maximum request size"
            )
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                raise ApplicationInputError("incomplete request body")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    _MAX_DRAIN_BYTES: ClassVar[int] = 4 * MAX_REQUEST_BODY_BYTES

    def _drain_bounded(self, remaining: int) -> None:
        remaining = min(remaining, self._MAX_DRAIN_BYTES)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            return

    def _require_json_content_type(self) -> None:
        header = self.headers.get("Content-Type")
        if header is None:
            raise ApplicationInputError("content-type application/json is required")
        parameters = [part.strip() for part in header.lower().split(";")]
        if parameters[0] != "application/json":
            raise ApplicationInputError("content-type must be application/json")
        for parameter in parameters[1:]:
            if parameter != "charset=utf-8":
                raise ApplicationInputError(
                    "only the charset=utf-8 parameter is accepted"
                )

    # ── Frozen operations ─────────────────────────────────────────────────

    def route_healthz(self) -> _Response:
        """Liveness only; deliberately touches no application component."""
        return HTTPStatus.OK, {"status": "ok"}

    def route_status(self) -> _Response:
        application = self._rest_application()
        snapshots = collect_status(
            collectors=application.collectors, clock=application.clock
        )
        return HTTPStatus.OK, {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "snapshots": canonical_snapshot_documents(snapshots),
        }

    def route_select(self) -> _Response:
        document = self._read_json_object()
        parsed = _parse_selection_fields(document)
        application = self._rest_application()
        catalog, profiles, profile_policy_version = load_configured_artifacts(
            application.catalog_path, application.model_policy_path
        )
        decision = select_from_inputs(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=profile_policy_version,
            profile_id=parsed.profile_id,
            requirement=parsed.requirement,
            tightening=parsed.tightening,
            policy=parsed.policy,
            replenishment_states=parsed.replenishment_states,
            collectors=application.collectors,
            clock=application.clock,
        )
        return HTTPStatus.OK, {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "decision": decision.to_dict(),
        }

    def route_simulate(self) -> _Response:
        document = self._read_json_object()
        parsed, overrides = _parse_simulation_fields(document)
        application = self._rest_application()
        catalog, profiles, profile_policy_version = load_configured_artifacts(
            application.catalog_path, application.model_policy_path
        )
        result = simulate_from_inputs(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=profile_policy_version,
            profile_id=parsed.profile_id,
            requirement=parsed.requirement,
            tightening=parsed.tightening,
            policy=parsed.policy,
            replenishment_states=parsed.replenishment_states,
            overrides=overrides,
            collectors=application.collectors,
            clock=application.clock,
        )
        return HTTPStatus.OK, {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "result": result.to_dict(),
        }


_ROUTES: Mapping[tuple[str, str], Callable[[RestRequestHandler], _Response]] = {
    ("GET", "/healthz"): RestRequestHandler.route_healthz,
    ("GET", "/v1/status"): RestRequestHandler.route_status,
    ("POST", "/v1/select"): RestRequestHandler.route_select,
    ("POST", "/v1/simulate"): RestRequestHandler.route_simulate,
}


# ── Client-input parsing (PHASE 10/11 semantics) ─────────────────────────────


@dataclass(frozen=True)
class _ParsedSelectionInputs:
    """Client-parsed selection inputs; artifact inputs stay server-side."""

    profile_id: str | None
    requirement: TaskRequirement | None
    tightening: TaskRequirement | None
    policy: SelectorPolicy | None
    replenishment_states: tuple[ReplenishmentState, ...]


_SELECT_REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "profile_id",
        "requirement",
        "tightening",
        "selector_policy",
        "replenishment_states",
    }
)
_SIMULATE_REQUEST_KEYS: frozenset[str] = _SELECT_REQUEST_KEYS | {"overrides"}

_T = TypeVar("_T")


def _parse_client_object(
    from_dict: Callable[[object], _T], value: object, field: str
) -> _T:
    """Run one typed ``from_dict`` boundary, classifying failures by type."""
    try:
        return from_dict(value)
    except (ValueError, SelectionContractError) as exc:
        raise ApplicationInputError(f"invalid {field}: {exc}") from exc


def _reject_unknown_keys(
    document: Mapping[str, object], allowed: frozenset[str]
) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        listed = ", ".join(repr(key) for key in unknown)
        raise ApplicationInputError(f"unknown request key(s): {listed}")


def _parse_common_selection_fields(
    document: Mapping[str, object],
) -> _ParsedSelectionInputs:
    """Parse the frozen missing/explicit-null field semantics (D-028)."""
    raw_profile = document.get("profile_id")
    if raw_profile is not None and not isinstance(raw_profile, str):
        raise ApplicationInputError("profile_id must be a string or null")
    raw_requirement = document.get("requirement")
    raw_tightening = document.get("tightening")
    raw_policy = document.get("selector_policy")
    return _ParsedSelectionInputs(
        profile_id=raw_profile,
        requirement=(
            _parse_client_object(
                TaskRequirement.from_dict, raw_requirement, "requirement"
            )
            if raw_requirement is not None
            else None
        ),
        tightening=(
            _parse_client_object(
                TaskRequirement.from_dict, raw_tightening, "tightening"
            )
            if raw_tightening is not None
            else None
        ),
        policy=(
            _parse_client_object(
                SelectorPolicy.from_dict, raw_policy, "selector_policy"
            )
            if raw_policy is not None
            else None
        ),
        replenishment_states=_parse_replenishment_states(document),
    )


def _parse_selection_fields(
    document: Mapping[str, object],
) -> _ParsedSelectionInputs:
    _reject_unknown_keys(document, _SELECT_REQUEST_KEYS)
    return _parse_common_selection_fields(document)


def _parse_simulation_fields(
    document: Mapping[str, object],
) -> tuple[_ParsedSelectionInputs, SimulationOverrides]:
    _reject_unknown_keys(document, _SIMULATE_REQUEST_KEYS)
    return _parse_common_selection_fields(document), _parse_overrides(document)


def _parse_overrides(document: Mapping[str, object]) -> SimulationOverrides:
    if "overrides" not in document:
        raise ApplicationInputError("overrides is required")
    raw = document["overrides"]
    if not isinstance(raw, dict):
        raise ApplicationInputError("overrides must be a JSON object")
    return _parse_client_object(
        SimulationOverrides.from_dict, cast("dict[str, object]", raw), "overrides"
    )


def _parse_replenishment_states(
    document: Mapping[str, object],
) -> tuple[ReplenishmentState, ...]:
    if "replenishment_states" not in document:
        return ()
    raw = document["replenishment_states"]
    if raw is None:
        # Frozen boundary semantics: the top-level field is an array; the
        # baseline-replacement tri-state exists only inside simulation
        # overrides (D-028).
        raise ApplicationInputError(
            "replenishment_states must be an array, not null"
        )
    if not isinstance(raw, list):
        raise ApplicationInputError("replenishment_states must be an array")
    states: list[ReplenishmentState] = []
    seen: set[tuple[str, str]] = set()
    for item in cast("list[object]", raw):
        state = _parse_client_object(
            ReplenishmentState.from_dict, item, "replenishment_states entry"
        )
        key = (state.provider, state.kind)
        if key in seen:
            raise ApplicationInputError(
                f"duplicate replenishment state for (provider, kind) {key}"
            )
        seen.add(key)
        states.append(state)
    return tuple(states)


def _is_decimal(value: str) -> bool:
    text = value.strip(" \t")
    return bool(text) and all(character in "0123456789" for character in text)


# ── Runtime entry point (PHASE 4) ────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the module parser; there is deliberately no bind-address flag."""
    parser = argparse.ArgumentParser(
        prog="python -m scarcity_router.server",
        description=(
            "Loopback-only local REST adapter for Scarcity Router (D-028): "
            + "GET /healthz, GET /v1/status, POST /v1/select, "
            + "POST /v1/simulate."
        ),
    )
    _ = parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"TCP port to bind on {BIND_HOST} (default: {DEFAULT_PORT})",
    )
    return parser


def make_server(
    application: RestApplication, *, port: int = DEFAULT_PORT
) -> RestHTTPServer:
    """Bind the loopback-only server on the given port (0 = kernel-assigned)."""
    return RestHTTPServer(port=port, application=application)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the module REST server until interrupted (Ctrl-C exits cleanly)."""
    parser = build_parser()
    arguments = cast("dict[str, object]", vars(parser.parse_args(argv)))
    raw_port = arguments.get("port")
    if (
        not isinstance(raw_port, int)
        or isinstance(raw_port, bool)
        or not 0 <= raw_port <= 65535
    ):
        parser.error("--port must be an integer between 0 and 65535")
    server = make_server(RestApplication(), port=raw_port)
    bound_host, bound_port = cast("tuple[str, int]", server.server_address)
    print(
        f"scarcity-router REST listening on http://{bound_host}:{bound_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = [
    "BIND_HOST",
    "DEFAULT_PORT",
    "ENVELOPE_SCHEMA_VERSION",
    "MAX_REQUEST_BODY_BYTES",
    "RestApplication",
    "RestHTTPServer",
    "RestRequestHandler",
    "build_parser",
    "main",
    "make_server",
]


if __name__ == "__main__":
    raise SystemExit(main())
