import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QPushButton

from lab_control.demo.store import DemoStore
from lab_control.domain.models import ConnectionState, DeviceCategory, LabMode
from lab_control.ui.main_window import MainWindow


def test_store_filters_devices_by_mode() -> None:
    store = DemoStore()
    electronic_ids = {device.device_id for device in store.visible_devices()}
    assert "PM-01" not in electronic_ids
    store.set_mode(LabMode.OPTICAL)
    optical_ids = {device.device_id for device in store.visible_devices()}
    assert "PM-01" in optical_ids
    assert "SA-01" not in optical_ids


def test_store_connection_demo_updates_state() -> None:
    store = DemoStore()
    before = next(device for device in store.visible_devices() if device.device_id == "SG-01")
    assert before.connection_state is ConnectionState.CONNECTED
    store.toggle_connection("SG-01")
    after = next(device for device in store.visible_devices() if device.device_id == "SG-01")
    assert after.connection_state is ConnectionState.DISCONNECTED


def test_main_window_has_five_navigation_items(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert len(window.nav_buttons) == 5
    assert set(window.nav_buttons) == {"设备管理", "实时监测", "数据处理", "操作日志", "系统设置"}


def test_navigation_rebuilds_submenu(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window._select_module("操作日志")
    assert [button.text() for button in window.submenu_buttons] == [
        "全部日志",
        "设备操作",
        "采集与处理",
        "异常与告警",
    ]
    window._select_module("实时监测")
    assert [button.text() for button in window.submenu_buttons] == ["总览"]


def test_mode_switch_updates_overview_and_topbar(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_mode_for_demo(LabMode.OPTICAL)
    assert window.store.mode is LabMode.OPTICAL
    assert DeviceCategory.OPTICAL_POWER_METER in window.overview_page.cards
    assert window.mode_badge.label.text() == "光学模式"
    assert not hasattr(window, "online_label")


def test_mode_control_is_only_on_software_settings_page(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.mode_badge.isVisible() is False
    window._select_module("系统设置")
    assert window.current_submenu == "软件设置"
    assert window.stack.currentWidget() is window.software_settings_page


def test_device_drawer_and_demo_action(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_device_drawer(DeviceCategory.SIGNAL_GENERATOR)
    assert window.drawer.category is DeviceCategory.SIGNAL_GENERATOR
    assert window.drawer.isHidden() is False
    buttons = window.drawer.findChildren(QPushButton, "smallButton")
    assert buttons
    buttons[0].click()
    assert "已演示" in window.store.snapshot().status_message
