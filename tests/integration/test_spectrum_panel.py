from dataclasses import asdict, replace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QMessageBox

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ApplyResult, Capability
from lab_control.domain.spectrum import SpectrumMarker
from lab_control.ui.pages.monitoring.spectrum import SpectrumMonitoringPage
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def spectrum(qapp, qtbot, tmp_path):
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://spectrum-ui", "rigol.dsa815", "mock"))
    runtime.connect(key).result()
    # GUI contract fixture, independent of the separately maintained DSA815 module migration.
    adapter = runtime.sessions.sessions[key].adapter
    if not any(c.name == "auto_setup" for c in adapter.caps.operations):
        adapter.caps = replace(
            adapter.caps,
            operations=(
                *adapter.caps.operations,
                Capability("auto_setup", writable=True, verification="simulated"),
            ),
        )
        runtime.sessions.submit(key, "refresh_capabilities").result()
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", "spectrum_analyzer", key)
    page = window.stack.currentWidget()
    qtbot.waitUntil(lambda: len(page.controls.current()) == 11, timeout=6000)
    try:
        yield window, page, runtime, key, runtime.sessions.sessions[key].adapter
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def edit(qtbot, page, name, value):
    field = page.controls.editors[name].editors["value"]
    field.input.setFocus()
    field.input.selectAll()
    qtbot.keyClicks(field.input, value)
    page.selector.combo.setFocus()
    qtbot.wait(160)
    return field


def test_navigation_layout_linked_frequency_apply_and_nan(spectrum, qtbot, monkeypatch):
    window, page, runtime, key, adapter = spectrum
    window.navigate("设备管理")
    assert "spectrum_analyzer" not in [window.submenu.tabData(i) for i in range(window.submenu.count())]
    window.navigate("设备管理", "spectrum_analyzer", key)
    assert isinstance(window.stack.currentWidget(), SpectrumMonitoringPage)
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda c: (writes.append(c), original(c))[1])
    field = edit(qtbot, page, "stop_frequency", "4")  # MHz
    assert page.controls.target()["span"] == 4e6
    assert page.controls.target()["center_frequency"] == 2e6
    assert not writes
    edit(qtbot, page, "stop_frequency", "nan")
    assert field.input.text() == "4" and field.feedback.isHidden()
    page.controls.apply_button.click()
    qtbot.waitUntil(lambda: not page.controls.running and not page.controls.dirty(), timeout=5000)
    assert [c.operation for c in writes] == ["stop_frequency"]
    assert not page.controls.apply_button.isEnabled()
    window.fit_to_screen(QRect(0, 0, 1024, 600))
    qtbot.wait(200)
    assert page.plot.width() >= 200
    assert page.controls.apply_button.isVisible()
    assert page.splitter.orientation().name == "Horizontal"


def test_auto_confirm_and_markers_do_not_write(spectrum, qtbot, monkeypatch):
    _, page, runtime, key, adapter = spectrum
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda c: (writes.append(c), original(c))[1])
    marker = SpectrumMarker(1, 1e6, True, 1e5)
    page.markers = [marker]
    runtime.repository.save_workspace(page.marker_key, [asdict(marker)])
    page.plot.set_markers(page.markers)
    page.toggle_monitoring()
    qtbot.waitUntil(lambda: page.plot.readings[1].amplitude is not None)
    assert not writes
    field = edit(qtbot, page, "reference_level", "-15")
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    page.controls.auto_button.click()
    assert not writes and field.input.text() == "-15"
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    page.controls.auto_button.click()
    qtbot.waitUntil(lambda: not page.controls.running, timeout=5000)
    assert [c.operation for c in writes] == ["auto_setup"]
    qtbot.waitUntil(lambda: field.input.text() == "-10")
    assert runtime.monitoring.snapshots()[key]["running"]


def test_partial_failure_stops_and_keeps_remaining_draft(spectrum, qtbot, monkeypatch):
    _, page, _, _, adapter = spectrum
    writes = []
    edit(qtbot, page, "stop_frequency", "4")
    edit(qtbot, page, "reference_level", "-15")
    original = adapter.apply

    def apply(command):
        writes.append(command.operation)
        if command.operation == "reference_level":
            return ApplyResult("rejected", command.operation, command.parameters, error="fixture rejection")
        return original(command)

    monkeypatch.setattr(adapter, "apply", apply)
    page.controls.apply_button.click()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert writes == ["stop_frequency", "reference_level"]
    assert page.controls.editors["reference_level"].dirty
    assert "失败" in page.controls.status.text()


def test_draft_units_device_isolation_and_failed_auto(spectrum, qtbot, monkeypatch):
    _, page, runtime, key, adapter = spectrum
    field = edit(qtbot, page, "stop_frequency", "4")
    field.unit.setCurrentIndex(field.unit.findText("kHz"))
    qtbot.wait(100)
    assert field.input.text() == "4000"
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(adapter, "apply", lambda c: ApplyResult("rejected", c.operation, {}, error="timeout"))
    page.controls.auto_button.click()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert field.input.text() == "4000" and page.controls.editors["stop_frequency"].dirty
    second = runtime.add(ConnectionConfig("mock://spectrum-second", "rs.fph", "mock"))
    runtime.connect(second).result()
    qtbot.waitUntil(lambda: second in page.controller.records)
    page.selector.select(second)
    assert not page.controls.auto_button.isEnabled()
    page.selector.select(key)
    qtbot.waitUntil(lambda: len(page.controls.current()) == 11)
    field = page.controls.editors["stop_frequency"].editors["value"]
    assert field.input.text() == "4000" and field.unit.currentText() == "kHz"


def test_slow_reader_keeps_ui_editable_and_marker_dialog_local(spectrum, qtbot, monkeypatch):
    import threading

    from PySide6.QtCore import Qt

    from lab_control.ui.pages.monitoring.spectrum_markers import MarkerDialog

    _, page, runtime, key, adapter = spectrum
    gate, entered = threading.Event(), threading.Event()
    original = adapter.acquire
    monkeypatch.setattr(
        adapter, "acquire", lambda request: (entered.set(), gate.wait(3), original(request))[2]
    )
    try:
        page.toggle_monitoring()
        qtbot.waitUntil(entered.is_set)
        field = edit(qtbot, page, "reference_level", "-23")
        assert field.input.text() == "-23"
        dialog = MarkerDialog(SpectrumMarker(1, 1e6), page)
        qtbot.addWidget(dialog)
        dialog.tracking.setChecked(True)
        dialog.save()
        assert dialog.marker.tracking and dialog.result() == dialog.DialogCode.Accepted
        page.plot.set_markers([dialog.marker])
        seen = []
        page.plot.edit_marker.connect(seen.append)

        # Scene click dispatch must consume a marker double click before ViewBox auto-range.
        class Click:
            def button(self):
                return Qt.MouseButton.LeftButton

            def double(self):
                return True

            def accept(self):
                pass

        page.plot.edit_marker.disconnect(page.edit_marker)
        page.plot.overlays[1][0].mouseClickEvent(Click())
        page.plot.overlays[1][1].mouseClickEvent(Click())
        assert seen == [1, 1]
    finally:
        gate.set()
