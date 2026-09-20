"""Shared M10 acceptance fixtures (issue #95).

Deterministic, synthetic, CI-safe: everything here runs on loopback
sockets with self-generated test certificates (trustme), conspicuous
SYNTHETIC secrets only, and no live provider or paid inference. These
fixtures serve the e2e, TLS and security acceptance suites.
"""

from __future__ import annotations

import http.client
import json
import ssl
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

from scarcity_router.control_api import ControlPlane
from scarcity_router.gateway_server import GatewayHTTPServer, make_gateway_server
from scarcity_router.server_store import ServerStore
from scarcity_router.status import StatusCollectors

if TYPE_CHECKING:  # pragma: no cover - type-only import
    import trustme

from tests.server_fixtures import (
    FAKE_ADMIN_PASSWORD,
    PBKDF2_TEST_ITERATIONS,
    synthetic_collectors,
)

T_EVAL = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

#: A conspicuous marker embedded in prompts/responses of the e2e flows;
#: acceptance asserts it NEVER appears in audit, export or diagnostics.
PROMPT_MARKER = "SYNTHETIC-E2E-PROMPT-MARKER-q7x2"
RESPONSE_MARKER = "SYNTHETIC-E2E-RESPONSE-MARKER-m3k9"


# ── Real-clock composed-server harness ────────────────────────────────────────


class RealTimeServerHarness(unittest.TestCase):
    """ServerHarness with the real wall clock (needed by the M04 adapter's
    socket-timeout computations during real dispatch) plus streaming-aware
    HTTP helpers."""

    plane: ControlPlane
    server: GatewayHTTPServer
    thread: threading.Thread
    data_dir: Path
    client_key: str
    client_id: str
    cookie: str

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.plane = cast(ControlPlane, object())
        self.server = cast(GatewayHTTPServer, object())
        self.thread = cast(threading.Thread, object())
        self.data_dir = Path(".")
        self.client_key = ""
        self.client_id = ""
        self.cookie = ""

    @override
    def setUp(self) -> None:
        self.data_dir = Path(_temp_dir("scarcity-router-m10-store-"))
        self.plane = self.make_plane(self.data_dir)
        self._start_server()

    @override
    def tearDown(self) -> None:
        import shutil

        self.server.shutdown()
        self.server.server_close()
        _ = self.thread.join(timeout=10)
        self.plane.store.close()
        self.plane.close_worker_store()
        _ = shutil.rmtree(self.data_dir, ignore_errors=True)

    def make_plane(self, data_dir: Path) -> ControlPlane:
        from scarcity_router.server_ui import dispatch_ui

        store = ServerStore.open(data_dir)
        return ControlPlane(
            store=store,
            clock=None,  # real clock: real dispatch paths need real deadlines
            collectors=synthetic_collectors(),
            pbkdf2_iterations=PBKDF2_TEST_ITERATIONS,
            version="0.1.0.test",
            own_origins=("http://127.0.0.1:8787",),
            ui_dispatcher=dispatch_ui,
        )

    def _start_server(self) -> None:
        self.server = make_gateway_server(
            self.plane.current_application(),
            host="127.0.0.1",
            port=0,
            control_plane=self.plane,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    @property
    def port(self) -> int:
        return cast("tuple[str, int]", self.server.server_address)[1]

    def onboard(self, *, login: bool = True) -> None:
        status, payload, headers = self.exchange(
            "POST",
            "/control/bootstrap/admin",
            {"password": FAKE_ADMIN_PASSWORD, "confirm": True},
        )
        assert status == 200, payload
        cookies = [value for name, value in headers if name.lower() == "set-cookie"]
        assert cookies, "bootstrap did not set a session cookie"
        self.cookie = cookies[0].split(";", 1)[0].split("=", 1)[1]
        if login:
            issued = self.plane.service_issue_client_key({"label": "e2e client"})
            self.client_key = cast(str, issued["api_key"])
            self.client_id = cast(str, issued["client_id"])

    def exchange(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        with_session: bool = True,
        timeout: float = 30.0,
    ) -> tuple[int, object, list[tuple[str, str]]]:
        from scarcity_router.control_api import SESSION_COOKIE_NAME

        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        merged: dict[str, str] = {}
        if body is not None:
            merged["Content-Type"] = "application/json"
        if headers:
            merged.update(headers)
        if self.cookie and with_session:
            merged["Cookie"] = f"{SESSION_COOKIE_NAME}={self.cookie}"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=timeout
        )
        try:
            connection.request(method, path, body=body, headers=merged)
            response = connection.getresponse()
            raw = response.read()
            response_headers = response.getheaders()
            status = response.status
        finally:
            connection.close()
        parsed: object = None
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw.decode("utf-8", errors="replace")
        return status, parsed, response_headers

    def admin_post(self, path: str, payload: object | None = None) -> tuple[int, object]:
        csrf = self.plane.csrf_token_for_cookie(self.cookie)
        assert csrf is not None
        status, parsed, _headers = self.exchange(
            "POST", path, payload, headers={"X-Scarcity-CSRF": csrf}
        )
        return status, parsed

    def admin_get(self, path: str) -> tuple[int, object]:
        status, parsed, _headers = self.exchange("GET", path)
        return status, parsed

    # ── Streaming helpers ─────────────────────────────────────────────────

    def open_stream(
        self,
        path: str,
        payload: object,
        *,
        bearer: str | None = None,
    ) -> http.client.HTTPConnection:
        """Open an SSE request and return the (connected) connection.

        The caller reads from ``connection.getresponse()`` and may close
        the connection abruptly to simulate a client disconnect.
        """
        headers = {"Content-Type": "application/json"}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request(
            "POST", path, body=json.dumps(payload).encode("utf-8"), headers=headers
        )
        return connection

    def read_sse_payloads(self, response: http.client.HTTPResponse) -> list[dict[str, object]]:
        """Read a complete SSE body into its JSON payload list."""
        payloads: list[dict[str, object]] = []
        raw = response.read().decode("utf-8")
        for line in raw.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                payload = json.loads(line[len("data: ") :])
                if isinstance(payload, dict):
                    payloads.append(payload)
        return payloads


def _temp_dir(prefix: str) -> str:
    import shutil

    directory = tempfile.mkdtemp(prefix=prefix)

    def cleanup() -> None:
        _ = shutil.rmtree(directory, ignore_errors=True)

    # Registered per-test by callers via addCleanup where needed; the
    # harness removes its own directory in tearDown.
    return directory


def wait_until(
    condition: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.02,
    message: str = "condition not met",
) -> None:
    """Poll a condition with a hard deadline (real-clock tests only)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise AssertionError(message)


# ── trustme-backed TLS materials ──────────────────────────────────────────────


class TlsMaterials:
    """Self-generated, ephemeral test certificates (never real secrets).

    One CA, a server leaf valid for ``localhost``/``127.0.0.1``, a wrong
    CA (verification must fail), and an expired leaf (verification must
    fail). PEM files are written to a throwaway directory.
    """

    def __init__(self) -> None:
        import trustme


        self._ca = trustme.CA()
        self._wrong_ca = trustme.CA()
        self._expired_ca = trustme.CA()
        self._server_leaf = self._ca.issue_cert("localhost", "127.0.0.1")
        self._other_host_leaf = self._ca.issue_cert("other.example.org")
        self._expired_leaf = self._expired_ca.issue_cert(
            "localhost", "127.0.0.1", not_after=datetime(2020, 1, 1, tzinfo=timezone.utc)
        )
        self._directory = Path(tempfile.mkdtemp(prefix="scarcity-router-m10-tls-"))
        # trustme exposes the PEM material as properties (lists of Pem
        # objects), not methods.
        self.server_cert = self._write("server.crt", self._server_leaf.cert_chain_pems)
        self.server_key = self._write("server.key", [self._server_leaf.private_key_pem])
        self.other_host_cert = self._write(
            "other.crt", self._other_host_leaf.cert_chain_pems
        )
        self.other_host_key = self._write(
            "other.key", [self._other_host_leaf.private_key_pem]
        )
        self.expired_cert = self._write("expired.crt", self._expired_leaf.cert_chain_pems)
        self.expired_key = self._write(
            "expired.key", [self._expired_leaf.private_key_pem]
        )
        self.ca_file = self._write("ca.crt", [self._ca.cert_pem])
        self.wrong_ca_file = self._write("wrong-ca.crt", [self._wrong_ca.cert_pem])

    def _write(self, name: str, pems: "list[trustme.Blob]") -> Path:
        path = self._directory / name
        text = b"".join(pem.bytes() for pem in pems)
        _ = path.write_bytes(text)
        _ = path.chmod(0o600)
        return path

    def client_context(self) -> ssl.SSLContext:
        """A verifying client context trusting ONLY the test CA."""
        context = ssl.create_default_context(cafile=str(self.ca_file))
        return context

    def wrong_ca_context(self) -> ssl.SSLContext:
        """A verifying client context trusting only the WRONG CA."""
        return ssl.create_default_context(cafile=str(self.wrong_ca_file))

    def cleanup(self) -> None:
        import shutil

        _ = shutil.rmtree(self._directory, ignore_errors=True)


__all__ = [
    "PROMPT_MARKER",
    "RealTimeServerHarness",
    "RESPONSE_MARKER",
    "TlsMaterials",
    "wait_until",
]
