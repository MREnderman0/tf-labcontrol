"""Read-only history discovery. Processing products never become source recordings."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np

from lab_control.domain.processing import RecordedDevice, RecordingEntry
from lab_control.storage.layout import manifest, raw_folder
from lab_control.storage.repository import Repository

PROCESSING_FOLDER = "数据处理"


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else default


def linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def read_device(folder: Path) -> RecordedDevice:
    try:
        metadata = manifest(folder)
        if not metadata:
            if (folder / "_raw").is_dir():
                raise ValueError("原始记录清单 recording.json 缺失")
            # Older standalone snapshots can carry their identity inside the snapshot.
            snapshot = read_json(folder / "snapshot.json", {})
            if not snapshot and (folder / "samples.npz").is_file():
                with np.load(folder / "samples.npz", allow_pickle=False) as arrays:
                    if "metadata_json" in arrays:
                        snapshot = json.loads(str(arrays["metadata_json"].item()))
            metadata = {
                "kind": "single_frame",
                "identity": snapshot.get("identity"),
                "started_local": snapshot.get("timestamp", ""),
            }
        if not metadata.get("adapter_id") and metadata.get("identity"):
            from lab_control.domain.instruments import DeviceIdentity
            from lab_control.instruments.registry import match

            metadata["adapter_id"] = match(DeviceIdentity(**metadata["identity"])) or ""
        summary = read_json(raw_folder(folder) / "summary.json", {})
        if not isinstance(summary, dict):
            raise ValueError("采集摘要不是有效对象")
        count = summary.get("committed", 0)
        if type(count) is not int or count < 0:
            raise ValueError("采集摘要中的帧数无效")
        identity = metadata.get("identity")
        if identity is not None and not isinstance(identity, dict):
            raise ValueError("设备身份元数据无效")
        for key in ("adapter_id", "run_id", "slot_label", "started_local", "started_utc"):
            if metadata.get(key) is not None and not isinstance(metadata[key], str):
                raise ValueError(f"记录字段 {key} 无效")
        if identity and not isinstance(identity.get("model", ""), str):
            raise ValueError("设备型号无效")
        return RecordedDevice(folder, metadata, summary)
    except Exception as exc:
        return RecordedDevice(folder, {}, {}, f"记录无法读取：{exc}")


def scan_recordings(roots: tuple[Path, ...]) -> tuple[RecordingEntry, ...]:
    devices: dict[Path, RecordedDevice] = {}
    for root in roots:
        if not root.is_dir():
            continue
        if linked(root) or root.name == PROCESSING_FOLDER or root.name.startswith(".processing-"):
            continue
        root = root.resolve()
        for parent, children, files in os.walk(root, followlinks=False):
            folder = Path(parent)
            children[:] = [
                name
                for name in children
                if name != PROCESSING_FOLDER
                and not name.startswith(".processing-")
                and not linked(folder / name)
            ]
            if any(name in files for name in ("recording.json", "snapshot.json", "samples.npz")):
                capture = folder.parent if folder.name == "_raw" else folder
                devices.setdefault(capture, read_device(capture))
                children.clear()
    batch_parents = {
        device.folder.parent
        for device in devices.values()
        if device.metadata.get("scope") == "batch" and device.metadata.get("batch_id")
    }
    # Selecting a batch's child directory must still manage the entire batch.
    # A damaged sibling stays visible with an explicit error in that batch.
    for batch_parent in batch_parents:
        for child in batch_parent.iterdir():
            if child.is_dir() and not linked(child) and child.name != PROCESSING_FOLDER:
                if any(
                    (raw_folder(child) / name).is_file()
                    for name in ("recording.json", "snapshot.json", "samples.npz")
                ):
                    devices.setdefault(child, read_device(child))
    grouped: dict[Path, list[RecordedDevice]] = {}
    for device in devices.values():
        batch = device.folder.parent in batch_parents
        folder = device.folder.parent if batch else device.folder
        grouped.setdefault(folder, []).append(device)
    entries = []
    for folder, members in grouped.items():
        scope = "batch" if any(d.folder != folder for d in members) else "single_device"
        starts = [d.metadata.get("started_local") or d.metadata.get("started_utc") or "" for d in members]
        entries.append(
            RecordingEntry(
                folder,
                scope,
                min(filter(None, starts), default="时间未知"),
                tuple(sorted(members, key=lambda d: d.label)),
            )
        )

    def sort_key(entry):
        try:
            return datetime.fromisoformat(entry.started).timestamp()
        except (ValueError, TypeError):
            return 0.0

    return tuple(sorted(entries, key=sort_key, reverse=True))


class RecordingCatalog:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def roots(self) -> tuple[Path, ...]:
        roots = [self.repository.data_root, self.repository.root / "data"]
        roots.extend(Path(value) for value in self.repository.load_workspace("processing/roots", []))
        # A crash after publishing a move but before saving its new root remains
        # discoverable on restart. Never replay a deletion from this journal.
        for journal in (self.repository.root / "processing-operations").glob("*.json"):
            try:
                destination = read_json(journal, {}).get("destination")
                if destination and Path(destination).is_dir():
                    roots.append(Path(destination))
            except (OSError, ValueError, TypeError):
                continue
        return tuple(dict.fromkeys(path.resolve() for path in roots))

    def add_root(self, path: Path) -> None:
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError("请选择采集文件夹或数据根目录")
        with self.repository.lock:
            roots = self.repository.load_workspace("processing/roots", [])
            self.repository.save_workspace("processing/roots", list(dict.fromkeys([*roots, str(path)])))

    def scan(self) -> tuple[RecordingEntry, ...]:
        return scan_recordings(self.roots())
