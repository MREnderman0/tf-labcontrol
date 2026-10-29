"""One overview start groups its selected devices under one immutable save batch."""

import csv
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, ValidationError
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard

START = datetime(2026, 10, 26, 23, 59, 59, 999000, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def overview(qtbot, tmp_path):
    with ApplicationRuntime(root=tmp_path / "runtime", hardware=False) as runtime:
        keys = []
        for index in range(4):
            key = runtime.add(ConnectionConfig(f"mock://batch-meter-{index}", "joinwit.jw8105", "mock"))
            runtime.connect(key).result(3)
            if index < 3:
                runtime.bind_slot(key, f"optical_power_meter:{index + 1}", "general")
            keys.append(key)
        source = runtime.add(ConnectionConfig("mock://batch-source", "rs.sma100b", "mock"))
        runtime.connect(source).result(3)
        parent = QWidget()
        qtbot.addWidget(parent)
        layout = QVBoxLayout(parent)
        controller = WorkspaceController(runtime, parent)
        controller.timer.stop()
        runtime.monitoring.enable_settings_sync(False)
        dashboard = MonitoringDashboard(controller, parent)
        layout.addWidget(dashboard)
        try:
            yield runtime, controller, dashboard, keys, source
        finally:
            controller.close()


def select(dashboard, keys):
    for key, card in dashboard.cards.items():
        card.enabled.setChecked(key in keys)


def finish_batch(runtime, dashboard, keys, qtbot):
    service = runtime.monitoring
    qtbot.waitUntil(
        lambda: all(
            service.snapshots().get(key, {}).get("recording_state", {}).get("committed", 0) for key in keys
        ),
        timeout=8000,
    )
    recorders = [service.tasks[key].recorder for key in keys]
    dashboard.stop()
    for key, recorder in zip(keys, recorders, strict=True):
        service.stop(key).result(5)
        recorder.finished.result(5)
        assert not recorder.error and not recorder.csv_error
    return recorders


def metadata(recorder):
    return json.loads((recorder.folder / "_raw" / "recording.json").read_text(encoding="utf-8"))


def test_one_click_groups_meters_and_other_types_and_restart_creates_new_batch(overview, qtbot, monkeypatch):
    runtime, controller, dashboard, keys, source = overview
    monkeypatch.setattr("lab_control.storage.paths.local_now", lambda: START)
    selected = [*keys[:3], source]
    select(dashboard, selected)
    folders = []
    for _ in range(2):
        dashboard.toggle_sampling()
        recorders = finish_batch(runtime, dashboard, selected, qtbot)
        parents = {recorder.folder.parent for recorder in recorders}
        assert len(parents) == 1
        batch_folder = parents.pop()
        folders.append(batch_folder)
        assert batch_folder.parent == runtime.repository.data_root / "批量采集" / "2026-10-26"
        assert len(list(batch_folder.iterdir())) == 4
        manifests = [metadata(recorder) for recorder in recorders]
        assert len({item["batch_id"] for item in manifests}) == 1
        assert all(
            item["scope"] == "batch" and item["started_local"] == START.isoformat() for item in manifests
        )
        for index, recorder in enumerate(recorders[:3], 1):
            assert f"功率计{index}" in recorder.folder.name
            assert metadata(recorder)["slot_label"] == f"功率计{index}"
            with (recorder.folder / "数据_00001.csv").open(encoding="utf-8-sig", newline="") as stream:
                assert {row["设备槽位"] for row in csv.DictReader(stream)} == {f"功率计{index}"}
            assert "采集批次：" in (recorder.folder / "记录说明.txt").read_text(encoding="utf-8-sig")
        assert keys[3] not in runtime.monitoring.tasks
        assert not (runtime.repository.data_root / "单设备采集").exists()
        controller.poll()
    assert folders[0] != folders[1] and all(folder.is_dir() for folder in folders)


@pytest.mark.parametrize("count", [0, 1])
def test_empty_selection_does_not_save_and_single_selection_keeps_single_device_layout(
    overview, qtbot, count
):
    runtime, controller, dashboard, keys, _ = overview
    select(dashboard, keys[:count])
    messages = []
    controller.message.connect(messages.append)
    dashboard.toggle_sampling()
    if not count:
        assert not runtime.monitoring.tasks and messages
        assert not runtime.repository.data_root.exists()
        return
    recorder = finish_batch(runtime, dashboard, keys[:1], qtbot)[0]
    assert recorder.folder.is_relative_to(runtime.repository.data_root / "单设备采集")
    assert metadata(recorder)["scope"] == "single_device" and metadata(recorder)["batch_id"] is None
    assert not (runtime.repository.data_root / "批量采集").exists()


def test_slow_preparation_keeps_one_root_and_batch_across_midnight_and_directory_change(
    overview, qtbot, tmp_path, monkeypatch
):
    runtime, _, dashboard, keys, _ = overview
    selected = keys[:3]
    select(dashboard, selected)
    clock = [START]
    monkeypatch.setattr("lab_control.storage.paths.local_now", lambda: clock[0])
    original_root = runtime.repository.data_root
    entered, release = threading.Event(), threading.Event()
    factory = runtime.repository.create_recording

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return factory(*args, **kwargs)

    monkeypatch.setattr(runtime.repository, "create_recording", delayed)
    try:
        dashboard.toggle_sampling()
        assert entered.wait(3)
        assert not original_root.exists()
        pulses = []
        QTimer.singleShot(0, lambda: pulses.append(True))
        qtbot.waitUntil(lambda: bool(pulses))
        clock[0] += timedelta(seconds=2)
        runtime.repository.set_data_root(tmp_path / "next-root")
    finally:
        release.set()
    recorders = finish_batch(runtime, dashboard, selected, qtbot)
    assert len({r.folder.parent for r in recorders}) == 1
    assert all(r.folder.is_relative_to(original_root / "批量采集" / "2026-10-26") for r in recorders)
    assert all(metadata(r)["started_local"] == START.isoformat() for r in recorders)
    assert not list(runtime.repository.data_root.rglob("recording.json"))


def test_one_device_preparation_failure_does_not_relocate_or_stop_batch_peers(overview, qtbot, monkeypatch):
    runtime, controller, dashboard, keys, _ = overview
    select(dashboard, keys[:3])
    factory = runtime.repository.create_recording
    attempted = []
    messages = []
    controller.message.connect(messages.append)

    def selective_failure(*args, **kwargs):
        context = kwargs["context"]
        attempted.append(context)
        if context.device_id == keys[1]:
            raise PermissionError("fixture batch device cannot write")
        return factory(*args, **kwargs)

    monkeypatch.setattr(runtime.repository, "create_recording", selective_failure)
    dashboard.toggle_sampling()
    recorders = finish_batch(runtime, dashboard, [keys[0], keys[2]], qtbot)
    qtbot.waitUntil(lambda: runtime.monitoring.tasks[keys[1]].recording_preparation is None)
    controller.poll()
    assert any("fixture batch device cannot write" in message for message in messages)
    assert not runtime.monitoring.tasks[keys[1]].running
    assert not runtime.sessions.sessions[keys[1]].recording
    assert len({context.batch for context in attempted}) == 1
    assert all(context.batch is not None for context in attempted)
    assert len({recorder.folder.parent for recorder in recorders}) == 1
    assert not (runtime.repository.data_root / "单设备采集").exists()


def test_batch_context_cannot_be_silently_used_for_nonrecording_monitor(overview):
    runtime, _, _, keys, _ = overview
    batch = runtime.repository.new_batch_context()
    with pytest.raises(ValidationError, match="requires recording"):
        runtime.monitoring.start(keys[0], (AcquisitionRequest("scalar", quantity="power"),), batch=batch)
    assert not runtime.monitoring.tasks and not batch.folder.exists()
