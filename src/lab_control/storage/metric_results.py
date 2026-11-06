"""Verified source references and small durable results; no model algorithms."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import Field

from lab_control.domain.instruments import DeviceError
from lab_control.domain.metrics import MetricParameters
from lab_control.domain.processing import RecordedDevice
from lab_control.storage.layout import atomic_json, raw_folder
from lab_control.storage.processing import check_cancel, fingerprints, iter_source_frames, source_paths
from lab_control.storage.recording_catalog import RecordingCatalog, linked, read_json

RESULT_FOLDER = "指标结果"
MAX_RESULT_BYTES = 256 * 1024


class _SavedMetric(MetricParameters):
    value: float | int | None
    unit: str
    quality: Literal["good", "simulated", "unavailable"]


class _ResultDocument(MetricParameters):
    schema_version: Literal[1]
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    recording_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    device_id: str = Field(min_length=1)
    identity: dict[str, str]
    channels: list[Annotated[int, Field(ge=1)]] = Field(min_length=1)
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    profile_version: str = Field(min_length=1)
    parameters: dict[str, Any]
    cache_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    completed_at: str
    complete: bool
    quality: Literal["good", "simulated"]
    metrics: dict[str, _SavedMetric] = Field(min_length=1, max_length=128)


def validate_result(result: Any) -> None:
    try:
        saved = _ResultDocument.model_validate(result)
        json.dumps(result, allow_nan=False)
        if datetime.fromisoformat(saved.completed_at).utcoffset() is None:
            raise ValueError("Result completion time must have a timezone")
        if len(saved.channels) != len(set(saved.channels)):
            raise ValueError("Duplicate result channels")
        for name, metric in saved.metrics.items():
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name):
                raise ValueError("Invalid metric name")
            if (metric.value is None) != (metric.quality == "unavailable"):
                raise ValueError("Invalid metric quality")
            if saved.quality == "simulated" and metric.quality == "good":
                raise ValueError("Simulated results cannot report measured values")
    except (ValueError, TypeError) as exc:
        raise DeviceError("指标结果格式或数值损坏", "result_unavailable") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class MetricRecord:
    id: str
    device: RecordedDevice
    capture_folder: Path

    @property
    def device_id(self) -> str:
        return self.device.metadata["device_id"]


class MetricStore:
    def __init__(self, repository):
        self.repository = repository
        self.catalog = RecordingCatalog(repository)
        with repository.lock, repository.db:
            repository.db.execute(
                "CREATE TABLE IF NOT EXISTS metric_results (id TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )

    def records(self) -> tuple[dict[str, MetricRecord], set[str]]:
        records: dict[str, MetricRecord] = {}
        conflicts: set[str] = set()
        for entry in self.catalog.scan():
            for device in entry.devices:
                meta = device.metadata
                identity = meta.get("identity") or {}
                if (
                    device.error
                    or meta.get("version") != 1
                    or not all(
                        isinstance(meta.get(k), str) and meta[k]
                        for k in ("run_id", "device_id", "adapter_id")
                    )
                    or not isinstance(identity, dict)
                    or not all(
                        isinstance(identity.get(k), str) and identity[k].strip()
                        for k in ("manufacturer", "model", "serial")
                    )
                    or identity["serial"].strip().upper() in {"0", "N/A", "UNKNOWN"}
                    or linked(raw_folder(device.folder))
                ):
                    continue
                # A run ID is independent of names, locations, slots and processing versions.
                key = digest([meta["device_id"], meta["run_id"]])[:32]
                if key in records and records[key].device.folder != device.folder:
                    conflicts.add(key)
                records[key] = MetricRecord(key, device, entry.folder)
        return records, conflicts

    def resolve(self, recording_id: str) -> MetricRecord:
        records, conflicts = self.records()
        if recording_id in conflicts:
            raise DeviceError("记录 ID 对应多个目录，请在本机核对副本", "recording_conflict")
        if recording_id not in records:
            raise DeviceError("规范记录不存在或缺少可核验身份", "recording_unavailable")
        return records[recording_id]

    @staticmethod
    def stamp(record: MetricRecord) -> list[tuple[str, int, int]]:
        """Cheap admission snapshot; full hashing/decoding belongs to the bounded worker."""
        result = []
        for path in source_paths(record.device):
            if linked(path) or linked(path.parent):
                raise DeviceError("采集数据不能通过链接读取", "recording_unavailable")
            stat = path.stat()
            result.append((str(path.relative_to(record.device.folder)), stat.st_size, stat.st_mtime_ns))
        return result

    @staticmethod
    def ready(record: MetricRecord) -> bool:
        device, summary = record.device, record.device.summary
        if device.metadata.get("kind") not in {"single_frame", "continuous"}:
            raise DeviceError("未知采集类型", "recording_unavailable")
        if (
            summary.get("binary_done") is False
            or summary.get("analysis_state") == "generating"
            or (device.metadata["kind"] == "continuous" and not summary.get("binary_done"))
        ):
            raise DeviceError("记录尚未完成保存及转换", "recording_busy")
        return not any(summary.get(k) for k in ("error", "csv_error", "pause_reason", "paused"))

    @staticmethod
    def fingerprint(record: MetricRecord, cancel: threading.Event) -> str:
        # Content identity survives moves/copies and timestamp-only changes.
        return digest(
            [
                {k: v for k, v in item.items() if k != "mtime_ns"}
                for item in fingerprints(record.device, cancel)
            ]
        )

    def inspect(self, record: MetricRecord, cancel: threading.Event) -> dict:
        device = record.device
        complete = self.ready(record)
        before = self.fingerprint(record, cancel)
        count, channels, qualities = 0, set(), set()
        last_complete = None
        for frame in iter_source_frames(device):
            check_cancel(cancel)
            count += 1
            stamp = datetime.fromisoformat(frame["timestamp"])
            if stamp.utcoffset() is None or not frame.get("time_basis"):
                raise DeviceError("记录缺少明确的时间及来源", "recording_unavailable")
            expected = device.metadata["identity"]
            if any(
                frame.get("identity", {}).get(k, "") != expected.get(k, "")
                for k in ("manufacturer", "model", "serial")
            ):
                raise DeviceError("帧身份与记录清单不一致", "identity_conflict")
            if frame.get("validation") == "live":
                complete = False
            config = frame.get("configuration", {})
            if "complete" in config:
                last_complete = config["complete"]
            if config.get("has_data_gaps") or config.get("_acquisition_gap"):
                complete = False
            if not frame.get("measurements"):
                raise DeviceError("记录包含空帧", "recording_unavailable")
            for sample in frame["measurements"]:
                channel = sample.get("channel")
                data = np.asarray(sample["values"])
                axis = sample.get("axis")
                if (
                    type(channel) is not int
                    or channel < 1
                    or sample.get("quality") not in {"good", "simulated"}
                    or not data.size
                    or not np.isfinite(data).all()
                    or (axis is not None and (np.size(axis) != data.size or not np.isfinite(axis).all()))
                ):
                    raise DeviceError("记录包含无效通道、数值或质量", "recording_unavailable")
                channels.add(channel)
                qualities.add(sample["quality"])
        if not count:
            raise DeviceError("记录没有有效帧", "empty_recording")
        if last_complete is False:
            complete = False
        events = raw_folder(device.folder) / "acquisition-events.jsonl"
        if events.is_file():
            started, finished = False, False
            with events.open(encoding="utf-8") as stream:
                for line in stream:
                    check_cancel(cancel)
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise DeviceError("采集结束证据损坏", "recording_unavailable")
                    if event.get("event") == "acquisition_interrupted":
                        complete = False
                    if event.get("event") == "acquisition_started":
                        started = True
                    if event.get("event") == "acquisition_finished":
                        finished = True
                        if type(event.get("complete")) is not bool:
                            raise DeviceError("采集完整性证据损坏", "recording_unavailable")
                        complete = complete and event["complete"]
            if started and not finished:
                raise DeviceError("采集尚无成功或失败的收尾证据", "recording_busy")
        if self.fingerprint(record, cancel) != before:
            raise DeviceError("检查期间原始数据发生变化", "source_changed")
        return {
            "fingerprint": before,
            "channels": sorted(channels),
            "frames": count,
            "complete": complete,
            "quality": "simulated" if "simulated" in qualities else "good",
        }

    def index(self, record: MetricRecord, path: Path, result: dict) -> None:
        value = {
            "recording_id": record.id,
            "device_id": record.device_id,
            "name": path.name,
            "folder": str(record.device.folder),
            "key": result["cache_key"],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        with self.repository.lock, self.repository.db:
            old = self.repository.db.execute(
                "SELECT document FROM metric_results WHERE id=?", (result["id"],)
            ).fetchone()
            if old and json.loads(old[0])["sha256"] != value["sha256"]:
                raise DeviceError("结果 ID 或内容发生冲突", "result_conflict")
            self.repository.db.execute(
                "INSERT OR REPLACE INTO metric_results VALUES (?,?)",
                (result["id"], json.dumps(value, ensure_ascii=False)),
            )

    def results(self, record: MetricRecord) -> list[dict]:
        folder = record.device.folder / RESULT_FOLDER
        if linked(folder):
            raise DeviceError("结果目录不能是链接", "result_unavailable")
        values = []
        for path in sorted(folder.glob("*.json")):
            if linked(path) or path.stat().st_size > MAX_RESULT_BYTES:
                raise DeviceError("结果文件无效", "result_unavailable")
            try:
                result = read_json(path)
            except (ValueError, OSError) as exc:
                raise DeviceError("指标结果无法读取", "result_unavailable") from exc
            validate_result(result)
            if (
                result.get("recording_id") != record.id
                or result.get("device_id") != record.device_id
                or result.get("identity") != record.device.metadata["identity"]
                or path.stem != result["id"]
            ):
                raise DeviceError("结果与原始记录关联不符", "result_unavailable")
            self.index(record, path, result)
            values.append(result)
        return values

    def get(self, result_id: str) -> tuple[MetricRecord, dict]:
        with self.repository.lock:
            row = self.repository.db.execute(
                "SELECT document FROM metric_results WHERE id=?", (result_id,)
            ).fetchone()
        if row:
            record = self.resolve(json.loads(row[0])["recording_id"])
            for result in self.results(record):
                if result["id"] == result_id:
                    return record, result
        raise DeviceError("结果不存在，请先列出记录的处理结果", "result_unavailable")

    def publish(self, record: MetricRecord, result: dict) -> None:
        validate_result(result)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            raise DeviceError("指标结果过大；请只返回小型数值结果", "result_too_large")
        folder = record.device.folder / RESULT_FOLDER
        if linked(folder):
            raise DeviceError("结果目录不能是链接", "result_unavailable")
        folder.mkdir(exist_ok=True)
        path = folder / (result["id"] + ".json")
        if path.exists():
            raise DeviceError("结果 ID 已存在，未覆盖", "result_conflict")
        atomic_json(path, result)
        try:
            self.index(record, path, result)
        except Exception:
            # A published file without a committed index is not a successful job.
            # Keep evidence as an unlisted file for local diagnosis, never cache it.
            path.rename(path.with_suffix(".unindexed"))
            raise

    def invalidate(self, record: MetricRecord, result_id: str) -> None:
        path = record.device.folder / RESULT_FOLDER / (result_id + ".json")
        path.rename(path.with_suffix(".unindexed"))
        with self.repository.lock, self.repository.db:
            self.repository.db.execute("DELETE FROM metric_results WHERE id=?", (result_id,))
