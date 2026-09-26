import time
from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand

from ..hardware.benchmark import install_metadata_sinks, qualifies
from .samples import binary
from .test_adapter import device
from .test_context import full


def connected(tmp_path, exchanges):
    app = ApplicationRuntime(root=tmp_path, hardware=False)
    adapter, wire = device([("*IDN?", b"LECROY,WAVERUNNER9404M,TEST,9.2.0"), *exchanges])
    adapter.supports_live_acquisition = adapter.supports_stream_acquisition = True
    # This strict script tests acquisition invalidation; CVAR has its own wire tests.
    adapter.refresh_capabilities = lambda: None
    app.sessions.factory = lambda config: adapter
    key = app.add(ConnectionConfig("mock://scope-context", "lecroy.9404m", "mock"))
    app.connect(key, configure_acquisition=False).result(3)
    return app, key, adapter, wire


def test_session_refresh_and_apply_invalidate_without_cached_timestamp_forgery(tmp_path):
    settings = [("TDIV?", b"TDIV 0.001 S")]
    exchanges = (
        full()
        + [("C1:WF? ALL", binary())]
        + settings
        + full()
        + [("TDIV 0.002", b""), ("*OPC?", b"1"), ("TDIV?", b"TDIV 0.002 S")]
        + full()
    )
    app, key, adapter, wire = connected(tmp_path, exchanges)
    with app:
        req = AcquisitionRequest("waveform", consistency="live", context_id="one")
        first = app.sessions.submit(key, "acquire", req).result(3)
        cached = app.sessions.submit(key, "acquire", req).result(3)
        assert cached.validation == "cached" and first.settings_timestamps == cached.settings_timestamps
        app.sessions.submit(key, "read_settings", (("timebase",), 1)).result(3)
        assert adapter._acquisition_context is None
        assert app.sessions.submit(key, "acquire", req).result(3).validation == "full"
        assert (
            app.sessions.submit(key, "apply", DeviceCommand("timebase", {"value": 0.002})).result(3).outcome
            == "success"
        )
        assert adapter._acquisition_context is None
        assert app.sessions.submit(key, "acquire", req).result(3).validation == "full"
        assert not wire.exchanges


def test_metadata_recording_drains_real_public_queue_without_curve_files(tmp_path):
    adapter, _ = device(full() + [("C1:WF? ALL", binary())])
    request = AcquisitionRequest("waveform", consistency="stream", context_id="record")
    adapter.acquire(request)
    batch = adapter.acquire(request)
    repository = SimpleNamespace(root=tmp_path)
    report = {}
    install_metadata_sinks(SimpleNamespace(repository=repository), report)
    with pytest.raises(AssertionError, match="forbidden"):
        repository.export(batch)
    recorder = repository.create_recording("electronic", max_frames=2)
    for _ in range(2):
        assert recorder.reserve(1000)
        assert recorder.submit(batch)
    recorder.finish().result(3)
    state = recorder.snapshot()
    assert state["binary_done"] and state["committed"] == state["received"] == 2
    assert not state["pending"] and not list(tmp_path.rglob("*"))
    assert len(report["saved_frames"]) == 2


def test_corrupt_cached_frame_not_displayed_or_saved_and_recording_unlocks(tmp_path):
    app, key, adapter, wire = connected(tmp_path, full() + [("C1:WF? ALL", binary()[:-4])])
    report = {}
    with app:
        install_metadata_sinks(app, report)
        # Record from startup, ensuring both frames share the same stream context.
        with app.monitoring.lock:
            app.monitoring.start(key, (AcquisitionRequest("waveform"),), interval=0.05, record_interval=0.05)
            app.monitoring.set_recording(key, True)
        deadline = time.monotonic() + 3
        task = app.monitoring.tasks[key]
        while task.running and time.monotonic() < deadline:
            time.sleep(0.01)
        app.monitoring.stop(key).result(3)
        assert not task.running and adapter._acquisition_context is None
        assert len(report.get("saved_frames", [])) <= 1
        assert not app.sessions.snapshots()[0]["controlsLocked"]
        assert wire.history.count("C1:WF? ALL") == 2  # No fault replay.


def test_performance_gate_requires_median_and_tail_improvement():
    baseline = {"acquisition_seconds": {"median": 1.0, "p95": 2.0}}
    assert qualifies(baseline, {"acquisition_seconds": {"median": 0.8, "p95": 2.0}})
    assert not qualifies(baseline, {"acquisition_seconds": {"median": 0.81, "p95": 1.0}})
    assert not qualifies(baseline, {"acquisition_seconds": {"median": 0.5, "p95": 2.01}})
