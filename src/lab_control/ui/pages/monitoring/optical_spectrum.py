"""Optical spectrometer live monitoring workspace."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
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
from lab_control.ui.controller import state_label
from lab_control.ui.pages.monitoring.optical_spectrum_controls import OpticalSpectrumControls
from lab_control.ui.pages.monitoring.page import MonitoringPage
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.timing import MonitoringTiming
from lab_control.ui.widgets.device_selector import DeviceSelector


class OpticalSpectrumMonitoringPage(MonitoringPage):
    """A four-parameter, wavelength/dBm view for optical spectrum analyzers."""

    def __init__(self, controller, add_card, parent=None) -> None:
        # Skip the generic monitoring form while retaining its common page type
        # for workspace navigation and existing integrations.
        QWidget.__init__(self, parent)
        self.setObjectName("opticalSpectrumWorkspace")
        self.controller, self.add_card = controller, add_card
        self.device_id = ""
        self.mode = "general"
        self.data_source: DataSource | None = None
        self.controls: OpticalSpectrumControls | None = None
        # Compatibility surface for older workspace integrations; the visible
        # editor set is the four-item controls panel above.
        self.form = type("_OpticalFormCompatibility", (), {"editors": {}})()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "optical_spectrometer", self, compact=True)
        header.addWidget(self.selector, 1)
        self.connection = QLabel(self)
        self.connection.setObjectName("sourceConnectionState")
        header.addWidget(self.connection)
        self.start_button = QPushButton("开始监测", self)
        self.start_button.clicked.connect(self.toggle_monitoring)
        header.addWidget(self.start_button)
        layout.addLayout(header)
        self.empty = QLabel("请先从总览连接光谱仪，再选择设备。", self)
        self.empty.setObjectName("mutedText")
        layout.addWidget(self.empty)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        chart = self.chart = QFrame(self.splitter)
        chart.setObjectName("spectrumChart")
        chart.setMinimumWidth(280)
        plot_layout = QVBoxLayout(chart)
        plot_layout.setContentsMargins(10, 10, 10, 10)
        toolbar = QHBoxLayout()
        title = QLabel("实时光谱", chart)
        title.setObjectName("spectrumChartTitle")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.record_button = RecordingButton(chart)
        self.record_button.clicked.connect(self.record)
        toolbar.addWidget(self.record_button)
        tools = QToolButton(chart)
        tools.setObjectName("curveTools")
        tools.setProperty("buttonSize", "compact")
        tools.setText("曲线工具")
        tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(tools)
        self.save_action = menu.addAction("保存当前帧", self.save)
        menu.addAction("适应曲线", self.fit_plot)
        tools.setMenu(menu)
        toolbar.addWidget(tools)
        plot_layout.addLayout(toolbar)
        self.timing = MonitoringTiming(controller, self)
        self.timing.set_fixed_interval(True)
        plot_layout.addWidget(self.timing)
        self.plot = LivePlot(chart)
        plot_layout.addWidget(self.plot, 1)
        self.status = QLabel("监测未开始", chart)
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        self.actual = QLabel("实际 — s/帧")
        self.actual.setObjectName("rangeHint")
        footer.addWidget(self.actual)
        plot_layout.addLayout(footer)
        self.splitter.addWidget(chart)
        self.right = QWidget(self.splitter)
        self.right.setMinimumWidth(360)
        self.right.setMaximumWidth(430)
        self.right_layout = QVBoxLayout(self.right)
        self.right_layout.setContentsMargins(8, 0, 0, 0)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([800, 400])
        layout.addWidget(self.splitter, 1)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if self.controls and self.controls.running:
            return
        if device_id == self.device_id and self.controls:
            return
        self.device_id = device_id
        self.timing.bind(device_id, self.mode)
        self.plot.reset()
        if self.controls:
            self.right_layout.removeWidget(self.controls)
            self.controls.hide()
            self.controls.deleteLater()
            self.controls = None
        self.data_source = None
        self.splitter.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        if device_id:
            config = config_from_record(self.controller.records[device_id])
            self.form.editors = {cap.name: cap for cap in self.controller.capabilities(device_id).operations}
            self.data_source = next(
                (source for source in data_sources(config) if source.kind == "spectrum"), None
            )
            self.controls = OpticalSpectrumControls(self.controller, device_id, self.right)
            self.right_layout.addWidget(self.controls)
            self.controls.busy_changed.connect(lambda busy: self.selector.setEnabled(not busy))
            self.controller.focus_settings(device_id)
            QTimer.singleShot(0, self.controls.refresh_parameters)
        self.refresh()
        self.refresh_frame(device_id)

    def resizeEvent(self, event) -> None:  # noqa: N802
        QWidget.resizeEvent(self, event)
        orientation = Qt.Orientation.Vertical if self.width() < 850 else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([500, 300] if orientation == Qt.Orientation.Vertical else [800, 400])

    def focus_card(self, spec: dict) -> None:
        self.selector.select(spec["device_id"])

    def toggle_monitoring(self) -> None:
        if not self.data_source or not self.device_id:
            return
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        if task.get("running") or task.get("pending"):
            self.controller.watch(service.stop(self.device_id))
            return
        try:
            self.controller.ensure_applicable(self.device_id, workspace_mode=self.mode)
            self.timing.commit()
            service.start(
                self.device_id,
                (self.data_source.request((1,), points=2000, initiate=True, confirmed=True),),
                **self.timing.options(),
                mode=self.mode,
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def record(self) -> None:
        try:
            self.timing.commit()
            task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
            self.controller.runtime.monitoring.set_recording(
                self.device_id, not task.get("recording"), mode=self.mode
            )
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def save(self) -> None:
        if self.plot.displayed_frame is None:
            return
        try:
            self.controller.watch(
                self.controller.runtime.monitoring.export_displayed_frame(
                    self.device_id, self.plot.displayed_frame, self.mode
                ),
                lambda path: self.controller.message.emit(f"原始数据已保存 · 分析 CSV 已完成：{path}"),
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def fit_plot(self) -> None:
        self.plot.fit_current()

    def pin(self) -> None:
        if self.data_source:
            self.add_card(self.device_id, self.data_source.key, (1,))

    def refresh(self) -> None:
        if self.device_id != self.selector.device_id and not (self.controls and self.controls.running):
            self.select_device(self.selector.device_id)
            return
        state = self.controller.states.get(self.device_id, {})
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        self.timing.refresh(task)
        self.actual.setText(self.timing.actual.text())
        connected = state.get("connectionState") in {"connected", "busy"}
        self.connection.setText(
            "已连接" if connected else state_label(state.get("connectionState", "disconnected"))
        )
        self.start_button.setText("暂停监测" if task.get("running") else "开始监测")
        self.start_button.setEnabled(connected and not (self.controls and self.controls.running))
        self.record_button.refresh(
            task,
            connected=connected,
            busy=bool(self.controls and self.controls.running),
            locked=bool(state.get("controlsLocked")),
        )
        update_recording_frame(self.chart, task)
        self.save_action.setEnabled(
            self.plot.displayed_frame is not None
            and not task.get("display_saving")
            and not task.get("capture_saving")
        )
        self.status.setText(
            task.get("error")
            or task.get("recording_notice")
            or recording_save_notice(task)
            or task.get("notice")
            or (
                "设置中 · 曲线读取暂停"
                if self.controls and self.controls.running
                else "记录收尾中 · 正在排空已接收数据"
                if recording_phase(task) == "draining"
                else "正在记录原始数据"
                if task.get("recording")
                else "监测中 · 每周期单次扫描"
                if task.get("running")
                else "监测已暂停 · 保留最后一帧"
            )
        )

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible() or not self.data_source:
            return
        frame = self.controller.runtime.monitoring.latest(
            device_id, self.data_source.kind, self.data_source.quantity
        )
        if frame:
            self.plot.show_frame(frame, self.data_source, (1,))
            self.controller.runtime.monitoring.displayed(device_id, frame)
