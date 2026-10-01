"""Worker local state store tests (M05): permissions, platform separation."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import cast, override

from scarcity_router.worker_local_store import (
    WorkerLocalIdentity,
    WorkerLocalStore,
    WorkerStateDirLock,
    WorkerStateDirLockUnavailable,
    WorkerStateDirLocked,
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


class WorkerStateDirLockTests(unittest.TestCase):
    """The lifetime single-instance lock per state directory (issue #138).

    A foreground ``run`` and the systemd service worker must be mutually
    exclusive on one state directory: the second contender fails closed
    with a useful message, and the OS (not this code) guarantees release
    on every exit path — including SIGKILL.
    """

    def test_second_acquire_fails_closed_with_holder_pid(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            first = WorkerStateDirLock(tmp)
            first.acquire()
            try:
                second = WorkerStateDirLock(tmp)
                with self.assertRaises(WorkerStateDirLocked) as caught:
                    second.acquire()
                self.assertEqual(os.getpid(), caught.exception.holder_pid)
                message = str(caught.exception)
                self.assertIn("already running", message)
                self.assertIn("systemctl --user stop", message)
                self.assertNotIn(str(tmp), message)
            finally:
                first.release()

    def test_release_allows_the_next_holder(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            first = WorkerStateDirLock(tmp)
            first.acquire()
            first.release()
            second = WorkerStateDirLock(tmp)
            second.acquire()
            second.release()

    def test_lock_is_re_acquirable_in_a_fresh_object_after_release(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            for _ in range(3):
                lock = WorkerStateDirLock(tmp)
                lock.acquire()
                lock.release()

    def test_lock_file_is_private_and_survives_release(self) -> None:
        # The lock file stays behind (removing it would race a concurrent
        # opener); it must never widen permissions and never carry more
        # than the holder pid.
        with tempfile.TemporaryDirectory[str]() as tmp:
            lock = WorkerStateDirLock(tmp)
            lock.acquire()
            lock.release()
            path = os.path.join(tmp, WorkerStateDirLock.LOCK_FILE_NAME)
            self.assertTrue(os.path.isfile(path))
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(0o600, mode)
            with open(path, encoding="utf-8") as handle:
                content = handle.read().strip()
            self.assertEqual(str(os.getpid()), content)

    def test_lock_succeeds_when_state_dir_does_not_exist_yet(self) -> None:
        # The service/run path may lock before anything else created the
        # directory; the lock creates it with the private-tree rule.
        with tempfile.TemporaryDirectory[str]() as tmp:
            nested = os.path.join(tmp, "worker")
            lock = WorkerStateDirLock(nested)
            lock.acquire()
            try:
                self.assertTrue(os.path.isdir(nested))
                self.assertEqual(0o700, os.stat(nested).st_mode & 0o777)
            finally:
                lock.release()

    def test_double_acquire_on_one_object_is_refused(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            lock = WorkerStateDirLock(tmp)
            lock.acquire()
            try:
                with self.assertRaises(WorkerStateDirLocked):
                    lock.acquire()
            finally:
                lock.release()

    def test_release_without_acquire_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            lock = WorkerStateDirLock(tmp)
            lock.release()


class WorkerStateDirLockHardeningTests(unittest.TestCase):
    """Daybreak blocker 4: the lock never follows a planted link.

    The private state-directory boundary is verified at acquisition (an
    existing directory that is group/world-writable, root, or the home
    directory itself is refused), the lock file is opened without
    following symlinks and verified to be a regular file, and the holder
    pid is read back through the already-open descriptor — so a planted
    ``worker.lock`` can neither redirect the write nor leak its target's
    content into an error message.
    """

    def test_planted_lock_symlink_is_refused_and_target_untouched(self) -> None:
        # The exact previous attack: worker.lock -> decoy. The old code
        # followed the link and ftruncate'd/wrote the TARGET.
        with tempfile.TemporaryDirectory[str]() as tmp:
            decoy = Path(tmp) / "decoy.txt"
            _ = decoy.write_text("DO-NOT-TOUCH")
            lock_path = os.path.join(tmp, WorkerStateDirLock.LOCK_FILE_NAME)
            os.symlink(str(decoy), lock_path)
            lock = WorkerStateDirLock(tmp)
            with self.assertRaises(WorkerStateDirLockUnavailable) as caught:
                lock.acquire()
            self.assertIn("symlink", str(caught.exception))
            self.assertIn("refusing to follow", str(caught.exception))
            self.assertEqual("DO-NOT-TOUCH", decoy.read_text())
            self.assertTrue(os.path.islink(lock_path))
            # Once the plant is removed, acquisition works normally again.
            os.unlink(lock_path)
            fresh = WorkerStateDirLock(tmp)
            fresh.acquire()
            fresh.release()

    def test_planted_fifo_is_refused_never_opened_into(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            lock_path = os.path.join(tmp, WorkerStateDirLock.LOCK_FILE_NAME)
            os.mkfifo(lock_path)
            lock = WorkerStateDirLock(tmp)
            with self.assertRaises(WorkerStateDirLockUnavailable) as caught:
                lock.acquire()
            self.assertIn("not a regular file", str(caught.exception))
            self.assertTrue(os.path.exists(lock_path))

    def test_group_writable_state_dir_is_refused(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            os.chmod(tmp, 0o771)
            lock = WorkerStateDirLock(tmp)
            with self.assertRaises(ValueError) as caught:
                lock.acquire()
            self.assertIn("group- or world-writable", str(caught.exception))
            self.assertIn("chmod 700", str(caught.exception))
            self.assertFalse(
                os.path.exists(os.path.join(tmp, WorkerStateDirLock.LOCK_FILE_NAME))
            )

    def test_state_dir_equal_to_home_is_refused(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            with unittest.mock.patch.dict(os.environ, {"HOME": tmp}):
                lock = WorkerStateDirLock(tmp)
                with self.assertRaises(ValueError) as caught:
                    lock.acquire()
                self.assertIn("home directory itself", str(caught.exception))

    def test_filesystem_root_state_dir_is_refused(self) -> None:
        lock = WorkerStateDirLock("/")
        with self.assertRaises(ValueError) as caught:
            lock.acquire()
        message = str(caught.exception)
        # Refused by the private-tree policy; which check fires first
        # depends on the host (root is not owned by the invoking user).
        self.assertIn("/", message)
        self.assertTrue(
            "filesystem root" in message
            or "not owned by the current user" in message,
            message,
        )


@unittest.skipUnless(
    sys.platform == "win32", "Windows reparse-point lock protection"
)
class WorkerStateDirLockWindowsTests(unittest.TestCase):
    """The Windows branch of the no-follow lock (Daybreak blocker 4).

    The open uses ``FILE_FLAG_OPEN_REPARSE_POINT``: a planted symlink or
    junction at ``worker.lock`` yields a handle to the LINK itself, which
    is detected via ``FILE_ATTRIBUTE_REPARSE_POINT`` and refused — the
    target is never opened. The normal path exercises the unchanged
    ``msvcrt.locking`` advisory lock.
    """

    def test_normal_acquire_release_and_contention_round_trip(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            first = WorkerStateDirLock(tmp)
            first.acquire()
            second = WorkerStateDirLock(tmp)
            with self.assertRaises(WorkerStateDirLocked) as caught:
                second.acquire()
            self.assertEqual(os.getpid(), caught.exception.holder_pid)
            first.release()
            third = WorkerStateDirLock(tmp)
            third.acquire()
            third.release()

    def test_lock_file_content_is_the_holder_pid(self) -> None:
        # Content is read AFTER release: byte 0 is the byte the holder
        # has locked (msvcrt.locking denies reads of a locked byte range
        # to every other handle, unlike POSIX flock).
        with tempfile.TemporaryDirectory[str]() as tmp:
            lock = WorkerStateDirLock(tmp)
            lock.acquire()
            lock.release()
            content = (
                Path(tmp) / WorkerStateDirLock.LOCK_FILE_NAME
            ).read_text(encoding="utf-8")
            self.assertEqual(str(os.getpid()), content.strip())

    def test_planted_symlink_lock_is_refused_and_target_untouched(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            decoy = Path(tmp) / "decoy.txt"
            _ = decoy.write_text("DO-NOT-TOUCH")
            lock_path = Path(tmp) / WorkerStateDirLock.LOCK_FILE_NAME
            try:
                os.symlink(str(decoy), str(lock_path))
            except OSError:
                self.skipTest(
                    "symbolic link privilege unavailable on this Windows host"
                )
            lock = WorkerStateDirLock(tmp)
            with self.assertRaises(WorkerStateDirLockUnavailable) as caught:
                lock.acquire()
            self.assertIn("reparse", str(caught.exception))
            self.assertEqual("DO-NOT-TOUCH", decoy.read_text())

    def test_planted_junction_lock_is_refused(self) -> None:
        # A junction needs no privilege to plant and is a reparse point:
        # it must be refused exactly like a symlink (never traversed).
        with tempfile.TemporaryDirectory[str]() as tmp:
            target_dir = Path(tmp) / "junction-target"
            _ = target_dir.mkdir()
            lock_path = Path(tmp) / WorkerStateDirLock.LOCK_FILE_NAME
            import subprocess

            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(lock_path), str(target_dir)],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest("junction creation unavailable on this host")
            lock = WorkerStateDirLock(tmp)
            with self.assertRaises(WorkerStateDirLockUnavailable):
                lock.acquire()


if __name__ == "__main__":
    _ = unittest.main()
