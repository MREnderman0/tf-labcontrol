from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from uuid import uuid4

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.domain.instruments import (
    AcquisitionRequest,
    CapabilitySnapshot,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    InstrumentAdapter,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Quality,
    jsonable,
    utc_now,
)
from lab_control.storage.repository import Repository


@dataclass(frozen=True)
class SettingsFrame:
    session_id: str
    configuration_version: int
    sequence: int
    received_at: float
    reading: ParameterReading


@dataclass
class DeviceSession:
    device_id: str
    config: ConnectionConfig
    worker: ThreadPoolExecutor
    session_id: str = field(default_factory=lambda: uuid4().hex)
    state: str = "connecting"
    identity: DeviceIdentity | None = None
    adapter: InstrumentAdapter | None = None
    active_task: str = ""
    pending: int = 0
    last_status: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    stop: threading.Event = field(default_factory=threading.Event)
    stream_future: Future[Any] | None = None
    closing: bool = False
    monitoring: bool = False
    recording: bool = False
    configuration_version: int = 0
    control_pending: int = 0
    settings: dict[tuple[int, str], ParameterReading] = field(default_factory=dict)
    frames: dict[tuple[int, str], SettingsFrame] = field(default_factory=dict)
    read_sequence: int = 0
    control_group: bool = False
    capability_snapshot: CapabilitySnapshot | None = None


class SessionManager:
    def __init__(
        self,
        leases: ResourceLeases,
        repository: Repository,
        factory: Callable[[ConnectionConfig], InstrumentAdapter],
    ) -> None:
        self.leases, self.repository, self.factory = leases, repository, factory
        self.sessions: dict[str, DeviceSession] = {}
        self.lock = threading.RLock()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self.lock:
            self.listeners.append(callback)

        def unsubscribe() -> None:
            with self.lock:
                if callback in self.listeners:
                    self.listeners.remove(callback)

        return unsubscribe

    def emit(self, event: str, device_id: str, **payload: Any) -> None:
        with self.lock:
            listeners = tuple(self.listeners)
        message = {"event": event, "device_id": device_id, "timestamp": utc_now(), **payload}
        for listener in listeners:
            try:
                listener(message)
            except Exception as exc:
                self.repository.audit("listener_error", error=str(exc))

    def snapshots(self) -> tuple[dict[str, Any], ...]:
        with self.lock:
            return tuple(
                {
                    "stableDeviceId": item.device_id,
                    "sessionId": item.session_id,
                    "resource": item.config.resource,
                    "adapterId": item.config.adapter,
                    "connectionState": item.state,
                    "activeTask": item.active_task,
                    "pending": item.pending,
                    "lastStatus": dict(item.last_status),
                    "lastError": item.last_error,
                    "identity": jsonable(item.identity),
                    "monitoring": item.monitoring,
                    "recording": item.recording,
                    "configurationVersion": item.configuration_version,
                    "controlPending": item.control_pending,
                    "controlGroup": item.control_group,
                    "readSequence": item.read_sequence,
                    "capabilityVersion": item.capability_snapshot.version if item.capability_snapshot else 0,
                    "capabilityStale": item.capability_snapshot.stale if item.capability_snapshot else True,
                    "capabilityDetails": item.capability_snapshot.details if item.capability_snapshot else {},
                }
                for item in self.sessions.values()
            )

    def capability_snapshot(self, device_id: str) -> CapabilitySnapshot | None:
        with self.lock:
            session = self.sessions.get(device_id)
            return session.capability_snapshot if session else None

    def readback_frames(self, device_id: str) -> dict[tuple[int, str], SettingsFrame]:
        """Frames are immutable snapshots; callers must not mutate their reading values."""
        with self.lock:
            session = self.sessions.get(device_id)
            return dict(session.frames) if session else {}

    def _cache_readings(self, session: DeviceSession, readings: tuple[ParameterReading, ...]) -> None:
        with self.lock:
            for reading in readings:
                reading = replace(reading, value=deepcopy(reading.value))
                key = (reading.channel, reading.operation)
                session.settings[key] = reading
                session.read_sequence += 1
                session.frames[key] = SettingsFrame(
                    session.session_id,
                    session.configuration_version,
                    session.read_sequence,
                    time.monotonic(),
                    reading,
                )

    def set_control_group(self, device_id: str, active: bool) -> None:
        with self.lock:
            session = self.sessions.get(device_id)
            if session:
                session.control_group = active

    def try_submit_idle(
        self, device_id: str, operation: str, argument: Any = None, *, quiet: bool = False
    ) -> Future[Any] | None:
        """Atomically reserve an idle worker; background jobs never form a queue."""
        if operation not in {"read_settings", "observe", "acquire"}:
            raise ValueError("Idle submissions must be reads")
        with self.lock:
            session = self.sessions.get(device_id)
            if (
                session is None
                or session.closing
                or session.pending
                or session.control_group
                or session.state not in {"connected", "busy"}
                or (session.stream_future and not session.stream_future.done())
            ):
                return None
            session.pending += 1
            return session.worker.submit(self._execute, session, operation, argument, quiet=quiet)

    def _refresh_capabilities(self, session: DeviceSession, *, query: bool = True) -> CapabilitySnapshot:
        from lab_control.application.controls import control_capabilities

        adapter = session.adapter
        if adapter is None or session.identity is None:
            raise DeviceError("No connected adapter", "disconnected")
        previous = session.capability_snapshot
        error = ""
        failure = None
        if query:
            try:
                refresh = getattr(adapter, "refresh_capabilities", None)
                if refresh:
                    refresh()
            except Exception as exc:
                error, failure = str(exc), exc
        error = error or getattr(adapter, "capability_error", "")
        schema = adapter.capabilities()
        snapshot = CapabilitySnapshot(
            session.identity,
            {channel: control_capabilities(session.config, channel, schema) for channel in schema.channels},
            tuple(getattr(adapter, "detected_options", ()) or session.config.options),
            {
                **getattr(adapter, "capability_details", {}),
                "configured_options": list(session.config.options),
            },
            version=previous.version + 1 if previous else 1,
            stale=bool(error),
            error=error,
        )
        with self.lock:
            session.capability_snapshot = snapshot
        self.repository.audit("capabilities_refreshed", device_id=session.device_id, snapshot=snapshot)
        if failure:
            raise failure
        return snapshot

    def connect(self, device_id: str, config: ConnectionConfig) -> Future[DeviceIdentity]:
        with self.lock:
            if self.leases.owner("discovery:unknown_alias"):
                raise DeviceError(
                    "Identity probe is using an unconfirmed physical resource; retry after scan",
                    "resource_busy",
                )
            if device_id in self.sessions:
                raise DeviceError(
                    "Device already has a session; disconnect explicitly first", "resource_busy"
                )
            resources = (
                config.resource,
                *config.aliases,
                *((config.data_resource,) if config.data_resource else ()),
            )
            self.leases.acquire(device_id, resources)
            session = DeviceSession(
                device_id, config, ThreadPoolExecutor(max_workers=1, thread_name_prefix="instrument")
            )
            self.sessions[device_id] = session

        def opening() -> DeviceIdentity:
            try:
                adapter = self.factory(config)
                session.adapter = adapter
                identity = adapter.identify()
                if config.expected_serial and config.expected_serial != identity.serial:
                    raise DeviceError(
                        "Configured serial differs from connected instrument", "identity_conflict"
                    )
                if identity.key:
                    self.leases.acquire(device_id, (f"identity:{identity.key}",))
                with self.lock:
                    session.identity, session.state = identity, "connected"
                try:
                    self._refresh_capabilities(session)
                except Exception as exc:
                    session.last_error = "能力读取失败：" + str(exc)
                    if getattr(exc, "code", "") in {"timeout", "disconnected"}:
                        session.state = "degraded"
                self.repository.audit("connected", device_id=device_id, identity=identity, config=config)
                self.emit("connected", device_id, identity=jsonable(identity))
                return identity
            except Exception as exc:
                if session.adapter:
                    try:
                        session.adapter.close()
                    except Exception as cleanup_error:
                        self.repository.audit("cleanup_failed", device_id=device_id, error=str(cleanup_error))
                    session.adapter = None
                with self.lock:
                    session.state, session.last_error = "error", str(exc)
                self.leases.release(device_id)
                self.emit("error", device_id, error=str(exc))
                raise

        return session.worker.submit(opening)

    def submit(self, device_id: str, operation: str, argument: Any = None) -> Future[Any]:
        with self.lock:
            session = self.sessions[device_id]
            if session.closing or session.state not in {"connected", "busy"}:
                raise DeviceError(
                    "Session is unavailable; reconnect after resolving the error", "disconnected"
                )
            if session.stream_future and not session.stream_future.done():
                raise DeviceError("Stop acquisition before submitting another task", "task_busy")
            if operation not in {
                "identify",
                "status",
                "capabilities",
                "refresh_capabilities",
                "read_settings",
                "apply",
                "acquire",
                "observe",
            }:
                raise DeviceError("Unknown session operation", "invalid_parameter")
            session.pending += 1
            if operation == "apply":
                session.control_pending += 1
        return session.worker.submit(self._execute, session, operation, argument)

    def set_monitoring(self, device_id: str, enabled: bool, recording: bool = False) -> None:
        with self.lock:
            session = self.sessions[device_id]
            if enabled and (
                session.closing
                or session.state not in {"connected", "busy"}
                or (session.stream_future and not session.stream_future.done())
            ):
                raise DeviceError("Session cannot start monitoring", "task_busy")
            session.monitoring, session.recording = enabled, recording if enabled else False
            if not session.pending and session.state in {"connected", "busy"}:
                session.state = "busy" if enabled else "connected"
                session.active_task = "monitoring" if enabled else ""

    def _execute(self, session: DeviceSession, operation: str, argument: Any, *, quiet: bool = False) -> Any:
        device_id = session.device_id
        with self.lock:
            if session.state in {"error", "degraded"}:
                session.pending -= 1
                if operation == "apply":
                    session.control_pending -= 1
                raise DeviceError("Previous I/O failed; queued command was not sent", "disconnected")
            session.state, session.active_task = "busy", operation
        if not quiet:
            self.emit("task_started", device_id, operation=operation)
        try:
            adapter = session.adapter
            if adapter is None:
                raise DeviceError("No adapter", "disconnected")
            if not quiet:
                self.repository.audit(
                    "operation_requested",
                    device_id=device_id,
                    operation=operation,
                    argument=argument,
                    before=session.settings.get((argument.channel, argument.operation))
                    if isinstance(argument, DeviceCommand)
                    else None,
                )
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
                snapshot = session.capability_snapshot or self._refresh_capabilities(session)
                result = snapshot.channels[next(iter(snapshot.channels))]
            elif operation == "refresh_capabilities":
                result = self._refresh_capabilities(session)
            elif operation == "read_settings":
                operations, channel = argument
                result = adapter.read_settings(tuple(operations), channel)
                self._cache_readings(session, result)
            elif operation == "apply":
                if not isinstance(argument, DeviceCommand):
                    raise TypeError("Expected DeviceCommand")
                snapshot = self._refresh_capabilities(session)
                if snapshot.stale:
                    raise DeviceError("Refresh instrument ranges before writing", "invalid_parameter")
                if argument.channel not in snapshot.channels:
                    raise DeviceError("Invalid channel", "invalid_parameter")
                snapshot.channels[argument.channel].get(argument.operation).validate(argument.parameters)
                # Even an interrupted write can change hardware. Invalidate older reads
                # before I/O, including paths where the adapter raises instead of returning.
                with self.lock:
                    session.configuration_version += 1
                try:
                    result = adapter.apply(argument)
                finally:
                    self._refresh_capabilities(session, query=False)
                if result.outcome == "success":
                    try:
                        self._refresh_capabilities(session)
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
            elif operation == "observe":
                if not isinstance(argument, AcquisitionRequest):
                    raise TypeError("Expected AcquisitionRequest")
                argument.validate()
                readings = adapter.read_settings((argument.quantity,), argument.channels[0])
                self._cache_readings(session, readings)
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
                result = adapter.acquire(argument)
            if operation in {"acquire", "observe"}:
                result = replace(
                    result,
                    configuration={
                        **result.configuration,
                        "_session_configuration_version": session.configuration_version,
                    },
                )
            if not quiet:
                self.repository.audit(
                    "operation_completed",
                    device_id=device_id,
                    operation=operation,
                    result=result
                    if operation not in {"acquire", "observe"}
                    else {"timestamp": result.timestamp},
                )
            if not quiet:
                self.emit("result", device_id, operation=operation, result=result)
            return result
        except Exception as exc:
            code = getattr(exc, "code", "device_error")
            if code not in {"unsupported", "invalid_parameter", "confirmation_required", "missing_option"}:
                session.state = "degraded"
            session.last_error = str(exc)
            if not quiet:
                self.repository.audit(
                    "operation_failed", device_id=device_id, operation=operation, code=code, error=str(exc)
                )
            if not quiet:
                self.emit("error", device_id, error=str(exc), code=code)
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
            with self.lock:
                session.pending -= 1
                if operation == "apply":
                    session.control_pending -= 1
                session.active_task = "monitoring" if session.monitoring else ""
                if session.state == "busy":
                    session.state = "busy" if session.monitoring else "connected"
            if not quiet:
                self.emit("task_finished", device_id)

    def start_acquisition(
        self,
        device_id: str,
        request: AcquisitionRequest,
        *,
        interval: float = 0.5,
        count: int | None = None,
        mode: str = "electronic",
    ) -> Future[Any]:
        request.validate()
        if interval < 0.01 or (count is not None and count < 1):
            raise DeviceError("Invalid stream interval/count", "invalid_parameter")
        with self.lock:
            session = self.sessions[device_id]
            if session.pending or session.state != "connected" or session.closing:
                raise DeviceError("Device has an active task", "task_busy")
            session.stop.clear()
            session.pending = 1
            session.active_task = "continuous_acquisition"
            session.state = "busy"

        def stream() -> list[str]:
            paths = []
            self.repository.audit(
                "stream_started",
                device_id=device_id,
                request=request,
                interval=interval,
                count=count,
                mode=mode,
            )
            try:
                index = 0
                while not session.stop.is_set() and (count is None or index < count):
                    if session.adapter is None:
                        break
                    batch = session.adapter.acquire(request)
                    path = self.repository.export(batch, mode)
                    paths.append(str(path))
                    self.emit("measurement", device_id, batch=batch, path=str(path))
                    index += 1
                    if session.stop.wait(interval):
                        break
                return paths
            except Exception as exc:
                session.last_error, session.state = str(exc), "degraded"
                code = getattr(exc, "code", "parse_error")
                quality = Quality(code) if code in Quality._value2member_map_ else Quality.PARSE_ERROR
                fault = MeasurementBatch(
                    session.identity or DeviceIdentity("", ""),
                    (Measurement(request.kind, "", [], quality=quality, metadata={"error": str(exc)}),),
                    {"request": jsonable(request), "completed_batch_paths": paths},
                )
                fault_path = self.repository.export(fault, mode)
                self.repository.audit("stream_failed", device_id=device_id, error=str(exc))
                self.emit("error", device_id, error=str(exc), path=str(fault_path))
                raise
            finally:
                self.repository.audit(
                    "stream_stopped",
                    device_id=device_id,
                    batches=len(paths),
                    requested_stop=session.stop.is_set(),
                )
                with self.lock:
                    session.pending, session.active_task = 0, ""
                    if session.state == "busy":
                        session.state = "connected"
                self.emit("task_finished", device_id)

        with self.lock:
            session.stream_future = session.worker.submit(stream)
            return session.stream_future

    def stop_acquisition(self, device_id: str) -> None:
        with self.lock:
            self.sessions[device_id].stop.set()

    def disconnect(self, device_id: str) -> None:
        with self.lock:
            session = self.sessions[device_id]
            if session.pending or session.monitoring or session.state in {"connecting", "busy"}:
                raise DeviceError("Stop and await active tasks before disconnecting", "task_busy")
            session.closing = True
        try:
            if session.adapter:
                session.worker.submit(session.adapter.close).result()
        finally:
            session.worker.shutdown(wait=True)
            self.leases.release(device_id)
            with self.lock:
                del self.sessions[device_id]
            self.repository.audit(
                "disconnected", device_id=device_id, note="Physical output state is unchanged/unknown"
            )
            self.emit("disconnected", device_id)

    def close(self) -> None:
        errors = []
        for session in tuple(self.sessions.values()):
            session.stop.set()
        for device_id, session in tuple(self.sessions.items()):
            try:
                session.worker.submit(lambda: None).result()
                self.disconnect(device_id)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
