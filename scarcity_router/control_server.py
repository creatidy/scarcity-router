"""Composition entry point for the full server component (M09, issue #94).

One server process (D-041) serves all surfaces:

- the OpenAI-compatible execution surface v1 (M03): ``GET /v1/models``,
  ``POST /v1/chat/completions``;
- the authenticated machine-interface control endpoints (M08 parity):
  ``GET /v1/status``, ``POST /v1/select``, ``POST /v1/simulate``;
- the authenticated control API and lightweight web UI (M09):
  ``/control/**`` and ``/admin/**``;
- the execution adapters composed from administrator configuration
  (``scarcity_router.server_composition``): the M04 generic
  OpenAI-compatible HTTP adapter from provider endpoints and store-held
  credentials, and the M05 worker-bridged adapter from resource→worker
  bindings;
- the OPTIONAL M05 worker-protocol listener (``--worker-listen-port``):
  off by default, verified TLS required for any non-loopback bind (D-044).

This module is composition only: it opens the durable stores, builds the
:class:`~scarcity_router.control_api.ControlPlane` over them (the plane
owns the ONE pairing system and composes the adapters from
configuration) and binds the M03 execution server with the control plane
attached. There is no second deployed administration service. The
default deployment is honestly empty: no adapters, no worker listener,
no clients — exactly as M03 shipped. ``python -m
scarcity_router.worker_endpoint`` remains the standalone WORKER-side
entrypoint; it is not a second server. The frozen loopback REST v1
adapter (:mod:`scarcity_router.server`) is untouched and remains its own
surface.

Security posture (D-044): the default bind is loopback; a non-loopback
bind requires explicit TLS. First-run operation starts with NO
administrator credential and NO client keys — the web UI forces
onboarding, and until a key is issued every inference request is
explicitly unauthenticated (fail closed).
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import cast

from .control_api import ControlPlane
from .server_ui import dispatch_ui
from .gateway_server import (
    BIND_HOST,
    DEFAULT_PORT,
    build_tls_context,
    load_client_key_directory,
    make_gateway_server,
)
from .server_store import (
    ServerStore,
    ServerStoreError,
    default_server_data_dir,
)
from .worker_endpoint import build_tls_context as build_worker_tls_context

DEFAULT_DATA_DIR = default_server_data_dir()


def build_parser() -> argparse.ArgumentParser:
    """Build the full-server parser (composition flags only)."""
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    prog = (
        invoked
        if invoked == "scarcity-router-control"
        else "python -m scarcity_router.control_server"
    )
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Authenticated Scarcity Router server component: execution "
            + "surface v1, machine-interface control endpoints, control API "
            + "and administration web UI."
        ),
    )
    _ = parser.add_argument(
        "--host",
        default=BIND_HOST,
        metavar="HOST",
        help=f"bind address (default: {BIND_HOST}; non-loopback requires TLS)",
    )
    _ = parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"TCP port to bind (default: {DEFAULT_PORT})",
    )
    _ = parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        metavar="DIR",
        help=(
            "server data directory holding the durable store "
            + f"(default: {DEFAULT_DATA_DIR})"
        ),
    )
    _ = parser.add_argument(
        "--import-client-keys",
        default=None,
        metavar="FILE",
        help=(
            "one-time migration: import an M03-style owner-only client-keys "
            + "JSON file into the durable store (existing ids are kept)"
        ),
    )
    _ = parser.add_argument(
        "--tls-certfile",
        default=None,
        metavar="FILE",
        help="TLS certificate chain (required for non-loopback binds)",
    )
    _ = parser.add_argument(
        "--tls-keyfile",
        default=None,
        metavar="FILE",
        help="TLS private key (required for non-loopback binds)",
    )
    _ = parser.add_argument(
        "--worker-listen-host",
        default=BIND_HOST,
        metavar="HOST",
        help=(
            "worker-protocol listener bind address "
            + f"(default: {BIND_HOST}; non-loopback requires worker TLS)"
        ),
    )
    _ = parser.add_argument(
        "--worker-listen-port",
        default=None,
        type=int,
        metavar="PORT",
        help=(
            "worker-protocol listener port; OMITTED by default, which runs "
            + "no worker listener (workers cannot connect until it is on)"
        ),
    )
    _ = parser.add_argument(
        "--worker-tls-certfile",
        default=None,
        metavar="FILE",
        help="worker-listener TLS certificate chain (required for non-loopback)",
    )
    _ = parser.add_argument(
        "--worker-tls-keyfile",
        default=None,
        metavar="FILE",
        help="worker-listener TLS private key (required for non-loopback)",
    )
    return parser


def _import_client_keys(plane: ControlPlane, path: Path) -> int:
    """One-time migration of an M03 client-keys file into the store.

    Only SHA-256 hashes are copied; existing client ids are kept so this
    can never overwrite store state. The file stays the administrator's
    own artifact; the store becomes the single runtime source of truth.
    """
    from datetime import datetime, timezone

    directory = load_client_key_directory(path)
    now = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    imported = 0
    for client_id in directory.client_ids:
        if plane.import_client_key_hash(
            client_id=client_id,
            key_hash=directory.hash_for(client_id),
            label="imported from client-keys file",
            created_at=now,
        ):
            imported += 1
    return imported


def main(argv: list[str] | None = None) -> int:
    """Run the full server component until interrupted."""
    parser = build_parser()
    arguments = cast("dict[str, object]", vars(parser.parse_args(argv)))
    host = arguments["host"]
    if not isinstance(host, str) or not host:
        parser.error("--host must be a non-empty string")
    raw_port = arguments["port"]
    if (
        not isinstance(raw_port, int)
        or isinstance(raw_port, bool)
        or not 0 <= raw_port <= 65535
    ):
        parser.error("--port must be an integer between 0 and 65535")
    data_dir_value = arguments["data_dir"]
    if not isinstance(data_dir_value, str) or not data_dir_value:
        parser.error("--data-dir must be a non-empty string")
    certfile = arguments["tls_certfile"]
    keyfile = arguments["tls_keyfile"]
    if (certfile is None) != (keyfile is None):
        parser.error("--tls-certfile and --tls-keyfile must be used together")
    if not _loopback(host) and certfile is None:
        parser.error("a non-loopback bind requires --tls-certfile/--tls-keyfile")
    worker_host = arguments["worker_listen_host"]
    if not isinstance(worker_host, str) or not worker_host:
        parser.error("--worker-listen-host must be a non-empty string")
    worker_port = arguments["worker_listen_port"]
    if worker_port is not None:
        if (
            not isinstance(worker_port, int)
            or isinstance(worker_port, bool)
            or not 0 <= worker_port <= 65535
        ):
            parser.error("--worker-listen-port must be an integer between 0 and 65535")
    worker_certfile = arguments["worker_tls_certfile"]
    worker_keyfile = arguments["worker_tls_keyfile"]
    if (worker_certfile is None) != (worker_keyfile is None):
        parser.error(
            "--worker-tls-certfile and --worker-tls-keyfile must be used together"
        )
    if worker_port is not None and not _loopback(worker_host) and worker_certfile is None:
        parser.error(
            "a non-loopback worker-protocol listener requires "
            + "--worker-tls-certfile/--worker-tls-keyfile"
        )
    import_client_keys = arguments["import_client_keys"]
    if import_client_keys is not None and not isinstance(import_client_keys, str):
        parser.error("--import-client-keys must be a path")
    worker_tls_context = None
    if worker_port is not None and worker_certfile is not None and worker_keyfile is not None:
        try:
            worker_tls_context = build_worker_tls_context(
                str(worker_certfile), str(worker_keyfile)
            )
        except ValueError as exc:
            print(f"server: {exc}", file=sys.stderr)
            return 2
    try:
        data_dir = Path(data_dir_value)
        store = ServerStore.open(data_dir)
        plane = ControlPlane(
            store=store,
            own_origins=(f"http://{host}:{raw_port}", f"https://{host}:{raw_port}"),
            tls=certfile is not None,
            ui_dispatcher=dispatch_ui,
        )
        if import_client_keys is not None:
            imported = _import_client_keys(plane, Path(import_client_keys))
            print(f"imported {imported} client key(s) into the store", flush=True)
        tls_context = (
            build_tls_context(str(certfile), str(keyfile))
            if certfile is not None and keyfile is not None
            else None
        )
        server = make_gateway_server(
            plane.current_application(),
            host=host,
            port=raw_port,
            tls_context=tls_context,
            control_plane=plane,
        )
        worker_listener = None
        worker_reaper: threading.Thread | None = None
        worker_reaper_stop = threading.Event()
        if worker_port is not None:
            # The OPTIONAL worker-protocol listener: same process, its own
            # authenticated transport, off unless asked for.
            worker_listener = plane.worker_endpoint.attach_listener(
                host=worker_host,
                port=worker_port,
                tls_context=worker_tls_context,
            )
            worker_listener.serve_in_background()
            # Heartbeat liveness reaping runs with the listener: silent
            # sessions are closed after the grace window (D-044 bounds).
            worker_reaper = threading.Thread(
                target=_reap_liveness,
                args=(
                    plane.worker_endpoint,
                    plane.worker_endpoint.heartbeat_interval_seconds,
                    worker_reaper_stop,
                ),
                name="worker-endpoint-liveness",
                daemon=True,
            )
            worker_reaper.start()
    except (ValueError, OSError, ServerStoreError) as exc:
        print(f"server: {exc}", file=sys.stderr)
        return 2
    bound_host, bound_port = cast("tuple[str, int]", server.server_address)
    scheme = "https" if tls_context is not None else "http"
    origin = f"{scheme}://{bound_host}:{bound_port}"
    from . import get_version

    print(
        f"scarcity-router {get_version()} server component listening on "
        + f"{origin} (execution surface, control API, web UI at {origin}/admin)",
        flush=True,
    )
    if not plane.admin_configured():
        print(
            "first run: no administrator credential exists — open "
            + f"{origin}/admin to complete onboarding",
            flush=True,
        )
    if worker_listener is not None:
        worker_scheme = "srws" if worker_tls_context is not None else "srw"
        print(
            "worker-protocol listener on "
            + f"{worker_scheme}://{worker_host}:{worker_listener.bound_port} "
            + f"(protocol v1; pairing codes are issued at {origin}/admin/workers)",
            flush=True,
        )
    else:
        print(
            "worker-protocol listener: off (workers cannot connect; pass "
            + "--worker-listen-port to enable it)",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker_reaper_stop.set()
        if worker_reaper is not None:
            _ = worker_reaper.join(timeout=5)
        if worker_listener is not None:
            worker_listener.shutdown()
        server.server_close()
        plane.worker_endpoint.close_all_sessions()
        plane.close_worker_store()
        store.close()
    return 0


def _reap_liveness(
    endpoint: object,
    interval_seconds: int,
    stop: threading.Event,
) -> None:
    """Close heartbeat-silent worker sessions (bounded liveness reaping)."""
    from .worker_endpoint import WorkerEndpoint

    assert isinstance(endpoint, WorkerEndpoint)
    while not stop.wait(timeout=max(1, interval_seconds)):
        _ = endpoint.enforce_liveness()


def _loopback(host: str) -> bool:
    return host in (BIND_HOST, "localhost", "::1")


__all__ = [
    "DEFAULT_DATA_DIR",
    "build_parser",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
