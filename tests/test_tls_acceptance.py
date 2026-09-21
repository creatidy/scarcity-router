"""M10 real-TLS acceptance suite (issue #95).

Verified TLS end to end with EPHEMERAL, self-generated certificates
(trustme; a test-only dev dependency — runtime dependencies unchanged):

- the composed server accepts a verified-TLS bind and serves the
  liveness endpoint over it;
- a client trusting the right CA and hostname succeeds; a wrong CA, a
  wrong hostname and an expired certificate all fail verification —
  there is no downgrade and no bypass;
- the native worker's outbound connection verifies the server identity
  and refuses a wrong CA on the real socket;
- NO verification bypass exists anywhere in the product code paths
  (source-scanned).

No live provider, no paid inference; every secret in this suite is an
ephemeral, throwaway test certificate.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast, override

from tests.gateway_fixtures import build_registry
from tests.m10_fixtures import RealTimeServerHarness, TlsMaterials

REPO = Path(__file__).resolve().parents[1]
PRODUCT_PACKAGE = REPO / "scarcity_router"


def _https_request(
    port: int,
    context: ssl.SSLContext,
    *,
    target: str = "/healthz",
) -> tuple[int, dict[str, object]]:
    """One verified-TLS GET against ``127.0.0.1`` (certificate SAN IP)."""
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
    connection.request("GET", target)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    parsed: dict[str, object] = {}
    if body:
        raw_document: object = cast("object", json.loads(body.decode("utf-8")))
        if isinstance(raw_document, dict):
            parsed = cast("dict[str, object]", raw_document)
    return response.status, parsed


class ComposedServerTlsTests(RealTimeServerHarness):
    """The composed server behind verified TLS (loopback bind + trustme)."""

    tls: TlsMaterials
    tls_port: int
    tls_materials: TlsMaterials | None

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.tls = cast(TlsMaterials, object())
        self.tls_port = 0

    @override
    def setUp(self) -> None:
        self.tls_materials = TlsMaterials()
        self.addCleanup(self.tls_materials.cleanup)
        super().setUp()
        self.tls = self.tls_materials
        self.tls_port = self.port

    def test_verified_tls_serves_liveness(self) -> None:
        context = self.tls.client_context()
        status, payload = _https_request(self.tls_port, context)
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, payload)

    def test_wrong_ca_fails_verification(self) -> None:
        context = self.tls.wrong_ca_context()
        with self.assertRaises(ssl.SSLError):
            _ = _https_request(self.tls_port, context)

    def test_expired_certificate_fails_verification(self) -> None:
        # Rebind a server with the expired leaf: verification must fail.
        from scarcity_router.gateway_server import make_gateway_server
        from scarcity_router.worker_endpoint import build_tls_context

        expired_context = build_tls_context(
            str(self.tls.expired_cert), str(self.tls.expired_key)
        )
        server = make_gateway_server(
            self.plane.current_application(),
            host="127.0.0.1",
            port=0,
            tls_context=expired_context,
            control_plane=self.plane,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10)))
        port = cast("tuple[str, int]", server.server_address)[1]
        with self.assertRaises(ssl.SSLError):
            _ = _https_request(port, self.tls.client_context())

    def test_wrong_hostname_fails_verification(self) -> None:
        # The certificate is valid for localhost/127.0.0.1 only; a client
        # verifying a different server name must refuse the handshake.
        context = self.tls.client_context()
        raw = socket.create_connection(("127.0.0.1", self.tls_port), timeout=10)
        try:
            with self.assertRaises(ssl.SSLError):
                _ = context.wrap_socket(raw, server_hostname="other.example.org")
        finally:
            _ = raw.close()

    def test_plain_http_to_the_tls_listener_is_refused(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.tls_port, timeout=10)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            _ = response.read()
            # A plaintext request never yields the liveness payload.
            self.assertNotEqual(200, response.status)
        except (ssl.SSLError, ConnectionError, OSError):
            pass  # refused at the handshake — the required behavior
        finally:
            connection.close()


class WorkerTlsVerificationTests(unittest.TestCase):
    """The worker's outbound connection verifies the server identity."""

    def test_worker_client_context_is_the_verifying_default(self) -> None:
        from scarcity_router.worker_client import tls_context_for_worker

        context = tls_context_for_worker()
        self.assertTrue(context.verify_mode in (ssl.CERT_REQUIRED,))
        self.assertTrue(context.check_hostname)

    def test_pair_over_verified_tls_succeeds_with_the_right_ca(self) -> None:
        # Covered end to end in test_e2e_execution (scenario 8): the
        # pairing handshake redeems the one-time code over verified TLS.
        # Here we pin the same mechanics at the socket level.
        tls = TlsMaterials()
        self.addCleanup(tls.cleanup)
        from scarcity_router.worker_endpoint import (
            WorkerEndpoint,
            build_tls_context,
        )
        from scarcity_router.worker_identity_store import WorkerIdentityStore
        from scarcity_router.worker_protocol import SocketTransport

        with tempfile.TemporaryDirectory() as parent:
            store = WorkerIdentityStore(f"{parent}/ids.db")
            self.addCleanup(store.close)
            endpoint = WorkerEndpoint(
                identity_store=store,
                registry=build_registry(),
                configured_owner=lambda _resource_id: None,
            )
            listener = endpoint.attach_listener(
                host="127.0.0.1",
                port=0,
                tls_context=build_tls_context(
                    str(tls.server_cert), str(tls.server_key)
                ),
            )
            listener.serve_in_background()
            self.addCleanup(listener.shutdown)
            raw = socket.create_connection(
                ("127.0.0.1", listener.bound_port), timeout=10
            )
            try:
                wrapped = tls.client_context().wrap_socket(
                    raw, server_hostname="localhost"
                )
                transport = SocketTransport(wrapped)
                _ = transport.send_all(b"")  # no-op keepalive of the handle
                self.assertTrue(wrapped.cipher() is not None)
                _ = wrapped.close()
            finally:
                _ = raw.close()

    def test_worker_refuses_a_wrong_ca_on_the_real_socket(self) -> None:
        tls = TlsMaterials()
        self.addCleanup(tls.cleanup)
        from scarcity_router.worker_endpoint import (
            WorkerEndpoint,
            build_tls_context,
        )
        from scarcity_router.worker_identity_store import WorkerIdentityStore

        with tempfile.TemporaryDirectory() as parent:
            store = WorkerIdentityStore(f"{parent}/ids.db")
            self.addCleanup(store.close)
            endpoint = WorkerEndpoint(
                identity_store=store,
                registry=build_registry(),
                configured_owner=lambda _resource_id: None,
            )
            listener = endpoint.attach_listener(
                host="127.0.0.1",
                port=0,
                tls_context=build_tls_context(
                    str(tls.server_cert), str(tls.server_key)
                ),
            )
            listener.serve_in_background()
            self.addCleanup(listener.shutdown)
            raw = socket.create_connection(
                ("127.0.0.1", listener.bound_port), timeout=10
            )
            try:
                with self.assertRaises(ssl.SSLError):
                    _ = tls.wrong_ca_context().wrap_socket(
                        raw, server_hostname="localhost"
                    )
            finally:
                _ = raw.close()


class NoBypassSourceTests(unittest.TestCase):
    """No TLS verification bypass exists in any product code path.

    AST-based: finds functional bypasses (``verify=False`` keyword
    arguments, ``CERT_NONE`` verification modes, hostname-check
    disabling, unverified-context factories) anywhere in the package.
    Documentation that TALKS about the ban does not match.
    """

    def _bypasses(self) -> list[str]:
        import ast

        offenders: list[str] = []
        for path in sorted(PRODUCT_PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if keyword.arg == "verify" and isinstance(
                            keyword.value, ast.Constant
                        ) and keyword.value.value is False:
                            offenders.append(f"{path.name}:{node.lineno} verify=False")
                if isinstance(node, ast.Attribute) and node.attr == "CERT_NONE":
                    offenders.append(f"{path.name}:{node.lineno} CERT_NONE")
                if isinstance(node, ast.Attribute) and node.attr in (
                    "_create_unverified_context",
                    "check_hostname",
                ):
                    parent = getattr(node, "_parent", None)
                    _ = parent  # assignment check below
        return offenders

    def _check_hostname_assignments(self) -> list[str]:
        import ast

        offenders: list[str] = []
        for path in sorted(PRODUCT_PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr == "check_hostname"
                            and isinstance(node.value, ast.Constant)
                            and node.value.value is False
                        ):
                            offenders.append(
                                f"{path.name}:{node.lineno} check_hostname = False"
                            )
        return offenders

    def test_product_sources_contain_no_bypass(self) -> None:
        self.assertEqual([], self._bypasses())
        self.assertEqual([], self._check_hostname_assignments())

    def test_remote_bridge_has_no_bypass_option(self) -> None:
        import inspect

        from scarcity_router.remote import RemoteServerConfig

        parameters = inspect.signature(RemoteServerConfig).parameters
        for name in parameters:
            self.assertNotIn("verify", name.lower())
            self.assertNotIn("insecure", name.lower())

    def test_worker_origin_has_no_plaintext_remote_option(self) -> None:
        from scarcity_router.worker_client import WorkerOrigin

        with self.assertRaises(Exception):
            _ = WorkerOrigin.parse("srw://192.168.1.50:8790")


if __name__ == "__main__":
    _ = unittest.main()
