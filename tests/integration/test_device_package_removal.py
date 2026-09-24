import json

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.cli.main import main
from lab_control.domain.instruments import Unsupported
from lab_control.instruments import registry
from lab_control.ui.real_window import RealWindow, category_for


@pytest.mark.parametrize("removed", tuple(registry.DEVICE_DEFINITIONS))
def test_removed_package_keeps_old_inventory_cli_scan_and_ui_usable(
    qtbot, tmp_path, monkeypatch, capsys, removed
):
    monkeypatch.delitem(registry.DEVICE_DEFINITIONS, removed)
    monkeypatch.delitem(registry.PROFILES, removed)
    assert main(["models"]) == 0
    models = json.loads(capsys.readouterr().out)["models"]
    assert removed not in models and "rigol.dg5504pro" in models
    configs = (
        ConnectionConfig("mock://removed", removed, "mock"),
        ConnectionConfig("mock://remaining", "rigol.dg5504pro", "mock"),
    )
    runtime = ApplicationRuntime(configs, root=tmp_path, hardware=False)
    window = None
    try:
        old, other = [runtime.add(config) for config in configs]
        with pytest.raises(Unsupported):
            runtime.connect(old).result()
        runtime.connect(other).result()
        scan = runtime.discovery.scan().result()
        assert scan["state"] == "complete"
        records = {record["id"]: record for record in runtime.inventory.all()}
        assert old in records and category_for(records[old]) == "unknown"
        assert category_for(records[other]) == "signal_generator"
        window = RealWindow(runtime)
        qtbot.addWidget(window)
        window.show()
        window.navigate("设备管理", "signal_generator", other)
        qtbot.waitUntil(lambda: not window.controller.jobs)
        assert other in window.controller.records and old in window.controller.records
    finally:
        if window:
            window.timer.stop()
            window.controller.close()
            window.closing = True
            window.bridge.worker.shutdown()
        runtime.close()
