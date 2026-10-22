from copy import deepcopy
from threading import Event

import pytest

from lab_control.app import load_stylesheet
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceStatus
from lab_control.instruments.devices.coremorrow_xmt import DEFINITION
from lab_control.instruments.devices.coremorrow_xmt.adapter import CoreMorrowXmtAdapter
from lab_control.instruments.devices.coremorrow_xmt.tests.fakes import WirePeer
from lab_control.ui.pages.device_control.coremorrow_waveform import resolve_mode
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def wave_ui(qapp, qtbot, tmp_path):
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    peer = WirePeer()
    runtime.sessions.factory = lambda config: CoreMorrowXmtAdapter(peer, DEFINITION.profile, config)
    key = runtime.add(ConnectionConfig("mock://e00-wave", "coremorrow.xmt", "mock"))
    runtime.connect(key).result(5)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("设备管理", "power_supply", key)
    page = window.stack.currentWidget()
    qtbot.waitUntil(lambda: not page.initializing and len(page.wave_panels) == 3, timeout=5000)
    try:
        yield page, peer, runtime, key
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def prepare(page, qtbot):
    page.normal_button.click()
    qtbot.waitUntil(lambda: not page.command_pending, timeout=5000)
    assert page.control_mode == "waveform"
    assert page.mode_selector.currentData() == "waveform"


def test_open_is_read_only_and_unknown_pauses_polling(wave_ui):
    page, peer, runtime, key = wave_ui
    assert all(p[3] == 80 for p in peer.history)
    assert page.control_mode == "normal"
    assert "未确认" in page.mode_status.text()
    assert runtime.monitoring.settings_sync.wanted(key) == {}
    assert not page.wave_panels[1].send_button.isEnabled()
    assert page.wave_panels[1].stop_button.isEnabled()


def test_wave_send_stop_and_normal_restore(wave_ui, qtbot):
    page, peer, runtime, key = wave_ui
    prepare(page, qtbot)
    peer.history.clear()
    panel = page.wave_panels[1]
    panel.send_button.click()
    qtbot.waitUntil(lambda: not page.command_pending, timeout=5000)
    assert sum(p[3] == 15 for p in peer.history) == 1
    assert not panel.send_button.isEnabled()
    assert "命令已发送" in panel.status.text()
    assert runtime.monitoring.settings_sync.wanted(key) == {}
    assert page.control_mode == "waveform"
    page.normal_button.click()
    qtbot.waitUntil(lambda: not page.command_pending, timeout=5000)
    assert page.mode_selector.currentData() == "waveform"
    assert page.control_mode == "waveform"
    assert "返回标准波形模式" in page.mode_status.text()
    assert panel.send_button.isEnabled()
    assert sum(p[3] == 0 for p in peer.history) == 3
    assert len(peer.waves) == 0


def test_normal_voltage_edit_enabled_after_explicit_stop(wave_ui, qtbot):
    page, peer, _, _ = wave_ui
    prepare(page, qtbot)
    page.mode_selector.setCurrentIndex(0)
    panel = page.panels[1]
    field = panel.editor.editors["value"]
    assert field.isEnabled()
    field.restore({"value": "0.3", "factor": 1})
    panel.editor.save_draft()
    panel.refresh()
    peer.history.clear()
    assert panel.apply_button.isEnabled()
    panel.apply_button.click()
    qtbot.waitUntil(lambda: not panel.running, timeout=5000)
    assert sum(frame[3] == 0 for frame in peer.history) == 1
    assert field.input.text() == "0.3"


@pytest.mark.parametrize(
    "field,text",
    [("frequency", ""), ("frequency", "nan"), ("frequency", "0"), ("amplitude", "-1"), ("offset", "120")],
)
def test_invalid_input_never_writes(wave_ui, qtbot, field, text):
    page, peer, _, _ = wave_ui
    prepare(page, qtbot)
    peer.history.clear()
    panel = page.wave_panels[2]
    panel.inputs[field].setText(text)
    panel.send_button.click()
    assert not any(p[3] in (0, 15, 17) for p in peer.history)
    assert "未发送" in panel.status.text()


def test_saved_mode_and_parameters_return_without_output(wave_ui, qtbot):
    page, peer, _, _ = wave_ui
    page.mode_selector.setCurrentIndex(1)
    panel = page.wave_panels[2]
    panel.inputs["frequency"].setText("2")
    panel.waveform.setCurrentIndex(2)
    page.select_device("")
    page.select_device(page.selector.device_id)
    qtbot.waitUntil(lambda: not page.initializing, timeout=5000)
    assert page.control_mode == "waveform"
    assert page.wave_panels[2].inputs["frequency"].text() == "2"
    assert page.wave_panels[2].waveform.currentData() == "TRIANGLE"
    assert all(p[3] == 80 for p in peer.history)


def test_verified_state_wins_and_partial_parameters_keep_drafts(wave_ui, qtbot, monkeypatch):
    page, peer, runtime, key = wave_ui
    panel = page.wave_panels[1]
    panel.inputs["offset"].setText("0.3")
    adapter = runtime.sessions.sessions[key].adapter
    state = {
        "source": "instrument",
        "mode": "waveform",
        "channels": {
            "1": {
                "source": "instrument",
                "mode": "waveform",
                "running": True,
                "parameters": {"frequency": 2.0},
            }
        },
    }
    monkeypatch.setattr(adapter, "read_status", lambda: DeviceStatus({"control_state": deepcopy(state)}))
    page.request_state()
    qtbot.waitUntil(lambda: not page.initializing, timeout=5000)
    assert page.control_mode == "waveform"
    assert panel.inputs["frequency"].text() == "2.0"
    assert panel.inputs["offset"].text() == "0.3"
    assert "仪器读回" in panel.status.text()
    assert "仪器读回" in panel.inputs["frequency"].toolTip()
    assert "用户草稿" in panel.inputs["offset"].toolTip()
    assert all(p[3] == 80 for p in peer.history)


def test_completed_command_replaces_snapshot_seen_while_pending(wave_ui, qtbot, monkeypatch):
    page, peer, _, _ = wave_ui
    entered, release = Event(), Event()
    original = peer.write_frame

    def slow_write(frame):
        entered.set()
        assert release.wait(5)
        return original(frame)

    monkeypatch.setattr(peer, "write_frame", slow_write)
    page.normal_button.click()
    try:
        qtbot.waitUntil(entered.is_set)
        page.controller.poll()
        assert page.command_pending
        assert not page.wave_panels[1].send_button.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.command_pending, timeout=5000)
    assert not page.control_state["polling_paused"]
    assert page.wave_panels[1].send_button.isEnabled()


def test_late_state_query_does_not_replace_new_user_draft(wave_ui, qtbot, monkeypatch):
    page, _, runtime, key = wave_ui
    entered, release = Event(), Event()

    def late_read():
        entered.set()
        assert release.wait(5)
        return DeviceStatus(
            {
                "control_state": {
                    "channels": {
                        "1": {"source": "instrument", "mode": "waveform", "parameters": {"frequency": 2}}
                    }
                }
            }
        )

    monkeypatch.setattr(runtime.sessions.sessions[key].adapter, "read_status", late_read)
    page.request_state()
    try:
        qtbot.waitUntil(entered.is_set)
        page.wave_panels[1].inputs["frequency"].setText("7")
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.initializing, timeout=5000)
    assert page.wave_panels[1].inputs["frequency"].text() == "7"
    assert "用户草稿" in page.wave_panels[1].inputs["frequency"].toolTip()


def test_runtime_restart_preserves_device_drafts_without_output(qapp, qtbot, tmp_path):
    qapp.setStyleSheet(load_stylesheet())
    device_ids = []
    for attempt in range(2):
        runtime = ApplicationRuntime(root=tmp_path, hardware=False)
        peers = {f"mock://saved-{i}": WirePeer() for i in (1, 2)}
        runtime.sessions.factory = lambda config: CoreMorrowXmtAdapter(
            peers[config.resource], DEFINITION.profile, config
        )
        if attempt == 0:
            device_ids = [
                runtime.add(ConnectionConfig(address, "coremorrow.xmt", "mock")) for address in peers
            ]
        for key in device_ids:
            runtime.connect(key).result(5)
        window = RealWindow(runtime)
        qtbot.addWidget(window)
        window.show()
        try:
            for index, key in enumerate(device_ids):
                window.navigate("设备管理", "power_supply", key)
                page = window.stack.currentWidget()
                qtbot.waitUntil(lambda: not page.initializing and len(page.wave_panels) == 3, timeout=5000)
                normal = page.panels[1]
                wave = page.wave_panels[1]
                if attempt == 0:
                    normal.editor.editors["value"].restore({"value": str(2 + index), "factor": 1})
                    normal.editor.save_draft()
                    normal.direction.setCurrentIndex(index)
                    normal.step_value.setValue(0.1 + index)
                    page.mode_selector.setCurrentIndex(1)
                    wave.inputs["frequency"].setText(str(5 + index))
                else:
                    assert page.control_mode == "waveform"
                    assert wave.inputs["frequency"].text() == str(5 + index)
                    assert "上次记录" in wave.inputs["frequency"].toolTip()
                    assert normal.editor.editors["value"].input.text() == str(2 + index)
                    assert normal.direction.currentIndex() == index
                    assert normal.step_value.value() == pytest.approx(0.1 + index)
            assert all(frame[3] == 80 for peer in peers.values() for frame in peer.history)
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.bridge.worker.shutdown()
            window.close()


def test_three_wave_rows_fit_without_horizontal_scroll(wave_ui, qtbot):
    page, _, _, _ = wave_ui
    page.mode_selector.setCurrentIndex(1)
    qtbot.wait(40)
    panels = list(page.wave_panels.values())
    assert panels[0].y() < panels[1].y() < panels[2].y()
    assert all(panel.isVisible() for panel in panels)
    assert all(not panel.isVisible() for panel in page.panels.values())
    assert page.scroll_area.horizontalScrollBar().maximum() == 0, (
        page.width(),
        page.scroll_area.viewport().width(),
        page.body.minimumSizeHint().width(),
        [
            (w.objectName(), w.minimumSizeHint().width(), w.width())
            for w in (
                panels[0],
                panels[0].waveform,
                *panels[0].inputs.values(),
                panels[0].send_button,
                panels[0].stop_button,
                panels[0].status,
                panels[0].envelope,
            )
        ],
    )


def test_mode_resolution_never_promotes_command_records_to_instrument_state():
    assert resolve_mode({}, {}) == "normal"
    assert resolve_mode({}, {"mode": "waveform"}) == "waveform"
    assert resolve_mode({"source": "instrument", "mode": "normal"}, {"mode": "waveform"}) == "normal"
    assert resolve_mode({"source": "command_record", "mode": "normal"}, {"mode": "waveform"}) == "waveform"
    assert (
        resolve_mode(
            {"last_mode": "normal", "last_mode_at": "2026-10-16"},
            {"mode": "waveform", "updated_at": "2026-10-15"},
        )
        == "waveform"
    )
    assert (
        resolve_mode({"channels": {"1": {"source": "instrument", "mode": "normal"}}}, {"mode": "waveform"})
        == "waveform"
    )
    assert (
        resolve_mode(
            {
                "channels": {
                    "1": {"source": "instrument", "mode": "normal"},
                    "2": {"source": "instrument", "mode": "waveform"},
                }
            },
            {},
        )
        == "waveform"
    )
