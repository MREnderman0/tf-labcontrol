"""Binary and CSV worker responsibilities, coordinated by the Recording facade."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from lab_control.storage import recording as format

if TYPE_CHECKING:
    from lab_control.storage.recording import Recording


class BinaryRecordingWriter:
    def __init__(self, recording: Recording) -> None:
        self.recording = recording

    def run(self) -> None:
        recording = self.recording
        try:
            while True:
                recording.event.clear()
                with recording.lock:
                    item = recording.queue.popleft() if recording.queue else None
                    done = not recording.accepting and not recording.reserved and item is None
                if done:
                    break
                if item is None:
                    recording.event.wait()
                    continue
                sequence, batch, size = item
                started = time.perf_counter()
                path = recording.segments.append(sequence, batch)
                with recording.lock:
                    recording.committed += 1
                    recording.buffered -= 1
                    recording.buffer_bytes -= size
                    recording.last_path, recording.write_seconds = path, time.perf_counter() - started
                recording.wake()
        except Exception as exc:
            with recording.lock:
                recording.error = str(exc)
                recording.pause_reason = "记录已暂停：二进制写入失败 · " + str(exc)
                recording.accepting = False
                # Counts retain uncommitted evidence; completed failures need not
                # retain up to 128 MiB of arrays for the lifetime of the runtime.
                recording.queue.clear()
                recording.buffer_bytes = 0
        finally:
            try:
                recording.segments.close()
            except Exception as exc:
                with recording.lock:
                    recording.error = recording.error or str(exc)
                    recording.pause_reason = "记录已暂停：二进制文件收尾失败 · " + str(exc)
                    recording.accepting = False
            with recording.lock:
                recording.binary_done = True
            recording.csv_event.set()
            recording.wake()


class CsvConversionSupervisor:
    def __init__(self, recording: Recording) -> None:
        self.recording = recording

    def run(self) -> None:
        recording = self.recording
        process: format.subprocess.Popen | None = None
        target_count = 0
        next_at = time.monotonic() + recording.csv_interval
        try:
            while True:
                recording.csv_event.clear()
                if process is not None and process.poll() is not None:
                    completed, process = process, None
                    try:
                        status_path = recording.raw_folder / "csv-status.json"
                        status = json.loads(status_path.read_text(encoding="utf-8"))
                    finally:
                        format._CSV_SLOTS.release()
                    with recording.lock:
                        recording.converted = int(status.get("converted", recording.converted))
                        recording.csv_seconds = float(status.get("seconds", 0))
                        recording.csv_lag_seconds = float(status.get("csv_lag_seconds", 0))
                        recording.csv_error = status.get("error", "") or (
                            f"CSV process exited {completed.returncode}" if completed.returncode else ""
                        )
                        if not recording.csv_error and recording.converted < target_count:
                            recording.csv_error = "CSV conversion did not reach the committed target"
                    next_at = time.monotonic() + recording.csv_interval
                    recording.wake()
                with recording.lock:
                    done, committed, converted, error = (
                        recording.binary_done,
                        recording.committed,
                        recording.converted,
                        recording.csv_error,
                    )
                if (
                    process is None
                    and committed > converted
                    and not error
                    and (done or time.monotonic() >= next_at)
                    and format._CSV_SLOTS.acquire(blocking=False)
                ):
                    try:
                        from lab_control.launcher import worker_command

                        process = format.subprocess.Popen(
                            worker_command(recording.folder),
                            stdin=format.subprocess.DEVNULL,
                            stdout=format.subprocess.DEVNULL,
                            stderr=format.subprocess.DEVNULL,
                            creationflags=getattr(format.subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        target_count = committed
                    except Exception:
                        format._CSV_SLOTS.release()
                        raise
                if done and process is None and (committed <= converted or error):
                    break
                recording.csv_event.wait(0.1 if process else min(0.25, max(0.01, next_at - time.monotonic())))
            if recording.folder.exists():
                format._atomic_json(
                    recording.raw_folder / "summary.json",
                    recording.snapshot() | {"version": 1, "segments": recording.segments.paths},
                )
                recording._describe()
        except Exception as exc:
            with recording.lock:
                recording.csv_error = str(exc)
            # Preserve binary writing even if conversion cannot start.
            recording.thread.join()
            if recording.folder.exists():
                try:
                    format._atomic_json(
                        recording.raw_folder / "summary.json",
                        recording.snapshot() | {"version": 1, "segments": recording.segments.paths},
                    )
                    recording._describe()
                except Exception:
                    pass  # The in-memory error/counts remain available if the disk itself failed.
        finally:
            # Even a secondary summary/description failure must settle waiters.
            if not recording.finished.done():
                recording.finished.set_result(list(recording.segments.paths))
            recording.wake()
