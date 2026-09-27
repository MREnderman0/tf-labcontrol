"""Qt boundary: futures and snapshots only, never a device handle."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from lab_control.application.catalog import instrument_category, mode_applicable
from lab_control.application.config import config_from_record
from lab_control.application.controls import control_capabilities
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.presence import LIVE_CONNECTIONS
from lab_control.domain.instruments import DeviceCommand, DeviceError

Job = tuple[Future[Any], Callable[[Any], None] | None, Callable[[Exception], None] | None]


class WorkspaceController(QObject):
    refreshed = Signal()
    message = Signal(str)
    navigate = Signal(str, str, str)
    settings_changed = Signal(str)
    frame_ready = Signal(str)
    _frame_wakeup = Signal()
    _job_wakeup = Signal()

    def __init__(self, runtime: ApplicationRuntime, parent: QObject) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.mode = runtime.repository.load_workspace("current_mode", "electronic")
        self.records: dict[str, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.group_devices: set[str] = set()
        self.sync_device = ""
        self._sync_signature: tuple[Any, ...] = ()
        self._frame_lock = threading.Lock()
        self._frame_devices: set[str] = set()
        self._frame_wakeup.connect(self._deliver_frames, Qt.ConnectionType.QueuedConnection)
        self._job_wakeup.connect(self._deliver_jobs, Qt.ConnectionType.QueuedConnection)
        self._unsubscribe_frames = runtime.monitoring.subscribe_frames(self._queue_frame)
        self.runtime.monitoring.enable_settings_sync()
        self.jobs: list[Job] = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
        self.poll()

    def _queue_frame(self, device_id: str) -> None:
        with self._frame_lock:
            notify = not self._frame_devices
            self._frame_devices.add(device_id)
        if notify:
            self._frame_wakeup.emit()

    def _deliver_frames(self) -> None:
        with self._frame_lock:
            devices, self._frame_devices = self._frame_devices, set()
        if not self.runtime.closed:
            for device_id in devices:
                self.frame_ready.emit(device_id)

    def watch(
        self,
        future: Future[Any],
        callback: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.jobs.append((future, callback, on_error))
        future.add_done_callback(self._queue_job)

    def _queue_job(self, _future) -> None:
        if not self.runtime.closed:
            self._job_wakeup.emit()

    def _deliver_jobs(self) -> None:
        if self.runtime.closed:
            return
        self.states = {s["stableDeviceId"]: s for s in self.runtime.sessions.snapshots()}
        completed: list[Job] = []
        pending: list[Job] = []
        for job in self.jobs:
            (completed if job[0].done() else pending).append(job)
        self.jobs = pending
        for future, callback, on_error in completed:
            try:
                value = future.result()
                if callback:
                    callback(value)
            except Exception as exc:
                if on_error:
                    on_error(exc)
                self.message.emit(str(exc))

    def poll(self) -> None:
        if self.runtime.closed:
            self.timer.stop()
            return
        self.records = {r["id"]: r for r in self.runtime.inventory.all()}
        self.states = {s["stableDeviceId"]: s for s in self.runtime.sessions.snapshots()}
        self._deliver_jobs()
        self.refreshed.emit()
        state = self.states.get(self.sync_device, {})
        signature = (
            self.sync_device,
            state.get("sessionId"),
            state.get("configurationVersion"),
            state.get("readSequence"),
        )
        if signature != self._sync_signature:
            self._sync_signature = signature
            self.settings_changed.emit(self.sync_device)

    def focus_settings(self, device_id: str, advanced: tuple[tuple[int, str], ...] = ()) -> None:
        self.sync_device = device_id
        self.runtime.monitoring.focus_settings(device_id, advanced)

    def begin_group(self, device_id: str) -> None:
        self.runtime.sessions.set_control_group(device_id, True)
        self.group_devices.add(device_id)

    def end_group(self, device_id: str, *, refresh=True) -> None:
        self.group_devices.discard(device_id)
        self.runtime.sessions.set_control_group(device_id, False)
        if refresh:
            self.runtime.monitoring.refresh_settings(device_id)
        self.runtime.monitoring.wake.set()

    def set_mode(self, mode: str) -> None:
        if self.group_devices:
            raise DeviceError("参数正在分组应用，请完成后切换模式", "task_busy")
        if any(
            s.get("recording") or s.get("controlPending") or s.get("activeTask") == "continuous_acquisition"
            for s in self.runtime.sessions.snapshots()
        ):
            raise DeviceError("记录或控制进行中，请完成任务后切换模式", "task_busy")
        self.mode = mode
        self.runtime.repository.save_workspace("current_mode", mode)
        self.refreshed.emit()

    def capabilities(self, device_id: str, channel: int = 1):
        snapshot = self.runtime.sessions.capability_snapshot(device_id)
        if snapshot and channel in snapshot.channels:
            return snapshot.channels[channel]
        return control_capabilities(config_from_record(self.records[device_id]), channel)

    def has_connected_session(self, device_id: str) -> bool:
        return (
            device_id in self.records
            and self.states.get(device_id, {}).get("connectionState") in LIVE_CONNECTIONS
        )

    def connected_records(self, category: str = "") -> tuple[dict[str, Any], ...]:
        """Selection eligibility uses cached sessions, never discovery or a new query."""
        return tuple(
            record
            for key, record in self.records.items()
            if self.has_connected_session(key) and (not category or instrument_category(record) == category)
        )

    def ensure_applicable(self, device_id: str) -> None:
        if not mode_applicable(self.records[device_id], self.mode):
            raise DeviceError("此设备不适用于当前模式", "unsupported")

    def apply(
        self,
        device_id: str,
        command: DeviceCommand,
        callback: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
        *,
        group_member: bool = False,
    ) -> None:
        if device_id in self.group_devices and not group_member:
            raise DeviceError("当前设备正在应用一组参数", "task_busy")
        self.ensure_applicable(device_id)
        cap = self.capabilities(device_id, command.channel).get(command.operation)
        cap.validate(command.parameters)
        self.watch(self.runtime.monitoring.apply(device_id, command), callback, on_error)

    def close(self) -> None:
        self.timer.stop()
        self._unsubscribe_frames()
        self.runtime.monitoring.enable_settings_sync(False)


def background_read_only(state: dict[str, Any]) -> bool:
    """Presentation only: never use this to decide whether device I/O is safe."""
    return bool(
        state.get("connectionState") == "busy"
        and state.get("backgroundRead")
        and state.get("activeTask") == "read_settings"
        and state.get("pending", 0) <= 1
        and not any(state.get(key) for key in ("monitoring", "recording", "controlPending", "controlGroup"))
    )


def display_connection_state(state: dict[str, Any]) -> str:
    return "connected" if background_read_only(state) else str(state.get("connectionState", "disconnected"))


def display_task(state: dict[str, Any]) -> str:
    return "" if background_read_only(state) else str(state.get("activeTask") or "")


def state_label(state: str) -> str:
    return {
        "connected": "已连接",
        "connecting": "连接中",
        "disconnected": "未连接",
        "busy": "任务运行中",
        "degraded": "需要检查",
        "error": "异常",
    }.get(state, state)
