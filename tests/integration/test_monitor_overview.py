"""The monitoring overview is driven by connected sessions and slot preferences."""

import pytest
from PySide6.QtWidgets import QMessageBox

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.ui.pages.monitoring.timing import save_monitoring_interval
from lab_control.ui.real_window import RealWindow


@pytest.mark.parametrize(
    "adapter,action",
    [
        ("rigol.m300", "overview"),
        ("keysight.3458a", "overview"),
        ("keysight.3458a", "start"),
        ("keysight.3458a", "once"),
        ("ceyear.6362d", "overview"),
    ],
)
def test_sampling_click_starts_selected_device_without_confirmation(
    qtbot, tmp_path, monkeypatch, adapter, action
):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)

    def instrument(config):
        result = SimulatedInstrument(config)
        if config.adapter == "rigol.m300":
            result.capability_details = {
                "acquisition_channel_selection": "scan_list",
                "acquisition_channels": [1, 2],
            }
        return result

    runtime.sessions.factory = instrument
    key = runtime.add(ConnectionConfig("mock://direct-sampling/selected", adapter, "mock"))
    other = runtime.add(ConnectionConfig("mock://direct-sampling/unselected", "joinwit.jw8105", "mock"))
    for device in (key, other):
        runtime.connect(device).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    popups = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args: popups.append(args) or QMessageBox.StandardButton.No
    )
    try:
        runtime.monitoring.enable_settings_sync(False)
        window.navigate("实时监测")
        window.dashboard.cards[other].enabled.setChecked(False)
        save_monitoring_interval(window.controller, key, "general", 0.5, cadence="interval")
        if action == "overview":
            window.dashboard.sample_button.click()
        else:
            window.navigate("实时监测", "multimeter", key)
            page = window.stack.currentWidget()
            qtbot.waitUntil(page.buttons[action].isEnabled)
            page.buttons[action].click()
        assert not popups
        qtbot.waitUntil(lambda: key in runtime.monitoring.tasks, timeout=5000)
        task = runtime.monitoring.tasks[key]
        qtbot.waitUntil(
            lambda: runtime.monitoring.latest(key, task.requests[0].kind) is not None, timeout=5000
        )
        assert task.requests[0].confirmed
        if adapter == "rigol.m300":
            assert task.requests[0].channels == (1, 2)
        if adapter in {"keysight.3458a", "ceyear.6362d"}:
            assert task.requests[0].initiate
        assert other not in runtime.monitoring.tasks
        runtime.monitoring.stop(key).result(5)
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.bridge.worker.shutdown(wait=True)
        window.closing = True
        window.close()


def test_monitor_overview_uses_connected_devices_and_slot_preferences(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    first = runtime.add(ConnectionConfig("mock://overview/first", "rs.sma100b", "mock"))
    second = runtime.add(ConnectionConfig("mock://overview/second", "rs.sma100b", "mock"))
    runtime.add(ConnectionConfig("mock://overview/offline", "rs.sma100b", "mock"))
    for device_id, index in ((first, 1), (second, 2)):
        runtime.bind_slot(device_id, f"signal_generator:{index}", "general")
    runtime.connect(first).result(3)
    runtime.connect(second).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.controller.timer.stop()
    try:
        window.navigate("实时监测")
        assert "数据采集" not in window.nav_buttons
        assert set(window.dashboard.cards) == {first, second}
        assert set(window.dashboard.groups) == {"signal_generator"}

        first_card = window.dashboard.cards[first]
        second_card = window.dashboard.cards[second]
        assert not hasattr(first_card, "plot")
        assert not hasattr(second_card, "plot")
        assert first_card.interval.isEnabled() and second_card.interval.isEnabled()
        assert first_card.slot.parent() is first_card
        assert first_card.enabled.parent() is first_card
        assert first_card.interval.parent() is first_card
        assert first_card.details_dialog.findChild(type(first_card.enabled)) is None
        assert first_card.details_dialog.findChild(type(first_card.interval)) is None
        assert first_card.state.text() == "待开始" and second_card.state.text() == "待开始"
        assert 'href="#details"' in first_card.details_link.text()
        assert "详情" in first_card.details_link.text()
        assert "尚未开始记录" in first_card.detail_path.text()
        assert not first_card.details_dialog.isVisible()
        first_card.enabled.setChecked(False)
        first_key = window.dashboard._slot_key(window.controller.records[first])
        assert runtime.repository.load_workspace("monitor_overview/all")[first_key]["enabled"] is False

        # This verifies slot/UI state, not fastest-throughput backpressure. A
        # zero-I/O Mock can otherwise fill its bounded buffer before Qt polls.
        save_monitoring_interval(window.controller, second, "general", 0.5, cadence="interval")
        window.dashboard.toggle_sampling()
        assert not runtime.monitoring.snapshots().get(first, {}).get("running")
        qtbot.waitUntil(lambda: runtime.monitoring.snapshots()[second]["recording"])
        second_card.refresh()
        first_card.refresh()
        assert second_card.property("recordingPhase") == "recording"
        assert first_card.property("recordingPhase") == "idle"
        runtime.monitoring.stop(second).result(3)
        second_card.refresh()
        assert second_card.property("recordingPhase") == "idle"

        runtime.sessions.disconnect(first)
        window.controller.poll()
        assert first not in window.dashboard.cards
        runtime.connect(first).result(3)
        window.controller.poll()
        assert first in window.dashboard.cards
        assert not window.dashboard.cards[first].enabled.isChecked()
        assert second_card.spec["device_id"] == second
    finally:
        window.controller.close()


def test_monitor_overview_includes_optical_power_meter_in_electronic_layout(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    device = runtime.add(ConnectionConfig("mock://overview/power-meter", "joinwit.jw8105", "mock"))
    runtime.bind_slot(device, "optical_power_meter:1", "optical")
    runtime.connect(device).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.controller.timer.stop()
    try:
        window.navigate("实时监测")
        assert device in window.dashboard.cards
        spec = next(item for item in window.dashboard.specs if item["device_id"] == device)
        assert spec["category"] == "optical_power_meter"
        assert spec["recording_mode"] == "general"
        assert spec["sources"] == ["reading"]

        # Verify routing and selected channels without racing the bounded
        # recorder against a zero-I/O Mock at its fastest possible cadence.
        save_monitoring_interval(window.controller, device, "general", 0.5, cadence="interval")
        window.dashboard.toggle_sampling()
        qtbot.waitUntil(lambda: runtime.monitoring.snapshots().get(device, {}).get("recording"), timeout=5000)
        task = runtime.monitoring.tasks[device]
        assert task.mode == "general"
        assert task.requests[0].quantity == "power"
        assert task.requests[0].channels == tuple(range(1, 17))
        runtime.monitoring.stop(device).result(5)
    finally:
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()
