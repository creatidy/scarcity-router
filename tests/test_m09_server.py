"""Composition entry-point tests (M09): python -m scarcity_router.control_server.

Pins the composition boundary: parser defaults, the non-loopback/TLS
refusal, and that a running composition serves the execution surface
(authenticated), the machine-interface control endpoints and the web UI
from ONE listener.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scarcity_router.control_server import build_parser, main
from scarcity_router.server_store import ServerStore
from scarcity_router.server_ui import dispatch_ui

from tests.server_fixtures import FAKE_ADMIN_PASSWORD


class ParserTests(unittest.TestCase):
    def test_defaults(self) -> None:
        arguments = cast(
            "dict[str, object]", vars(build_parser().parse_args([]))
        )
        self.assertEqual("127.0.0.1", arguments["host"])
        self.assertEqual(8787, arguments["port"])
        self.assertIsNone(arguments["tls_certfile"])
        self.assertIsNone(arguments["tls_keyfile"])
        self.assertIsNotNone(arguments["data_dir"])

    def test_non_loopback_requires_tls_flags(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            with self.assertRaises(SystemExit):
                _ = main(
                    [
                        "--host",
                        "0.0.0.0",
                        "--data-dir",
                        str(Path(parent) / "server"),
                    ]
                )

    def test_tls_flags_must_pair(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            with self.assertRaises(SystemExit):
                _ = main(
                    [
                        "--data-dir",
                        str(Path(parent) / "server"),
                        "--tls-certfile",
                        str(Path(parent) / "cert.pem"),
                    ]
                )


class CompositionStartupTests(unittest.TestCase):
    def test_startup_with_unreadable_key_import_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            code = main(
                [
                    "--data-dir",
                    str(Path(parent) / "server"),
                    "--import-client-keys",
                    str(Path(parent) / "absent.json"),
                ]
            )
            self.assertEqual(2, code)

    def test_one_listener_serves_all_surfaces(self) -> None:
        """Execution, control API and UI answer on one bound port."""
        import os

        from scarcity_router.gateway_server import make_gateway_server
        from scarcity_router.control_api import ControlPlane

        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            try:
                plane = ControlPlane(
                    store=store,
                    clock=lambda: datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
                    pbkdf2_iterations=1000,
                    version="0.1.0.test",
                    ui_dispatcher=dispatch_ui,
                )
                server = make_gateway_server(
                    plane.current_application(),
                    host="127.0.0.1",
                    port=0,
                    control_plane=plane,
                )
                port = cast("tuple[str, int]", server.server_address)[1]
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True
                )
                thread.start()
                try:
                    # Web UI root redirects to onboarding.
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=10
                    )
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    _ = response.read()
                    self.assertEqual(303, response.status)
                    connection.close()
                    # Execution surface refuses anonymous traffic.
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=10
                    )
                    connection.request("GET", "/v1/models")
                    response = connection.getresponse()
                    _ = response.read()
                    self.assertEqual(401, response.status)
                    connection.close()
                    # Onboarding, then a client key, then the control API.
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=10
                    )
                    connection.request(
                        "POST",
                        "/control/bootstrap/admin",
                        body=json.dumps(
                            {
                                "password": FAKE_ADMIN_PASSWORD,
                                "confirm": True,
                            }
                        ),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    _ = response.read()
                    self.assertEqual(200, response.status)
                    cookie_header = [
                        value
                        for name, value in response.getheaders()
                        if name.lower() == "set-cookie"
                    ][0]
                    connection.close()
                    cookie = cookie_header.split(";", 1)[0]
                    issued = plane.service_issue_client_key({"label": "cli"})
                    api_key = cast(str, issued["api_key"])
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=10
                    )
                    connection.request(
                        "GET",
                        "/v1/status",
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    response = connection.getresponse()
                    document = cast(
                        "dict[str, object]", json.loads(response.read())
                    )
                    self.assertEqual(200, response.status)
                    self.assertEqual(1, document["schema_version"])
                    connection.close()
                    _ = cookie
                finally:
                    server.shutdown()
                    server.server_close()
                    _ = thread.join(timeout=10)
            finally:
                store.close()
        _ = os


if __name__ == "__main__":
    _ = unittest.main()
