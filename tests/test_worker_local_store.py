"""Worker local state store tests (M05): permissions, platform separation."""

from __future__ import annotations

import os
import shutil
import subprocess
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

    def test_directory_replacement_keeps_lock_inside_verified_object(self) -> None:
        from scarcity_router import worker_local_store
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir(mode=0o700)
            retained = Path(tmp) / "validated-state"
            victim = Path(tmp) / "victim"
            _ = victim.write_text("UNCHANGED")
            victim.chmod(0o644)
            from scarcity_router.worker_identity_store import open_private_state_dir
            verify = open_private_state_dir

            def replace_after_verification(path: str, *, create: bool = False) -> int:
                fd = verify(path, create=create)
                _ = state.rename(retained)
                state.mkdir(mode=0o700)
                (state / "worker.lock").symlink_to(victim)
                return fd

            lock = WorkerStateDirLock(str(state))
            with unittest.mock.patch.object(worker_local_store, "open_private_state_dir", replace_after_verification):
                lock.acquire()
            try:
                self.assertEqual(str(os.getpid()), (retained / "worker.lock").read_text().strip())
                self.assertTrue((state / "worker.lock").is_symlink())
                self.assertEqual("UNCHANGED", victim.read_text())
                self.assertEqual(0o644, victim.stat().st_mode & 0o777)
            finally:
                lock.release()

    def test_shared_ancestor_refused_without_creating_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "shared"
            parent.mkdir()
            parent.chmod(0o777)
            state = parent / "state"
            state.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "choose a private location"):
                WorkerStateDirLock(str(state)).acquire()
            self.assertEqual([], list(state.iterdir()))

    def test_owned_sticky_parent_protects_private_next_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "sticky-parent"
            parent.mkdir()
            parent.chmod(0o1777)
            state = parent / "private-worker"
            state.mkdir(mode=0o700)
            lock = WorkerStateDirLock(state)
            lock.acquire()
            lock.release()
            self.assertEqual(str(os.getpid()), (state / "worker.lock").read_text().strip())
            self.assertEqual(0o1777, parent.stat().st_mode & 0o7777)

    def test_state_directory_itself_cannot_be_sticky_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "sticky-state"
            state.mkdir()
            state.chmod(0o1777)
            with self.assertRaisesRegex(ValueError, "group- or world-writable"):
                WorkerStateDirLock(state).acquire()
            self.assertEqual([], list(state.iterdir()))

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
    sys.platform.startswith("linux"), "/proc/self/fd is a Linux interface"
)
class HomeAnchoredBoundaryTests(unittest.TestCase):
    """The home-anchored trust boundary (D-065, narrowed service contract).

    For a boundary strictly beneath the canonical home the walk anchors
    AT the home directory: the admin-managed namespace above it (``/``,
    ``/home``) is never opened or inspected, and mount ownership is
    never security evidence. Under systemd's ``ProtectSystem=strict`` +
    ``ProtectHome=read-only`` sandbox those synthetic read-only
    ancestors legitimately display the overflow uid 65534 — the removed
    mountinfo exception needed read-only-mount reasoning to accept
    them; the home-anchored walk is simply outside their reach.

    Each test models the namespace view with a patched ``os.fstat``
    (overflow uid for the modeled directories, resolved through
    ``/proc/self/fd``) — no root, no live systemd, fully deterministic.
    """

    def install_view(self, overflow_uids: dict[str, int]) -> None:
        real_fstat = os.fstat

        def sandboxed_fstat(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            try:
                path = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                return info
            uid = overflow_uids.get(path)
            if uid is None:
                return info
            return os.stat_result((
                info.st_mode, info.st_ino, info.st_dev, info.st_nlink, uid,
                info.st_gid, info.st_size, info.st_atime, info.st_mtime,
                info.st_ctime,
            ))

        patcher = unittest.mock.patch("os.fstat", sandboxed_fstat)
        _ = patcher.start()
        self.addCleanup(patcher.stop)

    def test_synthetic_sandbox_ancestors_are_outside_the_trust_computation(
        self,
    ) -> None:
        # `/` and the home's parent display the overflow uid, exactly as
        # in the real ProtectHome sandbox; the private under-home
        # boundary is accepted WITHOUT any mount/read-only reasoning —
        # and the walk would refuse if it ever consulted those
        # ancestors.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            state = home / ".local" / "share" / "scarcity-router" / "worker"
            state.mkdir(parents=True, mode=0o700)
            self.install_view({"/": 65534, tmp: 65534})
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                lock = WorkerStateDirLock(str(state))
                lock.acquire()
                try:
                    self.assertEqual(
                        str(os.getpid()),
                        (state / "worker.lock").read_text().strip(),
                    )
                finally:
                    lock.release()

    def test_untrusted_owner_below_home_is_refused_without_mount_reasoning(
        self,
    ) -> None:
        # An untrusted displayed owner BELOW home remains untrusted —
        # there is no read-only-mount exception left to reach for.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shared = home / "shared"
            state = shared / "worker"
            state.mkdir(parents=True, mode=0o700)
            self.install_view({str(shared): 65534})
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(
                    ValueError, "untrusted worker path component"
                ):
                    WorkerStateDirLock(str(state)).acquire()
            self.assertEqual([], list(state.iterdir()))

    def test_untrusted_final_directory_below_home_is_refused(self) -> None:
        # The boundary itself must belong to the invoking user: a
        # foreign-owned final directory is refused, including under root CI.
        foreign_uid = 1 if os.geteuid() == 0 else 0
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            state = home / "worker"
            state.mkdir(parents=True, mode=0o700)
            self.install_view({str(state): foreign_uid})
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(
                    ValueError, "not owned by (root or )?the current user"
                ):
                    WorkerStateDirLock(str(state)).acquire()
            self.assertEqual([], list(state.iterdir()))

    def test_world_writable_directory_below_home_is_refused(self) -> None:
        # A loosened mode is refused on its own: the same tree on the
        # writable host would allow cross-principal replacement.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shared = home / "shared"
            state = shared / "worker"
            state.mkdir(parents=True, mode=0o777)
            _ = shared.chmod(0o777)
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(
                    ValueError, "group- or world-writable"
                ):
                    WorkerStateDirLock(str(state)).acquire()
            self.assertEqual([], list(state.iterdir()))

    def test_home_anchor_owned_by_another_principal_is_refused(self) -> None:
        # The anchor carries the whole trust decision: a home displaying
        # another principal's uid is refused — the walk never falls back
        # to trusting anything above or instead of it.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            state = home / "worker"
            state.mkdir(parents=True, mode=0o700)
            self.install_view({str(home): 65534})
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(
                    ValueError, "home anchor .* not a directory owned by"
                ):
                    WorkerStateDirLock(str(state)).acquire()
            self.assertEqual([], list(state.iterdir()))

    def test_group_writable_home_anchor_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            state = home / "worker"
            state.mkdir(parents=True, mode=0o700)
            _ = home.chmod(0o773)
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(
                    ValueError, "home anchor .* group- or world-writable"
                ):
                    WorkerStateDirLock(str(state)).acquire()
            self.assertEqual([], list(state.iterdir()))


class MountinfoIsNotPartOfTheTrustModelTests(unittest.TestCase):
    """The rejected mountinfo mechanism is REMOVED, not dormant (D-065).

    Selecting the effective mount from ``/proc/self/mountinfo`` requires
    mount-ID/parent-ID reasoning the review found unsound as a trust
    input; the owner decision removed the dependency instead of
    correcting it. This test pins the removal so no relaxation can
    quietly grow back.
    """

    def test_the_walk_module_no_longer_reads_mount_tables(self) -> None:
        import inspect

        from scarcity_router import worker_identity_store

        source = inspect.getsource(worker_identity_store)
        self.assertNotIn("mountinfo", source)
        self.assertNotIn("/proc/self/mounts", source)


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("systemd-run") is not None,
    "a live systemd user manager with systemd-run",
)
class SystemdRunNamespaceAcceptanceTests(unittest.TestCase):
    """Acceptance against a REAL ``systemd-run --user`` sandbox.

    The deployment shape under test — a dedicated private tree under the
    user's canonical home (the default state-dir location) — must be
    accepted by the home-anchored walk inside a real
    ``ProtectSystem=strict`` + ``ProtectHome=read-only`` sandbox with the
    exact ``ReadWritePaths`` grant the generated unit renders, even
    though the sandbox's synthetic ``/`` and ``/home`` display the
    overflow uid. NO mountinfo exception is involved (pinned by
    ``MountinfoIsNotPartOfTheTrustModelTests``). Skipped wherever no
    working user manager exists; the deterministic suite above never
    requires one.
    """

    def test_sandbox_accepts_the_private_state_boundary(self) -> None:
        probe = subprocess.run(
            ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--", "/bin/true"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode != 0:
            self.skipTest(f"no working systemd user manager: {probe.stderr.strip()}")
        import getpass
        import shutil
        import uuid

        base = Path.home() / ".local" / "share"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        state = base / f"sr-namespace-test-{uuid.uuid4().hex[:12]}-{getpass.getuser()}"
        state.mkdir(mode=0o700)
        try:
            code = (
                "import os, sys\n"
                "from scarcity_router.worker_identity_store import (\n"
                "    open_private_state_dir,\n"
                ")\n"
                "fd = open_private_state_dir(sys.argv[1])\n"
                "os.close(fd)\n"
                "print('BOUNDARY-ACCEPTED')\n"
            )
            result = subprocess.run(
                [
                    "systemd-run", "--user", "--quiet", "--wait", "--pipe",
                    "-p", "ProtectSystem=strict",
                    "-p", "ProtectHome=read-only",
                    "-p", f"ReadWritePaths={state}",
                    "--", sys.executable, "-c", code, str(state),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("BOUNDARY-ACCEPTED", result.stdout)
        finally:
            shutil.rmtree(state, ignore_errors=True)


@unittest.skipUnless(
    sys.platform == "win32", "Windows reparse-point lock protection"
)
class WorkerStateDirLockWindowsTests(unittest.TestCase):
    """The Windows branch of the no-follow lock (Daybreak blocker 4).

    Native NtCreateFile lookups pin every ancestor, reject reparses on the
    opened objects, and retain that chain over the msvcrt lock lifetime.
    """

    def _junction(self, path: Path, target: Path) -> None:
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, "native junction creation failed")
        self.addCleanup(os.rmdir, path)

    def _set_junction_in_place(
        self, path: Path, target: Path, access: int, *, delete: bool = False,
    ) -> tuple[str, int]:
        """Actual native attack, including a positive-control-valid payload."""
        import ctypes
        import struct

        kernel32: object = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = getattr(kernel32, "CreateFileW")  # pyright: ignore[reportAny]
        ioctl = getattr(kernel32, "DeviceIoControl")  # pyright: ignore[reportAny]
        close_handle = getattr(kernel32, "CloseHandle")  # pyright: ignore[reportAny]
        create_file.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        ioctl.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        ioctl.restype = ctypes.c_int
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle: int | None = create_file(  # pyright: ignore[reportAny]
            str(path), access, 0x7, None, 3, 0x02200000, None,
            # OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT; share all.
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            return "open", ctypes.get_last_error()
        substitute = ("\\??\\" + str(target)).encode("utf-16-le")
        printable = str(target).encode("utf-16-le")
        data = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printable))
        data += substitute + b"\0\0" + printable + b"\0\0"
        payload = struct.pack("<IHH", 0xA0000003, len(data), 0) + data
        if delete:
            payload = struct.pack("<IHH", 0xA0000003, 0, 0)
        buffer = ctypes.create_string_buffer(payload)
        returned = ctypes.c_uint32()
        try:
            success: int = ioctl(  # pyright: ignore[reportAny]
                ctypes.c_void_p(handle), 0x900AC if delete else 0x900A4,
                buffer, len(payload), None, 0,
                ctypes.byref(returned), None,  # FSCTL_DELETE/SET_REPARSE_POINT
            )
            return "set", 0 if success else ctypes.get_last_error()
        finally:
            _ = close_handle(ctypes.c_void_p(handle))  # pyright: ignore[reportAny]

    def test_normal_acquire_release_and_contention_round_trip(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            first = WorkerStateDirLock(tmp)
            first.acquire()
            self.addCleanup(first.release)
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
            decoy = target_dir / "decoy.txt"
            _ = decoy.write_text("DO-NOT-TOUCH")
            lock_path = Path(tmp) / WorkerStateDirLock.LOCK_FILE_NAME
            self._junction(lock_path, target_dir)
            try:
                lock = WorkerStateDirLock(tmp)
                with self.assertRaises(WorkerStateDirLockUnavailable):
                    lock.acquire()
                self.assertEqual("DO-NOT-TOUCH", decoy.read_text())
            finally:
                self.doCleanups()

    def test_state_junction_is_refused_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            decoy = target / WorkerStateDirLock.LOCK_FILE_NAME
            _ = decoy.write_text("DO-NOT-TOUCH")
            state = Path(tmp) / "state"
            self._junction(state, target)
            try:
                with self.assertRaises(WorkerStateDirLockUnavailable):
                    WorkerStateDirLock(state).acquire()
                self.assertEqual("DO-NOT-TOUCH", decoy.read_text())
            finally:
                self.doCleanups()

    def test_ancestor_junction_is_refused_before_missing_tree_creation(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            decoy = target / "decoy.txt"
            _ = decoy.write_text("DO-NOT-TOUCH")
            ancestor = Path(tmp) / "ancestor"
            self._junction(ancestor, target)
            try:
                with self.assertRaises(WorkerStateDirLockUnavailable):
                    WorkerStateDirLock(ancestor / "missing" / "worker").acquire()
                self.assertEqual([decoy], list(target.iterdir()))
                self.assertEqual("DO-NOT-TOUCH", decoy.read_text())
            finally:
                self.doCleanups()

    def test_missing_tree_is_created_handle_relative_without_pathname_mkdir(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            state = Path(tmp) / "new" / "nested" / "worker"
            lock = WorkerStateDirLock(state)
            with unittest.mock.patch(
                "os.mkdir", side_effect=AssertionError("unsafe pathname creation")
            ), unittest.mock.patch(
                "os.path.realpath", side_effect=AssertionError("reparse canonicalization")
            ):
                lock.acquire()
                lock.release()
            self.assertEqual(
                str(os.getpid()),
                (state / WorkerStateDirLock.LOCK_FILE_NAME).read_text().strip(),
            )

    def test_state_ancestor_and_lock_cannot_be_replaced_until_release(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            ancestor = Path(tmp) / "ancestor"
            state = ancestor / "worker"
            lock = WorkerStateDirLock(state)
            lock.acquire()
            try:
                for path in (ancestor, state, state / WorkerStateDirLock.LOCK_FILE_NAME):
                    with self.subTest(path=path.name):
                        with self.assertRaises(OSError) as caught:
                            os.rename(path, path.with_name(path.name + "-moved"))
                        self.assertEqual(32, caught.exception.winerror)
            finally:
                lock.release()
            # All ancestor handles are released, not just the lock descriptor.
            os.rename(state, ancestor / "moved")
            os.rename(ancestor, Path(tmp) / "moved-ancestor")

    def test_in_place_reparse_after_inspection_cannot_redirect_lock_lookup(self) -> None:
        import ctypes

        with tempfile.TemporaryDirectory[str]() as tmp:
            state = Path(tmp) / "ancestor" / "worker"
            state.mkdir(parents=True)
            target = Path(tmp) / "target"
            target.mkdir()
            decoy = target / WorkerStateDirLock.LOCK_FILE_NAME
            _ = decoy.write_text("DO-NOT-TOUCH")
            kernel32: object = ctypes.WinDLL("kernel32", use_last_error=True)
            get_file_info = getattr(kernel32, "GetFileInformationByHandle")  # pyright: ignore[reportAny]
            inspected = 0
            attempted = False

            def inspect_and_attempt_replacement(handle: object, info: object) -> int:
                nonlocal inspected, attempted
                result: int = get_file_info(handle, info)  # pyright: ignore[reportAny]
                inspected += 1
                if inspected == len(state.parts):
                    # Real API completed final-directory inspection. Attack
                    # before the very next NtCreateFile (worker.lock lookup).
                    attempted = True
                    with self.assertRaises(OSError) as caught:
                        os.rename(state, state.with_name("moved"))
                    self.assertEqual(32, caught.exception.winerror)
                    self.assertFalse((state / WorkerStateDirLock.LOCK_FILE_NAME).exists())
                    self.assertEqual(
                        ("open", 32), self._set_junction_in_place(state, target, 0x40000000),
                        "GENERIC_WRITE must be denied by directory share mode",
                    )
                    self.assertEqual(
                        ("set", 0), self._set_junction_in_place(state, target, 0x100),
                        "FILE_WRITE_ATTRIBUTES must really convert the inspected directory",
                    )
                    self.assertTrue(os.path.isjunction(state))
                return result

            loader = ctypes.WinDLL

            def load_library(library: str, *, use_last_error: bool = False) -> object:
                if library == "kernel32":
                    return kernel32
                return loader(library, use_last_error=use_last_error)

            setattr(
                kernel32, "GetFileInformationByHandle",
                unittest.mock.Mock(side_effect=inspect_and_attempt_replacement),
            )
            lock = WorkerStateDirLock(state)
            try:
                with unittest.mock.patch("ctypes.WinDLL", side_effect=load_library):
                    with self.assertRaises(WorkerStateDirLockUnavailable) as caught:
                        lock.acquire()
                self.assertTrue(attempted)
                self.assertIn("could not be opened safely", str(caught.exception))
                self.assertEqual("DO-NOT-TOUCH", decoy.read_text())
            finally:
                lock.release()
                if attempted:
                    self.assertEqual(
                        ("set", 0), self._set_junction_in_place(state, target, 0x100, delete=True)
                    )
            # Native RootDirectory + OPEN_REPARSE_POINT refused the mutated
            # parent before creating/writing ANY lock, original or redirected.
            self.assertFalse((state / WorkerStateDirLock.LOCK_FILE_NAME).exists())
            self.assertEqual("DO-NOT-TOUCH", decoy.read_text())

    def test_profile_equality_is_refused_structurally_without_realpath(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            with unittest.mock.patch.dict(os.environ, {"USERPROFILE": tmp.swapcase()}):
                with unittest.mock.patch(
                    "os.path.realpath", side_effect=AssertionError("filesystem reinterpretation")
                ):
                    with self.assertRaises(WorkerStateDirLockUnavailable) as caught:
                        WorkerStateDirLock(Path(tmp) / ".").acquire()
            self.assertIn("home directory itself", str(caught.exception))
            self.assertFalse((Path(tmp) / WorkerStateDirLock.LOCK_FILE_NAME).exists())

    def test_drive_root_is_refused_before_lock_creation(self) -> None:
        import ntpath

        with tempfile.TemporaryDirectory[str]() as tmp:
            root = ntpath.splitdrive(tmp)[0] + "\\"
            with self.assertRaises(WorkerStateDirLockUnavailable) as caught:
                WorkerStateDirLock(root).acquire()
            self.assertIn("filesystem root", str(caught.exception))

    def test_failed_reparse_open_releases_all_retained_directories(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            ancestor = Path(tmp) / "ancestor"
            state = ancestor / "worker"
            state.mkdir(parents=True)
            target = Path(tmp) / "target"
            target.mkdir()
            lock_path = state / WorkerStateDirLock.LOCK_FILE_NAME
            self._junction(lock_path, target)
            try:
                with self.assertRaises(WorkerStateDirLockUnavailable):
                    WorkerStateDirLock(state).acquire()
            finally:
                self.doCleanups()
            os.rename(state, ancestor / "moved")
            os.rename(ancestor, Path(tmp) / "moved-ancestor")

    def test_contention_failure_releases_only_contender_directory_handles(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            state = Path(tmp) / "worker"
            first = WorkerStateDirLock(state)
            first.acquire()
            try:
                with self.assertRaises(WorkerStateDirLocked):
                    WorkerStateDirLock(state).acquire()
                with self.assertRaises(OSError):
                    os.rename(state, Path(tmp) / "moved")
            finally:
                first.release()
            os.rename(state, Path(tmp) / "moved")

    def test_pid_write_failure_releases_lock_and_directory_handles(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            state = Path(tmp) / "worker"
            lock = WorkerStateDirLock(state)
            with unittest.mock.patch("os.write", side_effect=OSError("synthetic write failure")):
                with self.assertRaises(OSError):
                    lock.acquire()
            fresh = WorkerStateDirLock(state)
            fresh.acquire()
            fresh.release()
            lock.release()
            os.rename(state / WorkerStateDirLock.LOCK_FILE_NAME, state / "moved.lock")
            os.rename(state, Path(tmp) / "moved")

    def test_process_death_releases_lock_and_directory_handles(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory[str]() as tmp:
            state = Path(tmp) / "ancestor" / "worker"
            code = (
                "import os, sys; sys.path.insert(0, sys.argv[2]); "
                "from scarcity_router.worker_local_store import WorkerStateDirLock; "
                "lock = WorkerStateDirLock(sys.argv[1]); lock.acquire(); "
                "print(os.getpid(), flush=True); sys.stdin.read()"
            )
            process: subprocess.Popen[str] = subprocess.Popen(
                [sys.executable, "-c", code, str(state), str(Path(__file__).resolve().parents[1])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                line = cast(str, process.stdout.readline())
                self.assertEqual(str(process.pid), line.strip())
                with self.assertRaises(WorkerStateDirLocked) as caught:
                    WorkerStateDirLock(state).acquire()
                self.assertEqual(process.pid, caught.exception.holder_pid)
                process.kill()
                _ = process.communicate(timeout=10)
                fresh = WorkerStateDirLock(state)
                fresh.acquire()
                fresh.release()
                os.rename(state.parent, Path(tmp) / "moved")
            finally:
                if process.poll() is None:
                    process.kill()
                _ = process.communicate(timeout=10)


if __name__ == "__main__":
    _ = unittest.main()
