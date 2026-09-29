import threading
from dataclasses import replace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QMessageBox, QPushButton

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ApplyResult, DeviceError, ParameterReading, Quality
from lab_control.ui.pages.device_control.power_supply import PowerSupplyPage
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def power(qapp, qtbot, tmp_path):
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://power-panel", "gwinstek.gpd4303s", "mock"))
    runtime.connect(key).result()
    adapter = runtime.sessions.sessions[key].adapter
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    # Retain regression coverage for the importable legacy control widget.
    # Public navigation is covered by test_power_workspace.
    page = PowerSupplyPage(window.controller)
    window.stack.addWidget(page)
    window.stack.setCurrentWidget(page)
    page.selector.select(key)
    with runtime.monitoring.lock:
        runtime.monitoring.settings_sync.foreground_interval = 0.1
    qtbot.waitUntil(lambda: all(p.count.text() == "正常" for p in page.panels.values()), timeout=6000)
    try:
        yield window, page, runtime, key, adapter
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def edit(qtbot, page, channel, name, value):
    editor = page.panels[channel].editors[name]
    field = editor.editors["value"]
    field.input.setFocus()
    field.input.selectAll()
    qtbot.keyClicks(field.input, value)
    page.selector.combo.setFocus()
    qtbot.wait(120)
    return editor, field


def test_grid_auto_reads_draft_protection_measurement_and_channel_apply(power, qtbot, monkeypatch):
    window, page, runtime, key, adapter = power
    assert list(page.panels) == [1, 2, 3, 4]
    assert page.selector.channel.isHidden()
    assert page.output.operation == "all_outputs"
    assert all(p.output is None for p in page.panels.values())
    assert not any(
        "读取" in b.text() or "LOCK" in b.text() for b in page.findChildren(QPushButton) if not b.isHidden()
    )
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda command: (writes.append(command), original(command))[1])
    editor, field = edit(qtbot, page, 1, "voltage", "2.5")
    assert page.panels[1].apply_button.isEnabled()
    assert not page.panels[2].apply_button.isEnabled()
    adapter.values["1.voltage"] = {"value": 3.3}
    qtbot.waitUntil(lambda: editor.current_value == {"value": 3.3})
    assert field.input.text() == "2.5" and editor.dirty
    assert "3.3" in editor.readback.text()
    assert "3.3" in page.panels[1].measurements["voltage"].text()
    assert not writes
    page.panels[1].apply_button.click()
    qtbot.waitUntil(lambda: not editor.dirty and not page.panels[1].running)
    assert [(c.channel, c.operation, c.parameters) for c in writes] == [(1, "voltage", {"value": 2.5})]
    assert adapter.values["2.voltage"] == {"value": 1}
    qtbot.waitUntil(lambda: not page.panels[1].apply_button.isEnabled())


def test_units_nan_live_update_and_raw_measurement_never_clamped(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    editor = page.panels[4].editors["current"]
    field = editor.editors["value"]
    field.unit.setCurrentIndex(field.unit.findText("mA"))
    page.selector.combo.setFocus()
    adapter.values["4.current"] = {"value": 0.25}
    qtbot.waitUntil(lambda: field.input.text() == "250")
    edit(qtbot, page, 4, "current", "nan")
    assert field.input.text() == "250" and not editor.dirty
    assert field.feedback.isHidden()
    original = adapter.read_settings
    monkeypatch.setattr(
        adapter,
        "read_settings",
        lambda names, channel: tuple(
            replace(r, value=1.234) if channel == 4 and r.operation == "measured_current" else r
            for r in original(names, channel)
        ),
    )
    qtbot.waitUntil(lambda: "1234 mA" in page.panels[4].measurements["current"].text())
    assert field.input.text() == "250"
    assert not runtime.repository.load_workspace(editor.key)


def test_partial_failure_preserves_other_drafts_and_output_is_separate(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    edit(qtbot, page, 2, "voltage", "2")
    current, _ = edit(qtbot, page, 2, "current", "0.2")
    original = adapter.apply
    writes = []

    def apply(command):
        writes.append(command.operation)
        if command.operation == "current":
            return ApplyResult("rejected", "current", command.parameters, error="fixture failure")
        return original(command)

    monkeypatch.setattr(adapter, "apply", apply)
    page.panels[2].apply_button.click()
    qtbot.waitUntil(lambda: not page.panels[2].running)
    assert writes == ["voltage", "current"]
    assert current.dirty and "失败" in page.panels[2].status.text()
    qtbot.waitUntil(page.output.button.isEnabled)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    page.output.button.click()
    assert writes == ["voltage", "current"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    page.output.button.click()
    qtbot.waitUntil(lambda: adapter.values["1.all_outputs"] == {"value": True})
    assert writes == ["voltage", "current", "all_outputs"]
    assert current.dirty


def test_three_channels_fallback_and_low_work_area(power, qtbot):
    window, page, runtime, _, _ = power
    key = runtime.add(ConnectionConfig("mock://three-channel", "itech.it6322", "mock"))
    runtime.connect(key).result()
    window.controller.poll()
    page.selector.select(key)
    assert list(page.panels) == [1, 2, 3]
    assert page.output is None
    assert all(p.output.operation == "output" for p in page.panels.values())
    window.fit_to_screen(QRect(0, 0, 1024, 600))
    qtbot.wait(100)
    assert window.frameGeometry().width() <= 1024 and window.frameGeometry().height() <= 600
    assert window.sidebar.width() == 232
    assert page.panels[1].y() == page.panels[2].y()
    assert page.panels[3].y() > page.panels[1].y()
    assert page.scroll_area.horizontalScrollBar().maximum() == 0
    for panel in page.panels.values():
        assert panel.visible_columns == 2
        assert panel.apply_button.size() == page.panels[1].apply_button.size()


def test_read_failure_does_not_turn_output_into_off(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    original = adapter.read_settings
    monkeypatch.setattr(
        adapter,
        "read_settings",
        lambda names, channel: tuple(
            ParameterReading(r.operation, r.channel, None, "instrument", quality=Quality.PARSE_ERROR)
            if r.operation == "all_outputs"
            else r
            for r in original(names, channel)
        ),
    )
    runtime.monitoring.refresh_settings(key)
    qtbot.waitUntil(lambda: page.output.value is None)
    assert "未知" in page.output.state.text() and not page.output.button.isEnabled()


def test_disconnect_waits_for_read_releases_once_and_never_writes_settings(tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://release", "gwinstek.gpd4303s", "mock"))
    runtime.connect(key).result()
    adapter = runtime.sessions.sessions[key].adapter
    history = []
    started, gate = threading.Event(), threading.Event()
    original = adapter.read_settings

    def read(names, channel):
        history.append("read")
        started.set()
        assert gate.wait(3)
        result = original(names, channel)
        history.append("read_finished")
        return result

    adapter.read_settings = read
    adapter.release_control = lambda: history.append("LOCAL")
    adapter.close = lambda: history.append("close")
    adapter.apply = lambda command: history.append("unexpected_write")
    future = runtime.sessions.try_submit_idle(key, "read_settings", (("voltage",), 1), quiet=True)
    assert started.wait(2)
    worker = threading.Thread(target=runtime.sessions.disconnect, args=(key,), kwargs={"drain_reads": True})
    worker.start()
    gate.set()
    worker.join(4)
    future.result()
    assert history == ["read", "read_finished", "LOCAL", "close"]
    assert key not in runtime.sessions.sessions
    runtime.close()


def test_local_failure_cleans_up_and_reports_unconfirmed(tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://release-fail", "gwinstek.gpd4303s", "mock"))
    runtime.connect(key).result()
    adapter = runtime.sessions.sessions[key].adapter

    def fail():
        raise DeviceError("fixture timeout", "timeout")

    adapter.release_control = fail
    with pytest.raises(DeviceError, match="返回本地控制未确认"):
        runtime.sessions.disconnect(key)
    assert adapter.closed and key not in runtime.sessions.sessions
    runtime.close()
