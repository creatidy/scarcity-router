"""Single composed-server ownership, using private stores and offline children."""

from __future__ import annotations

import os
import gc
import http.client
import json
import queue
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import TextIO, cast, final
from unittest import mock

from scarcity_router.control_api import ControlPlane
from scarcity_router.control_server import main
from scarcity_router.gateway_server import GatewayHTTPServer
from scarcity_router.resource_state import ResourceIdentity, ResourceRegistration
from scarcity_router.server_config import ResourceConfig, ServerConfiguration
from scarcity_router.server_ownership import ServerOwnership
from scarcity_router.server_store import ServerStore, ServerStoreError
from scarcity_router.control_api import hash_token


@final
class DuplicateStartupTests(unittest.TestCase):
    def test_refused_duplicates_preserve_epoch_journal_snapshot_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"

            def serving(server: GatewayHTTPServer) -> None:
                plane = server.control_plane
                assert isinstance(plane, ControlPlane)
                initial = plane.producer_event_snapshot()
                for port in (server.server_port, 0):
                    with self.subTest(port=port):
                        before = plane.producer_event_snapshot()
                        result = subprocess.run(
                            [sys.executable, "-m", "scarcity_router.control_server",
                             "--data-dir", str(data), "--port", str(port)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                            env={"PATH": os.environ.get("PATH", ""), "HOME": str(root)},
                            check=False,
                        )
                        self.assertEqual(2, result.returncode)
                        plane.store.check_producer_position(before.cursor.epoch, before.cursor.sequence)
                        self.assertEqual(before, plane.producer_event_snapshot())
                        identity = ResourceIdentity(f"resource-{port}", "local_app_adapter", "ollama", "local", "local_ungated")
                        resource = ResourceConfig(ResourceRegistration(identity, freshness_ttl_seconds=60), enabled=False)
                        plane._save_config(ServerConfiguration(resources=(resource,)))  # pyright: ignore[reportPrivateUsage]
                        after = plane.producer_event_snapshot()
                        self.assertEqual(before.cursor.epoch, after.cursor.epoch)
                        self.assertEqual(before.cursor.sequence + 1, after.cursor.sequence)
                        replay = plane.producer_replay(before.cursor)
                        self.assertIsNone(replay.resync)
                        self.assertEqual(after.payload, replay.events[-1].payload)
                self.assertEqual(2, len(plane.producer_replay(initial.cursor).events))
                raise KeyboardInterrupt

            with mock.patch.object(GatewayHTTPServer, "serve_forever", autospec=True, side_effect=serving):
                self.assertEqual(0, main(["--data-dir", str(data), "--port", "0"]))


@final
class OwnershipLifecycleTests(unittest.TestCase):
    def test_same_process_aliases_and_unrelated_descriptor_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            owner = ServerOwnership(data)
            try:
                for _ in range(3):
                    with self.assertRaises(ServerStoreError):
                        _ = ServerOwnership(data / ".")
                    fd = os.open(data, os.O_RDONLY) if os.name != "nt" else None
                    if fd is not None:
                        os.close(fd)
                    store = ServerStore.open(data)
                    store.close()
                    _ = gc.collect()
                    with self.assertRaises(ServerStoreError):
                        _ = ServerOwnership(data)
            finally:
                owner.close()
                owner.close()
            restarted = ServerOwnership(data)
            restarted.close()

    @unittest.skipIf(os.name == "nt", "POSIX directory aliases and inode locking")
    def test_symlink_alias_contends_and_replaced_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            owner = ServerOwnership(data)
            try:
                alias = root / "alias"
                alias.symlink_to(data, target_is_directory=True)
                with self.assertRaises(ServerStoreError):
                    _ = ServerOwnership(alias)
                # No replaceable lock-file inode exists on POSIX.
                self.assertEqual([], list(data.iterdir()))
                _ = data.rename(root / "retained")
                data.mkdir(mode=0o700)
                with self.assertRaisesRegex(ServerStoreError, "identity changed"):
                    owner.check_directory()
            finally:
                owner.close()

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_unsafe_directory_permissions_refuse_before_store_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            data.mkdir(mode=0o700)
            data.chmod(0o777)
            with self.assertRaises(ServerStoreError):
                _ = ServerOwnership(data)
            self.assertFalse((data / "server-state.sqlite3").exists())

    def test_gc_releases_exactly_once_without_affecting_new_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            owner = ServerOwnership(data)
            del owner
            _ = gc.collect()
            next_owner = ServerOwnership(data)
            try:
                _ = gc.collect()
                with self.assertRaises(ServerStoreError):
                    _ = ServerOwnership(data)
            finally:
                next_owner.close()

    def test_concurrent_threads_have_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            barrier = threading.Barrier(3)
            owners: list[ServerOwnership] = []
            rejected: list[ServerStoreError] = []

            def contend() -> None:
                _ = barrier.wait(timeout=5)
                try:
                    owners.append(ServerOwnership(data))
                except ServerStoreError as exc:
                    rejected.append(exc)

            threads = [threading.Thread(target=contend) for _ in range(2)]
            for thread in threads:
                thread.start()
            _ = barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            try:
                self.assertEqual(1, len(owners))
                self.assertEqual(1, len(rejected))
            finally:
                for owner in owners:
                    owner.close()

    def test_startup_failures_and_post_startup_exception_release_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            args = ["--data-dir", str(data), "--port", "0"]
            self.assertEqual(2, main(args + ["--import-client-keys", str(data / "missing")]))
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                port = cast(tuple[str, int], occupied.getsockname())[1]
                self.assertEqual(2, main(args + ["--port", str(port)]))
                self.assertEqual(2, main(args + ["--worker-listen-port", str(port)]))
            with mock.patch.object(GatewayHTTPServer, "serve_forever", side_effect=RuntimeError("synthetic")):
                with self.assertRaisesRegex(RuntimeError, "synthetic"):
                    _ = main(args)
            with mock.patch.object(GatewayHTTPServer, "serve_forever", side_effect=KeyboardInterrupt):
                self.assertEqual(0, main(args))

    def test_cleanup_error_still_closes_store_before_releasing_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            close = ServerStore.close
            closed: list[bool] = []

            def close_store(store: ServerStore) -> None:
                with self.assertRaises(ServerStoreError):
                    _ = ServerOwnership(data)
                close(store)
                closed.append(True)
                raise RuntimeError("synthetic cleanup failure")

            with mock.patch.object(GatewayHTTPServer, "serve_forever", side_effect=KeyboardInterrupt), \
                    mock.patch.object(ServerStore, "close", autospec=True, side_effect=close_store):
                with self.assertRaisesRegex(RuntimeError, "synthetic cleanup failure"):
                    _ = main(["--data-dir", str(data), "--port", "0", "--worker-listen-port", "0"])
            self.assertEqual([True], closed)
            restarted = ServerOwnership(data)
            restarted.close()

    def test_output_failure_after_binding_cleans_listeners_and_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            with mock.patch("builtins.print", side_effect=BrokenPipeError):
                with self.assertRaises(BrokenPipeError):
                    _ = main(["--data-dir", str(data), "--port", "0", "--worker-listen-port", "0"])
            restarted = ServerOwnership(data)
            restarted.close()


class ServerChild:
    """Actual CLI process; output reader provides bounded, portable readiness."""

    def __init__(self, data: Path, home: Path) -> None:
        self.process: subprocess.Popen[str] = subprocess.Popen(
            [sys.executable, "-m", "scarcity_router.control_server", "--data-dir", str(data), "--port", "0"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env={"PATH": os.environ.get("PATH", ""), "HOME": str(home)},
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.reader: threading.Thread = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        while line := cast(TextIO, self.process.stdout).readline():
            self.lines.put(line)
        self.lines.put(None)

    def ready(self) -> int | None:
        while True:
            line = self.lines.get(timeout=10)
            if line is None:
                return None
            match = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
            if match:
                return int(match.group(1))

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        _ = self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()


@final
class CrossProcessTests(unittest.TestCase):
    def start(self, data: Path, home: Path, processes: ExitStack) -> tuple[ServerChild, int]:
        child = ServerChild(data, home)
        _ = processes.callback(child.close)
        port = child.ready()
        self.assertIsNotNone(port)
        assert port is not None
        return child, port

    def exchange(self, port: int, path: str, payload: object | None = None) -> dict[str, object]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                "POST" if payload is not None else "GET", path,
                body=json.dumps(payload) if payload is not None else None,
                headers={"Authorization": "Bearer synthetic-observer", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            document = cast(dict[str, object], json.loads(response.read()))
            self.assertEqual(200, response.status)
            return document
        finally:
            connection.close()

    def prepare(self, data: Path) -> None:
        store = ServerStore.open(data)
        try:
            store.add_client_key(client_id="observer", key_hash=hash_token("synthetic-observer"),
                                 label="synthetic", created_at="2026-11-08T00:00:00.000Z", role="observer")
        finally:
            store.close()

    def test_independent_servers_claim_command_duplicates_and_crash_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as processes:
            root = Path(temporary)
            data = root / "data"
            self.prepare(data)
            first, port = self.start(data, root, processes)
            other, other_port = self.start(root / "independent", root, processes)
            self.assertEqual({"status": "ok"}, self.exchange(other_port, "/healthz"))
            before = self.exchange(port, "/observer/v1/snapshot")
            cursor = cast(dict[str, object], before["cursor"])
            for duplicate_port in (port, 0):
                result = subprocess.run(
                    [sys.executable, "-m", "scarcity_router.control_server", "--data-dir", str(data), "--port", str(duplicate_port)],
                    capture_output=True, timeout=10, check=False,
                    env={"PATH": os.environ.get("PATH", ""), "HOME": str(root)},
                )
                self.assertEqual(2, result.returncode)
                self.assertIn(b"another server", result.stderr)
                after = self.exchange(port, "/observer/v1/snapshot")
                self.assertEqual(cursor, after["cursor"])
                replay = self.exchange(port, "/observer/v1/replay", {"cursor": cursor})
                self.assertEqual([], replay["events"])
                self.assertIsNone(replay["resync_reason"])
            claim = subprocess.run(
                [sys.executable, "-m", "scarcity_router.control_server", "--data-dir", str(data), "--issue-bootstrap-claim"],
                capture_output=True, timeout=10, check=False,
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(root)},
            )
            self.assertEqual(0, claim.returncode)
            self.assertEqual(b"", claim.stderr)
            self.assertGreater(len(claim.stdout.strip()), 32)
            # Hash-only durable verification; never emit the transient claim.
            store = ServerStore.open(data)
            try:
                row = cast(tuple[str], store._connection.execute("SELECT token_hash FROM administrator_claim").fetchone())  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(hash_token(claim.stdout.decode().strip()), row[0])
            finally:
                store.close()
            self.assertEqual(cursor, self.exchange(port, "/observer/v1/snapshot")["cursor"])
            _ = self.exchange(port, "/control/bootstrap/admin", {
                "claim": claim.stdout.decode().strip(),
                "password": "synthetic-test-password-only", "confirm": True,
            })
            refused_claim = subprocess.run(
                [sys.executable, "-m", "scarcity_router.control_server", "--data-dir", str(data), "--issue-bootstrap-claim"],
                capture_output=True, timeout=10, check=False,
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(root)},
            )
            self.assertEqual(2, refused_claim.returncode)
            self.assertEqual(b"", refused_claim.stdout)
            self.assertEqual(cursor, self.exchange(port, "/observer/v1/snapshot")["cursor"])
            first.process.kill()
            _ = first.process.wait(timeout=10)
            restarted, next_port = self.start(data, root, processes)
            replay = self.exchange(next_port, "/observer/v1/replay", {"cursor": cursor})
            self.assertEqual("epoch_mismatch", replay["resync_reason"])
            self.assertNotEqual(cursor["epoch"], cast(dict[str, object], self.exchange(next_port, "/observer/v1/snapshot")["cursor"])["epoch"])
            restarted.close()
            other.close()

    def test_concurrent_process_startups_only_one_advances_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as processes:
            root = Path(temporary)
            data = root / "data"
            self.prepare(data)
            children = [ServerChild(data, root) for _ in range(2)]
            for child in children:
                _ = processes.callback(child.close)
            ports = [child.ready() for child in children]
            self.assertEqual(1, sum(port is not None for port in ports))
            port = next(port for port in ports if port is not None)
            cut = self.exchange(port, "/observer/v1/snapshot")
            cursor = cast(dict[str, object], cut["cursor"])
            self.assertEqual(0, cursor["sequence"])
            loser = children[ports.index(None)]
            self.assertEqual(2, loser.process.wait(timeout=10))
            self.assertEqual(cursor, self.exchange(port, "/observer/v1/snapshot")["cursor"])

    @unittest.skipUnless(os.name == "posix", "POSIX interrupt for graceful CLI shutdown")
    def test_normal_shutdown_releases_owner_and_restart_resynchronizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as processes:
            root = Path(temporary)
            data = root / "data"
            self.prepare(data)
            first, port = self.start(data, root, processes)
            before = self.exchange(port, "/observer/v1/snapshot")
            first.process.send_signal(signal.SIGINT)
            self.assertEqual(0, first.process.wait(timeout=10))
            _, next_port = self.start(data, root, processes)
            replay = self.exchange(next_port, "/observer/v1/replay", {"cursor": before["cursor"]})
            self.assertEqual("epoch_mismatch", replay["resync_reason"])
