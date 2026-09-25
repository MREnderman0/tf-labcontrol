from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QDateTime, QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QScreen
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabBar,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import instrument_category, mode_applicable
from lab_control.application.config import ConnectionConfig, config_from_record
from lab_control.application.controls import CATEGORY_LABELS, slot_label
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.presence import is_current, presence_label
from lab_control.domain.instruments import DeviceError, jsonable
from lab_control.instruments.registry import PROFILES, SDK_ADAPTERS, describe
from lab_control.ui.controller import (
    WorkspaceController,
    background_read_only,
    display_connection_state,
    display_task,
)
from lab_control.ui.pages.device_control.page import DeviceControlPage
from lab_control.ui.pages.device_control.power_supply import PowerSupplyPage
from lab_control.ui.pages.device_control.signal_source import SignalSourcePage
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.pages.monitoring.page import MonitoringPage
from lab_control.ui.pages.monitoring.spectrum import SpectrumMonitoringPage
from lab_control.ui.widgets.category_cards import CategoryCard, CategoryCardGrid
from lab_control.ui.widgets.device_pool import DeviceDetails, DevicePool, DeviceRowDelegate
from lab_control.ui.widgets.popup_surface import WorkspaceMenu
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox, choose_item

CATEGORIES = {
    "signal_generator": "信号源",
    "power_supply": "电源",
    "oscilloscope": "示波器",
    "spectrum_analyzer": "频谱仪",
    "temperature_controller": "温控设备",
    "optical_power_meter": "光学测量",
    "measurement_analyzers": "测量分析",
    "load_drivers": "负载 / 泵浦",
    "unknown": "未分类 / 未知设备",
}
PRIMARY_CATEGORIES = tuple(CATEGORIES)[:5]
CATEGORY_DESCRIPTIONS = {
    "signal_generator": "频率、幅度和输出状态",
    "power_supply": "电压、电流与通道状态",
    "oscilloscope": "波形、触发和采样配置",
    "spectrum_analyzer": "频率范围、RBW 与轨迹",
    "temperature_controller": "温度读回与稳定状态",
    "optical_power_meter": "光功率、光谱与传感器",
    "measurement_analyzers": "万用表、网络分析与相位噪声",
    "load_drivers": "电子负载与泵浦驱动",
    "unknown": "待确认身份与设备协议",
}


def category_for(record: dict[str, Any]) -> str:
    category = instrument_category(record)
    if category in {"multimeter", "network_analyzer", "phase_noise_analyzer"}:
        return "measurement_analyzers"
    if category in {"electronic_load", "pump_driver"}:
        return "load_drivers"
    if category == "optical_spectrometer":
        return "optical_power_meter"
    return category


def applicable(record: dict[str, Any], mode: str) -> bool:
    return mode_applicable(record, mode)


class ServiceBridge(QObject):
    completed = Signal(str, object)
    failed = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="desktop-service")

    def watch(self, future: Future[Any], label: str) -> None:
        def finish(task):
            try:
                self.completed.emit(label, task.result())
            except Exception as exc:
                self.failed.emit(str(exc))

        future.add_done_callback(finish)


class WorkspaceStack(QStackedWidget):
    """Inactive, larger forms must not impose their minimum width on the dashboard."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, 0)


class RealWindow(QMainWindow):
    def __init__(self, runtime: ApplicationRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self.controller = WorkspaceController(runtime, self)
        self.bridge = ServiceBridge(self)
        self.bridge.completed.connect(self._completed)
        self.bridge.failed.connect(self._failed)
        self.selected_id = ""
        self.active_category = ""
        self.rows: dict[str, QTreeWidgetItem] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.closing = False
        self._shutdown_started = False
        self._manual_sidebar_collapsed = False
        self._watched_screen: QScreen | None = None
        self._screen_connected = False
        self.setWindowTitle("TF LabControl · 分类卡片 · " + ("真实设备" if runtime.hardware else "模拟演示"))
        self.resize(1440, 900)
        self.setMinimumSize(1280, 760)
        self._build()
        self.fit_to_screen()
        self.controller.message.connect(self._failed)
        self.controller.navigate.connect(self.navigate)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)
        self.refresh()

    def _build(self) -> None:
        self._build_overview()
        self.overview_page = self.takeCentralWidget()
        assert self.overview_page is not None
        self.page_map: dict[tuple[str, str], QWidget] = {}
        self.current_module = "设备管理"
        root = QWidget()
        root.setObjectName("cardWorkspace")
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("workspaceSidebar")
        self.sidebar.setFixedWidth(232)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(18, 22, 18, 18)
        side.setSpacing(8)
        self.brand = QLabel("TF LABCONTROL")
        self.brand.setObjectName("commandBrand")
        side.addWidget(self.brand)
        self.brand_subline = QLabel("LABORATORY CONTROL")
        self.brand_subline.setObjectName("commandEyebrow")
        side.addWidget(self.brand_subline)
        side.addSpacing(24)
        self.sidebar_section = QLabel("工作区")
        self.sidebar_section.setObjectName("sidebarSection")
        side.addWidget(self.sidebar_section)
        self.nav_buttons = {}
        for module in ("设备管理", "数据采集", "实时监测", "数据处理", "操作日志", "系统设置"):
            button = QPushButton(module)
            button.setObjectName("workspaceNav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=module: self.navigate(value, "", ""))
            self.nav_buttons[module] = button
            side.addWidget(button)
        side.addStretch()
        badges = QHBoxLayout()
        badges.setSpacing(6)
        self.mode_badge = QLabel()
        self.mode_badge.setObjectName("modeBadge")
        self.runtime_badge = QLabel("● 真实设备" if self.runtime.hardware else "● 演示版")
        self.runtime_badge.setObjectName("runtimeBadge")
        badges.addWidget(self.mode_badge)
        badges.addWidget(self.runtime_badge)
        side.addLayout(badges)
        row.addWidget(self.sidebar)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        header = QFrame()
        header.setObjectName("commandHeader")
        top = QHBoxLayout(header)
        top.setContentsMargins(28, 18, 28, 16)
        header_heading = QVBoxLayout()
        header_heading.setSpacing(3)
        self.breadcrumb = QLabel()
        self.breadcrumb.setObjectName("commandEyebrow")
        self.section_title = QLabel("设备管理")
        self.section_title.setObjectName("commandModuleTitle")
        header_heading.addWidget(self.section_title)
        header_heading.addWidget(self.breadcrumb)
        top.addLayout(header_heading)
        top.addStretch()
        self.scan_button.setFixedHeight(30)
        top.addWidget(self.scan_button)
        self.clock_label = QLabel()
        self.clock_label.setObjectName("commandEyebrow")
        top.addWidget(self.clock_label)
        content_layout.addWidget(header)
        self.submenu = QTabBar()
        self.submenu.setObjectName("commandSubnav")
        self.submenu.setExpanding(False)
        self.submenu.setDrawBase(False)
        self.submenu.setUsesScrollButtons(True)
        self.submenu.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.submenu.currentChanged.connect(self.show_page)
        content_layout.addWidget(self.submenu)
        self.stack = WorkspaceStack()
        stack_layout = self.stack.layout()
        assert stack_layout is not None
        stack_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.stack.addWidget(self.overview_page)
        self.dashboard = MonitoringDashboard(self.controller)
        self.dashboard.open_card.connect(self.focus_card)
        self.stack.addWidget(self.dashboard)
        content_layout.addWidget(self.stack, 1)
        content_layout.addWidget(self.message)
        row.addWidget(content, 1)
        self.settings_page = QWidget()
        settings = QVBoxLayout(self.settings_page)
        heading = QLabel("软件设置")
        heading.setObjectName("pageTitle")
        settings.addWidget(heading)
        settings.addWidget(QLabel("实验模式"))
        settings.addWidget(self.mode)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.controller.mode)))
        settings.addWidget(QLabel("模式只影响布局、槽位候选和控制适用性；设备总览保留全部设备。"))
        directory = QLabel("数据目录：" + str(self.runtime.repository.root))
        directory.setWordWrap(True)
        settings.addWidget(directory)
        settings.addStretch()
        self.stack.addWidget(self.settings_page)
        self.setCentralWidget(root)
        self.navigate("设备管理", "", "")

    def toggle_sidebar(self) -> None:
        self._manual_sidebar_collapsed = not self._manual_sidebar_collapsed
        self.update_sidebar()

    def update_sidebar(self) -> None:
        collapsed = self._manual_sidebar_collapsed
        self.sidebar.setFixedWidth(76 if collapsed else 232)
        if layout := self.sidebar.layout():
            layout.setContentsMargins(8 if collapsed else 18, 22, 8 if collapsed else 18, 18)
        self.brand.setText("TF" if collapsed else "TF LABCONTROL")
        self.brand_subline.setVisible(not collapsed)
        self.sidebar_section.setVisible(not collapsed)
        self.mode_badge.setVisible(not collapsed)
        self.runtime_badge.setVisible(not collapsed)
        for label, button in self.nav_buttons.items():
            button.setText(label[:2] if collapsed else label)
            button.setToolTip(label)

    def fit_to_screen(self, available: QRect | None = None) -> None:
        """Use Qt logical screen coordinates, including work area and native window decoration."""
        if available is None:
            available = self.screen().availableGeometry()
        border = self.frameGeometry().size() - self.size()
        width = max(1, available.width() - max(0, border.width()))
        height = max(1, available.height() - max(0, border.height()))
        self.setMinimumSize(min(1280, width), min(760, height))
        if not self.isMaximized() and not self.isFullScreen():
            self.resize(min(self.width(), width), min(self.height(), height))
            frame = self.frameGeometry()
            x = max(available.x(), min(frame.x(), available.right() - frame.width() + 1))
            y = max(available.y(), min(frame.y(), available.bottom() - frame.height() + 1))
            self.move(self.pos().x() + x - frame.x(), self.pos().y() + y - frame.y())
        self.update_sidebar()

    def watch_screen(self, screen: QScreen) -> None:
        if self._watched_screen is not None:
            self._watched_screen.availableGeometryChanged.disconnect(self.fit_to_screen)
        self._watched_screen = screen
        screen.availableGeometryChanged.connect(self.fit_to_screen)
        QTimer.singleShot(0, self.fit_to_screen)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._screen_connected and (handle := self.windowHandle()):
            self._screen_connected = True
            handle.screenChanged.connect(self.watch_screen)
            self.watch_screen(handle.screen())

    def focus_card(self, spec: dict) -> None:
        page = self.stack.currentWidget()
        if isinstance(page, (MonitoringPage, SpectrumMonitoringPage)):
            page.focus_card(spec)

    def navigate(self, module: str, category: str = "", device_id: str = "") -> None:
        if module == "设备管理" and category == "spectrum_analyzer":
            module = "实时监测"
        if module not in self.nav_buttons:
            return
        self.device_pool.hide()
        self.current_module = module
        for name, button in self.nav_buttons.items():
            button.setChecked(name == module)
        self.section_title.setText(module)
        self.submenu.blockSignals(True)
        while self.submenu.count():
            self.submenu.removeTab(0)
        if module in {"设备管理", "实时监测"}:
            self.submenu.addTab("总览")
            self.submenu.setTabData(0, "")
            for value, label in CATEGORY_LABELS.items():
                if module == "设备管理" and value == "spectrum_analyzer":
                    continue
                index = self.submenu.addTab(label)
                self.submenu.setTabData(index, value)
                if value == category:
                    self.submenu.setCurrentIndex(index)
        else:
            self.submenu.addTab("软件设置" if module == "系统设置" else module)
            self.submenu.setTabData(0, "")
        self.submenu.blockSignals(False)
        self.show_page(device_id=device_id)

    def show_page(self, *_args, device_id: str = "") -> None:
        self.device_pool.hide()
        module, category = self.current_module, self.submenu.tabData(self.submenu.currentIndex()) or ""
        self.breadcrumb.setText(f"{module} / {self.submenu.tabText(self.submenu.currentIndex())}")
        if module == "设备管理" and not category:
            page = self.overview_page
        elif module == "实时监测" and not category:
            page = self.dashboard
        elif module == "系统设置":
            page = self.settings_page
        else:
            key = (module, category)
            page = self.page_map.get(key)
            if page is None:
                if module == "设备管理":
                    page = (
                        SignalSourcePage(self.controller)
                        if category == "signal_generator"
                        else PowerSupplyPage(self.controller)
                        if category == "power_supply"
                        else DeviceControlPage(self.controller, category)
                    )
                elif module == "实时监测":
                    page = (
                        SpectrumMonitoringPage(self.controller, self.dashboard.add)
                        if category == "spectrum_analyzer"
                        else MonitoringPage(self.controller, category, self.dashboard.add)
                    )
                else:
                    page = QWidget()
                    layout = QVBoxLayout(page)
                    title = QLabel(module)
                    title.setObjectName("pageTitle")
                    layout.addWidget(title)
                    message = {
                        "数据采集": "在实时监测的设备类型页开启记录，可保存原始数据和配置快照。",
                        "数据处理": "数据处理工作台尚未开放。已保存数据可使用 CSV / NPZ 导出文件。",
                        "操作日志": "设备操作、监测与异常按日保存在数据目录的 logs 文件夹。",
                    }[module]
                    layout.addWidget(QLabel(message))
                    button = QPushButton("进入实时监测")
                    button.clicked.connect(lambda: self.navigate("实时监测"))
                    layout.addWidget(button)
                    layout.addStretch()
                self.page_map[key] = page
                self.stack.addWidget(page)
        assert page is not None
        if device_id and isinstance(
            page,
            (DeviceControlPage, MonitoringPage, SpectrumMonitoringPage, SignalSourcePage, PowerSupplyPage),
        ):
            # Resolve a requested device before showEvent can start readback for the page.
            page.selector.select(device_id)
        self.stack.setCurrentWidget(page)

    def change_mode(self, *_args) -> None:
        try:
            self.controller.set_mode(self.mode.currentData())
        except DeviceError as exc:
            self.mode.blockSignals(True)
            self.mode.setCurrentIndex(self.mode.findData(self.controller.mode))
            self.mode.blockSignals(False)
            self._failed(str(exc))
        self.refresh()

    def _build_overview(self) -> None:
        root = QWidget()
        root.setObjectName("shellRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 24, 30, 24)
        layout.setSpacing(14)
        eyebrow = QLabel("设备管理 / 总览")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)
        toolbar = QHBoxLayout()
        heading = QLabel("设备总览")
        heading.setObjectName("pageTitle")
        toolbar.addWidget(heading)
        toolbar.addStretch()
        self.mode = WorkspaceComboBox()
        self.mode.addItem("电子学模式", "electronic")
        self.mode.addItem("光学模式", "optical")
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.scan_button = QPushButton("扫描设备")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.clicked.connect(self.scan)
        self.cancel_button = QPushButton("取消扫描")
        self.cancel_button.clicked.connect(lambda: self.runtime.discovery.cancel())
        toolbar.addWidget(self.cancel_button)
        for text, action in (("添加资源", self.add_resource), ("环境诊断", self.diagnose)):
            button = QPushButton(text)
            button.clicked.connect(action)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        description = QLabel("按设备类型查看设备池、连接状态和功能槽位。")
        description.setObjectName("pageDescription")
        layout.addWidget(description)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("overviewNote")
        layout.addWidget(self.summary)
        self.card_grid = CategoryCardGrid()
        self.cards = {
            category: CategoryCard(category, label, CATEGORY_DESCRIPTIONS[category], self.card_grid.widget())
            for category, label in CATEGORIES.items()
        }
        for card in self.cards.values():
            card.hide()
            card.clicked.connect(self.open_category)
        layout.addWidget(self.card_grid, 1)
        trend = QFrame()
        trend.setObjectName("overviewTrend")
        trend_layout = QHBoxLayout(trend)
        trend_text = QVBoxLayout()
        trend_text.setSpacing(2)
        trend_heading = QLabel("实时趋势预览")
        trend_heading.setObjectName("panelTitle")
        trend_caption = QLabel("选择设备后进入实时监测，查看波形、温度、电压、功率或频谱趋势。")
        trend_caption.setObjectName("pageDescription")
        trend_caption.setWordWrap(True)
        trend_text.addWidget(trend_heading)
        trend_text.addWidget(trend_caption)
        trend_layout.addLayout(trend_text, 1)
        monitor = QPushButton("进入监测")
        monitor.clicked.connect(lambda: self.navigate("实时监测"))
        trend_layout.addWidget(monitor)
        layout.addWidget(trend)
        self.device_pool = DevicePool(root)
        self.device_pool.cards = tuple(self.cards.values())
        self.device_pool.categoryRequested.connect(self.open_category)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.pool_title = QLabel("设备池")
        self.pool_title.setObjectName("poolTitle")
        titles.addWidget(self.pool_title)
        self.pool_count = QLabel()
        self.pool_count.setObjectName("poolCaption")
        titles.addWidget(self.pool_count)
        self.device_pool.heading.addLayout(titles, 1)
        self.pool_close = QPushButton("×")
        self.pool_close.setObjectName("poolClose")
        self.pool_close.setAccessibleName("关闭设备池")
        self.pool_close.setToolTip("关闭设备池 · Esc")
        self.pool_close.setFixedSize(32, 32)
        self.pool_close.clicked.connect(self.device_pool.hide)
        self.device_pool.heading.addWidget(self.pool_close, 0, Qt.AlignmentFlag.AlignTop)
        self.presence_filter = WorkspaceComboBox()
        for title, value in (("当前设备", "current"), ("历史与已配置", "history"), ("全部设备", "all")):
            self.presence_filter.addItem(title, value)
        self.presence_filter.currentIndexChanged.connect(self.select_category)
        self.category_filter = WorkspaceComboBox()
        self.category_filter.addItem("全部类别", "")
        for category, title in CATEGORIES.items():
            self.category_filter.addItem(title, category)
        self.category_filter.currentIndexChanged.connect(self.select_category)
        self.filter = WorkspaceComboBox()
        for title, value in (
            ("全部状态", "all"),
            ("已连接", "connected"),
            ("未连接", "disconnected"),
            ("忙碌 / 采集中", "busy"),
            ("未知 / 待补信息", "unknown"),
            ("异常", "error"),
            ("有适配器", "adapter"),
            ("已识别", "identified"),
        ):
            self.filter.addItem(title, value)
        self.filter.currentIndexChanged.connect(self.refresh)
        for combo in (self.presence_filter, self.category_filter, self.filter):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.presence_filter.setAccessibleName("设备范围")
        self.category_filter.setAccessibleName("设备类别")
        self.filter.setAccessibleName("设备状态")
        self.pool_list = QFrame()
        self.pool_list.setObjectName("poolListPanel")
        list_layout = QVBoxLayout(self.pool_list)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        list_heading = QLabel("设备列表")
        list_heading.setObjectName("poolGroupTitle")
        list_layout.addWidget(list_heading)
        self.tree = QTreeWidget()
        self.tree.setObjectName("poolDeviceList")
        self.tree.viewport().setObjectName("poolListViewport")
        self.tree.setAccessibleName("设备列表")
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["设备 / 型号", "类别", "VISA / 连接地址", "状态", "功能槽位", "操作"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setStretchLastSection(False)
        for column, width in enumerate((195, 90, 300, 95, 120, 110)):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.tree.setColumnWidth(column, width)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # The delegate presents name, model and state in one selectable device card.
        for column in (1, 2, 3, 4, 5):
            self.tree.setColumnHidden(column, True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setHeaderHidden(True)
        self.tree.setItemDelegate(DeviceRowDelegate(self.tree))
        self.tree.setMouseTracking(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setVerticalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        self.tree.setMinimumSize(0, 0)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        list_layout.addWidget(self.tree, 1)
        self.pool_list.setFocusProxy(self.tree)
        self.pool_empty = QLabel("暂无符合条件的设备\n可切换范围查看历史记录")
        self.pool_empty.setObjectName("poolEmpty")
        self.pool_empty.setWordWrap(True)
        self.pool_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        list_layout.addWidget(self.pool_empty, 1)
        self.details = DeviceDetails()
        self.action_buttons: dict[str, QPushButton | QAction] = {}
        self.pool_more = QPushButton("更多  ···")
        self.pool_more.setObjectName("poolMore")
        self.pool_more.setProperty("poolAction", True)
        self.pool_more.setFixedHeight(36)
        self.pool_menu = WorkspaceMenu(self.pool_more)
        self.pool_menu.setMinimumWidth(196)
        self.pool_more.setMenu(self.pool_menu)
        for key, title in (
            ("connect", "连接"),
            ("disconnect", "断开"),
            ("reconnect", "重连"),
            ("identify", "刷新身份"),
            ("status", "读取状态"),
            ("capabilities", "查看能力"),
            ("stop", "停止采集任务"),
            ("rename", "修改名称"),
            ("slot", "功能槽位"),
            ("resource", "选择主资源"),
            ("diagnostic", "设备诊断"),
            ("manage", "设备控制"),
            ("monitor", "实时监测"),
        ):
            control: QPushButton | QAction
            if key in {"connect", "disconnect", "stop", "slot", "manage", "monitor"}:
                control = QPushButton(title)
                control.setProperty("poolAction", True)
                control.setFixedHeight(36)
                control.clicked.connect(lambda _checked=False, key=key: self.action(key))
            else:
                control = self.pool_menu.addAction(title)
                control.triggered.connect(lambda _checked=False, key=key: self.action(key))
                if key in {"status", "resource"}:
                    self.pool_menu.addSeparator()
            self.action_buttons[key] = control
        main_actions = []
        for key in ("connect", "disconnect", "slot", "manage", "monitor"):
            action_button = self.action_buttons[key]
            assert isinstance(action_button, QPushButton)
            main_actions.append(action_button)
        self.action_buttons["connect"].setObjectName("primaryButton")
        self.action_buttons["stop"].setObjectName("poolStop")
        self.device_pool.set_actions((*main_actions, self.pool_more))
        hint = QLabel("断开软件连接不会关闭仪器物理输出。")
        hint.setObjectName("poolCaption")
        hint.setWordWrap(True)
        hint_row = QHBoxLayout()
        hint_row.addWidget(hint, 1)
        stop_button = self.action_buttons["stop"]
        assert isinstance(stop_button, QPushButton)
        hint_row.addWidget(stop_button)
        self.device_pool.footer.addLayout(hint_row)
        self.device_pool.set_content(
            self.pool_list, self.details, (self.presence_filter, self.category_filter, self.filter)
        )
        self.device_pool.hide()
        self.message = QLabel(
            ("真实设备" if self.runtime.hardware else "模拟演示") + " · 扫描不会自动连接或恢复输出"
        )
        self.message.setWordWrap(True)
        self.message.setObjectName("commandStatusBar")
        layout.addWidget(self.message)
        self.setCentralWidget(root)

    def scan(self) -> None:
        try:
            self.bridge.watch(self.runtime.discovery.scan(), "scan")
            self.refresh()
        except Exception as exc:
            self._failed(str(exc))

    def select_category(self, *_args) -> None:
        self.active_category = self.category_filter.currentData() or ""
        self.refresh()
        candidates = [row for row in self.rows.values() if not row.isHidden()]
        if candidates:
            self.tree.setCurrentItem(candidates[0])
        else:
            self.tree.clearSelection()
            self.selected_id = ""
            self.refresh_details()

    def open_category(self, category: str) -> None:
        self.category_filter.setCurrentIndex(self.category_filter.findData(category))
        self.filter.setCurrentIndex(0)
        self.select_category()
        self.show_device_pool(self.cards.get(category))

    def show_device_pool(self, trigger: QWidget | None = None) -> None:
        if self.stack.currentWidget() is not self.overview_page:
            return
        self.device_pool.present(trigger)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "nav_buttons"):
            self.update_sidebar()
        if hasattr(self, "device_pool") and not self.device_pool.isHidden():
            self.device_pool.position_in_overview()

    def refresh(self) -> None:
        if self.closing:
            return
        if hasattr(self, "mode_badge"):
            self.mode_badge.setText("● 电子学模式" if self.controller.mode == "electronic" else "● 光学模式")
            self.clock_label.setText(QDateTime.currentDateTime().toString("HH:mm:ss"))
        self.records = {item["id"]: item for item in self.runtime.inventory.all()}
        self.states = {item["stableDeviceId"]: item for item in self.runtime.sessions.snapshots()}
        current_records = {
            key: record for key, record in self.records.items() if is_current(record, self.states.get(key))
        }
        progress = self.runtime.discovery.progress
        running = self.runtime.discovery.is_scanning
        self.scan_button.setEnabled(not running)
        self.scan_button.setText("扫描中…" if running else "扫描设备")
        self.cancel_button.setEnabled(running)
        self.cancel_button.setVisible(running)
        connected = sum(
            s["connectionState"] in {"connected", "busy", "degraded"} for s in self.states.values()
        )
        identified = sum(bool(item["identity"]) for item in current_records.values())
        errors = sum(
            bool(item.get("lastError") or self.states.get(item["id"], {}).get("lastError"))
            or self.states.get(item["id"], {}).get("connectionState") in {"error", "degraded"}
            for item in current_records.values()
        )
        self.summary.setText(
            ("真实设备" if self.runtime.hardware else "模拟演示")
            + f" · 发现 {len(current_records)} 台 · 在线 {connected} 台 · 异常 {errors} 台 · 已识别 {identified} 台"
            + f" · 历史与已配置 {len(self.records) - len(current_records)} 台"
        )
        self.summary.setToolTip(
            f"最近扫描：{progress.get('finishedAt', '尚未扫描')}；本次结果 {len(progress.get('items', []))}"
        )
        current_filter = self.filter.currentData()
        presence_scope = self.presence_filter.currentData()
        for removed_id in set(self.rows) - self.records.keys():
            item = self.rows.pop(removed_id)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        visible_cards = []
        for category in CATEGORIES:
            records = [item for item in current_records.values() if category_for(item) == category]
            history_count = sum(category_for(item) == category for item in self.records.values()) - len(
                records
            )
            live = sum(
                self.states.get(item["id"], {}).get("connectionState") in {"connected", "busy", "degraded"}
                for item in records
            )
            known = sum(bool(item["identity"]) for item in records)
            bound = sum(bool(item.get("slots", {}).get(self.mode.currentData())) for item in records)
            abnormal = sum(
                bool(item.get("lastError"))
                or self.states.get(item["id"], {}).get("connectionState") in {"error", "degraded"}
                for item in records
            )
            index = self.category_filter.findData(category)
            category_text = f"{CATEGORIES[category]}  {live}/{len(records)}"
            if self.category_filter.itemText(index) != category_text:
                self.category_filter.setItemText(index, category_text)
            tooltip = (
                f"发现 {len(records)} · 识别 {known} · 连接 {live} · 异常 {abnormal} · 绑定 {bound}"
                f" · 历史与已配置 {history_count}"
            )
            if self.category_filter.itemData(index, Qt.ItemDataRole.ToolTipRole) != tooltip:
                self.category_filter.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)
            self.cards[category].update_counts(len(records), live, abnormal, bound)
            if (
                category in PRIMARY_CATEGORIES
                or records
                or history_count
                or (category == "optical_power_meter" and self.controller.mode == "optical")
            ):
                visible_cards.append(self.cards[category])
        self.card_grid.set_cards(tuple(visible_cards))
        for device_id, record in self.records.items():
            if device_id not in self.rows:
                row = QTreeWidgetItem(self.tree)
                row.setData(0, Qt.ItemDataRole.UserRole, device_id)
                self.rows[device_id] = row
            row = self.rows[device_id]
            state = self.states.get(device_id, {})
            connection = display_connection_state(state)
            name = record["displayName"] or record["identity"].get("model") or record["config"]["resource"]
            status_labels = {
                "connected": "已连接",
                "disconnected": "未连接",
                "busy": "忙碌",
                "connecting": "连接中",
                "degraded": "异常",
                "error": "异常",
            }
            colors = {
                "connected": "#8de3a3",
                "busy": "#efc66e",
                "connecting": "#efc66e",
                "degraded": "#ee7474",
                "error": "#ee7474",
            }
            texts = [
                name
                + "\n"
                + " · ".join(
                    value
                    for value in (
                        record["identity"].get("manufacturer") or "身份待确认",
                        record["identity"].get("model", ""),
                    )
                    if value
                ),
                CATEGORY_LABELS.get(instrument_category(record), "未分类"),
                record["config"]["resource"],
                "● "
                + (
                    "探测失败"
                    if connection == "disconnected" and record.get("presenceState") == "unreachable"
                    else status_labels.get(connection, connection)
                    if connection != "disconnected" or device_id in current_records
                    else presence_label(record, state)
                ),
                slot_label(record.get("slots", {}).get(self.mode.currentData(), "")),
            ]
            for index, text in enumerate(texts):
                if row.text(index) != text:
                    row.setText(index, text)
                    row.setToolTip(index, text)
            fallback_color = "#ee7474" if record.get("presenceState") == "unreachable" else "#8da19e"
            row.setForeground(3, QColor(colors.get(connection, fallback_color)))
            task = display_task(state)
            row.setToolTip(3, texts[3] + (" / " + task if task else ""))
            row.setToolTip(0, texts[0] + "\n" + record["identity"].get("serial", ""))
            show = not self.active_category or category_for(record) == self.active_category
            if presence_scope == "current":
                show &= device_id in current_records
            elif presence_scope == "history":
                show &= device_id not in current_records
            if current_filter == "connected":
                show &= connection in {"connected", "busy", "degraded"}
            elif current_filter in {"disconnected", "busy"}:
                show &= connection == current_filter
            elif current_filter == "unknown":
                show &= not bool(record["identity"])
            elif current_filter == "error":
                show &= bool(record.get("lastError") or state.get("lastError"))
            elif current_filter == "adapter":
                show &= bool(record["adapterId"])
            elif current_filter == "identified":
                show &= bool(record["identity"])
            row.setHidden(not show)
        visible_count = sum(not row.isHidden() for row in self.rows.values())
        self.pool_title.setText(CATEGORIES.get(self.active_category, "全部类别") + " · 设备池")
        self.pool_count.setText(f"{self.presence_filter.currentText()} · {visible_count} 台符合筛选")
        self.tree.setVisible(bool(visible_count))
        self.pool_empty.setVisible(not visible_count)
        if self.selected_id and (self.selected_id not in self.rows or self.rows[self.selected_id].isHidden()):
            self.tree.clearSelection()
            self.selected_id = ""
        self.refresh_details()

    def selection_changed(self) -> None:
        selected = self.tree.selectedItems()
        if selected:
            self.selected_id = selected[0].data(0, Qt.ItemDataRole.UserRole)
        self.refresh_details()

    def open_details(self, device_id: str) -> None:
        self.tree.setCurrentItem(self.rows[device_id])
        self.selected_id = device_id
        self.show_device_pool()
        self.refresh_details()

    def refresh_details(self) -> None:
        record = self.records.get(self.selected_id)
        state = self.states.get(self.selected_id, {})
        busy = bool(state.get("pending")) or state.get("connectionState") in {"busy", "connecting"}
        scanning = self.runtime.discovery.is_scanning
        for key, button in self.action_buttons.items():
            if key == "manage":
                button.setText("设备控制")
                button.setVisible(not bool(record and instrument_category(record) == "spectrum_analyzer"))
            enabled = bool(record)
            if key == "connect":
                enabled &= not bool(state) and bool(
                    record and record["adapterId"] and record["discoveryState"] != "conflict"
                )
            elif key in {"disconnect", "reconnect"}:
                # Teardown queues behind a single background read. The session
                # manager reserves closing atomically and still rejects controls.
                drainable = (
                    state.get("backgroundRead")
                    and state.get("pending") == 1
                    and state.get("connectionState") != "connecting"
                    and not any(
                        state.get(flag)
                        for flag in ("controlPending", "controlGroup", "monitoring", "recording")
                    )
                )
                enabled &= bool(state) and (not busy or bool(drainable))
            elif key in {"identify", "status"}:
                enabled &= state.get("connectionState") == "connected" and not busy
            elif key == "capabilities":
                enabled &= bool(record and record["adapterId"])
            elif key == "slot":
                enabled &= not busy and bool(
                    record and applicable(record, self.mode.currentData()) and record["adapterId"]
                )
            elif key == "resource":
                enabled &= not bool(state)
            elif key == "stop":
                enabled &= bool(
                    state.get("monitoring") or state.get("activeTask") == "continuous_acquisition"
                )
            elif key in {"manage", "monitor"}:
                enabled &= bool(
                    record
                    and instrument_category(record) in CATEGORY_LABELS
                    and state.get("connectionState") in {"connected", "busy", "degraded"}
                )
            if key in {"connect", "reconnect", "resource"}:
                enabled &= not scanning
            button.setEnabled(enabled)
            button.setToolTip(
                "正在扫描并核验设备，完成后可连接"
                if scanning and key in {"connect", "reconnect", "resource"}
                else "请先连接设备"
                if key in {"manage", "monitor"} and not enabled
                else "设备忙碌，请先停止任务"
                if busy and not background_read_only(state)
                else ""
            )
        self.action_buttons["connect"].setVisible(not bool(state))
        self.action_buttons["disconnect"].setVisible(bool(state))
        self.action_buttons["stop"].setVisible(self.action_buttons["stop"].isEnabled())
        self.device_pool.reflow_actions()
        self.pool_more.setEnabled(bool(record))
        if record:
            identity, config = record["identity"], record["config"]
            connection = display_connection_state(state)
            if record["discoveryState"] == "conflict":
                identity_state = "身份冲突"
            elif identity:
                identity_state = "已识别" if is_current(record, state) else "历史身份已保留"
            else:
                identity_state = "尚未识别"
            seen = QDateTime.fromString(record.get("lastSeenAt") or "", Qt.DateFormat.ISODateWithMs)
            probe = record.get("probeReason") or ""
            driver = record.get("driverStatus") or ""
            if driver == "error" and record.get("probeErrorCode") != "driver_missing":
                # Historical records used "error" for every probe failure.
                driver = "unconfirmed"
            fields = {
                **{
                    key: identity.get(key) or "未提供"
                    for key in ("manufacturer", "model", "serial", "firmware")
                },
                "confidence": "模拟身份"
                if identity.get("confidence") == "simulated"
                else identity.get("confidence") or "仅资源描述",
                "acceptance": "待实机验证",
                "connection": {
                    "connected": "已连接",
                    "disconnected": "未连接",
                    "busy": "忙碌 / 采集中",
                    "connecting": "连接中",
                    "degraded": "连接异常",
                    "error": "连接错误",
                }.get(connection, connection),
                "task": display_task(state) or "空闲",
                "driver": {
                    "enumerated": "已枚举",
                    "error": "运行库不可用",
                    "unconfirmed": "待核对（探测失败）",
                }.get(driver, driver or "待检查"),
                "adapter": record["adapterId"] or "待补协议",
                "resource": f"{config['transport'].upper()}  ·  {config['resource']}",
                "aliases": "\n".join(r for r in record["resources"] if r != config["resource"]) or "无",
                "mode": self.mode.currentText(),
                "slot": slot_label(record.get("slots", {}).get(self.mode.currentData(), "")),
                "presence": presence_label(record, state),
                "identity_state": identity_state,
                "seen": seen.toLocalTime().toString("yyyy-MM-dd HH:mm:ss")
                if seen.isValid()
                else record.get("lastSeenAt") or "尚无发现记录",
                "probe": {
                    "identified": "身份探测成功",
                    "probe_failed": "身份探测失败",
                    "pending_probe": "等待探测",
                    "metadata_only": "仅有资源描述",
                    "cached_active_session": "复用活动会话",
                    "cached_session_unconfirmed": "会话尚未确认",
                    "deferred_possible_alias_of_active_device": "可能为活动设备别名，延后探测",
                    "sdk_identity_requires_explicit_connection": "需手动连接后确认身份",
                    "not_seen": "本次未发现",
                    "scan_cancelled": "扫描已取消",
                    "enumeration_unconfirmed": "枚举尚未确认",
                    "address_reassigned": "地址已分配给其他设备",
                }.get(probe, probe or "尚未确认"),
                "error": str(state.get("lastError") or record.get("lastError") or "无"),
                "readback": json.dumps(state["lastStatus"], ensure_ascii=False, indent=2)
                if state.get("lastStatus")
                else "暂无状态读回",
            }
            self.details.set_fields(
                record["displayName"] or identity.get("model") or "待识别设备",
                f"{presence_label(record, state)}  ·  {identity.get('manufacturer') or '身份待确认'}",
                fields,
            )
        else:
            self.details.set_fields(
                "选择一台设备", "从列表选择设备；未接入的设备可在“历史与已配置”中查看。", {}
            )

    def action(self, key: str) -> None:
        device_id = self.selected_id
        try:
            if key == "connect":
                self.bridge.watch(self.runtime.connect(device_id), "connect")
            elif key in {"disconnect", "reconnect"}:

                def change():
                    self.runtime.sessions.disconnect(device_id, drain_reads=True)
                    if key == "reconnect":
                        return self.runtime.connect(device_id).result()

                self.bridge.watch(self.bridge.worker.submit(change), key)
            elif key in {"identify", "status"}:
                self.bridge.watch(self.runtime.sessions.submit(device_id, key), key)
            elif key == "capabilities":
                snapshot = self.runtime.sessions.capability_snapshot(device_id)
                self._completed(
                    "capabilities", snapshot or describe(config_from_record(self.records[device_id]))
                )
            elif key == "stop":
                self.runtime.sessions.stop_acquisition(device_id)
                self.controller.watch(self.runtime.monitoring.stop(device_id))
            elif key in {"manage", "monitor"}:
                category = instrument_category(self.records[device_id])
                if category in CATEGORY_LABELS:
                    self.navigate("设备管理" if key == "manage" else "实时监测", category, device_id)
            elif key == "diagnostic":
                self._completed(
                    "diagnostics",
                    {"device": self.records[device_id], "session": self.states.get(device_id, {})},
                )
            elif key == "resource":
                value, accepted = choose_item(
                    self,
                    "主连接资源",
                    "选择已发现的资源",
                    self.records[device_id]["resources"],
                )
                if accepted:
                    self.runtime.select_resource(device_id, value)
            elif key == "rename":
                value, accepted = QInputDialog.getText(self, "设备显示名", "名称")
                if accepted:
                    self.runtime.inventory.edit(device_id, display_name=value)
            elif key == "slot":
                slots = [
                    "",
                    instrument_category(self.records[device_id]) + ":1",
                    instrument_category(self.records[device_id]) + ":2",
                ]
                labels = ["解除绑定", *(slot_label(slot) for slot in slots[1:])]
                value, accepted = choose_item(
                    self,
                    "功能槽位",
                    "按能力选择槽位（空项解除绑定）",
                    labels,
                )
                if accepted:
                    self.runtime.bind_slot(device_id, slots[labels.index(value)], self.mode.currentData())
        except Exception as exc:
            self._failed(str(exc))
        self.refresh()

    def _completed(self, label: str, result: Any) -> None:
        self.message.setText(f"{label} 完成")
        if label in {"capabilities", "diagnostics"}:
            dialog = QDialog(self)
            dialog.setWindowTitle("能力与诊断")
            dialog.resize(760, 600)
            layout = QVBoxLayout(dialog)
            text = QTextBrowser()
            text.setPlainText(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
            layout.addWidget(text)
            dialog.exec()
        elif label == "scan":
            failures = {name: data for name, data in result["providers"].items() if data["state"] == "error"}
            self.message.setText(
                "扫描完成；" + (json.dumps(failures, ensure_ascii=False) if failures else "已有会话保持不变")
            )
        self.refresh()

    def _failed(self, message: str) -> None:
        self.message.setText(message)
        if self.runtime.closed:
            self.closing = True
            self.bridge.worker.shutdown(wait=False)
            self.close()

    def diagnose(self) -> None:
        self.bridge.watch(self.bridge.worker.submit(self.runtime.diagnostics), "diagnostics")

    def add_resource(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("添加已确认的设备资源")
        form = QFormLayout(dialog)
        resource, sdk = QLineEdit(), QLineEdit()
        serial = QLineEdit()
        baud, timeout = QSpinBox(), QDoubleSpinBox()
        baud.setRange(300, 3000000)
        baud.setValue(9600)
        timeout.setRange(0.1, 60)
        timeout.setValue(3)
        confirmed = QCheckBox("已核对 53100A 身份及 TSERVE 服务独占权")
        adapter, transport = WorkspaceComboBox(), WorkspaceComboBox()
        adapter.addItems(["", *PROFILES, *SDK_ADAPTERS])
        transport.addItems(["visa", "serial", "tcp", "vicp", "tlpmx", "pm100d", "pico", "tserve"])
        resource.setPlaceholderText("优先 PyVISA：TCPIP0::地址::inst0::INSTR / USB… / GPIB… / ASRL…")
        transport.setToolTip("支持 VISA 的设备优先 visa；专用 SDK/TSERVE 仅用于相应厂商协议。")
        form.addRow("资源地址", resource)
        form.addRow("准确型号适配器", adapter)
        form.addRow("传输方式", transport)
        form.addRow("SDK DLL 绝对路径", sdk)
        form.addRow("预期序列号（可选）", serial)
        form.addRow("串口波特率", baud)
        form.addRow(QLabel("可输入范围：300 ～ 3000000 baud（整数） · 软件通信限制"))
        form.addRow("通信超时（秒）", timeout)
        form.addRow(QLabel("可输入范围：0.1 ～ 60 s · 软件通信限制"))
        form.addRow(confirmed)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec():
            try:
                config = ConnectionConfig(
                    resource.text().strip(),
                    adapter.currentText(),
                    transport.currentText(),
                    sdk_path=sdk.text(),
                    expected_serial=serial.text().strip(),
                    baud_rate=baud.value(),
                    timeout=timeout.value(),
                    identity_confirmed=confirmed.isChecked(),
                )
                self.selected_id = self.runtime.add(config)
                self.refresh()
            except (DeviceError, ValueError) as exc:
                self._failed(str(exc))

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.closing:
            event.accept()
            return
        event.ignore()
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.timer.stop()
        self.controller.close()
        self.message.setText("正在停止扫描和采集并释放本软件会话…")
        central = self.centralWidget()
        if central:
            central.setEnabled(False)

        future = self.bridge.worker.submit(self.runtime.close)

        def finish(label: str, result: Any):
            if label == "shutdown":
                self.closing = True
                self.bridge.worker.shutdown(wait=False)
                self.close()

        self.bridge.completed.connect(finish)
        self.bridge.watch(future, "shutdown")
