from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from uuid import uuid4

from lab_control.application.acquisition_pipeline import validate_cadence
from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.recorded_acquisition import RecordedAcquisitionRunner
from lab_control.application.recordings import retain_recording
from lab_control.application.session_operations import SessionOperations
from lab_control.domain.instruments import (
    AcquisitionRequest,
    CapabilitySnapshot,
    ControlUpdate,
    DeviceError,
    DeviceIdentity,
    InstrumentAdapter,
    MeasurementBatch,
    ParameterReading,
    jsonable,
    utc_now,
)
from lab_control.domain.recording import RecordingError
from lab_control.domain.workspace import GENERAL
from lab_control.storage.audit import AuditSink
from lab_control.storage.recording import Recording
from lab_control.storage.repository import Repository
from lab_control.transport.diagnostics import AcquisitionStatistics


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
        self.audit = getattr(repository, "audit_sink", None) or AuditSink(
            lambda event, **details: repository.audit(event, **details)
        )
        self.sessions: dict[str, DeviceSession] = {}
        self.lock = threading.RLock()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self.recordings: list[Recording] = []
        self.maintenance_active = False
        self._closing = False
        self._closed = False
        self._close_lock = threading.Lock()
        self.operations = SessionOperations(self)
        self.acquisition_runner = RecordedAcquisitionRunner(self)

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
                self.audit.try_record("listener_error", error=str(exc))

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
                self._closing
                or session is None
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
        self.audit.try_record("capabilities_refreshed", device_id=session.device_id, snapshot=snapshot)
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
        # Reservation and worker submission are one admission transaction. Close
        # must not drain the worker in the gap before opening has been queued.
        with self.lock:
            return self._connect_locked(
                device_id,
                config,
                expected_firmware=expected_firmware,
                configure_acquisition=configure_acquisition,
            )

    def _connect_locked(
        self,
        device_id: str,
        config: ConnectionConfig,
        *,
        expected_firmware: str,
        configure_acquisition: bool,
    ) -> Future[DeviceIdentity]:
        with self.lock:
            if self._closing:
                raise DeviceError("Session manager is closing", "runtime_closing")
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
                        self.audit.try_record(
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
                self.audit.try_record("connected", device_id=device_id, identity=identity, config=config)
                self.emit("connected", device_id, identity=jsonable(identity))
                return identity
            except Exception as exc:
                if session.adapter:
                    try:
                        session.adapter.close()
                    except Exception as cleanup_error:
                        self.audit.try_record("cleanup_failed", device_id=device_id, error=str(cleanup_error))
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
            if self._closing:
                raise DeviceError("Session manager is closing", "runtime_closing")
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
                self._closing
                or session.closing
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
        try:
            # Setup can write instrument state, so persist its admission before I/O.
            # Keep this inside the cleanup boundary so audit failure releases the guard.
            self.repository.audit("acquisition_setup_started", device_id=session.device_id)
            update = prepare()
            if not isinstance(update, ControlUpdate) or not update.complete:
                raise DeviceError("采集模式调整未返回完整核验结果", "acquisition_setup_failed")
            self._cache_readings(session, update.readings)
            self._refresh_capabilities(session, query=False)
            self.audit.try_record(
                "acquisition_setup_completed",
                device_id=session.device_id,
                update=update,
                details=getattr(adapter, "capability_details", {}),
            )
            return True
        except Exception as exc:
            self._invalidate_acquisition(session, "error")
            self.audit.try_record(
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
        return self.operations.acquire(session, request, submitted_at)

    def _execute(
        self,
        session: DeviceSession,
        operation: str,
        argument: Any,
        *,
        quiet: bool = False,
        submitted_at: float | None = None,
    ) -> Any:
        return self.operations.execute(session, operation, argument, quiet=quiet, submitted_at=submitted_at)

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
                self._closing
                or session.pending
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
            retain_recording(self.recordings, recording, self.lock)
            session.stop.clear()
            session.pending = 1
            session.active_task = "continuous_acquisition"
            session.state = "busy"

            session.stream_future = session.worker.submit(
                self.acquisition_runner.run,
                session,
                request,
                recording,
                interval=interval,
                count=count,
                mode=mode,
                cadence=cadence,
                record_interval=record_interval,
                run_factory=run_factory,
            )
            result: Future[Any] = Future()
            # Publication cancellation cannot undo already submitted instrument I/O.
            result.set_running_or_notify_cancel()

            def completed(worker: Future[Any]) -> None:
                def converted(_finished: Future[Any]) -> None:
                    try:
                        paths = worker.result()
                        if recording.error or recording.csv_error:
                            raise RecordingError(recording.outcome())
                        result.set_result(paths)
                    except RecordingError as exc:
                        # The binary worker can fail before CSV finishes. Publish
                        # the final evidence, preserving the failure classification.
                        failure = RecordingError(recording.outcome(), exc.code)
                        for note in getattr(exc, "__notes__", ()):
                            failure.add_note(note)
                        result.set_exception(failure)
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
            failure = None
            try:
                release = getattr(adapter, "release_control", None)
                if release and session.identity is not None:
                    if session.state in {"error", "degraded"}:
                        raise DeviceError("返回本地控制未确认：通信异常，未发送释放命令", "disconnected")
                    try:
                        release()
                    except Exception as exc:
                        raise DeviceError(f"返回本地控制未确认：{exc}", "disconnected") from exc
                    self.audit.try_record(
                        "control_released",
                        device_id=device_id,
                        note="Release command sent; physical panel state is not independently verified",
                    )
            except Exception as exc:
                self.audit.try_record("control_release_failed", device_id=device_id, error=str(exc))
                failure = exc
            finally:
                try:
                    adapter.close()
                except Exception as exc:
                    if failure is None:
                        failure = exc
                    else:
                        failure.add_note(f"Adapter close failed: {exc}")
            if failure is not None:
                raise failure

        errors = []
        try:
            session.worker.submit(teardown).result()
        except Exception as exc:
            errors.append(exc)
        for cleanup in (lambda: session.worker.shutdown(wait=True), lambda: self.leases.release(device_id)):
            try:
                cleanup()
            except Exception as exc:
                errors.append(exc)
        with self.lock:
            del self.sessions[device_id]
        self.audit.try_record(
            "disconnected", device_id=device_id, note="Physical output state is unchanged/unknown"
        )
        self.emit("disconnected", device_id)
        if errors:
            for secondary in errors[1:]:
                errors[0].add_note(f"Additional disconnect failure: {secondary}")
            raise errors[0]

    def close(self) -> None:
        with self._close_lock:
            self._close_once()

    def _close_once(self) -> None:
        if self._closed:
            return
        with self.lock:
            self._closing = True
        errors = []
        for session in tuple(self.sessions.values()):
            session.stop.set()
        for device_id, session in tuple(self.sessions.items()):
            try:
                session.worker.submit(lambda: None).result()
            except Exception as exc:
                errors.append(exc)
            try:
                if session.direct_recording:
                    session.direct_recording.finish().result()
            except Exception as exc:
                errors.append(exc)
            try:
                self.disconnect(device_id, drain_reads=True)
            except Exception as exc:
                errors.append(exc)
        for recording in tuple(self.recordings):
            try:
                recording.finish().result()
            except Exception as exc:
                errors.append(exc)
        self._closed = True
        if errors:
            for secondary in errors[1:]:
                errors[0].add_note(f"Additional cleanup failure: {secondary}")
            raise errors[0]
