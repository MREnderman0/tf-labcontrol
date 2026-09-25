from __future__ import annotations

import hashlib
import importlib
import os
import re
import threading
from pathlib import Path

from lab_control.application.config import canonical_resource
from lab_control.domain.instruments import DeviceError


class ResourceLeases:
    """Atomic ownership shared by connecting sessions and temporary probes."""

    def __init__(self, root: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._owners: dict[str, str] = {}
        self.root = root
        self._files: dict[str, dict[str, FileLease]] = {}

    def acquire(self, owner: str, resources: tuple[str, ...], *, identified: bool = False) -> None:
        with self._lock:
            keys = {canonical_resource(item) for item in resources}
            # All LAN protocols on the same endpoint address one physical instrument.
            for key in tuple(keys):
                host = (
                    re.match(r"TCPIP\d*::([^:]+)::", key)
                    or re.match(r"(?:TCP|VICP)://([^/:]+)", key)
                    or re.match(r"SOCKET:([^:]+):", key)
                )
                if host:
                    keys.add("HOST:" + host[1].rstrip("."))
            if any(key in self._owners and self._owners[key] != owner for key in keys):
                raise DeviceError("Resource is owned by another session or probe", "resource_busy")
            pending: dict[str, FileLease] = {}
            if self.root is not None:
                held = self._files.get(owner, {})
                try:
                    if not held:
                        # Unknown aliases/probes exclude every active session. Known assets
                        # share this gate, but have exclusive endpoint and serial claims.
                        pending["gate"] = FileLease(self.root / "devices" / "aliases.lock", shared=identified)
                    for key in sorted(keys - held.keys()):
                        name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
                        pending[key] = FileLease(self.root / "devices" / name)
                except Exception:
                    for lease in reversed(tuple(pending.values())):
                        lease.close()
                    raise
                self._files.setdefault(owner, {}).update(pending)
            self._owners.update(dict.fromkeys(keys, owner))

    def owner(self, resource: str) -> str | None:
        with self._lock:
            return self._owners.get(canonical_resource(resource))

    def release(self, owner: str) -> None:
        with self._lock:
            self._owners = {key: value for key, value in self._owners.items() if value != owner}
            for lease in reversed(tuple(self._files.pop(owner, {}).values())):
                lease.close()


class FileLease:
    """Nonblocking OS lease, automatically released on process exit."""

    def __init__(self, path: Path, *, shared: bool = False, code: str = "resource_busy") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("a+b")
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                class Overlapped(ctypes.Structure):
                    _fields_ = [
                        ("internal", ctypes.c_size_t),
                        ("internal_high", ctypes.c_size_t),
                        ("offset", wintypes.DWORD),
                        ("offset_high", wintypes.DWORD),
                        ("event", wintypes.HANDLE),
                    ]

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                lock = kernel.LockFileEx
                lock.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(Overlapped),
                ]
                lock.restype = wintypes.BOOL
                # Same byte as the legacy msvcrt exclusive process lock.
                if not lock(
                    msvcrt.get_osfhandle(self.stream.fileno()),
                    1 if shared else 3,
                    0,
                    1,
                    0,
                    ctypes.byref(Overlapped()),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(self.stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.stream.close()
            message = (
                "Legacy hardware process or exclusive maintenance is running"
                if code == "process_busy"
                else "Device or unconfirmed identity probe is owned by another hardware process"
            )
            raise DeviceError(message, code) from exc

    def close(self) -> None:
        # Closing the handle releases the OS lock, including Windows byte-range locks.
        self.stream.close()


class ProcessLease(FileLease):
    """Shared compatibility gate; exclusive by default for legacy/maintenance callers."""

    def __init__(self, root: Path, *, shared: bool = False) -> None:
        super().__init__(root / "hardware.lock", shared=shared, code="process_busy")
