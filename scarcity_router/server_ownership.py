"""Process-lifetime ownership of a composed server, separate from SQLite locks."""

from __future__ import annotations

import errno
import os
import stat
import sys
import weakref
from pathlib import Path

from .server_store import ServerStoreError
from .worker_identity_store import open_private_state_dir
# Reuse the established no-follow, retained Windows handle-chain implementation.
from .worker_local_store import (
    _close_windows_directory_handles,  # pyright: ignore[reportPrivateUsage]
    _lock_fd_exclusive,  # pyright: ignore[reportPrivateUsage]
    _open_lock_file_windows,  # pyright: ignore[reportPrivateUsage]
)


def _release(fd: int, handles: list[int]) -> None:
    try:
        # Closing the owned descriptor releases flock/msvcrt locks. Never unlink
        # a lock entry, and never open or close a SQLite-inode descriptor here.
        os.close(fd)
    finally:
        if handles:
            _close_windows_directory_handles(handles)


class ServerOwnership:
    """Nonblocking server-only lease; administrative store opens do not take it.

    POSIX locks the directory inode itself: aliases contend on the same object,
    and there is no lock-file entry to unlink/recreate into split ownership.
    Windows retains the existing no-delete directory/lock handle chain and locks
    a byte of a separate non-secret file. Both locks die with the process.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path: Path = data_dir.resolve()
        fd: int | None = None
        handles: list[int] = []
        try:
            if sys.platform == "win32":
                fd, handles = _open_lock_file_windows(str(self.path), ".server-instance.lock")
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ServerStoreError("unsafe server ownership lock; administrator inspection required")
                directory = self.path.stat()
            else:
                fd = open_private_state_dir(self.path, create=True)
                directory = os.fstat(fd)
            try:
                _lock_fd_exclusive(fd)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ServerStoreError(
                        "another server is already running for this data directory; "
                        + "stop it before restarting, or use a separate --data-dir"
                    ) from None
                raise
            self._directory: os.stat_result = directory
            self._finalizer: weakref.finalize[..., ServerOwnership] = weakref.finalize(self, _release, fd, handles)
            fd = None
            try:
                self.check_directory()
            except BaseException:
                self.close()
                raise
        except ServerStoreError:
            raise
        except (OSError, ValueError):
            raise ServerStoreError(
                "server ownership unavailable; use a private owned data directory "
                + "with working OS locks and no unsafe links or permissions"
            ) from None
        finally:
            if fd is not None:
                _release(fd, handles)

    def check_directory(self) -> None:
        """Do not construct a producer through a replaced directory pathname."""
        current = self.path.lstat()
        if not stat.S_ISDIR(current.st_mode) or not os.path.samestat(current, self._directory):
            raise ServerStoreError("server data directory identity changed; refusing startup")

    def close(self) -> None:
        """Release once, also safe if startup/shutdown cleanup is repeated."""
        _ = self._finalizer()
