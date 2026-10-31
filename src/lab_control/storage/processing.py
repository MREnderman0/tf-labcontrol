"""Bounded offline processing into immutable generations beside source captures."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterator
from uuid import uuid4

import numpy as np

from lab_control.domain.instruments import jsonable
from lab_control.domain.processing import RecordedDevice
from lab_control.storage.analysis import _snapshot, _table, _text, _zone
from lab_control.storage.layout import atomic_json, raw_folder
from lab_control.storage.paths import filename_part
from lab_control.storage.recording import read_frames
from lab_control.storage.recording_catalog import PROCESSING_FOLDER, linked, read_json

PROCESSING_VERSION = 1


class ProcessingCancelled(Exception):
    pass


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise ProcessingCancelled("处理已取消；原始数据和此前完整结果均保留")


def source_paths(device: RecordedDevice) -> tuple[Path, ...]:
    raw = raw_folder(device.folder)
    if any(raw.glob("segment-*.sqlite3-wal")) and not device.summary.get("binary_done"):
        raise ValueError("原始数据库仍有活动写入，请待收尾后重试")
    files = [raw / "recording.json", raw / "summary.json", raw / "acquisition-events.jsonl"]
    segments = sorted(raw.glob("segment-*.sqlite3"))
    if segments:
        expected = [f"segment-{i:05d}.sqlite3" for i in range(1, len(segments) + 1)]
        if [p.name for p in segments] != expected:
            raise ValueError("原始数据片段缺失或编号不连续")
        files += segments
        files += [Path(str(segment) + "-wal") for segment in segments]
    else:
        files += [raw / "snapshot.json", raw / "samples.npz"]
        if device.metadata.get("kind") == "continuous" and device.summary.get("committed", 0):
            raise ValueError("原始数据片段缺失")
    return tuple(path for path in files if path.is_file())


def fingerprints(device: RecordedDevice, cancel: threading.Event) -> list[dict[str, Any]]:
    result = []
    for path in source_paths(device):
        if linked(path) or linked(path.parent):
            raise ValueError("采集数据不能通过符号链接或目录联接读取")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                check_cancel(cancel)
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("处理期间原始文件发生变化，请待采集结束后重试")
        result.append(
            {
                "path": str(path.relative_to(device.folder)),
                "bytes": after.st_size,
                "mtime_ns": after.st_mtime_ns,
                "sha256": digest.hexdigest(),
            }
        )
    return result


@contextmanager
def source_segment(path: Path):
    """Read a finalized WAL on a private copy; SQLite never writes source sidecars."""
    wal = Path(str(path) + "-wal")
    if not wal.exists():
        yield path
        return
    with TemporaryDirectory(prefix="lab-history-") as temporary:
        target = Path(temporary) / path.name
        shutil.copyfile(path, target)
        shutil.copyfile(wal, Path(str(target) + "-wal"))
        # Checkpoint only this disposable copy. Both count and frame reads then
        # see the same complete snapshot, including committed WAL transactions.
        with closing(sqlite3.connect(target)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        yield target


def iter_source_frames(device: RecordedDevice) -> Iterator[dict[str, Any]]:
    raw = raw_folder(device.folder)
    paths = sorted(raw.glob("segment-*.sqlite3"))
    total = previous = 0
    if paths:
        for path in paths:
            with source_segment(path) as snapshot:
                with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                    expected = db.execute("SELECT count(*) FROM frames").fetchone()[0]
                count = 0
                for frame in read_frames(snapshot):
                    sequence = frame["sequence"]
                    if sequence != previous + 1:
                        raise ValueError("原始帧编号缺失、重复或乱序")
                    count += 1
                    total += 1
                    previous = sequence
                    yield frame
                if count != expected:
                    raise ValueError("原始帧配置缺失，不能完整解析")
        committed = device.summary.get("committed")
        if committed is not None and total != committed:
            raise ValueError(f"原始帧数与记录摘要不一致：{total} / {committed}")
    elif (raw / "snapshot.json").is_file() or (raw / "samples.npz").is_file():
        yield _snapshot(raw)
    elif device.metadata.get("kind") != "continuous" or device.summary.get("committed") != 0:
        raise ValueError("原始数据缺失；无法确认这是空记录")


def result_home(entry_folder: Path, device: RecordedDevice) -> Path:
    key = str(device.metadata.get("run_id") or device.folder.name)
    suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    label = device.metadata.get("slot_label") or (device.metadata.get("identity") or {}).get("model")
    return entry_folder / PROCESSING_FOLDER / f"{filename_part(label or device.label)}_{suffix}"


def current_result(home: Path) -> Path | None:
    pointer = read_json(home / "current.json", {})
    if not isinstance(pointer, dict):
        raise ValueError("处理结果索引格式损坏")
    name = pointer.get("generation", "")
    if not name or Path(name).name != name or not name.startswith("result-"):
        return None
    result = home / name
    if linked(home) or linked(result) or not (result / "result.json").is_file():
        return None
    metadata = read_json(result / "result.json", {})
    if not isinstance(metadata, dict):
        raise ValueError("处理结果说明格式损坏")
    return result if metadata.get("state") == "complete" and (result / "data.sqlite3").is_file() else None


def _json(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def reusable_result(folder: Path, metadata: dict[str, Any]) -> bool:
    """A damaged cache or missing CSV is rebuilt, even with unchanged source data."""
    try:
        with closing(
            sqlite3.connect((folder / "data.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return False
            frames = db.execute(
                "SELECT count(*) FROM frames JOIN configs ON frames.config_id=configs.id"
            ).fetchone()[0]
            if (
                frames != metadata["frames"]
                or db.execute("SELECT count(DISTINCT seq) FROM samples").fetchone()[0] != frames
            ):
                return False
        previous = 0
        checked = set()
        with (folder / "帧索引.csv").open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                sequence = int(row["帧号"])
                if sequence not in {previous, previous + 1} or sequence < 1:
                    return False
                previous = sequence
                name = row["数据文件"]
                if name not in checked:
                    if (
                        Path(name).name != name
                        or not (folder / name).is_file()
                        or not (folder / name).stat().st_size
                    ):
                        return False
                    checked.add(name)
        return previous == frames
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, csv.Error):
        return False


class ProcessedWriter:
    """Buffered CSV plus exact typed arrays; only a published generation is readable."""

    def __init__(self, folder: Path, metadata: dict[str, Any], cancel: threading.Event):
        self.folder, self.metadata, self.cancel = folder, metadata, cancel
        self.db = sqlite3.connect(folder / "data.sqlite3")
        self.db.executescript("""
            PRAGMA user_version=1;
            CREATE TABLE configs(id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE frames(seq INTEGER PRIMARY KEY, config_id TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE samples(seq INTEGER, idx INTEGER, document TEXT NOT NULL,
                data BLOB NOT NULL, axis BLOB, PRIMARY KEY(seq,idx));
            CREATE TABLE timeline(seq INTEGER PRIMARY KEY, stamp TEXT NOT NULL, elapsed REAL NOT NULL);
            CREATE TABLE scalar(seq INTEGER, series TEXT, value REAL, config_id TEXT, PRIMARY KEY(series,seq));
            CREATE TABLE series(id TEXT PRIMARY KEY, document TEXT NOT NULL);
        """)
        self.zone = _zone(metadata)
        self.frames = self.points = self.part = self.rows = 0
        self.schema = ""
        self.stream: Any = None
        self.writer: Any = None
        self.index_stream = (folder / "帧索引.csv").open("x", encoding="utf-8-sig", newline="")
        self.index = csv.writer(self.index_stream)
        self.index.writerow(["帧号", "采集时间", "距记录开始(s)", "数据文件", "起始行", "结束行", "配置标识"])
        self.first_stamp: float | None = None
        self.kind: str | None = None
        self.previous_elapsed: float | None = None
        self.closed = False

    def append(self, frame: dict[str, Any]) -> None:
        check_cancel(self.cancel)
        samples = frame.get("measurements", [])
        if not samples:
            raise ValueError("记录包含空帧")
        is_array = False
        for sample in samples:
            values = np.asarray(sample["values"])
            axis = sample.get("axis")
            if (
                not values.size
                or values.dtype.kind not in "fiu"
                or not np.isfinite(values).all()
                or sample.get("quality", "good") not in {"good", "simulated"}
            ):
                raise ValueError("记录包含无效测量值，不发布不完整结果")
            if axis is not None:
                axis = np.asarray(axis)
                if axis.size != values.size or axis.dtype.kind not in "fiu" or not np.isfinite(axis).all():
                    raise ValueError("曲线横轴无效或与数据长度不一致")
            is_array |= axis is not None or values.size > 1
        kind = "frames" if is_array else "scalar"
        if self.kind is not None and self.kind != kind:
            raise ValueError("同次记录混合单点和整帧数据，须分开处理")
        self.kind = kind
        stamp = frame.get("timestamp", "")
        moment = datetime.fromisoformat(stamp)
        if moment.utcoffset() is None:
            raise ValueError("历史时间缺少时区，不能推测时间曲线")
        elapsed = frame.get("timing", {}).get("recording_elapsed_seconds")
        if elapsed is None:
            absolute = moment.timestamp()
            if self.first_stamp is None:
                self.first_stamp = absolute
            elapsed = absolute - self.first_stamp
        elapsed = float(elapsed)
        if not np.isfinite(elapsed) or (
            self.previous_elapsed is not None and elapsed < self.previous_elapsed
        ):
            raise ValueError("采集相对时间无效或发生回退")
        self.previous_elapsed = elapsed
        seq = self.frames + 1
        config = _json(
            {
                "configuration": frame.get("configuration", {}),
                "settings_timestamps": frame.get("settings_timestamps", {}),
            }
        )
        config_id = hashlib.sha256(config.encode("utf-8")).hexdigest()
        document = {
            key: value
            for key, value in frame.items()
            if key not in {"measurements", "configuration", "settings_timestamps", "config_id"}
        }
        self.db.execute("INSERT OR IGNORE INTO configs VALUES (?,?)", (config_id, config))
        self.db.execute("INSERT INTO frames VALUES (?,?,?)", (seq, config_id, _json(document)))
        self.db.execute("INSERT INTO timeline VALUES (?,?,?)", (seq, stamp, elapsed))
        setting_id = hashlib.sha256(_json(frame.get("configuration", {})).encode("utf-8")).hexdigest()
        for index, sample in enumerate(samples):
            data = np.asarray(sample["values"])
            axis = None if sample.get("axis") is None else np.asarray(sample["axis"])
            description = {key: value for key, value in sample.items() if key not in {"values", "axis"}}
            description.update(
                dtype=data.dtype.str,
                shape=data.shape,
                axis_dtype=None if axis is None else axis.dtype.str,
                axis_shape=None if axis is None else axis.shape,
            )
            self.db.execute(
                "INSERT INTO samples VALUES (?,?,?,?,?)",
                (seq, index, _json(description), data.tobytes(), None if axis is None else axis.tobytes()),
            )
            signature = _json(
                [
                    sample.get("channel", 1),
                    sample["quantity"],
                    sample.get("unit", ""),
                    sample.get("axis_unit", ""),
                ]
            )
            series = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
            self.db.execute("INSERT OR IGNORE INTO series VALUES (?,?)", (series, _json(description)))
            if kind == "scalar":
                self.db.execute(
                    "INSERT INTO scalar VALUES (?,?,?,?)", (seq, series, float(data.item()), setting_id)
                )
            self.points += data.size
        header, schema, _array, rows = _table(frame, self.zone, self.metadata.get("slot_label", ""))
        if self.stream is None or schema != self.schema:
            self._new_part(header, schema)
        first = self.rows + 2
        for index, row in enumerate(rows):
            if index % 4096 == 0:
                check_cancel(self.cancel)
            if self.rows >= 1_000_000:
                self._index(seq, stamp, elapsed, first, config_id)
                self._new_part(header, schema)
                first = 2
            self.writer.writerow([_text(value) for value in row])
            self.rows += 1
        self._index(seq, stamp, elapsed, first, config_id)
        self.frames += 1
        if self.frames % 256 == 0:
            self.db.commit()

    def _index(self, seq, stamp, elapsed, first, config) -> None:
        if self.rows + 1 >= first:
            self.index.writerow(
                [seq, stamp, elapsed, f"数据_{self.part:05d}.csv", first, self.rows + 1, config]
            )

    def _new_part(self, header, schema) -> None:
        if self.stream is not None:
            self.stream.close()
        self.part += 1
        self.rows = 0
        self.schema = schema
        self.stream = (self.folder / f"数据_{self.part:05d}.csv").open("x", encoding="utf-8-sig", newline="")
        self.writer = csv.writer(self.stream)
        self.writer.writerow(header)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.db.commit()
        finally:
            self.db.close()
            self.index_stream.close()
            if self.stream is not None:
                self.stream.close()


def process_device(
    device: RecordedDevice,
    entry: Path,
    cancel: threading.Event,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
    *,
    finalize: Callable[[Path], None] | None = None,
) -> Path:
    if device.error:
        raise ValueError(device.error)
    if device.summary.get("binary_done") is False or device.summary.get("analysis_state") == "generating":
        raise ValueError("记录仍在采集、收尾或转换")
    from lab_control.instruments.registry import DEVICE_DEFINITIONS

    definition = DEVICE_DEFINITIONS.get(device.metadata.get("adapter_id", ""))
    profile = definition.offline_processing if definition else None
    if profile is None:
        raise ValueError(f"{device.label} 暂不支持离线处理")
    source = fingerprints(device, cancel)
    home = result_home(entry, device)
    if linked(home.parent) or linked(home):
        raise ValueError("处理目录不能是符号链接或目录联接")
    try:
        previous = current_result(home)
    except (OSError, ValueError, TypeError):
        previous = None  # Rebuild a damaged pointer without removing prior generations.
    if previous:
        saved = read_json(previous / "result.json", {})
        if (
            saved.get("version") == PROCESSING_VERSION
            and saved.get("inputs") == source
            and reusable_result(previous, saved)
        ):
            if finalize is not None:
                finalize(previous)
            return previous
    home.mkdir(parents=True, exist_ok=True)
    staging = home / f".processing-{uuid4().hex}"
    staging.mkdir()
    writer = None
    try:
        writer = ProcessedWriter(staging, device.metadata, cancel)
        for frame in iter_source_frames(device):
            writer.append(frame)
            if writer.frames % 64 == 0:
                progress(writer.frames, int(device.summary.get("committed", 0)))
        writer.close()
        stats: dict[str, Any] = {
            "frames": writer.frames,
            "points": writer.points,
            "kind": writer.kind or profile.kind,
        }
        writer = None
        check_cancel(cancel)
        if fingerprints(device, cancel) != source:
            raise ValueError("处理期间原始数据变化，未发布结果")
        metadata = {
            "version": PROCESSING_VERSION,
            "state": "complete",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "software_version": "0.3.0",
            "source": os.path.relpath(device.folder, home),
            "inputs": source,
            "recording": device.metadata,
            "power_units": profile.power_units,
            **stats,
            "warning": device.summary.get("error") or device.summary.get("paused") or "",
        }
        atomic_json(staging / "result.json", metadata)
        if finalize is not None:
            finalize(staging)
            if fingerprints(device, cancel) != source:
                raise ValueError("生成图片期间原始数据变化，未发布结果")
        check_cancel(cancel)
        (staging / "处理说明.txt").write_text(
            f"设备：{device.label}\n已处理 {stats['frames']} 帧、{stats['points']} 个数值。\n"
            "CSV 保留全部有效数值；data.sqlite3 保存原精度数组、配置、通道及时间索引。\n"
            "时间表示原记录读回时间，不推断物理触发或设备间同步。\n"
            "曲线缩放只影响显示，不插值、不补零、不修改原始文件。\n"
            f"原记录提示：{metadata['warning'] or '无'}\n",
            encoding="utf-8-sig",
        )
        for path in staging.rglob("*"):
            if not path.is_file():
                continue
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
        generation = home / f"result-{uuid4().hex}"
        staging.rename(generation)
        atomic_json(home / "current.json", {"generation": generation.name})
        progress(stats["frames"], stats["frames"])
        return generation
    finally:
        if writer is not None:
            writer.close()
        if staging.exists() and staging.resolve().parent == home.resolve() and not linked(staging):
            shutil.rmtree(staging)
