"""Durable MI startup boundary tests using synthetic local inputs."""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scarcity_router.control_server import main
from scarcity_router.mi_server_config import load_mi_server_config
from scarcity_router.server_store import ServerStore
from tests import test_mi_consumer as fixture
from tests.m10_fixtures import TlsMaterials
from tests.test_mi_server_config import configuration


@final
class MIStartupTests(unittest.TestCase):
    root = Path()
    data = Path()
    config = Path()
    old = ""
    receipt = ""
    cut = ""
    args: list[str] = []

    @override
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "state"
        publication = self.root / "publication.json"
        _ = publication.write_bytes(fixture.encoded(fixture.public())[0])
        self.config = self.root / "trust.json"
        document = configuration(publication)
        _ = self.config.write_text(json.dumps(document))
        store = ServerStore.open(self.data)
        self.old = load_mi_server_config(self.config).activate(store).digest
        self.receipt = "1" * 64
        self.cut = hashlib.sha256(b"retained cut").hexdigest()
        store.retain_mi_receipt(self.receipt, b"retained receipt", self.cut, b"retained cut")
        store.close()
        document["max_bytes"] = fixture.BOUND - 1
        _ = self.config.write_text(json.dumps(document))
        self.args = ["--data-dir", str(self.data), "--port", "0", "--mi-config", str(self.config)]

    def assert_state(self, digest: str | None) -> None:
        store = ServerStore.open(self.data)
        try:
            self.assertEqual(digest, store.current_mi_cut_digest())
            self.assertEqual(b"retained receipt", store.get_mi_receipt(self.receipt))
            self.assertEqual(b"retained cut", store.get_retained_mi_cut(self.cut))
        finally:
            store.close()

    def test_invalid_tls_preserves_fresh_and_existing_state(self) -> None:
        bad = self.root / "bad.pem"
        _ = bad.write_text("not a certificate")
        tls = ["--tls-certfile", str(bad), "--tls-keyfile", str(bad)]
        self.assertEqual(2, main(self.args + tls))
        self.assert_state(self.old)
        fresh = self.root / "fresh"
        self.assertEqual(2, main(["--data-dir", str(fresh), "--port", "0", "--mi-config", str(self.config)] + tls))
        store = ServerStore.open(fresh)
        try:
            self.assertIsNone(store.current_mi_cut_digest())
        finally:
            store.close()

    def test_listener_failures_then_successful_tls_retry(self) -> None:
        tls = TlsMaterials()
        self.addCleanup(tls.cleanup)
        tls_args = ["--tls-certfile", str(tls.server_cert), "--tls-keyfile", str(tls.server_key)]
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = cast(tuple[str, int], occupied.getsockname())[1]
            self.assertEqual(2, main(self.args + ["--port", str(port)] + tls_args))
            self.assert_state(self.old)
            self.assertEqual(2, main(self.args + ["--worker-listen-port", str(port)] + tls_args))
            self.assert_state(self.old)
        with mock.patch("scarcity_router.gateway_server.GatewayHTTPServer.serve_forever", side_effect=KeyboardInterrupt):
            self.assertEqual(0, main(self.args + ["--worker-listen-port", "0"] + tls_args))
        store = ServerStore.open(self.data)
        try:
            new = store.current_mi_cut_digest()
        finally:
            store.close()
        self.assertIsNotNone(new)
        self.assertNotEqual(self.old, new)
        self.assert_state(new)

    def test_invalid_publication_and_interrupted_transaction_preserve_state(self) -> None:
        document = cast(dict[str, object], json.loads(self.config.read_text()))
        publication = Path(cast(str, document["publication"]))
        original = publication.read_bytes()
        _ = publication.write_bytes(b"invalid")
        self.assertEqual(2, main(self.args))
        self.assert_state(self.old)
        _ = publication.write_bytes(original)
        with mock.patch("scarcity_router.mi_consumer.MIConsumer.adopt", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = main(self.args)
        self.assert_state(self.old)
        with mock.patch("scarcity_router.gateway_server.GatewayHTTPServer.serve_forever", side_effect=KeyboardInterrupt):
            self.assertEqual(0, main(self.args))
        store = ServerStore.open(self.data)
        try:
            self.assertNotEqual(self.old, store.current_mi_cut_digest())
        finally:
            store.close()

    def test_interruption_after_commit_keeps_new_cut_for_retry(self) -> None:
        with mock.patch("scarcity_router.control_api.ControlPlane.activate_startup_mi", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = main(self.args)
        store = ServerStore.open(self.data)
        try:
            committed = store.current_mi_cut_digest()
        finally:
            store.close()
        self.assertIsNotNone(committed)
        self.assertNotEqual(self.old, committed)
        self.assert_state(committed)
        with mock.patch("scarcity_router.gateway_server.GatewayHTTPServer.serve_forever", side_effect=KeyboardInterrupt):
            self.assertEqual(0, main(self.args))
        self.assert_state(committed)

    def test_interruption_after_write_before_commit_rolls_back(self) -> None:
        store = ServerStore.open(self.data)
        self.addCleanup(store.close)
        connection = store._connection  # pyright: ignore[reportPrivateUsage]

        def execute(sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            if sql == "COMMIT":
                raise KeyboardInterrupt
            return connection.execute(sql, parameters)

        with mock.patch.object(store, "_connection", wraps=connection) as wrapped:
            wrapped.configure_mock(**{"execute.side_effect": execute, "in_transaction": True})
            with self.assertRaises(KeyboardInterrupt):
                store.replace_mi_cut(b"candidate", hashlib.sha256(b"candidate").hexdigest(), lambda _: None)
        self.assert_state(self.old)
