import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.storage.recording_workers import CsvConversionSupervisor
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.recording_state import recording_save_notice

from .test_monitoring_timing import connected, wait_for

REQUEST = AcquisitionRequest("spectrum")


def test_slow_preparation_keeps_qt_and_other_monitor_live_and_stop_cancels(qtbot, tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    release, entered = threading.Event(), threading.Event()
    with runtime:
        other = runtime.add(ConnectionConfig("mock://other", "rigol.dsa815", "mock"))
        runtime.connect(other).result(3)
        service = runtime.monitoring
        parent = QWidget()
        qtbot.addWidget(parent)
        controller = WorkspaceController(runtime, parent)
        factory = runtime.repository.create_recording
        original_root = runtime.repository.data_root
        created = []

        def delayed(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            recorder = factory(*args, **kwargs)
            created.append(recorder)
            return recorder

        monkeypatch.setattr(runtime.repository, "create_recording", delayed)
        try:
            service.start(other, (REQUEST,), interval=0.05)
            wait_for(lambda: service.tasks[other].sequence > 0)
            before = service.tasks[other].sequence
            controller.start_monitoring(key, (REQUEST,), interval=0.05, recording=True)
            assert entered.wait(3)
            assert service.snapshots()[key]["recording_preparing"]
            assert runtime.sessions.sessions[key].recording
            pulses = []
            QTimer.singleShot(0, lambda: pulses.append(True))
            qtbot.waitUntil(lambda: bool(pulses) and service.tasks[other].sequence > before + 1)
            runtime.repository.set_data_root(tmp_path / "next-root")
            stopped = service.stop(key)
            assert not stopped.done() and runtime.sessions.sessions[key].recording
        finally:
            release.set()
        stopped.result(5)
        assert len(created) == 1 and created[0].folder.is_relative_to(original_root)
        assert created[0].snapshot()["received"] == 0 and created[0].binary_done
        assert not runtime.sessions.sessions[key].recording
        assert not service.tasks[key].recording
        controller.close()


def test_failed_background_start_restores_existing_monitor_settings(tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    with runtime:
        service = runtime.monitoring
        service.start(key, (REQUEST,), interval=0.5, record_interval=1)

        def fail(*args, **kwargs):
            raise PermissionError("save root is read only")

        monkeypatch.setattr(runtime.repository, "create_recording", fail)
        future = service.start(
            key, (REQUEST,), interval=0.05, record_interval=None, recording=True, background=True
        )
        with pytest.raises(PermissionError, match="read only"):
            future.result(3)
        task = service.tasks[key]
        assert task.running and not task.recording and task.recording_preparation is None
        assert task.interval == 0.5 and task.record_interval == 1
        assert not runtime.sessions.sessions[key].recording
        service.stop(key).result(3)


def test_late_csv_failure_survives_new_batch_and_notifies_once(qtbot, tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    release = threading.Event()
    original = CsvConversionSupervisor.run
    delayed = []

    def fail_first(supervisor):
        if delayed:
            return original(supervisor)
        recording = supervisor.recording
        delayed.append(recording)
        try:
            assert release.wait(10)
            recording.thread.join()
            with recording.lock:
                recording.csv_error = "CSV output disk denied"
        finally:
            recording.finished.set_result([])
            recording.wake()

    monkeypatch.setattr(CsvConversionSupervisor, "run", fail_first)
    with runtime:
        service = runtime.monitoring
        parent = QWidget()
        qtbot.addWidget(parent)
        controller = WorkspaceController(runtime, parent)
        controller.timer.stop()
        messages = []
        controller.message.connect(messages.append)
        try:
            service.start(key, (REQUEST,), interval=0.05, recording=True)
            first = service.tasks[key].recorder
            wait_for(lambda: first.snapshot()["committed"] > 0)
            service.set_recording(key, False)
            wait_for(lambda: first.binary_done)
            service.set_recording(key, True)
            second = service.tasks[key].recorder
            assert first is not second and not first.finished.done()
            release.set()
            first.finished.result(3)
            wait_for(lambda: bool(service.recording_results_since(0)))
            state = service.snapshots()[key]
            assert state["recording"] and state["recording_state"]["csv_error"] == ""
            assert "CSV output disk denied" in recording_save_notice(state)
            controller.poll()
            controller.poll()
            assert len(messages) == 1 and str(first.folder) in messages[0]
            assert service.recording_results_since(0)[0]["error"] == "CSV output disk denied"
        finally:
            release.set()
            controller.close()
        service.stop(key).result(5)


def test_close_rejects_new_recording_preparation(tmp_path):
    runtime, key = connected(tmp_path)
    runtime.close()
    with pytest.raises(DeviceError, match="closing"):
        runtime.monitoring.set_recording(key, True, background=True)


def test_batch_preparation_is_one_per_device_and_cancelled_queue_does_no_disk_work(tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    release = threading.Event()
    entered = []
    with runtime:
        keys = [key]
        for index in range(5):
            other = runtime.add(ConnectionConfig(f"mock://queue-{index}", "rigol.dsa815", "mock"))
            runtime.connect(other).result(3)
            keys.append(other)
        factory = runtime.repository.create_recording

        def delayed(*args, **kwargs):
            entered.append(kwargs["context"])
            assert release.wait(10)
            return factory(*args, **kwargs)

        monkeypatch.setattr(runtime.repository, "create_recording", delayed)
        service = runtime.monitoring
        try:
            preparations = [service.start(k, (REQUEST,), recording=True, background=True) for k in keys]
            wait_for(lambda: len(entered) == 2)
            assert service.set_recording(keys[-1], True, background=True) is preparations[-1]
            stopped = [service.stop(k) for k in keys]
        finally:
            release.set()
        for future in preparations:
            with pytest.raises(DeviceError) as error:
                future.result(5)
            assert error.value.code == "recording_cancelled"
        for future in stopped:
            future.result(5)
        assert len(entered) == 2
        assert all(not s.recording and not s.monitoring for s in runtime.sessions.sessions.values())
