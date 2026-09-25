from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from lab_control.domain.instruments import MeasurementBatch, ValidationError, jsonable, utc_now


def application_home() -> Path:
    return Path(
        os.environ.get("LAB_CONTROL_HOME")
        or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl"
    )


class Repository:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or application_home()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
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
            return [json.loads(row[0]) for row in self.db.execute("SELECT document FROM inventory")]

    def save(self, record: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO inventory VALUES (?, ?)",
                (record["id"], json.dumps(jsonable(record), ensure_ascii=False)),
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

    def create_recording(self, mode: str = "electronic", **options):
        from lab_control.storage.recording import Recording

        return Recording(self.root, mode, **options)

    def export(self, batch: MeasurementBatch, mode: str = "electronic") -> Path:
        if mode not in {"electronic", "optical"}:
            raise ValidationError("Invalid experiment mode")
        folder = self.root / "data" / mode / (utc_now().replace(":", "-") + "-" + uuid4().hex[:8])
        folder.mkdir(parents=True)
        (folder / "snapshot.json").write_text(
            json.dumps(jsonable(batch), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
        arrays: dict[str, Any] = {}
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
                ]
            )
            for index, measurement in enumerate(batch.measurements):
                values = np.asarray(measurement.values).reshape(-1)
                axis = (
                    np.asarray(measurement.axis) if measurement.axis is not None else np.arange(values.size)
                )
                if axis.size != values.size:
                    raise ValidationError("Axis/data length mismatch")
                arrays[f"values_{index}"] = values
                arrays[f"axis_{index}"] = axis
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
                        ]
                    )
        np.savez_compressed(folder / "samples.npz", **arrays)
        return folder

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
