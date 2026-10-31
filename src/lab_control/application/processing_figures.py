"""Transactional PNG sets built from the physical values in an offline cache."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable
from uuid import uuid4

import numpy as np

from lab_control.storage.layout import atomic_json
from lab_control.storage.processing import check_cancel
from lab_control.storage.processing_results import ResultReader
from lab_control.storage.recording_catalog import linked, read_json

FIGURE_VERSION = 1
LABELS = {
    "power": "Power",
    "frequency": "Frequency",
    "temperature": "Temperature",
    "waveform": "Voltage",
    "spectrum": "Power",
    "voltage": "Voltage",
    "resistance": "Resistance",
}


def _complete(folder: Path, info: dict) -> bool:
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
            path = directory / item["path"]
            if path.parent != directory or linked(path) or path.stat().st_size != item["bytes"]:
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
    """Full scalar plots, spectra per frame and waveform PNGs per frame/channel.

    Only one frame and one raster are held at a time. An existing complete set
    stays available until the replacement set and its manifest are published.
    """
    from lab_control.ui.widgets.publication_figure import export_figure

    reader = ResultReader(folder)
    info = reader.info()
    check_cancel(cancel)
    if _complete(folder, info):
        progress(info["frames"], info["frames"])
        return
    images = folder / "图片"
    if linked(images):
        raise ValueError("图片目录不能是目录链接")
    images.mkdir(exist_ok=True)
    staging = images / f".processing-png-{uuid4().hex}"
    staging.mkdir()
    files = []

    def draw(group, curves, name, *, frame=None, timestamp=None, series=None):
        check_cancel(cancel)
        quantity, unit, axis = group[:3]
        x_label = (
            "Elapsed time (s)"
            if info["kind"] == "scalar"
            else {"nm": "Wavelength (nm)", "s": "Time (s)"}.get(axis, f"X ({axis})" if axis else "Point")
        )
        path = staging / f"{name}.png"
        export_figure(path, curves, x_label, f"{LABELS.get(quantity, quantity)} ({unit})", width_mm=183)
        files.append(
            {
                "path": path.name,
                "bytes": path.stat().st_size,
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
            groups: dict[tuple, list[dict]] = {}
            for series in info["series"]:
                key: tuple = (series["quantity"], series.get("unit", ""), series.get("axis_unit", ""))
                groups.setdefault(key, []).append(series)
            progress(0, len(groups))
            for index, (key, selected) in enumerate(groups.items(), 1):
                check_cancel(cancel)
                ids = tuple(s["id"] for s in selected)
                data = reader.scalar(ids, points=12000)
                curves = [(f"CH {s.get('channel', 1)}", *data[s["id"]]) for s in selected]
                draw(key, curves, f"time-series-{index:02d}", series=ids)
                progress(index, len(groups))
        else:
            progress(0, info["frames"])
            for sequence in range(1, info["frames"] + 1):
                check_cancel(cancel)
                frame = reader.frame(sequence)
                frames: dict[tuple, list[tuple[str, np.ndarray, np.ndarray]]] = {}
                for sample in frame["measurements"]:
                    key = (sample["quantity"], sample.get("unit", ""), sample.get("axis_unit", ""))
                    if sample["quantity"] == "waveform":
                        key += (sample.get("channel", 1),)
                    y = sample["values"].reshape(-1)
                    x = np.arange(y.size) if sample["axis"] is None else sample["axis"].reshape(-1)
                    frames.setdefault(key, []).append((f"CH {sample.get('channel', 1)}", x, y))
                for index, (key, curves) in enumerate(frames.items(), 1):
                    suffix = f"CH{key[3]}" if len(key) == 4 else f"group-{index:02d}"
                    draw(
                        key,
                        curves,
                        f"frame-{sequence:06d}-{suffix}",
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
