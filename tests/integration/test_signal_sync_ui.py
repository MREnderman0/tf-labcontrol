import threading
import time
from dataclasses import replace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPushButton

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.registry import PROFILES
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def synced(qapp, qtbot, tmp_path):
    if "tek.afg31252" not in PROFILES:
        pytest.skip("Optional AFG UI fixture")
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://auto-ui", "tek.afg31252", "mock"))
    runtime.connect(key).result()
    adapter = runtime.sessions.sessions[key].adapter
    adapter.values.update({"1.output": {"value": 0.0}, "2.output": {"value": 1.0}})
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    with runtime.monitoring.lock:
        runtime.monitoring.settings_sync.foreground_interval = 0.1
    window.show()
    window.navigate("设备管理", "signal_generator", key)
    page = window.stack.currentWidget()
    try:
        qtbot.waitUntil(lambda: all("正常" in o.state.text() for o in page.outputs.values()), timeout=6000)
        yield window, page, runtime, key, adapter
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_float_output_status_live_values_units_and_no_manual_reads(synced, qtbot, monkeypatch):
    window, page, runtime, key, adapter = synced
    assert page.outputs[1].state.text() == "CH 1 关闭 · 正常"
    assert page.outputs[2].state.text() == "CH 2 开启 · 正常"
    assert not any("读取" in b.text() and not b.isHidden() for b in page.findChildren(QPushButton))
    editor = page.panels[1].editors["frequency"]
    field = editor.editors["value"]
    field.unit.setCurrentIndex(field.unit.findText("MHz"))
    page.selector.combo.setFocus()
    saved, writes = [], []
    original = runtime.repository.save_workspace
    monkeypatch.setattr(
        runtime.repository,
        "save_workspace",
        lambda name, value: (saved.append(name), original(name, value))[1],
    )
    monkeypatch.setattr(adapter, "apply", writes.append)
    adapter.values["1.frequency"] = {"value": 2e6}
    adapter.values["1.waveform"] = {"value": "SQUare"}
    qtbot.waitUntil(lambda: field.input.text() == "2")
    assert field.unit.currentText() == "MHz"
    waveform = page.panels[1].editors["waveform"]
    qtbot.waitUntil(lambda: waveform.editors["value"].input.currentData() == "SQU")
    assert not editor.readback.isVisible() and not waveform.readback.isVisible()
    assert not page.panels[1].apply_button.isEnabled()
    qtbot.wait(350)
    assert not [name for name in saved if name.startswith("draft/")]
    assert not writes
    assert page.last_columns == 2 and window.sidebar.width() == 232


def test_draft_and_focused_input_survive_external_change_then_reconcile(synced, qtbot):
    _, page, runtime, key, adapter = synced
    editor = page.panels[1].editors["amplitude"]
    field = editor.editors["value"]
    field.input.setFocus()
    field.input.selectAll()
    qtbot.keyClicks(field.input, "3")
    page.selector.combo.setFocus()
    adapter.values["1.amplitude"] = {"value": 2}
    qtbot.waitUntil(lambda: editor.current_value == {"value": 2})
    assert field.input.text() == "3" and editor.dirty
    assert "2 Vpp" in editor.readback.text() and editor.readback.isVisible()
    assert runtime.repository.load_workspace(editor.key)["fields"]["value"]["value"] == "3"
    adapter.values["1.amplitude"] = {"value": 3}
    qtbot.waitUntil(lambda: not editor.dirty)
    assert runtime.repository.load_workspace(editor.key) is None
    assert not editor.readback.isVisible()
    field.input.setFocus()
    adapter.values["1.amplitude"] = {"value": 4}
    qtbot.waitUntil(lambda: editor.current_value == {"value": 4})
    assert field.input.text() == "3"
    page.selector.combo.setFocus()
    qtbot.waitUntil(lambda: field.input.text() == "4")


def test_slow_sync_keeps_ui_responsive_and_hidden_pages_do_not_render(synced, qtbot):
    window, page, runtime, key, adapter = synced
    started, release = threading.Event(), threading.Event()
    original = adapter.read_settings

    def slow(names, ch):
        started.set()
        assert release.wait(3)
        return original(names, ch)

    adapter.read_settings = slow
    runtime.monitoring.refresh_settings(key)
    ticks = []
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(20)
    try:
        qtbot.waitUntil(started.is_set)
        field = page.panels[1].editors["amplitude"].editors["value"]
        field.input.setFocus()
        field.input.selectAll()
        qtbot.keyClicks(field.input, "2.5")
        window.navigate("设备管理")
        qtbot.wait(200)
        assert not page.isVisible()
        assert runtime.monitoring.settings_sync.focus == ""
        assert len(ticks) >= 5
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.2
        assert field.input.text() == "2.5"
    finally:
        timer.stop()
        release.set()
    adapter.read_settings = original
    window.navigate("设备管理", "signal_generator", key)
    qtbot.waitUntil(lambda: page.isVisible())
    assert field.input.text() == "2.5"


def test_twenty_connected_sources_keep_event_loop_responsive(synced, qtbot):
    window, page, runtime, key, _ = synced
    for index in range(19):
        other = runtime.add(ConnectionConfig(f"mock://ui-load/{index}", "rigol.dg5504pro", "mock"))
        runtime.connect(other).result()
    ticks = []
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(20)
    try:
        qtbot.wait(1500)
        assert len(ticks) >= 20
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.25
        assert runtime.monitoring.settings_sync.max_observed_concurrent <= 4
        assert page.device_id == key and page.last_columns == 2
    finally:
        timer.stop()


def test_stale_and_invalid_output_cannot_look_normal_or_enable_output(synced, qtbot):
    _, page, runtime, key, adapter = synced
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not runtime.sessions.sessions[key].pending)
    session = runtime.sessions.sessions[key]
    with runtime.sessions.lock:
        for name, frame in session.frames.items():
            session.frames[name] = replace(frame, received_at=time.monotonic() - 4)
    qtbot.waitUntil(lambda: "待同步" in page.outputs[1].state.text())
    assert not page.outputs[1].button.isEnabled()
    adapter.values["1.output"] = {"value": "unexpected"}
    runtime.monitoring.enable_settings_sync()
    qtbot.waitUntil(lambda: "同步异常" in page.outputs[1].state.text())
    assert not page.outputs[1].button.isEnabled()
    assert "未读取" not in page.outputs[1].state.text()


def test_old_configuration_frame_does_not_overwrite_newer_state(synced, qtbot):
    _, page, runtime, key, adapter = synced
    session = runtime.sessions.sessions[key]
    editor = page.panels[1].editors["amplitude"]
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not session.pending)
    with runtime.sessions.lock:
        old = session.frames[(1, "amplitude")]
        session.configuration_version += 1
    editor.editors["value"].set_value(8)
    qtbot.wait(150)
    assert editor.editors["value"].input.text() == "8"
    assert old.configuration_version < session.configuration_version
    adapter.values["1.amplitude"] = {"value": 4}
    runtime.monitoring.enable_settings_sync()
    qtbot.waitUntil(lambda: editor.editors["value"].input.text() == "4")


def test_old_sequence_and_hidden_advanced_settings_are_ignored(synced, qtbot):
    _, page, runtime, key, _ = synced
    runtime.monitoring.enable_settings_sync(False)
    session = runtime.sessions.sessions[key]
    qtbot.waitUntil(lambda: not session.pending)
    page.update_readings(key)
    editor = page.panels[1].editors["amplitude"]
    with runtime.sessions.lock:
        frame = session.frames[(1, "amplitude")]
        session.frames[(1, "amplitude")] = replace(
            frame, sequence=frame.sequence - 1, reading=replace(frame.reading, value={"value": 9})
        )
    page.update_readings(key)
    assert editor.current_value == {"value": 1}
    # Only the selected advanced task contributes to the scheduler scope.
    wanted = runtime.monitoring.settings_sync.wanted(key)
    assert all("sweep" not in name and "arb" not in name for _, name in wanted)
    sweep = next(i for i in range(page.tabs.count()) if page.tabs.tabText(i) == "扫频")
    page.tabs.setCurrentIndex(sweep)
    qtbot.wait(100)
    wanted = runtime.monitoring.settings_sync.wanted(key)
    assert any("sweep" in name for _, name in wanted)
    assert all(name != "load" for _, name in wanted)


def test_fast_navigation_and_reconnect_use_new_session_readback(synced, qtbot):
    window, page, runtime, key, _ = synced
    previous = runtime.sessions.sessions[key].session_id
    for _ in range(5):
        window.navigate("设备管理")
        window.navigate("设备管理", "signal_generator", key)
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not runtime.sessions.sessions[key].pending)
    runtime.sessions.disconnect(key)
    qtbot.waitUntil(lambda: "未连接" in page.outputs[1].state.text())
    runtime.connect(key).result()
    session = runtime.sessions.sessions[key]
    session.adapter.values["1.output"] = {"value": 1.0}
    session.adapter.values["1.amplitude"] = {"value": 4}
    runtime.monitoring.enable_settings_sync()
    qtbot.waitUntil(lambda: page.outputs[1].state.text() == "CH 1 开启 · 正常")
    assert session.session_id != previous
    assert page.outputs[1].sync_token[0] == session.session_id
    assert page.panels[1].editors["amplitude"].editors["value"].input.text() == "4"
