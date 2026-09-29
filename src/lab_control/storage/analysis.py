"""Human-readable, bounded and resumable exports of committed measurements.

No instrument imports or model-specific protocol knowledge belong here. A frame
is committed only after all its data parts and index rows have been fsynced.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import math
import os
import re
import struct
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, Iterator

import numpy as np

from lab_control.storage.layout import atomic_json, atomic_text, manifest, raw_folder

MAX_ROWS = 1_000_000
MAX_BYTES = 100 * 1024 * 1024
INDEX = "帧索引.csv"
CHECKPOINT = "analysis-checkpoint.json"
PENDING = "analysis-pending.json"
LABELS = {
    "voltage": "电压",
    "current": "电流",
    "power": "功率",
    "reading": "读数",
    "waveform": "波形",
    "spectrum": "幅度",
    "phase_noise": "相位噪声",
    "am_noise": "幅度噪声",
    "noise_floor": "噪声底",
    "stability": "稳定度",
    "phase_difference": "相位差",
    "frequency": "频率",
    "s_parameters": "复数参数",
    "measured_voltage": "实测电压",
    "measured_current": "实测电流",
}
ARRAY_QUANTITIES = {"waveform", "spectrum", "phase_noise", "am_noise", "noise_floor", "stability"}


def _read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _zone(metadata: dict[str, Any]) -> timezone:
    try:
        offset = datetime.fromisoformat(metadata["started_local"]).utcoffset()
        if offset is not None:
            return timezone(offset)
    except (KeyError, TypeError, ValueError):
        pass
    return UTC


def _local(stamp: Any, zone: timezone) -> str:
    try:
        value = datetime.fromisoformat(stamp)
        if value.utcoffset() is not None:
            return value.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S.%f")
    except (TypeError, ValueError):
        pass
    return ""


def _time_header(zone: timezone) -> str:
    return f"本地时间({zone.tzname(None)})"


def _text(value: Any) -> Any:
    # Labels and instrument status text must remain text when opened in Excel.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _line(values: list[Any]) -> bytes:
    text = io.StringIO(newline="")
    csv.writer(text).writerow([_text(value) for value in values])
    return text.getvalue().encode("utf-8")


def _quality(frame: dict[str, Any]) -> str:
    qualities = {s.get("quality", "unknown") for s in frame["measurements"]}
    labels = {"good": "正常", "simulated": "模拟", "unknown": "未知"}
    if len(qualities) == 1:
        quality = next(iter(qualities))
        return labels.get(quality, quality)
    return "; ".join(
        f"CH{s.get('channel', 1)} {LABELS.get(s['quantity'], s['quantity'])}="
        + labels.get(s.get("quality", "unknown"), s.get("quality", "unknown"))
        for s in frame["measurements"]
    )


def _table(frame: dict[str, Any], zone: timezone) -> tuple[list[str], str, bool, Iterator[list[Any]]]:
    samples = frame["measurements"]
    values = [np.asarray(s["values"]).reshape(-1) for s in samples]
    axes = [None if s.get("axis") is None else np.asarray(s["axis"]).reshape(-1) for s in samples]
    if not samples:
        raise ValueError("Frame has no measurements")
    for sample, data, axis in zip(samples, values, axes, strict=True):
        if data.dtype.hasobject or (axis is not None and axis.size != data.size):
            raise ValueError("Invalid sample arrays or axis/data length mismatch")
        if not data.size and "display_value" not in sample.get("metadata", {}):
            raise ValueError("Empty measurement without a status value")
    array = any(
        axis is not None or data.size > 1 or s["quantity"] in ARRAY_QUANTITIES
        for s, data, axis in zip(samples, values, axes, strict=True)
    )
    header = ["帧号", "点序号"] if array else [_time_header(zone), "距记录开始(s)"]
    names: set[str] = set()
    signature: list[Any] = [array]
    for index, (sample, data, axis) in enumerate(zip(samples, values, axes, strict=True)):
        quantity, unit = sample["quantity"], sample.get("unit", "")
        label = f"CH{sample.get('channel', 1)}{LABELS.get(quantity, quantity)}"
        if label in names:
            label += f"_{index + 1}"
        names.add(label)
        metadata = sample.get("metadata", {})
        complex_values = np.iscomplexobj(data)
        signature.append(
            [
                sample.get("channel", 1),
                quantity,
                unit,
                sample.get("axis_unit", ""),
                axis is not None,
                complex_values,
                "source" in metadata,
            ]
        )
        if axis is not None:
            axis_unit = sample.get("axis_unit", "")
            axis_name = "时间" if axis_unit == "s" else "频率" if axis_unit == "Hz" else "横轴"
            if quantity == "stability":
                axis_name = "平均时间"
            elif quantity in {"phase_noise", "am_noise", "noise_floor"} and axis_unit == "Hz":
                axis_name = "偏移频率"
            header.append(f"{label}_{axis_name}" + (f"({axis_unit})" if axis_unit else ""))
        suffix = f"({unit})" if unit else ""
        if complex_values:
            header.extend(
                [
                    f"{label}_实部{suffix}",
                    f"{label}_虚部{suffix}",
                    f"{label}_线性幅值{suffix}",
                    f"{label}_相位(deg)",
                ]
            )
        else:
            header.append(label + suffix)
        if "source" in metadata:
            header.append(label + "_来源")
    if not array:
        header.append("状态")

    def rows() -> Iterator[list[Any]]:
        count = max((data.size for data in values), default=0) if array else 1
        for point in range(count):
            row = (
                [frame.get("sequence", 1), point + 1]
                if array
                else [
                    _local(frame.get("timestamp"), zone),
                    frame.get("timing", {}).get("recording_elapsed_seconds", ""),
                ]
            )
            for sample, data, axis in zip(samples, values, axes, strict=True):
                metadata = sample.get("metadata", {})
                present = point < data.size
                if axis is not None:
                    row.append(axis[point] if present else "")
                if np.iscomplexobj(data):
                    value = data[point] if present else 0j
                    row.extend(
                        [
                            value.real,
                            value.imag,
                            abs(value),
                            math.degrees(np.angle(value)) if value != 0 else "",
                        ]
                        if present
                        else [""] * 4
                    )
                else:
                    row.append(
                        data[point] if present else metadata.get("display_value", "") if not array else ""
                    )
                if "source" in metadata:
                    row.append(metadata["source"] if present or not array else "")
            if not array:
                row.append(_quality(frame))
            yield row

    return header, json.dumps(signature, ensure_ascii=False), array, rows()


def _owned(name: str) -> bool:
    return name == INDEX or bool(re.fullmatch(r"(?:数据|帧索引)_\d{5,}\.csv", name))


@contextmanager
def _export_lock(folder: Path):
    with (folder / "analysis.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class AnalysisWriter:
    """One global checkpoint covers all output parts and the frame index."""

    def __init__(
        self, folder: Path, metadata: dict[str, Any], *, max_rows: int = MAX_ROWS, max_bytes: int = MAX_BYTES
    ) -> None:
        if max_rows < 1 or max_bytes < 1:
            raise ValueError("Analysis file limits must be positive")
        self.folder, self.raw, self.metadata = folder, raw_folder(folder), metadata
        self.zone = _zone(metadata)
        self.max_rows, self.max_bytes = max_rows, max_bytes
        self.state = _read_json(
            self.raw / CHECKPOINT,
            {
                "version": 1,
                "sequence": 0,
                "frames": 0,
                "files": {},
                "current": "",
                "schema": "",
            },
        )
        if self.state.get("version") != 1:
            raise ValueError("Unsupported analysis checkpoint version")
        self.pending: list[str] = []
        self._recover()

    def _recover(self) -> None:
        files = self.state["files"]
        pending = _read_json(self.raw / PENDING, [])
        if any(not _owned(name) for name in [*files, *pending]):
            raise ValueError("Invalid analysis checkpoint filename")
        for name, value in files.items():
            offset = value["offset"]
            if not isinstance(offset, int) or offset < 0:
                raise ValueError("Invalid analysis checkpoint offset")
            with (self.folder / name).open("r+b") as stream:
                if stream.seek(0, 2) < offset:
                    raise OSError("Analysis CSV is shorter than its committed checkpoint")
                stream.truncate(offset)
        for name in pending:
            if name not in files:
                (self.folder / name).unlink(missing_ok=True)
        (self.raw / PENDING).unlink(missing_ok=True)

    def _create(self, name: str, header: list[str]) -> None:
        target = self.folder / name
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing analysis file: {target}")
        self.pending.append(name)
        atomic_json(self.raw / PENDING, self.pending)
        encoded = codecs.BOM_UTF8 + _line(header)
        if len(encoded) >= self.max_bytes and name.startswith("数据_"):
            raise ValueError("Analysis header exceeds the file size limit")
        with target.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        self.state["files"][name] = {"offset": len(encoded), "rows": 0, "header": header}

    def _part(self, header: list[str], schema: str) -> str:
        number = sum(name.startswith("数据_") for name in self.state["files"]) + 1
        name = f"数据_{number:05d}.csv"
        self._create(name, header)
        self.state.update(current=name, schema=schema)
        return name

    def _index_header(self) -> list[str]:
        return [
            "帧号",
            "数据文件",
            "起始行",
            "结束行",
            "起始点",
            "结束点",
            _time_header(self.zone),
            "距记录开始(s)",
            "读取耗时(s)",
            "状态",
            "配置版本",
            "通道接收时间(UTC)",
            "仪器触发时间(原样)",
            "时间来源",
        ]

    def _index(self, frame: dict[str, Any], name: str, start: int, end: int, point: int, count: int) -> None:
        timing = frame.get("timing", {})

        def channel_values(key: str) -> str:
            return "; ".join(
                f"CH{s.get('channel', 1)}={s['metadata'][key]}"
                for s in frame["measurements"]
                if s.get("metadata", {}).get(key)
            )

        row = [
            frame.get("sequence", 1),
            name,
            start + 2,
            end + 1,
            point + 1,
            point + count,
            _local(frame.get("timestamp"), self.zone),
            timing.get("recording_elapsed_seconds", ""),
            timing.get("read_duration_seconds", ""),
            _quality(frame),
            frame.get("config_id", frame.get("configuration", {}).get("_session_configuration_version", "")),
            channel_values("received_at"),
            channel_values("trigger_time_local"),
            "; ".join(
                filter(
                    None,
                    [
                        frame.get("time_basis", ""),
                        channel_values("received_time_source"),
                        channel_values("trigger_time_source"),
                        channel_values("axis_origin"),
                    ],
                )
            ),
        ]
        encoded = _line(row)
        if len(encoded) + len(codecs.BOM_UTF8) + len(_line(self._index_header())) > MAX_BYTES:
            raise ValueError("One frame index row exceeds the file size limit")
        index_name = self.state.get("index_current", INDEX)
        part = self.state["files"].get(index_name)
        if part is not None and (part["rows"] >= MAX_ROWS or part["offset"] + len(encoded) > MAX_BYTES):
            number = sum(name.startswith("帧索引") for name in self.state["files"]) + 1
            index_name = f"帧索引_{number:05d}.csv"
        if index_name not in self.state["files"]:
            self._create(index_name, self._index_header())
        self.state["index_current"] = index_name
        with (self.folder / index_name).open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            self.state["files"][index_name]["offset"] = stream.tell()
            self.state["files"][index_name]["rows"] += 1

    def append(self, frame: dict[str, Any]) -> None:
        header, schema, array, rows = _table(frame, self.zone)
        indexed = array and self.metadata.get("kind") != "single_frame"
        # Spill the serialized frame to disk: exact byte limits without a giant
        # StringIO or repeatedly formatting millions of values on rollover.
        with TemporaryFile(dir=self.raw) as spool:
            row_count = byte_count = 0
            header_size = len(codecs.BOM_UTF8) + len(_line(header))
            for row in rows:
                encoded = _line(row)
                if len(encoded) + header_size > self.max_bytes:
                    raise ValueError("One analysis row exceeds the file size limit")
                spool.write(struct.pack("<Q", len(encoded)))
                spool.write(encoded)
                row_count += 1
                byte_count += len(encoded)
            name = self.state["current"]
            part = self.state["files"].get(name)
            if (
                part is None
                or schema != self.state["schema"]
                or (
                    part["rows"]
                    and (
                        part["rows"] + row_count > self.max_rows
                        or part["offset"] + byte_count > self.max_bytes
                    )
                )
            ):
                name = self._part(header, schema)
            spool.seek(0)
            point = 0
            while point < row_count:
                part = self.state["files"][name]
                start, first_point = part["rows"], point
                with (self.folder / name).open("ab") as output:
                    while point < row_count:
                        position = spool.tell()
                        length = struct.unpack("<Q", spool.read(8))[0]
                        if part["rows"] >= self.max_rows or output.tell() + length > self.max_bytes:
                            spool.seek(position)
                            break
                        output.write(spool.read(length))
                        part["rows"] += 1
                        point += 1
                    output.flush()
                    os.fsync(output.fileno())
                    part["offset"] = output.tell()
                if indexed and point > first_point:
                    self._index(frame, name, start, part["rows"], first_point, point - first_point)
                if point < row_count:
                    name = self._part(header, schema)
        self.state.update(
            sequence=frame.get("sequence", 1),
            frames=self.state["frames"] + 1,
            identity=frame.get("identity", {}),
            last_timestamp=frame.get("timestamp", ""),
        )
        self.state.setdefault("first_timestamp", frame.get("timestamp", ""))
        origins = {
            f"CH{s.get('channel', 1)}={s['metadata']['axis_origin']}"
            for s in frame["measurements"]
            if s.get("metadata", {}).get("axis_origin")
        }
        self.state["axis_origins"] = sorted(set(self.state.get("axis_origins", [])) | origins)
        atomic_json(self.raw / CHECKPOINT, self.state)
        (self.raw / PENDING).unlink(missing_ok=True)
        self.pending = []


def write_description(folder: Path, status: str, *, error: str = "") -> None:
    metadata = manifest(folder)
    state = _read_json(raw_folder(folder) / CHECKPOINT, {})
    identity = metadata.get("identity") or state.get("identity") or {}
    zone = _zone(metadata)
    lines = [
        "采集记录说明",
        f"保存状态：{status}",
        f"设备：{identity.get('manufacturer', '')} {identity.get('model', '未知')}",
        f"序列号：{identity.get('serial', '') or '未提供'}",
        f"记录类型：{metadata.get('kind', '未知')}；模式：{metadata.get('mode', '未知')}",
        f"开始／保存时间：{metadata.get('started_local', '未知')}",
        f"开始 UTC：{metadata.get('started_utc', '未知')}",
        f"时间显示基准：{zone.tzname(None)}（本次记录固定偏移；历史未提供本地偏移时使用 UTC）",
        f"已生成分析数据的有效帧数：{state.get('frames', 0)}",
        f"原始首帧时间：{state.get('first_timestamp', '未知')}",
        f"原始末帧时间：{state.get('last_timestamp', '未知')}",
    ]
    summary = _read_json(raw_folder(folder) / "summary.json", {})
    if summary:
        lines.append(
            f"原始记录：已接收 {summary.get('received', 0)} 帧；已提交 {summary.get('committed', 0)} 帧"
        )
    if error:
        lines.append(f"错误：{error}；保留原始文件，可离线重新导出到新目录。")
    if metadata.get("source"):
        lines.append(f"离线导出来源：{metadata['source']}（原始数据仍在来源目录）")
    if state.get("axis_origins"):
        lines.append("横轴来源声明：" + "; ".join(state["axis_origins"]))
    lines.extend(["", "分析文件："])
    previous = None
    for name, item in state.get("files", {}).items():
        lines.append(f"- {name}：{item['rows']} 行数据")
        if item["header"] != previous:
            lines.append("  列：" + " / ".join(item["header"]))
            previous = item["header"]
    lines.extend(
        [
            "",
            "使用说明：",
            "CSV 为 UTF-8 BOM；首行为表头，可按帧号筛选并选择各通道的 X/Y 列画图。",
            "帧索引的起始／结束行为包含表头的 CSV 逻辑行号；点序号从 1 开始，跨文件不重置。",
            "多通道按点序号排列，各自保留真实横轴；空白表示未提供，不补零、不插值。",
            "主机时间表示读回时刻，不是写盘时间或物理触发时间；不同通道／设备不保证同步。",
            "距记录开始(s)来自单调时钟，未知时留空；曲线时间轴独立于此列。",
            "仪器触发时间保留原样和来源；时区未知时不转换成 UTC。",
            "参数／状态读回不是物理输出测量；零幅值复数的相位留空。",
            "数组按原顺序展平；完整形状、精度、配置版本和时间来源保存在原始数据中。",
            "_raw 内为原始数据、元数据和恢复文件；普通分析无需打开，勿删除以免失去恢复能力。",
        ]
    )
    atomic_text(folder / "记录说明.txt", "\r\n".join(lines) + "\r\n", encoding="utf-8-sig")


def _snapshot(raw: Path) -> dict[str, Any]:
    document = _read_json(raw / "snapshot.json", None)
    arrays_path = raw / "samples.npz"
    if arrays_path.exists():
        with np.load(arrays_path, allow_pickle=False) as arrays:
            if document is None and "metadata_json" in arrays:
                document = json.loads(str(arrays["metadata_json"].item()))
            if document is None:
                raise ValueError("Snapshot metadata is required to interpret these arrays")
            for index, sample in enumerate(document["measurements"]):
                sample["values"] = arrays[f"values_{index}"].copy()
                # Legacy NPZ always included a synthetic index. Do not promote
                # that index to an instrument axis when the JSON says absent.
                if sample.get("axis") is not None or ("axis" not in sample and sample.get("axis_unit")):
                    sample["axis"] = arrays[f"axis_{index}"].copy()
    elif document is not None:
        for sample in document["measurements"]:

            def decode(value: Any) -> Any:
                if isinstance(value, dict) and "real" in value and "imag" in value:
                    return complex(value["real"], value["imag"])
                return [decode(item) for item in value] if isinstance(value, list) else value

            sample["values"] = np.asarray(decode(sample["values"]))
    if document is None:
        raise ValueError("No readable raw frames or snapshot found")
    document["sequence"] = 1
    return document


def convert_analysis(
    folder: Path, *, source: Path | None = None, max_rows: int = MAX_ROWS, max_bytes: int = MAX_BYTES
) -> dict[str, Any]:
    from lab_control.storage.recording import read_frames

    started = time.perf_counter()
    raw = raw_folder(source or folder)
    metadata = manifest(folder)
    with _export_lock(raw_folder(folder)):
        writer = AnalysisWriter(folder, metadata, max_rows=max_rows, max_bytes=max_bytes)
        maximum_lag = 0.0
        paths = sorted(raw.glob("segment-*.sqlite3"))
        if paths:
            for path in paths:
                for frame in read_frames(path, writer.state["sequence"]):
                    writer.append(frame)
                    try:
                        stamp = datetime.fromisoformat(frame["timestamp"])
                        maximum_lag = max(maximum_lag, (datetime.now(UTC) - stamp).total_seconds())
                    except (ValueError, TypeError):
                        pass
        elif (raw / "snapshot.json").exists() or (raw / "samples.npz").exists():
            if not writer.state["frames"]:
                writer.append(_snapshot(raw))
        result = {
            "converted": writer.state["frames"],
            "seconds": time.perf_counter() - started,
            "csv_lag_seconds": maximum_lag,
            "error": "",
        }
        summary_path = raw_folder(folder) / "summary.json"
        summary = _read_json(summary_path, {})
        complete = source is not None or metadata.get("kind") == "single_frame"
        if summary.get("binary_done"):
            complete = writer.state["frames"] >= summary.get("committed", 0)
            summary.update(
                converted=writer.state["frames"],
                csv_error="",
                csv_seconds=result["seconds"],
                csv_lag_seconds=maximum_lag,
                analysis_state="complete" if complete else "generating",
            )
            atomic_json(summary_path, summary)
        write_description(
            folder,
            "原始数据已保存；分析 CSV 已完成" if complete else "原始数据已保存；分析 CSV 生成中",
            error=summary.get("error", ""),
        )
        return result


def export_analysis(source: Path, destination: Path) -> Path:
    """Offline export into a new, disjoint directory; never changes the source."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if (
        not source.is_dir()
        or source == destination
        or source in destination.parents
        or destination in source.parents
    ):
        raise ValueError("Choose an existing source and a separate new destination directory")
    metadata = manifest(source)
    raw = raw_folder(source)
    if not list(raw.glob("segment-*.sqlite3")) and metadata.get("kind") == "continuous":
        if _read_json(raw / "summary.json", {}).get("committed", 0):
            raise ValueError("Committed raw segments are missing")
    elif not list(raw.glob("segment-*.sqlite3")):
        frame = _snapshot(raw)
        metadata = {"kind": "single_frame", "identity": frame.get("identity"), **metadata}
        metadata.setdefault("started_local", frame.get("timestamp", ""))
        metadata.setdefault("started_utc", frame.get("timestamp", ""))
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "_raw").mkdir()
    atomic_json(
        destination / "_raw" / "recording.json",
        {
            "version": 1,
            **metadata,
            "layout_version": 2,
            "source": str(source),
        },
    )
    try:
        convert_analysis(destination, source=source)
        write_description(destination, "分析 CSV 已完成")
    except Exception as exc:
        write_description(destination, "分析 CSV 生成失败", error=str(exc))
        raise
    return destination


def verify_analysis(folder: Path) -> dict[str, Any]:
    """Offline evidence check shared by model-owned hardware test entrypoints."""
    from lab_control.domain.instruments import DeviceError
    from lab_control.storage.recording import read_frames

    def data_rows():
        for path in sorted(folder.glob("数据_*.csv")):
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream)
                header = next(reader)
                for line, row in enumerate(reader, 2):
                    yield path.name, line, header, row

    def index_rows():
        for path in sorted(folder.glob("帧索引*.csv")):
            with path.open(encoding="utf-8-sig", newline="") as stream:
                yield from csv.DictReader(stream)

    data, indexes = data_rows(), index_rows()
    frames = samples = cached = 0
    contexts: set[str] = set()
    timestamps: set[str] = set()
    zone = _zone(manifest(folder))
    try:
        for path in sorted(raw_folder(folder).glob("segment-*.sqlite3")):
            for frame in read_frames(path):
                frames += 1
                samples += sum(np.asarray(m["values"]).size for m in frame["measurements"])
                cached += frame.get("validation") == "cached"
                contexts.add(frame.get("performance", {}).get("acquisition_context_id", ""))
                timestamps.add(json.dumps(frame.get("settings_timestamps", {}), sort_keys=True))
                header, _, array, expected = _table(frame, zone)
                parts: list[dict[str, Any]] = []
                for point, row in enumerate(expected, 1):
                    name, line, actual_header, actual = next(data)
                    if actual_header != header or actual != next(csv.reader([_line(row).decode("utf-8")])):
                        raise ValueError("Binary/analysis CSV sample mismatch")
                    if not parts or parts[-1]["数据文件"] != name:
                        parts.append(
                            {
                                "帧号": frame["sequence"],
                                "数据文件": name,
                                "起始行": line,
                                "起始点": point,
                                "配置版本": frame["config_id"],
                            }
                        )
                    parts[-1].update(结束行=line, 结束点=point)
                if array:
                    for part in parts:
                        index = next(indexes)
                        if any(index[key] != str(value) for key, value in part.items()):
                            raise ValueError("Binary/analysis frame index mismatch")
        if next(data, None) is not None or next(indexes, None) is not None or not frames or not samples:
            raise ValueError("Missing or extra analysis data")
    except (ValueError, KeyError, StopIteration) as exc:
        raise DeviceError(f"Analysis verification failed: {exc}", "recording_failed") from exc
    finally:
        data.close()
        indexes.close()
    return {
        "frames": frames,
        "samples": samples,
        "sample_rows": samples,
        "cached_frames": cached,
        "contexts": sorted(contexts),
        "distinct_settings_snapshots": len(timestamps),
        "equal": True,
    }
