from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lab_control.application.config import canonical_resource
from lab_control.domain.instruments import DeviceError


class ResourceLeases:
    """Atomic ownership shared by connecting sessions and temporary probes."""

    def __init__(self, root: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._owners: dict[str, str] = {}
        self._shared_unknown_owners: set[str] = set()
        self.root = root
        self._files: dict[str, dict[str, FileLease]] = {}

    def acquire(self, owner: str, resources: tuple[str, ...], *, identified: bool = False) -> None:
        with self._lock:
            keys = {canonical_resource(item) for item in resources}
            unknown_alias = canonical_resource("discovery:unknown_alias")
            # All LAN protocols on the same endpoint address one physical instrument.
            for key in tuple(keys):
                host = (
                    re.match(r"TCPIP\d*::([^:]+)::", key)
                    or re.match(r"(?:TCP|VICP)://([^/:]+)", key)
                    or re.match(r"SOCKET:([^:]+):", key)
                )
                if host:
                    keys.add("HOST:" + host[1].rstrip("."))
            if unknown_alias in keys:
                if identified and unknown_alias in self._owners and self._owners[unknown_alias] != owner:
                    raise DeviceError("Resource is owned by another session or probe", "resource_busy")
                if not identified and (
                    unknown_alias in self._owners or self._shared_unknown_owners
                ):
                    raise DeviceError("Resource is owned by another session or probe", "resource_busy")
            if any(
                key != unknown_alias
                and key in self._owners
                and self._owners[key] != owner
                for key in keys
            ):
                raise DeviceError("Resource is owned by another session or probe", "resource_busy")
            pending: dict[str, FileLease] = {}
            if self.root is not None:
                held = self._files.get(owner, {})
                try:
                    if not held:
                        # Unknown aliases/probes exclude every active session. Known assets
                        # share this gate, but have exclusive endpoint and serial claims.
                        pending["gate"] = FileLease(self.root / "devices" / "aliases.lock", shared=identified)
                    exclusive_keys = keys - held.keys()
                    if identified:
                        # The shared aliases gate above represents this
                        # sentinel; an additional exclusive hash lock would
                        # accidentally serialize all safe probes again.
                        exclusive_keys.discard(unknown_alias)
                    for key in sorted(exclusive_keys):
                        name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
                        pending[key] = FileLease(self.root / "devices" / name)
                except Exception:
                    for lease in reversed(tuple(pending.values())):
                        lease.close()
                    raise
                self._files.setdefault(owner, {}).update(pending)
            # Confirmed/safe probes share the unknown-alias gate. Its shared
            # OS lease still blocks an unconfirmed probe, while endpoint and
            # explicit aliases remain exclusive in this owner map.
            owned_keys = keys - ({unknown_alias} if identified else set())
            self._owners.update(dict.fromkeys(owned_keys, owner))
            if identified and unknown_alias in keys:
                self._shared_unknown_owners.add(owner)

    def owner(self, resource: str) -> str | None:
        with self._lock:
            return self._owners.get(canonical_resource(resource))

    def release(self, owner: str) -> None:
        with self._lock:
            self._owners = {key: value for key, value in self._owners.items() if value != owner}
            self._shared_unknown_owners.discard(owner)
            for lease in reversed(tuple(self._files.pop(owner, {}).values())):
                lease.close()


class FileLease:
    """Nonblocking OS lease, automatically released on process exit."""

    def __init__(self, path: Path, *, shared: bool = False, code: str = "resource_busy") -> None:
        self.path = path
        self._owner_metadata = path.with_suffix(path.suffix + ".owner.json") if code == "process_busy" else None
        self._owner_token = uuid.uuid4().hex if self._owner_metadata and not shared else ""
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
            detail = self._read_owner_detail() if code == "process_busy" else ""
            message = (
                "Legacy hardware process or exclusive maintenance is running"
                if code == "process_busy"
                else "Device or unconfirmed identity probe is owned by another hardware process"
            )
            if detail:
                message += f" ({detail})"
            raise DeviceError(message, code) from exc
        if self._owner_metadata and self._owner_token:
            self._write_owner_metadata()

    def close(self) -> None:
        # Closing the handle releases the OS lock, including Windows byte-range locks.
        self.stream.close()
        if self._owner_metadata and self._owner_token:
            self._clear_owner_metadata()

    def _read_owner_detail(self) -> str:
        """Read advisory holder information; the OS lock remains authoritative."""
        if self._owner_metadata is None or not self._owner_metadata.exists():
            return ""
        try:
            data = json.loads(self._owner_metadata.read_text(encoding="utf-8"))
            pid = data.get("pid")
            started = data.get("started_at")
            parts = [f"PID {pid}" if pid else ""]
            if started:
                parts.append(f"started {started}")
            return ", ".join(part for part in parts if part)
        except (OSError, ValueError, TypeError):
            return ""

    def _write_owner_metadata(self) -> None:
        assert self._owner_metadata is not None
        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "token": self._owner_token,
        }
        temporary = self._owner_metadata.with_name(
            f"{self._owner_metadata.name}.{os.getpid()}.{self._owner_token}.tmp"
        )
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self._owner_metadata)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _clear_owner_metadata(self) -> None:
        assert self._owner_metadata is not None
        try:
            data = json.loads(self._owner_metadata.read_text(encoding="utf-8"))
            if data.get("token") != self._owner_token:
                return
        except (OSError, ValueError, TypeError):
            return
        try:
            self._owner_metadata.unlink(missing_ok=True)
        except OSError:
            pass


class ProcessLease(FileLease):
    """Shared compatibility gate; exclusive by default for legacy/maintenance callers."""

    def __init__(self, root: Path, *, shared: bool = False) -> None:
        super().__init__(root / "hardware.lock", shared=shared, code="process_busy")
