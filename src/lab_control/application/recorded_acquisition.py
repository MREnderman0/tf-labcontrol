from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from lab_control.application.acquisition_pipeline import immutable_batch
from lab_control.domain.instruments import (
    AcquisitionRequest,
)
from lab_control.domain.recording import RecordingError
from lab_control.storage.recording import Recording

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
                    batch = immutable_batch(
                        next(run) if run is not None else manager._acquire(session, stream_request)
                    )
                except StopIteration:
                    recording.cancel_reservation()
                    break
                if save_due:
                    if not recording.submit(batch):
                        raise RecordingError(
                            recording.outcome(), "recording_failed" if recording.error else "recording_paused"
                        )
                    next_save = started + (record_interval or 0)
                manager.emit("measurement", device_id, batch=batch, path=recording.last_path)
                index += 1
                if run is None and session.stop.wait(
                    0 if cadence == "fastest" else max(0, started + interval - time.monotonic())
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
            if io_started and getattr(exc, "code", "") not in {"recording_failed", "recording_paused"}:
                session.state = "degraded"
            manager.audit.try_record("stream_failed", device_id=device_id, error=str(exc))
            manager.emit("error", device_id, error=str(exc))
            raise
        finally:
            cleanup_errors = []
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
