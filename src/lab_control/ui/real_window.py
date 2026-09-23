from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
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
from lab_control.domain.instruments import DeviceError, jsonable
from lab_control.instruments.registry import PROFILES, SDK_ADAPTERS, describe
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.page import DeviceControlPage
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.pages.monitoring.page import MonitoringPage

CATEGORIES = {
    "signal_generator": "信号源",
    "power_supply": "电源",
    "oscilloscope": "示波器",
    "spectrum_analyzer": "频谱仪",
    "optical_power_meter": "光学测量",
    "measurement_analyzers": "万用表 / 网络 / 相噪",
    "temperature_load_drivers": "温控 / 负载 / 泵浦",
    "unknown": "未分类 / 未知设备",
}


def category_for(record: dict[str, Any]) -> str:
    category = instrument_category(record)
    if category in {"multimeter", "network_analyzer", "phase_noise_analyzer"}:
        return "measurement_analyzers"
    if category in {"temperature_controller", "electronic_load", "pump_driver"}:
        return "temperature_load_drivers"
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
        self.setStyleSheet(
            Path(__file__).with_name("styles").joinpath("real.qss").read_text(encoding="utf-8")
        )
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
        self.last_details = ""
        self.closing = False
        self.setWindowTitle("TF LabControl · " + ("真实设备" if runtime.hardware else "模拟演示"))
        self.resize(1450, 900)
        self._build()
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
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("workspaceSidebar")
        self.sidebar.setFixedWidth(196)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(14, 24, 14, 18)
        self.brand = QLabel("TF LABCONTROL")
        self.brand.setObjectName("controlTitle")
        side.addWidget(self.brand)
        side.addSpacing(26)
        self.nav_buttons = {}
        for module in ("设备管理", "数据采集", "实时监测", "数据处理", "操作日志", "系统设置"):
            button = QPushButton(module)
            button.setObjectName("workspaceNav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=module: self.navigate(value, "", ""))
            self.nav_buttons[module] = button
            side.addWidget(button)
        side.addStretch()
        badge = QLabel("真实设备" if self.runtime.hardware else "MOCK · 模拟演示")
        badge.setWordWrap(True)
        side.addWidget(badge)
        collapse = QPushButton("收起 / 展开")
        collapse.clicked.connect(self.toggle_sidebar)
        side.addWidget(collapse)
        row.addWidget(self.sidebar)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 14, 12, 8)
        top = QHBoxLayout()
        self.section_title = QLabel("设备管理")
        self.section_title.setObjectName("controlTitle")
        top.addWidget(self.section_title)
        self.submenu = QComboBox()
        self.submenu.setMinimumWidth(180)
        self.submenu.currentIndexChanged.connect(self.show_page)
        top.addWidget(self.submenu)
        top.addStretch()
        self.mode_badge = QLabel()
        top.addWidget(self.mode_badge)
        content_layout.addLayout(top)
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
        collapsed = self.sidebar.width() > 100
        self.sidebar.setFixedWidth(76 if collapsed else 196)
        self.brand.setText("TF" if collapsed else "TF LABCONTROL")
        for label, button in self.nav_buttons.items():
            button.setText(label[:2] if collapsed else label)
            button.setToolTip(label)

    def focus_card(self, spec: dict) -> None:
        page = self.stack.currentWidget()
        if isinstance(page, MonitoringPage):
            page.focus_card(spec)

    def navigate(self, module: str, category: str = "", device_id: str = "") -> None:
        if module not in self.nav_buttons:
            return
        self.current_module = module
        for name, button in self.nav_buttons.items():
            button.setChecked(name == module)
        self.section_title.setText(module)
        self.submenu.blockSignals(True)
        self.submenu.clear()
        if module in {"设备管理", "实时监测"}:
            self.submenu.addItem("总览", "")
            for value, label in CATEGORY_LABELS.items():
                self.submenu.addItem(label, value)
            self.submenu.setCurrentIndex(max(0, self.submenu.findData(category)))
        else:
            self.submenu.addItem("软件设置" if module == "系统设置" else module, "")
        self.submenu.blockSignals(False)
        self.show_page()
        page = self.stack.currentWidget()
        if device_id and isinstance(page, (DeviceControlPage, MonitoringPage)):
            page.selector.select(device_id)

    def show_page(self, *_args) -> None:
        module, category = self.current_module, self.submenu.currentData() or ""
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
                    page = DeviceControlPage(self.controller, category)
                elif module == "实时监测":
                    page = MonitoringPage(self.controller, category, self.dashboard.add)
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
        layout.setContentsMargins(26, 20, 26, 20)
        toolbar = QHBoxLayout()
        heading = QLabel("设备管理 / 总览")
        heading.setObjectName("pageTitle")
        toolbar.addWidget(heading)
        toolbar.addStretch()
        self.mode = QComboBox()
        self.mode.addItem("电子学模式", "electronic")
        self.mode.addItem("光学模式", "optical")
        self.mode.currentIndexChanged.connect(self.change_mode)
        toolbar.addWidget(self.mode)
        self.scan_button = QPushButton("扫描设备")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.clicked.connect(self.scan)
        toolbar.addWidget(self.scan_button)
        self.cancel_button = QPushButton("取消扫描")
        self.cancel_button.clicked.connect(lambda: self.runtime.discovery.cancel())
        toolbar.addWidget(self.cancel_button)
        for text, action in (("添加资源", self.add_resource), ("环境诊断", self.diagnose)):
            button = QPushButton(text)
            button.clicked.connect(action)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(0, 0, 14, 0)
        cards = QGridLayout()
        self.cards: dict[str, QPushButton] = {}
        for index, (category, title) in enumerate(CATEGORIES.items()):
            button = QPushButton(title)
            button.setObjectName("deviceCard")
            button.setMinimumHeight(118)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, category=category: self.select_category(category))
            self.cards[category] = button
            cards.addWidget(button, index // 3, index % 3)
        overview_layout.addLayout(cards)
        self.filter = QComboBox()
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
        overview_layout.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["设备", "发现状态", "连接 / 任务", "当前模式"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        overview_layout.addWidget(self.tree, 1)
        splitter.addWidget(overview)
        self.drawer = QFrame()
        self.drawer.setObjectName("deviceDrawer")
        drawer_layout = QVBoxLayout(self.drawer)
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(False)
        drawer_layout.addWidget(self.details, 1)
        self.action_buttons: dict[str, QPushButton] = {}
        grid = QGridLayout()
        for index, (key, title) in enumerate(
            (
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
                ("manage", "设备控制页"),
                ("monitor", "实时监测页"),
            )
        ):
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, key=key: self.action(key))
            self.action_buttons[key] = button
            grid.addWidget(button, index // 3, index % 3)
        drawer_layout.addLayout(grid)
        drawer_layout.addWidget(QLabel("断开软件连接不会关闭仪器物理输出。"))
        splitter.addWidget(self.drawer)
        splitter.setSizes([850, 550])
        layout.addWidget(splitter, 1)
        self.message = QLabel(
            ("真实设备" if self.runtime.hardware else "模拟演示") + " · 扫描不会自动连接或恢复输出"
        )
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.setCentralWidget(root)

    def scan(self) -> None:
        try:
            self.bridge.watch(self.runtime.discovery.scan(), "scan")
            self.refresh()
        except Exception as exc:
            self._failed(str(exc))

    def select_category(self, category: str) -> None:
        self.active_category = "" if self.active_category == category else category
        self.refresh()

    def refresh(self) -> None:
        if self.closing:
            return
        if hasattr(self, "mode_badge"):
            self.mode_badge.setText("电子学模式" if self.controller.mode == "electronic" else "光学模式")
        self.records = {item["id"]: item for item in self.runtime.inventory.all()}
        self.states = {item["stableDeviceId"]: item for item in self.runtime.sessions.snapshots()}
        progress = self.runtime.discovery.progress
        running = progress.get("state") == "running"
        self.scan_button.setEnabled(not running)
        self.scan_button.setText("扫描中…" if running else "扫描设备")
        self.cancel_button.setEnabled(running)
        connected = sum(
            s["connectionState"] in {"connected", "busy", "degraded"} for s in self.states.values()
        )
        identified = sum(bool(item["identity"]) for item in self.records.values())
        errors = sum(
            bool(item.get("lastError") or self.states.get(item["id"], {}).get("lastError"))
            or self.states.get(item["id"], {}).get("connectionState") in {"error", "degraded"}
            for item in self.records.values()
        )
        self.summary.setText(
            f"发现 {len(self.records)} · 已识别 {identified} · 已连接 {connected} · 未知 {len(self.records) - identified} · 异常 {errors}"
            f"  | 最近扫描：{progress.get('finishedAt', '未完成')}  | 本次结果 {len(progress.get('items', []))}"
        )
        current_filter = self.filter.currentData()
        for removed_id in set(self.rows) - self.records.keys():
            item = self.rows.pop(removed_id)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        for category, button in self.cards.items():
            records = [item for item in self.records.values() if category_for(item) == category]
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
            button.setText(
                f"{CATEGORIES[category]}\n发现 {len(records)} · 识别 {known} · 连接 {live}\n未连接 {len(records) - live} · 未知 {len(records) - known}\n异常 {abnormal} · 已绑定 {bound}"
            )
            button.setChecked(self.active_category == category)
        for device_id, record in self.records.items():
            if device_id not in self.rows:
                row = QTreeWidgetItem(self.tree)
                row.setData(0, Qt.ItemDataRole.UserRole, device_id)
                self.rows[device_id] = row
            row = self.rows[device_id]
            state = self.states.get(device_id, {})
            connection = state.get("connectionState", "disconnected")
            name = record["displayName"] or record["identity"].get("model") or record["config"]["resource"]
            texts = [
                name,
                record["discoveryState"],
                connection + (" / " + state["activeTask"] if state.get("activeTask") else ""),
                "适用" if applicable(record, self.mode.currentData()) else "不适用",
            ]
            for index, text in enumerate(texts):
                if row.text(index) != text:
                    row.setText(index, text)
            show = not self.active_category or category_for(record) == self.active_category
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
        self.refresh_details()

    def selection_changed(self) -> None:
        selected = self.tree.selectedItems()
        if selected:
            self.selected_id = selected[0].data(0, Qt.ItemDataRole.UserRole)
        self.refresh_details()

    def refresh_details(self) -> None:
        record = self.records.get(self.selected_id)
        state = self.states.get(self.selected_id, {})
        busy = bool(state.get("pending")) or state.get("connectionState") in {"busy", "connecting"}
        for key, button in self.action_buttons.items():
            enabled = bool(record)
            if key == "connect":
                enabled &= not bool(state) and bool(
                    record and record["adapterId"] and record["discoveryState"] != "conflict"
                )
            elif key in {"disconnect", "reconnect"}:
                enabled &= bool(state) and not busy
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
                enabled &= bool(record and instrument_category(record) in CATEGORY_LABELS)
            button.setEnabled(enabled)
            button.setToolTip("设备忙碌，请先停止任务" if busy else "")
        if record:
            identity, config = record["identity"], record["config"]
            lines = [
                record["displayName"] or identity.get("model") or "待识别设备",
                "",
                f"厂家：{identity.get('manufacturer') or '未识别'}",
                f"型号：{identity.get('model') or '待确认'}",
                f"序列号：{identity.get('serial') or '未提供'}",
                f"固件：{identity.get('firmware') or '未提供'}",
                f"身份可信度：{identity.get('confidence') or '仅资源描述'}",
                "",
                f"发现状态：{record['discoveryState']}",
                f"连接状态：{state.get('connectionState', 'disconnected')}",
                f"当前任务：{state.get('activeTask') or '空闲'}",
                f"驱动状态：{record.get('driverStatus', '待检查')}",
                f"适配器：{record['adapterId'] or '待补协议'}",
                "验收状态：待实机验证",
                "",
                f"主资源（{config['transport']}）：{config['resource']}",
                "备用资源：" + ("、".join(r for r in record["resources"] if r != config["resource"]) or "无"),
                f"当前槽位：{slot_label(record.get('slots', {}).get(self.mode.currentData(), ''))}",
                f"最近发现：{record.get('lastSeenAt', '')}",
                f"身份探测：{record.get('probeReason', '')}",
                "",
                "最近状态读回：",
                json.dumps(state.get("lastStatus", {}), ensure_ascii=False, indent=2),
                "",
                "最近错误：" + (state.get("lastError") or record.get("lastError") or "无"),
            ]
            text = "\n".join(lines)
        else:
            text = "选择类别和设备查看资源、身份、适配器、驱动及会话诊断。"
        if text != self.last_details:
            scroll = self.details.verticalScrollBar().value()
            self.details.setPlainText(text)
            self.details.verticalScrollBar().setValue(scroll)
            self.last_details = text

    def action(self, key: str) -> None:
        device_id = self.selected_id
        try:
            if key == "connect":
                self.bridge.watch(self.runtime.connect(device_id), "connect")
            elif key in {"disconnect", "reconnect"}:

                def change():
                    self.runtime.sessions.disconnect(device_id)
                    if key == "reconnect":
                        return self.runtime.connect(device_id).result()

                self.bridge.watch(self.bridge.worker.submit(change), key)
            elif key in {"identify", "status"}:
                self.bridge.watch(self.runtime.sessions.submit(device_id, key), key)
            elif key == "capabilities":
                self._completed("capabilities", describe(config_from_record(self.records[device_id])))
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
                value, accepted = QInputDialog.getItem(
                    self,
                    "主连接资源",
                    "选择已发现的资源",
                    self.records[device_id]["resources"],
                    editable=False,
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
                value, accepted = QInputDialog.getItem(
                    self,
                    "功能槽位",
                    "按能力选择槽位（空项解除绑定）",
                    labels,
                    editable=False,
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
        adapter, transport = QComboBox(), QComboBox()
        adapter.addItems(["", *PROFILES, *SDK_ADAPTERS])
        transport.addItems(["visa", "serial", "tcp", "vicp", "tlpmx", "pm100d", "pico", "tserve"])
        form.addRow("资源地址", resource)
        form.addRow("准确型号适配器", adapter)
        form.addRow("传输方式", transport)
        form.addRow("SDK DLL 绝对路径", sdk)
        form.addRow("预期序列号（可选）", serial)
        form.addRow("串口波特率", baud)
        form.addRow("通信超时（秒）", timeout)
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
