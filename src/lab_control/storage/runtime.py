"""Resolve the one physical runtime root used by real desktop sessions."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


def _packaged_redirect(logical: Path, physical: Path) -> bool:
    """Recognize MSIX's private LocalAppData copy, not ordinary junctions."""
    source = PureWindowsPath(str(logical))
    target = PureWindowsPath(str(physical))
    try:
        relative = target.relative_to(source.parent)
    except ValueError:
        return False
    parts = tuple(part.casefold() for part in relative.parts)
    return (
        len(parts) == 5
        and parts[0] == "packages"
        and parts[2:4] == ("localcache", "local")
        and parts[4] == source.name.casefold()
    )


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
        final = buffer.value
        if final.startswith("\\\\?\\UNC\\"):
            final = "\\\\" + final[len("\\\\?\\UNC\\") :]
        else:
            final = final.removeprefix("\\\\?\\")
        return Path(final)
    finally:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)


def runtime_home() -> Path:
    """Return the canonical physical root for the current user runtime.

    Windows packaged/desktop views can expose different databases through the
    same textual path. Refuse implicit selection of a private packaged copy;
    maintenance must explicitly select the verified desktop runtime first.
    """
    logical = Path(
        os.environ.get("LAB_CONTROL_HOME")
        or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl"
    )
    marker = logical / "inventory.sqlite3"
    if marker.exists():
        try:
            resolved = physical_path(marker)
        except OSError:
            return logical
        if resolved.name == marker.name and resolved.parent.name == logical.name:
            if _packaged_redirect(logical, resolved.parent):
                from lab_control.domain.instruments import DeviceError

                raise DeviceError(
                    "Windows 将运行目录重定向到了应用私有副本，可能与 run.bat 启动的程序使用不同配置。"
                    "请从 run.bat 打开正式程序；维护脚本须核对正式库存和设备锁后显式设置 "
                    f"LAB_CONTROL_HOME。逻辑目录：{logical}；实际目录：{resolved.parent}",
                    "path_redirected",
                )
            return resolved.parent
    return logical


def hardware_lock_home() -> Path:
    """Per-user coordination root, independent of the configurable data root.

    Maintenance PowerShell uses this same LocalAppData path. Resolve the
    directory itself when present; inventory redirection must not select a
    different lock every time the user changes LAB_CONTROL_HOME.
    """
    logical = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl"
    if logical.is_dir():
        return physical_path(logical)
    return logical.absolute()
