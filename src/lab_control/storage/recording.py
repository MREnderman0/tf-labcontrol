"""Bounded asynchronous recording with durable binary frames and resumable CSV.

SQLite is owned by the binary writer. CSV runs in a separate interpreter and reads
only committed rows. No instrument handles or waveform arguments cross processes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

from lab_control.application.acquisition_pipeline import FrozenDict, batch_bytes, validate_recorded_batch
from lab_control.domain.instruments import MeasurementBatch, ValidationError, jsonable
from lab_control.domain.workspace import WORKSPACE_MODES
from lab_control.storage.frame_timing import TIMING_COLUMNS, timing_columns
from lab_control.storage.layout import manifest, raw_folder
from lab_control.storage.paths import SaveContext

_CSV_SLOTS = threading.BoundedSemaphore(2)


def _json(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        output.write(_json(value))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class SegmentWriter:
    def __init__(self, folder: Path, *, seconds: float = 60, size: int = 64 * 1024 * 1024) -> None:
        self.folder, self.seconds, self.size = folder, seconds, size
        self.connection: sqlite3.Connection | None = None
        self.paths: list[str] = []
        self.opened = 0.0
        self.bytes = 0

    def _open(self) -> sqlite3.Connection:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"segment-{len(self.paths) + 1:05d}.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript("""
            PRAGMA user_version=1;
            CREATE TABLE configs (id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE frames (seq INTEGER PRIMARY KEY, config_id TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE samples (seq INTEGER, idx INTEGER, document TEXT NOT NULL,
                data BLOB NOT NULL, axis BLOB, PRIMARY KEY(seq, idx));
        """)
        self.paths.append(str(path))
        self.connection, self.opened, self.bytes = connection, time.monotonic(), 0
        return connection

    def append(self, sequence: int, batch: MeasurementBatch) -> str:
        if self.connection and (time.monotonic() - self.opened >= self.seconds or self.bytes >= self.size):
            self.close()
        connection = self.connection or self._open()
        config = _json(
            {"configuration": batch.configuration, "settings_timestamps": batch.settings_timestamps}
        )
        config_id = hashlib.sha256(config.encode("utf-8")).hexdigest()
        metadata = {
            f.name: getattr(batch, f.name)
            for f in fields(batch)
            if f.name not in {"configuration", "settings_timestamps", "measurements"}
        }
        with connection:
            connection.execute("INSERT OR IGNORE INTO configs VALUES (?, ?)", (config_id, config))
            connection.execute("INSERT INTO frames VALUES (?, ?, ?)", (sequence, config_id, _json(metadata)))
            for index, sample in enumerate(batch.measurements):
                data = np.asarray(sample.values)
                axis = None if sample.axis is None else np.asarray(sample.axis)
                if data.dtype.hasobject or (axis is not None and axis.dtype.hasobject):
                    raise ValidationError("Object arrays cannot be recorded")
                if axis is not None and axis.size != data.size:
                    raise ValidationError("Axis/data length mismatch")
                descriptor = {
                    f.name: getattr(sample, f.name)
                    for f in fields(sample)
                    if f.name not in {"values", "axis"}
                }
                descriptor.update(
                    dtype=data.dtype.str,
                    shape=data.shape,
                    axis_dtype=None if axis is None else axis.dtype.str,
                    axis_shape=None if axis is None else axis.shape,
                )
                connection.execute(
                    "INSERT INTO samples VALUES (?, ?, ?, ?, ?)",
                    (
                        sequence,
                        index,
                        _json(descriptor),
                        data.tobytes(),
                        None if axis is None else axis.tobytes(),
                    ),
                )
        self.bytes += batch_bytes(batch)
        return self.paths[-1]

    def close(self) -> None:
        if self.connection:
            self.connection.close()
            self.connection = None


def read_frames(path: Path, after: int = 0) -> Iterator[dict[str, Any]]:
    """Read committed version-one frames, preserving dtype and array shape."""
    # Closed segments have checkpointed their WAL. Immutable reads avoid SQLite
    # creating new -wal/-shm files in historical (possibly read-only) folders.
    # An active writer retains these sidecars and requires normal WAL visibility.
    closed = not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm"))
    connection = sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if closed else ""), uri=True)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValidationError("Unsupported recording version")
        for sequence, config_id, document, config in connection.execute(
            "SELECT seq, config_id, frames.document, configs.document FROM frames "
            "JOIN configs ON configs.id=config_id WHERE seq>? ORDER BY seq",
            (after,),
        ):
            frame = json.loads(document) | json.loads(config)
            frame.update(sequence=sequence, config_id=config_id, measurements=[])
            for descriptor, data, axis in connection.execute(
                "SELECT document, data, axis FROM samples WHERE seq=? ORDER BY idx", (sequence,)
            ):
                sample = json.loads(descriptor)
                sample["values"] = np.frombuffer(data, dtype=sample.pop("dtype")).reshape(sample.pop("shape"))
                axis_dtype, axis_shape = sample.pop("axis_dtype"), sample.pop("axis_shape")
                sample["axis"] = (
                    None if axis is None else np.frombuffer(axis, dtype=axis_dtype).reshape(axis_shape)
                )
                frame["measurements"].append(sample)
            yield frame
    finally:
        connection.close()


def convert_csv(folder: Path) -> dict[str, Any]:
    """Checkpoint only after a complete frame is durable; truncate any uncommitted tail."""
    if manifest(folder).get("layout_version", 1) == 2:
        from lab_control.storage.analysis import convert_analysis

        return convert_analysis(folder)
    started = time.perf_counter()
    converted = 0
    maximum_lag = 0.0
    for path in sorted(folder.glob("segment-*.sqlite3")):
        checkpoint = path.with_suffix(".csv.checkpoint.json")
        state = (
            json.loads(checkpoint.read_text(encoding="utf-8"))
            if checkpoint.exists()
            else {"sequence": 0, "offset": 0, "frames": 0, "csv_format_version": 2}
        )
        # Existing checkpoints describe legacy headers. Never append a different
        # column layout to a previously committed CSV.
        csv_version = state.get("csv_format_version", 1)
        if csv_version not in (1, 2):
            raise ValidationError("Unsupported CSV format version")
        output_path = path.with_suffix(".csv")
        with output_path.open("a+b") as output:
            if output.seek(0, 2) < state["offset"]:
                raise OSError("CSV is shorter than its committed checkpoint")
            output.truncate(state["offset"])
            for frame in read_frames(path, state["sequence"]):
                text = io.StringIO(newline="")
                writer = csv.writer(text)
                if not state["offset"]:
                    writer.writerow(
                        [
                            "sequence",
                            "config_id",
                            "timestamp",
                            "channel",
                            "quantity",
                            "unit",
                            "quality",
                            "axis",
                            "axis_unit",
                            "real",
                            "imaginary",
                            *(TIMING_COLUMNS if csv_version == 2 else ()),
                        ]
                    )
                for sample in frame["measurements"]:
                    values = sample["values"].reshape(-1)
                    axis = sample["axis"]
                    axis = np.arange(values.size) if axis is None else axis.reshape(-1)
                    sample_timing = (
                        timing_columns(
                            frame.get("timing", {}),
                            sample.get("metadata", {}),
                            frame.get("time_basis", ""),
                        )
                        if csv_version == 2
                        else ()
                    )
                    for x, value in zip(axis, values, strict=True):
                        writer.writerow(
                            [
                                frame["sequence"],
                                frame["config_id"],
                                frame["timestamp"],
                                sample["channel"],
                                sample["quantity"],
                                sample["unit"],
                                sample["quality"],
                                x,
                                sample["axis_unit"],
                                np.real(value),
                                np.imag(value),
                                *sample_timing,
                            ]
                        )
                output.write(text.getvalue().encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
                state = {
                    "sequence": frame["sequence"],
                    "offset": output.tell(),
                    "frames": state["frames"] + 1,
                    "csv_format_version": csv_version,
                }
                _atomic_json(checkpoint, state)
                try:
                    stamp = datetime.fromisoformat(frame["timestamp"])
                    maximum_lag = max(maximum_lag, (datetime.now(timezone.utc) - stamp).total_seconds())
                except (ValueError, TypeError):
                    pass
            converted += state["frames"]
    return {
        "converted": converted,
        "seconds": time.perf_counter() - started,
        "csv_lag_seconds": maximum_lag,
        "error": "",
    }


class Recording:
    """One bounded recording, with a single reservation for its next in-flight read."""

    def __init__(
        self,
        root: Path,
        mode: str,
        *,
        csv_interval: float = 5,
        wake: Callable[[], None] = lambda: None,
        max_frames: int = 64,
        max_bytes: int = 128 * 1024 * 1024,
        context: SaveContext | None = None,
        prepare_storage: bool = True,
    ) -> None:
        if mode not in WORKSPACE_MODES:
            raise ValidationError("Invalid experiment mode")
        self.validate_csv_interval(csv_interval)
        self.started_monotonic = time.perf_counter()
        self.context = context or SaveContext(root / "data", mode, "continuous")
        self.context.validate(mode, "continuous")
        self.folder = self.context.folder
        self.raw_folder = self.context.raw_folder
        if prepare_storage:
            self.context.prepare()
        self.csv_interval, self.wake = csv_interval, wake
        self.max_frames, self.max_bytes = max_frames, max_bytes
        self.lock = threading.RLock()
        self.event, self.csv_event = threading.Event(), threading.Event()
        self.queue: deque[tuple[int, MeasurementBatch, int]] = deque()
        self.reserved = 0
        self.buffered = self.buffer_bytes = 0
        self.received = self.committed = self.converted = 0
        self.last_path = self.error = self.csv_error = self.pause_reason = ""
        self.write_seconds = self.csv_seconds = 0.0
        self.csv_lag_seconds = 0.0
        self.accepting, self.binary_done = True, False
        self.finished: Future[list[str]] = Future()
        self.segments = SegmentWriter(self.raw_folder)
        self.thread = threading.Thread(target=self._write_loop, name="binary-recording", daemon=True)
        self.csv_thread = threading.Thread(target=self._csv_loop, name="csv-supervisor", daemon=True)
        self.thread.start()
        self.csv_thread.start()

    @staticmethod
    def validate_csv_interval(value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 3600:
            raise ValidationError("CSV conversion interval must be 1..3600 s")

    def reserve(self, size: int) -> bool:
        with self.lock:
            if not self.accepting or self.reserved:
                return False
            if self.buffered >= self.max_frames or self.buffer_bytes + size > self.max_bytes:
                self.pause_reason = "记录已暂停：保存缓冲已满，正在排空已接收帧"
                self.accepting = False
                self.event.set()
                return False
            self.reserved = size
            return True

    def cancel_reservation(self) -> None:
        with self.lock:
            self.reserved = 0
        self.event.set()

    def submit(self, batch: MeasurementBatch) -> bool:
        validate_recorded_batch(batch)
        completed = batch.performance.get("received_at_monotonic")
        if batch.timing.get("version") == 1 and isinstance(completed, (float, int)):
            batch = replace(
                batch,
                timing=FrozenDict(
                    {
                        **batch.timing,
                        "recording_elapsed_seconds": completed - self.started_monotonic,
                    }
                ),
            )
        size = batch_bytes(batch)
        with self.lock:
            if not self.reserved:
                raise RuntimeError("Recording frame requires a reservation")
            self.reserved = 0
            if self.error or self.buffer_bytes + size > self.max_bytes:
                self.pause_reason = self.error or "记录已暂停：本帧超出保存缓冲容量，未接纳该帧"
                self.accepting = False
                self.event.set()
                return False
            self.received += 1
            self.buffered += 1
            self.buffer_bytes += size
            self.queue.append((self.received, batch, size))
        self.event.set()
        return True

    def finish(self) -> Future[list[str]]:
        with self.lock:
            self.accepting = False
        self.event.set()
        return self.finished

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(
                received=self.received,
                pending=self.buffered,
                committed=self.committed,
                converted=self.converted,
                paused=self.pause_reason,
                error=self.error,
                csv_error=self.csv_error,
                binary_done=self.binary_done,
                last_path=self.last_path,
                write_seconds=self.write_seconds,
                csv_seconds=self.csv_seconds,
                csv_interval=self.csv_interval,
                csv_lag_seconds=self.csv_lag_seconds,
                buffer_bytes=self.buffer_bytes,
                folder=str(self.folder),
                started_local=self.context.started_at.isoformat(),
                started_utc=self.context.started_utc,
                analysis_state=(
                    "failed"
                    if self.csv_error
                    else "complete"
                    if self.binary_done and self.converted >= self.committed
                    else "generating"
                ),
            )

    def _write_loop(self) -> None:
        try:
            while True:
                self.event.clear()
                with self.lock:
                    item = self.queue.popleft() if self.queue else None
                    done = not self.accepting and not self.reserved and item is None
                if done:
                    break
                if item is None:
                    self.event.wait()
                    continue
                sequence, batch, size = item
                started = time.perf_counter()
                path = self.segments.append(sequence, batch)
                with self.lock:
                    self.committed += 1
                    self.buffered -= 1
                    self.buffer_bytes -= size
                    self.last_path, self.write_seconds = path, time.perf_counter() - started
                self.wake()
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.pause_reason = "记录已暂停：二进制写入失败 · " + str(exc)
                self.accepting = False
        finally:
            try:
                self.segments.close()
            except Exception as exc:
                with self.lock:
                    self.error = self.error or str(exc)
                    self.pause_reason = "记录已暂停：二进制文件收尾失败 · " + str(exc)
                    self.accepting = False
            with self.lock:
                self.binary_done = True
            self.csv_event.set()
            self.wake()

    def _csv_loop(self) -> None:
        process: subprocess.Popen | None = None
        target_count = 0
        next_at = time.monotonic() + self.csv_interval
        try:
            while True:
                self.csv_event.clear()
                if process is not None and process.poll() is not None:
                    completed, process = process, None
                    try:
                        status_path = self.raw_folder / "csv-status.json"
                        status = json.loads(status_path.read_text(encoding="utf-8"))
                    finally:
                        _CSV_SLOTS.release()
                    with self.lock:
                        self.converted = int(status.get("converted", self.converted))
                        self.csv_seconds = float(status.get("seconds", 0))
                        self.csv_lag_seconds = float(status.get("csv_lag_seconds", 0))
                        self.csv_error = status.get("error", "") or (
                            f"CSV process exited {completed.returncode}" if completed.returncode else ""
                        )
                        if not self.csv_error and self.converted < target_count:
                            self.csv_error = "CSV conversion did not reach the committed target"
                    next_at = time.monotonic() + self.csv_interval
                    self.wake()
                with self.lock:
                    done, committed, converted, error = (
                        self.binary_done,
                        self.committed,
                        self.converted,
                        self.csv_error,
                    )
                if (
                    process is None
                    and committed > converted
                    and not error
                    and (done or time.monotonic() >= next_at)
                    and _CSV_SLOTS.acquire(blocking=False)
                ):
                    try:
                        from lab_control.launcher import worker_command

                        process = subprocess.Popen(
                            worker_command(self.folder),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        target_count = committed
                    except Exception:
                        _CSV_SLOTS.release()
                        raise
                if done and process is None and (committed <= converted or error):
                    break
                self.csv_event.wait(0.1 if process else min(0.25, max(0.01, next_at - time.monotonic())))
            if self.folder.exists():
                _atomic_json(
                    self.raw_folder / "summary.json",
                    self.snapshot() | {"version": 1, "segments": self.segments.paths},
                )
                self._describe()
            self.finished.set_result(list(self.segments.paths))
        except Exception as exc:
            with self.lock:
                self.csv_error = str(exc)
            # Preserve binary writing even if conversion cannot start.
            self.thread.join()
            if self.folder.exists():
                try:
                    _atomic_json(
                        self.raw_folder / "summary.json",
                        self.snapshot() | {"version": 1, "segments": self.segments.paths},
                    )
                    self._describe()
                except OSError:
                    pass  # The in-memory error/counts remain available if the disk itself failed.
            self.finished.set_result(list(self.segments.paths))
        finally:
            self.wake()

    def _describe(self) -> None:
        if self.context.layout_version == 2:
            from lab_control.storage.analysis import write_description

            error = self.error or self.csv_error
            write_description(
                self.folder, "保存未完整完成" if error else "原始数据已保存；分析 CSV 已完成", error=error
            )


def csv_worker(folder: Path) -> int:
    target = folder.resolve()
    if not target.is_dir():
        return 2
    try:
        result = convert_csv(target)
    except Exception as failure:
        result = {"error": str(failure)}
        try:
            if manifest(target).get("layout_version", 1) == 2:
                from lab_control.storage.analysis import write_description

                write_description(target, "分析 CSV 生成失败", error=str(failure))
        except (OSError, ValueError):
            pass
    try:
        _atomic_json(raw_folder(target) / "csv-status.json", result)
    except OSError:
        return 2
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--csv":
        raise SystemExit("Usage: python -m lab_control.storage.recording --csv RECORDING_FOLDER")
    raise SystemExit(csv_worker(Path(sys.argv[2])))
