"""Shared live monitoring, scheduled through the existing per-device session workers."""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Callable
from uuid import uuid4

from lab_control.application.acquisition_pipeline import (
    validate_cadence,
    validate_recorded_batch,
)
from lab_control.application.acquisition_recovery import RECOVERABLE_CODES
from lab_control.application.frame_exports import FrameExportService
from lab_control.application.monitor_recovery import MonitorRecovery
from lab_control.application.monitor_scheduler import MonitorScheduler
from lab_control.application.monitoring_state import CaptureSave as CaptureSave
from lab_control.application.monitoring_state import MonitorFrame as MonitorFrame
from lab_control.application.monitoring_state import MonitorTask as MonitorTask
from lab_control.application.power_window import PowerWindowSnapshot
from lab_control.application.recordings import retain_recording
from lab_control.application.sessions import SessionManager
from lab_control.application.settings_sync import SettingsSync
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    MeasurementBatch,
    ValidationError,
)
from lab_control.domain.workspace import GENERAL, WORKSPACE_MODES
from lab_control.storage.paths import BatchSaveContext, SaveContext
from lab_control.storage.recording import Recording
from lab_control.storage.repository import Repository


def merge_requests(requests: tuple[AcquisitionRequest, ...]) -> tuple[AcquisitionRequest, ...]:
    grouped: dict[tuple[Any, ...], AcquisitionRequest] = {}
    for request in requests:
        request.validate()
        key = (request.kind, request.quantity)
        previous = grouped.get(key)
        if previous and previous.waveform_extent != request.waveform_extent:
            raise ValidationError("Cannot merge different waveform extents")
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
        self.audit = sessions.audit
        self.lock = threading.RLock()
        self.tasks: dict[str, MonitorTask] = {}
        self.stop_revisions: dict[str, int] = {}
        self.capture_saves: dict[str, CaptureSave] = {}
        self.display_exports: dict[str, Future[Any]] = {}
        self.save_seconds: dict[str, float] = {}
        self.save_report_at: dict[str, float] = {}
        self.recordings: list[Recording] = []
        self.recording_results: deque[dict[str, Any]] = deque(maxlen=32)
        self._recording_result_id = 0
        self.preparer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="record-prepare")
        self.frame_listeners: list[Callable[[str], None]] = []
        self.wake = threading.Event()
        self.closing = False
        self._closed = False
        self._close_lock = threading.Lock()
        self.settings_sync = SettingsSync(sessions, self.wake.set)
        self.writer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="monitor-export")
        self.unsubscribe = sessions.subscribe(self._event)
        self.exports = FrameExportService(self)
        self.scheduler = MonitorScheduler(self)
        self.recovery = MonitorRecovery(self)
        self.thread = threading.Thread(target=self._loop, name="monitor-scheduler", daemon=True)
        self.thread.start()

    def subscribe_frames(self, callback: Callable[[str], None]) -> Callable[[], None]:
        with self.lock:
            self.frame_listeners.append(callback)

        def unsubscribe() -> None:
            with self.lock:
                if callback in self.frame_listeners:
                    self.frame_listeners.remove(callback)

        return unsubscribe

    def displayed(self, device_id: str, frame: MonitorFrame) -> None:
        with self.lock:
            task = self.tasks.get(device_id)
            if task:
                task.display_seconds = max(0, time.perf_counter() - frame.received_at)
                if task.timings and task.timings[-1]["sequence"] == frame.sequence:
                    task.timings[-1]["display_seconds"] = task.display_seconds

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
        record_interval: float | None = 0.05,
        mode: str = GENERAL,
        once: bool = False,
        cadence: str | None = None,
        csv_interval: float = 5,
        recording: bool = False,
        background: bool = False,
        batch: BatchSaveContext | None = None,
    ) -> Future[Any] | None:
        preparation = None
        if batch is not None and not recording:
            raise ValidationError("A batch save context requires recording")
        if not requests or not 0.05 <= interval <= 60 or mode not in WORKSPACE_MODES:
            raise ValidationError("Choose data sources, a 0.05..60 s interval and a valid mode")
        self.validate_intervals(interval, record_interval)
        validate_cadence(interval, cadence)
        Recording.validate_csv_interval(csv_interval)
        merged = merge_requests(requests)
        if any(request.waveform_extent == "memory" for request in merged):
            raise ValidationError("Use the explicit memory waveform operation outside continuous recording")
        with self.lock:
            if self.closing:
                raise DeviceError("Monitoring is closing")
            task = self.tasks.get(device_id)
            if (
                task
                and not task.running
                and (
                    task.pending
                    or task.write_pending
                    or task.recording_preparation
                    or (task.recorder and not task.recorder.binary_done)
                )
            ):
                raise DeviceError("Wait until the current monitoring batch has stopped", "task_busy")
            if task and task.running and task.mode != mode:
                raise DeviceError("Stop monitoring before assigning another recording mode", "task_busy")
            if task and task.running and once:
                raise DeviceError("Monitoring is already running", "task_busy")
            if task and task.running:
                previous = (
                    task.requests,
                    task.request_revision,
                    task.interval,
                    task.record_interval,
                    task.cadence,
                    task.csv_interval,
                    task.recording,
                )
                task.requests = merge_requests((*task.requests, *merged))
                task.request_revision += 1
                task.interval = interval
                task.record_interval = record_interval
                task.cadence = cadence or "interval"
                task.csv_interval = csv_interval
                revision = task.request_revision

                def restore_task() -> None:
                    # A later user edit owns its new request revision.
                    if task.request_revision == revision:
                        (
                            task.requests,
                            task.request_revision,
                            task.interval,
                            task.record_interval,
                            task.cadence,
                            task.csv_interval,
                            task.recording,
                        ) = previous

                try:
                    if recording and not task.recording:
                        preparation = self._begin_recording_locked(
                            device_id, task, mode=mode, on_failure=restore_task, batch=batch
                        )
                    else:
                        self.sessions.set_monitoring(
                            device_id, True, bool(task.recording or task.recording_preparation)
                        )
                except Exception:
                    (
                        task.requests,
                        task.request_revision,
                        task.interval,
                        task.record_interval,
                        task.cadence,
                        task.csv_interval,
                        task.recording,
                    ) = previous
                    try:
                        self.sessions.set_monitoring(device_id, True, bool(task.recording))
                    except DeviceError:
                        with self.sessions.lock:
                            session = self.sessions.sessions.get(device_id)
                            if session is not None:
                                session.recording = bool(task.recording)
                    raise
            else:
                candidate = MonitorTask(merged, mode, interval, record_interval, once=once)
                candidate.cadence = cadence or "interval"
                candidate.csv_interval = csv_interval
                self.tasks[device_id] = candidate
                try:
                    if recording:
                        preparation = self._begin_recording_locked(
                            device_id, candidate, mode=mode, new_task=True, batch=batch
                        )
                    else:
                        self.sessions.set_monitoring(device_id, True, False)
                except Exception:
                    self.tasks.pop(device_id, None)
                    self.sessions.set_monitoring(device_id, False)
                    raise
            self.audit.try_record("monitor_started", device_id=device_id, requests=merged, mode=mode)
        self.wake.set()
        if preparation is not None and not background:
            preparation.result()
        return preparation

    def replace_requests(self, device_id: str, requests: tuple[AcquisitionRequest, ...]) -> None:
        """Replace a running task's acquisition set without stopping monitoring.

        A request already in flight is allowed to finish at the session worker, but
        its result is discarded when its revision no longer matches the task. This
        keeps adapters that cannot interrupt a single read safe while making the
        next scheduled frame use the new channel set.
        """
        merged = merge_requests(requests) if requests else ()
        if any(request.waveform_extent == "memory" for request in merged):
            raise ValidationError("Use the explicit memory waveform operation outside continuous recording")
        with self.lock:
            task = self.tasks.get(device_id)
            if not task or not task.running:
                raise DeviceError("Monitoring must be running before refreshing requests", "task_busy")
            previous_channels = {
                ch
                for request in task.requests
                if request.kind == "scalar" and request.quantity == "power"
                for ch in request.channels
            }
            next_channels = {
                ch
                for request in merged
                if request.kind == "scalar" and request.quantity == "power"
                for ch in request.channels
            }
            task.power_window.mark_gap(previous_channels ^ next_channels)
            task.requests = merged
            task.request_revision += 1
            task.frames.clear()
            task.frame_history.clear()
            task.notice = ""
            task.next_at = 0
            task.index = 0
            self.audit.try_record(
                "monitor_requests_replaced",
                device_id=device_id,
                requests=merged,
                request_revision=task.request_revision,
            )
        self.wake.set()

    def _begin_recording_locked(
        self,
        device_id: str,
        task: MonitorTask,
        *,
        mode: str | None = None,
        new_task: bool = False,
        on_failure: Callable[[], None] | None = None,
        batch: BatchSaveContext | None = None,
    ) -> Future[Any] | None:
        """Reserve a bounded preparation job; filesystem work never holds the monitor lock."""
        if task.recording:
            return None
        if task.recording_preparation is not None:
            return task.recording_preparation
        # At most one job per active device, including queued jobs; repeated
        # clicks reuse its Future. The executor limits concurrent filesystem I/O.
        if task.recorder and not task.recorder.binary_done:
            raise DeviceError("Wait for the previous recording to drain", "task_busy")
        next_mode = mode or task.mode
        if next_mode not in WORKSPACE_MODES:
            raise ValidationError("Invalid recording mode")
        previous_lock = bool(
            task.pending_record_session
            or task.write_pending
            or (task.recorder and not task.recorder.binary_done)
        )
        self.sessions.set_monitoring(device_id, True, True)
        try:
            context = self.repository.new_save_context(
                next_mode, "continuous", device_id=device_id, batch=batch
            )
        except Exception:
            self._restore_recording_lock(device_id, previous_lock)
            raise
        result: Future[Any] = Future()
        result.set_running_or_notify_cancel()
        task.recording_preparation = result
        task.recording_cancelled = False
        task.recording_notice = "正在准备记录目录…"
        csv_interval = task.csv_interval

        def require_active() -> None:
            session = self.sessions.sessions.get(device_id)
            if (
                self.closing
                or not task.running
                or task.recording_cancelled
                or self.tasks.get(device_id) is not task
                or session is None
                or session.closing
                or session.state not in {"connected", "busy"}
            ):
                raise DeviceError("记录准备已取消；未开始采集记录", "recording_cancelled")

        def prepare() -> None:
            recorder = None
            error = None
            try:
                with self.lock:
                    require_active()
                recorder = self.repository.create_recording(
                    next_mode, context=context, csv_interval=csv_interval, wake=self.wake.set
                )
                with self.lock:
                    require_active()
                    task.mode = next_mode
                    task.acquisition_context_id = uuid4().hex
                    task.recorder = recorder
                    self._track_recording(device_id, recorder)
                    task.recording_notice = ""
                    task.record_session = context.started_utc
                    task.last_record_at = task.next_record_at = 0
                    task.recording = True
            except Exception as exc:
                error = exc
                if recorder is not None:
                    # Cancellation may arrive during filesystem work. Drain the
                    # empty recorder outside the monitor lock before releasing guards.
                    self._track_recording(device_id, recorder)
                    try:
                        recorder.finish().result()
                    except Exception as cleanup_error:
                        exc.add_note(f"Recording cleanup failed: {cleanup_error}")
                with self.lock:
                    if new_task:
                        task.running = False
                    if on_failure is not None:
                        on_failure()
                    task.recording_notice = str(exc)
                    self._restore_recording_lock(device_id, previous_lock)
            finally:
                with self.lock:
                    task.recording_preparation = None
                if error is None:
                    result.set_result(None)
                else:
                    result.set_exception(error)
                self.wake.set()

        try:
            self.preparer.submit(prepare)
        except Exception:
            task.recording_preparation = None
            self._restore_recording_lock(device_id, previous_lock)
            raise
        return result

    def _restore_recording_lock(self, device_id: str, previous: bool) -> None:
        with self.sessions.lock:
            session = self.sessions.sessions.get(device_id)
            if session is not None:
                session.recording = previous

    def _track_recording(self, device_id: str, recorder: Recording) -> None:
        retain_recording(self.recordings, recorder, self.lock)
        recorder.finished.add_done_callback(lambda _future: self._recording_completed(device_id, recorder))

    def _recording_completed(self, device_id: str, recorder: Recording) -> None:
        state = recorder.snapshot()
        error = state.get("error") or state.get("csv_error") or ""
        with self.lock:
            self._recording_result_id += 1
            result = {
                "id": self._recording_result_id,
                "device_id": device_id,
                "folder": str(recorder.folder),
                "error": error,
                "committed": state.get("committed", 0),
            }
            self.recording_results.append(result)
        self.audit.try_record("recording_completed", **result)
        self.wake.set()

    def recording_results_since(self, cursor: int) -> tuple[dict[str, Any], ...]:
        with self.lock:
            return tuple(dict(r) for r in self.recording_results if r["id"] > cursor)

    @staticmethod
    def validate_intervals(interval: float, record_interval: float | None) -> None:
        if (
            any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in (interval, record_interval)
                if value is not None
            )
            or interval is None
            or not 0.05 <= interval <= 60
            or (record_interval is not None and not 0.05 <= record_interval <= 86400)
        ):
            raise ValidationError("Refresh interval must be 0.05..60 s; save interval must be 0.05..86400 s")

    def set_intervals(
        self,
        device_id: str,
        interval: float,
        record_interval: float | None,
        *,
        cadence: str | None = None,
        csv_interval: float | None = None,
    ) -> None:
        """Local scheduling only; does not start acquisition, recording, or device I/O."""
        self.validate_intervals(interval, record_interval)
        validate_cadence(interval, cadence)
        if csv_interval is not None:
            Recording.validate_csv_interval(csv_interval)
        with self.lock:
            task = self.tasks.get(device_id)
            if task:
                task.interval, task.record_interval = interval, record_interval
                if cadence is not None:
                    task.cadence = cadence
                if csv_interval is not None:
                    task.csv_interval = csv_interval
                    if task.recorder:
                        task.recorder.csv_interval = csv_interval
                task.next_at = 0 if task.cadence == "fastest" else task.last_started_at + interval
                task.next_record_at = (
                    task.last_record_at + record_interval
                    if task.last_record_at and record_interval is not None
                    else 0
                )
        self.wake.set()

    def apply(self, device_id: str, command: DeviceCommand) -> Future[Any]:
        future = self.sessions.submit(device_id, "apply", command)
        self.wake.set()
        return future

    def export(self, frame: MonitorFrame, mode: str) -> Future[Any]:
        return self.exports.export(frame, mode)

    @staticmethod
    def _validate_saved_batch(batch: MeasurementBatch) -> None:
        validate_recorded_batch(batch)

    def export_displayed_frame(self, device_id: str, frame: MonitorFrame, mode: str) -> Future[Any]:
        return self.exports.export_displayed_frame(device_id, frame, mode)

    def _export(
        self, device_id: str, batch: MeasurementBatch, mode: str, context: SaveContext | None = None
    ) -> Any:
        return self.exports.write(device_id, batch, mode, context)

    def capture_and_export(self, device_id: str, request: AcquisitionRequest, mode: str) -> Future[Any]:
        return self.exports.capture_and_export(device_id, request, mode)

    def read_memory_waveform(self, device_id: str, request: AcquisitionRequest) -> Future[Any]:
        return self.exports.read_memory_waveform(device_id, request)

    def _finish_captures(self) -> None:
        return self.exports.finish_captures()

    def stop_revision(self, device_id: str) -> int:
        with self.lock:
            return self.stop_revisions.get(device_id, 0)

    def stop(self, device_id: str, *, preserve_intent: bool = False) -> Future[Any]:
        with self.lock:
            if not preserve_intent:
                self.stop_revisions[device_id] = self.stop_revisions.get(device_id, 0) + 1
            task = self.tasks.get(device_id)
            result: Future[Any] = Future()
            if not task:
                result.set_result(None)
                return result
            if task.stop_future and not task.stop_future.done():
                return task.stop_future
            task.running = False
            self.recovery.cancel(task)
            task.recording = False
            task.recording_cancelled = True
            if not task.configuration_wait_expired:
                task.configuration_wait_deadline, task.notice = 0, ""
            task.stop_future = result
            if task.recorder:
                # CSV may finish after a new monitoring task replaces this one.
                # Its stop waiter must not depend on that task staying in tasks.
                def recorded(_future: Future[Any]) -> None:
                    with self.lock:
                        if self.tasks.get(device_id) is not task and not result.done():
                            result.set_result(None)
                    self.wake.set()

                task.recorder.finished.add_done_callback(recorded)
        self.wake.set()
        return result

    def set_recording(
        self, device_id: str, enabled: bool, *, mode: str | None = None, background: bool = False
    ) -> Future[Any] | None:
        preparation = None
        with self.lock:
            if self.closing:
                raise DeviceError("Monitoring is closing")
            if enabled and device_id in self.capture_saves and self.capture_saves[device_id].display_request:
                raise DeviceError("请等待完整波形读取完成", "task_busy")
            task = self.tasks.get(device_id)
            if not task or not task.running:
                raise DeviceError("Start monitoring before recording", "task_busy")
            if enabled and not task.recording and task.recorder and not task.recorder.binary_done:
                raise DeviceError("Wait for the previous recording to drain", "task_busy")
            if mode is not None and mode not in WORKSPACE_MODES:
                raise ValidationError("Invalid recording mode")
            if enabled and mode and task.mode != mode:
                if task.recording or task.pending_record_session or task.write_pending:
                    raise DeviceError("Wait for the previous recording to finish", "task_busy")
            if enabled:
                if not task.recording:
                    preparation = self._begin_recording_locked(device_id, task, mode=mode)
            else:
                task.recording = False
                task.recording_cancelled = True
                self.sessions.set_monitoring(
                    device_id,
                    True,
                    bool(
                        task.recording_preparation
                        or task.pending_record_session
                        or task.write_pending
                        or (task.recorder and not task.recorder.binary_done)
                    ),
                )
            self.audit.try_record(
                "monitor_recording",
                device_id=device_id,
                enabled=enabled,
                mode=task.mode,
                record_session=task.record_session,
            )
        self.wake.set()
        if preparation is not None and not background:
            preparation.result()
        return preparation

    def latest(self, device_id: str, kind: str, quantity: str = "", channel: int = 1) -> MonitorFrame | None:
        with self.lock:
            task = self.tasks.get(device_id)
            if not task:
                return None
            key = (kind, quantity + (f":{channel}" if kind == "status" else ""))
            frame = task.frames.get(key)
        if frame:
            snapshot = next((s for s in self.sessions.snapshots() if s["stableDeviceId"] == device_id), {})
            if frame.session_id != snapshot.get("sessionId") or frame.configuration_version != snapshot.get(
                "configurationVersion"
            ):
                return None
        return frame

    def recent(
        self, device_id: str, kind: str, quantity: str = "", channel: int = 1
    ) -> tuple[MonitorFrame, ...]:
        """Return the bounded replay window for a live data source.

        Earlier configuration versions remain usable trend history; only a
        changed instrument session invalidates the cached frames.
        """
        with self.lock:
            task = self.tasks.get(device_id)
            if not task:
                return ()
            key = (kind, quantity + (f":{channel}" if kind == "status" else ""))
            frames = tuple(task.frame_history.get(key, ()))
        snapshot = next((s for s in self.sessions.snapshots() if s["stableDeviceId"] == device_id), {})
        session_id = snapshot.get("sessionId")
        if session_id:
            frames = tuple(frame for frame in frames if frame.session_id == session_id)
        return frames

    def power_window(self, device_id: str) -> PowerWindowSnapshot | None:
        """Read a coherent 30-minute display/statistics snapshot without instrument I/O."""
        with self.lock:
            task = self.tasks.get(device_id)
            if task is None:
                return None
            return task.power_window.snapshot(
                task.monitoring_context_id,
                time.perf_counter(),
                stopped=not task.running and not task.pending,
            )

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return {
                device_id: {
                    "running": task.running,
                    "recording": task.recording,
                    "recording_preparing": task.recording_preparation is not None,
                    "pending": bool(
                        task.pending
                        or task.write_pending
                        or task.recording_preparation
                        or task.recovery_pending
                    ),
                    "recovering": task.recovery is not None,
                    "recovery": task.recovery.snapshot() if task.recovery else {},
                    "error": task.error,
                    "sequence": task.sequence,
                    "monitoring_context_id": task.monitoring_context_id,
                    "last_path": task.last_path,
                    "mode": task.mode,
                    "interval": task.interval,
                    "cadence": task.cadence,
                    "csv_interval": task.csv_interval,
                    "record_interval": task.record_interval,
                    "frame_interval": task.frame_interval,
                    "acquisition_seconds": task.acquisition_seconds,
                    "notice": task.notice,
                    "capture_saving": device_id in self.capture_saves,
                    "display_saving": device_id in self.display_exports,
                    "save_seconds": self.save_seconds.get(device_id),
                    "recording_state": task.recorder.snapshot() if task.recorder else {},
                    "recording_notice": task.recording_notice,
                    "recording_failures": tuple(
                        dict(r) for r in self.recording_results if r["device_id"] == device_id and r["error"]
                    ),
                    "display_seconds": task.display_seconds,
                    "scheduler_seconds": task.scheduler_seconds,
                }
                for device_id, task in self.tasks.items()
            } | {
                device_id: {
                    "capture_saving": device_id in self.capture_saves,
                    "display_saving": device_id in self.display_exports,
                }
                for device_id in self.capture_saves.keys() | self.display_exports.keys()
                if device_id not in self.tasks
            }

    def _event(self, event: dict[str, Any]) -> None:
        self.wake.set()
        if event["event"] == "result" and event.get("operation") == "apply":
            result = event["result"]
            if result.outcome != "success":
                self._pause(event["device_id"], result.error or result.outcome)
        elif event["event"] == "error":
            if event.get("operation") in {"acquire", "observe"} and event.get("code") in RECOVERABLE_CODES:
                # The completed read owns classification, buffer release and recovery.
                # An unrelated settings error must still pause the target task.
                return
            self._pause(event["device_id"], event.get("error", "Device operation failed"))

    def _pause(self, device_id: str, error: str) -> None:
        with self.lock:
            task = self.tasks.get(device_id)
            if task:
                self.recovery.cancel(task)
                task.running, task.error = False, error
                task.recording = False
                self.audit.try_record("monitor_paused", device_id=device_id, error=error)
        self.wake.set()

    def _finish_batch(self, device_id: str, task: MonitorTask, session_id: str = "") -> None:
        return self.scheduler.finish_batch(device_id, task, session_id)

    def _tick(self) -> None:
        return self.scheduler.tick()

    def _loop(self) -> None:
        return self.scheduler.loop()

    def close(self) -> None:
        with self._close_lock:
            self._close_once()

    def _close_once(self) -> None:
        if self._closed:
            return
        errors = []
        with self.lock:
            self.closing = True
            try:
                self.settings_sync.stop()
            except Exception as exc:
                errors.append(exc)
            for task in self.tasks.values():
                task.running = False
                task.recording = False
                self.recovery.cancel(task)
        self.wake.set()
        for cleanup in (
            lambda: self.preparer.shutdown(wait=True),
            self.thread.join,
            self.unsubscribe,
            lambda: self.writer.shutdown(wait=True),
        ):
            try:
                cleanup()
            except Exception as exc:
                errors.append(exc)
        for recording in tuple(self.recordings):
            try:
                recording.finish().result()
            except Exception as exc:
                errors.append(exc)
        with self.lock:
            # Binary draining can outlive the scheduler's last device read.
            # Release occupancy only after it has actually completed.
            try:
                self._tick()
            except Exception as exc:
                errors.append(exc)
        self._closed = True
        if errors:
            for secondary in errors[1:]:
                errors[0].add_note(f"Additional cleanup failure: {secondary}")
            raise errors[0]
