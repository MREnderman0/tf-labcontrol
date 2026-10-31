import threading

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QToolButton

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.real_window import RealWindow


def test_record_stop_remains_available_during_reconnection(qtbot):
    from lab_control.ui.pages.monitoring.recording_state import RecordingButton

    button = RecordingButton()
    qtbot.addWidget(button)
    button.refresh(
        {"running": True, "recovering": True, "recording": True},
        connected=False,
        busy=False,
        locked=True,
    )
    assert button.isEnabled() and button.text() == "停止记录"
    button.refresh(
        {"running": True, "recovering": True, "recording": False},
        connected=False,
        busy=False,
        locked=True,
    )
    assert not button.isEnabled()


@pytest.mark.parametrize(
    "adapter,category", [("lecroy.9404m", "oscilloscope"), ("rigol.dsa815", "spectrum_analyzer")]
)
def test_record_button_card_outline_switching_and_real_binary_drain(
    qapp, qtbot, tmp_path, monkeypatch, adapter, category
):
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    keys = [runtime.add(ConnectionConfig(f"mock://record-{index}", adapter, "mock")) for index in (1, 2)]
    for key in keys:
        runtime.connect(key).result(5)
    key, other = keys
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", category, key)
    page = window.stack.currentWidget()
    entered, release = threading.Event(), threading.Event()
    factory = runtime.repository.create_recording

    def create(mode, **options):
        recorder = factory(mode, **options)
        append = recorder.segments.append

        def blocked(sequence, batch):
            entered.set()
            assert release.wait(15)
            return append(sequence, batch)

        recorder.segments.append = blocked
        return recorder

    monkeypatch.setattr(runtime.repository, "create_recording", create)

    def ready():
        values = page.controls.values if category == "oscilloscope" else page.controls.current()
        return bool(values) and not page.controls.running

    try:
        qtbot.waitUntil(ready, timeout=6000)
        assert not page.record_button.isEnabled()
        tools = page.findChild(QToolButton, "curveTools")
        assert page.record_button.geometry().right() < tools.geometry().left()
        assert page.record_button.height() == tools.height() == 30
        assert not any("记录" in action.text() for action in tools.menu().actions())
        # Exercise user-stop drain, separately from the queue-full pause tests.
        page.timing.cadence.setCurrentIndex(page.timing.cadence.findData("interval"))
        page.toggle_monitoring()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
        window.controller.poll()
        assert page.record_button.isEnabled()
        page.pin()
        card = next(iter(window.dashboard.cards.values()))
        page.record_button.click()
        qtbot.waitUntil(entered.is_set)
        window.controller.poll()
        card.refresh()
        assert page.record_button.text() == "停止记录", runtime.monitoring.snapshots()[key]
        assert page.chart.property("recordingPhase") == card.property("recordingPhase") == "recording"
        assert "本设备" in card.toolTip()
        page.selector.select(other)
        qtbot.waitUntil(lambda: page.device_id == other and ready(), timeout=6000)
        assert page.chart.property("recordingPhase") == "idle" and not page.record_button.isEnabled()
        card.refresh()
        assert card.property("recordingPhase") == "recording"
        page.selector.select(key)
        qtbot.waitUntil(lambda: page.device_id == key and ready(), timeout=6000)
        window.controller.poll()
        page.record_button.click()
        window.controller.poll()
        card.refresh()
        assert runtime.monitoring.snapshots()[key]["running"]
        assert page.chart.property("recordingPhase") == card.property("recordingPhase") == "draining"
        assert page.record_button.text() == "收尾中…" and not page.record_button.isEnabled()
        assert "收尾中" in page.status.text()
        assert card.state.text() == "收尾中"
        assert "收尾中" in card.detail_state.text()
        assert (
            str(runtime.monitoring.snapshots()[key]["recording_state"]["folder"]) in card.detail_path.text()
        )
        assert next(s for s in runtime.sessions.snapshots() if s["stableDeviceId"] == key)["controlsLocked"]
        release.set()
        qtbot.waitUntil(lambda: not runtime.monitoring.snapshots()[key]["recording_state"]["pending"])
        qtbot.waitUntil(lambda: runtime.monitoring.snapshots()[key]["recording_state"]["binary_done"])
        qtbot.waitUntil(
            lambda: (
                not next(s for s in runtime.sessions.snapshots() if s["stableDeviceId"] == key)[
                    "controlsLocked"
                ]
            )
        )
        window.controller.poll()
        card.refresh()
        assert page.chart.property("recordingPhase") == card.property("recordingPhase") == "idle"
        assert page.record_button.text() == "开始记录" and page.record_button.isEnabled()
        recorder = runtime.monitoring.tasks[key].recorder
        qtbot.waitUntil(recorder.finished.done, timeout=10000)
        window.controller.poll()
        card.refresh()
        assert "分析 CSV 已完成" in page.status.text()
        assert card.state.text() == "待处理"
        assert "分析 CSV 已完成" in card.detail_state.text()
        assert str(recorder.folder) in card.detail_path.text()
        assert "已保存帧数" in card.detail_frames.text()
        assert str(recorder.folder) in page.status.text()
        assert (recorder.folder / "数据_00001.csv").is_file()
        runtime.monitoring.stop(key).result(5)
    finally:
        release.set()
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
