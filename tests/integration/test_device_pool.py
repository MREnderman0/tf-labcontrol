import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QInputDialog, QPushButton

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.service import DiscoveryCandidate
from lab_control.domain.instruments import DeviceIdentity
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


@pytest.mark.parametrize("replace_binding", [False, True])
def test_occupied_slot_is_selectable_and_requires_confirmation(workspace, qtbot, replace_binding):
    window, runtime, previous = workspace
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not runtime.sessions.sessions[previous].pending)
    config = ConnectionConfig("mock://replacement", "rs.sma100b", "mock", expected_serial="NEW-FIXTURE")
    key = runtime.add(config)
    runtime.connect(key).result(3)
    runtime.inventory.edit(previous, display_name="原设备")
    runtime.bind_slot(previous, "signal_generator:1", "general")
    runtime.bind_slot(key, "signal_generator:2", "general")
    window.refresh()
    window.open_category("signal_generator")
    window.tree.setCurrentItem(window.rows[key])
    seen = []

    def confirm():
        dialog = QApplication.activeModalWidget()
        try:
            assert dialog.windowTitle() == "替换槽位绑定"
            buttons = dialog.findChild(QDialogButtonBox)
            cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
            assert cancel.isDefault()
            seen.append("confirm")
            button = (
                QDialogButtonBox.StandardButton.Ok
                if replace_binding
                else QDialogButtonBox.StandardButton.Cancel
            )
            qtbot.mouseClick(buttons.button(button), Qt.MouseButton.LeftButton)
        finally:
            if dialog.isVisible():
                dialog.reject()

    def choose():
        dialog = QApplication.activeModalWidget()
        try:
            combo = dialog.findChild(WorkspaceComboBox)
            assert "原设备" in combo.itemText(1) and "已绑定" in combo.itemText(1)
            combo.setCurrentIndex(1)
            QTimer.singleShot(0, confirm)
            dialog.accept()
        except Exception:
            dialog.reject()
            raise

    QTimer.singleShot(0, choose)
    qtbot.mouseClick(window.action_buttons["slot"], Qt.MouseButton.LeftButton)
    assert seen == ["confirm"]
    records = {r["id"]: r for r in runtime.inventory.all()}
    assert records[previous]["slots"]["general"] == ("" if replace_binding else "signal_generator:1")
    assert records[key]["slots"]["general"] == (
        "signal_generator:1" if replace_binding else "signal_generator:2"
    )
    assert runtime.sessions.sessions[previous].state == "connected"
    assert runtime.sessions.sessions[key].state == "connected"


def test_overview_batch_shows_progress_results_and_ignores_filters(workspace, qtbot, monkeypatch):
    import threading

    from PySide6.QtWidgets import QTextBrowser

    from lab_control.demo.instrument import SimulatedInstrument
    from lab_control.domain.instruments import DeviceError

    window, runtime, key = workspace
    runtime.monitoring.enable_settings_sync(False)
    for name, adapter in (("good", "thorlabs.pm100d"), ("bad", "rs.sma100b")):
        config = ConnectionConfig(f"mock://{name}", adapter, "mock", expected_serial=name)
        runtime.inventory.merge(
            DiscoveryCandidate(config, "configured"), "scan", SimulatedInstrument(config).identify(), adapter
        )
    entered, release = threading.Event(), threading.Event()

    class Slow(SimulatedInstrument):
        def identify(self):
            entered.set()
            assert release.wait(5)
            return super().identify()

    def factory(config):
        if config.expected_serial == "bad":
            raise DeviceError("fixture 连接失败", "timeout")
        return Slow(config)

    monkeypatch.setattr(runtime.sessions, "factory", factory)
    window.open_category("signal_generator")
    window.presence_filter.setCurrentIndex(window.presence_filter.findData("history"))
    window.device_pool.hide()
    window.refresh()
    qtbot.mouseClick(window.connect_all_button, Qt.MouseButton.LeftButton)
    try:
        assert entered.wait(2)
        window.refresh()
        assert "3" in window.connection_summary.text() and window.stop_connect_button.isVisible()
        assert not window.scan_button.isEnabled() and not window.connect_all_button.isEnabled()
        for width, height in ((1440, 900), (1000, 650)):
            from PySide6.QtCore import QRect

            window.fit_to_screen(QRect(0, 0, width, height))
            window.resize(width - 32, height - 60)
            qtbot.wait(10)
            for button in (window.connect_all_button, window.stop_connect_button):
                rect = button.rect().translated(button.mapTo(window.overview_page, QPoint()))
                assert window.overview_page.rect().contains(rect)
    finally:
        release.set()
    qtbot.waitUntil(lambda: runtime.connection_progress()["state"] == "completed")
    window.refresh()
    assert "成功 1" in window.connection_summary.text()
    assert "已连接 1" in window.connection_summary.text() and "失败 1" in window.connection_summary.text()
    assert window.connection_results_button.isVisible() and window.stop_connect_button.isHidden()
    assert window.scan_button.isEnabled() and window.connect_all_button.isEnabled()
    assert runtime.sessions.sessions[key].state == "connected"

    def inspect():
        dialog = QApplication.activeModalWidget()
        try:
            assert dialog.windowTitle() == "一键连接结果"
            assert "fixture 连接失败" in dialog.findChild(QTextBrowser).toPlainText()
        finally:
            dialog.reject()

    QTimer.singleShot(0, inspect)
    qtbot.mouseClick(window.connection_results_button, Qt.MouseButton.LeftButton)


@pytest.fixture
def workspace(qtbot, tmp_path, qapp):
    old_style, old_font = qapp.styleSheet(), qapp.font()
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    config = ConnectionConfig("mock://pool", "rs.sma100b", "mock")
    key = runtime.add(config)
    runtime.connect(key).result()
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.timer.stop()
    window.refresh()
    yield window, runtime, key
    window.closing = True
    window.hide()
    window.bridge.worker.shutdown()
    runtime.close()
    qapp.setStyleSheet(old_style)
    qapp.setFont(old_font)


def test_pool_is_bounded_by_overview_and_closes_on_every_navigation(workspace, qtbot):
    window, _runtime, _key = workspace
    window.open_category("signal_generator")
    assert window.device_pool.parentWidget() is window.overview_page
    for width, height in ((1440, 900), (1000, 650), (860, 600), (1600, 1000)):
        window.resize(width, height)
        qtbot.wait(20)
        assert window.device_pool.geometry() == window.overview_page.rect()
        rect = window.device_pool.panel.geometry()
        assert rect.top() >= 24 and rect.left() >= 24
        assert rect.bottom() < window.overview_page.height() - 24
        assert rect.right() < window.overview_page.width() - 24
        assert rect.width() <= 960 and rect.height() <= 640
        assert abs(rect.center().x() - window.overview_page.rect().center().x()) <= 1
        assert abs(rect.center().y() - window.overview_page.rect().center().y()) <= 1
        for button in (window.pool_close, window.pool_more, window.action_buttons["monitor"]):
            assert isinstance(button, QPushButton)
            top_left = button.mapTo(window.device_pool.panel, QPoint())
            assert window.device_pool.panel.rect().contains(button.rect().translated(top_left))
    window.submenu.setCurrentIndex(1)
    assert window.device_pool.isHidden()
    window.submenu.setCurrentIndex(0)
    assert window.device_pool.isHidden()
    window.open_category("signal_generator")
    window.navigate("实时监测")
    assert window.device_pool.isHidden()
    window.navigate("设备管理")
    window.refresh()
    assert window.device_pool.isHidden()


def test_blank_escape_popup_and_background_refresh(workspace, qtbot):
    window, _runtime, key = workspace
    window.open_category("signal_generator")
    qtbot.mouseClick(window.overview_page, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert window.device_pool.isHidden()
    window.tree.clearSelection()
    window.tree.setCurrentItem(window.rows[key])
    window.refresh()
    window.resize(window.width() - 10, window.height())
    assert window.device_pool.isHidden()
    window.open_category("signal_generator")
    qtbot.mouseClick(window.pool_title, Qt.MouseButton.LeftButton)
    assert window.device_pool.isVisible()
    window.filter.showPopup()
    qtbot.keyClick(window.filter.view(), Qt.Key.Key_Escape)
    assert window.device_pool.isVisible()
    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    qtbot.mouseClick(dialog, Qt.MouseButton.LeftButton)
    assert window.device_pool.isVisible()
    dialog.close()
    qtbot.keyClick(window.tree, Qt.Key.Key_Escape)
    assert window.device_pool.isHidden()


def test_history_filter_and_counts_share_current_presence(workspace):
    window, runtime, key = workspace
    cfg = ConnectionConfig("mock://unplugged", "rs.sma100b", "mock")
    identity = DeviceIdentity("Rohde&Schwarz", "SMA100B", "OFFLINE-FIXTURE")
    old = runtime.inventory.merge(DiscoveryCandidate(cfg, "configured"), "old", identity, cfg.adapter)
    runtime.inventory.merge(
        DiscoveryCandidate(cfg, "configured"), "new", error="fixture unplugged", reason="probe_failed"
    )
    window.refresh()
    window.open_category("signal_generator")
    assert not window.rows[key].isHidden()
    assert window.rows[old["id"]].isHidden()
    assert window.cards["signal_generator"].visible_count.text() == "可见 1 台"
    assert "发现 1 台" in window.summary.text()
    window.presence_filter.setCurrentIndex(window.presence_filter.findData("history"))
    assert window.rows[key].isHidden() and not window.rows[old["id"]].isHidden()
    assert "探测失败" in window.rows[old["id"]].text(3)
    assert window.details.values["identity_state"].text() == "历史身份已保留"
    assert window.details.values["probe"].text() == "身份探测失败"
    window.presence_filter.setCurrentIndex(window.presence_filter.findData("all"))
    assert all(not row.isHidden() for row in window.rows.values())


def test_exposed_card_switches_directly_and_minimum_layout_reaches_all_content(workspace, qtbot):
    window, _runtime, _key = workspace
    window.resize(1700, 1000)
    qtbot.wait(20)
    window.open_category("power_supply")
    point = window.device_pool.mapFromGlobal(window.cards["signal_generator"].mapToGlobal(QPoint(5, 5)))
    assert not window.device_pool.panel.geometry().contains(point)
    qtbot.mouseClick(window.device_pool, Qt.MouseButton.LeftButton, pos=point)
    assert window.device_pool.isVisible()
    assert window.active_category == "signal_generator"
    from PySide6.QtCore import QRect

    window.fit_to_screen(QRect(0, 0, 1920, 1080))
    window.resize(900, 620)
    qtbot.wait(20)
    assert window.size().toTuple() == (1280, 760)
    assert window.device_pool.pages.currentIndex() == 0
    scroll = window.device_pool.scroll_area
    scroll.ensureWidgetVisible(window.details.values["readback"])
    qtbot.wait(20)
    field = window.details.values["readback"]
    center = field.mapTo(scroll.viewport(), field.rect().center())
    assert scroll.viewport().rect().contains(center)
    assert scroll.horizontalScrollBar().maximum() == 0
    button = window.action_buttons["monitor"]
    assert isinstance(button, QPushButton)
    point = button.mapTo(window.device_pool.panel, button.rect().center())
    assert window.device_pool.panel.rect().contains(point)
    assert (
        not scroll.viewport().rect().contains(scroll.viewport().mapFromGlobal(button.mapToGlobal(QPoint())))
    )


def test_scan_completion_never_reopens_pool(workspace, qtbot):
    window, runtime, _key = workspace
    window.open_category("signal_generator")
    window.device_pool.hide()
    window.scan()
    qtbot.waitUntil(lambda: runtime.discovery.future.done(), timeout=3000)
    qtbot.wait(30)
    window.refresh()
    assert window.device_pool.isHidden()


def test_backdrop_consumes_click_without_invoking_underlying_buttons(workspace, qtbot):
    window, _runtime, _key = workspace
    window.resize(1800, 1200)
    qtbot.wait(20)
    window.open_category("signal_generator")
    triggered = []
    underlying = QPushButton("must not trigger", window.overview_page)
    underlying.setGeometry(2, 2, 100, 20)
    underlying.clicked.connect(lambda: triggered.append(True))
    underlying.show()
    window.device_pool.raise_()
    qtbot.mouseClick(window.device_pool, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert window.device_pool.isHidden()
    assert triggered == []


def test_focus_more_menu_and_real_child_dialog_close_one_layer(workspace, qtbot):
    window, _runtime, _key = workspace
    window.activateWindow()
    card = window.cards["signal_generator"]
    card.setFocus()
    window.open_category("signal_generator")
    qtbot.wait(20)
    assert window.device_pool.panel.isAncestorOf(QApplication.focusWidget())
    window.pool_menu.popup(window.pool_more.mapToGlobal(QPoint()))
    qtbot.wait(20)
    qtbot.keyClick(window.pool_menu, Qt.Key.Key_Escape)
    assert not window.pool_menu.isVisible()
    assert window.device_pool.isVisible()
    for _ in range(2):
        window.pool_menu.popup(window.pool_more.mapToGlobal(QPoint()))
        qtbot.wait(20)
        outside = window.pool_menu.mapFromGlobal(window.device_pool.mapToGlobal(QPoint(5, 5)))
        qtbot.mouseClick(window.pool_menu, Qt.MouseButton.LeftButton, pos=outside)
        qtbot.wait(20)
        assert not window.pool_menu.isVisible()
        assert window.device_pool.isVisible()
    dialogs = []

    def cancel_rename():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QInputDialog)
        dialogs.append(dialog)
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)

    QTimer.singleShot(20, cancel_rename)
    window.action_buttons["rename"].trigger()
    assert dialogs and window.device_pool.isVisible()
    # The offscreen platform does not restore the parent window's activation after a native dialog.
    window.activateWindow()
    qtbot.wait(20)
    qtbot.keyClick(window.tree, Qt.Key.Key_Escape)
    assert window.device_pool.isHidden()
    assert QApplication.focusWidget() is card


def test_refresh_retains_selection_and_scroll_without_io(workspace, qtbot, monkeypatch):
    window, runtime, key = workspace
    window.resize(1440, 900)
    window.open_category("signal_generator")
    qtbot.wait(20)
    scroll = window.device_pool.scroll_area
    scroll.ensureWidgetVisible(window.details.values["error"])
    qtbot.wait(20)
    position = scroll.verticalScrollBar().value()
    assert position > 0
    row = window.rows[key]
    monkeypatch.setattr(runtime.sessions, "submit", lambda *_a, **_kw: pytest.fail("UI refresh sent I/O"))
    for _ in range(3):
        window.refresh()
        qtbot.wait(1)
    assert window.selected_id == key and window.tree.currentItem() is row
    assert scroll.verticalScrollBar().value() == position
    assert window.details.values["connection"].text() == "已连接"


def test_empty_filter_clears_actions_and_more_preserves_enabled_rules(workspace, qtbot):
    window, _runtime, key = workspace
    window.open_category("signal_generator")
    assert not window.action_buttons["connect"].isVisible()
    assert window.action_buttons["disconnect"].isEnabled()
    assert window.action_buttons["identify"].isEnabled()
    assert not window.action_buttons["resource"].isEnabled()
    window.states[key].update(pending=True, activeTask="continuous_acquisition")
    window.refresh_details()
    assert not window.action_buttons["disconnect"].isEnabled()
    assert not window.action_buttons["identify"].isEnabled()
    assert window.action_buttons["stop"].isVisible() and window.action_buttons["stop"].isEnabled()
    window.filter.setCurrentIndex(window.filter.findData("disconnected"))
    assert not window.selected_id and window.pool_empty.isVisible()
    assert not window.pool_more.isEnabled()
    assert all(not action.isEnabled() for action in window.action_buttons.values())
    window.device_pool.hide()
    window.open_category("power_supply")
    window.activateWindow()
    qtbot.wait(20)
    assert window.device_pool.panel.isAncestorOf(QApplication.focusWidget())


def test_long_fields_wrap_and_stopping_action_remains_reachable(workspace, qtbot):
    window, runtime, key = workspace
    runtime.inventory.edit(key, display_name="很长的设备名称" * 20)
    window.refresh()
    window.open_category("signal_generator")
    for width in (860, 1440, 1000):
        window.resize(width, 650)
        window.states[key].update(pending=True, activeTask="continuous_acquisition")
        window.refresh_details()
        window.details.values["resource"].setText("TCPIP::" + "a" * 300 + "::INSTR")
        qtbot.wait(20)
        scroll = window.device_pool.scroll_area
        scroll.ensureWidgetVisible(window.details.values["resource"])
        qtbot.wait(20)
        assert scroll.horizontalScrollBar().maximum() == 0
        field = window.details.values["resource"]
        right = field.mapTo(scroll.viewport(), field.rect().topRight()).x()
        assert right < scroll.viewport().width()
        stop = window.action_buttons["stop"]
        assert isinstance(stop, QPushButton)
        assert stop.isVisible()
        rect = stop.rect().translated(stop.mapTo(window.device_pool.panel, QPoint()))
        assert window.device_pool.panel.rect().contains(rect)


def test_primary_actions_share_geometry_and_slot_is_outside_menu(workspace, qtbot):
    window, _runtime, key = workspace
    window.open_category("signal_generator")
    assert isinstance(window.action_buttons["slot"], QPushButton)
    assert "功能槽位" not in [action.text() for action in window.pool_menu.actions()]
    buttons = [window.action_buttons[name] for name in ("disconnect", "slot", "manage", "monitor")]
    buttons.append(window.pool_more)
    qtbot.wait(20)
    assert len({button.height() for button in buttons}) == 1
    assert len({button.font().pixelSize() for button in buttons}) == 1
    assert all(button.isVisible() and button.property("poolAction") for button in buttons)
    assert [button.x() for button in buttons] == sorted(button.x() for button in buttons)
    window.pool_menu.popup(window.pool_more.mapToGlobal(QPoint()))
    qtbot.wait(10)
    assert all(
        window.pool_menu.actionGeometry(action).height() == 34
        for action in window.pool_menu.actions()
        if not action.isSeparator()
    )
    window.pool_menu.hide()
    window.states[key].update(pending=True, activeTask="continuous_acquisition")
    window.refresh_details()
    assert not window.action_buttons["slot"].isEnabled()


def test_external_slot_button_binds_and_unbinds_through_shared_dialog(workspace, qtbot):
    window, runtime, key = workspace
    runtime.monitoring.enable_settings_sync(False)
    qtbot.waitUntil(lambda: not runtime.sessions.sessions[key].pending)
    window.open_category("signal_generator")
    for index, expected in ((1, "signal_generator:1"), (0, "")):

        def choose():
            dialog = QApplication.activeModalWidget()
            assert dialog.windowTitle() == "功能槽位"
            dialog.findChild(WorkspaceComboBox).setCurrentIndex(index)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(10, choose)
        qtbot.mouseClick(window.action_buttons["slot"], Qt.MouseButton.LeftButton)
        record = next(record for record in runtime.inventory.all() if record["id"] == key)
        assert record["slots"].get("general", "") == expected
        assert window.device_pool.isVisible()
