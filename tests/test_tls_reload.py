"""Offline verification of externally managed TLS replacement material."""

from __future__ import annotations

import http.client
import os
import re
import select
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import cast, final
from unittest import mock

from scarcity_router.tls_reload import verified_server_context
from tests.m10_fixtures import TlsMaterials, bytes_blob


@final
class CertificatePreflightTests(unittest.TestCase):
    def _materials(self) -> TlsMaterials:
        materials = TlsMaterials()
        self.addCleanup(materials.cleanup)
        return materials

    def test_verified_pair_is_accepted(self) -> None:
        tls = self._materials()
        context = verified_server_context(
            str(tls.server_cert), str(tls.server_key),
            hostname="localhost", ca_file=str(tls.ca_file),
        )
        self.assertIsInstance(context, ssl.SSLContext)

    def test_preflight_does_not_enable_environment_key_logging(self) -> None:
        tls = self._materials()
        with tempfile.TemporaryDirectory() as directory:
            keylog = Path(directory) / "keylog"
            with mock.patch.dict(os.environ, {"SSLKEYLOGFILE": str(keylog)}):
                _ = verified_server_context(
                    str(tls.server_cert), str(tls.server_key),
                    hostname="localhost", ca_file=str(tls.ca_file),
                )
            self.assertFalse(keylog.exists())

    def test_invalid_replacements_are_refused(self) -> None:
        tls = self._materials()
        with tempfile.TemporaryDirectory() as directory:
            expired_ca = Path(directory) / "expired-ca.pem"
            _ = expired_ca.write_bytes(bytes_blob(tls._expired_ca.cert_pem))  # pyright: ignore[reportPrivateUsage]
            cases = (
                (tls.server_cert, tls.server_key, "localhost", tls.wrong_ca_file),
                (tls.server_cert, tls.server_key, "other.example.org", tls.ca_file),
                (tls.server_cert, tls.other_host_key, "localhost", tls.ca_file),
                (tls.expired_cert, tls.expired_key, "localhost", expired_ca),
                (Path(directory) / "missing.pem", tls.server_key, "localhost", tls.ca_file),
            )
            for cert, key, hostname, ca in cases:
                with self.subTest(cert=cert.name, hostname=hostname, ca=ca.name):
                    with self.assertRaises(ValueError):
                        _ = verified_server_context(
                            str(cert), str(key), hostname=hostname, ca_file=str(ca)
                        )
            with self.assertRaisesRegex(ValueError, "certificate expired"):
                _ = verified_server_context(
                    str(tls.expired_cert), str(tls.expired_key),
                    hostname="localhost", ca_file=str(expired_ca),
                )
            with self.assertRaisesRegex(ValueError, "certificate chain is untrusted"):
                _ = verified_server_context(
                    str(tls.server_cert), str(tls.server_key),
                    hostname="localhost", ca_file=str(tls.wrong_ca_file),
                )


@unittest.skipUnless(hasattr(signal, "SIGHUP"), "requires POSIX SIGHUP")
@final
class CertificateReloadIntegrationTests(unittest.TestCase):
    def test_expired_replacement_refuses_startup_without_exposing_material(self) -> None:
        tls = TlsMaterials()
        self.addCleanup(tls.cleanup)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expired_ca = root / "expired-ca.pem"
            _ = expired_ca.write_bytes(bytes_blob(tls._expired_ca.cert_pem))  # pyright: ignore[reportPrivateUsage]
            result = subprocess.run(
                [sys.executable, "-m", "scarcity_router.control_server",
                 "--data-dir", str(root / "data"), "--tls-certfile", str(tls.expired_cert),
                 "--tls-keyfile", str(tls.expired_key), "--tls-reload-hostname", "localhost",
                 "--tls-reload-ca-cert", str(expired_ca)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(root)},
            )
            self.assertEqual(2, result.returncode)
            self.assertIn(b"TLS replacement failed", result.stderr)
            self.assertNotIn(tls.expired_key.read_bytes(), result.stderr)

    def test_reload_validates_both_listeners_before_switching(self) -> None:
        tls = TlsMaterials()
        self.addCleanup(tls.cleanup)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            cert = root / "cert.pem"
            key = root / "key.pem"
            worker_cert = root / "worker-cert.pem"
            worker_key = root / "worker-key.pem"
            for target, source in ((cert, tls.server_cert), (worker_cert, tls.server_cert),
                                   (key, tls.server_key), (worker_key, tls.server_key)):
                _ = target.write_bytes(source.read_bytes())
            process = subprocess.Popen(
                [sys.executable, "-m", "scarcity_router.control_server",
                 "--host", "127.0.0.1", "--port", "0", "--data-dir", str(root / "data"),
                 "--tls-certfile", str(cert), "--tls-keyfile", str(key),
                 "--tls-reload-hostname", "localhost", "--tls-reload-ca-cert", str(tls.ca_file),
                 "--worker-listen-port", "0", "--worker-tls-certfile", str(worker_cert),
                 "--worker-tls-keyfile", str(worker_key), "--worker-tls-reload-hostname", "localhost",
                 "--worker-tls-reload-ca-cert", str(tls.ca_file)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(root)},
            )
            _ = stack.callback(self._stop, process)
            output = self._wait_output(process, b"verified TLS reload ready")
            http_match = re.search(rb"listening on https://127\.0\.0\.1:(\d+)", output)
            worker_match = re.search(rb"worker-protocol listener on srws://127\.0\.0\.1:(\d+)", output)
            self.assertIsNotNone(http_match)
            self.assertIsNotNone(worker_match)
            assert http_match is not None and worker_match is not None
            http_port = int(http_match.group(1))
            worker_port = int(worker_match.group(1))
            old_http = self._peer_certificate(http_port, tls.client_context())
            old_worker = self._peer_certificate(worker_port, tls.client_context())
            persistent = http.client.HTTPSConnection(
                "localhost", http_port, context=tls.client_context(), timeout=5
            )
            _ = stack.callback(persistent.close)
            persistent.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{http_port}"})
            response = persistent.getresponse()
            self.assertEqual(200, response.status)
            _ = response.read()
            original_socket = persistent.sock
            self.assertIsNotNone(original_socket)

            leaf: object = tls._ca.issue_cert("localhost", "127.0.0.1")  # pyright: ignore[reportPrivateUsage]
            new_cert = b"".join(TlsMaterials._cert_pems(leaf))  # pyright: ignore[reportPrivateUsage]
            new_key = TlsMaterials._private_key_pem(leaf)  # pyright: ignore[reportPrivateUsage]
            _ = cert.write_bytes(new_cert)
            _ = key.write_bytes(new_key)
            _ = worker_cert.write_bytes(tls.other_host_cert.read_bytes())
            _ = worker_key.write_bytes(tls.other_host_key.read_bytes())
            process.send_signal(signal.SIGHUP)
            _ = self._wait_output(process, b"TLS reload failed")
            self.assertEqual(old_http, self._peer_certificate(http_port, tls.client_context()))
            self.assertEqual(old_worker, self._peer_certificate(worker_port, tls.client_context()))

            _ = worker_cert.write_bytes(new_cert)
            _ = worker_key.write_bytes(new_key)
            process.send_signal(signal.SIGHUP)
            _ = self._wait_output(process, b"certificate contexts reloaded")
            new_http = self._peer_certificate(http_port, tls.client_context())
            new_worker = self._peer_certificate(worker_port, tls.client_context())
            self.assertNotEqual(old_http, new_http)
            self.assertEqual(new_http, new_worker)
            persistent.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{http_port}"})
            response = persistent.getresponse()
            self.assertEqual(200, response.status)
            _ = response.read()
            self.assertIs(original_socket, persistent.sock)

    @staticmethod
    def _peer_certificate(port: int, context: ssl.SSLContext) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                return cast(bytes, connection.getpeercert(binary_form=True))

    @staticmethod
    def _wait_output(process: subprocess.Popen[bytes], marker: bytes) -> bytes:
        assert process.stdout is not None
        output = b""
        deadline = time.monotonic() + 10
        while marker not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise AssertionError("server did not report expected lifecycle state")
            data = os.read(process.stdout.fileno(), 4096)
            if not data:
                raise AssertionError(f"server exited before expected lifecycle state: {output!r}")
            output += data
        return output

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            _ = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
