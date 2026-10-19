import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.storage.recording import read_frames

from .test_monitoring_timing import connected, intercept_recording, wait_for


def streaming_adapter(runtime, key):
    adapter = runtime.sessions.sessions[key].adapter
    adapter.supports_live_acquisition = True
    adapter.supports_stream_acquisition = True
    original = adapter.acquire
    requests = []

    def read(request):
        requests.append(request)
        batch = original(request)
        config = batch.configuration or {"span": 2e6}
        return replace(
            batch,
            validation="cached" if request.consistency == "stream" else request.consistency,
            configuration=config,
            settings_timestamps={k: "startup-time" for k in config},
            performance={"acquisition_context_id": request.context_id},
        )

    adapter.acquire = read
    return adapter, requests


def test_recording_uses_one_run_context_without_full_save_requests_and_monitoring_resumes(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        _, requests = streaming_adapter(runtime, key)
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), interval=0.05, record_interval=0.05)
        wait_for(lambda: requests)
        assert requests[-1].consistency == "live"
        service.set_recording(key, True)
        wait_for(lambda: len([r for r in requests if r.consistency == "stream"]) >= 3)
        service.capture_and_export(key, AcquisitionRequest("spectrum"), "electronic").result(3)
        first = {r.context_id for r in requests if r.consistency == "stream"}
        assert len(first) == 1 and "" not in first
        service.set_recording(key, False)
        wait_for(lambda: requests[-1].consistency == "live")
        assert not any(r.consistency == "full" for r in requests)
        service.set_recording(key, True)
        wait_for(lambda: requests[-1].consistency == "stream" and requests[-1].context_id not in first)
        service.stop(key).result(3)
        snapshots = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.rglob("snapshot.json")]
        assert snapshots and all(s["validation"] == "cached" for s in snapshots)
        assert all(set(s["settings_timestamps"].values()) == {"startup-time"} for s in snapshots)


def test_controls_auto_and_parameter_reads_lock_only_during_recording(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter, _ = streaming_adapter(runtime, key)
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), interval=0.05)
        command = DeviceCommand("center_frequency", {"value": 2e6})
        assert service.apply(key, command).result(3).outcome == "success"
        calls = []
        adapter.apply = lambda cmd: calls.append(cmd)
        service.set_recording(key, True)
        for operation, argument in (
            ("apply", command),
            ("apply", DeviceCommand("auto_setup")),
            ("read_settings", (("rbw",), 1)),
            ("refresh_capabilities", None),
        ):
            with pytest.raises(DeviceError) as exc:
                runtime.sessions.submit(key, operation, argument)
            assert exc.value.code == "task_busy"
        with pytest.raises(DeviceError):
            runtime.sessions.set_control_group(key, True)
        assert not calls and runtime.sessions.snapshots()[0]["controlsLocked"]
        service.stop(key).result(3)
        assert not runtime.sessions.snapshots()[0]["controlsLocked"]


def test_recording_rejected_during_control_group_does_not_change_mode_or_flags(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),))
        runtime.sessions.set_control_group(key, True)
        try:
            with pytest.raises(DeviceError):
                runtime.monitoring.set_recording(key, True, mode="optical")
            task = runtime.monitoring.tasks[key]
            assert not task.recording and not task.acquisition_context_id and task.mode == "general"
            assert not runtime.sessions.sessions[key].recording
        finally:
            runtime.sessions.set_control_group(key, False)
        runtime.monitoring.stop(key).result(3)


@pytest.mark.parametrize("missing", ["context", "timestamp", "field"])
def test_cached_frame_without_complete_recording_provenance_cannot_be_saved(tmp_path, missing):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter, _ = streaming_adapter(runtime, key)
        batch = adapter.acquire(AcquisitionRequest("spectrum", consistency="stream", context_id="record"))
        if missing == "context":
            batch = replace(batch, performance={})  # Monitoring cache is not a recorded frame.
        elif missing == "timestamp":
            batch = replace(batch, settings_timestamps={k: "" for k in batch.configuration})
        else:
            batch = replace(batch, configuration={**batch.configuration, "unread_field": 0})
        with pytest.raises(DeviceError, match="快照来源"):
            runtime.monitoring._validate_saved_batch(batch)


def test_explicit_parameter_refresh_invalidates_context_but_quiet_read_does_not(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter, _ = streaming_adapter(runtime, key)
        reasons = []
        adapter.invalidate_acquisition_context = reasons.append
        future = runtime.sessions.try_submit_idle(key, "read_settings", (("rbw",), 1), quiet=True)
        future.result(3)
        assert not reasons
        runtime.sessions.submit(key, "read_settings", (("rbw",), 1)).result(3)
        runtime.sessions.submit(key, "refresh_capabilities").result(3)
        assert reasons == ["explicit_read", "explicit_refresh"]


def test_direct_continuous_capture_locks_and_gets_a_new_context_on_restart(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter, requests = streaming_adapter(runtime, key)
        read = adapter.acquire
        entered, release = threading.Event(), threading.Event()
        adapter.acquire = lambda request: (entered.set(), release.wait(3), read(request))[2]
        future = runtime.sessions.start_acquisition(
            key, AcquisitionRequest("spectrum"), interval=0.01, count=2
        )
        assert entered.wait(2)
        try:
            assert runtime.sessions.snapshots()[0]["controlsLocked"]
            with pytest.raises(DeviceError):
                runtime.sessions.submit(key, "apply", DeviceCommand("auto_setup"))
        finally:
            release.set()
        paths = future.result(3)
        assert len(paths) == 1
        assert sum(len(list(read_frames(Path(path)))) for path in paths) == 2
        first = requests[0].context_id
        assert all(r.consistency == "stream" and r.context_id == first for r in requests)
        runtime.sessions.start_acquisition(
            key, AcquisitionRequest("spectrum"), interval=0.01, count=1
        ).result(3)
        assert requests[-1].context_id != first
        assert not runtime.sessions.snapshots()[0]["controlsLocked"]


def test_stream_saves_next_fresh_frame_when_writer_finishes_during_read(tmp_path):
    runtime, key = connected(tmp_path)
    release = threading.Event()
    with runtime:
        adapter, requests = streaming_adapter(runtime, key)
        read = adapter.acquire
        saved = []

        def acquire(request):
            if requests:
                release.set()
                time.sleep(0.03)
            return read(request)

        def export(batch, mode):
            saved.append(batch)
            if len(saved) == 1:
                assert release.wait(3)
            return "mock-path"

        adapter.acquire = acquire
        intercept_recording(runtime, export)
        with runtime.monitoring.lock:
            runtime.monitoring.start(
                key, (AcquisitionRequest("spectrum"),), interval=0.05, record_interval=0.05
            )
            runtime.monitoring.set_recording(key, True)
        try:
            wait_for(lambda: len(saved) >= 2)
            assert saved[1].timestamp > saved[0].timestamp
            assert len(requests) <= 3
        finally:
            release.set()
            runtime.monitoring.stop(key).result(3)
