"""Bounded settings polling, driven by the existing monitoring scheduler.

All methods run under MonitoringService.lock. This class owns no device handles,
threads, or unbounded work queues. CLI runtimes leave it disabled.
"""

from __future__ import annotations

import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable

from lab_control.application.sessions import SessionManager, SettingsFrame
from lab_control.domain.instruments import ParameterReading
from lab_control.domain.oscilloscope import CONTROL_NAMES as SCOPE_SETTINGS
from lab_control.domain.readback import usable_reading
from lab_control.domain.spectrum import SETTINGS as SPECTRUM_SETTINGS

BASE = ("output", "waveform", "frequency", "power", "amplitude", "offset")
POWER_BASE = ("all_outputs", "output", "voltage", "current", "measured_voltage", "measured_current")
Key = tuple[int, str]


@dataclass
class SyncTask:
    session_id: str
    configuration_version: int = 0
    pending_version: int = 0
    pending: Future[Any] | None = None
    keys: tuple[Key, ...] = ()
    errors: dict[Key, str] = field(default_factory=dict)
    retry_at: float = 0
    failures: int = 0
    force_after: float = 0
    batches: int = 0
    last_log: tuple[str, str] | None = None


class SettingsSync:
    def __init__(self, sessions: SessionManager, wake: Callable[[], None] = lambda: None) -> None:
        self.sessions = sessions
        self.wake = wake
        self.enabled = False
        self.focus = ""
        self.advanced: tuple[Key, ...] = ()
        self.tasks: dict[str, SyncTask] = {}
        self.round_robin: list[str] = []
        self.max_concurrent = 4
        self.foreground_interval = 30.0
        self.background_interval = 60.0
        self.dispatch_interval = 0.05
        self.next_dispatch_at = 0.0
        self.max_observed_concurrent = 0

    def poll_interval(self, device_id: str) -> float:
        return self.foreground_interval if device_id == self.focus else self.background_interval

    def set_focus(self, device_id: str, advanced: tuple[Key, ...] = ()) -> None:
        if (device_id, advanced) == (self.focus, self.advanced):
            return
        self.focus, self.advanced = device_id, advanced
        self.request(device_id)

    def request(self, device_id: str) -> None:
        task = self.tasks.get(device_id)
        if task:
            task.force_after = time.monotonic()

    def wanted(self, device_id: str) -> dict[Key, Any]:
        snapshot = self.sessions.capability_snapshot(device_id)
        if not snapshot or not snapshot.channels:
            return {}
        category = next(iter(snapshot.channels.values())).category
        base = {
            "signal_generator": BASE,
            "power_supply": POWER_BASE,
            "spectrum_analyzer": SPECTRUM_SETTINGS,
            "oscilloscope": SCOPE_SETTINGS,
        }.get(category)
        if base is None:
            return {}
        requested = set(self.advanced) if device_id == self.focus else set()
        result = {}
        for channel, schema in snapshot.channels.items():
            for cap in schema.operations:
                key = (channel if cap.channel_scoped else 1, cap.name)
                if (cap.name in base or key in requested) and cap.available and cap.readable:
                    if not any(spec.kind == "array" for spec in cap.parameters.values()):
                        result[key] = cap
        return result

    @staticmethod
    def current(frame: SettingsFrame | None, state: dict[str, Any]) -> bool:
        return bool(
            frame
            and frame.session_id == state.get("sessionId")
            and frame.configuration_version == state.get("configurationVersion")
        )

    def status(self, device_id: str, keys: tuple[Key, ...]) -> tuple[str, str]:
        state = next((s for s in self.sessions.snapshots() if s["stableDeviceId"] == device_id), {})
        task = self.tasks.get(device_id)
        connection = state.get("connectionState")
        if connection in {"error", "degraded"}:
            return "同步异常", state.get("lastError", "")
        if connection not in {"connected", "busy"}:
            return "未连接", "连接成功后自动同步"
        if task and any(key in task.errors for key in keys):
            return "同步异常", "；".join(dict.fromkeys(task.errors[k] for k in keys if k in task.errors))
        wanted = self.wanted(device_id)
        keys = tuple(key for key in keys if key in wanted)
        if not keys:
            return "无可读参数", "此功能没有可用的轻量读回"
        frames = self.sessions.readback_frames(device_id)
        age = 3 * self.poll_interval(device_id)
        now = time.monotonic()
        if all(
            self.current(frames.get(k), state)
            and usable_reading(frames[k].reading, wanted[k].parameters)
            and now - frames[k].received_at <= age
            for k in keys
        ):
            return "正常", "显示已与仪器同步；不代表物理输出验收"
        if (
            state.get("controlGroup")
            or state.get("controlPending")
            or state.get("monitoring")
            or state.get("recording")
            or state.get("activeTask") in {"continuous_acquisition", "acquire", "observe"}
        ):
            return "同步已暂停", "设备任务优先，空闲后自动继续"
        return ("待同步" if any(k in frames for k in keys) else "同步中"), "等待最新有效读回"

    def _transition(self, device_id: str, task: SyncTask, status: str, error: str = "") -> None:
        value = (status, error)
        if value != task.last_log:
            task.last_log = value
            self.sessions.repository.audit(
                "settings_sync_state", device_id=device_id, status=status, error=error, batches=task.batches
            )

    def tick(self, states: dict[str, dict[str, Any]]) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        for device_id in tuple(self.tasks):
            task = self.tasks[device_id]
            state = states.get(device_id, {})
            if state.get("sessionId") != task.session_id:
                # Reconnection invalidates old futures and all their error/freshness state.
                del self.tasks[device_id]
                continue
            version = state.get("configurationVersion", 0)
            if version != task.configuration_version:
                task.configuration_version = version
                task.errors.clear()
                task.failures, task.retry_at = 0, 0
            if task.pending and task.pending.done():
                future, task.pending = task.pending, None
                task.batches += 1
                if task.pending_version != version:
                    # A user write finished before this result was consumed. Its
                    # values and error state both belong to an obsolete configuration.
                    continue
                try:
                    readings: tuple[ParameterReading, ...] = future.result()
                    wanted = self.wanted(device_id)
                    returned = {(r.channel, r.operation): r for r in readings}
                    for key in task.keys:
                        reading = returned.get(key)
                        cap = wanted.get(key)
                        if cap and (reading is None or not usable_reading(reading, cap.parameters)):
                            task.errors[key] = (reading.error if reading else "") or f"{key[1]} 读回无效"
                        else:
                            task.errors.pop(key, None)
                    if any(k in task.errors for k in task.keys):
                        raise ValueError("；".join(task.errors[k] for k in task.keys if k in task.errors))
                    if not task.errors:
                        task.failures, task.retry_at = 0, 0
                        self._transition(device_id, task, "healthy")
                except Exception as exc:
                    for key in task.keys:
                        task.errors[key] = str(exc)
                    delays = (2, 5, 10, 30)
                    task.retry_at = now + delays[min(task.failures, len(delays) - 1)]
                    task.failures += 1
                    self._transition(device_id, task, "failed", str(exc))
        for device_id, state in states.items():
            if device_id not in self.tasks and self.wanted(device_id):
                self.tasks[device_id] = SyncTask(
                    state["sessionId"], configuration_version=state.get("configurationVersion", 0)
                )
                self.round_robin.append(device_id)
        self.round_robin = list(dict.fromkeys(k for k in self.round_robin if k in self.tasks))
        active = sum(task.pending is not None for task in self.tasks.values())
        order = ([self.focus] if self.focus in self.tasks else []) + [
            k for k in self.round_robin if k != self.focus
        ]
        for device_id in order:
            if active >= self.max_concurrent or now < self.next_dispatch_at:
                break
            task, state = self.tasks[device_id], states.get(device_id, {})
            if (
                task.pending
                or now < task.retry_at
                or state.get("connectionState") not in {"connected", "busy"}
            ):
                continue
            frames = self.sessions.readback_frames(device_id)
            interval = self.poll_interval(device_id)
            wanted = self.wanted(device_id)
            due = [
                key
                for key in wanted
                if not self.current(frames.get(key), state)
                or key in task.errors
                or frames[key].received_at < task.force_after
                or now - frames[key].received_at >= interval
            ]
            # Oldest channel first prevents starvation when queries take longer than the interval.
            due.sort(
                key=lambda k: (
                    frames[k].received_at if self.current(frames.get(k), state) else 0,
                    k[1] not in {"output", "all_outputs"},
                    k[0],
                )
            )
            if not due:
                continue
            channel = due[0][0]
            keys = tuple(k for k in due if k[0] == channel)[:4]
            submitted = self.sessions.try_submit_idle(
                device_id, "read_settings", (tuple(k[1] for k in keys), channel), quiet=True
            )
            if submitted is None:
                continue
            # Space automatic batches globally, including first reads and
            # resumption after monitoring. Never catch up missed dispatch slots.
            self.next_dispatch_at = now + self.dispatch_interval
            task.pending, task.keys = submitted, keys
            submitted.add_done_callback(lambda _future: self.wake())
            task.pending_version = state.get("configurationVersion", 0)
            active += 1
            self.max_observed_concurrent = max(self.max_observed_concurrent, active)
            self.round_robin.remove(device_id)
            self.round_robin.append(device_id)

    def stop(self) -> None:
        self.enabled = False
        for device_id, task in self.tasks.items():
            self.sessions.repository.audit(
                "settings_sync_summary", device_id=device_id, batches=task.batches, failures=task.failures
            )
