from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Quality,
    jsonable,
    utc_now,
)
from lab_control.transport.diagnostics import capture_io
from lab_control.transport.read_wait import acquisition_read_wait

if TYPE_CHECKING:
    from lab_control.application.sessions import DeviceSession, SessionManager


class SessionOperations:
    """Single-operation implementation; the manager owns admission and its worker."""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    def acquire(
        self, session: DeviceSession, request: AcquisitionRequest, submitted_at: float | None = None
    ) -> MeasurementBatch:
        manager = self.manager
        if session.adapter is None:
            raise DeviceError("No adapter", "disconnected")
        request.validate()
        if request.waveform_extent != "default" and request.waveform_extent not in getattr(
            session.adapter, "waveform_extents", ()
        ):
            raise DeviceError("Requested waveform extent is not supported by this model", "unsupported")
        if (
            request.consistency == "stream"
            and not getattr(session.adapter, "supports_stream_acquisition", False)
        ) or (
            request.consistency == "live" and not getattr(session.adapter, "supports_live_acquisition", False)
        ):
            request = replace(request, consistency="full")
        started = time.perf_counter()
        started_utc = utc_now()
        with (
            capture_io() as capture,
            acquisition_read_wait(
                session.monitoring or session.active_task == "continuous_acquisition",
                transport=getattr(session.adapter, "transport", None),
            ),
        ):
            try:
                try:
                    batch = session.adapter.acquire(request)
                except DeviceError as exc:
                    if (
                        exc.code != "acquisition_setup_required"
                        or not request.allow_setup
                        or not manager._prepare_acquisition_state(session)
                    ):
                        raise
                    # Only this explicit, pre-waveform setup condition permits a
                    # second read. Transport/parser failures never enter this path.
                    batch = session.adapter.acquire(request)
            except Exception as exc:
                manager.audit.try_record(
                    "acquisition_transition_timing"
                    if getattr(exc, "code", "") == "configuration_changed"
                    else "acquisition_io_failed",
                    device_id=session.device_id,
                    performance=capture.summary(),
                    code=getattr(exc, "code", "device_error"),
                )
                manager._invalidate_acquisition(session, "error")
                raise
        completed = time.perf_counter()
        completed_utc = utc_now()
        performance = {
            **batch.performance,
            **capture.summary(),
            "acquisition_seconds": completed - started,
            "received_at_monotonic": completed,
            "queue_seconds": max(0, started - submitted_at) if submitted_at is not None else 0,
        }
        with manager.lock:
            summary = session.acquisition_statistics.add(performance, batch.validation)
        if summary is not None:
            manager.audit.try_record("acquisition_statistics", device_id=session.device_id, summary=summary)
        if capture.details:
            manager.audit.try_record(
                "acquisition_io_details", device_id=session.device_id, commands=capture.details
            )
        return replace(
            batch,
            timestamp=completed_utc,
            time_basis="host_utc_at_read_completed; waveform axes from instrument",
            timing={
                "version": 1,
                "host_read_started_at": started_utc,
                "host_read_completed_at": completed_utc,
                "read_duration_seconds": completed - started,
                "clock_source": "host_utc; durations=monotonic",
            },
            performance=performance,
            configuration={
                **batch.configuration,
                "_session_configuration_version": session.configuration_version,
            },
        )

    def execute(
        self,
        session: DeviceSession,
        operation: str,
        argument: Any,
        *,
        quiet: bool = False,
        submitted_at: float | None = None,
    ) -> Any:
        manager = self.manager
        device_id = session.device_id
        with manager.lock:
            session.background_read = False
            if session.state in {"error", "degraded"}:
                session.pending -= 1
                if manager._controls_instrument(operation, argument):
                    session.control_pending -= 1
                raise DeviceError("Previous I/O failed; queued command was not sent", "disconnected")
            session.state, session.active_task = "busy", operation
            session.background_read = quiet and operation == "read_settings"
        if not quiet:
            manager.emit("task_started", device_id, operation=operation)
        audit_rejected = False
        try:
            adapter = session.adapter
            if adapter is None:
                raise DeviceError("No adapter", "disconnected")
            if not quiet and operation not in {"acquire", "observe"}:
                try:
                    manager.repository.audit(
                        "operation_requested",
                        device_id=device_id,
                        operation=operation,
                        argument=argument,
                        before=session.settings.get((argument.channel, argument.operation))
                        if isinstance(argument, DeviceCommand)
                        else None,
                    )
                except Exception:
                    audit_rejected = True
                    raise
            result: Any
            if operation == "identify":
                result = adapter.identify()
                if session.identity and result.key != session.identity.key:
                    raise DeviceError("Identity changed in existing session", "identity_conflict")
                session.identity = result
            elif operation == "status":
                result = adapter.read_status()
                session.last_status = jsonable(result)
            elif operation == "capabilities":
                snapshot = session.capability_snapshot or manager._refresh_capabilities(session)
                if not snapshot.channels:
                    raise DeviceError("等待设备通道识别，请重新连接或刷新能力", "invalid_parameter")
                result = snapshot.channels[next(iter(snapshot.channels))]
            elif operation == "refresh_capabilities":
                manager._invalidate_acquisition(session, "explicit_refresh")
                result = manager._refresh_capabilities(session)
                if manager._prepare_acquisition_state(session):
                    result = session.capability_snapshot
            elif operation == "read_settings":
                if not quiet:
                    manager._invalidate_acquisition(session, "explicit_read")
                operations, channel = argument
                result = adapter.read_settings(tuple(operations), channel)
                # Model-owned caches may now imply different channel ranges/permissions.
                # Publish only actual changes, without extra I/O or identical audit snapshots.
                if callable(getattr(adapter, "capabilities_for_channel", None)):
                    manager._refresh_capabilities(session, query=False, changed_only=True)
                manager._cache_readings(session, result)
            elif operation == "apply":
                if not isinstance(argument, DeviceCommand):
                    raise TypeError("Expected DeviceCommand")
                snapshot = manager._refresh_capabilities(session)
                if snapshot.stale:
                    raise DeviceError("Refresh instrument ranges before writing", "invalid_parameter")
                if argument.channel not in snapshot.channels:
                    raise DeviceError("Invalid channel", "invalid_parameter")
                snapshot.channels[argument.channel].get(argument.operation).validate(argument.parameters)
                # Even an interrupted write can change hardware. Invalidate older reads
                # before I/O, including paths where the adapter raises instead of returning.
                with manager.lock:
                    session.configuration_version += 1
                # Opted-in models own selective invalidation, including failure paths.
                if not getattr(adapter, "supports_control_updates", False):
                    manager._invalidate_acquisition(session, "apply")
                try:
                    result = adapter.apply(argument)
                finally:
                    manager._refresh_capabilities(session, query=False)
                if result.outcome == "success":
                    try:
                        manager._refresh_capabilities(session)
                    except Exception as exc:
                        # Do not replay an already completed write when the subsequent refresh fails.
                        session.last_error = "设置已执行，能力刷新失败：" + str(exc)
                        session.state = "degraded"
                if result.outcome != "success":
                    session.last_error = result.error or result.outcome
                else:
                    if session.state != "degraded":
                        session.last_error = ""
                    session.settings[(argument.channel, argument.operation)] = ParameterReading(
                        argument.operation,
                        argument.channel,
                        result.readback if result.readback is not None else result.requested,
                        "session_cache",
                        result.timestamp,
                    )
                if result.outcome in {"uncertain", "partial"}:
                    session.state = "degraded"
                if result.outcome == "success" and session.state != "degraded" and result.control_update:
                    manager._cache_readings(session, result.control_update.readings)
                elif result.outcome != "success":
                    manager._invalidate_acquisition(session, "error")
            elif operation == "observe":
                if not isinstance(argument, AcquisitionRequest):
                    raise TypeError("Expected AcquisitionRequest")
                argument.validate()
                with acquisition_read_wait(session.monitoring, transport=getattr(adapter, "transport", None)):
                    readings = adapter.read_settings((argument.quantity,), argument.channels[0])
                manager._cache_readings(session, readings)
                reading = readings[0]
                value = reading.value
                if isinstance(value, dict):
                    value = value.get("value")
                cap = adapter.capabilities().get(argument.quantity)
                spec = cap.parameters.get("value")
                result = MeasurementBatch(
                    session.identity or DeviceIdentity("", ""),
                    (
                        Measurement(
                            argument.quantity,
                            spec.unit if spec else "",
                            [float(value)] if isinstance(value, (float, int, bool)) else [],
                            argument.channels[0],
                            quality=reading.quality,
                            metadata={"display_value": value, "source": reading.source},
                        ),
                    ),
                    {},
                )
            else:
                if not isinstance(argument, AcquisitionRequest):
                    raise TypeError("Expected AcquisitionRequest")
                result = manager._acquire(session, argument, submitted_at)
            if operation in {"acquire", "observe"}:
                # Optional, model-owned readings tied to this exact acquisition. No inferred
                # settings from arbitrary configuration dictionaries or extra device queries.
                acquisition_readings = getattr(adapter, "acquisition_readings", None)
                if operation == "acquire" and callable(acquisition_readings):
                    manager._cache_readings(session, acquisition_readings(result))
                result = replace(
                    result,
                    configuration={
                        **result.configuration,
                        "_session_configuration_version": session.configuration_version,
                    },
                )
            if not quiet and operation not in {"acquire", "observe"}:
                manager.audit.try_record(
                    "operation_completed",
                    device_id=device_id,
                    operation=operation,
                    result=result
                    if operation not in {"acquire", "observe"}
                    else {"timestamp": result.timestamp},
                )
            if not quiet:
                manager.emit("result", device_id, operation=operation, result=result)
            return result
        except Exception as exc:
            manager._invalidate_acquisition(session, "error")
            code = getattr(exc, "code", "device_error")
            if operation == "acquire" and code == "configuration_changed":
                # A complete reply with changing axis/settings is an invalid frame,
                # not a broken transport. Never publish it or replay any control.
                with manager.lock:
                    session.configuration_version += 1
                manager.audit.try_record(
                    "acquisition_discarded", device_id=device_id, code=code, error=str(exc)
                )
                raise
            if not audit_rejected and code not in {
                "unsupported",
                "invalid_parameter",
                "confirmation_required",
                "missing_option",
                "acquisition_budget_exceeded",
                "acquisition_setup_required",
            }:
                session.state = "degraded"
            session.last_error = str(exc)
            if not quiet:
                manager.audit.try_record(
                    "operation_failed", device_id=device_id, operation=operation, code=code, error=str(exc)
                )
            if not quiet:
                manager.emit("error", device_id, operation=operation, error=str(exc), code=code)
            if (
                operation == "acquire"
                and isinstance(exc, DeviceError)
                and code in {q.value for q in Quality} | {"sdk_error", "acquisition_failed"}
            ):
                quality = Quality(code) if code in Quality._value2member_map_ else Quality.PARSE_ERROR
                return MeasurementBatch(
                    session.identity or DeviceIdentity("", ""),
                    (Measurement(argument.kind, "", [], quality=quality, metadata={"error": str(exc)}),),
                    {"request": jsonable(argument)},
                )
            raise
        finally:
            # Model-owned report metadata may change capabilities without an
            # extra query (including a rejected request for a removed channel).
            adapter = session.adapter
            revision = getattr(adapter, "capability_revision", None)
            previous_revision = (
                session.capability_snapshot.details.get("capability_revision")
                if session.capability_snapshot
                else None
            )
            if revision is not None and revision != previous_revision and session.identity:
                try:
                    manager._refresh_capabilities(session, query=False, changed_only=True)
                except Exception as exc:
                    manager.audit.try_record("capability_publish_failed", device_id=device_id, error=str(exc))
            with manager.lock:
                session.background_read = False
                session.pending -= 1
                if manager._controls_instrument(operation, argument):
                    session.control_pending -= 1
                session.active_task = "monitoring" if session.monitoring else ""
                if session.state == "busy":
                    session.state = "busy" if session.monitoring else "connected"
            if not quiet:
                manager.emit("task_finished", device_id)
