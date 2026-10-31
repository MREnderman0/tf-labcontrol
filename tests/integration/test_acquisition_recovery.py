"""A missing acquisition frame never abandons an authorized continuous task."""

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.storage.recording import read_frames


def wait_for(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Recovery did not reach expected state")


@pytest.mark.parametrize(
    "adapter", ["joinwit.jw8105", "rigol.dsa815", "rs.rtm3004", "gwinstek.gpd4303s", "pico.6404e"]
)
def test_monitoring_reconnects_same_asset_and_continues_same_recording(tmp_path, adapter):
    config = ConnectionConfig("mock://recovery", adapter, "mock")
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        created = []

        def factory(selected):
            assert selected == config
            result = SimulatedInstrument(selected)
            original = result.acquire
            calls = 0

            def acquire(request):
                nonlocal calls
                calls += 1
                if len(created) == 1 and calls == 2:
                    raise DeviceError("temporary cable outage", "timeout")
                return original(request)

            result.acquire = acquire
            result.apply = lambda _command: pytest.fail("Recovery must not replay controls")
            created.append(result)
            return result

        runtime.sessions.factory = factory
        device = runtime.add(config)
        runtime.connect(device).result(3)
        runtime.monitoring.start(
            device,
            (AcquisitionRequest("scalar", points=1),),
            interval=0.05,
            record_interval=None,
            recording=True,
        )
        task = runtime.monitoring.tasks[device]
        recorder = task.recorder
        wait_for(lambda: len(created) == 2 and task.sequence >= 4)
        assert task.running and task.recording and task.recorder is recorder
        assert created[0].closed
        assert runtime.sessions.sessions[device].config == config
        runtime.monitoring.stop(device).result(10)
        frames = [f for p in recorder.segments.paths for f in read_frames(Path(p))]
        assert len(frames) >= 4
        gaps = [
            f["configuration"]["_acquisition_gap"] for f in frames if "_acquisition_gap" in f["configuration"]
        ]
        assert len(gaps) == 1 and gaps[0]["attempts"] == 1
        assert all(m["quality"] == "simulated" for f in frames for m in f["measurements"])
        events = [
            json.loads(line)
            for line in (recorder.raw_folder / "acquisition-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert [e["event"] for e in events] == ["acquisition_interrupted", "acquisition_resumed"]
        assert not runtime.sessions.sessions[device].controls_locked


def test_unavailable_device_keeps_retrying_until_user_stop_without_affecting_peer(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://target", "rigol.dg5504pro", "mock")
        target = runtime.add(config)
        runtime.connect(target).result(3)
        peer_config = replace(config, resource="mock://peer")
        peer = runtime.add(peer_config)
        runtime.connect(peer).result(3)
        old = runtime.sessions.sessions[target].adapter
        old.acquire = lambda _request: (_ for _ in ()).throw(DeviceError("gone", "disconnected"))
        attempts = []

        def unavailable(selected):
            attempts.append(time.monotonic())
            raise DeviceError("still unplugged", "connection_failed")

        runtime.sessions.factory = unavailable
        request = AcquisitionRequest("waveform", points=32)
        runtime.monitoring.start(peer, (request,), interval=0.05)
        runtime.monitoring.start(target, (request,), interval=0.05, recording=True)
        task = runtime.monitoring.tasks[target]
        wait_for(lambda: len(attempts) >= 3)
        assert task.running and task.recording and not task.recorder.binary_done
        assert runtime.monitoring.snapshots()[target]["recovering"]
        assert runtime.monitoring.tasks[peer].sequence > 3
        runtime.monitoring.stop(target).result(10)
        stopped = len(attempts)
        time.sleep(0.1)
        assert len(attempts) == stopped and not task.recovery_pending
        assert not runtime.sessions.sessions[target].monitoring
        assert not runtime.sessions.sessions[target].controls_locked


def test_stop_during_identity_read_never_resumes_acquisition(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://stop-recovery", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        runtime.sessions.sessions[device].adapter.acquire = lambda _r: (_ for _ in ()).throw(
            DeviceError("gone", "timeout")
        )
        entered, release = threading.Event(), threading.Event()
        created = []

        def factory(selected):
            result = SimulatedInstrument(selected)
            identity = result.identify

            def identify():
                entered.set()
                assert release.wait(3)
                return identity()

            result.identify = identify
            result.acquire = lambda _r: pytest.fail("User stop must win")
            created.append(result)
            return result

        runtime.sessions.factory = factory
        runtime.monitoring.start(device, (AcquisitionRequest("waveform", points=32),), interval=0.05)
        try:
            assert entered.wait(3)
            stopped = runtime.monitoring.stop(device)
            assert not stopped.done()
            task = runtime.monitoring.tasks[device]
            assert runtime.monitoring.snapshots()[device]["stopping"]
            with pytest.raises(DeviceError) as caught:
                runtime.monitoring.start(device, (AcquisitionRequest("waveform", points=32),))
            assert caught.value.code == "task_busy"
            assert runtime.monitoring.tasks[device] is task
        finally:
            release.set()
        stopped.result(5)
        assert created[0].closed
        assert not runtime.monitoring.tasks[device].running


@pytest.mark.parametrize("code", ["timeout", "disconnected", "parse_error"])
def test_direct_recording_recovers_without_resetting_count_or_destination(tmp_path, code):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://direct", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        session = runtime.sessions.sessions[device]
        session.adapter.acquire = lambda _r: (_ for _ in ()).throw(
            DeviceError("temporary read failure", code)
        )
        paths = runtime.sessions.start_acquisition(
            device, AcquisitionRequest("waveform", points=32), count=3, interval=0.01
        ).result(10)
        frames = [f for p in paths for f in read_frames(Path(p))]
        assert len(frames) == 3
        assert frames[0]["configuration"]["_acquisition_gap"]["code"] == code
        assert session.state == "connected" and not session.controls_locked


def test_identity_conflict_is_fatal_and_uncertain_control_is_never_replayed(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://identity", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        session = runtime.sessions.sessions[device]
        session.adapter.acquire = lambda _r: (_ for _ in ()).throw(DeviceError("gone", "timeout"))

        def wrong_device(selected):
            result = SimulatedInstrument(selected)
            result.identity = replace(result.identity, serial="wrong instrument")
            return result

        runtime.sessions.factory = wrong_device
        runtime.monitoring.start(device, (AcquisitionRequest("waveform", points=32),), interval=0.05)
        wait_for(lambda: not runtime.monitoring.tasks[device].running)
        assert "身份" in runtime.monitoring.tasks[device].error
        assert runtime.monitoring.tasks[device].sequence == 0
        runtime.monitoring.stop(device).result(5)
        with pytest.raises(DeviceError):
            runtime.sessions.submit(device, "apply", DeviceCommand("output", {"value": True}))


def test_error_event_before_future_completion_does_not_pause_monitor(tmp_path):
    """Real VISA reproduced this window between degraded and Future completion."""
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://event-race", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        session = runtime.sessions.sessions[device]
        session.adapter.acquire = lambda _r: (_ for _ in ()).throw(DeviceError("gone", "timeout"))
        entered, release = threading.Event(), threading.Event()

        def hold_failed_worker(event):
            if event["event"] == "error" and event.get("operation") == "acquire":
                entered.set()
                assert release.wait(5)

        runtime.sessions.subscribe(hold_failed_worker)
        runtime.monitoring.start(device, (AcquisitionRequest("waveform", points=32),), interval=0.05)
        task = runtime.monitoring.tasks[device]
        try:
            assert entered.wait(3)
            with runtime.monitoring.lock:
                runtime.monitoring._tick()
                assert task.running and not task.error
                runtime.monitoring.set_recording(device, False)
        finally:
            release.set()
        wait_for(lambda: task.sequence >= 2)
        assert task.running and not task.error


def test_invalid_recovery_frame_cannot_end_gap_or_reach_recording(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://invalid-recovery", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        runtime.sessions.sessions[device].adapter.acquire = lambda _r: (_ for _ in ()).throw(
            DeviceError("gone", "timeout")
        )
        attempts = []

        def factory(selected):
            result = SimulatedInstrument(selected)
            attempts.append(result)
            original = result.acquire
            if len(attempts) == 1:

                def invalid(request):
                    batch = original(request)
                    return replace(
                        batch,
                        measurements=(replace(batch.measurements[0], values=[float("nan")], axis=None),),
                    )

                result.acquire = invalid
            return result

        runtime.sessions.factory = factory
        runtime.monitoring.start(
            device, (AcquisitionRequest("waveform", points=32),), interval=0.05, recording=True
        )
        task = runtime.monitoring.tasks[device]
        wait_for(lambda: task.sequence >= 2)
        runtime.monitoring.stop(device).result(10)
        frames = [f for p in task.recorder.segments.paths for f in read_frames(Path(p))]
        assert len(attempts) == 2
        assert frames[0]["configuration"]["_acquisition_gap"]["failed_reads"] == 2
        events = [
            json.loads(line)["event"]
            for line in (task.recorder.raw_folder / "acquisition-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert events.count("acquisition_resumed") == 1


def test_uncertain_recovery_initialization_is_never_replayed(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://initialization", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        runtime.sessions.sessions[device].adapter.acquire = lambda _r: (_ for _ in ()).throw(
            DeviceError("gone", "timeout")
        )
        attempts = []

        def factory(selected):
            result = SimulatedInstrument(selected)

            def prepare():
                attempts.append(1)
                raise DeviceError("format write uncertain", "timeout")

            result.prepare_acquisition = prepare
            return result

        runtime.sessions.factory = factory
        runtime.monitoring.start(device, (AcquisitionRequest("waveform", points=32),), interval=0.05)
        task = runtime.monitoring.tasks[device]
        wait_for(lambda: not task.running)
        runtime.monitoring.stop(device).result(5)
        assert len(attempts) == 1 and "初始化" in task.error


def test_gap_journal_failure_cleans_up_once_and_keeps_scheduler_alive(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://journal", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        runtime.monitoring.start(
            device, (AcquisitionRequest("waveform", points=32),), interval=0.05, recording=True
        )
        task = runtime.monitoring.tasks[device]
        wait_for(lambda: task.sequence >= 1)
        # A directory at the event path models a durable-write failure.
        (task.recorder.raw_folder / "acquisition-events.jsonl").mkdir()
        runtime.sessions.sessions[device].adapter.acquire = lambda _r: (_ for _ in ()).throw(
            DeviceError("gone", "timeout")
        )
        wait_for(lambda: not task.running and task.recovery is None)
        runtime.monitoring.stop(device).result(10)
        assert runtime.monitoring.thread.is_alive()
        assert task.recorder.error and "缺口" in task.error


def test_stop_recording_during_outage_preserves_monitoring_intent(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://record-stop", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        runtime.sessions.sessions[device].adapter.acquire = lambda _r: (_ for _ in ()).throw(
            DeviceError("gone", "timeout")
        )
        original_factory = runtime.sessions.factory
        runtime.sessions.factory = lambda _c: (_ for _ in ()).throw(
            DeviceError("offline", "connection_failed")
        )
        runtime.monitoring.start(
            device, (AcquisitionRequest("waveform", points=32),), interval=0.05, recording=True
        )
        task = runtime.monitoring.tasks[device]
        wait_for(lambda: task.recovery and task.recovery.attempts >= 1)
        runtime.monitoring.set_recording(device, False)
        wait_for(lambda: task.recorder.binary_done)
        runtime.sessions.factory = original_factory
        wait_for(lambda: task.sequence >= 2)
        assert task.running and not task.recording and task.recorder.received == 0


def test_single_read_does_not_turn_into_continuous_recovery(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://once", "rigol.dg5504pro", "mock")
        device = runtime.add(config)
        runtime.connect(device).result(3)
        session = runtime.sessions.sessions[device]
        original = session.adapter
        original.acquire = lambda _r: (_ for _ in ()).throw(DeviceError("late", "timeout"))
        runtime.sessions.factory = lambda _c: pytest.fail("Single read must not reconnect")
        runtime.monitoring.start(device, (AcquisitionRequest("waveform", points=32),), once=True)
        wait_for(lambda: not runtime.monitoring.tasks[device].running)
        assert session.adapter is original and not session.recovery
