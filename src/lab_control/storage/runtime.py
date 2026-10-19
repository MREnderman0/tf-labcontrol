"""Resolve the one physical runtime root used by real desktop sessions."""

from __future__ import annotations

import os
from pathlib import Path


def physical_path(path: Path) -> Path:
    """Return the OS-resolved path for an existing file or directory."""
    if os.name != "nt":
        return path.resolve(strict=True)
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    open_file.restype = wintypes.HANDLE
    handle = open_file(str(path), 0, 7, None, 3, 0x02000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        get_path = kernel.GetFinalPathNameByHandleW
        get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        size = get_path(handle, buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value.removeprefix("\\\\?\\"))
    finally:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)


def runtime_home() -> Path:
    """Return the canonical physical root for the current user runtime.

    Windows packaged/desktop views can expose the same AppData file through two
    textual paths. Once the inventory exists, use its final physical parent so
    storage, locks and hardware evidence address one root consistently.
    """
    logical = Path(os.environ.get("LAB_CONTROL_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl")
    marker = logical / "inventory.sqlite3"
    if marker.exists():
        try:
            resolved = physical_path(marker)
        except OSError:
            return logical
        if resolved.name == marker.name and resolved.parent.name == logical.name:
            return resolved.parent
    return logical
