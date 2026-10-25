"""Approved category entry points remain reachable without current device evidence."""

import pytest
from PySide6.QtCore import Qt

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.inventory import Inventory
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceIdentity
from lab_control.storage.repository import Repository
from lab_control.ui.real_window import RealWindow

PRIMARY_CATEGORIES = (
    "signal_generator",
    "power_supply",
    "oscilloscope",
    "spectrum_analyzer",
    "temperature_controller",
)


@pytest.fixture
def workspace(qtbot, tmp_path, request):
    if request.param == "history":
        repository = Repository(tmp_path / "simulation")
        try:
            inventory = Inventory(repository)
            for adapter, model in (("rs.sma100b", "SMA100B"), ("thorlabs.pm100d", "PM100D")):
                config = ConnectionConfig(f"mock://history/{adapter}", adapter, "mock")
                inventory.merge(
                    DiscoveryCandidate(config, "configured"),
                    "previous-launch",
                    DeviceIdentity("fixture", model, "HISTORICAL-FIXTURE"),
                    adapter,
                )
        finally:
            repository.close()
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    runtime.monitoring.enable_settings_sync(False)
    window.show()
    window.refresh()
    try:
        yield window, runtime
    finally:
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


@pytest.mark.parametrize("workspace", ["empty", "history"], indirect=True)
def test_primary_category_entries_remain_visible_without_current_devices(workspace, qtbot):
    window, runtime = workspace
    assert tuple(card.category for card in window.card_grid.cards) == PRIMARY_CATEGORIES
    assert not runtime.sessions.snapshots()
    assert "发现 0 台" in window.summary.text()
    for category in PRIMARY_CATEGORIES:
        card = window.cards[category]
        assert card.isVisible()
        assert card.visible_count.text() == "可见 0 台"
        assert card.online_count.text() == "00"
        assert all(value.text() == "0" for value in card.metrics.values())
        qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
        assert window.device_pool.isVisible()
        assert window.category_filter.currentData() == category
        assert window.pool_empty.isVisible()
        window.device_pool.hide()


@pytest.mark.parametrize("workspace", ["empty"], indirect=True)
def test_extra_categories_still_follow_current_presence_without_inflating_counts(workspace):
    window, runtime = workspace
    config = ConnectionConfig("mock://optical-entry", "thorlabs.pm100d", "mock")
    candidate = DiscoveryCandidate(config, "configured")
    runtime.inventory.merge(
        candidate, "current", DeviceIdentity("Thorlabs", "PM100D", "OPTICAL-FIXTURE"), config.adapter
    )
    window.refresh()
    assert tuple(card.category for card in window.card_grid.cards) == (
        *PRIMARY_CATEGORIES,
        "optical_power_meter",
    )
    card = window.cards["optical_power_meter"]
    assert card.visible_count.text() == "可见 1 台"
    assert card.online_count.text() == "00"
    assert card.metrics["disconnected"].text() == "1"
    runtime.inventory.merge(candidate, "later", error="fixture unplugged", reason="probe_failed")
    window.refresh()
    assert tuple(card.category for card in window.card_grid.cards) == PRIMARY_CATEGORIES
    assert card.isHidden()
    assert card.visible_count.text() == "可见 0 台"
    assert len(runtime.inventory.all()) == 1
