import math
import threading
from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.storage.recording import read_frames
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.form import OperationEditor
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.pages.monitoring.frequency_counter import FrequencyCounterMonitoringPage

from .fake import Counter
from .test_adapter import request


@pytest.fixture
def workspace(qtbot, tmp_path):
    config = ConnectionConfig("mock://counter", "keysight.53220a", "mock", expected_serial="TEST-COUNTER")
    runtime = ApplicationRuntime((config,), root=tmp_path, hardware=False)
    wire = Counter()
    runtime.sessions.factory = lambda c: build_scpi_adapter(c, wire)
    device = runtime.add(config)
    runtime.connect(device, configure_acquisition=False).result(3)
    parent = QWidget()
    qtbot.addWidget(parent)
    controller = WorkspaceController(runtime, parent)
    controller.timer.stop()
    page = FrequencyCounterMonitoringPage(controller)
    qtbot.addWidget(page)
    page.select_device(device)
    page.show()
    yield runtime, device, wire, controller, page
    controller.close()
    runtime.close()


def test_page_and_overview_share_exclusive_selection(workspace, qtbot):
    runtime, device, _, controller, page = workspace
    dashboard = MonitoringDashboard(controller)
    qtbot.addWidget(dashboard)
    assert page.channels() == (1,)
    assert not page.findChildren(OperationEditor)
    assert runtime.monitoring.settings_sync.advanced == ()
    page.channel_checks[2].click()
    assert page.channels() == (2,)
    assert dashboard._channels(device, controller.records[device]) == (2,)
    dashboard.refresh()
    grouped, _ = dashboard._requests()
    assert grouped[0][1][0].channels == (2,)
    assert grouped[0][1][0].points == 1
    page.channel_checks[2].click()
    assert page.channels() == (2,)


def test_trend_export_and_every_frame_recording(workspace, qtbot):
    runtime, device, _, controller, page = workspace
    runtime.monitoring.start(device, (request(),), interval=0.05, recording=True)
    qtbot.waitUntil(lambda: runtime.monitoring.snapshots()[device]["sequence"] >= 4, timeout=5000)
    runtime.monitoring.stop(device).result(10)
    controller.poll()
    page.refresh()
    page.refresh_frame(device)
    frame = page.plot.displayed_frame
    assert frame is not None
    assert {key[0] for key in page.plot.curves} == {1}
    history = list(page.plot.history[(1, "frequency")])
    page.refresh_frame(device)
    assert list(page.plot.history[(1, "frequency")]) == history
    assert history == sorted(history)
    exported = runtime.monitoring.export_displayed_frame(device, frame, "general").result(10)
    assert Path(exported).exists()
    recorder = runtime.monitoring.tasks[device].recorder
    paths = recorder.finish().result(10)
    frames = [f for path in paths for f in read_frames(Path(path))]
    assert len(frames) == recorder.snapshot()["committed"] == recorder.snapshot()["received"]
    assert len(frames) >= 4
    assert all(f["measurements"][0]["unit"] == "Hz" for f in frames)


def test_channel_lock_until_pending_measurement_finishes(workspace, qtbot):
    runtime, device, wire, _, page = workspace
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(3)

    wire.during_read = hold
    try:
        runtime.monitoring.start(device, (request(),), interval=0.1, recording=True)
        qtbot.waitUntil(entered.is_set, timeout=2000)
        stopped = runtime.monitoring.stop(device)
        page.refresh()
        assert not page.channel_checks[2].isEnabled()
        release.set()
        stopped.result(10)
        qtbot.waitUntil(
            lambda: not runtime.monitoring.snapshots()[device].get("recording_draining"), timeout=5000
        )
        page.refresh()
        assert page.channel_checks[2].isEnabled()
    finally:
        release.set()


@pytest.mark.parametrize("invalid", [math.nan, 9.91e37])
def test_invalid_frame_never_reaches_trend_or_disk(workspace, qtbot, invalid):
    runtime, device, wire, _, page = workspace
    wire.measurement = invalid
    runtime.monitoring.start(device, (request(),), interval=0.05, recording=True)
    qtbot.waitUntil(lambda: bool(runtime.monitoring.snapshots()[device].get("error")), timeout=5000)
    runtime.monitoring.stop(device).result(10)
    page.refresh_frame(device)
    assert page.plot.displayed_frame is None
    assert runtime.monitoring.latest(device, "scalar", "frequency") is None
    recorder = runtime.monitoring.tasks[device].recorder
    if recorder:
        recorder.finish().result(10)
        assert recorder.snapshot()["committed"] == 0
