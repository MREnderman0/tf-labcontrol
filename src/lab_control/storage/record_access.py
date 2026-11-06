"""One reservation boundary for history operations and offline metric jobs."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from lab_control.application.leases import FileLease
from lab_control.domain.instruments import DeviceError


class RecordAccess:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.held: dict[Path, FileLease] = {}

    def acquire(self, folder: Path) -> Path:
        path = folder.resolve(strict=True)
        with self.lock:
            if any(path == p or path in p.parents or p in path.parents for p in self.held):
                raise DeviceError("此采集记录已有处理或文件操作，请等待完成", "recording_busy")
            digest = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
            self.held[path] = FileLease(self.root / "processing-locks" / f"{digest}.lock")
        return path

    def release(self, path: Path) -> None:
        with self.lock:
            self.held[path].close()
            del self.held[path]
