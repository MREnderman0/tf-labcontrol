"""Shared live monitoring, scheduled through the existing per-device session workers."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING

from lab_control.application.acquisition_pipeline import (
    FrozenDict,
    immutable_batch,
)
from lab_control.application.monitoring_state import MonitorFrame, MonitorTask
from lab_control.domain.instruments import (
    DeviceError,
    MeasurementBatch,
    Quality,
)

if TYPE_CHECKING:
    from lab_control.application.monitoring import MonitoringService


class MonitorScheduler:
    """Collaborator using the facade's shared lock and state; owns no device connection."""

    def __init__(self, owner: MonitoringService) -> None:
        self.owner = owner

    def finish_batch(self, device_id: str, task: MonitorTask, session_id: str = "") -> None:
        owner = self.owner
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
                owner._pause(device_id, "本次读取期间配置变化，未取得有效曲线。" + str(exc))
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
            owner._validate_saved_batch(batch)
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
            owner.audit.try_record(
                "pipeline_statistics",
                device_id=device_id,
                frames=list(task.timings),
                recording=task.recorder.snapshot() if task.recorder else {},
            )
            task.timing_report_at = now
        for callback in tuple(owner.frame_listeners):
            try:
                callback(device_id)
            except Exception as exc:
                owner.audit.try_record("frame_listener_failed", device_id=device_id, error=str(exc))
        # Start-to-start interval: slow device I/O does not incur another full
        # interval afterwards. There is still only one in-flight read, no catch-up queue.
        task.next_at = 0 if task.cadence == "fastest" else task.last_started_at + task.interval
        if task.once:
            task.running = False

    def tick(self) -> None:
        owner = self.owner
        owner._finish_captures()
        snapshots = {s["stableDeviceId"]: s for s in owner.sessions.snapshots()}
        for device_id, task in tuple(owner.tasks.items()):
            try:
                if (
                    task.running
                    and task.configuration_wait_deadline
                    and time.monotonic() >= task.configuration_wait_deadline
                ):
                    task.running = False
                    task.configuration_wait_expired = True
                    task.notice = "等待稳定已达 30 秒 · 已暂停读取，设置稳定后可重新开始"
                    owner.audit.try_record("monitor_settle_expired", device_id=device_id)
                if task.write_pending and task.write_pending.done():
                    writing, task.write_pending = task.write_pending, None
                    task.last_path = str(writing.result())
                if task.pending and task.pending.done():
                    owner._finish_batch(device_id, task, snapshots.get(device_id, {}).get("sessionId", ""))
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
                    owner.sessions.set_monitoring(
                        device_id,
                        True,
                        bool(
                            task.recording
                            or task.recording_preparation
                            or task.pending_record_session
                            or task.write_pending
                            or (task.recorder and not task.recorder.binary_done)
                        ),
                    )
                if not task.running:
                    continue
                if (
                    not state
                    or task.recording_preparation is not None
                    or task.pending
                    or state["pending"]
                    or time.monotonic() < task.next_at
                    or device_id in owner.capture_saves
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
                future = owner.sessions.try_submit_idle(
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
                    future.add_done_callback(lambda _future: owner.wake.set())
                elif save_due and task.recorder:
                    task.recorder.cancel_reservation()
            except Exception as exc:
                if task.recorder:
                    task.recorder.cancel_reservation()
                owner._pause(device_id, str(exc))
            finally:
                # An error can finish the last in-flight read/write during close.
                # Release the monitoring flag in this tick, before _loop may exit.
                if (
                    not task.running
                    and not task.pending
                    and not task.write_pending
                    and task.recording_preparation is None
                    and (not task.recorder or task.recorder.binary_done)
                    and device_id not in owner.capture_saves
                ):
                    if device_id in snapshots:
                        owner.sessions.set_monitoring(device_id, False)
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
        if not owner.closing:
            owner.settings_sync.tick({s["stableDeviceId"]: s for s in owner.sessions.snapshots()})

    def loop(self) -> None:
        owner = self.owner
        while True:
            owner.wake.clear()
            with owner.lock:
                owner._tick()
                if (
                    owner.closing
                    and not owner.capture_saves
                    and all(
                        not t.pending and not t.write_pending and t.recording_preparation is None
                        for t in owner.tasks.values()
                    )
                ):
                    break
                deadlines = [
                    t.next_at
                    for t in owner.tasks.values()
                    if t.running and not t.pending and t.next_at > time.monotonic()
                ]
                if owner.settings_sync.enabled and owner.settings_sync.next_dispatch_at > time.monotonic():
                    deadlines.append(owner.settings_sync.next_dispatch_at)
                # Idle worker/task completions wake us; the bounded fallback serves
                # low-priority settings synchronization, never fastest frame pacing.
                timeout = min(0.1, max(0.001, min(deadlines) - time.monotonic())) if deadlines else 0.1
            owner.wake.wait(timeout)
