"""Windows handle-based opening for a worker-local private CA file.

The caller still validates the exact bytes with OpenSSL. This module accepts
only local fixed/RAM disks and rejects reparse points and DOS device names.
Directory handles deny delete sharing while descendants are opened, preventing
the pathname from being redirected between component checks.
"""

from __future__ import annotations

import ctypes
import importlib
import ntpath
import os
import re
from ctypes import wintypes
from typing import Protocol, cast, final


class WindowsCaOpenError(ValueError):
    """A safe, non-secret failure to open a Windows CA file."""


class _Function(Protocol):
    argtypes: object
    restype: object
    def __call__(self, *args: object) -> object: ...


class _Kernel(Protocol):
    CreateFileW: _Function
    CloseHandle: _Function
    GetFileType: _Function
    GetFileInformationByHandleEx: _Function
    GetDriveTypeW: _Function


class _Msvcrt(Protocol):
    def open_osfhandle(self, handle: int, flags: int) -> int: ...


class _Loader(Protocol):
    def __call__(self, name: str, *, use_last_error: bool) -> object: ...


@final
class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


_READ_ATTRIBUTES = 0x0080
_GENERIC_READ = 0x80000000
_SHARE_READ = 0x00000001
_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_OPEN_REPARSE_POINT = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_ATTRIBUTE_DIRECTORY = 0x10
_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_TYPE_DISK = 1
_FILE_ATTRIBUTE_TAG_INFO = 9
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} |
                      {f"COM{number}" for number in range(1, 10)} |
                      {f"LPT{number}" for number in range(1, 10)} |
                      {f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "¹²³"})


def _local_components(path: str) -> tuple[str, tuple[str, ...]]:
    """Reject remote, device, alternate-stream and ambiguous DOS paths."""
    drive, tail = ntpath.splitdrive(path)
    if re.fullmatch(r"[A-Za-z]:", drive) is None or not tail.startswith("\\"):
        raise WindowsCaOpenError("private CA files must be on a local fixed disk")
    parts = tuple(part for part in tail.split("\\") if part)
    if not parts or any(
        part in (".", "..") or ":" in part or part.endswith((" ", "."))
        or any(ord(character) < 32 or character in '<>"|?*' for character in part)
        or part.partition(".")[0].rstrip(" ").upper() in _RESERVED for part in parts
    ):
        raise WindowsCaOpenError("the private CA path is not a safe regular-file path")
    return drive + "\\", parts


def open_windows_regular(path: str) -> int:
    """Open a regular file without following or racing reparse-point parents."""
    root, parts = _local_components(path)
    try:
        loader = cast(_Loader, getattr(ctypes, "WinDLL"))
        kernel = cast(_Kernel, loader("kernel32", use_last_error=True))
        runtime = cast(_Msvcrt, cast(object, importlib.import_module("msvcrt")))
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetFileType.argtypes = [wintypes.HANDLE]
        kernel.GetFileType.restype = wintypes.DWORD
        kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetDriveTypeW.restype = wintypes.UINT
    except (AttributeError, ImportError, OSError, TypeError):
        raise WindowsCaOpenError("safe Windows private-CA handling is unavailable") from None

    opened: list[int] = []
    file_handle: int | None = None
    try:
        if kernel.GetDriveTypeW(root) not in (3, 6):
            raise WindowsCaOpenError("private CA files must be on a local fixed disk")
        current = root
        for index in range(len(parts) + 1):
            directory = index < len(parts)
            if index:
                current = ntpath.join(current, parts[index - 1])
            desired = _READ_ATTRIBUTES if directory else _GENERIC_READ
            sharing = _SHARE_READ | _SHARE_WRITE if directory else _SHARE_READ
            flags = _OPEN_REPARSE_POINT | (_BACKUP_SEMANTICS if directory else 0)
            raw = kernel.CreateFileW(current, desired, sharing, None, _OPEN_EXISTING, flags, None)
            if not isinstance(raw, int) or raw == _INVALID_HANDLE:
                raise WindowsCaOpenError("the private CA file could not be opened safely")
            opened.append(raw)
            info = _AttributeTagInfo()
            if (kernel.GetFileType(raw) != _FILE_TYPE_DISK
                    or not kernel.GetFileInformationByHandleEx(raw, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info))):
                raise WindowsCaOpenError("the private CA path could not be inspected")
            attributes = cast(int, getattr(info, "attributes"))
            if (attributes & _ATTRIBUTE_REPARSE_POINT
                    or bool(attributes & _ATTRIBUTE_DIRECTORY) != directory):
                raise WindowsCaOpenError("the private CA path contains a reparse point or non-regular object")
            if not directory:
                file_handle = raw
        assert file_handle is not None
        fd = runtime.open_osfhandle(file_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        opened.remove(file_handle)  # The C runtime now owns this handle.
        return fd
    except (OSError, ValueError, TypeError, OverflowError):
        raise WindowsCaOpenError("the private CA file could not be opened safely") from None
    finally:
        for handle in reversed(opened):
            try:
                _ = kernel.CloseHandle(handle)
            except (OSError, TypeError):
                pass
