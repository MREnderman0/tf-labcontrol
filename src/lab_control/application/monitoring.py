"""Shared live monitoring, scheduled through the existing per-device session workers."""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from uuid import uuid4

from lab_control.application.acquisition_pipeline import (
    FrozenDict,
    immutable_batch,
    validate_cadence,
    validate_displayed_batch,
    validate_recorded_batch,
)
from lab_control.application.sessions import SessionManager
from lab_control.application.settings_sync import SettingsSync
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    MeasurementBatch,
    Quality,
    ValidationError,
)
from lab_control.domain.workspace import GENERAL, WORKSPACE_MODES
from lab_control.storage.paths import SaveContext
from lab_control.storage.recording import Recording
from lab_control.storage.repository import Repository


@dataclass(frozen=True)
class MonitorFrame:
    sequence: int
    configuration_version: int
    request: AcquisitionRequest
    batch: MeasurementBatch
    received_at: float = field(default_factory=time.perf_counter)
    session_id: str = ""


@dataclass
class CaptureSave:
    read: Future[Any]
    result: Future[Any]
    mode: str
    write: Future[Any] | None = None
    display_request: AcquisitionRequest | None = None
    session_id: str = ""
    save_context: SaveContext | None = None


@dataclass
class MonitorTask:
    requests: tuple[AcquisitionRequest, ...]
    mode: str
    interval: float
    record_interval: float | None = 0.05
    running: bool = True
    recording: bool = False
    pending: Future[Any] | None = None
    pending_request: AcquisitionRequest | None = None
    request_revision: int = 0
    pending_revision: int = 0
    write_pending: Future[Any] | None = None
    next_at: float = 0
    index: int = 0
    sequence: int = 0
    frames: dict[tuple[str, str], MonitorFrame] = field(default_factory=dict)
    # Bounded replay window for scalar workspaces. The latest-frame map above
    # remains the fast path; this lets hidden pages catch up without affecting
    # acquisition or recording.
    frame_history: dict[tuple[str, str], deque[MonitorFrame]] = field(default_factory=dict)
    error: str = ""
    last_path: str = ""
    stop_future: Future[Any] | None = None
    record_session: str = ""
    acquisition_context_id: str = ""
    monitoring_context_id: str = field(default_factory=lambda: uuid4().hex)
    once: bool = False
    pending_record_session: str = ""
    pending_record_mode: str = GENERAL
    last_started_at: float = 0
    last_record_at: float = 0
    next_record_at: float = 0
    last_frame_at: float = 0
    frame_interval: float | None = None
    acquisition_seconds: float | None = None
    configuration_changes: int = 0
    configuration_wait_deadline: float = 0
    configuration_wait_expired: bool = False
    notice: str = ""
    cadence: str = "interval"
    csv_interval: float = 5
    recorder: Recording | None = None
    recording_notice: str = ""
    display_seconds: float | None = None
    scheduler_seconds: float = 0
    timings: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=120))
    timing_report_at: float = 0


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
        self.lock = threading.RLock()
        self.tasks: dict[str, MonitorTask] = {}
        self.stop_revisions: dict[str, int] = {}
        self.capture_saves: dict[str, CaptureSave] = {}
        self.display_exports: dict[str, Future[Any]] = {}
        self.save_seconds: dict[str, float] = {}
        self.save_report_at: dict[str, float] = {}
        self.recordings: list[Recording] = []
        self.frame_listeners: list[Callable[[str], None]] = []
        self.wake = threading.Event()
        self.closing = False
        self.settings_sync = SettingsSync(sessions, self.wake.set)
        self.writer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="monitor-export")
        self.unsubscribe = sessions.subscribe(self._event)
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
    ) -> None:
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
                and (task.pending or task.write_pending or (task.recorder and not task.recorder.binary_done))
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
                try:
                    if recording and not task.recording:
                        self._begin_recording_locked(device_id, task, mode=mode)
                    else:
                        self.sessions.set_monitoring(device_id, True, bool(task.recording))
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
                        self._begin_recording_locked(device_id, candidate, mode=mode)
                    else:
                        self.sessions.set_monitoring(device_id, True, False)
                except Exception:
                    self.tasks.pop(device_id, None)
                    self.sessions.set_monitoring(device_id, False)
                    raise
            self.repository.audit("monitor_started", device_id=device_id, requests=merged, mode=mode)
        self.wake.set()

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
            task.requests = merged
            task.request_revision += 1
            task.frames.clear()
            task.frame_history.clear()
            task.notice = ""
            task.next_at = 0
            task.index = 0
            self.repository.audit(
                "monitor_requests_replaced",
                device_id=device_id,
                requests=merged,
                request_revision=task.request_revision,
            )
        self.wake.set()

    def _begin_recording_locked(self, device_id: str, task: MonitorTask, *, mode: str | None = None) -> None:
        """Create a recorder while holding ``self.lock`` before any frame is scheduled."""
        if task.recording:
            return
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
            context = self.repository.new_save_context(next_mode, "continuous", device_id=device_id)
            recorder = self.repository.create_recording(
                next_mode,
                context=context,
                csv_interval=task.csv_interval,
                wake=self.wake.set,
            )
        except Exception:
            try:
                self.sessions.set_monitoring(device_id, True, previous_lock)
            except DeviceError:
                # A preparation failure may also invalidate the session. Restore the
                # reservation directly so the original filesystem error is preserved.
                with self.sessions.lock:
                    session = self.sessions.sessions.get(device_id)
                    if session is not None:
                        session.recording = previous_lock
            raise
        task.mode = next_mode
        task.acquisition_context_id = uuid4().hex
        task.recorder = recorder
        self.recordings.append(recorder)
        task.recording_notice = ""
        task.record_session = context.started_utc
        task.last_record_at = task.next_record_at = 0
        task.recording = True

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
        with self.lock:
            if self.closing:
                raise DeviceError("Monitoring is closing")
            self._validate_saved_batch(frame.batch)
            context = self.repository.new_save_context(mode, "single_frame", identity=frame.batch.identity)
            return self.writer.submit(self.repository.export, frame.batch, mode, context=context)

    @staticmethod
    def _validate_saved_batch(batch: MeasurementBatch) -> None:
        validate_recorded_batch(batch)

    def export_displayed_frame(self, device_id: str, frame: MonitorFrame, mode: str) -> Future[Any]:
        """Export this displayed snapshot without I/O or upgrading its provenance."""
        validate_displayed_batch(frame.batch)
        if mode not in WORKSPACE_MODES:
            raise ValidationError("Invalid recording mode")
        with self.lock:
            if self.closing or device_id in self.display_exports or device_id in self.capture_saves:
                raise DeviceError("当前帧正在保存，请等待完成", "task_busy")
            context = self.repository.new_save_context(
                mode, "single_frame", device_id=device_id, identity=frame.batch.identity
            )
            future = self.writer.submit(self.repository.export, frame.batch, mode, context=context)
            self.display_exports[device_id] = future

            def finished(_future: Future[Any]) -> None:
                with self.lock:
                    self.display_exports.pop(device_id, None)
                self.wake.set()

            future.add_done_callback(finished)
            return future

    def _export(
        self, device_id: str, batch: MeasurementBatch, mode: str, context: SaveContext | None = None
    ) -> Any:
        self._validate_saved_batch(batch)
        started = time.perf_counter()
        try:
            return self.repository.export(batch, mode, context=context)
        finally:
            elapsed = time.perf_counter() - started
            with self.lock:
                self.save_seconds[device_id] = elapsed
                now = time.monotonic()
                report = now - self.save_report_at.get(device_id, 0) >= 5
                if report:
                    self.save_report_at[device_id] = now
            if report:
                self.repository.audit("monitor_save_timing", device_id=device_id, seconds=elapsed)

    def capture_and_export(self, device_id: str, request: AcquisitionRequest, mode: str) -> Future[Any]:
        """One explicit full read, queued behind in-flight I/O, then asynchronous save."""
        request = replace(request, consistency="full", initiate=False, context_id="")
        request.validate()
        with self.lock:
            if self.closing or device_id in self.capture_saves or device_id in self.display_exports:
                raise DeviceError("采集并保存正在进行，请等待完成", "task_busy")
            if mode not in WORKSPACE_MODES:
                raise ValidationError("Invalid recording mode")
            state = next((s for s in self.sessions.snapshots() if s["stableDeviceId"] == device_id), {})
            task = self.tasks.get(device_id)
            if task and task.recording and state.get("supportsStreamAcquisition"):
                request = replace(request, consistency="stream", context_id=task.acquisition_context_id)
            if state.get("controlGroup") or state.get("controlPending"):
                raise DeviceError("请等待当前设置完成", "task_busy")
            result: Future[Any] = Future()
            context = self.repository.new_save_context(mode, "single_frame", device_id=device_id)
            read = self.sessions.submit(
                device_id, "observe" if request.kind == "status" else "acquire", request
            )
            self.capture_saves[device_id] = CaptureSave(read, result, mode, save_context=context)
            read.add_done_callback(lambda _future: self.wake.set())
        self.wake.set()
        return result

    def read_memory_waveform(self, device_id: str, request: AcquisitionRequest) -> Future[Any]:
        """Explicit, serialized memory snapshot; no implicit export or recording."""
        request = replace(
            request, waveform_extent="memory", consistency="full", context_id="", initiate=False
        )
        request.validate()
        with self.lock:
            state = next((s for s in self.sessions.snapshots() if s["stableDeviceId"] == device_id), {})
            session = self.sessions.sessions.get(device_id)
            if not session or not getattr(session.adapter, "supports_memory_waveform", False):
                raise DeviceError("当前型号未提供完整存储读取", "unsupported")
            if (
                self.closing
                or device_id in self.capture_saves
                or state.get("controlsLocked")
                or state.get("controlGroup")
                or state.get("controlPending")
            ):
                raise DeviceError("请等待控制或记录收尾完成", "task_busy")
            self.sessions.set_control_group(device_id, True)
            try:
                future = self.sessions.submit(device_id, "acquire", request)
            except Exception:
                self.sessions.set_control_group(device_id, False)
                raise
            result: Future[Any] = Future()
            self.capture_saves[device_id] = CaptureSave(
                future, result, "", display_request=request, session_id=state["sessionId"]
            )
            future.add_done_callback(lambda _: self.wake.set())
        self.wake.set()
        return result

    def _finish_captures(self) -> None:
        for device_id, capture in tuple(self.capture_saves.items()):
            try:
                if capture.write is not None:
                    if not capture.write.done():
                        continue
                    path = capture.write.result()
                    capture.result.set_result(path)
                    del self.capture_saves[device_id]
                elif capture.read.done():
                    task = self.tasks.get(device_id)
                    if task and (task.write_pending or capture.display_request and task.pending):
                        continue
                    batch = immutable_batch(capture.read.result())
                    self._validate_saved_batch(batch)
                    if capture.display_request is not None:
                        frame = MonitorFrame(
                            (task.sequence if task else 0) + 1,
                            int(batch.configuration.get("_session_configuration_version", 0)),
                            capture.display_request,
                            batch,
                            session_id=capture.session_id,
                        )
                        self.sessions.set_control_group(device_id, False)
                        capture.result.set_result(frame)
                        del self.capture_saves[device_id]
                        continue
                    capture.write = self.writer.submit(
                        self._export, device_id, batch, capture.mode, capture.save_context
                    )
                    capture.write.add_done_callback(lambda _future: self.wake.set())
            except Exception as exc:
                if capture.display_request is not None:
                    self.sessions.set_control_group(device_id, False)
                    self._pause(device_id, str(exc))
                capture.result.set_exception(exc)
                del self.capture_saves[device_id]
                self.repository.audit("capture_save_failed", device_id=device_id, error=str(exc))

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
            task.recording = False
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

    def set_recording(self, device_id: str, enabled: bool, *, mode: str | None = None) -> None:
        with self.lock:
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
                    self._begin_recording_locked(device_id, task, mode=mode)
            else:
                task.recording = False
                self.sessions.set_monitoring(
                    device_id,
                    True,
                    bool(
                        task.pending_record_session
                        or task.write_pending
                        or (task.recorder and not task.recorder.binary_done)
                    ),
                )
            self.repository.audit(
                "monitor_recording",
                device_id=device_id,
                enabled=enabled,
                mode=task.mode,
                record_session=task.record_session,
            )
        self.wake.set()

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

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return {
                device_id: {
                    "running": task.running,
                    "recording": task.recording,
                    "pending": bool(task.pending or task.write_pending),
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
            self._pause(event["device_id"], event.get("error", "Device operation failed"))

    def _pause(self, device_id: str, error: str) -> None:
        with self.lock:
            task = self.tasks.get(device_id)
            if task:
                task.running, task.error = False, error
                task.recording = False
                self.repository.audit("monitor_paused", device_id=device_id, error=error)
        self.wake.set()

    def _finish_batch(self, device_id: str, task: MonitorTask, session_id: str = "") -> None:
        future, request = task.pending, task.pending_request
        if future is None or request is None:
            return
        pending_revision = task.pending_revision
        task.pending = None
        record_session, task.pending_record_session = task.pending_record_session, ""
        if pending_revision != task.request_revision:
            try:
                future.result()
            except Exception:
                pass
            if record_session and task.recorder:
                task.recorder.cancel_reservation()
            task.next_at = 0
            return
        try:
            batch: MeasurementBatch = future.result()
        except DeviceError as exc:
            if record_session and task.recorder:
                task.recorder.cancel_reservation()
            if exc.code != "configuration_changed":
                raise
            task.configuration_changes += 1
            task.frames.clear()
            task.frame_interval, task.last_frame_at = None, 0
            if task.once:
                self._pause(device_id, "本次读取期间配置变化，未取得有效曲线。" + str(exc))
                return
            if not task.running and not task.configuration_wait_expired:
                task.notice = ""
                return
            if task.configuration_wait_expired:
                return
            now = time.monotonic()
            if not task.configuration_wait_deadline:
                task.configuration_wait_deadline = now + 30
            task.notice = "仪器正在自动调整或配置变化 · 等待稳定曲线（最多 30 秒）"
            task.next_at = min(now + task.interval, task.configuration_wait_deadline)
            return
        good = all(m.quality in {Quality.GOOD, Quality.SIMULATED} for m in batch.measurements)
        if not good:
            raise DeviceError(
                "; ".join(
                    m.metadata.get("error", m.quality.value)
                    for m in batch.measurements
                    if m.quality not in {Quality.GOOD, Quality.SIMULATED}
                )
            )
        if task.configuration_wait_expired:
            # Drain an already submitted read without extending the 30 s wait.
            # Its errors still propagate above; late data is not shown or saved.
            if record_session and task.recorder:
                task.recorder.cancel_reservation()
            return
        batch = immutable_batch(batch)
        if record_session and task.recorder:
            batch = replace(
                batch,
                configuration=FrozenDict(
                    {
                        **batch.configuration,
                        "monitor_record_session": record_session,
                        "monitor_request": request,
                        "monitor_refresh_interval": task.interval,
                        "monitor_save_interval": task.record_interval,
                        "monitor_cadence": task.cadence,
                    }
                ),
            )
            self._validate_saved_batch(batch)
            if not task.recorder.submit(batch):
                task.recording = False
                task.recording_notice = task.recorder.pause_reason
            task.last_record_at = task.last_started_at
            task.next_record_at = (
                task.last_record_at + task.record_interval if task.record_interval is not None else 0
            )
        now = time.monotonic()
        task.acquisition_seconds = now - task.last_started_at
        task.frame_interval = now - task.last_frame_at if task.last_frame_at else None
        task.last_frame_at = now
        task.configuration_changes, task.notice = 0, ""
        task.configuration_wait_deadline = 0
        task.sequence += 1
        key = (
            request.kind,
            request.quantity + (f":{request.channels[0]}" if request.kind == "status" else ""),
        )
        task.frames[key] = MonitorFrame(
            task.sequence,
            int(batch.configuration.get("_session_configuration_version", 0)),
            request,
            batch,
            batch.performance.get("received_at_monotonic", time.perf_counter()),
            session_id,
        )
        if request.kind == "scalar":
            task.frame_history.setdefault(key, deque(maxlen=1200)).append(task.frames[key])
        task.timings.append(
            {
                "sequence": task.sequence,
                "acquisition_seconds": task.acquisition_seconds,
                "scheduler_seconds": task.scheduler_seconds,
            }
        )
        if now - task.timing_report_at >= 5:
            self.repository.audit(
                "pipeline_statistics",
                device_id=device_id,
                frames=list(task.timings),
                recording=task.recorder.snapshot() if task.recorder else {},
            )
            task.timing_report_at = now
        for callback in tuple(self.frame_listeners):
            try:
                callback(device_id)
            except Exception as exc:
                self.repository.audit("frame_listener_failed", device_id=device_id, error=str(exc))
        # Start-to-start interval: slow device I/O does not incur another full
        # interval afterwards. There is still only one in-flight read, no catch-up queue.
        task.next_at = 0 if task.cadence == "fastest" else task.last_started_at + task.interval
        if task.once:
            task.running = False

    def _tick(self) -> None:
        self._finish_captures()
        snapshots = {s["stableDeviceId"]: s for s in self.sessions.snapshots()}
        for device_id, task in tuple(self.tasks.items()):
            try:
                if (
                    task.running
                    and task.configuration_wait_deadline
                    and time.monotonic() >= task.configuration_wait_deadline
                ):
                    task.running = False
                    task.configuration_wait_expired = True
                    task.notice = "等待稳定已达 30 秒 · 已暂停读取，设置稳定后可重新开始"
                    self.repository.audit("monitor_settle_expired", device_id=device_id)
                if task.write_pending and task.write_pending.done():
                    writing, task.write_pending = task.write_pending, None
                    task.last_path = str(writing.result())
                if task.pending and task.pending.done():
                    self._finish_batch(device_id, task, snapshots.get(device_id, {}).get("sessionId", ""))
                if task.recorder:
                    info = task.recorder.snapshot()
                    task.last_path = info["last_path"] or task.last_path
                    if info["paused"]:
                        task.recording = False
                        task.recording_notice = info["paused"]
                    if not task.recording and not task.pending_record_session:
                        task.recorder.finish()
                state = snapshots.get(device_id)
                if not state or state["connectionState"] in {"error", "degraded"}:
                    task.running = False
                    task.error = task.error or (state or {}).get("lastError", "Device disconnected")
                if state and task.running:
                    self.sessions.set_monitoring(
                        device_id,
                        True,
                        bool(
                            task.recording
                            or task.pending_record_session
                            or task.write_pending
                            or (task.recorder and not task.recorder.binary_done)
                        ),
                    )
                if not task.running:
                    continue
                if (
                    not state
                    or task.pending
                    or state["pending"]
                    or time.monotonic() < task.next_at
                    or device_id in self.capture_saves
                ):
                    continue
                if not task.requests:
                    continue
                request = task.requests[task.index % len(task.requests)]
                save_due = bool(
                    task.recording
                    and (task.record_interval is None or time.monotonic() >= task.next_record_at)
                )
                if (
                    save_due
                    and task.recorder
                    and not task.recorder.reserve(
                        max(65536, request.points * len(request.channels) * 32 + 4096)
                    )
                ):
                    task.recording = save_due = False
                    task.recording_notice = task.recorder.pause_reason
                request = replace(
                    request,
                    consistency="stream"
                    if state.get("supportsStreamAcquisition") and task.recording and not request.initiate
                    else "live"
                    if state.get("supportsLiveAcquisition") and not save_due and not task.once
                    else "full",
                    context_id=task.acquisition_context_id if task.recording else task.monitoring_context_id,
                )
                future = self.sessions.try_submit_idle(
                    device_id, "observe" if request.kind == "status" else "acquire", request
                )
                if future is not None:
                    task.index += 1
                    task.pending_request = request
                    task.pending_revision = task.request_revision
                    task.last_started_at = time.monotonic()
                    task.scheduler_seconds = (
                        max(0, task.last_started_at - task.last_frame_at) if task.last_frame_at else 0
                    )
                    task.pending_record_session = task.record_session if save_due else ""
                    task.pending_record_mode = task.mode
                    task.pending = future
                    future.add_done_callback(lambda _future: self.wake.set())
                elif save_due and task.recorder:
                    task.recorder.cancel_reservation()
            except Exception as exc:
                if task.recorder:
                    task.recorder.cancel_reservation()
                self._pause(device_id, str(exc))
            finally:
                # An error can finish the last in-flight read/write during close.
                # Release the monitoring flag in this tick, before _loop may exit.
                if (
                    not task.running
                    and not task.pending
                    and not task.write_pending
                    and (not task.recorder or task.recorder.binary_done)
                    and device_id not in self.capture_saves
                ):
                    if device_id in snapshots:
                        self.sessions.set_monitoring(device_id, False)
                    task.recording = False
                    if (
                        task.stop_future
                        and not task.stop_future.done()
                        and (not task.recorder or task.recorder.finished.done())
                    ):
                        task.stop_future.set_result(None)
                if not task.running and task.recorder and not task.pending:
                    task.recorder.finish()
        # Monitoring gets first refusal on idle workers; settings polling never queues behind it.
        if not self.closing:
            self.settings_sync.tick({s["stableDeviceId"]: s for s in self.sessions.snapshots()})

    def _loop(self) -> None:
        while True:
            self.wake.clear()
            with self.lock:
                self._tick()
                if (
                    self.closing
                    and not self.capture_saves
                    and all(not t.pending and not t.write_pending for t in self.tasks.values())
                ):
                    break
                deadlines = [
                    t.next_at
                    for t in self.tasks.values()
                    if t.running and not t.pending and t.next_at > time.monotonic()
                ]
                if self.settings_sync.enabled and self.settings_sync.next_dispatch_at > time.monotonic():
                    deadlines.append(self.settings_sync.next_dispatch_at)
                # Idle worker/task completions wake us; the bounded fallback serves
                # low-priority settings synchronization, never fastest frame pacing.
                timeout = min(0.1, max(0.001, min(deadlines) - time.monotonic())) if deadlines else 0.1
            self.wake.wait(timeout)

    def close(self) -> None:
        with self.lock:
            self.closing = True
            self.settings_sync.stop()
            for task in self.tasks.values():
                task.running = False
                task.recording = False
        self.wake.set()
        self.thread.join()
        self.unsubscribe()
        self.writer.shutdown(wait=True)
        for recording in self.recordings:
            recording.finish().result()
        with self.lock:
            # Binary draining can outlive the scheduler's last device read.
            # Release occupancy only after it has actually completed.
            self._tick()
