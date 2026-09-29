import threading
import time
from concurrent.futures import Future
from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring import MonitoringService, MonitorTask
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Quality,
    ValidationError,
)


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def connected(tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://cadence", "rigol.dsa815", "mock"))
    runtime.connect(key).result(3)
    return runtime, key


def intercept_recording(runtime, callback):
    """Observe/block the actual binary sink, without changing manual export."""
    factory = runtime.repository.create_recording

    def create(mode, **options):
        recording = factory(mode, **options)
        append = recording.segments.append

        def write(sequence, batch):
            callback(batch, mode)
            return append(sequence, batch)

        recording.segments.append = write
        return recording

    runtime.repository.create_recording = create


def test_auto_transition_discards_frame_without_disconnect_or_save(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        session = runtime.sessions.sessions[key]
        adapter = session.adapter
        original = adapter.acquire
        calls, writes, saved = [], [], []
        fail = [0]

        def acquire(request):
            calls.append(time.monotonic())
            if fail[0]:
                fail[0] -= 1
                raise DeviceError("axis changed", "configuration_changed")
            return original(request)

        original_apply = adapter.apply
        adapter.apply = lambda command: (writes.append(command.operation), original_apply(command))[1]
        adapter.acquire = acquire
        intercept_recording(runtime, lambda batch, mode: saved.append(batch))
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.1)
        wait_for(lambda: runtime.monitoring.latest(key, "spectrum") is not None)
        runtime.sessions.set_control_group(key, True)
        wait_for(lambda: not session.pending)
        result = runtime.monitoring.apply(key, DeviceCommand("auto_setup")).result(3)
        assert result.outcome == "success"
        fail[0] = 5
        before = runtime.monitoring.snapshots()[key]["sequence"]
        with runtime.monitoring.lock:
            runtime.sessions.set_control_group(key, False)
            runtime.monitoring.set_recording(key, True)
        wait_for(lambda: runtime.monitoring.snapshots()[key]["sequence"] > before)
        runtime.monitoring.stop(key).result(3)
        assert session.state == "connected" and not session.last_error
        assert writes == ["auto_setup"] and saved
        assert all(batch.measurements and batch.measurements[0].values.size for batch in saved)
        audit = next((runtime.repository.root / "logs").glob("*.jsonl")).read_text(encoding="utf-8")
        assert '"event": "acquisition_discarded"' in audit
        assert '"event": "operation_failed"' not in audit
        assert '"event": "acquisition_io_failed"' not in audit
        assert runtime.monitoring.tasks[key].configuration_wait_deadline == 0


def test_continuously_changing_frames_wait_up_to_30_seconds_without_error(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        session = runtime.sessions.sessions[key]
        calls = []

        def acquire(_request):
            calls.append(1)
            raise DeviceError("axis changed", "configuration_changed")

        session.adapter.acquire = acquire
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.05)
        runtime.monitoring.set_recording(key, True)
        wait_for(lambda: len(calls) >= 5)
        with runtime.monitoring.lock:
            task = runtime.monitoring.tasks[key]
            assert task.running and not task.error and "等待稳定" in task.notice
            assert time.monotonic() < task.configuration_wait_deadline <= time.monotonic() + 30
            task.configuration_wait_deadline = time.monotonic() - 1
        wait_for(lambda: not runtime.monitoring.snapshots()[key]["running"])
        wait_for(lambda: not session.monitoring)
        state = runtime.monitoring.snapshots()[key]
        assert not state["error"] and "30 秒" in state["notice"]
        assert session.state == "connected" and not session.last_error
        assert runtime.monitoring.latest(key, "spectrum") is None
        recorder = runtime.monitoring.tasks[key].recorder
        recorder.finished.result(3)
        assert (recorder.folder / "_raw" / "recording.json").exists()
        assert (recorder.folder / "_raw" / "summary.json").exists()
        assert not list(recorder.folder.glob("segment-*"))


def test_single_read_configuration_change_is_not_retried(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        calls = []

        def acquire(_request):
            calls.append(1)
            raise DeviceError("axis changed", "configuration_changed")

        runtime.sessions.sessions[key].adapter.acquire = acquire
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.05, once=True)
        wait_for(lambda: not runtime.monitoring.snapshots()[key]["running"])
        assert calls == [1]
        assert "本次读取" in runtime.monitoring.snapshots()[key]["error"]


def test_settle_deadline_does_not_save_late_inflight_frame(tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    with runtime:
        service = runtime.monitoring
        request = AcquisitionRequest("spectrum")
        batch = runtime.sessions.sessions[key].adapter.acquire(request)
        future = Future()
        future.set_result(batch)
        saved = []
        runtime.repository.export = lambda *args, **options: saved.append(args)
        with service.lock:
            task = MonitorTask(
                (request,),
                "electronic",
                1,
                pending=future,
                pending_request=request,
                pending_record_session="fixture",
                configuration_wait_deadline=100,
            )
            service.tasks[key] = task
            monkeypatch.setattr("lab_control.application.monitoring.time.monotonic", lambda: 100)
            service._tick()
            monkeypatch.undo()
            assert not task.running and task.configuration_wait_expired
            assert not task.error and not task.frames and not saved and task.pending is None


@pytest.mark.parametrize("code", ["timeout", "disconnected", "parse_error"])
def test_transport_and_parse_faults_still_stop_without_retry(tmp_path, code):
    runtime, key = connected(tmp_path)
    with runtime:
        session = runtime.sessions.sessions[key]
        calls = []

        def acquire(_request):
            calls.append(1)
            raise DeviceError("fixture failure", code)

        session.adapter.acquire = acquire
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.05)
        wait_for(lambda: not runtime.monitoring.snapshots()[key]["running"])
        assert calls == [1] and session.state == "degraded"


def test_record_interval_is_separate_and_slow_writer_does_not_stall_display(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        entered, release = threading.Event(), threading.Event()
        paths = []

        def export(batch, mode):
            paths.append((time.monotonic(), batch.configuration["monitor_save_interval"]))
            entered.set()
            assert release.wait(4)
            return "saved"

        intercept_recording(runtime, export)
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.05, record_interval=0.3)
        runtime.monitoring.set_recording(key, True)
        try:
            assert entered.wait(2)
            sequence = runtime.monitoring.snapshots()[key]["sequence"]
            wait_for(lambda: runtime.monitoring.snapshots()[key]["sequence"] >= sequence + 4)
            assert len(paths) == 1
        finally:
            release.set()
        wait_for(lambda: len(paths) >= 3)
        runtime.monitoring.stop(key).result(3)
        # A slow disk may drain buffered frames in a burst; selection, not write
        # completion, is governed by the save interval.
        assert all(interval == 0.3 for _, interval in paths)
        assert runtime.monitoring.snapshots()[key]["sequence"] > len(paths) * 2


def test_completed_slow_frame_does_not_add_an_extra_refresh_delay(tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    with runtime:
        request = AcquisitionRequest("spectrum")
        batch = runtime.sessions.sessions[key].adapter.acquire(request)
        future = Future()
        future.set_result(batch)
        task = MonitorTask(
            (request,), "electronic", 0.5, pending=future, pending_request=request, last_started_at=10
        )
        with runtime.monitoring.lock:
            monkeypatch.setattr("lab_control.application.monitoring.time.monotonic", lambda: 11.5)
            runtime.monitoring._finish_batch(key, task)
            assert task.next_at == 10.5 and task.acquisition_seconds == 1.5
            monkeypatch.undo()


def test_invalid_quality_is_never_sent_to_writer(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        request = AcquisitionRequest("spectrum")
        batch = runtime.sessions.sessions[key].adapter.acquire(request)
        batch = replace(batch, measurements=(replace(batch.measurements[0], quality=Quality.PARSE_ERROR),))
        future = Future()
        future.set_result(batch)
        saved = []
        runtime.repository.export = lambda *args, **options: saved.append(args)
        task = MonitorTask(
            (request,),
            "electronic",
            0.5,
            pending=future,
            pending_request=request,
            pending_record_session="authorized",
            last_started_at=time.monotonic(),
        )
        with pytest.raises(DeviceError, match="parse_error"):
            runtime.monitoring._finish_batch(key, task)
        assert task.write_pending is None and not task.frames and not saved


def test_failed_writer_pauses_only_recording_task_and_does_not_retry(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        calls = []

        def export(*_args):
            calls.append(1)
            raise OSError("disk fixture unavailable")

        intercept_recording(runtime, export)
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.05)
        runtime.monitoring.set_recording(key, True)
        wait_for(lambda: not runtime.monitoring.snapshots()[key]["recording"])
        assert runtime.monitoring.snapshots()[key]["running"]
        runtime.monitoring.stop(key).result(3)
        assert calls == [1]
        assert "disk fixture unavailable" in runtime.monitoring.snapshots()[key]["recording_notice"]
        assert runtime.sessions.sessions[key].state == "connected"


@pytest.mark.parametrize("operation", ["acquire", "export"])
def test_shutdown_drains_failed_future_and_releases_monitoring_in_same_tick(tmp_path, operation):
    runtime, key = connected(tmp_path)
    session = runtime.sessions.sessions[key]
    future = Future()
    future.set_exception(DeviceError("shutdown fixture", "timeout"))
    request = AcquisitionRequest("spectrum")
    with runtime.monitoring.lock:
        runtime.sessions.set_monitoring(key, True, True)
        runtime.monitoring.tasks[key] = MonitorTask(
            (request,),
            "electronic",
            0.5,
            running=False,
            recording=True,
            pending=future if operation == "acquire" else None,
            pending_request=request,
            write_pending=future if operation == "export" else None,
        )
        # Finish the failure in the final tick; there must be no dependency on a later tick.
        runtime.monitoring.closing = True
    runtime.close()
    assert not session.monitoring and not session.recording
    assert not runtime.monitoring.thread.is_alive()


@pytest.mark.parametrize(
    "refresh,save",
    [(0, 1), (61, 1), (0.5, 0), (0.5, 86401), (float("nan"), 1), (0.5, float("inf")), (True, 1)],
)
def test_invalid_intervals_are_rejected(refresh, save):
    with pytest.raises(ValidationError):
        MonitoringService.validate_intervals(refresh, save)
