from dataclasses import replace

import pytest
from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QFocusEvent, QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ApplyResult, ParameterReading
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def source(qtbot, qapp, tmp_path):
    from lab_control.app import load_stylesheet, load_ui_font

    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyle("Fusion")
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime((), root=tmp_path / "runtime", hardware=False)
    ids = {}
    for name, adapter in (("rf", "rs.sma100b"), ("awg", "tek.afg31252")):
        cfg = ConnectionConfig(f"mock://{name}", adapter, "mock", identity_confirmed=True)
        ids[name] = runtime.add(cfg)
        runtime.connect(ids[name]).result()
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("设备管理", "signal_generator", ids["awg"])
    page = window.stack.currentWidget()
    qtbot.waitUntil(
        lambda: all(p.editors["frequency"].current_value is not None for p in page.panels.values())
    )
    try:
        yield window, page, runtime, ids
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def edit(qtbot, panel, name, value):
    field = panel.editors[name].editors["value"]
    field.input.setFocus()
    field.input.selectAll()
    qtbot.keyClicks(field.input, str(value))
    return field


def test_channel_apply_and_drafts_survive_resize(source, qtbot, monkeypatch):
    window, page, runtime, ids = source
    first, second = page.panels[1], page.panels[2]
    adapter = runtime.sessions.sessions[ids["awg"]].adapter
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: (writes.append(cmd), original(cmd))[1])
    field = edit(qtbot, first, "amplitude", "1e20")
    qtbot.keyClick(field.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: float(field.input.text()) == field.spec.maximum)
    first.read()
    qtbot.waitUntil(lambda: not first.reading)
    assert first.editors["amplitude"].dirty
    assert float(field.input.text()) == field.spec.maximum
    edit(qtbot, second, "offset", "-0.1")
    sessions = [s["sessionId"] for s in runtime.sessions.snapshots()]
    for size in ((1100, 760), (1440, 900)):
        window.resize(*size)
        qtbot.wait(10)
    assert first is page.panels[1] and second is page.panels[2]
    assert second.editors["offset"].dirty
    assert [s["sessionId"] for s in runtime.sessions.snapshots()] == sessions
    assert writes == []
    first.apply()
    qtbot.waitUntil(lambda: not first.running)
    assert [(c.operation, c.channel) for c in writes] == [("amplitude", 1)]
    assert second.editors["offset"].dirty
    assert not first.editors["amplitude"].dirty
    assert not page.outputs[1].value and not page.outputs[2].value


def test_partial_failure_stops_remaining_commands(source, qtbot, monkeypatch):
    window, page, runtime, ids = source
    panel = page.panels[1]
    adapter = runtime.sessions.sessions[ids["awg"]].adapter
    writes = []
    original = adapter.apply

    def apply(command):
        writes.append(command.operation)
        if command.operation == "amplitude":
            return ApplyResult("rejected", "amplitude", command.parameters, error="fixture rejection")
        return original(command)

    monkeypatch.setattr(adapter, "apply", apply)
    edit(qtbot, panel, "frequency", "2")
    edit(qtbot, panel, "amplitude", "2")
    edit(qtbot, panel, "offset", "0.2")
    panel.apply()
    qtbot.waitUntil(lambda: not panel.running)
    assert writes == ["frequency", "amplitude"]
    assert not panel.editors["frequency"].dirty
    assert panel.editors["amplitude"].dirty and panel.editors["offset"].dirty
    assert "失败：幅度" in panel.status.text() and "未执行：偏移" in panel.status.text()
    assert not window.controller.group_devices


def test_changed_range_corrects_draft_and_stops_before_write(source, qtbot, monkeypatch):
    _, page, runtime, ids = source
    panel = page.panels[1]
    adapter = runtime.sessions.sessions[ids["awg"]].adapter
    original = adapter.apply
    writes = []

    def apply(command):
        result = original(command)
        writes.append(command.operation)
        if command.operation == "frequency":
            adapter.caps = replace(
                adapter.caps,
                operations=tuple(
                    replace(cap, parameters={"value": replace(cap.parameters["value"], maximum=0.5)})
                    if cap.name == "amplitude"
                    else cap
                    for cap in adapter.caps.operations
                ),
            )
        return result

    monkeypatch.setattr(adapter, "apply", apply)
    edit(qtbot, panel, "frequency", "2")
    field = edit(qtbot, panel, "amplitude", "3")
    panel.apply()
    qtbot.waitUntil(lambda: not panel.running)
    assert writes == ["frequency"]
    assert float(field.input.text()) == 0.5
    assert panel.editors["amplitude"].dirty
    assert "再次应用" in panel.status.text()


def test_readback_not_clamped_and_output_requires_separate_action(source, qtbot, monkeypatch):
    window, page, runtime, ids = source
    window.navigate("设备管理", "signal_generator", ids["rf"])
    qtbot.waitUntil(lambda: not window.controller.jobs)
    panel = page.panels[1]
    power = panel.editors["power"]
    power.show_reading(ParameterReading("power", 1, {"value": 21}, "instrument"))
    assert "21" in power.readback.text()
    assert power.editors["value"].input.text() == "21"
    adapter = runtime.sessions.sessions[ids["rf"]].adapter
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: (writes.append(cmd), original(cmd))[1])
    edit(qtbot, panel, "frequency", "120")
    panel.apply()
    qtbot.waitUntil(lambda: not panel.running)
    assert all(c.operation != "output" for c in writes)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.No)
    page.outputs[1].toggle()
    assert all(c.operation != "output" for c in writes)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.Yes)
    page.outputs[1].toggle()
    qtbot.waitUntil(lambda: not page.outputs[1].busy)
    assert writes[-1].operation == "output" and writes[-1].confirmed


def test_compact_default_buttons_visible(source, qtbot):
    window, page, _, _ = source
    for size in ((1440, 900), (1100, 760)):
        window.resize(*size)
        qtbot.wait(100)
        assert page.last_columns == 2
        assert page.scroll_area.horizontalScrollBar().maximum() == 0
        for panel in page.panels.values():
            point = panel.apply_button.mapTo(
                page.scroll_area.viewport(), panel.apply_button.rect().bottomRight()
            )
            assert point.y() < page.scroll_area.viewport().height()


def test_clamp_feedback_never_overlaps_value_or_range(source, qtbot):
    window, page, _, _ = source
    for size in ((1440, 900), (1100, 760)):
        window.resize(*size)
        field = edit(qtbot, page.panels[1], "amplitude", "1e20")
        qtbot.keyClick(field.input, Qt.Key.Key_Return)
        qtbot.wait(150)
        assert field.feedback.isVisible()
        assert field.range_label.y() > field.input.geometry().bottom()
        assert field.feedback.y() > field.range_label.geometry().bottom()
        editor = page.panels[1].editors["amplitude"]
        assert editor.readback.isVisible()
        assert editor.readback.y() > field.geometry().bottom()
        assert editor.status.y() > field.geometry().bottom()
        assert page.panels[1].editors["offset"].y() > editor.geometry().bottom()


def test_small_work_area_keeps_two_columns_and_sidebar(source, qtbot):
    window, page, _, _ = source
    for width, height in ((1366, 728), (1024, 600), (800, 560)):
        window.resize(1440, 900)
        available = QRect(0, 0, width, height)
        window.fit_to_screen(available)
        qtbot.wait(150)
        assert available.contains(window.frameGeometry())
        assert window.sidebar.width() == 232
        assert page.last_columns == 2
        assert page.channel_cards[0].y() == page.channel_cards[1].y()
        assert page.channel_cards[0].x() < page.channel_cards[1].x()
        assert page.scroll_area.widget().width() <= page.scroll_area.viewport().width()
        for panel in page.panels.values():
            page.scroll_area.ensureWidgetVisible(panel.apply_button)
            qtbot.wait(20)
            button = panel.apply_button
            corner = button.mapTo(page.scroll_area.viewport(), button.rect().bottomRight())
            assert 0 <= corner.x() < page.scroll_area.viewport().width()
            assert 0 <= corner.y() < page.scroll_area.viewport().height()
        window.resize(10, 10)
        qtbot.wait(50)
        assert window.size() == window.minimumSize()
        assert page.last_columns == 2


def test_dirty_button_matches_changes_and_mock_has_no_duplicate_readback(source, qtbot, monkeypatch):
    _, page, runtime, ids = source
    panel = page.panels[1]
    adapter = runtime.sessions.sessions[ids["awg"]].adapter
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: (writes.append(cmd), original(cmd))[1])
    assert not panel.apply_button.isEnabled()
    assert not hasattr(panel, "read_button")
    assert panel.apply_button.width() == 110
    field = edit(qtbot, panel, "amplitude", "2")
    qtbot.waitUntil(panel.apply_button.isEnabled)
    edit(qtbot, panel, "amplitude", "1.0")
    qtbot.waitUntil(lambda: not panel.apply_button.isEnabled())
    assert not writes
    edit(qtbot, panel, "amplitude", "2")
    panel.apply()
    qtbot.waitUntil(lambda: not panel.running)
    assert not panel.apply_button.isEnabled()
    assert panel.editors["amplitude"].current_value == {"value": float(field.input.text())}
    assert not panel.editors["amplitude"].readback.isVisible()
    assert len(writes) == 1


def test_invalid_draft_is_not_saved_and_blur_restores_previous(source, qtbot, monkeypatch):
    _, page, runtime, ids = source
    panel = page.panels[1]
    editor = panel.editors["amplitude"]
    writes = []
    adapter = runtime.sessions.sessions[ids["awg"]].adapter
    monkeypatch.setattr(adapter, "apply", writes.append)
    field = edit(qtbot, panel, "amplitude", "NaN")
    assert runtime.repository.load_workspace(editor.key) is None
    QApplication.sendEvent(field.input, QFocusEvent(QEvent.Type.FocusOut))
    qtbot.waitUntil(lambda: field.input.text() == "1")
    assert not field.feedback.text() and not editor.dirty
    field = edit(qtbot, panel, "amplitude", "2")
    qtbot.keyClick(field.input, Qt.Key.Key_Return)
    qtbot.wait(50)
    edit(qtbot, panel, "amplitude", "inf")
    QApplication.sendEvent(field.input, QFocusEvent(QEvent.Type.FocusOut))
    qtbot.waitUntil(lambda: field.input.text() == "2")
    assert runtime.repository.load_workspace(editor.key)["fields"]["value"]["value"] == "2"
    assert not field.feedback.text() and editor.dirty
    assert not writes
