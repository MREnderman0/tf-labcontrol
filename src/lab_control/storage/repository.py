from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryFile, mkstemp
from typing import Any
from uuid import uuid4

import numpy as np

from lab_control.domain.instruments import DeviceIdentity, MeasurementBatch, jsonable, utc_now
from lab_control.domain.workspace import GENERAL, slot_label
from lab_control.storage.audit import AuditSink
from lab_control.storage.frame_timing import TIMING_COLUMNS, timing_columns
from lab_control.storage.paths import BatchSaveContext, SaveContext, SaveKind
from lab_control.storage.runtime import runtime_home


def application_home() -> Path:
    return runtime_home()


class Repository:
    def __init__(self, root: Path | None = None, *, simulation: bool = False) -> None:
        self.root = root or application_home()
        self.simulation = simulation
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.audit_sink = AuditSink(lambda event, **details: self.audit(event, **details))
        self.db = sqlite3.connect(self.root / "inventory.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS inventory (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS scans (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS workspace (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        self.db.commit()

    def load_workspace(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT document FROM workspace WHERE id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def save_workspace(self, key: str, value: Any) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO workspace VALUES (?, ?)",
                (key, json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False)),
            )

    def records(self) -> list[dict[str, Any]]:
        with self.lock:
            # A damaged historical database may expose a scan document through
            # a stale SQLite primary-key index.  Keep the inventory boundary
            # strict so one malformed row cannot prevent the application from
            # opening the remaining device records; the original row remains
            # untouched for offline repair.
            result = []
            for row in self.db.execute("SELECT document FROM inventory"):
                document = json.loads(row[0])
                if isinstance(document, dict) and isinstance(document.get("candidates"), dict):
                    result.append(document)
            return result

    def save(self, record: dict[str, Any]) -> None:
        self.save_many((record,))

    def save_many(self, records: tuple[dict[str, Any], ...]) -> None:
        """Commit related inventory changes together, rolling back every row on failure."""
        rows = [(record["id"], json.dumps(jsonable(record), ensure_ascii=False)) for record in records]
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO inventory VALUES (?, ?)",
                rows,
            )

    def scan(self, record: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO scans VALUES (?, ?)",
                (record["id"], json.dumps(jsonable(record), ensure_ascii=False)),
            )

    def remove_inventory(self, device_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM inventory WHERE id = ?", (device_id,))

    def audit(self, event: str, **details: Any) -> None:
        with self.lock:
            folder = self.root / "logs"
            folder.mkdir(exist_ok=True)
            with (folder / f"{utc_now()[:10]}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        jsonable({"timestamp": utc_now(), "event": event, **details}),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    + "\n"
                )

    @property
    def data_root_base(self) -> Path:
        value = self.load_workspace("data_storage/root")
        return Path(value).absolute() if value else (self.root / "data").absolute()

    @property
    def data_root(self) -> Path:
        value = self.load_workspace("data_storage/root")
        if value:
            return (Path(value) / "simulation" if self.simulation else Path(value)).absolute()
        return (self.root / "data").absolute()

    def set_data_root(self, root: Path) -> Path:
        from lab_control.domain.instruments import ValidationError

        root = root.expanduser()
        if not root.is_absolute():
            raise ValidationError("请选择数据保存目录的绝对路径")
        root = root.resolve()
        custom = root != (self.root / "data").resolve()
        effective = root / "simulation" if self.simulation and custom else root
        effective.mkdir(parents=True, exist_ok=True)
        with TemporaryFile(dir=effective, prefix=".labcontrol-write-") as probe:
            probe.write(b"storage-check")
            probe.flush()
            os.fsync(probe.fileno())
            probe.seek(0)
            if probe.read() != b"storage-check":
                raise OSError("数据目录读写校验失败")
        self.save_workspace("data_storage/root", str(root) if custom else None)
        return effective

    def new_batch_context(self) -> BatchSaveContext:
        return BatchSaveContext(self.data_root)

    def new_save_context(
        self,
        mode: str,
        kind: SaveKind,
        *,
        device_id: str = "",
        identity: DeviceIdentity | None = None,
        adapter_id: str = "",
        batch: BatchSaveContext | None = None,
    ) -> SaveContext:
        slot = ""
        if device_id:
            record = next((r for r in self.records() if r["id"] == device_id), {})
            slots = record.get("slots", {})
            slot = slots.get(GENERAL, slots.get(mode, ""))
            adapter_id = adapter_id or record.get("adapterId", "")
            if identity is None and record.get("identity"):
                identity = DeviceIdentity(**record["identity"])
        return SaveContext(
            batch.root if batch else self.data_root,
            mode,
            kind,
            device_id,
            identity,
            adapter_id,
            batch=batch,
            slot=slot,
            slot_label=slot_label(slot) if slot else "",
        )

    def create_recording(self, mode: str | None = None, *, context: SaveContext | None = None, **options):
        from lab_control.storage.recording import Recording

        mode = (context.mode if context else GENERAL) if mode is None else mode
        return Recording(
            self.root, mode, context=context or self.new_save_context(mode, "continuous"), **options
        )

    def export(
        self, batch: MeasurementBatch, mode: str | None = None, *, context: SaveContext | None = None
    ) -> Path:
        from lab_control.domain.instruments import ValidationError

        mode = (context.mode if context else GENERAL) if mode is None else mode
        context = context or self.new_save_context(mode, "single_frame", identity=batch.identity)
        context.validate(mode, "single_frame")
        folder = context.prepare()
        raw = context.raw_folder
        (raw / "snapshot.json").write_text(
            json.dumps(jsonable(batch), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
        arrays: dict[str, Any] = {}
        for index, measurement in enumerate(batch.measurements):
            values = np.asarray(measurement.values).reshape(-1)
            axis = np.asarray(measurement.axis) if measurement.axis is not None else np.arange(values.size)
            if axis.size != values.size:
                raise ValidationError("Axis/data length mismatch")
            arrays[f"values_{index}"] = values
            arrays[f"axis_{index}"] = axis
        if context.layout_version == 1:
            self._export_legacy_csv(batch, raw)
        # A unicode scalar loads without pickle and travels with the arrays.
        document = jsonable(
            {f.name: getattr(batch, f.name) for f in fields(batch) if f.name != "measurements"}
        )
        document["measurements"] = [
            jsonable(
                {f.name: getattr(sample, f.name) for f in fields(sample) if f.name not in {"values", "axis"}}
            )
            for sample in batch.measurements
        ]
        arrays["metadata_json"] = np.asarray(json.dumps(document, ensure_ascii=False, allow_nan=False))
        # Publish the archive only after NumPy has closed and flushed it.  A
        # direct write to the final name lets a concurrent UI reader observe
        # the zero-length/partial ZIP between file creation and completion.
        temporary_fd, temporary_name = mkstemp(prefix=".samples-", suffix=".npz", dir=raw)
        try:
            with os.fdopen(temporary_fd, "wb") as stream:
                np.savez_compressed(stream, **arrays)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, raw / "samples.npz")
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise
        if context.layout_version == 2:
            from lab_control.domain.instruments import DeviceError
            from lab_control.storage.analysis import convert_analysis, write_description

            try:
                convert_analysis(folder)
                write_description(folder, "原始数据已保存；分析 CSV 已完成")
            except Exception as exc:
                try:
                    write_description(folder, "分析 CSV 生成失败", error=str(exc))
                except OSError:
                    pass
                raise DeviceError(
                    f"原始数据已保存至 {folder}；分析 CSV 生成失败：{exc}", "export_failed"
                ) from exc
        return folder

    @staticmethod
    def _export_legacy_csv(batch: MeasurementBatch, folder: Path) -> None:
        with (folder / "samples.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "timestamp",
                    "channel",
                    "quantity",
                    "unit",
                    "quality",
                    "axis",
                    "axis_unit",
                    "real",
                    "imaginary",
                    *TIMING_COLUMNS,
                ]
            )
            for index, measurement in enumerate(batch.measurements):
                values = np.asarray(measurement.values).reshape(-1)
                axis = (
                    np.asarray(measurement.axis) if measurement.axis is not None else np.arange(values.size)
                )
                sample_timing = timing_columns(batch.timing, measurement.metadata, batch.time_basis)
                for x, y in zip(axis, values, strict=True):
                    writer.writerow(
                        [
                            batch.timestamp,
                            measurement.channel,
                            measurement.quantity,
                            measurement.unit,
                            measurement.quality,
                            x,
                            measurement.axis_unit,
                            np.real(y),
                            np.imag(y),
                            *sample_timing,
                        ]
                    )

    def report(self, report: dict[str, Any]) -> Path:
        folder = self.root / "reports"
        folder.mkdir(exist_ok=True)
        path = folder / f"{uuid4().hex}.json"
        path.write_text(
            json.dumps(jsonable(report), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
        return path

    def close(self) -> None:
        with self.lock:
            self.db.close()
