"""Connected-only selectors use isolated inventory and Mock sessions, never hardware."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.pages.monitoring.dashboard import AddCardDialog
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def workspace(qtbot, tmp_path):
    runtime = ApplicationRuntime((), root=tmp_path, hardware=False)
    ids = {}
    for label, model in (
        ("first", "rs.sma100b"),
        ("second", "rs.sma100b"),
        ("power", "keithley.2231a"),
        ("offline", "tek.afg31252"),
    ):
        key = runtime.add(ConnectionConfig(f"mock://selection/{label}", model, "mock"))
        ids[label] = key
        if label != "offline":
            runtime.connect(key).result()
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.timer.stop()
    window.controller.timer.stop()
    runtime.monitoring.enable_settings_sync(False)
    try:
        yield window, runtime, ids
    finally:
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()


def keys(combo):
    return {combo.itemData(i) for i in range(combo.count())}


@pytest.mark.parametrize(
    "state,visible",
    [
        ("connected", True),
        ("busy", True),
        ("degraded", True),
        ("connecting", False),
        ("error", False),
        ("disconnected", False),
    ],
)
def test_all_selectors_share_cached_session_filter(workspace, qtbot, monkeypatch, state, visible):
    window, runtime, ids = workspace
    controller = window.controller
    controller.states[ids["first"]]["connectionState"] = state
    monkeypatch.setattr(runtime.sessions, "submit", lambda *a, **kw: pytest.fail("Filtering sent I/O"))
    expected = {ids["second"]} | ({ids["first"]} if visible else set())
    assert {r["id"] for r in controller.connected_records("signal_generator")} == expected
    for module in ("设备管理", "实时监测"):
        window.navigate(module, "signal_generator")
        selector = window.stack.currentWidget().selector
        assert keys(selector.combo) == expected
        assert all("未绑定" not in selector.combo.itemText(i) for i in range(selector.combo.count()))
        if state == "degraded":
            assert "需要检查" in selector.combo.itemText(selector.combo.findData(ids["first"]))
    dialog = AddCardDialog(controller, window)
    qtbot.addWidget(dialog)
    assert keys(dialog.device) == expected | {ids["power"]}


@pytest.mark.parametrize(
    "module,category,target",
    [
        ("设备管理", "signal_generator", "first"),
        ("设备管理", "power_supply", "power"),
        ("实时监测", "power_supply", "power"),
    ],
)
def test_disconnect_clears_open_popup_and_page_without_switch(workspace, qtbot, module, category, target):
    window, runtime, ids = workspace
    key = ids[target]
    window.navigate(module, category, key)
    page = window.stack.currentWidget()
    page.selector.combo.showPopup()
    qtbot.waitUntil(lambda: runtime.sessions.sessions[key].pending == 0)
    runtime.sessions.disconnect(key)
    window.controller.poll()
    assert not page.selector.combo.view().isVisible()
    assert page.selector.device_id == ""
    assert page.selector.channel.count() == 0
    assert page.selector.combo.currentIndex() == -1
    assert page.selector.connect_button.isVisible()
    if category == "signal_generator":
        assert not page.panels and not page.outputs and not page.advanced_groups
        assert page.advanced_channel.count() == 0
        assert page.tabs.isHidden() and page.empty.isVisible()
        assert not page.monitor_button.isEnabled()
        assert window.controller.sync_device == ""
    elif category == "power_supply":
        assert window.current_module == "实时监测"
        assert page.controls is None and not page.channel_checks
        assert page.splitter.isHidden()
        assert page.empty.isVisible() and window.controller.sync_device == ""
    else:
        assert not page.form.editors and page.form.device_id == ""
        if module == "实时监测":
            assert not page.channel_checks and page.source.count() == 0
            assert page.splitter.isHidden()
        else:
            assert page.form.isHidden()
    for _ in range(3):
        window.controller.poll()
        assert page.selector.device_id == ""
    qtbot.mouseClick(page.selector.connect_button, Qt.MouseButton.LeftButton)
    assert window.stack.currentWidget() is window.overview_page


@pytest.mark.parametrize("module", ["设备管理", "实时监测"])
def test_invalid_navigation_never_focuses_another_device(workspace, monkeypatch, module):
    window, runtime, ids = workspace
    focused = []
    monkeypatch.setattr(runtime.monitoring, "focus_settings", lambda device, *_: focused.append(device))
    window.navigate(module, "signal_generator", ids["offline"])
    page = window.stack.currentWidget()
    assert page.selector.device_id == ""
    assert not any(focused)
    page.selector.select(ids["first"])
    # An invalid target also clears an existing page, even while its former device is busy.
    window.controller.group_devices.add(ids["first"])
    window.navigate(module, "signal_generator", "missing-record")
    assert page.selector.device_id == ""


def test_monitor_dialog_disconnect_preserves_saved_cards_and_slots(workspace, qtbot):
    window, runtime, ids = workspace
    window.dashboard.add(ids["first"], "frequency", (1,))
    saved = runtime.repository.load_workspace("monitor_layout/electronic")
    slots = window.controller.records[ids["first"]]["slots"]
    dialog = AddCardDialog(window.controller, window)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.device.setCurrentIndex(dialog.device.findData(ids["first"]))
    dialog.device.showPopup()
    runtime.sessions.disconnect(ids["first"])
    window.controller.poll()
    assert dialog.device.currentIndex() == -1 and not dialog.device.view().isVisible()
    assert not dialog.checks and dialog.source.count() == dialog.view.count() == 0
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.accept()
    assert dialog.isVisible()
    assert runtime.repository.load_workspace("monitor_layout/electronic") == saved
    assert window.dashboard.specs == saved
    assert window.controller.records[ids["first"]]["slots"] == slots
    dialog.device.setCurrentIndex(dialog.device.findData(ids["second"]))
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_offline_pool_actions_require_connection(workspace):
    window, _runtime, ids = workspace
    window.presence_filter.setCurrentIndex(window.presence_filter.findData("all"))
    window.open_category("signal_generator")
    window.tree.setCurrentItem(window.rows[ids["offline"]])
    window.refresh_details()
    for name in ("manage", "monitor"):
        assert not window.action_buttons[name].isEnabled()
        assert "请先连接" in window.action_buttons[name].toolTip()
