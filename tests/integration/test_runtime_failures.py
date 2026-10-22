"""Failure boundaries must settle software work without repeating instrument I/O."""

import threading

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.monitoring import MonitoringService
from lab_control.application.sessions import SessionManager
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.storage.repository import Repository


def connected(tmp_path):
    repository = Repository(tmp_path)
    sessions = SessionManager(ResourceLeases(), repository, SimulatedInstrument)
    sessions.connect("test", ConnectionConfig("mock://failure", "rigol.dg5504pro", "mock")).result(3)
    return repository, sessions


def test_stream_start_audit_failure_settles_future_and_releases_session(tmp_path, monkeypatch):
    repository, sessions = connected(tmp_path)
    audit = repository.audit

    def fail(event, **details):
        if event == "stream_started":
            raise OSError("audit disk unavailable")
        audit(event, **details)

    monkeypatch.setattr(repository, "audit", fail)
    try:
        future = sessions.start_acquisition("test", AcquisitionRequest("waveform", points=32), count=1)
        with pytest.raises(OSError, match="audit disk unavailable"):
            future.result(3)
        state = sessions.snapshots()[0]
        assert state["pending"] == 0
        assert state["activeTask"] == ""
        assert state["connectionState"] == "connected"
    finally:
        # Also lets this regression fail safely against the old implementation.
        session = sessions.sessions["test"]
        session.direct_recording.cancel_reservation()
        session.direct_recording.finish().result(5)
        session.pending, session.active_task = 0, ""
        sessions.close()
        repository.close()


@pytest.mark.parametrize("stage", ["binary", "csv"])
def test_direct_recording_failure_is_reported_without_degrading_device(tmp_path, monkeypatch, stage):
    repository, sessions = connected(tmp_path)

    def fail(*args, **kwargs):
        raise OSError(f"{stage} unavailable")

    monkeypatch.setattr(
        "lab_control.storage.recording.SegmentWriter.append"
        if stage == "binary"
        else "lab_control.storage.recording.subprocess.Popen",
        fail,
    )
    try:
        future = sessions.start_acquisition("test", AcquisitionRequest("waveform", points=32), count=1)
        with pytest.raises(DeviceError, match=f"{stage} unavailable") as failure:
            future.result(8)
        assert failure.value.code == "recording_failed"
        assert failure.value.outcome.received == 1
        assert failure.value.outcome.committed == (0 if stage == "binary" else 1)
        assert sessions.snapshots()[0]["connectionState"] == "connected"
        assert sessions.snapshots()[0]["pending"] == 0
    finally:
        sessions.close()
        repository.close()


def test_pause_audit_failure_does_not_escape_scheduler(tmp_path, monkeypatch):
    repository, sessions = connected(tmp_path)
    monitoring = MonitoringService(sessions, repository)
    try:
        monitoring.start("test", (AcquisitionRequest("waveform", points=32),), interval=1)

        def fail(*args, **kwargs):
            raise OSError("audit disk unavailable")

        monkeypatch.setattr(repository, "audit", fail)
        monitoring._pause("test", "original acquisition failure")
        assert monitoring.thread.is_alive()
        assert monitoring.snapshots()["test"]["error"] == "original acquisition failure"
    finally:
        monkeypatch.undo()
        monitoring.close()
        sessions.close()
        repository.close()


def test_failed_writer_releases_queued_frames_but_preserves_counts(tmp_path):
    from lab_control.storage.recording import Recording

    repository, sessions = connected(tmp_path)
    batch = sessions.submit("test", "acquire", AcquisitionRequest("waveform", points=32)).result(3)
    recording = Recording(tmp_path, "electronic")
    entered, release = threading.Event(), threading.Event()

    def fail(*args):
        entered.set()
        assert release.wait(3)
        raise OSError("binary unavailable")

    recording.segments.append = fail
    try:
        assert recording.reserve(65536) and recording.submit(batch)
        assert entered.wait(3)
        assert recording.reserve(65536) and recording.submit(batch)
        assert recording.reserve(65536) and recording.submit(batch)
        release.set()
        recording.finish().result(5)
        assert recording.snapshot()["pending"] == 3
        assert recording.snapshot()["received"] == 3
        assert not recording.queue
        assert recording.snapshot()["buffer_bytes"] == 0
    finally:
        release.set()
        recording.finish().result(5)
        sessions.close()
        repository.close()


def test_direct_buffer_pressure_reports_partial_outcome(tmp_path, monkeypatch):
    repository, sessions = connected(tmp_path)
    create = repository.create_recording

    def bounded(*args, **kwargs):
        return create(*args, **kwargs, max_bytes=1)

    monkeypatch.setattr(repository, "create_recording", bounded)
    try:
        future = sessions.start_acquisition("test", AcquisitionRequest("waveform", points=32), count=1)
        with pytest.raises(DeviceError) as failure:
            future.result(5)
        assert failure.value.code == "recording_paused"
        assert failure.value.outcome.received == 0
        assert failure.value.outcome.pause_reason
        assert sessions.snapshots()[0]["connectionState"] == "connected"
    finally:
        sessions.close()
        repository.close()


def test_finished_recordings_retain_only_latest_batch(tmp_path):
    repository, sessions = connected(tmp_path)
    try:
        for _ in range(3):
            sessions.start_acquisition("test", AcquisitionRequest("waveform", points=32), count=1).result(8)
        assert len(sessions.recordings) == 1
        assert sessions.recordings[0] is sessions.sessions["test"].direct_recording
    finally:
        sessions.close()
        repository.close()


def test_summary_failure_still_settles_recording(tmp_path, monkeypatch):
    from lab_control.storage.recording import Recording

    def fail(*args):
        raise ValueError("invalid summary")

    monkeypatch.setattr(Recording, "_describe", fail)
    recording = Recording(tmp_path, "electronic")
    assert recording.finish().result(3) == []
    assert recording.binary_done
    assert "invalid summary" in recording.csv_error


def test_audit_failure_evidence_is_bounded_and_does_not_recurse():
    from lab_control.storage.audit import AuditSink

    calls = []

    def fail(event, **details):
        calls.append(event)
        raise OSError("logs unavailable")

    audit = AuditSink(fail)
    for index in range(40):
        assert not audit.try_record(str(index))
    assert len(calls) == 40
    assert len(audit.failures()) == 32
    assert audit.failures()[0]["event"] == "8"


def test_runtime_close_attempts_every_resource_after_failures():
    from types import SimpleNamespace

    from lab_control.application.runtime import ApplicationRuntime

    runtime = ApplicationRuntime.__new__(ApplicationRuntime)
    calls = []

    def resource(name):
        def close():
            calls.append(name)
            if name in {"monitoring", "repository"}:
                raise OSError(name + " close failed")

        return SimpleNamespace(close=close, lock=threading.RLock())

    for name in (
        "bulk_connection",
        "monitoring",
        "discovery",
        "sessions",
        "visa",
        "repository",
        "process_lease",
    ):
        setattr(runtime, name, resource(name))
    runtime.closed = False
    runtime._close_lock = threading.Lock()
    with pytest.raises(OSError, match="monitoring close failed") as failure:
        runtime.close()
    assert calls[-1] == "process_lease" and len(calls) == 7
    assert runtime.closed
    assert "repository close failed" in " ".join(failure.value.__notes__)
    runtime.close()
    assert len(calls) == 7


def test_required_control_audit_failure_sends_no_command(tmp_path, monkeypatch):
    from lab_control.domain.instruments import DeviceCommand

    repository, sessions = connected(tmp_path)
    calls = []
    monkeypatch.setattr(sessions.sessions["test"].adapter, "apply", calls.append)
    audit = repository.audit

    def fail(event, **details):
        if event == "operation_requested":
            raise OSError("required audit unavailable")
        return audit(event, **details)

    monkeypatch.setattr(repository, "audit", fail)
    try:
        with pytest.raises(OSError, match="required audit unavailable"):
            sessions.submit("test", "apply", DeviceCommand("frequency", {"value": 1000})).result(3)
        assert not calls
        assert sessions.snapshots()[0]["connectionState"] == "connected"
        assert sessions.snapshots()[0]["pending"] == 0
    finally:
        sessions.close()
        repository.close()


@pytest.mark.parametrize("audit_fails", [False, True])
def test_acquisition_setup_requires_audit_and_releases_control_guard(tmp_path, monkeypatch, audit_fails):
    from lab_control.domain.instruments import ControlUpdate

    repository, sessions = connected(tmp_path)
    session = sessions.sessions["test"]
    adapter = session.adapter
    calls = []
    audit = repository.audit

    def record(event, **details):
        if event == "acquisition_setup_started":
            calls.append("audit")
            if audit_fails:
                raise OSError("required setup audit unavailable")
        return audit(event, **details)

    def prepare():
        calls.append("prepare")
        return ControlUpdate((), (), True)

    monkeypatch.setattr(repository, "audit", record)
    monkeypatch.setattr(adapter, "acquisition_setup_needed", lambda: True, raising=False)
    monkeypatch.setattr(adapter, "prepare_acquisition_state", prepare, raising=False)
    try:
        future = sessions.submit("test", "refresh_capabilities")
        if audit_fails:
            with pytest.raises(OSError, match="required setup audit unavailable"):
                future.result(3)
            assert calls == ["audit"]
        else:
            assert future.result(3) is session.capability_snapshot
            assert calls == ["audit", "prepare"]
        assert session.control_pending == 0
        assert session.pending == 0
        assert session.active_task == ""
        assert session.worker.submit(lambda: "drained").result(3) == "drained"
    finally:
        sessions.close()
        repository.close()


def test_runtime_construction_unwinds_started_scheduler(tmp_path, monkeypatch):
    from lab_control.application.runtime import ApplicationRuntime

    closed = []
    close = MonitoringService.close

    def observe_close(self):
        close(self)
        closed.append(not self.thread.is_alive())

    def fail(*args):
        raise RuntimeError("bulk construction failed")

    monkeypatch.setattr(MonitoringService, "close", observe_close)
    monkeypatch.setattr("lab_control.application.bulk_connection.BulkConnectionService", fail)
    with pytest.raises(RuntimeError, match="bulk construction failed"):
        ApplicationRuntime(root=tmp_path, hardware=False)
    assert closed == [True]


def test_accepted_stream_future_cannot_cancel_instrument_work(tmp_path, monkeypatch):
    repository, sessions = connected(tmp_path)
    adapter = sessions.sessions["test"].adapter
    acquire = adapter.acquire
    entered, release = threading.Event(), threading.Event()
    calls = []

    def delayed(request):
        calls.append(request)
        entered.set()
        assert release.wait(3)
        return acquire(request)

    monkeypatch.setattr(adapter, "acquire", delayed)
    try:
        future = sessions.start_acquisition("test", AcquisitionRequest("waveform", points=32), count=1)
        assert entered.wait(3)
        assert not future.cancel()
        release.set()
        assert future.result(8)
        assert len(calls) == 1
        assert sessions.snapshots()[0]["pending"] == 0
    finally:
        release.set()
        sessions.close()
        repository.close()


def test_disconnect_preserves_release_error_and_still_removes_session(tmp_path, monkeypatch):
    repository, sessions = connected(tmp_path)
    adapter = sessions.sessions["test"].adapter

    def release():
        raise OSError("release failed")

    def close():
        raise OSError("adapter close failed")

    monkeypatch.setattr(adapter, "release_control", release, raising=False)
    monkeypatch.setattr(adapter, "close", close)
    try:
        with pytest.raises(DeviceError, match="release failed") as failure:
            sessions.disconnect("test")
        assert "adapter close failed" in " ".join(failure.value.__notes__)
        assert "test" not in sessions.sessions
        assert not sessions.leases.owner("mock://failure")
    finally:
        sessions.close()
        repository.close()


def test_runtime_context_preserves_body_error_and_cleanup_notes(monkeypatch):
    from lab_control.application.runtime import ApplicationRuntime

    runtime = ApplicationRuntime.__new__(ApplicationRuntime)
    primary = ValueError("operation failed")
    primary.add_note("original detail")
    cleanup = OSError("close failed")
    cleanup.add_note("secondary cleanup failed")

    def fail():
        raise cleanup

    monkeypatch.setattr(runtime, "close", fail)
    with pytest.raises(ValueError) as failure:
        with runtime:
            raise primary
    assert failure.value is primary
    assert "original detail" in primary.__notes__
    assert "close failed" in " ".join(primary.__notes__)
    assert "secondary cleanup failed" in " ".join(primary.__notes__)


def test_runtime_context_propagates_cleanup_error_without_body_error(monkeypatch):
    from lab_control.application.runtime import ApplicationRuntime

    runtime = ApplicationRuntime.__new__(ApplicationRuntime)
    cleanup = OSError("close failed")

    def fail():
        raise cleanup

    monkeypatch.setattr(runtime, "close", fail)
    with pytest.raises(OSError) as failure:
        with runtime:
            pass
    assert failure.value is cleanup
