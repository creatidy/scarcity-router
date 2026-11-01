"""Bounded durable producer journal, independent of audit sequences."""

from pathlib import Path
import os
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scarcity_router.server_store import ServerStore, ServerStoreError


class ProducerStoreTests(TestCase):
    def test_committed_event_survives_abrupt_process_exit(self) -> None:
        with TemporaryDirectory() as directory:
            store = ServerStore.open(Path(directory))
            epoch, _ = store.start_producer_epoch()
            store.close()
            code = (
                "import os,sys; from pathlib import Path; "
                "from scarcity_router.server_store import ServerStore; "
                "s=ServerStore.open(Path(sys.argv[1])); "
                "s.append_producer_event(sys.argv[2], '{\"ok\":true}'); os._exit(0)"
            )
            result = subprocess.run(
                [sys.executable, "-c", code, directory, epoch],
                env={"PATH": os.defpath, "HOME": directory}, check=False, capture_output=True, timeout=30,
            )
            self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", errors="replace"))
            store = ServerStore.open(Path(directory))
            try:
                self.assertEqual((1, ((1, '{"ok":true}'),), None), store.read_producer_events(epoch, 0))
                next_epoch, sequence = store.start_producer_epoch()
                self.assertNotEqual(epoch, next_epoch)
                self.assertEqual((1, 1), (sequence, store.read_producer_events(epoch, 0)[0]))
                self.assertEqual("epoch_mismatch", store.read_producer_events(epoch, 0)[2])
            finally:
                store.close()

    def test_retention_gap_and_restart_epoch(self) -> None:
        with TemporaryDirectory() as directory:
            store = ServerStore.open(Path(directory))
            try:
                epoch, start = store.start_producer_epoch()
                self.assertEqual(0, start)
                with patch("scarcity_router.server_store._PRODUCER_EVENT_LIMIT", 2):
                    self.assertEqual(1, store.append_producer_event(epoch, '{"revision":1}'))
                    self.assertEqual(2, store.append_producer_event(epoch, '{"revision":2}'))
                    self.assertEqual(3, store.append_producer_event(epoch, '{"revision":3}'))
                    self.assertEqual((3, (), "cursor_expired"), store.read_producer_events(epoch, 0))
                    high, events, gap = store.read_producer_events(epoch, 1)
                    self.assertEqual((3, None), (high, gap))
                    self.assertEqual((2, 3), tuple(sequence for sequence, _ in events))
                store.close()
                store = ServerStore.open(Path(directory))
                new_epoch, sequence = store.start_producer_epoch()
                self.assertNotEqual(epoch, new_epoch)
                self.assertEqual(3, sequence)
                self.assertEqual((3, (), "epoch_mismatch"), store.read_producer_events(epoch, 3))
                with self.assertRaises(ServerStoreError):
                    _ = store.append_producer_event(epoch, "{}")
                self.assertEqual(4, store.append_producer_event(new_epoch, "{}"))
            finally:
                store.close()

    def test_invalid_and_oversized_records_fail_without_advancing(self) -> None:
        with TemporaryDirectory() as directory:
            store = ServerStore.open(Path(directory))
            try:
                epoch, sequence = store.start_producer_epoch()
                with patch("scarcity_router.server_store._PRODUCER_RECORD_BYTES", 2):
                    with self.assertRaises(ServerStoreError):
                        _ = store.append_producer_event(epoch, "abc")
                self.assertEqual((sequence, (), None), store.read_producer_events(epoch, sequence))
                for cursor in (-1, True):
                    with self.assertRaises(ServerStoreError):
                        _ = store.read_producer_events(epoch, cursor)
            finally:
                store.close()

    def test_missing_middle_sequence_and_byte_retention_require_resync(self) -> None:
        with TemporaryDirectory() as directory:
            store = ServerStore.open(Path(directory))
            try:
                epoch, _ = store.start_producer_epoch()
                for payload in ("aaa", "bbb", "ccc"):
                    _ = store.append_producer_event(epoch, payload)
                with sqlite3.connect(store.path) as connection:
                    _ = connection.execute("DELETE FROM producer_events WHERE sequence = 2")
                self.assertEqual((3, (), "history_gap"), store.read_producer_events(epoch, 0))
                with patch("scarcity_router.server_store._PRODUCER_EVENT_BYTES", 3):
                    self.assertEqual(4, store.append_producer_event(epoch, "ddd"))
                self.assertEqual((4, (), "cursor_expired"), store.read_producer_events(epoch, 2))
                self.assertEqual((4, ((4, "ddd"),), None), store.read_producer_events(epoch, 3))
            finally:
                store.close()
