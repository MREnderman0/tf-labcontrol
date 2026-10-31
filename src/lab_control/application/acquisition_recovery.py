"""Shared, cancellable recovery of acquisition connections; never replay controls."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from lab_control.domain.instruments import DeviceError, MeasurementBatch, utc_now

if TYPE_CHECKING:
    from lab_control.application.sessions import DeviceSession, SessionManager
    from lab_control.storage.recording import Recording


RECOVERABLE_CODES = frozenset(
    {
        "timeout",
        "disconnected",
        "connection_failed",
        "driver_missing",
        "resource_busy",
        "parse_error",
        "sdk_error",
        "acquisition_failed",
        "data_gap",
    }
)


def recoverable(error: BaseException) -> bool:
    return isinstance(error, DeviceError) and error.code in RECOVERABLE_CODES


@dataclass
class Recovery:
    error: str
    code: str
    last_valid_at: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=utc_now)
    started: float = field(default_factory=time.monotonic)
    next_at: float = 0
    attempts: int = 0
    failed_reads: int = 1
    ready: bool = False
    cancelled: threading.Event = field(default_factory=threading.Event)

    def failed(self, error: BaseException) -> None:
        self.error, self.code = str(error), getattr(error, "code", "device_error")
        self.ready = False
        # No queue of missed samples, no busy loop, no upper bound on outage duration.
        self.next_at = time.monotonic() + (0.2, 0.5, 1.0, 2.0, 5.0)[min(max(self.attempts - 1, 0), 4)]

    def snapshot(self) -> dict[str, Any]:
        return dict(
            id=self.id,
            started_at=self.started_at,
            elapsed_seconds=max(0, time.monotonic() - self.started),
            last_valid_at=self.last_valid_at,
            attempts=self.attempts,
            failed_reads=self.failed_reads,
            error=self.error,
            code=self.code,
            next_retry_seconds=max(0, self.next_at - time.monotonic()),
        )


class AcquisitionRecovery:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    def event(
        self, session: DeviceSession, recovery: Recovery, event: str, recording: Recording | None = None
    ) -> None:
        details = recovery.snapshot()
        self.manager.audit.try_record(event, device_id=session.device_id, recovery=details)
        if recording is not None and not recording.error:
            recording.note_event(event, details)

    def reconnect(self, session: DeviceSession, recovery: Recovery) -> None:
        """One attempt on the existing device worker, keeping every resource lease."""
        manager = self.manager

        def check() -> None:
            if (
                recovery.cancelled.is_set()
                or session.closing
                or manager._closing
                or (session.active_task == "continuous_acquisition" and session.stop.is_set())
            ):
                raise DeviceError("自动重连已取消", "recovery_cancelled")

        check()
        original = session.identity
        if original is None:
            raise DeviceError("自动重连缺少原设备身份", "identity_conflict")
        recovery.attempts += 1
        with manager.lock:
            session.recovery = recovery
            session.state = "recovering"
            session.last_error = recovery.error
            session.settings.clear()
            session.frames.clear()
            session.last_status.clear()
            session.configuration_version += 1
            if session.capability_snapshot:
                session.capability_snapshot = replace(session.capability_snapshot, stale=True)
        manager.emit("recovery_started", session.device_id, recovery=recovery.snapshot())
        candidate = None
        try:
            # Do not send LOCAL, reset, saved parameter settings or output commands.
            reopen = getattr(session.adapter, "reopen_acquisition", None)
            if callable(reopen):
                # Keep model-owned measurement services alive across socket failures.
                candidate = session.adapter
                reopen()
            elif session.adapter is not None:
                manager._invalidate_acquisition(session, "reconnect")
                session.adapter.close()
                session.adapter = None
            check()
            if candidate is None:
                candidate = manager.factory(session.config)
            check()
            identity = candidate.identify()
            same_model = (identity.manufacturer.strip().casefold(), identity.model.strip().casefold()) == (
                original.manufacturer.strip().casefold(),
                original.model.strip().casefold(),
            )
            if not same_model or (original.serial and identity.serial != original.serial):
                raise DeviceError("重连设备身份与原采集设备不一致，已停止恢复", "identity_conflict")
            if (
                session.config.expected_serial
                and identity.serial
                and identity.serial != session.config.expected_serial
            ):
                raise DeviceError("重连设备序列号与配置不一致", "identity_conflict")
            if original.firmware and identity.firmware != original.firmware:
                raise DeviceError("重连设备固件与原采集会话不一致", "qualification_stale")
            check()
            bind = getattr(candidate, "bind_control_state", None)
            if callable(bind):
                key = f"device-control-state/{session.device_id}"
                bind(
                    lambda: manager.repository.load_workspace(key),
                    lambda value: manager.repository.save_workspace(key, value),
                )
            prepare = getattr(candidate, "prepare_acquisition", None)
            if session.acquisition_setup_enabled and callable(prepare):
                # Only the model's declared format initialization is authorized.
                # Its failure may follow an uncertain write: never automatically repeat it.
                try:
                    prepare()
                except Exception as exc:
                    raise DeviceError(
                        f"重连采集初始化未确认：{exc}", "recovery_initialization_failed"
                    ) from exc
            check()
            session.adapter, candidate = candidate, None
            session.identity = identity
            manager._refresh_capabilities(session)
            check()
            with manager.lock:
                session.state = (
                    "busy"
                    if session.monitoring or session.active_task == "continuous_acquisition"
                    else "connected"
                )
                session.last_error = ""
                recovery.ready = True
            manager.emit("reconnected", session.device_id, recovery=recovery.snapshot())
        except Exception:
            if candidate is not None and candidate is not session.adapter:
                try:
                    candidate.close()
                except Exception as cleanup:
                    manager.audit.try_record(
                        "recovery_cleanup_failed", device_id=session.device_id, error=str(cleanup)
                    )
            with manager.lock:
                session.state = "recovering"
            raise

    def complete(
        self,
        session: DeviceSession,
        recovery: Recovery,
        batch: MeasurementBatch,
        recording: Recording | None = None,
    ) -> MeasurementBatch:
        details = recovery.snapshot() | {"resumed_at": batch.timestamp}
        self.event(session, recovery, "acquisition_resumed", recording)
        with self.manager.lock:
            session.recovery = None
            session.last_error = ""
        return replace(batch, configuration={**batch.configuration, "_acquisition_gap": details})

    def wait(self, session: DeviceSession, recovery: Recovery) -> bool:
        """Direct recordings use their worker; a stop interrupts every backoff."""
        while not session.stop.is_set() and not self.manager._closing:
            if session.stop.wait(max(0, recovery.next_at - time.monotonic())):
                break
            try:
                self.reconnect(session, recovery)
                return True
            except DeviceError as exc:
                if exc.code == "recovery_cancelled":
                    break
                if not recoverable(exc):
                    raise
                recovery.failed(exc)
                self.event(session, recovery, "reconnect_failed")
        recovery.cancelled.set()
        return False
