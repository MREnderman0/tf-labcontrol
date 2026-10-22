import threading
import time

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand
from lab_control.storage.recording import read_frames

from .test_monitoring_timing import connected, wait_for


def test_fastest_is_completion_driven_serial_and_does_not_starve_control(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter = runtime.sessions.sessions[key].adapter
        acquire, apply = adapter.acquire, adapter.apply
        entered, release = threading.Event(), threading.Event()
        events = []

        def read(request):
            events.append("read")
            entered.set()
            assert release.wait(3)
            return acquire(request)

        adapter.acquire = read
        adapter.apply = lambda command: (events.append("apply"), apply(command))[1]
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),), cadence="fastest")
        try:
            assert entered.wait(2)
            controlled = runtime.monitoring.apply(key, DeviceCommand("center_frequency", {"value": 2e6}))
            assert not controlled.done() and events == ["read"]
        finally:
            release.set()
        assert controlled.result(3).outcome == "success"
        wait_for(lambda: len(events) >= 4)
        assert events[:2] == ["read", "apply"]
        runtime.monitoring.stop(key).result(3)
        count = len(events)
        time.sleep(0.08)
        assert len(events) == count


def test_fastest_to_interval_changes_deadline_without_device_writes(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), cadence="fastest")
        wait_for(lambda: service.snapshots()[key]["sequence"] >= 3)
        service.set_intervals(key, 0.5, 1, cadence="interval")
        state = service.snapshots()[key]
        time.sleep(0.15)
        assert service.snapshots()[key]["sequence"] <= state["sequence"] + 1
        service.set_intervals(key, 0.5, 1, cadence="fastest")
        wait_for(lambda: service.snapshots()[key]["sequence"] > state["sequence"] + 2)
        service.stop(key).result(3)


def test_buffer_pause_keeps_monitoring_and_releases_lock_after_binary_drain(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        factory = runtime.repository.create_recording
        entered, release = threading.Event(), threading.Event()

        def create(mode, **options):
            recorder = factory(mode, max_frames=2, **options)
            append = recorder.segments.append

            def slow(sequence, batch):
                entered.set()
                assert release.wait(5)
                return append(sequence, batch)

            recorder.segments.append = slow
            return recorder

        runtime.repository.create_recording = create
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), cadence="fastest", record_interval=None)
        service.set_recording(key, True)
        try:
            assert entered.wait(2)
            wait_for(lambda: not service.snapshots()[key]["recording"])
            state = service.snapshots()[key]
            assert state["running"] and "缓冲" in state["recording_notice"]
            assert runtime.sessions.snapshots()[0]["controlsLocked"]
            wait_for(lambda: service.snapshots()[key]["sequence"] > state["sequence"] + 2)
        finally:
            release.set()
        wait_for(lambda: not runtime.sessions.snapshots()[0]["controlsLocked"])
        service.stop(key).result(5)
        recorder = service.tasks[key].recorder
        assert recorder.received == recorder.committed == recorder.converted == 2
        assert (
            sum(len(list(read_frames(path))) for path in recorder.raw_folder.glob("segment-*.sqlite3")) == 2
        )


@pytest.mark.parametrize("cadence,interval", [("bad", 1), ("interval", 0.1), ("fastest", 0)])
def test_explicit_cadence_validates_before_io(tmp_path, cadence, interval):
    runtime, key = connected(tmp_path)
    with runtime:
        with pytest.raises(Exception):
            runtime.monitoring.start(
                key, (AcquisitionRequest("spectrum"),), cadence=cadence, interval=interval
            )
        assert key not in runtime.monitoring.tasks


def test_stop_retains_lock_until_binary_done_but_not_csv(tmp_path, monkeypatch):
    from concurrent.futures import Future

    from lab_control.storage.recording import Recording

    release_csv = threading.Event()
    entered_write, release_write = threading.Event(), threading.Event()

    def csv_wait(self):
        self.thread.join()
        assert release_csv.wait(5)
        self.finished.set_result(list(self.segments.paths))
        self.wake()

    monkeypatch.setattr(Recording, "_csv_loop", csv_wait)
    runtime, key = connected(tmp_path)
    with runtime:
        factory = runtime.repository.create_recording

        def create(mode, **options):
            recorder = factory(mode, **options)
            append = recorder.segments.append

            def write(sequence, batch):
                entered_write.set()
                assert release_write.wait(5)
                return append(sequence, batch)

            recorder.segments.append = write
            return recorder

        runtime.repository.create_recording = create
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), cadence="fastest")
        service.set_recording(key, True)
        stopped: Future | None = None
        try:
            assert entered_write.wait(2)
            service.set_recording(key, False)
            assert runtime.sessions.snapshots()[0]["controlsLocked"]
            stopped = service.stop(key)
            release_write.set()
            wait_for(lambda: not runtime.sessions.snapshots()[0]["controlsLocked"])
            assert not stopped.done()
            service.start(key, (AcquisitionRequest("spectrum"),), cadence="fastest")
            assert (
                service.apply(key, DeviceCommand("center_frequency", {"value": 2e6})).result(3).outcome
                == "success"
            )
        finally:
            release_write.set()
            release_csv.set()
        stopped.result(3)
        service.stop(key).result(3)


@pytest.mark.parametrize("cadence", ["fastest", "interval"])
def test_unified_recording_saves_each_submitted_frame_and_not_inflight_monitor(tmp_path, cadence):
    from dataclasses import replace

    runtime, key = connected(tmp_path)
    with runtime:
        adapter = runtime.sessions.sessions[key].adapter
        original = adapter.acquire
        entered, release = threading.Event(), threading.Event()
        batches = []

        def read(request):
            if not batches:
                entered.set()
                assert release.wait(4)
            time.sleep(0.01)
            batch = replace(original(request), performance={"fixture_frame_id": len(batches)})
            batches.append((request.context_id, len(batches)))
            return batch

        adapter.acquire = read
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), cadence=cadence, record_interval=None)
        try:
            assert entered.wait(2)
            service.set_recording(key, True)
            context = service.tasks[key].acquisition_context_id
        finally:
            release.set()
        wait_for(lambda: service.tasks[key].recorder.committed >= 4, timeout=5)
        service.stop(key).result(5)
        recorder = service.tasks[key].recorder
        saved = [
            meta["performance"]["fixture_frame_id"]
            for path in recorder.raw_folder.glob("segment-*.sqlite3")
            for meta in read_frames(path)
        ]
        expected = [stamp for ctx, stamp in batches if ctx == context]
        assert batches[0][0] != context and batches[0][1] not in saved
        assert saved == expected and len(saved) >= 4
        assert recorder.received == recorder.committed == recorder.converted == len(saved)
        assert service.snapshots()[key]["record_interval"] is None
