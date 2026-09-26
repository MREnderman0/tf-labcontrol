"""Oscilloscope workspace: passive monitoring and explicit instrument gestures."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import config_from_record
from lab_control.application.controls import DataSource, data_sources
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.ui.controller import state_label
from lab_control.ui.pages.monitoring.oscilloscope_controls import OscilloscopeControls
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot
from lab_control.ui.pages.monitoring.timing import MonitoringTiming
from lab_control.ui.widgets.device_selector import DeviceSelector


class OscilloscopeMonitoringPage(QWidget):
    def __init__(self, controller, add_card, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.add_card = controller, add_card
        self.device_id, self.mode = "", controller.mode
        self.controls: OscilloscopeControls | None = None
        self.source: DataSource | None = None
        self.memory_busy = False
        self.plot: OscilloscopePlot
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "oscilloscope", self, compact=True)
        header.addWidget(self.selector, 1)
        self.connection = QLabel()
        self.connection.setObjectName("sourceConnectionState")
        header.addWidget(self.connection)
        self.start_button = QPushButton("开始监测")
        self.start_button.clicked.connect(self.toggle_monitoring)
        header.addWidget(self.start_button)
        layout.addLayout(header)
        self.empty = QLabel("请先从总览连接示波器，再选择设备。")
        layout.addWidget(self.empty)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        chart = QFrame()
        chart.setObjectName("spectrumChart")
        chart.setMinimumWidth(280)
        chart_layout = QVBoxLayout(chart)
        chart_layout.setContentsMargins(10, 8, 10, 8)
        tools_row = QHBoxLayout()
        title = QLabel("时域波形")
        title.setObjectName("spectrumChartTitle")
        tools_row.addWidget(title)
        tools_row.addStretch()
        tools = QToolButton()
        tools.setText("曲线工具")
        tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(tools)
        self.record_action = menu.addAction("开始记录", self.record)
        self.save_action = menu.addAction("保存当前帧", self.save)
        self.memory_action = menu.addAction("读取完整存储波形", self.read_memory)
        self.memory_action.setVisible(False)
        self.pin_action = menu.addAction("添加到监测总览", self.pin)
        menu.addSeparator()
        self.refresh_action = menu.addAction("刷新参数", self.refresh_parameters)
        self.fit_action = menu.addAction("恢复仪器视图", lambda: self.plot.fit_current())
        self.timing_action = menu.addAction("采集节奏")
        self.timing_action.setCheckable(True)
        tools.setMenu(menu)
        tools_row.addWidget(tools)
        chart_layout.addLayout(tools_row)
        self.timing = MonitoringTiming(controller)
        self.timing.hide()
        self.timing_action.toggled.connect(self.timing.setVisible)
        chart_layout.addWidget(self.timing)
        self.plot = OscilloscopePlot()
        chart_layout.addWidget(self.plot, 1)
        footer = QHBoxLayout()
        self.status = QLabel("监测未开始")
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.actual = QLabel("实际 — s/帧")
        self.actual.setObjectName("rangeHint")
        footer.addWidget(self.actual)
        chart_layout.addLayout(footer)
        self.splitter.addWidget(chart)
        self.right = QWidget()
        self.right.setMinimumWidth(300)
        self.right.setMaximumWidth(360)
        self.right_layout = QVBoxLayout(self.right)
        self.right_layout.setContentsMargins(6, 0, 0, 0)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([850, 330])
        layout.addWidget(self.splitter, 1)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if self.memory_busy or self.controls and self.controls.running:
            return
        if (self.device_id, self.mode) == (device_id, self.controller.mode) and self.controls:
            return
        self.device_id, self.mode = device_id, self.controller.mode
        self.timing.bind(device_id, self.mode)
        self.plot.reset()
        if self.controls:
            self.controls.hide()
            self.right_layout.removeWidget(self.controls)
            self.controls.deleteLater()
            self.controls = None
        self.source = None
        self.splitter.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        if device_id:
            self.source = next(
                (
                    s
                    for s in data_sources(config_from_record(self.controller.records[device_id]))
                    if s.kind == "waveform"
                ),
                None,
            )
            self.controls = OscilloscopeControls(self.controller, device_id, self.start, self.right)
            self.right_layout.addWidget(self.controls)
            self.controls.busy_changed.connect(lambda busy: self.selector.setEnabled(not busy))
            self.controls.channels_changed.connect(self.channels_changed)
        if self.isVisible():
            self.focus_settings()
        self.refresh()

    def focus_settings(self) -> None:
        self.controller.focus_settings(self.device_id, self.controls.keys() if self.controls else ())

    def refresh_parameters(self) -> None:
        if self.controls:
            self.controls.refresh_parameters()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.focus_settings()
        self.refresh_frame(self.device_id)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")

    def focus_card(self, spec: dict) -> None:
        self.selector.select(spec["device_id"])
        if self.controls and spec.get("channels"):
            check = self.controls.checks.get(spec["channels"][0])
            if check:
                self.controls.scroller.ensureWidgetVisible(check)
                check.setFocus()

    def channels_changed(self) -> None:
        # An explicit OFF must disappear immediately, including the all-off pause.
        if self.controls:
            channels = self.controls.channels()
            self.plot.restrict_channels(channels)
            self.plot.last_token = None
            self.plot.meta.setText("通道已更新 · 等待新波形" if channels else "所有通道已关闭 · 监测暂停")

    def start(self) -> None:
        if not self.source or not self.controls or not self.controls.channels():
            return
        try:
            self.controller.ensure_applicable(self.device_id)
            self.timing.commit()
            self.controller.runtime.monitoring.start(
                self.device_id,
                (self.source.request(self.controls.channels()),),
                **self.timing.options(),
                mode=self.mode,
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def toggle_monitoring(self) -> None:
        service = self.controller.runtime.monitoring
        if service.snapshots().get(self.device_id, {}).get("running"):
            self.controller.watch(service.stop(self.device_id))
        else:
            self.start()

    def record(self) -> None:
        service = self.controller.runtime.monitoring
        try:
            service.set_recording(
                self.device_id,
                not service.snapshots().get(self.device_id, {}).get("recording"),
                mode=self.mode,
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def save(self) -> None:
        if self.plot.displayed_frame is not None:
            try:
                self.controller.watch(
                    self.controller.runtime.monitoring.export_displayed_frame(
                        self.device_id, self.plot.displayed_frame, self.mode
                    ),
                    lambda path: self.controller.message.emit(f"已保存当前帧：{path}"),
                )
                self.refresh()
            except Exception as exc:
                self.controller.message.emit(str(exc))

    def read_memory(self) -> None:
        if not self.controls or self.memory_busy:
            return
        device_id = self.device_id
        try:
            request = AcquisitionRequest(
                "waveform", self.controls.channels(), confirmed=True, waveform_extent="memory"
            )
            future = self.controller.runtime.monitoring.read_memory_waveform(device_id, request)
            self.memory_busy = True
            self.selector.setEnabled(False)

            def finished(frame=None):
                self.memory_busy = False
                self.selector.setEnabled(True)
                if frame is not None and self.source and self.controls and device_id == self.device_id:
                    self.plot.show_frame(frame, self.source, self.controls.channels())
                self.refresh()

            self.controller.watch(future, finished, lambda _exc: finished())
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def pin(self) -> None:
        if self.source and self.controls and self.controls.channels():
            self.add_card(self.device_id, self.source.key, self.controls.channels())

    def refresh(self) -> None:
        if (self.device_id, self.mode) != (self.selector.device_id, self.controller.mode) and not (
            self.memory_busy or self.controls and self.controls.running
        ):
            self.select_device(self.selector.device_id)
            return
        state = self.controller.states.get(self.device_id, {})
        task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
        busy = self.memory_busy or bool(self.controls and self.controls.running)
        snapshot = self.controller.runtime.sessions.capability_snapshot(self.device_id)
        memory_supported = bool(snapshot and snapshot.details.get("memory_waveform"))
        connected = state.get("connectionState") in {"connected", "busy"}
        self.memory_action.setVisible(memory_supported)
        self.memory_action.setEnabled(
            connected
            and not busy
            and not state.get("controlsLocked")
            and bool(self.controls and self.controls.channels())
        )
        self.connection.setText(
            "已连接" if connected else state_label(state.get("connectionState", "disconnected"))
        )
        self.start_button.setText("暂停监测" if task.get("running") else "开始监测")
        self.start_button.setEnabled(
            bool(
                connected
                and not busy
                and (task.get("running") or self.controls and self.controls.channels())
                and not (task.get("pending") and not task.get("running"))
            )
        )
        self.record_action.setText("停止记录" if task.get("recording") else "开始记录")
        self.record_action.setEnabled(bool(task.get("running") and not busy))
        self.save_action.setEnabled(
            self.plot.displayed_frame is not None
            and not task.get("display_saving")
            and not task.get("capture_saving")
        )
        self.refresh_action.setEnabled(bool(connected and not busy and not state.get("controlsLocked")))
        self.pin_action.setEnabled(bool(self.controls and self.controls.channels()))
        self.timing.refresh(task)
        self.actual.setText(self.timing.actual.text())
        old = self.plot.displayed_frame
        waiting = old is not None and (
            old.session_id != state.get("sessionId")
            or old.configuration_version != state.get("configurationVersion")
        )
        self.status.setText(
            task.get("error")
            or task.get("recording_notice")
            or task.get("recording_state", {}).get("csv_error")
            or task.get("notice")
            or (
                "完整波形读取中 · 完成后自动续采"
                if self.memory_busy
                else "设置中 · 暂停后续读取"
                if busy
                else "等待当前配置的新帧 · 保留旧波形及刻度"
                if waiting
                else "记录中 · 逐帧保存"
                if task.get("recording")
                else "监测中"
                if task.get("running")
                else "监测已暂停"
            )
        )

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible() or not self.source or not self.controls:
            return
        service = self.controller.runtime.monitoring
        frame = service.latest(device_id, self.source.kind, self.source.quantity)
        if frame:
            self.plot.show_frame(frame, self.source, self.controls.channels())
            service.displayed(device_id, frame)
