"""Electronics controls use real widgets/session workers with isolated Mock devices."""

import math
from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ApplyResult, DeviceError
from lab_control.instruments.registry import PROFILES
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def source(qapp, qtbot, tmp_path, monkeypatch):
    models = ("siglent.sdg2082x", "tek.afg31252", "rigol.dg5504pro", "rs.sma100b")
    if not set(models) <= PROFILES.keys():
        pytest.skip("Cross-model UI coverage requires these optional model packages")
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    ids, writes = {}, []
    for model in models:
        key = runtime.add(ConnectionConfig(f"mock://electronics/{model}", model, "mock"))
        runtime.connect(key).result()
        ids[model] = key
        adapter = runtime.sessions.sessions[key].adapter
        original = adapter.apply
        monkeypatch.setattr(adapter, "apply", lambda cmd, fn=original: (writes.append(cmd), fn(cmd))[1])
    with runtime.monitoring.lock:
        runtime.monitoring.settings_sync.foreground_interval = 0.1
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.navigate("设备管理", "signal_generator", ids[models[0]])
    page = window.stack.currentWidget()
    qtbot.waitUntil(lambda: all(p.editors["phase"].current_value is not None for p in page.panels.values()))
    try:
        yield window, page, runtime, ids, writes
    finally:
        if page.settings_dialog:
            page.settings_dialog.reject()
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def edit(qtbot, editor, text):
    field = editor.editors["value"]
    field.input.setFocus()
    field.input.selectAll()
    qtbot.keyClicks(field.input, text)
    qtbot.keyClick(field.input, Qt.Key.Key_Tab)
    return field


def open_settings(page, qtbot):
    page.open_settings()
    dialog = page.settings_dialog
    qtbot.waitUntil(lambda: dialog.current["reference_clock"] is not None)
    return dialog


def test_five_parameters_layout_and_rf_capabilities(source, qtbot):
    window, page, _, ids, writes = source
    assert page.tabs.count() == 0 and not page.tabs.isVisible()
    assert set(page.panels[1].editors) == {"waveform", "frequency", "amplitude", "offset", "phase"}
    for size in ((1280, 760), (1440, 900)):
        window.resize(*size)
        qtbot.wait(50)
        assert window.size().toTuple() == size
        assert page.channel_cards[0].y() == page.channel_cards[1].y()
        group = page.panels[1]
        assert group.fields.getItemPosition(0) == (0, 0, 1, 2)
        assert group.fields.getItemPosition(4) == (2, 1, 1, 1)
        assert page.outputs[1].parent().objectName() == "sourceChannelHeader"
        assert page.scroll_area.horizontalScrollBar().maximum() == 0
    from PySide6.QtCore import QRect

    window.fit_to_screen(QRect(0, 0, 1920, 1080))
    window.resize(500, 400)
    assert window.size().toTuple() == (1280, 760)
    window.navigate("设备管理", "signal_generator", ids["rigol.dg5504pro"])
    qtbot.wait(100)
    assert len(page.panels) == 4
    assert page.grid.getItemPosition(2)[:2] == (1, 0)
    assert page.scroll_area.verticalScrollBar().maximum() > 0
    window.navigate("设备管理", "signal_generator", ids["rs.sma100b"])
    assert set(page.panels[1].editors) == {"frequency", "power"}
    assert page.panels[1].editors["power"].editors["value"].unit.currentText() == "dBm"
    assert writes == []


def test_degree_display_radian_submit_and_channel_draft_isolation(source, qtbot):
    window, page, runtime, ids, writes = source
    key = ids["tek.afg31252"]
    adapter = runtime.sessions.sessions[key].adapter
    adapter.values["1.phase"] = {"value": math.pi / 2}
    window.navigate("设备管理", "signal_generator", key)
    phase = page.panels[1].editors["phase"]
    field = phase.editors["value"]
    qtbot.waitUntil(lambda: field.input.text() == "90")
    assert field.unit.currentText() == "°"
    assert field.range_label.text() == "-180 ～ 180 °"
    edit(qtbot, page.panels[2].editors["offset"], "0.25")
    edit(qtbot, phase, "180")
    assert phase.parameters()["value"] == pytest.approx(math.pi)
    page.panels[1].apply()
    qtbot.waitUntil(lambda: not page.panels[1].running)
    assert [(c.operation, c.channel) for c in writes] == [("phase", 1)]
    assert writes[0].parameters["value"] == pytest.approx(math.pi)
    assert page.panels[2].editors["offset"].dirty
    assert not phase.dirty and field.input.text() == "180"


def test_reference_cancel_zero_writes_and_separate_global_apply(source, qtbot):
    _, page, runtime, ids, writes = source
    key = ids["siglent.sdg2082x"]
    main = page.panels[1].editors["amplitude"]
    edit(qtbot, main, "0.2")
    saved = runtime.repository.load_workspace(main.key)
    dialog = open_settings(page, qtbot)
    dialog.choices["reference_clock"]["EXT"].click()
    assert dialog.apply_button.isEnabled()
    dialog.reject()
    assert not writes and main.dirty
    assert runtime.repository.load_workspace(main.key) == saved
    assert (1, "reference_clock") not in runtime.monitoring.settings_sync.wanted(key)
    dialog = open_settings(page, qtbot)
    assert not dialog.drafts and dialog.choices["reference_clock"]["INT"].isChecked()
    dialog.choices["reference_clock"]["EXT"].click()
    dialog.apply_button.click()
    qtbot.waitUntil(lambda: not dialog.busy and dialog.current["reference_clock"] == "EXT")
    assert [(c.operation, c.channel, c.parameters) for c in writes] == [
        ("reference_clock", 1, {"value": "EXT"})
    ]
    assert main.dirty and runtime.repository.load_workspace(main.key) == saved
    assert "外部参考" in dialog.current_labels["reference_clock"].text()
    assert "锁定" not in dialog.status.text()
    assert runtime.sessions.sessions[key].adapter.values["1.output"] == {"value": False}


@pytest.mark.parametrize("outcome", ["exception", "uncertain", "rejected"])
def test_reference_failure_never_replays(source, qtbot, monkeypatch, outcome):
    _, page, runtime, ids, _ = source
    adapter = runtime.sessions.sessions[ids["siglent.sdg2082x"]].adapter
    calls = []

    def fail(cmd):
        calls.append(cmd)
        if outcome == "exception":
            raise DeviceError("timeout fixture", "timeout")
        return ApplyResult(outcome, cmd.operation, cmd.parameters, error="fixture failure")

    monkeypatch.setattr(adapter, "apply", fail)
    dialog = open_settings(page, qtbot)
    dialog.choices["reference_clock"]["EXT"].click()
    dialog.apply_button.click()
    qtbot.waitUntil(lambda: not dialog.busy)
    qtbot.wait(500)
    assert len(calls) == 1
    assert "失败" in dialog.status.text() or "未完成" in dialog.status.text()
    assert dialog.drafts == {"reference_clock": "EXT"}


def test_unknown_reference_missing_capability_and_disconnect(source, qtbot):
    window, page, runtime, ids, writes = source
    key = ids["siglent.sdg2082x"]
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not runtime.sessions.sessions[key].pending)
    page.open_settings()
    dialog = page.settings_dialog
    assert dialog.current["reference_clock"] is None
    assert not any(b.isChecked() for b in dialog.choices["reference_clock"].values())
    dialog.choices["reference_clock"]["EXT"].click()
    assert not dialog.apply_button.isEnabled()
    dialog.reject()
    adapter = runtime.sessions.sessions[key].adapter
    adapter.caps = replace(
        adapter.caps, operations=tuple(c for c in adapter.caps.operations if c.name != "reference_clock")
    )
    runtime.sessions.submit(key, "refresh_capabilities").result()
    window.controller.poll()
    page.open_settings()
    dialog = page.settings_dialog
    assert not any(b.isEnabled() for b in dialog.choices["reference_clock"].values())
    assert "未开放" in dialog.current_labels["reference_clock"].text()
    runtime.sessions.disconnect(key)
    qtbot.waitUntil(lambda: not page.device_id)
    assert page.settings_dialog is None and not writes


def test_device_and_mode_switch_discard_only_dialog_draft(source, qtbot):
    window, page, _, ids, writes = source
    editor = page.panels[1].editors["offset"]
    edit(qtbot, editor, "0.1")
    dialog = open_settings(page, qtbot)
    dialog.choices["reference_clock"]["EXT"].click()
    window.navigate("设备管理", "signal_generator", ids["tek.afg31252"])
    assert page.settings_dialog is None
    window.navigate("设备管理", "signal_generator", ids["siglent.sdg2082x"])
    assert page.panels[1].editors["offset"].dirty
    dialog = open_settings(page, qtbot)
    dialog.choices["reference_clock"]["EXT"].click()
    window.controller.set_mode("optical")
    window.navigate("设备管理", "signal_generator", ids["siglent.sdg2082x"])
    page = window.stack.currentWidget()
    assert page.settings_dialog is None
    assert page.tabs.count() > 0 and not page.settings_button.isVisible()
    assert any("sweep" in p.editors for p in page.advanced_groups)
    assert writes == []
