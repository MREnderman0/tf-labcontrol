"""Transactional PNG sets built from the physical values in an offline cache."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from pathlib import Path
from tempfile import mkdtemp
from typing import Callable
from uuid import uuid4

import numpy as np

from lab_control.storage.layout import atomic_json
from lab_control.storage.paths import filename_part
from lab_control.storage.processing import check_cancel
from lab_control.storage.processing_paths import filesystem_path
from lab_control.storage.processing_results import ResultReader
from lab_control.storage.recording_catalog import linked, read_json

FIGURE_VERSION = 4
LABELS = {
    "power": "Power",
    "frequency": "Frequency",
    "temperature": "Temperature",
    "waveform": "Voltage",
    "spectrum": "Power",
    "voltage": "Voltage",
    "resistance": "Resistance",
}
NAMES = {
    "power": "功率",
    "frequency": "频率",
    "temperature": "温度",
    "waveform": "波形",
    "spectrum": "光谱",
    "voltage": "电压",
    "resistance": "电阻",
}


def _png_path(directory: Path, name: str) -> Path:
    relative = Path(name)
    if relative.drive or relative.is_absolute() or ".." in relative.parts or relative.suffix != ".png":
        raise ValueError("图片清单路径无效")
    path = directory / relative
    if any(linked(parent) for parent in (directory, path, *path.parents) if parent.is_relative_to(directory)):
        raise ValueError("图片路径不能包含目录链接")
    return path


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _matches(directory: Path, item: dict) -> bool:
    path = _png_path(directory, item["path"])
    return path.is_file() and path.stat().st_size == item["bytes"] and _digest(path) == item["sha256"]


def _complete(folder: Path, info: dict, cancel: threading.Event) -> bool:
    try:
        saved = read_json(folder / "png.json", {})
        if not isinstance(saved, dict):
            return False
        if (
            saved.get("version") != FIGURE_VERSION
            or saved.get("frames") != info["frames"]
            or saved.get("kind") != info["kind"]
            or "files" not in saved
            or (info["frames"] and not saved["files"])
        ):
            return False
        directory = folder / saved["directory"]
        if directory.parent != folder / "图片" or linked(directory) or linked(directory.parent):
            return False
        for item in saved["files"]:
            check_cancel(cancel)
            path = _png_path(directory, item["path"])
            if not _matches(directory, item):
                return False
            with path.open("rb") as stream:
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def generate_pngs(
    folder: Path,
    cancel: threading.Event,
    progress: Callable[[int, int], None],
) -> None:
    """Combine channels unless the model explicitly requests separate PNGs.

    Only one frame and one raster are held at a time. An existing complete set
    stays available until the replacement set and its manifest are published.
    """
    from lab_control.instruments.registry import DEVICE_DEFINITIONS
    from lab_control.ui.widgets.publication_figure import export_figure

    folder = filesystem_path(folder)
    reader = ResultReader(folder)
    info = reader.info()
    definition = DEVICE_DEFINITIONS.get(info.get("recording", {}).get("adapter_id", ""))
    profile = definition.offline_processing if definition else None
    separate = bool(profile and profile.separate_channel_pngs)
    check_cancel(cancel)
    if _complete(folder, info, cancel):
        progress(info["frames"], info["frames"])
        return
    images = folder / "图片"
    if linked(images):
        raise ValueError("图片目录不能是目录链接")
    images.mkdir(exist_ok=True)
    staging = Path(mkdtemp(prefix=".processing-png-", dir=images))
    files = []
    channels = {s.get("channel", 1) for s in info["series"]}
    is_sequence = info["kind"] == "frames" and info["frames"] > 1

    def group_key(sample):
        return (
            sample["quantity"],
            sample.get("unit", ""),
            sample.get("axis_unit", ""),
            sample.get("channel", 1) if separate else None,
        )

    groups: dict[tuple, list[dict]] = {}
    for series in info["series"]:
        groups.setdefault(group_key(series), []).append(series)
    names = {}
    used = set()
    for key in groups:
        quantity, unit, axis, channel = key
        stem = filename_part(f"{NAMES.get(quantity, quantity)}_{unit}".rstrip("_"))
        name = stem
        index = 1
        while (channel, name) in used:
            index += 1
            name = f"{stem}-{index}"
        used.add((channel, name))
        names[key] = name

    def draw(group, curves, *, frame=None, timestamp=None, series=None):
        check_cancel(cancel)
        quantity, unit, axis = group[:3]
        x_label = (
            "Elapsed time (s)"
            if info["kind"] == "scalar"
            else {"nm": "Wavelength (nm)", "s": "Time (s)"}.get(axis, f"X ({axis})" if axis else "Point")
        )
        relative = Path(f"CH{filename_part(str(group[3]))}") if separate and len(channels) > 1 else Path()
        if is_sequence:
            relative /= "帧序列"
        name = names[group] + (f"_{frame:06d}" if is_sequence else "")
        relative /= f"{name}.png"
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        export_figure(
            path,
            curves,
            x_label,
            f"{LABELS.get(quantity, quantity)} ({unit})",
            width_mm=183,
            check_cancel=lambda: check_cancel(cancel),
        )
        files.append(
            {
                "path": relative.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _digest(path),
                "frame": frame,
                "timestamp": timestamp,
                "series": series,
                "quantity": quantity,
                "unit": unit,
                "axis_unit": axis,
                "channels": [name for name, _x, _y in curves],
            }
        )

    try:
        if info["kind"] == "scalar":
            progress(0, len(groups))
            for index, (key, selected) in enumerate(groups.items(), 1):
                check_cancel(cancel)
                ids = tuple(s["id"] for s in selected)
                data = reader.scalar(ids, points=12000)
                curves = [(f"CH {s.get('channel', 1)}", *data[s["id"]]) for s in selected]
                draw(key, curves, series=ids)
                progress(index, len(groups))
            stats = info.get("temperature_statistics", {})
            if stats.get("channels"):
                from lab_control.ui.widgets.publication_table import export_table

                name = "温度峰峰值.png"
                path = staging / name

                def number(value):
                    return "--" if value is None else f"{value:.9g}"

                rows = [
                    (
                        f"CH {row['channel']}",
                        row["unit"],
                        number(row["minimum"]),
                        number(row["maximum"]),
                        number(row["peak_to_peak"]),
                        str(row["count"]),
                    )
                    for row in stats["channels"]
                ]
                export_table(
                    path,
                    f"Temperature peak-to-peak | final 30 min\nElapsed window: {stats['start_elapsed']:g} - {stats['end_elapsed']:g} s",
                    ("Channel", "Unit", "Minimum", "Maximum", "Peak-to-peak", "Samples"),
                    rows,
                    check_cancel=lambda: check_cancel(cancel),
                )
                files.append(
                    {
                        "path": name,
                        "bytes": path.stat().st_size,
                        "sha256": _digest(path),
                        "quantity": "temperature",
                        "statistics": stats,
                    }
                )
        else:
            progress(0, info["frames"])
            for sequence in range(1, info["frames"] + 1):
                check_cancel(cancel)
                frame = reader.frame(sequence)
                frames: dict[tuple, list[tuple[str, np.ndarray, np.ndarray]]] = {}
                for sample in frame["measurements"]:
                    key = group_key(sample)
                    y = sample["values"].reshape(-1)
                    x = np.arange(y.size) if sample["axis"] is None else sample["axis"].reshape(-1)
                    frames.setdefault(key, []).append((f"CH {sample.get('channel', 1)}", x, y))
                for key, curves in frames.items():
                    draw(
                        key,
                        curves,
                        frame=sequence,
                        timestamp=frame["timestamp"],
                    )
                progress(sequence, info["frames"])
        check_cancel(cancel)
        generation = images / f"自动生成-{uuid4().hex[:12]}"
        staging.rename(generation)
        atomic_json(
            folder / "png.json",
            {
                "version": FIGURE_VERSION,
                "style": "nature-inspired-v1",
                "source": "data.sqlite3",
                "directory": generation.relative_to(folder).as_posix(),
                "frames": info["frames"],
                "kind": info["kind"],
                "width_mm": 183,
                "dpi": 600,
                "files": files,
                "note": "物理单位；仅显示使用极值包络，完整精度数据保留在 CSV/SQLite 中",
            },
        )
    finally:
        if staging.exists() and staging.resolve().parent == images.resolve() and not linked(staging):
            shutil.rmtree(staging)


def publish_pngs(folder: Path, destination: Path, cancel: threading.Event) -> None:
    """Expose only images; keep old layout/user files in the internal cache.

    Stage the entire tree before renaming. Failure or cancellation during copy
    leaves the previous visible tree intact; a failed rename rolls it back.
    """
    folder, destination = filesystem_path(folder), filesystem_path(destination)
    check_cancel(cancel)
    manifest = read_json(folder / "png.json")
    if not _complete(folder, ResultReader(folder).info(), cancel):
        raise ValueError("PNG 图片集不完整，不能发布")
    if linked(destination.parent) or linked(destination):
        raise ValueError("处理图片目录不能是目录链接")
    existing: set[str] = set()
    for parent, directories, files in os.walk(destination, followlinks=False):
        check_cancel(cancel)
        for name in [*directories, *files]:
            if linked(Path(parent) / name):
                raise ValueError("处理图片目录不能包含链接")
        existing.update((Path(parent) / name).relative_to(destination).as_posix() for name in files)
    if destination.is_dir() and existing == {item["path"] for item in manifest["files"]}:
        for item in manifest["files"]:
            check_cancel(cancel)
            if not _matches(destination, item):
                break
        else:
            return
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=".processing-publish-", dir=folder.parent))
    backup = folder.parent / f"previous-images-{uuid4().hex}"
    try:
        for item in manifest["files"]:
            check_cancel(cancel)
            source = _png_path(folder / manifest["directory"], item["path"])
            target = _png_path(staging, item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if not _matches(staging, item):
                raise OSError("PNG 图片复制校验失败，保留已有图片")
        check_cancel(cancel)
        if destination.exists():
            destination.rename(backup)
        try:
            staging.rename(destination)
        except OSError as exc:
            if backup.exists() and not destination.exists():
                try:
                    backup.rename(destination)
                except OSError as restore_error:
                    raise OSError(f"图片目录恢复失败，旧文件保留在 {backup}：{restore_error}") from exc
            raise
    finally:
        if staging.exists() and staging.resolve().parent == folder.parent.resolve() and not linked(staging):
            shutil.rmtree(staging)
