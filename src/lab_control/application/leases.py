from __future__ import annotations

import importlib
import os
import threading
from pathlib import Path

from lab_control.application.config import canonical_resource
from lab_control.domain.instruments import DeviceError


class ResourceLeases:
    """Atomic ownership shared by connecting sessions and temporary probes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owners: dict[str, str] = {}

    def acquire(self, owner: str, resources: tuple[str, ...]) -> None:
        with self._lock:
            keys = {canonical_resource(item) for item in resources}
            if any(key in self._owners and self._owners[key] != owner for key in keys):
                raise DeviceError("Resource is owned by another session or probe", "resource_busy")
            self._owners.update(dict.fromkeys(keys, owner))

    def owner(self, resource: str) -> str | None:
        with self._lock:
            return self._owners.get(canonical_resource(resource))

    def release(self, owner: str) -> None:
        with self._lock:
            self._owners = {key: value for key, value in self._owners.items() if value != owner}


class ProcessLease:
    """Advisory process-wide hardware ownership. Never kills other processes."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.stream = (root / "hardware.lock").open("a+b")
        self.stream.seek(0)
        if os.fstat(self.stream.fileno()).st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.stream.close()
            raise DeviceError("Another LabControl hardware process is running", "process_busy") from exc

    def close(self) -> None:
        if self.stream.closed:
            return
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(self.stream, fcntl.LOCK_UN)
        self.stream.close()
