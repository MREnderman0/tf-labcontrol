from dataclasses import asdict, replace

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QMessageBox

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ApplyResult, Capability, DeviceCommand, DeviceError
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


def test_recording_locks_parameters_and_auto_but_monitoring_keeps_them_editable(spectrum, qtbot):
    window, page, runtime, key, adapter = spectrum
    field = edit(qtbot, page, "reference_level", "-17")
    writes = []
    original = adapter.apply
    adapter.apply = lambda cmd: writes.append(cmd) or original(cmd)
    page.toggle_monitoring()
    qtbot.waitUntil(lambda: runtime.monitoring.latest(key, "spectrum") is not None)
    window.controller.poll()
    assert field.input.isEnabled() and page.controls.auto_button.isEnabled()
    page.record()
    window.controller.poll()
    assert not field.input.isEnabled()
    assert page.marker_list.isEnabled()  # Software-only analysis does not change instrument parameters.
    assert not page.controls.auto_button.isEnabled() and not page.controls.apply_button.isEnabled()
    assert "参数已锁定" in page.controls.health.text()
    page.controls.auto_button.click()
    assert writes == [] and field.input.text() == "-17"
    page.record()
    qtbot.waitUntil(lambda: not runtime.sessions.snapshots()[0]["controlsLocked"])
    window.controller.poll()
    assert runtime.monitoring.snapshots()[key]["running"]
    assert field.input.isEnabled() and page.controls.auto_button.isEnabled()
    assert field.input.text() == "-17"
    runtime.monitoring.stop(key).result(3)


def test_save_displayed_frame_without_reading_and_preserve_draft(spectrum, qtbot, monkeypatch):
    _, page, runtime, key, adapter = spectrum
    adapter.supports_live_acquisition = True
    original = adapter.acquire
    requested, saved = [], []

    def acquire(request):
        requested.append(request)
        return replace(original(request), validation=request.consistency)

    monkeypatch.setattr(adapter, "acquire", acquire)
    monkeypatch.setattr(runtime.repository, "export", lambda batch, mode: saved.append(batch) or "mock-path")
    page.toggle_monitoring()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    runtime.monitoring.stop(key).result(3)
    field = edit(qtbot, page, "reference_level", "-17")
    field.input.setFocus()
    page.refresh()
    assert page.save_action.text() == "保存当前帧"
    assert page.timing.actual.toolTip() == ""
    displayed = page.plot.displayed_frame
    # A newer frame can already exist in the service while Qt still shows this one.
    task = runtime.monitoring.tasks[key]
    frame_key = next(iter(task.frames))
    task.frames[frame_key] = replace(displayed, batch=replace(displayed.batch, timestamp="later"))
    before = len(requested)
    page.save_action.trigger()
    qtbot.waitUntil(lambda: bool(saved))
    assert saved[0] is displayed.batch and saved[0].validation == "live"
    assert len(requested) == before
    assert field.input.text() == "-17" and field.input.hasFocus()
    runtime.monitoring.stop(key).result(3)


def test_save_old_display_after_parameter_change_and_single_row(spectrum, qtbot, monkeypatch):
    _, page, runtime, key, _ = spectrum
    from PySide6.QtWidgets import QDoubleSpinBox

    assert len(page.timing.findChildren(QDoubleSpinBox)) == 1
    assert not page.save_action.isEnabled()
    page.toggle_monitoring()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    runtime.monitoring.stop(key).result(3)
    qtbot.wait(50)
    displayed = page.plot.displayed_frame
    runtime.sessions.submit(key, "apply", DeviceCommand("center_frequency", {"value": 2e6})).result(3)
    page.refresh()
    assert runtime.monitoring.latest(key, "spectrum") is None
    assert page.save_action.isEnabled()
    saved = []
    monkeypatch.setattr(runtime.repository, "export", lambda batch, mode: saved.append(batch) or "mock-path")
    page.save_action.trigger()
    qtbot.waitUntil(lambda: bool(saved))
    assert saved[0] is displayed.batch
    assert (
        saved[0].configuration["_session_configuration_version"]
        < runtime.sessions.snapshots()[0]["configurationVersion"]
    )
    controls = [page.timing.cadence, page.interval, page.timing.actual]
    page.timing_action.setChecked(True)
    qtbot.waitUntil(page.timing.isVisible)
    qtbot.wait(30)
    assert (
        max(w.geometry().center().y() for w in controls) - min(w.geometry().center().y() for w in controls)
        <= 2
    )


def test_auto_settling_is_neutral_and_preserves_draft_and_focus(spectrum, qtbot, monkeypatch):
    import time

    _, page, runtime, key, adapter = spectrum
    calls = []

    def changing(_request):
        calls.append(1)
        raise DeviceError("Auto is adjusting frequency", "configuration_changed")

    monkeypatch.setattr(adapter, "acquire", changing)
    field = edit(qtbot, page, "reference_level", "-17")
    field.input.setFocus()
    page.toggle_monitoring()
    qtbot.waitUntil(lambda: len(calls) >= 4, timeout=5000)
    page.refresh()
    assert "等待稳定" in page.status.text()
    assert page.connection.text() == "已连接"
    assert not runtime.monitoring.snapshots()[key]["error"]
    assert field.input.text() == "-17" and field.input.hasFocus()
    with runtime.monitoring.lock:
        runtime.monitoring.tasks[key].configuration_wait_deadline = time.monotonic() - 1
    qtbot.waitUntil(lambda: not runtime.monitoring.snapshots()[key]["running"])
    page.refresh()
    assert "30 秒" in page.status.text() and page.connection.text() == "已连接"
    assert not runtime.monitoring.snapshots()[key]["error"]
    assert field.input.text() == "-17" and field.input.hasFocus()
    assert not (runtime.repository.root / "data").exists()


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


def test_manual_refresh_during_monitoring_preserves_draft_and_does_not_write(spectrum, qtbot, monkeypatch):
    _, page, runtime, key, adapter = spectrum
    field = edit(qtbot, page, "reference_level", "-17")
    writes = []
    monkeypatch.setattr(adapter, "apply", writes.append)
    page.toggle_monitoring()
    qtbot.waitUntil(lambda: runtime.monitoring.latest(key, "spectrum") is not None)
    qtbot.mouseClick(page.controls.refresh_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "参数已刷新" in page.controls.status.text())
    assert field.input.text() == "-17" and page.controls.editors["reference_level"].dirty
    assert runtime.monitoring.snapshots()[key]["running"] and not writes


def test_timing_controls_are_local_persistent_and_separate_per_device(spectrum, qtbot, monkeypatch):
    window, page, runtime, key, adapter = spectrum
    writes = []
    monkeypatch.setattr(adapter, "apply", writes.append)
    assert page.timing.cadence.currentData() == "fastest" and not page.interval.isEnabled()
    page.timing.cadence.setCurrentIndex(page.timing.cadence.findData("interval"))
    page.interval.setValue(0.75)
    page.interval.editingFinished.emit()
    assert not writes and key not in runtime.monitoring.tasks
    assert runtime.repository.load_workspace(page.timing.key) == {
        "interval": 0.75,
        "record_interval": None,
        "cadence": "interval",
        "csv_interval": 5,
    }
    second = runtime.add(ConnectionConfig("mock://timing-second", "rs.fph", "mock"))
    runtime.connect(second).result()
    qtbot.waitUntil(lambda: second in window.controller.records)
    page.selector.select(second)
    assert page.interval.value() == 0.5
    page.selector.select(key)
    assert page.interval.value() == 0.75
    window.controller.set_mode("optical")
    qtbot.waitUntil(lambda: page.mode == "optical")
    assert page.interval.value() == 0.5
    page.interval.setValue(3)
    page.interval.editingFinished.emit()
    window.controller.set_mode("electronic")
    qtbot.waitUntil(lambda: page.mode == "electronic")
    assert page.interval.value() == 0.75
    page.toggle_monitoring()
    qtbot.waitUntil(lambda: runtime.monitoring.snapshots()[key]["frame_interval"] is not None)
    qtbot.waitUntil(lambda: "—" not in page.timing.actual.text())
    assert "实际" in page.timing.actual.text() and "—" not in page.timing.actual.text()
    page.interval.setValue(0.5)
    page.interval.editingFinished.emit()
    task = runtime.monitoring.snapshots()[key]
    assert task["interval"] == 0.5 and task["record_interval"] is None
    assert task["running"] and not task["recording"] and not writes
    assert not (runtime.repository.root / "data").exists()
    window.dashboard.add(key, page.source.key, (1,), "Timing fixture")
    # Overview start must not reset either an active cadence or a saved preference.
    window.dashboard.start()
    assert runtime.monitoring.snapshots()[key]["interval"] == 0.5
    assert runtime.monitoring.snapshots()[key]["record_interval"] is None
    runtime.monitoring.stop(key).result(3)
    window.dashboard.start()
    assert runtime.monitoring.snapshots()[key]["interval"] == 0.5
    assert runtime.monitoring.snapshots()[key]["record_interval"] is None


def test_spectrum_pool_has_only_one_monitor_entry_and_other_categories_keep_control(spectrum, qtbot):
    window, _, runtime, key, _ = spectrum
    window.navigate("设备管理")
    window.open_category("spectrum_analyzer")
    window.tree.setCurrentItem(window.rows[key])
    window.refresh()
    assert window.action_buttons["manage"].isHidden()
    assert window.action_buttons["monitor"].text() == "实时监测"
    assert window.action_buttons["monitor"].isVisible()
    second = runtime.add(ConnectionConfig("mock://pool-source", "rs.sma100b", "mock"))
    runtime.connect(second).result()
    window.refresh()
    window.open_category("signal_generator")
    window.tree.setCurrentItem(window.rows[second])
    window.refresh()
    assert window.action_buttons["manage"].isVisible()
    assert window.action_buttons["manage"].text() == "设备控制"


def test_legacy_timing_migrates_and_fastest_retains_custom_value(spectrum):
    window, page, runtime, key, _ = spectrum
    from lab_control.ui.pages.monitoring.timing import monitoring_options

    runtime.repository.save_workspace(
        page.timing.key, {"interval": 0.1, "record_interval": 2, "csv_interval": 30}
    )
    options = monitoring_options(window.controller, key, "electronic")
    assert options == {"interval": 0.5, "record_interval": None, "cadence": "interval", "csv_interval": 5}
    page.timing.bind("", "electronic")
    page.timing.bind(key, "electronic")
    page.interval.setValue(1.25)
    page.interval.editingFinished.emit()
    page.timing.cadence.setCurrentIndex(page.timing.cadence.findData("fastest"))
    assert not page.interval.isEnabled() and page.interval.value() == 1.25
    assert runtime.repository.load_workspace(page.timing.key)["cadence"] == "fastest"
    page.timing.cadence.setCurrentIndex(page.timing.cadence.findData("interval"))
    assert page.interval.isEnabled() and page.interval.value() == 1.25
    assert key not in runtime.monitoring.tasks


def test_frame_notification_draws_without_general_poll_and_hidden_page_skips(spectrum, qtbot, monkeypatch):
    window, page, runtime, key, _ = spectrum
    window.controller.timer.stop()
    window.timer.stop()
    notifications, draws = [], []
    window.controller.refreshed.connect(lambda: notifications.append(1))
    draw = page.plot.show_frame

    def record(*args):
        draws.append(args[0].sequence)
        return draw(*args)

    monkeypatch.setattr(page.plot, "show_frame", record)
    field = page.controls.editors["reference_level"].editors["value"].input
    field.setText("-17")
    field.setFocus()
    page.toggle_monitoring()
    qtbot.waitUntil(lambda: len(draws) >= 2)
    assert not notifications and field.text() == "-17" and field.hasFocus()
    page.hide()
    count = len(draws)
    seq = runtime.monitoring.snapshots()[key]["sequence"]
    qtbot.waitUntil(lambda: runtime.monitoring.snapshots()[key]["sequence"] > seq + 2)
    assert len(draws) == count
    runtime.monitoring.stop(key).result(3)
