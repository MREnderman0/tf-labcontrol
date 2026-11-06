"""One reservation boundary for history operations and offline metric jobs."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterable
from pathlib import Path

from lab_control.application.leases import FileLease
from lab_control.domain.instruments import DeviceError


class RecordAccess:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.held: dict[Path, FileLease] = {}

    def acquire(self, folder: Path) -> Path:
        return self.acquire_many((folder,))[0]

    def acquire_many(self, folders: Iterable[Path]) -> tuple[Path, ...]:
        paths = tuple(dict.fromkeys(folder.resolve(strict=True) for folder in folders))
        if not paths:
            return ()
        selected = set(paths)
        with self.lock:
            held_ancestors = {parent for path in self.held for parent in path.parents}
            if any(
                path in self.held
                or path in held_ancestors
                or any(parent in self.held or parent in selected for parent in path.parents)
                for path in paths
            ):
                raise DeviceError("此采集记录已有处理或文件操作，请等待完成", "recording_busy")
            acquired = []
            try:
                for path in paths:
                    digest = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
                    self.held[path] = FileLease(self.root / "processing-locks" / f"{digest}.lock")
                    acquired.append(path)
            except Exception:
                for path in reversed(acquired):
                    self.release(path)
                raise
        return paths

    def release(self, path: Path) -> None:
        with self.lock:
            self.held[path].close()
            del self.held[path]
