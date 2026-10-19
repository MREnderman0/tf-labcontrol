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
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Measurement,
    MeasurementBatch,
)
from lab_control.instruments.registry import PROFILES
from lab_control.ui.controller import display_connection_state, display_task
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
        window.activateWindow()
        qtbot.waitUntil(window.isActiveWindow)
        yield window, page, runtime, key, adapter
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_float_output_status_live_values_units_and_no_manual_reads(synced, qtbot, monkeypatch):
    window, page, runtime, key, adapter = synced
    assert page.outputs[1].state.text() == "输出关闭 · 正常"
    assert page.outputs[2].state.text() == "输出开启 · 正常"
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


def test_background_read_keeps_status_selection_focus_and_draft_stable(synced, qtbot, monkeypatch):
    window, page, runtime, key, adapter = synced
    runtime.monitoring.enable_settings_sync(False)
    session = runtime.sessions.sessions[key]
    qtbot.waitUntil(lambda: not session.pending)
    window.controller.poll()
    window.refresh()
    window.tree.setCurrentItem(window.rows[key])
    window.filter.setCurrentIndex(window.filter.findData("connected"))
    window.refresh()
    editor = page.panels[1].editors["amplitude"]
    field = editor.editors["value"]
    field.input.setFocus()
    field.input.selectAll()
    qtbot.keyClicks(field.input, "2.5")
    assert field.input.hasFocus()
    signature = page.selector.signature
    label = page.selector.combo.currentText()
    status = window.rows[key].text(3)
    tooltip = window.rows[key].toolTip(3)
    saved = runtime.repository.load_workspace(editor.key)
    selected, replacements, messages, events = [], [], [], []
    page.selector.selected.connect(selected.append)
    window.controller.message.connect(messages.append)
    runtime.sessions.subscribe(events.append)
    original_replace = page.selector.combo.replace_items
    monkeypatch.setattr(
        page.selector.combo,
        "replace_items",
        lambda *args, **kwargs: (replacements.append(args), original_replace(*args, **kwargs))[1],
    )
    original = adapter.read_settings
    started, release = threading.Event(), threading.Event()

    def paused(names, channel):
        started.set()
        assert release.wait(8)
        return original(names, channel)

    monkeypatch.setattr(adapter, "read_settings", paused)

    def stable():
        window.controller.poll()
        window.refresh()
        assert page.connection_label.text() == "已连接"
        assert page.selector.combo.currentText() == label
        assert page.selector.signature == signature and page.device_id == key
        assert window.rows[key].text(3) == status
        assert window.rows[key].toolTip(3) == tooltip
        assert window.details.values["connection"].text() == "已连接"
        assert window.details.values["task"].text() == "空闲"
        assert not window.rows[key].isHidden() and window.selected_id == key
        assert field.input.hasFocus() and field.input.text() == "2.5"
        assert editor.dirty and runtime.repository.load_workspace(editor.key) == saved

    for _ in range(3):
        started.clear()
        release.clear()
        stable()
        future = runtime.sessions.try_submit_idle(key, "read_settings", (("amplitude",), 1), quiet=True)
        assert future is not None
        try:
            qtbot.waitUntil(started.is_set)
            state = runtime.sessions.snapshots()[0]
            assert state["backgroundRead"] and state["connectionState"] == "busy" and state["pending"] == 1
            assert runtime.sessions.leases.owner(session.config.resource) == key
            with pytest.raises(DeviceError, match="Stop and await"):
                runtime.sessions.disconnect(key)
            for _ in range(5):
                stable()
                qtbot.wait(20)
            # The UI reserves teardown and drains this read; direct disconnect
            # without drain_reads above must still reject the occupied session.
            assert window.action_buttons["disconnect"].isEnabled()
            assert not window.action_buttons["disconnect"].toolTip()
            # The busy filter must not include a device just for an automatic read.
            window.filter.setCurrentIndex(window.filter.findData("busy"))
            assert window.rows[key].isHidden()
            window.filter.setCurrentIndex(window.filter.findData("connected"))
            window.tree.setCurrentItem(window.rows[key])
        finally:
            release.set()
        future.result(timeout=3)
        assert not runtime.sessions.snapshots()[0]["backgroundRead"]
        stable()
    assert not selected and not replacements and not messages and not events


@pytest.mark.parametrize("operation", ["read_settings", "apply", "continuous_acquisition"])
def test_foreground_work_keeps_progress_visible(synced, qtbot, monkeypatch, operation):
    window, page, runtime, key, adapter = synced
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not runtime.sessions.sessions[key].pending)
    started, release = threading.Event(), threading.Event()
    method = "acquire" if operation == "continuous_acquisition" else operation
    original = getattr(adapter, method)

    def paused(*args):
        started.set()
        assert release.wait(8)
        if operation == "continuous_acquisition":
            return MeasurementBatch(adapter.identify(), (Measurement("voltage", "V", [0]),), {})
        return original(*args)

    monkeypatch.setattr(adapter, method, paused)
    if operation == "continuous_acquisition":
        future = runtime.sessions.start_acquisition(key, AcquisitionRequest("waveform", (1,)), count=1)
    else:
        argument = (
            (("amplitude",), 1)
            if operation == "read_settings"
            else DeviceCommand("amplitude", {"value": 2}, 1)
        )
        future = runtime.sessions.submit(key, operation, argument)
    try:
        qtbot.waitUntil(started.is_set)
        window.controller.poll()
        window.refresh()
        window.tree.setCurrentItem(window.rows[key])
        window.refresh_details()
        assert "忙碌" in page.connection_label.text()
        assert "任务运行中" in page.selector.combo.currentText()
        assert "忙碌" in window.rows[key].text(3)
        assert window.details.values["task"].text() == operation
        assert not runtime.sessions.snapshots()[0]["backgroundRead"]
    finally:
        release.set()
    future.result(timeout=3)
    window.controller.poll()
    assert page.connection_label.text() == "已连接"


@pytest.mark.parametrize("failure", ["timeout", "invalid"])
def test_background_failure_clears_marker_and_keeps_error_visible(synced, qtbot, monkeypatch, failure):
    window, page, runtime, key, adapter = synced
    runtime.monitoring.enable_settings_sync(False)
    session = runtime.sessions.sessions[key]
    qtbot.waitUntil(lambda: not session.pending)
    original = adapter.read_settings
    started, release = threading.Event(), threading.Event()

    def paused(names, channel):
        started.set()
        assert release.wait(8)
        if failure == "timeout":
            raise DeviceError("Background read timeout fixture", "timeout")
        return tuple(replace(reading, value="invalid") for reading in original(names, channel))

    monkeypatch.setattr(adapter, "read_settings", paused)
    runtime.monitoring.refresh_settings(key)
    runtime.monitoring.enable_settings_sync()
    try:
        qtbot.waitUntil(started.is_set)
        assert runtime.sessions.snapshots()[0]["backgroundRead"]
        window.controller.poll()
        assert page.connection_label.text() == "已连接"
    finally:
        release.set()
    qtbot.waitUntil(lambda: any("同步异常" in output.state.text() for output in page.outputs.values()))
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not session.pending)
    window.controller.poll()
    assert not runtime.sessions.snapshots()[0]["backgroundRead"]
    if failure == "timeout":
        assert page.connection_label.text() == "连接异常"
        assert "需要检查" in page.selector.combo.currentText()
    runtime.sessions.disconnect(key)
    qtbot.waitUntil(lambda: not page.device_id)
    assert page.connection_label.text() == "未选择"


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"connectionState": "degraded"}, "degraded"),
        ({"connectionState": "error"}, "error"),
        ({"connectionState": "disconnected"}, "disconnected"),
        ({"connectionState": "connecting"}, "connecting"),
        ({"monitoring": True}, "busy"),
        ({"recording": True}, "busy"),
        ({"controlPending": 1}, "busy"),
        ({"controlGroup": True}, "busy"),
        ({"pending": 2}, "busy"),
    ],
)
def test_background_marker_never_hides_errors_or_other_work(override, expected):
    state = {"connectionState": "busy", "activeTask": "read_settings", "backgroundRead": True, "pending": 1}
    state.update(override)
    assert display_connection_state(state) == expected
    assert display_task(state) == "read_settings"


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
    assert field.input.hasFocus()
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
    window, page, runtime, key, _ = synced
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
    # Electronics creates no hidden advanced editors/subscriptions.
    wanted = runtime.monitoring.settings_sync.wanted(key)
    assert all("sweep" not in name and "arb" not in name for _, name in wanted)
    assert (1, "phase") in wanted and (2, "phase") in wanted
    assert (1, "reference_clock") not in wanted
    assert page.tabs.count() == 0
    window.controller.set_mode("optical")
    window.navigate("设备管理", "signal_generator", key)
    page = window.stack.currentWidget()
    assert page.tabs.count() == 0
    qtbot.wait(100)
    wanted = runtime.monitoring.settings_sync.wanted(key)
    assert all("sweep" not in name and "arb" not in name for _, name in wanted)
    assert window.controller.mode == "general"



def test_fast_navigation_and_reconnect_use_new_session_readback(synced, qtbot):
    window, page, runtime, key, _ = synced
    previous = runtime.sessions.sessions[key].session_id
    for _ in range(5):
        window.navigate("设备管理")
        window.navigate("设备管理", "signal_generator", key)
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not runtime.sessions.sessions[key].pending)
    runtime.sessions.disconnect(key)
    qtbot.waitUntil(lambda: not page.device_id)
    assert not page.outputs and not page.panels
    assert window.controller.sync_device == ""
    # The last disconnected device removes its category tab. Complete that
    # navigation before reconnecting instead of racing the window refresh timer.
    window.refresh()
    runtime.connect(key).result()
    session = runtime.sessions.sessions[key]
    session.adapter.values["1.output"] = {"value": 1.0}
    session.adapter.values["1.amplitude"] = {"value": 4}
    runtime.monitoring.enable_settings_sync()
    qtbot.waitUntil(lambda: page.selector.combo.findData(key) >= 0)
    assert page.selector.device_id == ""  # Reconnecting never silently restores a target.
    window.navigate("设备管理", "signal_generator")
    assert window.stack.currentWidget() is page
    page.selector.select(key)
    qtbot.waitUntil(lambda: page.outputs[1].state.text() == "输出开启 · 正常")
    assert session.session_id != previous
    assert page.outputs[1].sync_token[0] == session.session_id
    assert page.panels[1].editors["amplitude"].editors["value"].input.text() == "4"
