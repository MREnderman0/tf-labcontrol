from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import mode_applicable
from lab_control.application.config import config_from_record
from lab_control.application.controls import CATEGORY_LABELS, MEASUREMENT_CATEGORIES, DataSource, data_sources
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.form import ControlForm
from lab_control.ui.pages.monitoring.channel_selection import instrument_scan_channels
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.pages.monitoring.recording_state import recording_phase, recording_save_notice
from lab_control.ui.pages.monitoring.scan import ScanMonitoringPage
from lab_control.ui.pages.monitoring.timing import MonitoringTiming
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class MonitoringPage(QWidget):
    scan_page: ScanMonitoringPage | None = None

    def __init__(self, controller: WorkspaceController, category: str, add_card, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.category, self.add_card = controller, category, add_card
        self.sources: tuple[DataSource, ...] = ()
        self.signature = None
        layout = QVBoxLayout(self)
        title = self.title = QLabel(CATEGORY_LABELS[category] + "实时监测")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.selector = DeviceSelector(controller, category)
        layout.addWidget(self.selector)
        self.empty = QLabel("暂无选中的已连接设备。请先在设备总览连接设备。")
        self.empty.setObjectName("mutedText")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self.splitter = splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_area = main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 12, 0)
        options = QHBoxLayout()
        self.source = WorkspaceComboBox()
        self.source.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.source.currentIndexChanged.connect(self.source_changed)
        options.addWidget(self.source, 1)
        self.points = QSpinBox()
        self.points.setRange(1, 4_000_000)
        self.points.setValue(2000)
        options.addWidget(QLabel("请求点数"))
        self.points.setToolTip(
            "请求点数不一定限制仪器传回长度；实际点数见曲线下方。完整波形型号保留全部有效点。"
        )
        options.addWidget(self.points)
        main_layout.addLayout(options)
        self.timing = MonitoringTiming(controller, self)
        self.interval = self.timing.interval
        main_layout.addWidget(self.timing)
        self.numeric_ranges = numeric_ranges = QLabel(
            "点数 1 ～ 4000000（整数） · 自定义采集间隔 0.5 ～ 60 s · 新帧到达后尽快显示"
        )
        numeric_ranges.setObjectName("rangeHint")
        numeric_ranges.setWordWrap(True)
        main_layout.addWidget(numeric_ranges)
        self.channel_row = QHBoxLayout()
        self.channel_checks: dict[int, QCheckBox] = {}
        main_layout.addLayout(self.channel_row)
        self.scan_channels_label = QLabel()
        self.scan_channels_label.setWordWrap(True)
        self.scan_channels_label.setObjectName("rangeHint")
        main_layout.addWidget(self.scan_channels_label)
        self.initiate = QCheckBox("读取前执行单次采集")
        self.initiate.setToolTip(
            "会改变仪器运行状态。Pico 必须启动采集；3458A 需要确认存储/触发行为。其他设备默认读取现有数据。"
        )
        main_layout.addWidget(self.initiate)
        self.button_layout = buttons = QGridLayout()
        self.button_columns = 5
        self.buttons: dict[str, QPushButton] = {}
        for key, label, callback in (
            ("once", "单次读取", lambda: self.start(True)),
            ("start", "开始监测", self.start),
            ("stop", "停止监测", self.stop),
            ("record", "开始记录", self.record),
            ("save", "保存当前帧", self.save),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            self.buttons[key] = button
            buttons.addWidget(button, 0, len(self.buttons) - 1)
        main_layout.addLayout(buttons)
        self.plot = LivePlot()
        main_layout.addWidget(self.plot, 1)
        footer = QHBoxLayout()
        self.status = QLabel("仅查看时不保存实验数据")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.fit_button = QPushButton("适应曲线")
        self.fit_button.clicked.connect(self.plot.fit_current)
        footer.addWidget(self.fit_button)
        main_layout.addLayout(footer)
        splitter.addWidget(main)
        self.form = ControlForm(controller)
        self.form.setMinimumWidth(330)
        self.form.setMinimumHeight(150)
        splitter.addWidget(self.form)
        splitter.setSizes([820, 400])
        layout.addWidget(splitter, 1)
        if category not in MEASUREMENT_CATEGORIES:
            self.form.hide()
            manage = QPushButton("前往设备管理调整输出 / 设定值 →")
            manage.clicked.connect(
                lambda: controller.navigate.emit("设备管理", category, self.selector.device_id)
            )
            main_layout.addWidget(manage)
        self.scan_page = None
        if category == "multimeter":
            self.scan_page = ScanMonitoringPage(controller, add_card, self, manage_selection=False)
            layout.addWidget(self.scan_page, 1)
            self.scan_page.hide()
            self.scan_page.selector.selected.connect(lambda device: self.selector.select(device))
        self.selector.selected.connect(self.select_device)
        self.selector.channel_changed.connect(self._select_parameter_channel)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def _select_parameter_channel(self, _channel: int) -> None:
        if self.instrument_scan_channels() is None:
            self.form.set_device(self.selector.device_id, self.selector.channel.currentData() or 1)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        orientation = Qt.Orientation.Vertical if self.width() < 850 else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([500, 260] if orientation == Qt.Orientation.Vertical else [820, 400])
        QTimer.singleShot(0, self, self.reflow_buttons)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_frame(self.selector.device_id)

    def reflow_buttons(self) -> None:
        columns = 3 if self.main_area.width() < 620 else 5
        if columns == self.button_columns:
            return
        self.button_columns = columns
        while self.button_layout.count():
            self.button_layout.takeAt(0)
        for index, button in enumerate(self.buttons.values()):
            self.button_layout.addWidget(button, index // columns, index % columns)

    def select_device(self, device_id: str) -> None:
        if self.scan_page is not None:
            scan = bool(device_id) and instrument_scan_channels(self.controller, device_id) is not None
            self.scan_page.setVisible(scan)
            self.title.setVisible(not scan)
            self.selector.setVisible(not scan)
            if scan:
                self.empty.hide()
                self.splitter.hide()
                self.form.set_device("", 1)
                self.scan_page.selector.select(device_id)
                self.scan_page.select_device(device_id)
                return
            if self.scan_page.device_id:
                self.scan_page.select_device("")
        self.timing.bind(device_id, self.controller.mode)
        self.splitter.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        self.source.blockSignals(True)
        self.source.clear()
        self.sources = (
            data_sources(config_from_record(self.controller.records[device_id])) if device_id else ()
        )
        for source in self.sources:
            self.source.addItem(source.label, source.key)
        self.source.blockSignals(False)
        while self.channel_row.count():
            item = self.channel_row.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
        self.channel_checks = {}
        if device_id:
            channels = (
                self.controller.capabilities(device_id).channels
                if self.category in {"power_supply", "oscilloscope"}
                else (1,)
            )
            for channel in () if self.instrument_scan_channels() is not None else channels:
                check = QCheckBox(f"CH {channel}")
                check.setChecked(channel == channels[0])
                self.channel_checks[channel] = check
                self.channel_row.addWidget(check)
            self.channel_row.addStretch()
            adapter = self.controller.records[device_id]["adapterId"]
            definition = DEVICE_DEFINITIONS.get(adapter)
            self.initiate.setChecked(bool(definition and definition.acquisition_initiate_default))
            self.initiate.setEnabled(definition is None or definition.acquisition_initiate_editable)
        self.plot.reset()
        self.source_changed()
        if self.category in MEASUREMENT_CATEGORIES:
            self.form.set_device(device_id, self.selector.channel.currentData() or 1)
        self.refresh()
        self.refresh_frame(device_id)

    def channels(self) -> tuple[int, ...]:
        scan = self.instrument_scan_channels()
        if scan is not None:
            return scan
        return tuple(channel for channel, check in self.channel_checks.items() if check.isChecked())

    def instrument_scan_channels(self) -> tuple[int, ...] | None:
        return instrument_scan_channels(self.controller, self.selector.device_id)

    def source_changed(self) -> None:
        self.plot.reset()
        source = self.selected_source()
        if source:
            self.points.setRange(1, 4_000_000)
            self.points.setValue(source.request((1,)).points)
            self.points.setEnabled(source.kind != "status")
            self.numeric_ranges.setText("请求点数 1 ～ 4000000（整数） · 自定义采集间隔 0.5 ～ 60 s")
            device_id = self.selector.device_id
            caps = self.controller.capabilities(device_id) if device_id else None
            point_cap = (
                next((cap for cap in caps.operations if cap.name == "sweep_points"), None) if caps else None
            )
            point = point_cap.parameters.get("value") if point_cap else None
            if (
                source.kind == "spectrum"
                and point_cap
                and not point_cap.writable
                and point
                and point.minimum is not None
                and point.minimum == point.maximum
            ):
                fixed = int(point.minimum)
                self.points.setRange(fixed, fixed)
                self.points.setValue(fixed)
                self.points.setEnabled(False)
                self.numeric_ranges.setText(f"点数 {fixed}（型号固定） · 自定义采集间隔 0.5 ～ 60 s")

    def focus_card(self, spec: dict) -> None:
        channels = tuple(spec["channels"])
        self.selector.select(spec["device_id"], channels[0] if channels else 1)
        if self.scan_page is not None and self.instrument_scan_channels() is not None:
            self.scan_page.focus_card(spec)
            return
        self.source.setCurrentIndex(max(0, self.source.findData(spec["source"])))
        for channel, check in self.channel_checks.items():
            check.setChecked(channel in channels)

    def selected_source(self):
        return next((source for source in self.sources if source.key == self.source.currentData()), None)

    def start(self, once: bool = False) -> None:
        if not once and self.controller.reconnect_if_needed(self.selector.device_id):
            return
        device_id, source = self.selector.device_id, self.selected_source()
        if not device_id or not source:
            return
        try:
            self.controller.ensure_applicable(device_id)
            adapter = self.controller.records[device_id]["adapterId"]
            definition = DEVICE_DEFINITIONS.get(adapter)
            confirmed = False
            if self.initiate.isChecked() or (definition and definition.acquisition_requires_confirmation):
                answer = QMessageBox.question(
                    self,
                    "确认采集行为",
                    f"设备：{self.selector.combo.currentText()}\n通道：{self.channels()}\n"
                    "将按所选方式启动仪器采集或读取存储；可能停止仪器内部记录。\n"
                    "本次授权用于当前监测任务的每批读取；不改变其他设备输出。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                confirmed = True
            request = source.request(
                self.channels(), self.points.value(), self.initiate.isChecked(), confirmed
            )
            self.timing.commit()
            self.controller.start_monitoring(
                device_id,
                (request,),
                **self.timing.options(),
                mode=self.controller.mode,
                once=bool(once),
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def stop(self) -> None:
        self.controller.watch(self.controller.runtime.monitoring.stop(self.selector.device_id))

    def record(self) -> None:
        device_id = self.selector.device_id
        try:
            self.controller.ensure_applicable(device_id)
            current = self.controller.runtime.monitoring.snapshots().get(device_id, {})
            self.timing.commit()
            self.controller.set_recording(
                device_id, not current.get("recording", False), mode=self.controller.mode
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def save(self) -> None:
        frame = self.plot.displayed_frame
        if frame is not None:
            try:
                service = self.controller.runtime.monitoring
                future = service.export_displayed_frame(self.selector.device_id, frame, self.controller.mode)
                self.controller.watch(
                    future,
                    lambda path: self.controller.message.emit(f"原始数据已保存 · 分析 CSV 已完成：{path}"),
                )
                self.refresh()
            except Exception as exc:
                self.controller.message.emit(str(exc))

    def pin(self) -> None:
        source = self.selected_source()
        if source and self.selector.device_id and self.channels():
            self.add_card(self.selector.device_id, source.key, self.channels())

    def refresh(self) -> None:
        scan = self.instrument_scan_channels()
        if self.scan_page is not None and scan is not None:
            self.scan_page.refresh()
            return
        self.scan_channels_label.setVisible(scan is not None)
        self.scan_channels_label.setText(
            "仪器扫描通道："
            + ("、".join(f"CH {ch}" for ch in scan) if scan else "未配置")
            + " · 在参数区配置扫描列表；采集会覆盖仪器内部读数，软件记录另存。"
            if scan is not None
            else ""
        )
        device_id = self.selector.device_id
        state = self.controller.states.get(device_id, {})
        task = self.controller.runtime.monitoring.snapshots().get(device_id, {})
        self.timing.bind(device_id, self.controller.mode)
        self.timing.refresh(task)
        record = self.controller.records.get(device_id)
        available = bool(
            record
            and mode_applicable(record, self.controller.mode)
            and state.get("connectionState") in {"connected", "busy"}
            and state.get("activeTask") != "continuous_acquisition"
            and not task.get("stopping")
            and recording_phase(task) not in {"preparing", "draining"}
        )
        self.buttons["start"].setText("开始监测")
        self.buttons["start"].setEnabled(available)
        refresh_reconnect_button(self.buttons["start"], self.controller, device_id, task)
        self.buttons["once"].setEnabled(available and not task.get("running") and not task.get("pending"))
        self.buttons["stop"].setEnabled(bool(task.get("running") or task.get("pending")))
        self.buttons["record"].setEnabled(bool((available or task.get("recording")) and task.get("running")))
        self.buttons["record"].setText("停止记录" if task.get("recording") else "开始记录")
        self.buttons["save"].setEnabled(
            self.plot.displayed_frame is not None
            and not task.get("display_saving")
            and not task.get("capture_saving")
        )
        text = (
            task.get("error")
            or task.get("recording_notice")
            or task.get("notice")
            or recording_save_notice(task)
            or (
                "正在记录原始数据"
                if task.get("recording")
                else "监测中 · 尚未开启记录"
                if task.get("running")
                else "已停止软件读取 · 仪器输出保持原状"
            )
        )
        if text != self.status.text():
            self.status.setText(text)
        if self.isVisible() and self.category in MEASUREMENT_CATEGORIES:
            self.form.set_device(device_id, self.selector.channel.currentData() or 1)

    def refresh_frame(self, device_id: str) -> None:
        if getattr(self, "scan_page", None) is not None and self.instrument_scan_channels() is not None:
            return
        source = self.selected_source()
        if device_id != self.selector.device_id or not self.isVisible() or not source:
            return
        service = self.controller.runtime.monitoring
        frame = service.latest(
            device_id, source.kind, source.quantity, self.channels()[0] if self.channels() else 1
        )
        if frame:
            self.plot.show_frame(frame, source, self.channels())
            service.displayed(device_id, frame)
