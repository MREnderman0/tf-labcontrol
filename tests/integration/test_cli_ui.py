import json

import pytest
from PySide6.QtCore import Qt

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.cli.main import main
from lab_control.ui.pages.device_control.power_supply import PowerSupplyPage
from lab_control.ui.pages.monitoring.power import PowerMonitoringPage
from lab_control.ui.real_window import CATEGORIES, RealWindow


def test_cli_mock_readonly_report_and_explicit_target(tmp_path, capsys):
    common = ["--mock", "--home", str(tmp_path)]
    assert main(common + ["identity"]) == 2
    assert "device_required" in capsys.readouterr().out
    args = common + ["--resource", "mock://one", "--adapter", "rigol.dg5504pro"]
    assert main(args + ["test"]) == 0
    assert len(list(tmp_path.rglob("reports/*.json"))) == 1
    assert main(args + ["acquire", "--kind", "waveform", "--points", "16"]) == 0


def test_scan_stress_reports_live_sessions_and_exported_capture(tmp_path, capsys):
    config = tmp_path / "instruments.toml"
    config.write_text(
        "\n".join(
            f'[[devices]]\nresource="mock://{i}"\nadapter="rigol.dg5504pro"\ntransport="mock"'
            for i in range(6)
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--mock",
                "--home",
                str(tmp_path),
                "--config",
                str(config),
                "scan-stress",
                "--resources",
                *(f"mock://{i}" for i in range(6)),
                "--capture-resource",
                "mock://0",
                "--kind",
                "waveform",
                "--points",
                "16",
                "--repeats",
                "4",
            ]
        )
        == 0
    )
    report = json.loads(next(tmp_path.rglob("reports/*.json")).read_text(encoding="utf-8"))
    assert report["software_checks_passed"] and not report["hardware_accepted"]
    assert len(report["before"]) == len(report["after"]) == 6
    assert report["capture_paths"]
    assert all(
        item["reason"] == "cached_active_session" for scan in report["scans"] for item in scan["items"]
    )
    capsys.readouterr()


def test_real_overview_incremental_mode_and_scan(qtbot, tmp_path, monkeypatch):
    config = ConnectionConfig("mock://one", "rigol.dg5504pro", "mock")
    runtime = ApplicationRuntime((config,), root=tmp_path, hardware=False)
    device_id = runtime.add(config)
    runtime.connect(device_id).result()
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.refresh()
    assert window.category_filter.count() == len(CATEGORIES) + 1
    assert len(window.card_grid.cards) == 1
    card = window.cards["signal_generator"]
    assert card.online_count.text() == "01"
    assert card.visible_count.text() == "可见 1 台"
    assert window.device_pool.isHidden()
    with monkeypatch.context() as context:
        context.setattr(
            runtime.sessions, "submit", lambda *_a, **_kw: pytest.fail("Opening cards must not send I/O")
        )
        qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert not window.device_pool.isHidden()
    assert window.selected_id == device_id
    row = window.rows[device_id]
    window.tree.setCurrentItem(row)
    session_id = runtime.sessions.snapshots()[0]["sessionId"]
    window.scan()
    qtbot.waitUntil(lambda: runtime.discovery.future.done(), timeout=3000)
    window.refresh()
    window.controller.set_mode("optical")
    assert window.controller.mode == "general"
    assert not hasattr(window, "mode")
    assert window.rows[device_id] is row
    assert window.selected_id == device_id
    assert runtime.sessions.snapshots()[0]["sessionId"] == session_id
    assert window.cards["signal_generator"] is card
    assert not any(c.category == "optical_power_meter" for c in window.card_grid.cards)
    assert window.action_buttons["disconnect"].isEnabled()
    window.timer.stop()
    runtime.close()
    window.closing = True
    window.bridge.worker.shutdown()


def test_optical_card_groups_spectrometer_and_power_meter(qtbot, tmp_path):
    configs = (
        ConnectionConfig("mock://spectrometer", "sim.optical_spectrometer", "mock"),
        ConnectionConfig("mock://power-meter", "sim.optical_power_meter", "mock"),
    )
    runtime = ApplicationRuntime(configs, root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        report = runtime.discovery.scan().result(timeout=5)
        assert report["state"] == "complete"
        window.refresh()
        card = window.cards["optical_power_meter"]
        assert card.discovered_count.text() == "发现 2 台"
        assert card.visible_count.text() == "可见 2 台"
    finally:
        window.timer.stop()
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()


def test_power_surfaces_route_coremorrow_to_management_and_other_supplies_to_monitoring(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    coremorrow = runtime.add(ConnectionConfig("mock://coremorrow", "coremorrow.xmt", "mock"))
    generic = runtime.add(ConnectionConfig("mock://generic-power", "gwinstek.gpd4303s", "mock"))
    runtime.connect(coremorrow).result()
    runtime.connect(generic).result()
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        window.refresh()
        assert "power_supply" in window._submenu_categories("设备管理")
        assert "power_supply" in window._submenu_categories("实时监测")

        window.navigate("设备管理", "power_supply", coremorrow)
        assert window.current_module == "设备管理"
        management = window.stack.currentWidget()
        assert isinstance(management, PowerSupplyPage)
        assert management.selector.combo.findData(coremorrow) >= 0
        assert management.selector.combo.findData(generic) == -1

        window.navigate("设备管理", "power_supply", generic)
        assert window.current_module == "实时监测"
        monitoring = window.stack.currentWidget()
        assert isinstance(monitoring, PowerMonitoringPage)
        assert monitoring.selector.combo.findData(generic) >= 0
        assert monitoring.selector.combo.findData(coremorrow) == -1
    finally:
        window.timer.stop()
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()


def test_overview_cards_only_show_current_scan_records(qtbot, tmp_path):
    config = ConnectionConfig("mock://unscanned", "rigol.dg5504pro", "mock")
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    device_id = runtime.add(config)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        window.refresh()
        assert not window.card_grid.cards
        runtime.connect(device_id).result()
        window.refresh()
        assert [card.category for card in window.card_grid.cards] == ["signal_generator"]
    finally:
        window.timer.stop()
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()
