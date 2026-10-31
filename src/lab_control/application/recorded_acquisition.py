from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from lab_control.application.acquisition_pipeline import (
    immutable_batch,
    validate_displayed_batch,
    validate_recorded_batch,
)
from lab_control.application.acquisition_recovery import Recovery, recoverable
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceError,
)
from lab_control.domain.recording import RecordingError
from lab_control.storage.recording import Recording
from lab_control.transport.read_wait import acquisition_read_wait

if TYPE_CHECKING:
    from lab_control.application.sessions import DeviceSession, SessionManager


class RecordedAcquisitionRunner:
    """Run and drain one authorized acquisition on its existing session worker."""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    def run(
        self,
        session: DeviceSession,
        request: AcquisitionRequest,
        recording: Recording,
        *,
        interval: float,
        count: int | None,
        mode: str,
        cadence: str | None,
        record_interval: float | None,
        run_factory: Any,
    ) -> list[str]:
        manager = self.manager
        device_id = session.device_id
        run = None
        stream_request = replace(
            request,
            consistency="stream"
            if getattr(session.adapter, "supports_stream_acquisition", False) and not request.initiate
            else "full",
            context_id=uuid4().hex,
        )
        io_started = False
        primary_error: Exception | None = None
        recovery: Recovery | None = None
        resume_state: Any = None
        last_valid_at = ""
        run_started = time.monotonic()
        try:
            manager.repository.audit(
                "stream_started",
                device_id=device_id,
                request=request,
                interval=interval,
                count=count,
                mode=mode,
            )
            run = run_factory(request, session.stop) if run_factory else None
            index = 0
            next_save = 0.0
            while run is not None or (not session.stop.is_set() and (count is None or index < count)):
                if recovery and not recovery.ready:
                    if not manager.recovery.wait(session, recovery):
                        break
                    if session.stop.is_set():
                        break
                    if run_factory:
                        resume = getattr(session.adapter, "resume_acquisition", None)
                        if not callable(resume):
                            raise DeviceError("此连续流尚未提供安全续接入口", "recovery_unsupported")
                        run = resume(request, session.stop, resume_state, time.monotonic() - run_started)
                if session.adapter is None:
                    break
                started = time.monotonic()
                save_due = started >= next_save
                if save_due and not recording.reserve(
                    max(65536, request.points * len(request.channels) * 32 + 4096)
                ):
                    raise RecordingError(
                        recording.outcome(), "recording_failed" if recording.error else "recording_paused"
                    )
                try:
                    io_started = True
                    with acquisition_read_wait(transport=getattr(session.adapter, "transport", None)):
                        batch = immutable_batch(
                            next(run) if run is not None else manager._acquire(session, stream_request)
                        )
                    validate_displayed_batch(batch)
                    if save_due:
                        validate_recorded_batch(batch)
                except StopIteration:
                    recording.cancel_reservation()
                    break
                except DeviceError as exc:
                    recording.cancel_reservation()
                    if not recoverable(exc):
                        raise
                    if run is not None:
                        resume_state = deepcopy(getattr(session.adapter, "recorded_run_state", None))
                        run.close()
                        run = None
                    if recovery is None:
                        recovery = Recovery(str(exc), exc.code, last_valid_at=last_valid_at)
                        manager.recovery.event(session, recovery, "acquisition_interrupted", recording)
                    else:
                        recovery.failed_reads += 1
                        recovery.failed(exc)
                    session.recovery = recovery
                    if session.stop.is_set():
                        break
                    continue
                if recovery:
                    batch = immutable_batch(manager.recovery.complete(session, recovery, batch, recording))
                    recovery = None
                if save_due:
                    if not recording.submit(batch):
                        raise RecordingError(
                            recording.outcome(), "recording_failed" if recording.error else "recording_paused"
                        )
                    next_save = started + (record_interval or 0)
                manager.emit("measurement", device_id, batch=batch, path=recording.last_path)
                last_valid_at = batch.timestamp
                index += 1
                now = time.monotonic()
                due = started + interval
                if run is None and session.stop.wait(
                    0 if cadence == "fastest" else (due - now if due > now else interval)
                ):
                    break
            recording.finish()
            recording.thread.join()
            if recording.error:
                raise RecordingError(recording.outcome())
            return list(recording.segments.paths)
        except Exception as exc:
            primary_error = exc
            session.last_error = str(exc)
            code = getattr(exc, "code", "")
            if io_started and code == "configuration_changed":
                # A complete reply with changing settings invalidates the old
                # configuration, not the transport. This direct API still ends
                # on the failed frame without retrying instrument work.
                with manager.lock:
                    session.configuration_version += 1
                manager.audit.try_record(
                    "acquisition_discarded", device_id=device_id, code=code, error=str(exc)
                )
            elif io_started and code not in {"recording_failed", "recording_paused"}:
                session.state = "degraded"
            manager.audit.try_record("stream_failed", device_id=device_id, error=str(exc))
            manager.emit("error", device_id, error=str(exc))
            raise
        finally:
            cleanup_errors = []
            if recovery:
                recovery.cancelled.set()
                if run_factory:
                    try:
                        cancel = getattr(session.adapter, "cancel_acquisition_recovery", None)
                        if not recovery.ready or not callable(cancel):
                            raise DeviceError(
                                "连接未恢复，仪器测量停止未确认；最终状态未知", "cleanup_failed"
                            )
                        cancel()
                    except Exception as exc:
                        session.last_error, session.state = str(exc), "degraded"
                        manager.audit.try_record("stream_cleanup_failed", device_id=device_id, error=str(exc))
                        cleanup_errors.append(exc)
                try:
                    manager.recovery.event(session, recovery, "acquisition_recovery_stopped", recording)
                except Exception as exc:
                    cleanup_errors.append(exc)
                session.recovery = None
                if session.state == "recovering":
                    session.state = "degraded"
            try:
                if run is not None:
                    run.close()
            except Exception as exc:
                session.last_error, session.state = str(exc), "degraded"
                manager.audit.try_record("stream_cleanup_failed", device_id=device_id, error=str(exc))
                manager.emit("error", device_id, error=str(exc))
                cleanup_errors.append(exc)
            finally:
                try:
                    for cleanup in (recording.cancel_reservation, recording.finish, recording.thread.join):
                        try:
                            cleanup()
                        except Exception as exc:
                            cleanup_errors.append(exc)
                finally:
                    with manager.lock:
                        session.pending, session.active_task = 0, ""
                        if session.state == "busy":
                            session.state = "connected"
                    manager.audit.try_record(
                        "stream_stopped",
                        device_id=device_id,
                        batches=recording.committed,
                        requested_stop=session.stop.is_set(),
                    )
                    manager.emit("task_finished", device_id)
            if cleanup_errors:
                error = primary_error or cleanup_errors[0]
                for secondary in cleanup_errors:
                    if secondary is not error:
                        error.add_note(f"Acquisition cleanup failed: {secondary}")
                if primary_error is None:
                    raise error
