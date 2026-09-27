"""The spectrum workspace: one plot, four setting rows, local markers."""

from __future__ import annotations

from dataclasses import asdict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import config_from_record
from lab_control.application.controls import DataSource, data_sources
from lab_control.domain.spectrum import SpectrumMarker
from lab_control.ui.controller import state_label
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.spectrum_controls import SpectrumControls
from lab_control.ui.pages.monitoring.spectrum_markers import MarkerDialog, SpectrumPlot, frequency_text
from lab_control.ui.pages.monitoring.timing import MonitoringTiming
from lab_control.ui.widgets.device_selector import DeviceSelector


class SpectrumMonitoringPage(QWidget):
    def __init__(self, controller, add_card, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumWorkspace")
        self.controller, self.add_card = controller, add_card
        self.device_id = ""
        self.mode = controller.mode
        self.controls: SpectrumControls | None = None
        self.source: DataSource | None = None
        self.markers: list[SpectrumMarker] = []
        self.marker_key = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "spectrum_analyzer", self, compact=True)
        header.addWidget(self.selector, 1)
        self.connection = QLabel(self)
        self.connection.setObjectName("sourceConnectionState")
        header.addWidget(self.connection)
        self.start_button = QPushButton("开始监测", self)
        self.start_button.clicked.connect(self.toggle_monitoring)
        header.addWidget(self.start_button)
        layout.addLayout(header)
        self.timing = MonitoringTiming(controller, self)
        self.interval = self.timing.interval
        self.timing.hide()
        self.empty = QLabel("请先从总览连接频谱仪，再选择设备。", self)
        layout.addWidget(self.empty)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        chart = self.chart = QFrame(self.splitter)
        chart.setObjectName("spectrumChart")
        chart.setMinimumWidth(280)
        plot_layout = QVBoxLayout(chart)
        plot_layout.setContentsMargins(10, 10, 10, 10)
        toolbar = QHBoxLayout()
        title = QLabel("频谱", chart)
        title.setObjectName("spectrumChartTitle")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.record_button = RecordingButton(chart)
        self.record_button.clicked.connect(self.record)
        toolbar.addWidget(self.record_button)
        tools = QToolButton(chart)
        tools.setObjectName("curveTools")
        tools.setFixedHeight(30)
        tools.setText("曲线工具")
        tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(tools)
        self.save_action = menu.addAction("保存当前帧", self.save)
        menu.addAction("添加到监测总览", self.pin)
        menu.addSeparator()
        menu.addAction("适应曲线", self.fit_plot)
        self.timing_action = menu.addAction("采集节奏")
        self.timing_action.setCheckable(True)
        self.timing_action.toggled.connect(self.timing.setVisible)
        tools.setMenu(menu)
        toolbar.addWidget(tools)
        plot_layout.addLayout(toolbar)
        plot_layout.addWidget(self.timing)
        self.plot = SpectrumPlot(chart)
        self.plot.edit_marker.connect(self.edit_marker)
        self.plot.readings_changed.connect(self.update_marker_list)
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
        self.marker_list: QListWidget | None = None
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if self.controls and self.controls.running:
            return
        if self.device_id == device_id and self.mode == self.controller.mode and self.controls:
            return
        self.device_id, self.mode = device_id, self.controller.mode
        self.timing.bind(device_id, self.mode)
        self.plot.reset()
        self.marker_list = None
        if self.controls:
            self.right_layout.removeWidget(self.controls)
            self.controls.hide()
            self.controls.deleteLater()
            self.controls = None
        self.source = None
        self.markers = []
        self.splitter.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        if device_id:
            config = config_from_record(self.controller.records[device_id])
            self.source = next((s for s in data_sources(config) if s.kind == "spectrum"), None)
            self.controls = SpectrumControls(self.controller, device_id, self.right)
            self.right_layout.addWidget(self.controls)
            self.controls.busy_changed.connect(lambda busy: self.selector.setEnabled(not busy))
            row = QHBoxLayout()
            label = QLabel("软件标记", self.controls)
            label.setObjectName("spectrumChartTitle")
            row.addWidget(label)
            row.addStretch()
            add = QPushButton("＋ 添加标记", self.controls)
            add.clicked.connect(self.add_marker)
            row.addWidget(add)
            self.controls.marker_slot.addLayout(row)
            self.marker_list = QListWidget(self.controls)
            self.marker_list.setObjectName("spectrumMarkerList")
            self.marker_list.setMinimumHeight(92)
            self.marker_list.setMaximumHeight(130)
            self.marker_list.itemDoubleClicked.connect(
                lambda item: self.edit_marker(item.data(Qt.ItemDataRole.UserRole))
            )
            self.controls.marker_slot.addWidget(self.marker_list)
            hint = QLabel("双击标记可编辑 · 仅作用于软件曲线", self.controls)
            hint.setObjectName("rangeHint")
            self.controls.marker_slot.addWidget(hint)
            self.marker_key = f"spectrum-markers/{self.mode}/{device_id}"
            stored = self.controller.runtime.repository.load_workspace(self.marker_key, [])
            if isinstance(stored, list):
                for value in stored:
                    try:
                        marker = SpectrumMarker(**value)
                        if marker.number not in {m.number for m in self.markers}:
                            self.markers.append(marker)
                    except (ValueError, TypeError):
                        continue
            self.plot.set_markers(self.markers)
        if self.isVisible():
            self.controller.focus_settings(device_id)
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.controller.focus_settings(self.device_id)
        self.refresh_frame(self.device_id)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")

    def focus_card(self, spec: dict) -> None:
        self.selector.select(spec["device_id"])

    def toggle_monitoring(self) -> None:
        if not self.source or not self.device_id:
            return
        service = self.controller.runtime.monitoring
        if service.snapshots().get(self.device_id, {}).get("running"):
            self.controller.watch(service.stop(self.device_id))
        else:
            try:
                self.controller.ensure_applicable(self.device_id)
                # Passive reads: never INIT, change trigger mode, or start a physical sweep.
                self.timing.commit()
                service.start(
                    self.device_id,
                    (self.source.request((1,)),),
                    **self.timing.options(),
                    mode=self.mode,
                )
            except Exception as exc:
                self.controller.message.emit(str(exc))

    def record(self) -> None:
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        try:
            self.timing.commit()
            service.set_recording(self.device_id, not task.get("recording"), mode=self.mode)
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def save(self) -> None:
        frame = self.plot.displayed_frame
        if frame is not None:
            try:
                self.controller.watch(
                    self.controller.runtime.monitoring.export_displayed_frame(
                        self.device_id, frame, self.mode
                    ),
                    lambda path: self.controller.message.emit(f"已保存当前帧：{path}"),
                )
                self.refresh()
            except Exception as exc:
                self.controller.message.emit(str(exc))

    def fit_plot(self) -> None:
        self.plot.fit_current()

    def pin(self) -> None:
        if self.source:
            self.add_card(self.device_id, self.source.key, (1,))

    def add_marker(self) -> None:
        current = self.controls.current() if self.controls else {}
        marker = SpectrumMarker(
            max((m.number for m in self.markers), default=0) + 1,
            current.get("center_frequency", 0),
            half_width=max(1.0, current.get("span", 20) * 0.05),
        )
        self.marker_dialog(marker, creating=True)

    def edit_marker(self, number: int) -> None:
        marker = next((m for m in self.markers if m.number == number), None)
        if marker:
            self.marker_dialog(marker)

    def marker_dialog(self, marker: SpectrumMarker, *, creating=False) -> None:
        dialog = MarkerDialog(marker, self, creating=creating)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.markers = [m for m in self.markers if m.number != marker.number]
            if not dialog.deleted:
                self.markers.append(dialog.marker)
            self.markers.sort(key=lambda m: m.number)
            self.controller.runtime.repository.save_workspace(
                self.marker_key, [asdict(m) for m in self.markers]
            )
            self.plot.set_markers(self.markers)
        dialog.deleteLater()

    def update_marker_list(self) -> None:
        if self.marker_list is None:
            return
        for i, marker in enumerate(self.markers):
            reading = self.plot.readings.get(marker.number)
            text = f"M{marker.number}  {'寻峰' if marker.tracking else '固定'}  "
            text += frequency_text(
                reading.position if reading and reading.position is not None else marker.frequency
            )
            text += (
                f"\n{reading.amplitude:.8g} {self.plot.amplitude_unit}"
                if reading and reading.amplitude is not None
                else "\n" + (reading.status if reading else "无数据")
            )
            item = self.marker_list.item(i)
            if item is None:
                item = QListWidgetItem(self.marker_list)
            if item.text() != text:
                item.setText(text)
            item.setData(Qt.ItemDataRole.UserRole, marker.number)
            item.setToolTip(
                f"中心：{frequency_text(marker.frequency)} · 搜索 ±{frequency_text(marker.half_width)}"
            )
        while self.marker_list.count() > len(self.markers):
            self.marker_list.takeItem(self.marker_list.count() - 1)

    def refresh(self) -> None:
        if self.device_id != self.selector.device_id and not (self.controls and self.controls.running):
            self.select_device(self.selector.device_id)
            return
        if self.mode != self.controller.mode:
            self.select_device(self.selector.device_id)
            return
        state = self.controller.states.get(self.device_id, {})
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        self.timing.refresh(task)
        self.actual.setText(self.timing.actual.text())
        busy = bool(self.controls and self.controls.running)
        connected = state.get("connectionState") in {"connected", "busy"}
        self.connection.setText(
            "已连接" if connected else state_label(state.get("connectionState", "disconnected"))
        )
        self.start_button.setText("暂停监测" if task.get("running") else "开始监测")
        self.start_button.setEnabled(
            connected and not busy and not (task.get("pending") and not task.get("running"))
        )
        self.record_button.refresh(
            task, connected=connected, busy=busy, locked=bool(state.get("controlsLocked"))
        )
        update_recording_frame(self.chart, task)
        frame = (
            service.latest(self.device_id, self.source.kind, self.source.quantity) if self.source else None
        )
        self.save_action.setEnabled(
            self.plot.displayed_frame is not None
            and not task.get("display_saving")
            and not task.get("capture_saving")
        )
        if frame is None and self.plot.trace is not None:
            self.plot.trace = None
            self.plot.meta.setText("等待当前配置的新频谱 · 图中保留旧曲线")
            self.plot.update_markers()
        text = (
            task.get("error")
            or task.get("recording_notice")
            or task.get("recording_state", {}).get("csv_error")
            or task.get("notice")
            or (
                "设置中 · 曲线读取暂停"
                if busy
                else "记录收尾中 · 正在排空已接收数据"
                if recording_phase(task) == "draining"
                else "正在记录原始数据"
                if task.get("recording")
                else "监测中"
                if task.get("running")
                else "监测已暂停 · 保留最后一帧"
            )
        )
        self.status.setText(text)

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible() or not self.source:
            return
        service = self.controller.runtime.monitoring
        frame = service.latest(device_id, self.source.kind, self.source.quantity)
        if frame:
            self.plot.show_frame(frame, self.source, (1,))
            service.displayed(device_id, frame)
