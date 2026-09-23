"""Shared live monitoring, scheduled through the existing per-device session workers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from lab_control.application.sessions import SessionManager
from lab_control.application.settings_sync import SettingsSync
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    MeasurementBatch,
    Quality,
    ValidationError,
    utc_now,
)
from lab_control.storage.repository import Repository


@dataclass(frozen=True)
class MonitorFrame:
    sequence: int
    configuration_version: int
    request: AcquisitionRequest
    batch: MeasurementBatch


@dataclass
class MonitorTask:
    requests: tuple[AcquisitionRequest, ...]
    mode: str
    interval: float
    running: bool = True
    recording: bool = False
    pending: Future[Any] | None = None
    pending_request: AcquisitionRequest | None = None
    write_pending: Future[Any] | None = None
    next_at: float = 0
    index: int = 0
    sequence: int = 0
    frames: dict[tuple[str, str], MonitorFrame] = field(default_factory=dict)
    error: str = ""
    last_path: str = ""
    stop_future: Future[Any] | None = None
    record_session: str = ""
    once: bool = False
    pending_record_session: str = ""
    pending_record_mode: str = "electronic"


def merge_requests(requests: tuple[AcquisitionRequest, ...]) -> tuple[AcquisitionRequest, ...]:
    grouped: dict[tuple[Any, ...], AcquisitionRequest] = {}
    for request in requests:
        request.validate()
        key = (request.kind, request.quantity)
        previous = grouped.get(key)
        channels = tuple(sorted(set(request.channels + (previous.channels if previous else ()))))
        # Settings are separate channel queries; waveform and scalar instruments can merge channels.
        if request.kind == "status":
            for channel in channels:
                grouped[(*key, channel)] = replace(request, channels=(channel,))
        else:
            grouped[key] = replace(request, channels=channels)
    return tuple(grouped.values())


class MonitoringService:
    def __init__(self, sessions: SessionManager, repository: Repository) -> None:
        self.sessions, self.repository = sessions, repository
        self.lock = threading.RLock()
        self.tasks: dict[str, MonitorTask] = {}
        self.wake = threading.Event()
        self.closing = False
        self.settings_sync = SettingsSync(sessions)
        self.writer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="monitor-export")
        self.unsubscribe = sessions.subscribe(self._event)
        self.thread = threading.Thread(target=self._loop, name="monitor-scheduler", daemon=True)
        self.thread.start()

    def enable_settings_sync(self, enabled: bool = True) -> None:
        with self.lock:
            self.settings_sync.enabled = enabled and not self.closing
        self.wake.set()

    def focus_settings(self, device_id: str, advanced: tuple[tuple[int, str], ...] = ()) -> None:
        with self.lock:
            self.settings_sync.set_focus(device_id, advanced)
        self.wake.set()

    def refresh_settings(self, device_id: str) -> None:
        with self.lock:
            self.settings_sync.request(device_id)
        self.wake.set()

    def settings_status(self, device_id: str, keys: tuple[tuple[int, str], ...]) -> tuple[str, str]:
        with self.lock:
            return self.settings_sync.status(device_id, keys)

    def start(
        self,
        device_id: str,
        requests: tuple[AcquisitionRequest, ...],
        *,
        interval: float = 0.5,
        mode: str = "electronic",
        once: bool = False,
    ) -> None:
        if not requests or not 0.05 <= interval <= 60 or mode not in {"electronic", "optical"}:
            raise ValidationError("Choose data sources, a 0.05..60 s interval and a valid mode")
        merged = merge_requests(requests)
        with self.lock:
            if self.closing:
                raise DeviceError("Monitoring is closing")
            task = self.tasks.get(device_id)
            if task and not task.running and (task.pending or task.write_pending):
                raise DeviceError("Wait until the current monitoring batch has stopped", "task_busy")
            if task and task.running and task.mode != mode:
                raise DeviceError("Stop monitoring before assigning another recording mode", "task_busy")
            if task and task.running and once:
                raise DeviceError("Monitoring is already running", "task_busy")
            self.sessions.set_monitoring(device_id, True, bool(task and task.recording))
            if task and task.running:
                task.requests = merge_requests((*task.requests, *merged))
                task.interval = interval
            else:
                self.tasks[device_id] = MonitorTask(merged, mode, interval, once=once)
            self.repository.audit("monitor_started", device_id=device_id, requests=merged, mode=mode)
        self.wake.set()

    def apply(self, device_id: str, command: DeviceCommand) -> Future[Any]:
        future = self.sessions.submit(device_id, "apply", command)
        self.wake.set()
        return future

    def export(self, frame: MonitorFrame, mode: str) -> Future[Any]:
        with self.lock:
            if self.closing:
                raise DeviceError("Monitoring is closing")
            return self.writer.submit(self.repository.export, frame.batch, mode)

    def stop(self, device_id: str) -> Future[Any]:
        with self.lock:
            task = self.tasks.get(device_id)
            result: Future[Any] = Future()
            if not task:
                result.set_result(None)
                return result
            if task.stop_future and not task.stop_future.done():
                return task.stop_future
            task.running = False
            task.stop_future = result
        self.wake.set()
        return result

    def set_recording(self, device_id: str, enabled: bool, *, mode: str | None = None) -> None:
        with self.lock:
            task = self.tasks.get(device_id)
            if not task or not task.running:
                raise DeviceError("Start monitoring before recording", "task_busy")
            if mode is not None and mode not in {"electronic", "optical"}:
                raise ValidationError("Invalid recording mode")
            if enabled and mode and task.mode != mode:
                if task.recording or task.pending_record_session or task.write_pending:
                    raise DeviceError("Wait for the previous recording to finish", "task_busy")
                task.mode = mode
            task.recording = enabled
            if enabled:
                task.record_session = utc_now()
            self.sessions.set_monitoring(
                device_id, True, bool(enabled or task.pending_record_session or task.write_pending)
            )
            self.repository.audit(
                "monitor_recording",
                device_id=device_id,
                enabled=enabled,
                mode=task.mode,
                record_session=task.record_session,
            )

    def latest(self, device_id: str, kind: str, quantity: str = "", channel: int = 1) -> MonitorFrame | None:
        with self.lock:
            task = self.tasks.get(device_id)
            if not task:
                return None
            key = (kind, quantity + (f":{channel}" if kind == "status" else ""))
            frame = task.frames.get(key)
        if frame:
            snapshot = next((s for s in self.sessions.snapshots() if s["stableDeviceId"] == device_id), {})
            if frame.configuration_version < snapshot.get("configurationVersion", 0):
                return None
        return frame

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return {
                device_id: {
                    "running": task.running,
                    "recording": task.recording,
                    "pending": bool(task.pending or task.write_pending),
                    "error": task.error,
                    "sequence": task.sequence,
                    "last_path": task.last_path,
                    "mode": task.mode,
                    "interval": task.interval,
                }
                for device_id, task in self.tasks.items()
            }

    def _event(self, event: dict[str, Any]) -> None:
        if event["event"] == "result" and event.get("operation") == "apply":
            result = event["result"]
            if result.outcome != "success":
                self._pause(event["device_id"], result.error or result.outcome)
        elif event["event"] == "error":
            self._pause(event["device_id"], event.get("error", "Device operation failed"))

    def _pause(self, device_id: str, error: str) -> None:
        with self.lock:
            task = self.tasks.get(device_id)
            if task:
                task.running, task.error = False, error
                self.repository.audit("monitor_paused", device_id=device_id, error=error)
        self.wake.set()

    def _finish_batch(self, device_id: str, task: MonitorTask) -> None:
        future, request = task.pending, task.pending_request
        if future is None or request is None:
            return
        task.pending = None
        record_session, task.pending_record_session = task.pending_record_session, ""
        batch: MeasurementBatch = future.result()
        good = all(m.quality in {Quality.GOOD, Quality.SIMULATED} for m in batch.measurements)
        if record_session:
            batch = replace(
                batch,
                configuration={
                    **batch.configuration,
                    "monitor_record_session": record_session,
                    "monitor_request": request,
                },
            )
            task.write_pending = self.writer.submit(self.repository.export, batch, task.pending_record_mode)
        if not good:
            raise DeviceError(
                "; ".join(
                    m.metadata.get("error", m.quality.value)
                    for m in batch.measurements
                    if m.quality not in {Quality.GOOD, Quality.SIMULATED}
                )
            )
        task.sequence += 1
        key = (
            request.kind,
            request.quantity + (f":{request.channels[0]}" if request.kind == "status" else ""),
        )
        task.frames[key] = MonitorFrame(
            task.sequence, int(batch.configuration.get("_session_configuration_version", 0)), request, batch
        )
        task.next_at = time.monotonic() + task.interval
        if task.once:
            task.running = False

    def _tick(self) -> None:
        snapshots = {s["stableDeviceId"]: s for s in self.sessions.snapshots()}
        for device_id, task in tuple(self.tasks.items()):
            try:
                if task.write_pending and task.write_pending.done():
                    writing, task.write_pending = task.write_pending, None
                    task.last_path = str(writing.result())
                if task.pending and task.pending.done():
                    self._finish_batch(device_id, task)
                state = snapshots.get(device_id)
                if not state or state["connectionState"] in {"error", "degraded"}:
                    task.running = False
                    task.error = task.error or (state or {}).get("lastError", "Device disconnected")
                if state and task.running:
                    self.sessions.set_monitoring(
                        device_id,
                        True,
                        bool(task.recording or task.pending_record_session or task.write_pending),
                    )
                if not task.running:
                    if not task.pending and not task.write_pending:
                        if state:
                            self.sessions.set_monitoring(device_id, False)
                        task.recording = False
                        if task.stop_future and not task.stop_future.done():
                            task.stop_future.set_result(None)
                    continue
                if (
                    not state
                    or task.pending
                    or task.write_pending
                    or state["pending"]
                    or time.monotonic() < task.next_at
                ):
                    continue
                request = task.requests[task.index % len(task.requests)]
                future = self.sessions.try_submit_idle(
                    device_id, "observe" if request.kind == "status" else "acquire", request
                )
                if future is not None:
                    task.index += 1
                    task.pending_request = request
                    task.pending_record_session = task.record_session if task.recording else ""
                    task.pending_record_mode = task.mode
                    task.pending = future
            except Exception as exc:
                self._pause(device_id, str(exc))
        # Monitoring gets first refusal on idle workers; settings polling never queues behind it.
        if not self.closing:
            self.settings_sync.tick({s["stableDeviceId"]: s for s in self.sessions.snapshots()})

    def _loop(self) -> None:
        while True:
            with self.lock:
                self._tick()
                if self.closing and all(not t.pending and not t.write_pending for t in self.tasks.values()):
                    break
            self.wake.wait(0.02)
            self.wake.clear()

    def close(self) -> None:
        with self.lock:
            self.closing = True
            self.settings_sync.stop()
            for task in self.tasks.values():
                task.running = False
        self.wake.set()
        self.thread.join()
        self.unsubscribe()
        self.writer.shutdown(wait=True)
