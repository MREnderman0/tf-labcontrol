import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

from .fake import Peer


@pytest.fixture
def workspace(qtbot, tmp_path):
    peer = Peer()
    config = ConnectionConfig(peer.resource, "rs.rtm3004", "mock")
    app = ApplicationRuntime((config,), root=tmp_path, hardware=False)
    app.sessions.factory = lambda cfg: build_scpi_adapter(cfg, peer)
    key = app.add(config)
    app.connect(key).result(5)
    window = RealWindow(app)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", "oscilloscope", key)
    page = window.stack.currentWidget()
    qtbot.waitUntil(lambda: page.controls and len(page.controls.values) >= 27 and not page.controls.running)
    try:
        yield window, page, app, key, peer
    finally:
        if peer.release:
            peer.release.set()
        window.timer.stop()
        window.controller.close()
        app.close()
        window.closing = True
        window.bridge.worker.shutdown()


@pytest.mark.parametrize("monitor,channels", [(True, (1, 2, 3)), (False, (1,)), (True, ())])
def test_auto_refreshes_model_state_and_only_resumes_previous_monitor(workspace, qtbot, monitor, channels):
    _, page, app, key, peer = workspace
    peer.auto_channels = channels
    if monitor:
        page.start()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    assert page.controls.auto_button.isEnabled()
    page.controls.auto_button.click()
    assert page.controls.auto_button.text() == "自动设置中…"
    qtbot.waitUntil(lambda: not page.controls.running)
    assert peer.history.count("AUT") == 1
    assert page.controls.channels() == channels
    assert page.controls.values[1, "timebase"] == 0.0002
    assert page.controls.values[1, "scale"] == 0.2
    assert page.controls.values[1, "offset"] == 0.05
    assert bool(app.monitoring.snapshots().get(key, {}).get("running")) == bool(monitor and channels)
    if monitor and channels:
        qtbot.waitUntil(lambda: page.plot.displayed_frame.configuration_version == 1)
        assert page.plot.displayed_frame.request.channels == channels
        assert len(page.plot.grid_lines) == 22


@pytest.mark.parametrize("rejected", [False, True])
def test_user_stop_wins_even_when_auto_completes_or_is_rejected(workspace, qtbot, rejected):
    _, page, app, key, peer = workspace
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    peer.entered, peer.release = threading.Event(), threading.Event()
    if rejected:
        peer.auto_error = '-221,"Settings conflict"'
    page.controls.auto_button.click()
    qtbot.waitUntil(peer.entered.is_set)
    page.toggle_monitoring()
    peer.release.set()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert not app.monitoring.snapshots()[key]["running"]
    assert peer.history.count("AUT") == 1
    assert page.controls.values[1, "timebase"] == 0.0002
    if rejected:
        assert "Settings conflict" in page.controls.status.text()


def test_rejected_auto_reads_actual_state_and_resumes_without_replaying(workspace, qtbot):
    _, page, app, key, peer = workspace
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    peer.auto_error = '-221,"Settings conflict"'
    page.controls.auto_button.click()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert peer.history.count("AUT") == 1
    assert page.controls.values[1, "timebase"] == 0.0002
    assert app.monitoring.snapshots()[key]["running"]
    assert "Settings conflict" in page.controls.status.text()


def test_auto_timeout_does_not_resume_or_publish_new_settings(workspace, qtbot):
    _, page, app, key, peer = workspace
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    peer.fail = "*OPC?"
    page.controls.auto_button.click()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert peer.history.count("AUT") == 1 and peer.closed
    assert not app.monitoring.snapshots()[key]["running"]
    assert app.sessions.sessions[key].state == "degraded"
    assert not page.controls.auto_button.isEnabled()


def test_recording_blocks_auto_in_ui_and_session(workspace, qtbot):
    _, page, app, key, peer = workspace
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    app.monitoring.set_recording(key, True)
    qtbot.waitUntil(lambda: app.sessions.sessions[key].controls_locked)
    page.controller.poll()
    assert not page.controls.auto_button.isEnabled()
    with pytest.raises(DeviceError, match="锁定"):
        app.sessions.submit(key, "apply", DeviceCommand("auto_setup", confirmed=True))
    assert "AUT" not in peer.history
    assert not page.controls.gear.isEnabled()


def choose_trigger_mode(page, value):
    def choose():
        dialog = QApplication.activeModalWidget()
        combo = next(c for c in dialog.findChildren(WorkspaceComboBox) if c.findData("AUTO") >= 0)
        combo.setCurrentIndex(combo.findData(value))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Apply).click()

    QTimer.singleShot(0, choose)
    page.controls.trigger_dialog()


def test_stopped_empty_channel_pauses_without_reconnect_then_explicit_run_updates(workspace, qtbot):
    _, page, app, key, peer = workspace
    peer.empty_channels = {4}
    page.start()
    qtbot.waitUntil(lambda: bool(app.monitoring.snapshots()[key]["notice"]))
    qtbot.waitUntil(lambda: not app.sessions.sessions[key].pending)
    task = app.monitoring.tasks[key]
    assert not task.running and task.recovery is None and not task.error
    assert "CH4" in task.notice and "仪器已停止" in task.notice
    assert not peer.closed and app.sessions.sessions[key].adapter.transport is peer
    assert not any(c.endswith(":DATA?") for c in peer.history)
    assert "CHAN4:DATA:XOR?" not in peer.history and "RUN" not in peer.history
    assert page.plot.displayed_frame is None
    qtbot.waitUntil(lambda: page.controls.gear.isEnabled())
    choose_trigger_mode(page, "AUTO")
    qtbot.waitUntil(lambda: not page.controls.running)
    assert peer.history.count("RUN") == 1
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    assert page.plot.displayed_frame.batch.performance["instrument_running"] is True
    assert page.plot.displayed_frame.request.channels == (1, 2, 3, 4)
    choose_trigger_mode(page, "STOP")
    qtbot.waitUntil(lambda: not page.controls.running)
    qtbot.waitUntil(lambda: page.plot.displayed_frame.batch.performance["instrument_running"] is False)
    page.refresh()
    assert "冻结波形" in page.status.text() and "仪器已停止" in page.status.text()
    assert peer.history.count("STOP") == 1 and peer.history.count("RUN") == 1


def test_empty_record_does_not_commit_or_reconnect(workspace, qtbot):
    _, page, app, key, peer = workspace
    # Start recording before scheduling the first empty frame.
    peer.empty_channels = {4}
    with app.monitoring.lock:
        page.start()
        preparation = app.monitoring.set_recording(key, True, background=True)
    preparation.result(5)
    qtbot.waitUntil(lambda: app.monitoring.tasks[key].recorder is not None)
    qtbot.waitUntil(lambda: not app.monitoring.tasks[key].running)
    recorder = app.monitoring.tasks[key].recorder
    qtbot.waitUntil(lambda: recorder.finished.done())
    assert recorder.received == recorder.committed == 0
    assert not list(recorder.folder.glob("segment-*"))
    assert app.sessions.sessions[key].state == "connected"
    assert app.monitoring.tasks[key].recovery is None and not peer.closed
