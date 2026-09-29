import threading
from dataclasses import replace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ApplyResult, DeviceCommand, Parameter
from lab_control.ui.pages.monitoring.oscilloscope import OscilloscopeMonitoringPage
from lab_control.ui.pages.monitoring.oscilloscope_controls import ScopeSlider
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def scope(qapp, qtbot, tmp_path):
    if qapp.property("scopeTestTheme") is None:
        qapp.setStyle("Fusion")
        qapp.setFont(QFont(load_ui_font(), 10))
        qapp.setStyleSheet(load_stylesheet())
        qapp.setProperty("scopeTestTheme", True)
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://scope-ui", "lecroy.9404m", "mock"))
    runtime.connect(key).result()
    adapter = runtime.sessions.sessions[key].adapter
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", "oscilloscope", key)
    page = window.stack.currentWidget()
    qtbot.waitUntil(lambda: len(page.controls.values) >= 30 and not page.controls.running, timeout=6000)
    try:
        yield window, page, runtime, key, adapter
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_navigation_keeps_card_and_only_monitoring_route(scope):
    window, page, _, key, _ = scope
    window.navigate("设备管理", "oscilloscope", key)
    assert window.stack.currentWidget() is page and isinstance(page, OscilloscopeMonitoringPage)
    window.navigate("设备管理")
    assert "oscilloscope" not in [window.submenu.tabData(i) for i in range(window.submenu.count())]
    assert "spectrum_analyzer" not in [window.submenu.tabData(i) for i in range(window.submenu.count())]
    assert "oscilloscope" in window.cards
    window.show_device_pool(window.cards["oscilloscope"])
    window.open_details(key)
    assert not window.action_buttons["manage"].isVisible()


def test_fixed_slider_endpoints_and_points_continue_monitoring(scope, qtbot, monkeypatch):
    _, page, runtime, key, adapter = scope
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: writes.append(cmd) or original(cmd))
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    field = page.controls.sliders[1, "offset"]
    low, high = field.low, field.high
    position = field.slider.maximum() * 3 // 5
    field.slider.setSliderDown(True)
    field.slider.setValue(position)
    field.slider.setSliderDown(False)
    qtbot.waitUntil(lambda: len(writes) == 1 and not page.controls.running)
    assert (field.low, field.high) == (low, high)
    assert abs(field.slider.value() - position) <= 1
    assert runtime.monitoring.snapshots()[key]["running"]
    page.controls.points.committed.emit(2500)
    qtbot.waitUntil(lambda: len(writes) == 2 and not page.controls.running)
    assert writes[-1].operation == "record_length" and adapter._value(1, "record_length") == 2500
    assert "1,000" in page.controls.points_range.text() and "软件" in page.controls.points_range.text()
    assert runtime.monitoring.snapshots()[key]["running"]
    page.controls.points.committed.emit(999)
    assert len(writes) == 2 and "未写入" in page.controls.status.text()


def test_point_original_outside_range_is_shown_without_write(scope, qtbot):
    _, page, _, _, adapter = scope
    adapter.values["1.record_length"] = {"value": 64_000_000}
    page.controls.refresh_parameters()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert page.controls.points.currentData() == 64000000
    assert "超出范围" in page.controls.points.currentText()
    assert adapter._value(1, "record_length") == 64_000_000


def test_shrunk_timebase_range_preserves_original_until_endpoint_gesture(qtbot):
    field = ScopeSlider("时基", "s", coarse=True)
    qtbot.addWidget(field)
    values = []
    field.committed.connect(values.append)
    assert field.configure(0.02, Parameter("number", "s/div", 5e-10, 0.001), 0.02)
    assert values == [] and field.actual == 0.02
    assert field.steps[-1] == 0.001 and field.maximum_label.text() == "1 ms"
    # The thumb is already at its new maximum; clicking/releasing must still
    # apply that valid endpoint exactly once, instead of appearing unresponsive.
    field.slider.setSliderDown(True)
    assert values == []
    field.slider.setSliderDown(False)
    assert values == [0.001]


@pytest.mark.parametrize("uncertain", [False, True])
def test_smaller_point_limit_clamps_once_in_control_group_and_preserves_monitor_intent(
    scope, qtbot, monkeypatch, uncertain
):
    window, page, runtime, key, adapter = scope
    adapter.values["1.record_length"] = {"value": 5000}
    page.controls.refresh_parameters()
    qtbot.waitUntil(lambda: not page.controls.running)
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    writes = []
    original = adapter.apply

    def shrink():
        adapter.caps = replace(
            adapter.caps,
            operations=tuple(
                replace(cap, parameters={"value": replace(cap.parameters["value"], maximum=2500)})
                if cap.name == "record_length"
                else cap
                for cap in adapter.caps.operations
            ),
        )

    def apply(command):
        writes.append(command)
        assert runtime.sessions.snapshots()[0]["controlGroup"]
        return (
            ApplyResult("uncertain", command.operation, command.parameters, error="timeout")
            if uncertain
            else original(command)
        )

    monkeypatch.setattr(adapter, "refresh_capabilities", shrink, raising=False)
    monkeypatch.setattr(adapter, "apply", apply)
    page.controls.refresh_parameters()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert [(c.operation, c.parameters) for c in writes] == [("record_length", {"value": 2500})]
    window.controller.poll()
    if uncertain:
        assert not runtime.monitoring.snapshots()[key]["running"]
        assert "timeout" in page.controls.status.text()
    else:
        assert runtime.monitoring.snapshots()[key]["running"]
        assert page.controls.points.currentData() == 2500
        assert adapter._value(1, "record_length") == 2500
    assert not runtime.sessions.snapshots()[0]["controlGroup"]


def test_both_curve_tool_menus_default_closed_and_keep_cadence_local(scope, qtbot, monkeypatch):
    window, page, runtime, _, adapter = scope
    monkeypatch.setattr(adapter, "apply", lambda cmd: pytest.fail("Cadence must not write to instrument"))
    assert not page.timing.isVisible()
    assert page.timing.cadence.currentData() == "interval"
    assert page.timing.interval.value() == 0.5 and page.timing.interval.isEnabled()
    page.timing_action.trigger()
    assert page.timing.isVisible()
    page.timing.cadence.setCurrentIndex(page.timing.cadence.findData("interval"))
    page.timing.interval.setValue(0.7)
    page.timing.commit()
    assert page.timing.interval.minimum() == 0.5 and page.timing.interval.maximum() == 60
    spec_key = runtime.add(ConnectionConfig("mock://spectrum-menu", "rigol.dsa815", "mock"))
    runtime.connect(spec_key).result()
    window.controller.poll()
    window.navigate("实时监测", "spectrum_analyzer", spec_key)
    spectrum = window.stack.currentWidget()
    assert not spectrum.timing.isVisible()
    spectrum.timing_action.trigger()
    assert spectrum.timing.isVisible()
    assert spectrum.timing.interval.minimum() == 0.5


def test_smaller_point_limit_preserves_already_valid_value(scope, qtbot, monkeypatch):
    _, page, _, _, adapter = scope

    def shrink():
        adapter.caps = replace(
            adapter.caps,
            operations=tuple(
                replace(cap, parameters={"value": replace(cap.parameters["value"], maximum=2500)})
                if cap.name == "record_length"
                else cap
                for cap in adapter.caps.operations
            ),
        )

    monkeypatch.setattr(adapter, "refresh_capabilities", shrink, raising=False)
    monkeypatch.setattr(adapter, "apply", lambda _: pytest.fail("In-range points must remain unchanged"))
    page.controls.refresh_parameters()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert page.controls.points.currentData() == 2000


def test_drag_release_and_keyboard_gesture_write_once(scope, qtbot, monkeypatch):
    _, page, _, _, adapter = scope
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: writes.append(cmd) or original(cmd))
    field = page.controls.sliders[1, "scale"]
    slider = field.slider
    slider.setSliderDown(True)
    slider.setValue(slider.value() + 1)
    slider.setValue(slider.value() + 1)
    qtbot.wait(250)
    assert writes == []
    slider.setSliderDown(False)
    qtbot.waitUntil(lambda: len(writes) == 1 and not page.controls.running)
    assert writes[0].operation == "scale" and field.actual == writes[0].parameters["value"]
    qtbot.keyPress(slider, Qt.Key.Key_Left)
    qtbot.wait(250)
    assert len(writes) == 1
    qtbot.keyRelease(slider, Qt.Key.Key_Left)
    qtbot.waitUntil(lambda: len(writes) == 2 and not page.controls.running)


def test_custom_readback_is_not_quantized_on_open(scope, qtbot):
    window, page, runtime, key, adapter = scope
    adapter.values["1.scale"] = {"value": 0.037}
    page.controls.refresh_parameters()
    qtbot.waitUntil(lambda: not page.controls.running)
    window.controller.poll()
    field = page.controls.sliders[1, "scale"]
    assert field.actual == 0.037 and "自定义" in field.value_label.text()
    assert adapter._value(1, "scale") == 0.037
    assert not runtime.monitoring.snapshots().get(key, {}).get("running")


def test_channel_off_replaces_requests_and_all_off_does_not_resume(scope, qtbot, monkeypatch):
    _, page, runtime, key, adapter = scope
    reads = []
    original = adapter.acquire
    monkeypatch.setattr(adapter, "acquire", lambda req: reads.append(req.channels) or original(req))
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    page.controls.checks[2].click()
    qtbot.waitUntil(lambda: not page.controls.running and 2 not in page.controls.channels())
    index = len(reads)
    qtbot.waitUntil(lambda: len(reads) > index + 1)
    assert all(2 not in channels for channels in reads[index:])
    for ch in (1, 3, 4):
        page.controls.checks[ch].click()
        qtbot.waitUntil(lambda: not page.controls.running and ch not in page.controls.channels())
    assert not runtime.monitoring.snapshots()[key]["running"]
    assert page.plot.displayed_frame is None
    page.controls.checks[1].click()
    qtbot.waitUntil(lambda: not page.controls.running and page.controls.channels() == (1,))
    assert not runtime.monitoring.snapshots()[key]["running"]


def test_monitor_edit_and_record_drain_lock(scope, qtbot, monkeypatch):
    window, page, runtime, key, _ = scope
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    assert page.controls.sliders[1, "scale"].isEnabled()
    page.record()
    window.controller.poll()
    assert not page.controls.sliders[1, "scale"].isEnabled()
    assert not page.controls.checks[1].isEnabled() and not page.controls.gear.isEnabled()
    page.record()
    qtbot.waitUntil(lambda: not runtime.sessions.snapshots()[0]["controlsLocked"])
    window.controller.poll()
    assert page.controls.sliders[1, "scale"].isEnabled()
    assert runtime.monitoring.snapshots()[key]["running"]


def test_display_save_no_instrument_read_or_normalized_data(scope, qtbot, monkeypatch):
    _, page, runtime, key, adapter = scope
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    runtime.monitoring.stop(key).result(3)
    old = page.plot.displayed_frame
    saved = []
    monkeypatch.setattr(adapter, "acquire", lambda req: pytest.fail("Export acquired new data"))
    monkeypatch.setattr(
        runtime.repository, "export", lambda batch, mode, **options: saved.append(batch) or "mock-path"
    )
    runtime.sessions.submit(key, "apply", DeviceCommand("scale", {"value": 0.2}, 1)).result()
    page.save()
    qtbot.waitUntil(lambda: bool(saved))
    assert saved[0] is old.batch
    assert saved[0].measurements[0].metadata["scope_display"]["units_per_div"] == 0.5


def test_uncertain_write_stops_without_replay(scope, qtbot, monkeypatch):
    _, page, runtime, key, adapter = scope
    writes = []
    monkeypatch.setattr(
        adapter,
        "apply",
        lambda cmd: (
            writes.append(cmd) or ApplyResult("uncertain", cmd.operation, cmd.parameters, error="timeout")
        ),
    )
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    page.controls.submit([DeviceCommand("offset", {"value": 0.02}, 1)])
    qtbot.waitUntil(lambda: not page.controls.running)
    assert len(writes) == 1 and "timeout" in page.controls.status.text()
    assert not runtime.monitoring.snapshots()[key]["running"]
    assert not runtime.sessions.snapshots()[0]["controlGroup"]


def test_trigger_dialog_cancel_zero_writes_and_apply_only_changed_fields(scope, qtbot, monkeypatch):
    from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

    _, page, _, _, adapter = scope
    writes = []
    original = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: writes.append(cmd) or original(cmd))

    def edit(accept):
        dialog = page.controls.findChild(QDialog)
        source, slope, mode = dialog.findChildren(WorkspaceComboBox)
        source.setCurrentIndex(source.findData("C2"))
        slope.setCurrentIndex(slope.findData("NEG"))
        mode.setCurrentIndex(mode.findData("SINGLE"))
        assert writes == []
        if accept:
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Apply).click()
        else:
            dialog.reject()

    QTimer.singleShot(0, lambda: edit(False))
    page.controls.trigger_dialog()
    qtbot.wait(20)
    assert writes == []
    QTimer.singleShot(0, lambda: edit(True))
    page.controls.trigger_dialog()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert [(c.operation, c.channel, c.parameters["value"]) for c in writes] == [
        ("trigger_source", 1, "C2"),
        ("trigger_slope", 2, "NEG"),
        ("trigger_mode", 1, "SINGLE"),
    ]
    assert "等待新波形" in page.controls.status.text()


def test_reconnect_drops_previous_frames_and_readings(scope, qtbot):
    window, page, runtime, key, _ = scope
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    old_session = page.plot.displayed_frame.session_id
    runtime.monitoring.stop(key).result(3)
    runtime.sessions.disconnect(key, drain_reads=True)
    window.controller.poll()
    assert page.controls is None and page.plot.displayed_frame is None
    runtime.connect(key).result()
    window.controller.poll()
    assert not page.selector.device_id
    page.selector.select(key)
    qtbot.waitUntil(lambda: len(page.controls.values) >= 30)
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    assert page.plot.displayed_frame.session_id != old_session


def test_overview_removes_closed_channel_and_keeps_other_grid_traces(scope, qtbot):
    window, page, _, _, _ = scope
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    page.pin()
    window.navigate("实时监测")
    card = next(iter(window.dashboard.cards.values()))
    qtbot.waitUntil(lambda: card.plot.displayed_frame is not None)
    page.controls.set_channel(1, False)
    qtbot.waitUntil(lambda: not page.controls.running and 1 not in page.controls.channels())
    qtbot.waitUntil(lambda: not card.plot.curves[1, "waveform"].isVisible())
    assert card.plot.curves[2, "waveform"].isVisible() and card.plot.instrument_view


def test_stop_recording_retains_ui_lock_through_blocked_binary_write(scope, qtbot, monkeypatch):
    window, page, runtime, key, _ = scope
    entered, release = threading.Event(), threading.Event()
    factory = runtime.repository.create_recording

    def create(mode, **options):
        recorder = factory(mode, **options)
        append = recorder.segments.append

        def blocked(sequence, batch):
            entered.set()
            assert release.wait(8)
            return append(sequence, batch)

        recorder.segments.append = blocked
        return recorder

    monkeypatch.setattr(runtime.repository, "create_recording", create)
    page.start()
    page.record()
    try:
        qtbot.waitUntil(entered.is_set)
        page.record()
        window.controller.poll()
        assert runtime.sessions.snapshots()[0]["controlsLocked"]
        assert not page.controls.checks[1].isEnabled() and not page.controls.gear.isEnabled()
        assert not page.controls.points.isEnabled()
        assert not page.refresh_action.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not runtime.sessions.snapshots()[0]["controlsLocked"])
    window.controller.poll()
    assert page.controls.checks[1].isEnabled() and runtime.monitoring.snapshots()[key]["running"]
