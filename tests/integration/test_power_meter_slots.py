"""Three model-only meters keep distinct slot bindings throughout the workspace."""

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import DeviceError, DeviceIdentity, ValidationError
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.device_selector import DeviceSelector


def test_three_model_only_meters_keep_slots_in_ui_and_after_restart(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        SimulatedInstrument,
        "identify",
        lambda self: DeviceIdentity("Joinwit", "JW8105", confidence="model_only"),
    )
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.monitoring.enable_settings_sync(False)
    ids = []
    for index in range(1, 4):
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
                ]
                return labels[selected], True

            monkeypatch.setattr("lab_control.ui.workspace.actions.choose_item", choose)
            window.choose_slot(key)
        window.controller.poll()
        window.refresh()
        selector.refresh()
        window.dashboard.refresh()
        assert len(runtime.inventory.all()) == 3
        assert len(runtime.sessions.sessions) == 3
        assert tuple(card.category for card in window.card_grid.cards) == ("optical_power_meter",)
        assert window.cards["optical_power_meter"].online_count.text() == "03"
        assert selector.combo.count() == 3
        for index, key in enumerate(ids, 1):
            text = selector.combo.itemText(selector.combo.findData(key))
            assert text.startswith(f"功率计{index} · JW8105")
            assert f"mock://meter-{index}" in text
            assert window.dashboard.cards[key].slot.text() == f"功能槽位：功率计{index}"
        with pytest.raises(DeviceError) as conflict:
            runtime.bind_slot(ids[0], "optical_power_meter:3", "general")
        assert conflict.value.code == "slot_conflict"
        with pytest.raises(ValidationError):
            runtime.bind_slot(ids[0], "optical_power_meter:4", "general")
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
