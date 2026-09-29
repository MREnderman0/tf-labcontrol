import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring import merge_requests
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, ApplyResult, DeviceCommand, DeviceError
from lab_control.storage.recording import read_frames


def wait_for(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Monitoring did not reach the expected state")


def test_monitor_serializes_control_shares_channels_and_records_only_on_request(tmp_path):
    config = ConnectionConfig("mock://scope", "rs.rtm3004", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device).result()
        session = runtime.sessions.sessions[device]
        original_id = session.session_id
        adapter = session.adapter
        reading, release = threading.Event(), threading.Event()
        original_acquire, original_apply = adapter.acquire, adapter.apply
        events = []

        def acquire(request):
            events.append("read-start")
            reading.set()
            assert release.wait(2)
            result = original_acquire(request)
            events.append("read-end")
            return result

        def apply(command):
            events.append("apply")
            return original_apply(command)

        adapter.acquire, adapter.apply = acquire, apply
        request = AcquisitionRequest("waveform", (1,), 64)
        try:
            runtime.monitoring.start(device, (request, replace(request, channels=(2,))), interval=0.05)
            assert reading.wait(2)
            future = runtime.monitoring.apply(device, DeviceCommand("timebase", {"value": 0.002}))
            assert not future.done()
            with pytest.raises(DeviceError, match="Stop"):
                runtime.sessions.disconnect(device)
            scan = runtime.discovery.scan().result(timeout=2)
            assert scan["items"][0]["reason"] == "cached_active_session"
            assert events == ["read-start"]
        finally:
            release.set()
        assert future.result(timeout=2).outcome == "success"
        wait_for(lambda: runtime.monitoring.latest(device, "waveform") is not None)
        frame = runtime.monitoring.latest(device, "waveform")
        assert events.index("apply") > events.index("read-end")
        assert frame.configuration_version == 1
        assert frame.request.channels == (1, 2)
        assert session.session_id == original_id
        assert np.isclose(frame.batch.measurements[0].axis[-1], 0.01)
        assert np.isclose(frame.batch.measurements[0].axis[0], -0.01)
        assert not (runtime.repository.root / "data").exists()
        runtime.monitoring.set_recording(device, True)
        wait_for(lambda: bool(runtime.monitoring.snapshots()[device]["last_path"]))
        runtime.monitoring.stop(device).result(timeout=3)
        snapshots = list(runtime.repository.data_root.glob("单设备采集/*/*_continuous_*/_raw/segment-*.sqlite3"))
        assert snapshots
        saved = next(read_frames(snapshots[0]))
        assert saved["configuration"]["monitor_record_session"]
        assert len(saved["measurements"][0]["values"]) == 64
        assert not session.monitoring and session.state == "connected"


@pytest.mark.parametrize("outcome", ["rejected", "partial", "uncertain"])
def test_failed_control_pauses_only_target(tmp_path, outcome):
    configs = tuple(ConnectionConfig(f"mock://{i}", "siglent.spd4306x", "mock") for i in range(2))
    with ApplicationRuntime(configs, root=tmp_path, hardware=False) as runtime:
        ids = [runtime.add(c) for c in configs]
        for device in ids:
            runtime.connect(device).result()
            runtime.monitoring.start(device, (AcquisitionRequest(points=1),), interval=0.05)
        wait_for(lambda: all(runtime.monitoring.latest(d, "scalar") for d in ids))
        session = runtime.sessions.sessions[ids[0]]
        session.adapter.apply = lambda c: ApplyResult(outcome, c.operation, c.parameters, error="test fault")
        runtime.monitoring.apply(ids[0], DeviceCommand("voltage", {"value": 2})).result()
        wait_for(lambda: not runtime.monitoring.snapshots()[ids[0]]["running"])
        assert runtime.monitoring.snapshots()[ids[0]]["error"] == "test fault"
        assert runtime.monitoring.snapshots()[ids[1]]["running"]
        assert runtime.monitoring.latest(ids[0], "scalar") is None  # Old configuration cannot repaint.


def test_twenty_devices_scan_control_and_stop_keep_sessions(tmp_path):
    configs = tuple(ConnectionConfig(f"mock://{i}", "siglent.spd4306x", "mock") for i in range(20))
    with ApplicationRuntime(configs, root=tmp_path, hardware=False) as runtime:
        ids = [runtime.add(c) for c in configs]
        for device in ids:
            runtime.connect(device).result()
            runtime.monitoring.start(device, (AcquisitionRequest(points=1),), interval=0.05)
        wait_for(lambda: all(runtime.monitoring.latest(d, "scalar") for d in ids))
        before = {s["stableDeviceId"]: s["sessionId"] for s in runtime.sessions.snapshots()}
        for _ in range(3):
            result = runtime.discovery.scan().result(timeout=3)
            assert all(item["reason"] == "cached_active_session" for item in result["items"])
        futures = [runtime.monitoring.apply(d, DeviceCommand("voltage", {"value": 1.5})) for d in ids]
        assert all(f.result(timeout=3).outcome == "success" for f in futures)
        wait_for(lambda: all(runtime.monitoring.latest(d, "scalar") for d in ids))
        assert before == {s["stableDeviceId"]: s["sessionId"] for s in runtime.sessions.snapshots()}
        assert all(len(t.frames) == 1 for t in runtime.monitoring.tasks.values())
        stops = [runtime.monitoring.stop(d) for d in ids]
        for stop in stops:
            stop.result(timeout=3)
        assert not (runtime.repository.root / "data").exists()


def test_duplicate_requests_share_data_and_new_options_replace_old():
    request = AcquisitionRequest("waveform", (1,), 100)
    merged = merge_requests((request, request, replace(request, channels=(2,), points=200)))
    assert merged == (replace(request, channels=(1, 2), points=200),)
    reads = merge_requests((AcquisitionRequest("status", (1, 2), 1, "frequency"),))
    assert len(reads) == 2 and reads[0].channels == (1,) and reads[1].channels == (2,)


def test_stop_recording_drains_authorized_batch_and_releases_mode_lock(tmp_path):
    config = ConnectionConfig("mock://recording", "rs.rtm3004", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device).result()
        session = runtime.sessions.sessions[device]
        entered, release = threading.Event(), threading.Event()
        release.set()
        original = session.adapter.acquire

        def acquire(request):
            entered.set()
            assert release.wait(2)
            return original(request)

        session.adapter.acquire = acquire
        runtime.monitoring.start(device, (AcquisitionRequest("waveform", points=16),), interval=0.1)
        wait_for(lambda: runtime.monitoring.latest(device, "waveform") is not None)
        entered.clear()
        release.clear()
        try:
            runtime.monitoring.set_recording(device, True, mode="optical")
            assert entered.wait(2)
            runtime.monitoring.set_recording(device, False)
            assert session.recording  # The in-flight batch still belongs to the recording.
        finally:
            release.set()
        wait_for(lambda: bool(runtime.monitoring.snapshots()[device]["last_path"]))
        wait_for(lambda: not session.recording)
        runtime.monitoring.stop(device).result(timeout=3)
        paths = list(runtime.repository.data_root.glob("单设备采集/*/*_continuous_*/_raw/segment-*.sqlite3"))
        assert sum(len(list(read_frames(path))) for path in paths) == 1
        assert not (runtime.repository.root / "data/electronic").exists()
