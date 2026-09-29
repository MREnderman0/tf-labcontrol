"""Generic recorded-run lifecycle, including failure and buffer-pressure cleanup."""

import threading

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, Measurement, MeasurementBatch
from lab_control.storage.recording import read_frames
from lab_control.storage.repository import Repository


class FiniteInstrument(SimulatedInstrument):
    recorded_run_kinds = ("finite_test",)
    cleanup_failure = False
    closed_run = False
    released = threading.Event()

    def validate_recorded_run(self, request):
        request.validate()

    def run_acquisition(self, request, stop):
        try:
            for index in range(3):
                yield MeasurementBatch(self.identity, (Measurement("voltage", "V", [index]),), {})
                if stop.is_set():
                    break
        finally:
            self.closed_run = True
            self.released.set()
            if self.cleanup_failure:
                raise DeviceError("Injected final cleanup failure", "cleanup_failed")


@pytest.fixture
def manager(tmp_path):
    repository = Repository(tmp_path)
    sessions = SessionManager(ResourceLeases(), repository, FiniteInstrument)
    sessions.connect("test", ConnectionConfig("mock://finite", "rigol.dg5504pro", "mock")).result(3)
    try:
        yield sessions, sessions.sessions["test"].adapter, repository
    finally:
        sessions.close()
        repository.close()


def test_finite_run_ends_without_count_records_every_frame_and_cleans_up(manager):
    sessions, instrument, _ = manager
    future = sessions.start_acquisition("test", AcquisitionRequest("finite_test"))
    paths = future.result(15)
    assert instrument.closed_run
    from pathlib import Path

    assert sum(len(list(read_frames(Path(path)))) for path in paths) == 3
    assert sessions.snapshots()[0]["activeTask"] == ""
    assert sessions.snapshots()[0]["connectionState"] == "connected"


def test_cleanup_failure_still_drains_recording_and_releases_task(manager):
    sessions, instrument, _ = manager
    instrument.cleanup_failure = True
    future = sessions.start_acquisition("test", AcquisitionRequest("finite_test"))
    with pytest.raises(DeviceError, match="cleanup"):
        future.result(15)
    state = sessions.snapshots()[0]
    assert state["activeTask"] == "" and state["connectionState"] == "degraded"
    assert sessions.sessions["test"].direct_recording.binary_done
    assert instrument.closed_run


def test_buffer_pressure_closes_generator_once_without_silent_success(manager, monkeypatch):
    sessions, instrument, repository = manager
    create = repository.create_recording

    def recording(*args, **kwargs):
        result = create(*args, **kwargs)
        reserve = result.reserve
        calls = 0

        def limited(size):
            nonlocal calls
            calls += 1
            return reserve(size) if calls == 1 else False

        result.reserve = limited
        return result

    monkeypatch.setattr(repository, "create_recording", recording)
    future = sessions.start_acquisition("test", AcquisitionRequest("finite_test"))
    with pytest.raises(DeviceError, match="buffer"):
        future.result(15)
    assert instrument.closed_run
    assert sessions.sessions["test"].direct_recording.committed == 1
    assert sessions.snapshots()[0]["activeTask"] == ""


def test_explicit_record_thinning_is_rejected_for_contiguous_runs(manager):
    sessions, _, _ = manager
    with pytest.raises(DeviceError, match="every frame"):
        sessions.start_acquisition("test", AcquisitionRequest("finite_test"), record_interval=5)


def test_run_factory_failure_also_releases_recording_and_session(manager):
    sessions, instrument, _ = manager

    def fail(*_):
        raise DeviceError("Initialization failed")

    instrument.run_acquisition = fail
    with pytest.raises(DeviceError, match="Initialization"):
        sessions.start_acquisition("test", AcquisitionRequest("finite_test")).result(15)
    assert not sessions.snapshots()[0]["activeTask"]
    assert sessions.sessions["test"].direct_recording.binary_done
