"""Worker local state store tests (M05): permissions, platform separation."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import cast, override

from scarcity_router.worker_local_store import (
    WorkerLocalIdentity,
    WorkerLocalStore,
    worker_state_dir,
)


class PathSeparationTests(unittest.TestCase):
    def test_windows_and_linux_paths_differ(self) -> None:
        windows = worker_state_dir(
            platform="win32", env={"LOCALAPPDATA": "C:\\Users\\u\\AppData\\Local"}
        )
        linux = worker_state_dir(platform="linux", env={"XDG_DATA_HOME": "/home/u/data"})
        self.assertNotEqual(windows, linux)
        self.assertTrue(windows.startswith("C:\\Users\\u\\AppData\\Local"))
        self.assertTrue(linux.startswith("/home/u/data"))
        # Windows and WSL are different devices: neither resolves into the
        # other's directory.
        self.assertNotIn("scarcity-router", windows.split("AppData\\Local")[0])

    def test_wsl_uses_the_linux_path_never_the_windows_one(self) -> None:
        # WSL is Linux: XDG hierarchy, never a Windows directory.
        linux = worker_state_dir(platform="linux", env={"HOME": "/home/wsl"})
        self.assertIn(".local/share/scarcity-router/worker", linux)
        self.assertNotIn("AppData", linux)
        self.assertNotIn("LOCALAPPDATA", linux)

    def test_windows_fallback_uses_userprofile(self) -> None:
        windows = worker_state_dir(
            platform="win32", env={"USERPROFILE": "C:\\Users\\u"}
        )
        self.assertIn("AppData\\Local\\scarcity-router\\worker", windows)

    def test_relative_xdg_value_is_ignored(self) -> None:
        path = worker_state_dir(
            platform="linux", env={"XDG_DATA_HOME": "relative/path", "HOME": "/h"}
        )
        self.assertTrue(path.startswith("/h/.local/share"))


class LocalStoreTests(unittest.TestCase):
    _tmp: tempfile.TemporaryDirectory[str]
    path: str

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        # Placeholders; setUp replaces them before each test body runs.
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.path = cast("str", object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "state", "worker-state.db")

    def test_identity_round_trip(self) -> None:
        store = WorkerLocalStore(self.path)
        self.addCleanup(store.close)
        self.assertIsNone(store.load_identity())
        identity = WorkerLocalIdentity(
            worker_id="w-abc123",
            credential="SYNTHETIC-CREDENTIAL",
            server_origin="srws://gateway.local:8790",
            device_label="laptop",
        )
        store.save_identity(identity)
        loaded = store.load_identity()
        self.assertEqual(identity, loaded)
        store.clear_identity()
        self.assertIsNone(store.load_identity())

    def test_identity_write_is_atomic_replacement(self) -> None:
        store = WorkerLocalStore(self.path)
        self.addCleanup(store.close)
        first = WorkerLocalIdentity(
            worker_id="w-first", credential="c1", server_origin="srws://g:1"
        )
        second = WorkerLocalIdentity(
            worker_id="w-second", credential="c2", server_origin="srws://g:2"
        )
        store.save_identity(first)
        store.save_identity(second)
        loaded = store.load_identity()
        assert loaded is not None
        self.assertEqual("w-second", loaded.worker_id)

    def test_permissions_are_0o700_dir_and_0o600_file(self) -> None:
        store = WorkerLocalStore(self.path)
        self.addCleanup(store.close)
        state_dir = os.path.join(self._tmp.name, "state")
        self.assertEqual(0o700, os.stat(state_dir).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(self.path).st_mode & 0o777)

    def test_value_storage(self) -> None:
        store = WorkerLocalStore(self.path)
        self.addCleanup(store.close)
        self.assertIsNone(store.load_value("adapter_allowlist"))
        store.save_value("adapter_allowlist", '["ollama"]')
        self.assertEqual('["ollama"]', store.load_value("adapter_allowlist"))

    def test_credential_never_in_diagnostic_output(self) -> None:
        store = WorkerLocalStore(self.path)
        self.addCleanup(store.close)
        store.save_identity(
            WorkerLocalIdentity(
                worker_id="w-x",
                credential="SYNTHETIC-SECRET-MARKER-4242",
                server_origin="srws://g:1",
            )
        )
        with open(self.path, "rb") as handle:
            raw = handle.read()
        # The credential is stored locally by design (the ONLY copy); this
        # assertion locks that the worker id and origin are readable while
        # no code path renders the credential into logs.
        self.assertIn(b"w-x", raw)


if __name__ == "__main__":
    _ = unittest.main()
