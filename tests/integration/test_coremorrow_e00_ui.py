from dataclasses import replace

import pytest

from lab_control.app import load_stylesheet
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ParameterReading
from lab_control.ui.pages.device_control.coremorrow_e00 import CoreMorrowE00Page
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def e00(qapp, qtbot, tmp_path):
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://coremorrow-ui", "coremorrow.xmt", "mock"))
    runtime.connect(key).result(5)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("设备管理", "power_supply", key)
    page = window.stack.currentWidget()
    assert isinstance(page, CoreMorrowE00Page)
    qtbot.waitUntil(lambda: len(page.panels) == 3, timeout=5000)
    qtbot.waitUntil(
        lambda: (1, "voltage") in runtime.sessions.sessions[key].frames,
        timeout=5000,
    )
    try:
        yield window, page, runtime, key, runtime.sessions.sessions[key].adapter
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def _edit(panel, value: str) -> None:
    field = panel.editor.editors["value"]
    field.restore({"value": value, "factor": 1})
    panel.editor.save_draft()
    panel.refresh()


def test_e00_readback_is_separate_from_target_input(e00, qtbot):
    _, page, runtime, key, _ = e00
    panel = page.panels[1]
    _edit(panel, "2")
    panel.editor.show_reading(ParameterReading("voltage", 1, 2.0002, "instrument"))
    assert panel.editor.editors["value"].input.text() == "2"
    session = runtime.sessions.sessions[key]
    frame = session.frames[(1, "voltage")]
    session.frames[(1, "voltage")] = replace(
        frame,
        sequence=frame.sequence + 1,
        reading=ParameterReading("voltage", 1, 2.0002, "instrument"),
    )
    page.update_readings(key)
    assert panel.actual_display.value() == pytest.approx(2.0002, abs=0.0001)
    assert "2.0002" in panel.actual_detail.text()
    assert panel.editor.editors["value"].input.text() == "2"


def test_e00_step_uses_draft_and_sends_one_voltage_write(e00, qtbot, monkeypatch):
    _, page, _, _, adapter = e00
    panel = page.panels[1]
    _edit(panel, "1")
    panel.step_value.setValue(0.5)
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda command: (writes.append(command), original(command))[1])
    panel.step_button.click()
    qtbot.waitUntil(lambda: not panel.running, timeout=5000)
    assert len(writes) == 1
    assert writes[0].operation == "voltage"
    assert writes[0].channel == 1
    assert writes[0].parameters == {"value": 1.5}
    assert panel.editor.editors["value"].input.text() == "1.5"


def test_e00_negative_step_uses_draft(e00, qtbot, monkeypatch):
    _, page, _, _, adapter = e00
    panel = page.panels[2]
    _edit(panel, "1.5")
    panel.direction.setCurrentIndex(1)
    panel.step_value.setValue(0.5)
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda command: (writes.append(command), original(command))[1])
    panel.step_button.click()
    qtbot.waitUntil(lambda: not panel.running, timeout=5000)
    assert len(writes) == 1
    assert writes[0].channel == 2
    assert writes[0].parameters == {"value": 1.0}
    assert panel.editor.editors["value"].input.text() == "1"


def test_e00_step_rejects_out_of_range_target_without_write(e00, qtbot, monkeypatch):
    _, page, _, _, adapter = e00
    panel = page.panels[1]
    _edit(panel, "0.1")
    panel.direction.setCurrentIndex(1)
    panel.step_value.setValue(0.2)
    writes = []
    monkeypatch.setattr(adapter, "apply", lambda command: writes.append(command))
    panel.step_button.click()
    qtbot.wait(100)
    assert writes == []
    assert "范围" in panel.status.text()
    assert panel.editor.editors["value"].input.text() == "0.1"


def test_e00_channels_are_compact_vertical_rows(e00):
    _, page, _, _, _ = e00
    assert list(page.panels) == [1, 2, 3]
    assert page.panels[1].y() < page.panels[2].y() < page.panels[3].y()
    assert page.scroll_area.horizontalScrollBar().maximum() == 0
    assert all(panel.step_value.value() == pytest.approx(0.1) for panel in page.panels.values())
