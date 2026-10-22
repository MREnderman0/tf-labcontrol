from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from uuid import uuid4

from lab_control.application.acquisition_pipeline import immutable_batch, validate_cadence
from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.domain.instruments import (
    AcquisitionRequest,
    CapabilitySnapshot,
    ControlUpdate,
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
from lab_control.domain.workspace import GENERAL
from lab_control.storage.recording import Recording
from lab_control.storage.repository import Repository
from lab_control.transport.diagnostics import AcquisitionStatistics, capture_io


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
    background_read: bool = False
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
    acquisition_statistics: AcquisitionStatistics = field(default_factory=AcquisitionStatistics)
    acquisition_preparation: dict[str, Any] = field(default_factory=dict)
    acquisition_setup_enabled: bool = False
    direct_recording: Recording | None = None

    @property
    def controls_locked(self) -> bool:
        return self.recording or self.active_task == "continuous_acquisition"


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
        self.recordings: list[Recording] = []
        self.maintenance_active = False

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
                    "recordingState": item.direct_recording.snapshot() if item.direct_recording else {},
                    "connectionState": item.state,
                    "activeTask": item.active_task,
                    "backgroundRead": item.background_read,
                    "pending": item.pending,
                    "lastStatus": dict(item.last_status),
                    "lastError": item.last_error,
                    "identity": jsonable(item.identity),
                    "monitoring": item.monitoring,
                    "recording": item.recording,
                    "controlsLocked": item.controls_locked,
                    "acquisitionPreparation": deepcopy(item.acquisition_preparation),
                    "configurationVersion": item.configuration_version,
                    "controlPending": item.control_pending,
                    "controlGroup": item.control_group,
                    "readSequence": item.read_sequence,
                    "capabilityVersion": item.capability_snapshot.version if item.capability_snapshot else 0,
                    "capabilityStale": item.capability_snapshot.stale if item.capability_snapshot else True,
                    "capabilityDetails": item.capability_snapshot.details if item.capability_snapshot else {},
                    "supportsLiveAcquisition": bool(
                        getattr(item.adapter, "supports_live_acquisition", False)
                    ),
                    "supportsStreamAcquisition": bool(
                        getattr(item.adapter, "supports_stream_acquisition", False)
                    ),
                    "acquisitionStatistics": item.acquisition_statistics.snapshot(),
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
                previous = session.frames.get(key)
                received_at = (
                    previous.received_at
                    if reading.source == "control_cache"
                    and previous
                    and previous.reading.timestamp == reading.timestamp
                    else time.monotonic()
                )
                session.settings[key] = reading
                session.read_sequence += 1
                session.frames[key] = SettingsFrame(
                    session.session_id,
                    session.configuration_version,
                    session.read_sequence,
                    received_at,
                    reading,
                )

    def set_control_group(self, device_id: str, active: bool) -> None:
        with self.lock:
            session = self.sessions.get(device_id)
            if session:
                if active and session.controls_locked:
                    raise DeviceError("采集中参数已锁定，请先停止采集或记录", "task_busy")
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
                or (quiet and (session.monitoring or session.recording))
                or (operation == "read_settings" and session.controls_locked)
                or session.state not in {"connected", "busy"}
                or (session.stream_future and not session.stream_future.done())
            ):
                return None
            session.pending += 1
            session.background_read = quiet and operation == "read_settings"
            return session.worker.submit(
                self._execute, session, operation, argument, quiet=quiet, submitted_at=time.perf_counter()
            )

    def _refresh_capabilities(
        self, session: DeviceSession, *, query: bool = True, changed_only: bool = False
    ) -> CapabilitySnapshot:
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
        channel_schema = getattr(adapter, "capabilities_for_channel", None)
        snapshot = CapabilitySnapshot(
            session.identity,
            {
                channel: control_capabilities(
                    session.config, channel, channel_schema(channel) if channel_schema else schema
                )
                for channel in schema.channels
            },
            tuple(getattr(adapter, "detected_options", ()) or session.config.options),
            {
                **getattr(adapter, "capability_details", {}),
                "configured_options": list(session.config.options),
                **(
                    {"capability_revision": adapter.capability_revision}
                    if hasattr(adapter, "capability_revision")
                    else {}
                ),
            },
            version=previous.version + 1 if previous else 1,
            stale=bool(error),
            error=error,
        )
        if (
            changed_only
            and previous
            and (snapshot.channels, snapshot.options, snapshot.details, snapshot.stale, snapshot.error)
            == (previous.channels, previous.options, previous.details, previous.stale, previous.error)
        ):
            return previous
        with self.lock:
            session.capability_snapshot = snapshot
        self.repository.audit("capabilities_refreshed", device_id=session.device_id, snapshot=snapshot)
        if failure:
            raise failure
        return snapshot

    def connect(
        self,
        device_id: str,
        config: ConnectionConfig,
        *,
        expected_firmware: str = "",
        configure_acquisition: bool = True,
    ) -> Future[DeviceIdentity]:
        with self.lock:
            if self.maintenance_active:
                raise DeviceError("安装向导运行期间暂停连接", "maintenance_busy")
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
                *(("USBPNP::" + config.usb_instance_id,) if config.usb_instance_id else ()),
                *((config.data_resource,) if config.data_resource else ()),
                *((f"asset:{config.adapter}:{config.expected_serial}",) if config.expected_serial else ()),
            )
            # An explicit adapter/resource pair is safe to open alongside another
            # selected device even when the model protocol cannot report a serial
            # number (JW8105 is one such model).  The endpoint, aliases and any
            # configured asset serial remain exclusive below; the shared gate only
            # prevents an unconfirmed discovery probe from racing active sessions.
            known = bool(config.adapter)
            self.leases.acquire(device_id, resources, identified=known)
            session = DeviceSession(
                device_id, config, ThreadPoolExecutor(max_workers=1, thread_name_prefix="instrument")
            )
            self.sessions[device_id] = session

        def opening() -> DeviceIdentity:
            try:
                adapter = self.factory(config)
                session.adapter = adapter
                identity = adapter.identify()
                # Some model-level protocols have no serial-number query.  An
                # expected serial is still retained as a user-supplied asset label
                # and lease key, but can only be checked when the adapter reports
                # an actual serial.  A reported mismatch remains a hard conflict.
                if config.expected_serial and identity.serial and config.expected_serial != identity.serial:
                    raise DeviceError(
                        "Configured serial differs from connected instrument", "identity_conflict"
                    )
                if expected_firmware and identity.firmware != expected_firmware:
                    raise DeviceError("Firmware differs from transport qualification", "qualification_stale")
                if identity.key:
                    self.leases.acquire(
                        device_id, (f"identity:{identity.key}", f"asset:{config.adapter}:{identity.serial}")
                    )
                bind_state = getattr(adapter, "bind_control_state", None)
                if callable(bind_state):
                    # Optional model-owned journal. Bind only after identity validation;
                    # the hook must not perform device I/O or replay saved commands.
                    state_key = f"device-control-state/{device_id}"
                    bind_state(
                        lambda: self.repository.load_workspace(state_key),
                        lambda value: self.repository.save_workspace(state_key, value),
                    )
                prepare = getattr(adapter, "prepare_acquisition", None)
                session.acquisition_setup_enabled = configure_acquisition
                if configure_acquisition and callable(prepare):
                    # Explicit connection only, after identity/ownership checks;
                    # discovery and read-only hardware preflight never run setup.
                    try:
                        prepare()
                    finally:
                        session.acquisition_preparation = deepcopy(
                            getattr(adapter, "acquisition_preparation", {})
                        )
                        self.repository.audit(
                            "acquisition_prepared",
                            device_id=device_id,
                            result=session.acquisition_preparation,
                        )
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

    @staticmethod
    def _controls_instrument(operation: str, argument: Any) -> bool:
        # Explicit memory reads may stop/run the instrument. Keep their queued
        # and active lifetime under the same recording guard as parameter writes.
        return operation == "apply" or (
            operation == "acquire"
            and isinstance(argument, AcquisitionRequest)
            and argument.waveform_extent == "memory"
        )

    def submit(self, device_id: str, operation: str, argument: Any = None) -> Future[Any]:
        with self.lock:
            session = self.sessions[device_id]
            if session.closing or session.state not in {"connected", "busy"}:
                raise DeviceError(
                    "Session is unavailable; reconnect after resolving the error", "disconnected"
                )
            if session.stream_future and not session.stream_future.done():
                raise DeviceError("Stop acquisition before submitting another task", "task_busy")
            if session.controls_locked and (
                self._controls_instrument(operation, argument)
                or operation in {"read_settings", "refresh_capabilities"}
            ):
                raise DeviceError("采集中参数已锁定，请先停止采集或记录", "task_busy")
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
            if self._controls_instrument(operation, argument):
                session.control_pending += 1
        return session.worker.submit(
            self._execute, session, operation, argument, submitted_at=time.perf_counter()
        )

    def set_monitoring(self, device_id: str, enabled: bool, recording: bool = False) -> None:
        with self.lock:
            session = self.sessions[device_id]
            if enabled and (
                session.closing
                or session.state not in {"connected", "busy"}
                or (session.stream_future and not session.stream_future.done())
                or (recording and (session.control_group or session.control_pending))
            ):
                raise DeviceError("Session cannot start monitoring", "task_busy")
            session.monitoring, session.recording = enabled, recording if enabled else False
            if not session.pending and session.state in {"connected", "busy"}:
                session.state = "busy" if enabled else "connected"
                session.active_task = "monitoring" if enabled else ""

    @staticmethod
    def _invalidate_acquisition(session: DeviceSession, reason: str) -> None:
        invalidate = getattr(session.adapter, "invalidate_acquisition_context", None)
        if callable(invalidate):
            invalidate(reason)

    def _prepare_acquisition_state(self, session: DeviceSession) -> bool:
        """Optional model setup, serialized with controls and forbidden while recording."""
        adapter = session.adapter
        needed = getattr(adapter, "acquisition_setup_needed", None)
        prepare = getattr(adapter, "prepare_acquisition_state", None)
        if not session.acquisition_setup_enabled or not callable(needed) or not needed():
            return False
        if not callable(prepare):
            return False
        with self.lock:
            if session.controls_locked or session.closing:
                raise DeviceError("采集模式需要调整；请先停止记录并等待收尾，再开始监测", "unsupported")
            session.control_pending += 1
            session.configuration_version += 1
        self.repository.audit("acquisition_setup_started", device_id=session.device_id)
        try:
            update = prepare()
            if not isinstance(update, ControlUpdate) or not update.complete:
                raise DeviceError("采集模式调整未返回完整核验结果", "acquisition_setup_failed")
            self._cache_readings(session, update.readings)
            self._refresh_capabilities(session, query=False)
            self.repository.audit(
                "acquisition_setup_completed",
                device_id=session.device_id,
                update=update,
                details=getattr(adapter, "capability_details", {}),
            )
            return True
        except Exception as exc:
            self._invalidate_acquisition(session, "error")
            self.repository.audit(
                "acquisition_setup_failed",
                device_id=session.device_id,
                error=str(exc),
                details=getattr(adapter, "capability_details", {}),
            )
            raise
        finally:
            with self.lock:
                session.control_pending -= 1

    def _acquire(
        self, session: DeviceSession, request: AcquisitionRequest, submitted_at: float | None = None
    ) -> MeasurementBatch:
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
        with capture_io() as capture:
            try:
                try:
                    batch = session.adapter.acquire(request)
                except DeviceError as exc:
                    if exc.code != "acquisition_setup_required" or not self._prepare_acquisition_state(
                        session
                    ):
                        raise
                    # Only this explicit, pre-waveform setup condition permits a
                    # second read. Transport/parser failures never enter this path.
                    batch = session.adapter.acquire(request)
            except Exception as exc:
                self.repository.audit(
                    "acquisition_transition_timing"
                    if getattr(exc, "code", "") == "configuration_changed"
                    else "acquisition_io_failed",
                    device_id=session.device_id,
                    performance=capture.summary(),
                    code=getattr(exc, "code", "device_error"),
                )
                self._invalidate_acquisition(session, "error")
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
        with self.lock:
            summary = session.acquisition_statistics.add(performance, batch.validation)
        if summary is not None:
            self.repository.audit("acquisition_statistics", device_id=session.device_id, summary=summary)
        if capture.details:
            self.repository.audit(
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

    def _execute(
        self,
        session: DeviceSession,
        operation: str,
        argument: Any,
        *,
        quiet: bool = False,
        submitted_at: float | None = None,
    ) -> Any:
        device_id = session.device_id
        with self.lock:
            session.background_read = False
            if session.state in {"error", "degraded"}:
                session.pending -= 1
                if self._controls_instrument(operation, argument):
                    session.control_pending -= 1
                raise DeviceError("Previous I/O failed; queued command was not sent", "disconnected")
            session.state, session.active_task = "busy", operation
            session.background_read = quiet and operation == "read_settings"
        if not quiet:
            self.emit("task_started", device_id, operation=operation)
        try:
            adapter = session.adapter
            if adapter is None:
                raise DeviceError("No adapter", "disconnected")
            if not quiet and operation not in {"acquire", "observe"}:
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
                if not snapshot.channels:
                    raise DeviceError("等待设备通道识别，请重新连接或刷新能力", "invalid_parameter")
                result = snapshot.channels[next(iter(snapshot.channels))]
            elif operation == "refresh_capabilities":
                self._invalidate_acquisition(session, "explicit_refresh")
                result = self._refresh_capabilities(session)
                if self._prepare_acquisition_state(session):
                    result = session.capability_snapshot
            elif operation == "read_settings":
                if not quiet:
                    self._invalidate_acquisition(session, "explicit_read")
                operations, channel = argument
                result = adapter.read_settings(tuple(operations), channel)
                # Model-owned caches may now imply different channel ranges/permissions.
                # Publish only actual changes, without extra I/O or identical audit snapshots.
                if callable(getattr(adapter, "capabilities_for_channel", None)):
                    self._refresh_capabilities(session, query=False, changed_only=True)
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
                # Opted-in models own selective invalidation, including failure paths.
                if not getattr(adapter, "supports_control_updates", False):
                    self._invalidate_acquisition(session, "apply")
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
                if result.outcome == "success" and session.state != "degraded" and result.control_update:
                    self._cache_readings(session, result.control_update.readings)
                elif result.outcome != "success":
                    self._invalidate_acquisition(session, "error")
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
                result = self._acquire(session, argument, submitted_at)
            if operation in {"acquire", "observe"}:
                # Optional, model-owned readings tied to this exact acquisition. No inferred
                # settings from arbitrary configuration dictionaries or extra device queries.
                acquisition_readings = getattr(adapter, "acquisition_readings", None)
                if operation == "acquire" and callable(acquisition_readings):
                    self._cache_readings(session, acquisition_readings(result))
                result = replace(
                    result,
                    configuration={
                        **result.configuration,
                        "_session_configuration_version": session.configuration_version,
                    },
                )
            if not quiet and operation not in {"acquire", "observe"}:
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
            self._invalidate_acquisition(session, "error")
            code = getattr(exc, "code", "device_error")
            if operation == "acquire" and code == "configuration_changed":
                # A complete reply with changing axis/settings is an invalid frame,
                # not a broken transport. Never publish it or replay any control.
                with self.lock:
                    session.configuration_version += 1
                self.repository.audit("acquisition_discarded", device_id=device_id, code=code, error=str(exc))
                raise
            if code not in {
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
                    self._refresh_capabilities(session, query=False, changed_only=True)
                except Exception as exc:
                    self.repository.audit("capability_publish_failed", device_id=device_id, error=str(exc))
            with self.lock:
                session.background_read = False
                session.pending -= 1
                if self._controls_instrument(operation, argument):
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
        mode: str = GENERAL,
        cadence: str | None = None,
        record_interval: float | None = None,
        csv_interval: float = 5,
    ) -> Future[Any]:
        request.validate()
        if request.waveform_extent == "memory":
            raise DeviceError(
                "Full memory reads are not available during continuous recording", "unsupported"
            )
        validate_cadence(interval, cadence, minimum=0.01)
        if (count is not None and (type(count) is not int or count < 1)) or (
            record_interval is not None
            and (
                isinstance(record_interval, bool)
                or not isinstance(record_interval, (int, float))
                or not 0.05 <= record_interval <= 86400
            )
        ):
            raise DeviceError("Invalid stream interval/count", "invalid_parameter")
        Recording.validate_csv_interval(csv_interval)
        with self.lock:
            session = self.sessions[device_id]
            if (
                session.pending
                or session.monitoring
                or session.control_group
                or session.state != "connected"
                or session.closing
            ):
                raise DeviceError("Device has an active task", "task_busy")
            context = self.repository.new_save_context(
                mode,
                "continuous",
                device_id=device_id,
                identity=session.identity,
                adapter_id=session.config.adapter,
            )
            run_factory = None
            if request.kind in getattr(session.adapter, "recorded_run_kinds", ()):
                if record_interval is not None or count is not None:
                    raise DeviceError(
                        "Finite instrument runs record every frame and manage their own end",
                        "invalid_parameter",
                    )
                getattr(session.adapter, "validate_recorded_run")(request)
                run_factory = getattr(session.adapter, "run_acquisition")
                session.configuration_version += 1
                self._invalidate_acquisition(session, "recorded_run")
            elif request.duration is not None or request.sample_rate is not None:
                raise DeviceError("Adapter does not support finite instrument runs", "unsupported")
            recording = self.repository.create_recording(mode, context=context, csv_interval=csv_interval)
            session.direct_recording = recording
            self.recordings.append(recording)
            session.stop.clear()
            session.pending = 1
            session.active_task = "continuous_acquisition"
            session.state = "busy"

        def stream() -> list[str]:
            run = None
            stream_request = replace(
                request,
                consistency="stream"
                if getattr(session.adapter, "supports_stream_acquisition", False) and not request.initiate
                else "full",
                context_id=uuid4().hex,
            )
            self.repository.audit(
                "stream_started",
                device_id=device_id,
                request=request,
                interval=interval,
                count=count,
                mode=mode,
            )
            try:
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
                        if run is not None:
                            raise DeviceError(
                                recording.pause_reason or "Recording buffer full; instrument run stopped",
                                "recording_paused",
                            )
                        break
                    try:
                        batch = immutable_batch(
                            next(run) if run is not None else self._acquire(session, stream_request)
                        )
                    except StopIteration:
                        recording.cancel_reservation()
                        break
                    if save_due:
                        if not recording.submit(batch):
                            if run is not None:
                                raise DeviceError(
                                    recording.pause_reason
                                    or "Recording rejected frame; instrument run stopped",
                                    "recording_paused",
                                )
                            break
                        next_save = started + (record_interval or 0)
                    self.emit("measurement", device_id, batch=batch, path=recording.last_path)
                    index += 1
                    if run is None and session.stop.wait(
                        0 if cadence == "fastest" else max(0, started + interval - time.monotonic())
                    ):
                        break
                recording.finish()
                recording.thread.join()
                if run is not None and recording.error:
                    raise DeviceError(recording.error, "recording_failed")
                return list(recording.segments.paths)
            except Exception as exc:
                session.last_error, session.state = str(exc), "degraded"
                self.repository.audit("stream_failed", device_id=device_id, error=str(exc))
                self.emit("error", device_id, error=str(exc))
                raise
            finally:
                cleanup_error = None
                try:
                    if run is not None:
                        run.close()
                except Exception as exc:
                    session.last_error, session.state = str(exc), "degraded"
                    self.repository.audit("stream_cleanup_failed", device_id=device_id, error=str(exc))
                    self.emit("error", device_id, error=str(exc))
                    cleanup_error = exc
                finally:
                    recording.cancel_reservation()
                    recording.finish()
                    recording.thread.join()
                self.repository.audit(
                    "stream_stopped",
                    device_id=device_id,
                    batches=recording.committed,
                    requested_stop=session.stop.is_set(),
                )
                with self.lock:
                    session.pending, session.active_task = 0, ""
                    if session.state == "busy":
                        session.state = "connected"
                self.emit("task_finished", device_id)
                if cleanup_error is not None:
                    raise cleanup_error

        with self.lock:
            session.stream_future = session.worker.submit(stream)
            result: Future[Any] = Future()

            def completed(worker: Future[Any]) -> None:
                def converted(_finished: Future[Any]) -> None:
                    try:
                        paths = worker.result()
                        if run_factory and (recording.error or recording.csv_error):
                            raise DeviceError(recording.error or recording.csv_error, "recording_failed")
                        result.set_result(paths)
                    except Exception as exc:
                        result.set_exception(exc)

                recording.finished.add_done_callback(converted)

            session.stream_future.add_done_callback(completed)
            return result

    def stop_acquisition(self, device_id: str) -> None:
        with self.lock:
            self.sessions[device_id].stop.set()

    def disconnect(self, device_id: str, *, drain_reads: bool = False) -> None:
        with self.lock:
            session = self.sessions[device_id]
            background_only = drain_reads and session.pending == 1 and session.background_read
            if (
                session.closing
                or session.control_pending
                or session.control_group
                or (session.pending and not background_only)
                or session.monitoring
                or session.state == "connecting"
            ):
                raise DeviceError("Stop and await active tasks before disconnecting", "task_busy")
            # Reserve teardown atomically: no poll may race the idle check and queue after LOCAL.
            session.closing = True

        def teardown() -> None:
            adapter = session.adapter
            if adapter is None:
                return
            try:
                release = getattr(adapter, "release_control", None)
                if release and session.identity is not None:
                    if session.state in {"error", "degraded"}:
                        raise DeviceError("返回本地控制未确认：通信异常，未发送释放命令", "disconnected")
                    try:
                        release()
                    except Exception as exc:
                        raise DeviceError(f"返回本地控制未确认：{exc}", "disconnected") from exc
                    self.repository.audit(
                        "control_released",
                        device_id=device_id,
                        note="Release command sent; physical panel state is not independently verified",
                    )
            except Exception as exc:
                self.repository.audit("control_release_failed", device_id=device_id, error=str(exc))
                raise
            finally:
                adapter.close()

        try:
            session.worker.submit(teardown).result()
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
                if session.direct_recording:
                    session.direct_recording.finish().result()
                self.disconnect(device_id, drain_reads=True)
            except Exception as exc:
                errors.append(exc)
        for recording in self.recordings:
            recording.finish().result()
        if errors:
            raise errors[0]
