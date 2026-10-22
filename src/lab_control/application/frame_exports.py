"""Shared live monitoring, scheduled through the existing per-device session workers."""

from __future__ import annotations

import time
from concurrent.futures import Future
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from lab_control.application.acquisition_pipeline import (
    immutable_batch,
    validate_displayed_batch,
)
from lab_control.application.monitoring_state import CaptureSave, MonitorFrame
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceError,
    MeasurementBatch,
    ValidationError,
)
from lab_control.domain.workspace import WORKSPACE_MODES
from lab_control.storage.paths import SaveContext

if TYPE_CHECKING:
    from lab_control.application.monitoring import MonitoringService


class FrameExportService:
    """Collaborator using the facade's shared lock and state; owns no device connection."""

    def __init__(self, owner: MonitoringService) -> None:
        self.owner = owner

    def export(self, frame: MonitorFrame, mode: str) -> Future[Any]:
        owner = self.owner
        with owner.lock:
            if owner.closing:
                raise DeviceError("Monitoring is closing")
            owner._validate_saved_batch(frame.batch)
            context = owner.repository.new_save_context(mode, "single_frame", identity=frame.batch.identity)
            return owner.writer.submit(owner.repository.export, frame.batch, mode, context=context)

    def export_displayed_frame(self, device_id: str, frame: MonitorFrame, mode: str) -> Future[Any]:
        """Export this displayed snapshot without I/O or upgrading its provenance."""
        owner = self.owner
        validate_displayed_batch(frame.batch)
        if mode not in WORKSPACE_MODES:
            raise ValidationError("Invalid recording mode")
        with owner.lock:
            if owner.closing or device_id in owner.display_exports or device_id in owner.capture_saves:
                raise DeviceError("当前帧正在保存，请等待完成", "task_busy")
            context = owner.repository.new_save_context(
                mode, "single_frame", device_id=device_id, identity=frame.batch.identity
            )
            future = owner.writer.submit(owner.repository.export, frame.batch, mode, context=context)
            owner.display_exports[device_id] = future

            def finished(_future: Future[Any]) -> None:
                with owner.lock:
                    owner.display_exports.pop(device_id, None)
                owner.wake.set()

            future.add_done_callback(finished)
            return future

    def write(
        self, device_id: str, batch: MeasurementBatch, mode: str, context: SaveContext | None = None
    ) -> Any:
        owner = self.owner
        owner._validate_saved_batch(batch)
        started = time.perf_counter()
        try:
            return owner.repository.export(batch, mode, context=context)
        finally:
            elapsed = time.perf_counter() - started
            with owner.lock:
                owner.save_seconds[device_id] = elapsed
                now = time.monotonic()
                report = now - owner.save_report_at.get(device_id, 0) >= 5
                if report:
                    owner.save_report_at[device_id] = now
            if report:
                owner.audit.try_record("monitor_save_timing", device_id=device_id, seconds=elapsed)

    def capture_and_export(self, device_id: str, request: AcquisitionRequest, mode: str) -> Future[Any]:
        """One explicit full read, queued behind in-flight I/O, then asynchronous save."""
        owner = self.owner
        request = replace(request, consistency="full", initiate=False, context_id="")
        request.validate()
        with owner.lock:
            if owner.closing or device_id in owner.capture_saves or device_id in owner.display_exports:
                raise DeviceError("采集并保存正在进行，请等待完成", "task_busy")
            if mode not in WORKSPACE_MODES:
                raise ValidationError("Invalid recording mode")
            state = next((s for s in owner.sessions.snapshots() if s["stableDeviceId"] == device_id), {})
            task = owner.tasks.get(device_id)
            if task and task.recording and state.get("supportsStreamAcquisition"):
                request = replace(request, consistency="stream", context_id=task.acquisition_context_id)
            if state.get("controlGroup") or state.get("controlPending"):
                raise DeviceError("请等待当前设置完成", "task_busy")
            result: Future[Any] = Future()
            result.set_running_or_notify_cancel()
            context = owner.repository.new_save_context(mode, "single_frame", device_id=device_id)
            read = owner.sessions.submit(
                device_id, "observe" if request.kind == "status" else "acquire", request
            )
            owner.capture_saves[device_id] = CaptureSave(read, result, mode, save_context=context)
            read.add_done_callback(lambda _future: owner.wake.set())
        owner.wake.set()
        return result

    def read_memory_waveform(self, device_id: str, request: AcquisitionRequest) -> Future[Any]:
        """Explicit, serialized memory snapshot; no implicit export or recording."""
        owner = self.owner
        request = replace(
            request, waveform_extent="memory", consistency="full", context_id="", initiate=False
        )
        request.validate()
        with owner.lock:
            state = next((s for s in owner.sessions.snapshots() if s["stableDeviceId"] == device_id), {})
            session = owner.sessions.sessions.get(device_id)
            if not session or not getattr(session.adapter, "supports_memory_waveform", False):
                raise DeviceError("当前型号未提供完整存储读取", "unsupported")
            if (
                owner.closing
                or device_id in owner.capture_saves
                or state.get("controlsLocked")
                or state.get("controlGroup")
                or state.get("controlPending")
            ):
                raise DeviceError("请等待控制或记录收尾完成", "task_busy")
            owner.sessions.set_control_group(device_id, True)
            try:
                future = owner.sessions.submit(device_id, "acquire", request)
            except Exception:
                owner.sessions.set_control_group(device_id, False)
                raise
            result: Future[Any] = Future()
            result.set_running_or_notify_cancel()
            owner.capture_saves[device_id] = CaptureSave(
                future, result, "", display_request=request, session_id=state["sessionId"]
            )
            future.add_done_callback(lambda _: owner.wake.set())
        owner.wake.set()
        return result

    def finish_captures(self) -> None:
        owner = self.owner
        for device_id, capture in tuple(owner.capture_saves.items()):
            try:
                if capture.write is not None:
                    if not capture.write.done():
                        continue
                    path = capture.write.result()
                    capture.result.set_result(path)
                    del owner.capture_saves[device_id]
                elif capture.read.done():
                    task = owner.tasks.get(device_id)
                    if task and (task.write_pending or capture.display_request and task.pending):
                        continue
                    batch = immutable_batch(capture.read.result())
                    owner._validate_saved_batch(batch)
                    if capture.display_request is not None:
                        frame = MonitorFrame(
                            (task.sequence if task else 0) + 1,
                            int(batch.configuration.get("_session_configuration_version", 0)),
                            capture.display_request,
                            batch,
                            session_id=capture.session_id,
                        )
                        owner.sessions.set_control_group(device_id, False)
                        capture.result.set_result(frame)
                        del owner.capture_saves[device_id]
                        continue
                    capture.write = owner.writer.submit(
                        owner._export, device_id, batch, capture.mode, capture.save_context
                    )
                    capture.write.add_done_callback(lambda _future: owner.wake.set())
            except Exception as exc:
                if capture.display_request is not None:
                    owner.sessions.set_control_group(device_id, False)
                    owner._pause(device_id, str(exc))
                capture.result.set_exception(exc)
                del owner.capture_saves[device_id]
                owner.audit.try_record("capture_save_failed", device_id=device_id, error=str(exc))
