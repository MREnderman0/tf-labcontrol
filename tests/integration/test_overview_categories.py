"""Only categories with current device evidence appear in the overview."""

import pytest
from PySide6.QtCore import Qt

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.inventory import Inventory
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceIdentity
from lab_control.storage.repository import Repository
from lab_control.ui.real_window import RealWindow


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
def test_empty_categories_are_hidden_and_history_remains_accessible(workspace, qtbot):
    window, runtime = workspace
    assert window.card_grid.cards == ()
    assert not runtime.sessions.snapshots()
    assert "发现 0 台" in window.summary.text()
    assert window.overview_empty.isVisible()
    for card in window.cards.values():
        assert card.isHidden()
        assert card.visible_count.text() == "可见 0 台"
        assert card.online_count.text() == "00"
        assert all(value.text() == "0" for value in card.metrics.values())
    qtbot.mouseClick(window.device_list_button, Qt.MouseButton.LeftButton)
    assert window.device_pool.isVisible()
    assert window.pool_empty.isVisible()
    window.presence_filter.setCurrentIndex(window.presence_filter.findData("history"))
    assert sum(not row.isHidden() for row in window.rows.values()) == len(runtime.inventory.all())
    assert window.card_grid.cards == ()


@pytest.mark.parametrize("workspace", ["empty"], indirect=True)
@pytest.mark.parametrize(
    "adapter,category",
    [
        ("rs.sma100b", "signal_generator"),
        ("siglent.spd4306x", "power_supply"),
        ("rigol.dho924", "oscilloscope"),
        ("rigol.dsa815", "spectrum_analyzer"),
        ("sim.temperature_controller", "temperature_controller"),
        ("thorlabs.pm100d", "optical_power_meter"),
    ],
)
def test_all_categories_follow_current_presence_without_inflating_counts(workspace, adapter, category):
    window, runtime = workspace
    config = ConnectionConfig("mock://category-entry", adapter, "mock")
    candidate = DiscoveryCandidate(config, "configured")
    runtime.inventory.merge(
        candidate, "current", DeviceIdentity("fixture", "MODEL", "CATEGORY-FIXTURE"), config.adapter
    )
    window.refresh()
    assert tuple(card.category for card in window.card_grid.cards) == (category,)
    assert not window.overview_empty.isVisible()
    card = window.cards[category]
    assert card.isVisible()
    assert card.visible_count.text() == "可见 1 台"
    assert card.online_count.text() == "00"
    assert card.metrics["disconnected"].text() == "1"
    runtime.inventory.merge(candidate, "later", error="fixture unplugged", reason="probe_failed")
    window.refresh()
    assert window.card_grid.cards == ()
    assert window.overview_empty.isVisible()
    assert card.isHidden()
    assert card.visible_count.text() == "可见 0 台"
    assert len(runtime.inventory.all()) == 1
