from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from PySide6.QtCore import QObject, QRect, QSize, QTimer, Signal
from PySide6.QtGui import QAction, QScreen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.controller import (
    WorkspaceController,
)
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.widgets.category_cards import CategoryCard, CategoryCardGrid
from lab_control.ui.widgets.device_pool import DeviceDetails, DevicePool
from lab_control.ui.widgets.popup_surface import WorkspaceMenu
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox
from lab_control.ui.workspace.actions import WorkspaceActions
from lab_control.ui.workspace.categories import (
    CATEGORIES as CATEGORIES,
)
from lab_control.ui.workspace.categories import (
    CATEGORY_DESCRIPTIONS as CATEGORY_DESCRIPTIONS,
)
from lab_control.ui.workspace.categories import (
    applicable as applicable,
)
from lab_control.ui.workspace.categories import (
    category_for as category_for,
)
from lab_control.ui.workspace.dialogs import ConnectionDialogs
from lab_control.ui.workspace.navigation import WorkspaceNavigation
from lab_control.ui.workspace.overview import WorkspaceOverview
from lab_control.ui.workspace.shutdown import WindowShutdown


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
    # Public widget references stay compatible; WorkspaceOverview constructs them.
    scan_button: QPushButton
    cancel_button: QPushButton
    connect_all_button: QPushButton
    stop_connect_button: QPushButton
    connection_results_button: QPushButton
    address_list_button: QPushButton
    pool_close: QPushButton
    pool_more: QPushButton
    summary: QLabel
    connection_summary: QLabel
    pool_title: QLabel
    pool_count: QLabel
    pool_empty: QLabel
    message: QLabel
    presence_filter: WorkspaceComboBox
    category_filter: WorkspaceComboBox
    filter: WorkspaceComboBox
    card_grid: CategoryCardGrid
    cards: dict[str, CategoryCard]
    device_pool: DevicePool
    pool_list: QFrame
    tree: QTreeWidget
    details: DeviceDetails
    action_buttons: dict[str, QPushButton | QAction]
    pool_menu: WorkspaceMenu

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
        self._submenu_signature: tuple[str, tuple[str, ...]] = ("", ())
        self._static_address_dialog: QDialog | None = None
        self._static_address_status: QLabel | None = None
        self._refresh_static_address_dialog: Callable[[], None] | None = None
        self.setWindowTitle("TF LabControl · 分类卡片 · " + ("真实设备" if runtime.hardware else "模拟演示"))
        self.resize(1440, 900)
        self.setMinimumSize(1280, 760)
        self._navigation = WorkspaceNavigation(self)
        self._overview = WorkspaceOverview(self)
        self._actions = WorkspaceActions(self)
        self._dialogs = ConnectionDialogs(self)
        self._shutdown = WindowShutdown(self)
        self._build()
        self.fit_to_screen()
        self.controller.message.connect(self._failed)
        self.controller.navigate.connect(self.navigate)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._overview.refresh)
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
        for module in ("设备管理", "实时监测", "数据处理", "操作日志", "系统设置"):
            button = QPushButton(module)
            button.setObjectName("workspaceNav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=module: self.navigate(value, "", ""))
            self.nav_buttons[module] = button
            side.addWidget(button)
        side.addStretch()
        badges = QHBoxLayout()
        badges.setSpacing(6)
        self.runtime_badge = QLabel("● 真实设备" if self.runtime.hardware else "● 演示版")
        self.runtime_badge.setObjectName("runtimeBadge")
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
        self.scan_button.setProperty("buttonSize", "compact")
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
        self.settings_page = QScrollArea()
        self.settings_page.setWidgetResizable(True)
        self.settings_page.setFrameShape(QFrame.Shape.NoFrame)
        settings_body = QWidget()
        self.settings_page.setWidget(settings_body)
        settings = QVBoxLayout(settings_body)
        heading = QLabel("软件设置")
        heading.setObjectName("pageTitle")
        settings.addWidget(heading)
        from lab_control.ui.pages.storage_settings import StorageSettingsPanel

        self.storage_panel = StorageSettingsPanel(self.runtime.repository, settings_body)
        settings.addWidget(self.storage_panel)
        directory = QLabel("软件库存与日志目录：" + str(self.runtime.repository.root))
        directory.setWordWrap(True)
        settings.addWidget(directory)
        from lab_control.ui.pages.environment_repair import EnvironmentRepairPanel

        self.environment_panel = EnvironmentRepairPanel(self.runtime, settings_body)
        settings.addWidget(self.environment_panel, 1)
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
        self._navigation.focus_card(spec)

    def _connected_categories(
        self,
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> frozenset[str]:
        return self._navigation._connected_categories(records, states)

    def _submenu_categories(
        self,
        module: str,
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, ...]:
        return self._navigation._submenu_categories(module, records, states)

    def _rebuild_submenu(
        self,
        module: str,
        category: str = "",
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        return self._navigation._rebuild_submenu(module, category, records, states)

    def _sync_submenu(self) -> None:
        self._navigation._sync_submenu()

    def navigate(self, module: str, category: str = "", device_id: str = "") -> None:
        self._navigation.navigate(module, category, device_id)

    def show_page(self, *_args, device_id: str = "") -> None:
        self._navigation.show_page(*_args, device_id=device_id)

    def _build_overview(self) -> None:
        self._overview._build_overview()

    def prepare_first_run(self) -> None:
        self._actions.prepare_first_run()

    def scan(self) -> None:
        self._actions.scan()

    def select_category(self, *_args) -> None:
        self._overview.select_category(*_args)

    def open_category(self, category: str) -> None:
        self._overview.open_category(category)

    def show_device_pool(self, trigger: QWidget | None = None) -> None:
        self._overview.show_device_pool(trigger)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "nav_buttons"):
            self.update_sidebar()
        if hasattr(self, "device_pool") and not self.device_pool.isHidden():
            self.device_pool.position_in_overview()

    def refresh(self) -> None:
        self.controller.refresh_snapshot()
        self._overview.refresh()

    def selection_changed(self) -> None:
        self._overview.selection_changed()

    def open_details(self, device_id: str) -> None:
        self._overview.open_details(device_id)

    def refresh_details(self) -> None:
        self._overview.refresh_details()

    def action(self, key: str) -> None:
        self._actions.action(key)

    def choose_slot(self, device_id: str) -> None:
        self._actions.choose_slot(device_id)

    def connect_all(self) -> None:
        self._actions.connect_all()

    @staticmethod
    def connection_result_text(progress: dict[str, Any]) -> str:
        items = progress.get("items", [])
        counts = {
            status: sum(item["status"] == status for item in items)
            for status in ("connected", "already_connected", "failed", "skipped")
        }
        title = {"cancelled": "连接已停止", "error": "批量连接中断"}.get(progress["state"], "连接完成")
        return (
            f"{title} · 成功 {counts['connected']} · 已连接 {counts['already_connected']}"
            f" · 失败 {counts['failed']} · 跳过 {counts['skipped']}"
        )

    def show_connection_results(self) -> None:
        self._actions.show_connection_results()

    def _completed(self, label: str, result: Any) -> None:
        self._actions._completed(label, result)

    def _failed(self, message: str) -> None:
        self._actions._failed(message)

    def diagnose(self) -> None:
        self._actions.diagnose()

    def show_static_addresses(self) -> None:
        self._dialogs.show_static_addresses()

    def configure_adapter(self, device_id: str) -> None:
        self._dialogs.configure_adapter(device_id)

    def closeEvent(self, event) -> None:
        self._shutdown.closeEvent(event)
