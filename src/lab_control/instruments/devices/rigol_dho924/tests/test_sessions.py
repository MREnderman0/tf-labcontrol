import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring import merge_requests
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.storage.recording import read_frames

from ..adapter import DHO924Adapter
from ..profile import profile
from .fake import Peer


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Expected scheduler state was not reached")


@pytest.fixture
def runtime(tmp_path):
    config = ConnectionConfig("mock://dho924-integration", "rigol.dho924", "mock", expected_serial="DHO-TEST")
    app = ApplicationRuntime((config,), root=tmp_path, hardware=False)
    peer = Peer()
    app.sessions.factory = lambda cfg: DHO924Adapter(peer, profile(), cfg)
    key = app.add(config)
    app.connect(key).result(5)
    try:
        yield app, key, peer
    finally:
        app.close()


def test_memory_serializes_after_live_read_and_resumes_without_export(runtime):
    app, key, peer = runtime
    obj = app.sessions.sessions[key].adapter
    entered, release = threading.Event(), threading.Event()
    original = obj.acquire

    def acquire(request):
        if request.waveform_extent != "memory" and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(request)

    obj.acquire = acquire
    request = AcquisitionRequest("waveform", (1, 2), confirmed=True)
    app.monitoring.start(key, (request,), interval=0.05)
    assert entered.wait(3)
    try:
        memory = app.monitoring.read_memory_waveform(key, request)
        assert not memory.done() and app.sessions.snapshots()[0]["controlGroup"]
        with pytest.raises(DeviceError):
            app.monitoring.set_recording(key, True)
    finally:
        release.set()
    frame = memory.result(5)
    assert len(frame.batch.measurements[0].values) == 10000
    assert frame.request.waveform_extent == "memory"
    assert not app.sessions.snapshots()[0]["controlGroup"]
    assert app.monitoring.snapshots()[key]["running"]
    assert peer.state[":TRIG:STAT"] == "AUTO"
    wait_for(lambda: app.monitoring.tasks[key].sequence >= 2)
    assert not (app.repository.root / "data").exists()
    app.monitoring.stop(key).result(5)
    history = list(peer.history)
    app.monitoring.export_displayed_frame(key, frame, "electronic").result(5)
    assert peer.history == history


def test_recording_locks_memory_and_settings_and_drains_exact_arrays(runtime):
    app, key, peer = runtime
    app.monitoring.start(key, (AcquisitionRequest("waveform", (1, 2)),), interval=0.05, record_interval=None)
    wait_for(lambda: app.monitoring.latest(key, "waveform") is not None)
    app.monitoring.set_recording(key, True)
    with pytest.raises(DeviceError):
        app.monitoring.read_memory_waveform(key, AcquisitionRequest("waveform", confirmed=True))
    memory = AcquisitionRequest("waveform", confirmed=True, waveform_extent="memory")
    with pytest.raises(DeviceError, match="锁定"):
        app.sessions.submit(key, "acquire", memory)
    with pytest.raises(DeviceError, match="锁定"):
        app.monitoring.capture_and_export(key, memory, "electronic")
    with pytest.raises(DeviceError):
        app.sessions.submit(key, "apply", DeviceCommand("offset", {"value": 0.01}))
    task = app.monitoring.tasks[key]
    wait_for(lambda: task.recorder and task.recorder.snapshot()["committed"] >= 2)
    app.monitoring.stop(key).result(10)
    task.recorder.finished.result(10)
    status = task.recorder.snapshot()
    assert status["received"] == status["committed"] == status["converted"]
    assert status["binary_done"] and not app.sessions.snapshots()[0]["controlsLocked"]
    saved = next(read_frames(next(task.recorder.raw_folder.glob("segment-*.sqlite3"))))
    assert saved["performance"]["waveform_extent"] == "screen"
    assert saved["measurements"][0]["metadata"]["scope_display"]["units_per_div"] == 0.05
    expected = (np.arange(1000) % 40) * (0.05 / 7500)
    np.testing.assert_allclose(saved["measurements"][0]["values"], expected)


def test_new_extent_is_explicit_and_legacy_models_reject_memory_without_io(tmp_path):
    request = AcquisitionRequest("waveform", confirmed=True, waveform_extent="memory")
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        key = app.add(ConnectionConfig("mock://legacy", "lecroy.9404m", "mock"))
        app.connect(key).result(5)
        with pytest.raises(DeviceError, match="extent"):
            app.sessions.submit(key, "acquire", request).result(5)
        with pytest.raises(DeviceError):
            app.monitoring.start(key, (request,))
        with pytest.raises(DeviceError, match="continuous recording"):
            app.sessions.start_acquisition(key, request, count=1)
        assert not app.sessions.recordings
    with pytest.raises(DeviceError):
        merge_requests((replace(request, waveform_extent="screen"), request))


def test_direct_memory_request_blocks_recording_until_worker_finishes(runtime):
    app, key, peer = runtime
    obj = app.sessions.sessions[key].adapter
    entered, release = threading.Event(), threading.Event()
    original = obj.acquire

    def acquire(request):
        if request.waveform_extent == "memory":
            entered.set()
            assert release.wait(3)
        return original(request)

    obj.acquire = acquire
    app.monitoring.start(key, (AcquisitionRequest("waveform"),), interval=0.5)
    wait_for(lambda: app.monitoring.latest(key, "waveform") is not None)
    request = AcquisitionRequest("waveform", confirmed=True, waveform_extent="memory")
    future = app.sessions.submit(key, "acquire", request)
    try:
        assert entered.wait(3)
        assert app.sessions.snapshots()[0]["controlPending"]
        with pytest.raises(DeviceError):
            app.monitoring.set_recording(key, True)
    finally:
        release.set()
    future.result(5)
    assert not app.sessions.snapshots()[0]["controlPending"]
    assert peer.state[":TRIG:STAT"] == "AUTO"
    app.monitoring.stop(key).result(5)


def test_failure_releases_memory_group_and_retains_last_frame(runtime):
    app, key, peer = runtime
    app.monitoring.start(key, (AcquisitionRequest("waveform"),), interval=0.5)
    wait_for(lambda: app.monitoring.latest(key, "waveform") is not None)
    frame = app.monitoring.latest(key, "waveform")
    peer.fail = ":WAV:FORM WORD"
    with pytest.raises(DeviceError):
        app.monitoring.read_memory_waveform(key, AcquisitionRequest("waveform", confirmed=True)).result(5)
    assert not app.sessions.snapshots()[0]["controlGroup"]
    assert not app.monitoring.snapshots()[key]["running"]
    assert app.monitoring.tasks[key].frames["waveform", ""] is frame
    assert peer.state[":TRIG:STAT"] == "STOP"
