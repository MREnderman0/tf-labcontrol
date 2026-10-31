"""Recovery scheduling on the existing monitoring lock and per-device worker."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from lab_control.application.acquisition_recovery import Recovery, recoverable

if TYPE_CHECKING:
    from lab_control.application.monitoring import MonitoringService
    from lab_control.application.monitoring_state import MonitorTask


class MonitorRecovery:
    def __init__(self, owner: MonitoringService) -> None:
        self.owner = owner

    def begin(self, device_id: str, task: MonitorTask, error: BaseException) -> bool:
        owner = self.owner
        if owner.closing or not task.running or task.once or not recoverable(error):
            return False
        session = owner.sessions.sessions.get(device_id)
        if session is None or session.control_pending or session.control_group:
            return False
        if task.recovery is None:
            task.recovery = Recovery(
                str(error),
                getattr(error, "code", "disconnected"),
                last_valid_at=max((frame.batch.timestamp for frame in task.frames.values()), default=""),
            )
            try:
                owner.sessions.recovery.event(
                    session,
                    task.recovery,
                    "acquisition_interrupted",
                    task.recorder if task.recording else None,
                )
            except Exception as exc:
                owner._pause(device_id, str(exc))
                return True
        else:
            task.recovery.failed_reads += 1
            task.recovery.failed(error)
        session.recovery = task.recovery
        session.state = "recovering"
        task.error = ""
        task.configuration_wait_deadline = 0
        task.notice = "通信中断 · 正在自动重连，恢复后继续采集"
        owner.wake.set()
        return True

    def cancel(self, task: MonitorTask) -> None:
        if task.recovery:
            task.recovery.cancelled.set()

    def tick(self, device_id: str, task: MonitorTask) -> bool:
        """True means no new sample may be submitted in this tick."""
        owner = self.owner
        recovery = task.recovery
        if recovery is None:
            return False
        if task.recovery_pending and task.recovery_pending.done():
            future, task.recovery_pending = task.recovery_pending, None
            try:
                future.result()
            except Exception as exc:
                if task.running and not recovery.cancelled.is_set():
                    if not recoverable(exc):
                        owner._pause(device_id, str(exc))
                    else:
                        recovery.failed(exc)
                        owner.audit.try_record(
                            "reconnect_failed", device_id=device_id, recovery=recovery.snapshot()
                        )
        if not task.running or recovery.cancelled.is_set():
            recovery.cancelled.set()
            if task.recovery_pending is None:
                session = owner.sessions.sessions.get(device_id)
                task.recovery = None
                if session:
                    session.recovery = None
                    if session.state == "recovering":
                        session.state = "degraded"
                    # Clear first: a broken disk must not repeatedly fail cleanup.
                    owner.sessions.recovery.event(
                        session, recovery, "acquisition_recovery_stopped", task.recorder
                    )
            return True
        if recovery.ready:
            return False
        task.notice = f"正在自动重连 · 第 {recovery.attempts + 1} 次 · 中断 {time.monotonic() - recovery.started:.1f} 秒"
        if task.recovery_pending is None and not task.pending and time.monotonic() >= recovery.next_at:
            task.recovery_pending = owner.sessions.try_recover(device_id, recovery)
            if task.recovery_pending:
                task.recovery_pending.add_done_callback(lambda _future: owner.wake.set())
        return True
