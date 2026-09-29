"""Verify OS-resolved runtime paths before real hardware I/O."""

from __future__ import annotations

import os
from pathlib import Path

from lab_control.domain.instruments import DeviceError


def physical_path(path: Path) -> Path:
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


def verify_runtime_paths(home: Path, lock_root: Path) -> dict[str, str]:
    paths = {
        "runtime": home,
        "inventory": home / "inventory.sqlite3",
        "global_lock": lock_root / "hardware.lock",
        "device_locks": lock_root / "devices",
    }
    if (home / "config").exists():
        paths["config"] = home / "config"
    result = {name: str(physical_path(path)) for name, path in paths.items()}
    if any(Path(result[name]) != path.absolute() for name, path in paths.items()):
        raise DeviceError(f"Formal runtime paths are redirected: {result}", "path_redirected")
    return result
