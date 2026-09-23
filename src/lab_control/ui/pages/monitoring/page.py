from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
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
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.form import ControlForm
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.widgets.device_selector import DeviceSelector


class MonitoringPage(QWidget):
    def __init__(self, controller: WorkspaceController, category: str, add_card, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.category, self.add_card = controller, category, add_card
        self.sources: tuple[DataSource, ...] = ()
        self.signature = None
        layout = QVBoxLayout(self)
        title = QLabel(CATEGORY_LABELS[category] + "实时监测")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.selector = DeviceSelector(controller, category)
        layout.addWidget(self.selector)
        self.splitter = splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_area = main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 12, 0)
        options = QHBoxLayout()
        self.source = QComboBox()
        self.source.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.source.currentIndexChanged.connect(self.source_changed)
        options.addWidget(self.source, 1)
        self.points = QSpinBox()
        self.points.setRange(1, 4_000_000)
        self.points.setValue(2000)
        options.addWidget(QLabel("点数"))
        options.addWidget(self.points)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.05, 60)
        self.interval.setValue(0.5)
        self.interval.setSuffix(" s")
        options.addWidget(QLabel("轮询"))
        options.addWidget(self.interval)
        main_layout.addLayout(options)
        self.channel_row = QHBoxLayout()
        self.channel_checks: dict[int, QCheckBox] = {}
        main_layout.addLayout(self.channel_row)
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
            ("save", "保存当前数据", self.save),
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
        pin = QPushButton("添加此项目到监测总览")
        pin.clicked.connect(self.pin)
        footer.addWidget(pin)
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
        self.selector.selected.connect(self.select_device)
        self.selector.channel_changed.connect(
            lambda _ch: self.form.set_device(
                self.selector.device_id, self.selector.channel.currentData() or 1
            )
        )
        controller.refreshed.connect(self.refresh)
        self.select_device(self.selector.device_id)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        orientation = Qt.Orientation.Vertical if self.width() < 850 else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([500, 260] if orientation == Qt.Orientation.Vertical else [820, 400])
        QTimer.singleShot(0, self.reflow_buttons)

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
            for channel in channels:
                check = QCheckBox(f"CH {channel}")
                check.setChecked(channel == channels[0])
                self.channel_checks[channel] = check
                self.channel_row.addWidget(check)
            self.channel_row.addStretch()
            adapter = self.controller.records[device_id]["adapterId"]
            self.initiate.setChecked(adapter in {"pico.6404e", "keysight.3458a"})
            self.initiate.setEnabled(adapter not in {"pico.6404e", "microchip.53100a"})
        self.plot.reset()
        self.source_changed()
        if self.category in MEASUREMENT_CATEGORIES:
            self.form.set_device(device_id, self.selector.channel.currentData() or 1)
        self.refresh()

    def channels(self) -> tuple[int, ...]:
        return tuple(channel for channel, check in self.channel_checks.items() if check.isChecked())

    def source_changed(self) -> None:
        self.plot.reset()
        source = self.selected_source()
        if source:
            self.points.setValue(source.request((1,)).points)
            self.points.setEnabled(source.kind != "status")

    def focus_card(self, spec: dict) -> None:
        channels = tuple(spec["channels"])
        self.selector.select(spec["device_id"], channels[0] if channels else 1)
        self.source.setCurrentIndex(max(0, self.source.findData(spec["source"])))
        for channel, check in self.channel_checks.items():
            check.setChecked(channel in channels)

    def selected_source(self):
        return next((source for source in self.sources if source.key == self.source.currentData()), None)

    def start(self, once: bool = False) -> None:
        device_id, source = self.selector.device_id, self.selected_source()
        if not device_id or not source:
            return
        try:
            self.controller.ensure_applicable(device_id)
            adapter = self.controller.records[device_id]["adapterId"]
            confirmed = False
            if self.initiate.isChecked() or adapter == "keysight.3458a":
                answer = QMessageBox.question(
                    self,
                    "确认采集行为",
                    f"设备：{self.selector.combo.currentText()}\n通道：{self.channels()}\n"
                    "将按所选方式启动仪器采集或读取存储。3458A 的存储读取可能停止内部记录。\n"
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
            self.controller.runtime.monitoring.start(
                device_id,
                (request,),
                interval=self.interval.value(),
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
            self.controller.runtime.monitoring.set_recording(
                device_id, not current.get("recording", False), mode=self.controller.mode
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def save(self) -> None:
        source = self.selected_source()
        if source:
            frame = self.controller.runtime.monitoring.latest(
                self.selector.device_id,
                source.kind,
                source.quantity,
                self.channels()[0] if self.channels() else 1,
            )
            if frame:
                future = self.controller.runtime.monitoring.export(frame, self.controller.mode)
                self.controller.watch(
                    future, lambda path: self.controller.message.emit(f"已保存原始数据：{path}")
                )

    def pin(self) -> None:
        source = self.selected_source()
        if source and self.selector.device_id and self.channels():
            self.add_card(self.selector.device_id, source.key, self.channels())

    def refresh(self) -> None:
        device_id = self.selector.device_id
        state = self.controller.states.get(device_id, {})
        task = self.controller.runtime.monitoring.snapshots().get(device_id, {})
        record = self.controller.records.get(device_id)
        available = bool(
            record
            and mode_applicable(record, self.controller.mode)
            and state.get("connectionState") in {"connected", "busy"}
            and state.get("activeTask") != "continuous_acquisition"
        )
        self.buttons["start"].setEnabled(available)
        self.buttons["once"].setEnabled(available and not task.get("running") and not task.get("pending"))
        self.buttons["stop"].setEnabled(bool(task.get("running") or task.get("pending")))
        self.buttons["record"].setEnabled(available and bool(task.get("running")))
        self.buttons["record"].setText("停止记录" if task.get("recording") else "开始记录")
        source = self.selected_source()
        frame = (
            self.controller.runtime.monitoring.latest(
                device_id, source.kind, source.quantity, self.channels()[0] if self.channels() else 1
            )
            if source
            else None
        )
        self.buttons["save"].setEnabled(bool(frame))
        if frame and source and self.isVisible():
            self.plot.show_frame(frame, source, self.channels())
        text = task.get("error") or (
            "正在记录原始数据"
            if task.get("recording")
            else "监测中 · 尚未开启记录"
            if task.get("running")
            else "已停止软件读取 · 仪器输出保持原状"
        )
        if text != self.status.text():
            self.status.setText(text)
        if self.isVisible() and self.category in MEASUREMENT_CATEGORIES:
            self.form.set_device(device_id, self.selector.channel.currentData() or 1)
