"""Offline history jobs and whole-capture file operations. Never opens hardware."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from lab_control.application.leases import FileLease
from lab_control.domain.processing import RecordingEntry
from lab_control.storage.layout import atomic_json, raw_folder
from lab_control.storage.processing import ProcessingCancelled, process_device
from lab_control.storage.recording_catalog import RecordingCatalog, linked, read_device, read_json
from lab_control.storage.repository import Repository


class ProcessingService:
    def __init__(self, repository: Repository, active: Callable[[Path], bool] = lambda _path: False):
        self.repository = repository
        self.catalog = RecordingCatalog(repository)
        self.active = active
        self.lock = threading.RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="offline-processing")
        self.read_worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="history-reader")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.closed = False

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return deepcopy(self.jobs)

    def _update(self, key: str, **values) -> None:
        with self.lock:
            self.jobs[key].update(values)

    def check_idle(self, entry: RecordingEntry, *, allow_corrupt=False) -> None:
        if not entry.folder.is_dir() or linked(entry.folder):
            raise ValueError("采集目录已不存在或是目录链接，请刷新列表")
        if self.active(entry.folder):
            raise ValueError("此记录正在采集、收尾或转换，请等待完成")
        for device in entry.devices:
            raw = raw_folder(device.folder)
            try:
                state = read_json(raw / "summary.json", {})
                if not isinstance(state, dict):
                    raise ValueError("采集摘要不是有效对象")
            except (OSError, ValueError):
                if allow_corrupt:
                    continue  # This device reports failure; the other devices can still be processed.
                raise
            if state and (state.get("binary_done") is False or state.get("analysis_state") == "generating"):
                raise ValueError("记录尚未完成收尾或 CSV 转换，请检查采集状态")
            if any(raw.glob("segment-*.sqlite3-wal")) and not state.get("binary_done"):
                raise ValueError("原始数据库仍有活动写入，请待收尾后重试")

    def _submit(self, entry: RecordingEntry, action: str, operation) -> Future:
        with self.lock:
            if self.closed:
                raise RuntimeError("数据处理服务已关闭")
            if any(
                job["state"] in {"queued", "running"}
                and (
                    Path(key) == entry.folder
                    or Path(key) in entry.folder.parents
                    or entry.folder in Path(key).parents
                )
                for key, job in self.jobs.items()
            ):
                raise ValueError("此采集记录已有任务，请等待完成或取消处理")
            self.check_idle(entry, allow_corrupt=action == "process")
            cancel = threading.Event()
            self.cancel_events[entry.key] = cancel
            self.jobs[entry.key] = {
                "state": "queued",
                "action": action,
                "done": 0,
                "total": 0,
                "device": "",
                "results": [],
                "error": "",
            }

        def run():
            lease = None
            try:
                digest = hashlib.sha256(os.path.normcase(entry.key).encode("utf-8")).hexdigest()
                lease = FileLease(self.repository.root / "processing-locks" / f"{digest}.lock")
                self.check_idle(entry, allow_corrupt=action == "process")
                self._update(entry.key, state="running")
                value = operation(cancel)
                failed = isinstance(value, list) and value and all(item.get("error") for item in value)
                self._update(
                    entry.key,
                    state="failed" if failed else "complete",
                    error="全部设备处理失败，查看设备详情" if failed else "",
                )
                self.repository.audit("history_" + action, source=entry.key, result=value)
                return value
            except ProcessingCancelled as exc:
                self._update(entry.key, state="cancelled", error=str(exc))
                raise
            except Exception as exc:
                self._update(entry.key, state="failed", error=str(exc))
                self.repository.audit("history_" + action + "_failed", source=entry.key, error=str(exc))
                raise
            finally:
                if lease:
                    lease.close()

        return self.worker.submit(run)

    def process(self, entry: RecordingEntry) -> Future:
        from lab_control.application.processing_figures import generate_pngs
        from lab_control.ui.widgets.publication_figure import prepare_publication_font

        # Resolve the installed font on the calling GUI thread before workers
        # paint offscreen QImages. The storage-only API remains Qt-independent.
        prepare_publication_font()

        def operation(cancel):
            results = []
            for device in entry.devices:
                device = read_device(device.folder)
                if cancel.is_set():
                    raise ProcessingCancelled("处理已取消，已完成设备的结果保留")
                self._update(
                    entry.key,
                    device=device.label,
                    stage="整理数据",
                    unit="帧",
                    done=0,
                    total=device.summary.get("committed", 0),
                )

                def figures(folder):
                    self._update(entry.key, stage="生成 PNG", done=0, total=0, unit="帧/组")
                    generate_pngs(
                        folder, cancel, lambda done, total: self._update(entry.key, done=done, total=total)
                    )

                try:
                    result = process_device(
                        device,
                        entry.folder,
                        cancel,
                        lambda done, total: self._update(entry.key, done=done, total=total),
                        finalize=figures,
                    )
                    results.append(
                        {
                            "device": device.label,
                            "source": str(device.folder),
                            "result": str(result),
                            "error": "",
                        }
                    )
                except ProcessingCancelled:
                    raise
                except Exception as exc:
                    results.append(
                        {
                            "device": device.label,
                            "source": str(device.folder),
                            "result": "",
                            "error": str(exc),
                        }
                    )
                self._update(entry.key, results=list(results))
            return results

        return self._submit(entry, "process", operation)

    def cancel(self, entry: RecordingEntry) -> None:
        with self.lock:
            if event := self.cancel_events.get(entry.key):
                event.set()

    def export_figure(self, entry: RecordingEntry, payload: dict) -> Future:
        if payload.get("format") not in {"png", "svg", "pdf"}:
            raise ValueError("不支持的图片格式")

        def operation(_cancel):
            from lab_control.ui.widgets.publication_figure import export_figure

            folder = Path(payload["folder"]).resolve(strict=True)
            if not folder.is_relative_to(entry.folder / "数据处理"):
                raise ValueError("图片必须保存在本次采集的处理结果中")
            self._files(folder)
            images = folder / "图片"
            images.mkdir(exist_ok=True)
            name = f"frame-{payload['frame']}" if payload.get("frame") else "time-series"
            identity = uuid4().hex[:10]
            staging = images / f".processing-figure-{identity}"
            staging.mkdir()
            destination = images / f"export-{identity}"
            separate = payload.get("quantity") == "waveform" and payload["format"] == "png"
            groups = [[curve] for curve in payload["curves"]] if separate else [payload["curves"]]
            paths = []
            try:
                for index, curves in enumerate(groups, 1):
                    suffix = f"-{curves[0][0].replace(' ', '')}" if separate else ""
                    path = staging / f"{name}{suffix}-{index}.{payload['format']}"
                    export_figure(
                        path, curves, payload["x_label"], payload["y_label"], width_mm=payload["width_mm"]
                    )
                    atomic_json(
                        path.with_suffix(".json"),
                        {key: value for key, value in payload.items() if key not in {"folder", "curves"}}
                        | {
                            "source": "../../data.sqlite3",
                            "channels": [curve[0] for curve in curves],
                            "style": "nature-inspired-v1",
                            "note": "显示用极值包络；原精度完整数据保留在 CSV 与 SQLite 中",
                        },
                    )
                    paths.append(str(destination / path.name))
                staging.rename(destination)
            finally:
                if staging.exists() and staging.resolve().parent == images.resolve() and not linked(staging):
                    shutil.rmtree(staging)
            return {"figure": paths[0], "figures": paths}

        return self._submit(entry, "figure", operation)

    def move(self, entry: RecordingEntry, parent: Path) -> Future:
        return self._submit(entry, "move", lambda _cancel: self._move(entry, parent / entry.name))

    def rename(self, entry: RecordingEntry, name: str) -> Future:
        if (
            not name
            or name in {".", ".."}
            or name != name.strip(" .")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name, re.I)
        ):
            raise ValueError("文件夹名称为空、含非法字符或为 Windows 保留名称")
        return self._submit(entry, "rename", lambda _cancel: self._move(entry, entry.folder.parent / name))

    def trash(self, entry: RecordingEntry) -> Future:
        def operation(_cancel):
            self.check_idle(entry)
            self._files(entry.folder)
            from PySide6.QtCore import QFile

            result = QFile.moveToTrash(str(entry.folder))
            # PySide bindings expose either bool or (bool, pathInTrash).
            success, location = result if isinstance(result, tuple) else (result, "")
            if not success:
                raise OSError("无法移入系统回收站，原目录保留；未执行永久删除")
            return {"trash": location}

        return self._submit(entry, "trash", operation)

    @staticmethod
    def _files(root: Path) -> tuple[Path, ...]:
        if linked(root):
            raise ValueError("不能操作符号链接或目录联接")
        result: list[Path] = []
        for directory, names, files in os.walk(root, followlinks=False):
            for name in [*names, *files]:
                path = Path(directory) / name
                if linked(path):
                    raise ValueError("采集目录包含链接，已停止文件操作")
            result.extend(Path(directory) / name for name in files)
        return tuple(result)

    @classmethod
    def _signature(cls, root: Path) -> dict[str, str]:
        result = {}
        for path in cls._files(root):
            with path.open("rb") as stream:
                result[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
        return result

    @staticmethod
    def _same_volume(source: Path, parent: Path) -> bool:
        return source.stat().st_dev == parent.stat().st_dev

    def _move(self, entry: RecordingEntry, destination: Path) -> dict:
        self.check_idle(entry)
        source = entry.folder.resolve(strict=True)
        if not entry.devices or any(not d.folder.is_relative_to(source) for d in entry.devices):
            raise ValueError("采集记录范围不一致，请刷新后重试")
        parent = destination.parent.resolve(strict=True)
        destination = parent / destination.name
        if destination.exists() or destination == source or source in destination.parents:
            raise ValueError("目标已存在或位于源目录内，未覆盖任何文件")
        self._files(source)
        operation = uuid4().hex
        journal = self.repository.root / "processing-operations" / f"{operation}.json"
        journal.parent.mkdir(parents=True, exist_ok=True)
        state = {"version": 1, "source": str(source), "destination": str(destination), "state": "starting"}
        atomic_json(journal, state)
        staging = parent / f".processing-move-{operation}"
        try:
            if self._same_volume(source, parent):
                source.rename(destination)
            else:
                original = self._signature(source)
                shutil.copytree(source, staging)
                if self._signature(staging) != original or self._signature(source) != original:
                    raise OSError("复制校验失败或源文件发生变化，源目录保留")
                self.check_idle(entry)
                staging.rename(destination)
                state.update(state="copied_verified")
                atomic_json(journal, state)
                self.catalog.add_root(destination)
                # Remove only the verified original after publishing a complete destination.
                if source != entry.folder.resolve(strict=True) or source == source.parent:
                    raise ValueError("源目录边界发生变化")
                self._files(source)
                self.check_idle(entry)
                shutil.rmtree(source)
            self.catalog.add_root(destination)
            state.update(state="complete")
            atomic_json(journal, state)
            return {"destination": str(destination)}
        except Exception as exc:
            state.update(state="failed", error=str(exc), destination_exists=destination.exists())
            atomic_json(journal, state)
            raise OSError(f"文件操作未完整完成：{exc}；详情：{journal}") from exc
        finally:
            if staging.exists() and staging.resolve().parent == parent and not linked(staging):
                shutil.rmtree(staging)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for event in self.cancel_events.values():
                event.set()
        self.worker.shutdown(wait=True, cancel_futures=False)
        self.read_worker.shutdown(wait=True, cancel_futures=True)
