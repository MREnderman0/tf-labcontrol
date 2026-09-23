from __future__ import annotations

from PySide6.QtCore import QDateTime, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from lab_control.demo.store import DemoStore
from lab_control.domain.models import DeviceCategory, LabMode
from lab_control.ui.pages.overview_page import OverviewPage
from lab_control.ui.pages.placeholder_page import PlaceholderPage
from lab_control.ui.pages.settings_page import SoftwareSettingsPage
from lab_control.ui.widgets.device_drawer import DeviceDrawer
from lab_control.ui.widgets.mode_selector import DemoBadge, ModeBadge

NAVIGATION = {
    "设备管理": ("总览", "信号源", "电源", "示波器", "频谱仪", "温控"),
    "数据采集": ("采集配置", "采集中", "历史记录"),
    "实时监测": ("监测工作台",),
    "数据处理": ("处理工作台",),
    "操作日志": ("全部日志", "设备操作", "采集与处理", "异常与告警"),
    "系统设置": ("软件设置", "数据存储", "通信设置", "日志设置"),
}


class MainWindow(QMainWindow):
    def __init__(self, store: DemoStore | None = None) -> None:
        super().__init__()
        self.store = store or DemoStore(self)
        self.current_module = "设备管理"
        self.current_submenu = "总览"
        self.nav_buttons: dict[str, QPushButton] = {}
        self.submenu_buttons: list[QPushButton] = []
        self.setWindowTitle("TF LabControl · 设计演示")
        self.resize(1440, 900)
        self.setMinimumSize(1180, 760)
        self._build()
        self.store.changed.connect(self._refresh_shell)
        self.store.status_changed.connect(self.status_label.setText)
        self._select_module("设备管理")
        self._refresh_shell()

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._refresh_clock)
        self.clock_timer.start(1000)
        self._refresh_clock()

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("shellRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_sidebar())

        content = QWidget()
        content.setObjectName("contentArea")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._build_topbar())
        content_layout.addWidget(self._build_submenu())

        self.stack = QStackedWidget()
        self.overview_page = OverviewPage(self.store)
        self.overview_page.category_clicked.connect(self._open_device_drawer)
        self.stack.addWidget(self.overview_page)
        self._placeholder_pages: dict[tuple[str, str], QWidget] = {}
        self.software_settings_page = SoftwareSettingsPage(self.store)
        self.software_settings_page.mode_requested.connect(self._request_mode_switch)
        self._placeholder_pages[("系统设置", "软件设置")] = self.software_settings_page
        self.stack.addWidget(self.software_settings_page)
        content_layout.addWidget(self.stack, 1)

        self.status_label = QLabel("设计演示已就绪 · 未连接真实设备")
        self.status_label.setObjectName("statusBarLabel")
        content_layout.addWidget(self.status_label)

        root_layout.addWidget(content, 1)
        self.drawer = DeviceDrawer(self.store, self)
        self.drawer.closed.connect(self.drawer.hide)
        root_layout.addWidget(self.drawer)
        self.setCentralWidget(root)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 22, 18, 18)
        layout.setSpacing(8)

        brand = QLabel("TF LABCONTROL")
        brand.setObjectName("brand")
        version = QLabel("LABORATORY CONTROL / DESIGN")
        version.setObjectName("brandSubline")
        layout.addWidget(brand)
        layout.addWidget(version)
        layout.addSpacing(24)

        section = QLabel("工作区")
        section.setObjectName("sidebarSection")
        layout.addWidget(section)
        for module in NAVIGATION:
            button = QPushButton(module)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, value=module: self._select_module(value))
            self.nav_buttons[module] = button
            layout.addWidget(button)

        layout.addStretch(1)
        self.mode_badge = ModeBadge()
        self.mode_badge.setFixedWidth(112)
        self.demo_badge = DemoBadge()
        self.demo_badge.setFixedWidth(74)
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.setSpacing(6)
        badge_row.addWidget(self.mode_badge)
        badge_row.addWidget(self.demo_badge)
        layout.addLayout(badge_row)
        return sidebar

    def _build_topbar(self) -> QFrame:
        topbar = QFrame()
        topbar.setObjectName("topbar")
        layout = QHBoxLayout(topbar)
        layout.setContentsMargins(28, 18, 28, 16)
        layout.setSpacing(14)

        heading = QVBoxLayout()
        self.module_title = QLabel("设备管理")
        self.module_title.setObjectName("moduleTitle")
        self.breadcrumb = QLabel("设备管理 / 总览")
        self.breadcrumb.setObjectName("breadcrumb")
        heading.addWidget(self.module_title)
        heading.addWidget(self.breadcrumb)
        layout.addLayout(heading)
        layout.addStretch(1)

        scan = QPushButton("扫描演示")
        scan.setObjectName("primaryButton")
        scan.setFixedHeight(30)
        scan.clicked.connect(self.store.scan_demo)
        layout.addWidget(scan)

        self.clock_label = QLabel()
        self.clock_label.setObjectName("clockLabel")
        layout.addWidget(self.clock_label)
        return topbar

    def _build_submenu(self) -> QFrame:
        submenu = QFrame()
        submenu.setObjectName("submenuBar")
        self.submenu_layout = QHBoxLayout(submenu)
        self.submenu_layout.setContentsMargins(28, 0, 28, 0)
        self.submenu_layout.setSpacing(6)
        return submenu

    def _select_module(self, module: str) -> None:
        if module not in NAVIGATION:
            return
        self.current_module = module
        self.current_submenu = NAVIGATION[module][0]
        for name, button in self.nav_buttons.items():
            button.setChecked(name == module)
        self.module_title.setText(module)
        self._rebuild_submenu()
        self._show_current_page()

    def _rebuild_submenu(self) -> None:
        while self.submenu_layout.count():
            item = self.submenu_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.submenu_buttons.clear()
        for submenu in NAVIGATION[self.current_module]:
            button = QPushButton(submenu)
            button.setObjectName("submenuButton")
            button.setCheckable(True)
            button.setChecked(submenu == self.current_submenu)
            button.clicked.connect(lambda checked=False, value=submenu: self._select_submenu(value))
            self.submenu_layout.addWidget(button)
            self.submenu_buttons.append(button)
        self.submenu_layout.addStretch(1)

    def _select_submenu(self, submenu: str) -> None:
        self.current_submenu = submenu
        for button in self.submenu_buttons:
            button.setChecked(button.text() == submenu)
        self._show_current_page()

    def _show_current_page(self) -> None:
        self.breadcrumb.setText(f"{self.current_module} / {self.current_submenu}")
        if self.current_module == "设备管理" and self.current_submenu == "总览":
            self.stack.setCurrentWidget(self.overview_page)
            return
        key = (self.current_module, self.current_submenu)
        page = self._placeholder_pages.get(key)
        if page is None:
            page = PlaceholderPage(
                self.current_submenu,
                f"{self.current_module}中的{self.current_submenu}将在后续设计评审中继续展开。",
            )
            self._placeholder_pages[key] = page
            self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)

    def _request_mode_switch(self, mode: LabMode) -> None:
        if mode is self.store.mode:
            return
        answer = QMessageBox.question(
            self,
            "切换设计模式",
            f"确认切换到{mode.label}？当前仅更新演示界面状态。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.store.set_mode(mode)
        else:
            self.software_settings_page.refresh()

    def set_mode_for_demo(self, mode: LabMode) -> None:
        """Switch without a modal dialog for automated UI checks."""
        self.store.set_mode(mode)

    def _open_device_drawer(self, category: DeviceCategory) -> None:
        self.drawer.set_category(category)

    def _refresh_shell(self) -> None:
        self.mode_badge.set_mode(self.store.mode)
        self.overview_page.refresh()
        if self.drawer.isVisible():
            self.drawer.refresh()

    def _refresh_clock(self) -> None:
        self.clock_label.setText(QDateTime.currentDateTime().toString("HH:mm:ss"))
