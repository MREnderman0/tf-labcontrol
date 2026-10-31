"""Four model-only meters keep distinct slots and reuse saved connection settings."""

import pytest
from PySide6.QtWidgets import QInputDialog

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.service import DiscoveryCandidate
from lab_control.domain.instruments import DeviceError, DeviceIdentity, ValidationError
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.device_selector import DeviceSelector


def test_four_model_only_meters_keep_slots_in_ui_and_after_restart(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        SimulatedInstrument,
        "identify",
        lambda self: DeviceIdentity("Joinwit", "JW8105", confidence="model_only"),
    )
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.monitoring.enable_settings_sync(False)
    ids = []
    for index in range(1, 5):
        key = runtime.add(ConnectionConfig(f"mock://meter-{index}", "joinwit.jw8105", "mock"))
        runtime.connect(key).result(5)
        ids.append(key)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    selector = DeviceSelector(window.controller, "optical_power_meter", window, compact=True)
    try:
        for index, key in enumerate(ids, 1):

            def choose(_parent, _title, _prompt, labels, selected=index):
                assert [label.split("（")[0] for label in labels] == [
                    "解除绑定",
                    "功率计1",
                    "功率计2",
                    "功率计3",
                    "功率计4",
                ]
                return labels[selected], True

            monkeypatch.setattr("lab_control.ui.workspace.actions.choose_item", choose)
            window.choose_slot(key)
        window.controller.poll()
        window.refresh()
        selector.refresh()
        window.dashboard.refresh()
        assert len(runtime.inventory.all()) == 4
        assert len(runtime.sessions.sessions) == 4
        assert tuple(card.category for card in window.card_grid.cards) == ("optical_power_meter",)
        assert window.cards["optical_power_meter"].online_count.text() == "04"
        assert selector.combo.count() == 4
        for index, key in enumerate(ids, 1):
            text = selector.combo.itemText(selector.combo.findData(key))
            assert text.startswith(f"功率计{index} · JW8105")
            assert f"mock://meter-{index}" in text
            assert window.dashboard.cards[key].slot.text() == f"功能槽位：功率计{index}"
        with pytest.raises(DeviceError) as conflict:
            runtime.bind_slot(ids[0], "optical_power_meter:3", "general")
        assert conflict.value.code == "slot_conflict"
        with pytest.raises(ValidationError):
            runtime.bind_slot(ids[0], "optical_power_meter:5", "general")
        runtime.sessions.disconnect(ids[1])
        window.controller.poll()
        selector.refresh()
        assert selector.combo.findData(ids[1]) == -1
        runtime.connect(ids[1]).result(5)
        expected = {key: {"general": f"optical_power_meter:{index}"} for index, key in enumerate(ids, 1)}
        assert {r["id"]: r["slots"] for r in runtime.inventory.all()} == expected
    finally:
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
    with ApplicationRuntime(root=tmp_path, hardware=False) as reopened:
        for key in reversed(ids):
            reopened.connect(key).result(5)
        assert {r["id"]: r["slots"] for r in reopened.inventory.all()} == expected


@pytest.mark.parametrize("entry", ["connect", "reconnect", "connect_all"])
def test_serial_meter_connection_reuses_saved_model_without_identity_prompt(
    qtbot, tmp_path, monkeypatch, entry
):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.monitoring.enable_settings_sync(False)
    config = ConnectionConfig("COM77", "joinwit.jw8105", "serial", usb_instance_id="FIXTURE-USB-BRIDGE")
    identity = DeviceIdentity("Joinwit", "JW8105", confidence="model_only")
    opened = []

    class Meter(SimulatedInstrument):
        def identify(self):
            return identity

    def factory(selected):
        opened.append(selected)
        return Meter(selected)

    monkeypatch.setattr(runtime.sessions, "factory", factory)
    record = runtime.inventory.merge(
        DiscoveryCandidate(config, "serial"), "scan-fixture", identity, config.adapter
    )
    key = record["id"]
    runtime.bind_slot(key, "optical_power_meter:4", "general")
    if entry in {"connect", "reconnect"}:
        runtime.connect(key).result(3)
        if entry == "connect":
            runtime.sessions.disconnect(key)
        opened.clear()
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    window.selected_id = key
    watched = []
    monkeypatch.setattr(window.bridge, "watch", lambda future, label: watched.append(future))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: pytest.fail("Unexpected identity prompt"))
    monkeypatch.setattr(window, "configure_adapter", lambda *a: pytest.fail("Unexpected model prompt"))
    monkeypatch.setattr(window, "_failed", pytest.fail)
    try:
        if entry == "connect_all":
            window.connect_all()
        else:
            window.action(entry)
        assert len(watched) == 1
        qtbot.waitUntil(watched[0].done, timeout=3000)
        result = watched[0].result()
        if entry == "connect_all":
            assert [item["status"] for item in result["items"]] == ["connected"]
        else:
            assert result == identity
        assert opened == [config]
        saved = next(r for r in runtime.inventory.all() if r["id"] == key)
        assert saved["config"]["expected_serial"] == ""
        assert saved["identity"]["confidence"] == "model_only"
        assert saved["slots"] == {"general": "optical_power_meter:4"}
    finally:
        window.controller.close()
        window.bridge.worker.shutdown()
        runtime.close()
        window.closing = True
