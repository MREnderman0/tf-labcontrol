from __future__ import annotations

import json
from typing import TYPE_CHECKING

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.bulk_connection import device_label
from lab_control.application.catalog import instrument_category
from lab_control.application.controls import CATEGORY_LABELS, slot_label
from lab_control.discovery.presence import is_current, presence_label
from lab_control.ui.controller import (
    background_read_only,
    display_connection_state,
    display_task,
)
from lab_control.ui.widgets.category_cards import CategoryCard, CategoryCardGrid
from lab_control.ui.widgets.device_pool import DeviceDetails, DevicePool, DeviceRowDelegate
from lab_control.ui.widgets.popup_surface import WorkspaceMenu
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox
from lab_control.ui.workspace.categories import (
    CATEGORIES,
    CATEGORY_DESCRIPTIONS,
    PRIMARY_CATEGORIES,
    applicable,
    category_for,
)

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class WorkspaceOverview:
    """Category cards, device pool and cached detail presentation."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window

    def _build_overview(self) -> None:
        window = self.window
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
        window.scan_button = QPushButton("扫描设备")
        window.scan_button.setObjectName("primaryButton")
        window.scan_button.clicked.connect(window.scan)
        window.cancel_button = QPushButton("取消扫描")
        window.cancel_button.clicked.connect(lambda: window.runtime.discovery.cancel())
        window.connect_all_button = QPushButton("一键连接")
        window.connect_all_button.setObjectName("primaryButton")
        window.connect_all_button.clicked.connect(window.connect_all)
        toolbar.addWidget(window.connect_all_button)
        window.stop_connect_button = QPushButton("停止连接")
        window.stop_connect_button.clicked.connect(window.runtime.cancel_connect_all)
        toolbar.addWidget(window.stop_connect_button)
        window.connection_results_button = QPushButton("连接结果")
        window.connection_results_button.clicked.connect(window.show_connection_results)
        toolbar.addWidget(window.connection_results_button)
        toolbar.addWidget(window.cancel_button)
        window.address_list_button = QPushButton("地址列表")
        window.address_list_button.clicked.connect(window.show_static_addresses)
        toolbar.addWidget(window.address_list_button)
        for text, action in (("环境诊断", window.diagnose),):
            button = QPushButton(text)
            button.clicked.connect(action)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        description = QLabel("按设备类型查看设备池、连接状态和功能槽位。")
        description.setObjectName("pageDescription")
        layout.addWidget(description)
        window.summary = QLabel()
        window.summary.setWordWrap(True)
        window.summary.setObjectName("overviewNote")
        layout.addWidget(window.summary)
        window.connection_summary = QLabel()
        window.connection_summary.setWordWrap(True)
        window.connection_summary.setObjectName("overviewNote")
        layout.addWidget(window.connection_summary)
        window.card_grid = CategoryCardGrid()
        window.cards = {
            category: CategoryCard(
                category, label, CATEGORY_DESCRIPTIONS[category], window.card_grid.widget()
            )
            for category, label in CATEGORIES.items()
        }
        for card in window.cards.values():
            card.hide()
            card.clicked.connect(window.open_category)
        layout.addWidget(window.card_grid, 1)
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
        monitor.clicked.connect(lambda: window.navigate("实时监测"))
        trend_layout.addWidget(monitor)
        layout.addWidget(trend)
        window.device_pool = DevicePool(root)
        window.device_pool.cards = tuple(window.cards.values())
        window.device_pool.categoryRequested.connect(window.open_category)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        window.pool_title = QLabel("设备池")
        window.pool_title.setObjectName("poolTitle")
        titles.addWidget(window.pool_title)
        window.pool_count = QLabel()
        window.pool_count.setObjectName("poolCaption")
        titles.addWidget(window.pool_count)
        window.device_pool.heading.addLayout(titles, 1)
        window.pool_close = QPushButton("×")
        window.pool_close.setObjectName("poolClose")
        window.pool_close.setAccessibleName("关闭设备池")
        window.pool_close.setToolTip("关闭设备池 · Esc")
        window.pool_close.setFixedSize(32, 32)
        window.pool_close.clicked.connect(window.device_pool.hide)
        window.device_pool.heading.addWidget(window.pool_close, 0, Qt.AlignmentFlag.AlignTop)
        window.presence_filter = WorkspaceComboBox()
        for title, value in (("当前设备", "current"), ("历史与已配置", "history"), ("全部设备", "all")):
            window.presence_filter.addItem(title, value)
        window.presence_filter.currentIndexChanged.connect(window.select_category)
        window.category_filter = WorkspaceComboBox()
        window.category_filter.addItem("全部类别", "")
        for category, title in CATEGORIES.items():
            window.category_filter.addItem(title, category)
        window.category_filter.currentIndexChanged.connect(window.select_category)
        window.filter = WorkspaceComboBox()
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
            window.filter.addItem(title, value)
        window.filter.currentIndexChanged.connect(window.refresh)
        for combo in (window.presence_filter, window.category_filter, window.filter):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        window.presence_filter.setAccessibleName("设备范围")
        window.category_filter.setAccessibleName("设备类别")
        window.filter.setAccessibleName("设备状态")
        window.pool_list = QFrame()
        window.pool_list.setObjectName("poolListPanel")
        list_layout = QVBoxLayout(window.pool_list)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        list_heading = QLabel("设备列表")
        list_heading.setObjectName("poolGroupTitle")
        list_layout.addWidget(list_heading)
        window.tree = QTreeWidget()
        window.tree.setObjectName("poolDeviceList")
        window.tree.viewport().setObjectName("poolListViewport")
        window.tree.setAccessibleName("设备列表")
        window.tree.setColumnCount(6)
        window.tree.setHeaderLabels(["设备 / 型号", "类别", "VISA / 连接地址", "状态", "功能槽位", "操作"])
        window.tree.setRootIsDecorated(False)
        window.tree.setAlternatingRowColors(False)
        window.tree.setUniformRowHeights(True)
        window.tree.header().setStretchLastSection(False)
        for column, width in enumerate((195, 90, 300, 95, 120, 110)):
            window.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            window.tree.setColumnWidth(column, width)
        window.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # The delegate presents name, model, state and slot in one selectable device card.
        for column in (1, 2, 3, 4, 5):
            window.tree.setColumnHidden(column, True)
        window.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        window.tree.setHeaderHidden(True)
        window.tree.setItemDelegate(DeviceRowDelegate(window.tree))
        window.tree.setMouseTracking(True)
        window.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        window.tree.setVerticalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        window.tree.setMinimumSize(0, 0)
        window.tree.itemSelectionChanged.connect(window.selection_changed)
        list_layout.addWidget(window.tree, 1)
        window.pool_list.setFocusProxy(window.tree)
        window.pool_empty = QLabel("暂无符合条件的设备\n可切换范围查看历史记录")
        window.pool_empty.setObjectName("poolEmpty")
        window.pool_empty.setWordWrap(True)
        window.pool_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        list_layout.addWidget(window.pool_empty, 1)
        window.details = DeviceDetails()
        window.action_buttons = {}
        window.pool_more = QPushButton("更多  ···")
        window.pool_more.setObjectName("poolMore")
        window.pool_more.setProperty("poolAction", True)
        window.pool_more.setProperty("buttonSize", "panel")
        window.pool_menu = WorkspaceMenu(window.pool_more)
        window.pool_menu.setMinimumWidth(196)
        window.pool_more.setMenu(window.pool_menu)
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
            ("configure", "指定适配器"),
            ("diagnostic", "设备诊断"),
            ("manage", "设备控制"),
            ("monitor", "实时监测"),
        ):
            control: QPushButton | QAction
            if key in {"connect", "disconnect", "stop", "slot", "manage", "monitor"}:
                control = QPushButton(title)
                control.setProperty("poolAction", True)
                control.setProperty("buttonSize", "panel")
                control.clicked.connect(lambda _checked=False, key=key: window.action(key))
            else:
                control = window.pool_menu.addAction(title)
                control.triggered.connect(lambda _checked=False, key=key: window.action(key))
                if key in {"status", "resource", "configure"}:
                    window.pool_menu.addSeparator()
            window.action_buttons[key] = control
        main_actions = []
        for key in ("connect", "disconnect", "slot", "manage", "monitor"):
            action_button = window.action_buttons[key]
            assert isinstance(action_button, QPushButton)
            main_actions.append(action_button)
        window.action_buttons["connect"].setObjectName("primaryButton")
        window.action_buttons["stop"].setObjectName("poolStop")
        window.device_pool.set_actions((*main_actions, window.pool_more))
        hint = QLabel("断开软件连接不会关闭仪器物理输出。")
        hint.setObjectName("poolCaption")
        hint.setWordWrap(True)
        hint_row = QHBoxLayout()
        hint_row.addWidget(hint, 1)
        stop_button = window.action_buttons["stop"]
        assert isinstance(stop_button, QPushButton)
        hint_row.addWidget(stop_button)
        window.device_pool.footer.addLayout(hint_row)
        window.device_pool.set_content(
            window.pool_list, window.details, (window.presence_filter, window.category_filter, window.filter)
        )
        window.device_pool.hide()
        window.message = QLabel(
            ("真实设备" if window.runtime.hardware else "模拟演示") + " · 扫描不会自动连接或恢复输出"
        )
        window.message.setWordWrap(True)
        window.message.setObjectName("commandStatusBar")
        layout.addWidget(window.message)
        window.setCentralWidget(root)

    def select_category(self, *_args) -> None:
        window = self.window
        window.active_category = window.category_filter.currentData() or ""
        window.refresh()
        candidates = [row for row in window.rows.values() if not row.isHidden()]
        if candidates:
            window.tree.setCurrentItem(candidates[0])
        else:
            window.tree.clearSelection()
            window.selected_id = ""
            window.refresh_details()

    def open_category(self, category: str) -> None:
        window = self.window
        window.category_filter.setCurrentIndex(window.category_filter.findData(category))
        window.filter.setCurrentIndex(0)
        window.select_category()
        window.show_device_pool(window.cards.get(category))

    def show_device_pool(self, trigger: QWidget | None = None) -> None:
        window = self.window
        if window.stack.currentWidget() is not window.overview_page:
            return
        window.device_pool.present(trigger)

    def refresh(self) -> None:
        window = self.window
        if window.closing:
            return
        if hasattr(window, "clock_label"):
            window.clock_label.setText(QDateTime.currentDateTime().toString("HH:mm:ss"))
        window.records = window.controller.records
        window.states = window.controller.states
        window._sync_submenu()
        current_records = {
            key: record
            for key, record in window.records.items()
            if is_current(record, window.states.get(key))
        }
        progress = window.runtime.discovery.progress
        running = window.runtime.discovery.is_scanning
        connection_progress = window.runtime.connection_progress()
        connecting = connection_progress["state"] in {"running", "stopping"}
        window.scan_button.setEnabled(not running and not connecting)
        window.scan_button.setToolTip("正在批量连接，请等待完成或停止连接" if connecting else "")
        window.scan_button.setText("扫描中…" if running else "扫描设备")
        window.cancel_button.setEnabled(running)
        window.cancel_button.setVisible(running)
        window.connect_all_button.setEnabled(not running and not connecting and bool(current_records))
        window.connect_all_button.setText("连接中…" if connecting else "一键连接")
        window.connect_all_button.setToolTip(
            "正在扫描并核验设备，完成后可连接" if running else "连接所有当前发现的设备，已有会话保持不变"
        )
        window.stop_connect_button.setVisible(connecting)
        window.stop_connect_button.setEnabled(connection_progress["state"] == "running")
        window.connection_results_button.setVisible(bool(connection_progress["items"]) and not connecting)
        window.connection_summary.setVisible(connection_progress["state"] != "idle")
        if connecting:
            prefix = (
                "正在停止，等待当前连接完成" if connection_progress["state"] == "stopping" else "正在连接"
            )
            window.connection_summary.setText(
                f"{prefix} · {connection_progress['completed']}/{connection_progress['total']}"
                f" · {connection_progress.get('current', '')}"
            )
        else:
            window.connection_summary.setText(window.connection_result_text(connection_progress))
        connected = sum(
            s["connectionState"] in {"connected", "busy", "degraded"} for s in window.states.values()
        )
        identified = sum(bool(item["identity"]) for item in current_records.values())
        errors = sum(
            bool(item.get("lastError") or window.states.get(item["id"], {}).get("lastError"))
            or window.states.get(item["id"], {}).get("connectionState") in {"error", "degraded"}
            for item in current_records.values()
        )
        window.summary.setText(
            ("真实设备" if window.runtime.hardware else "模拟演示")
            + f" · 发现 {len(current_records)} 台 · 在线 {connected} 台 · 异常 {errors} 台 · 已识别 {identified} 台"
            + f" · 历史与已配置 {len(window.records) - len(current_records)} 台"
        )
        window.summary.setToolTip(
            f"最近扫描：{progress.get('finishedAt', '尚未扫描')}；本次结果 {len(progress.get('items', []))}"
        )
        current_filter = window.filter.currentData()
        presence_scope = window.presence_filter.currentData()
        for removed_id in set(window.rows) - window.records.keys():
            item = window.rows.pop(removed_id)
            window.tree.takeTopLevelItem(window.tree.indexOfTopLevelItem(item))
        visible_cards = []
        for category in CATEGORIES:
            records = [item for item in current_records.values() if category_for(item) == category]
            history_count = sum(category_for(item) == category for item in window.records.values()) - len(
                records
            )
            live = sum(
                window.states.get(item["id"], {}).get("connectionState") in {"connected", "busy", "degraded"}
                for item in records
            )
            known = sum(bool(item["identity"]) for item in records)
            bound = sum(bool(item.get("slots", {}).get(window.controller.mode)) for item in records)
            abnormal = sum(
                bool(item.get("lastError"))
                or window.states.get(item["id"], {}).get("connectionState") in {"error", "degraded"}
                for item in records
            )
            index = window.category_filter.findData(category)
            category_text = f"{CATEGORIES[category]}  {live}/{len(records)}"
            if window.category_filter.itemText(index) != category_text:
                window.category_filter.setItemText(index, category_text)
            tooltip = (
                f"发现 {len(records)} · 识别 {known} · 连接 {live} · 异常 {abnormal} · 绑定 {bound}"
                f" · 历史与已配置 {history_count}"
            )
            if window.category_filter.itemData(index, Qt.ItemDataRole.ToolTipRole) != tooltip:
                window.category_filter.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)
            window.cards[category].update_counts(len(records), live, abnormal, bound)
            if category in PRIMARY_CATEGORIES or records:
                visible_cards.append(window.cards[category])
        window.card_grid.set_cards(tuple(visible_cards))
        for device_id, record in window.records.items():
            if device_id not in window.rows:
                row = QTreeWidgetItem(window.tree)
                row.setData(0, Qt.ItemDataRole.UserRole, device_id)
                window.rows[device_id] = row
            row = window.rows[device_id]
            state = window.states.get(device_id, {})
            connection = display_connection_state(state)
            name = device_label(record)
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
                window.controller.slot_notice(record["id"])
                or slot_label(record.get("slots", {}).get(window.controller.mode, "")),
            ]
            badges_changed = row.text(3) != texts[3] or row.text(4) != texts[4]
            for index, text in enumerate(texts):
                if row.text(index) != text:
                    row.setText(index, text)
                    row.setToolTip(index, text)
            fallback_color = "#ee7474" if record.get("presenceState") == "unreachable" else "#8da19e"
            row.setForeground(3, QColor(colors.get(connection, fallback_color)))
            task = display_task(state)
            row.setToolTip(3, texts[3] + (" / " + task if task else ""))
            row.setToolTip(
                0,
                texts[0]
                + "\n"
                + record["identity"].get("serial", "")
                + f"\n连接状态：{row.toolTip(3).removeprefix('● ')}"
                + f"\n功能槽位：{texts[4]}",
            )
            if badges_changed:
                # Both badges come from hidden columns; invalidate the visible card too.
                window.tree.viewport().update(window.tree.visualItemRect(row))
            show = not window.active_category or category_for(record) == window.active_category
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
        visible_count = sum(not row.isHidden() for row in window.rows.values())
        window.pool_title.setText(CATEGORIES.get(window.active_category, "全部类别") + " · 设备池")
        window.pool_count.setText(f"{window.presence_filter.currentText()} · {visible_count} 台符合筛选")
        window.tree.setVisible(bool(visible_count))
        window.pool_empty.setVisible(not visible_count)
        if window.selected_id and (
            window.selected_id not in window.rows or window.rows[window.selected_id].isHidden()
        ):
            window.tree.clearSelection()
            window.selected_id = ""
        window.refresh_details()

    def selection_changed(self) -> None:
        window = self.window
        selected = window.tree.selectedItems()
        if selected:
            window.selected_id = selected[0].data(0, Qt.ItemDataRole.UserRole)
        window.refresh_details()

    def open_details(self, device_id: str) -> None:
        window = self.window
        window.tree.setCurrentItem(window.rows[device_id])
        window.selected_id = device_id
        window.show_device_pool()
        window.refresh_details()

    def refresh_details(self) -> None:
        window = self.window
        record = window.records.get(window.selected_id)
        state = window.states.get(window.selected_id, {})
        busy = bool(state.get("pending")) or state.get("connectionState") in {"busy", "connecting"}
        scanning = window.runtime.discovery.is_scanning
        connecting = window.runtime.connection_progress()["state"] in {"running", "stopping"}
        for key, button in window.action_buttons.items():
            if key == "manage":
                button.setText("设备控制")
                button.setVisible(
                    not bool(
                        record
                        and instrument_category(record)
                        in {"spectrum_analyzer", "oscilloscope", "power_supply", "optical_power_meter"}
                    )
                )
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
                enabled &= (
                    not busy
                    and not any(
                        state.get(flag)
                        for flag in ("controlsLocked", "controlPending", "controlGroup", "monitoring")
                    )
                    and bool(
                        record
                        and applicable(record, window.controller.mode)
                        and record["adapterId"]
                        and not record.get("identityConflict")
                        and record["discoveryState"] != "conflict"
                    )
                )
            elif key == "resource":
                enabled &= not bool(state)
            elif key == "configure":
                enabled &= not bool(state) and not bool(record and record.get("identityConflict"))
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
            if key in {"connect", "reconnect", "resource", "configure"}:
                enabled &= not scanning
            if key in {"connect", "disconnect", "reconnect", "resource", "configure"}:
                enabled &= not connecting
            button.setEnabled(enabled)
            button.setToolTip(
                "正在批量连接，请等待完成或停止连接"
                if connecting and key in {"connect", "disconnect", "reconnect", "resource", "configure"}
                else "正在扫描并核验设备，完成后可连接"
                if scanning and key in {"connect", "reconnect", "resource", "configure"}
                else "请先连接设备"
                if key in {"manage", "monitor"} and not enabled
                else "设备忙碌，请先停止任务"
                if busy and not background_read_only(state)
                else ""
            )
        window.action_buttons["connect"].setVisible(not bool(state))
        window.action_buttons["disconnect"].setVisible(bool(state))
        window.action_buttons["stop"].setVisible(window.action_buttons["stop"].isEnabled())
        window.device_pool.reflow_actions()
        window.pool_more.setEnabled(bool(record))
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
            configured_serial = config.get("expected_serial", "")
            displayed_serial = identity.get("serial") or configured_serial or "未提供"
            fields = {
                **{
                    key: displayed_serial if key == "serial" else identity.get(key) or "未提供"
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
                "mode": "通用工作区",
                "slot": window.controller.slot_notice(record["id"])
                or slot_label(record.get("slots", {}).get(window.controller.mode, "")),
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
            window.details.set_fields(
                record["displayName"] or identity.get("model") or "待识别设备",
                f"{presence_label(record, state)}  ·  {identity.get('manufacturer') or '身份待确认'}",
                fields,
            )
        else:
            window.details.set_fields(
                "选择一台设备", "从列表选择设备；未接入的设备可在“历史与已配置”中查看。", {}
            )
