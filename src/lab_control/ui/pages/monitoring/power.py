"""Power workspace: one scalar request feeds both trends and channel readouts."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
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
from lab_control.application.controls import data_sources
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.ui.controller import state_label
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.pages.monitoring.power_controls import PowerControls
from lab_control.ui.pages.monitoring.power_plot import CHANNEL_COLORS, PowerPlot
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.timing import MonitoringTiming
from lab_control.ui.widgets.device_selector import DeviceSelector


class PowerMonitoringPage(QWidget):
    def __init__(self, controller, add_card, parent=None):
        super().__init__(parent)
        self.controller, self.add_card = controller, add_card
        self.device_id, self.mode = "", controller.mode
        self.controls: PowerControls | None = None
        self.channel_checks: dict[int, QCheckBox] = {}
        self.choices: dict[tuple[str, str], tuple[int, ...]] = {}
        self.transition = False
        self.generation = 0
        self.session_id = ""
        self.plot: PowerPlot
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "power_supply", self, compact=True, surface="monitoring")
        header.addWidget(self.selector, 1)
        self.connection = QLabel()
        self.connection.setObjectName("sourceConnectionState")
        header.addWidget(self.connection)
        self.start_button = QPushButton("开始监测")
        self.start_button.clicked.connect(self.toggle_monitoring)
        header.addWidget(self.start_button)
        layout.addLayout(header)
        self.empty = QLabel("请先从总览连接电源，再选择设备。")
        layout.addWidget(self.empty)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.chart = QFrame()
        self.chart.setObjectName("spectrumChart")
        chart_layout = QVBoxLayout(self.chart)
        chart_layout.setContentsMargins(10, 8, 10, 8)
        toolbar = QHBoxLayout()
        title = QLabel("电压 / 电流趋势")
        title.setObjectName("spectrumChartTitle")
        toolbar.addWidget(title, 1)
        self.record_button = RecordingButton()
        self.record_button.clicked.connect(self.record)
        toolbar.addWidget(self.record_button)
        tools = QToolButton()
        tools.setObjectName("curveTools")
        tools.setProperty("buttonSize", "compact")
        tools.setText("曲线工具")
        tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(tools)
        self.once_action = menu.addAction("单次读取", lambda: self.start(once=True))
        self.save_action = menu.addAction("保存当前帧", self.save)
        menu.addSeparator()
        menu.addAction("适应曲线", lambda: self.plot.fit_current())
        self.timing_action = menu.addAction("采集间隔")
        self.timing_action.setCheckable(True)
        tools.setMenu(menu)
        toolbar.addWidget(tools)
        chart_layout.addLayout(toolbar)
        self.timing = MonitoringTiming(controller)
        self.timing.hide()
        self.timing_action.toggled.connect(self.timing.setVisible)
        chart_layout.addWidget(self.timing)
        self.channel_row = QHBoxLayout()
        chart_layout.addLayout(self.channel_row)
        self.plot = PowerPlot()
        chart_layout.addWidget(self.plot, 1)
        self.status = QLabel("未开始监测 · 仅查看不保存数据")
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        self.actual = self.timing.actual
        if timing_layout := self.timing.layout():
            timing_layout.removeWidget(self.actual)
        footer.addWidget(self.actual)
        chart_layout.addLayout(footer)
        self.splitter.addWidget(self.chart)
        self.right = QWidget()
        self.right.setMinimumWidth(350)
        self.right.setMaximumWidth(410)
        self.right_layout = QVBoxLayout(self.right)
        self.right_layout.setContentsMargins(6, 0, 0, 0)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([800, 390])
        layout.addWidget(self.splitter, 1)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def channels(self) -> tuple[int, ...]:
        return tuple(ch for ch, check in self.channel_checks.items() if check.isChecked())

    def select_device(self, device_id: str) -> None:
        state = self.controller.states.get(self.device_id, {})
        if self.device_id in self.controller.group_devices or state.get("controlPending") or self.transition:
            self.selector.select(self.device_id)
            return
        if (self.device_id, self.mode) == (device_id, self.controller.mode) and self.controls:
            return
        self.generation += 1
        self.device_id, self.mode = device_id, self.controller.mode
        self.session_id = self.controller.states.get(device_id, {}).get("sessionId", "")
        if self.controls:
            self.right_layout.removeWidget(self.controls)
            self.controls.hide()
            self.controls.deleteLater()
            self.controls = None
        while self.channel_row.count():
            item = self.channel_row.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
        self.channel_checks.clear()
        self.plot.reset()
        self.timing.bind(device_id, self.mode)
        self.empty.setVisible(not device_id)
        self.splitter.setVisible(bool(device_id))
        if device_id:
            channels = self.controller.capabilities(device_id).channels
            selected = self.choices.get((device_id, self.mode), channels)
            with self.controller.runtime.monitoring.lock:
                task = self.controller.runtime.monitoring.tasks.get(device_id)
                if task and task.running:
                    selected = tuple(
                        sorted(
                            {
                                ch
                                for request in task.requests
                                if request.kind == "scalar"
                                for ch in request.channels
                            }
                        )
                    )
            for ch in channels:
                check = QCheckBox(f"CH {ch}")
                color = CHANNEL_COLORS[(ch - 1) % 4]
                check.setStyleSheet(
                    f"QCheckBox {{ color: {color}; }} "
                    f"QCheckBox::indicator:checked {{ background-color: {color}; border-color: {color}; }}"
                )
                check.setToolTip("勾选决定采集和记录范围，不改变仪器输出。")
                check.setChecked(ch in selected)
                check.toggled.connect(self.channels_changed)
                self.channel_checks[ch] = check
                self.channel_row.addWidget(check)
            self.channel_row.addStretch()
            self.controls = PowerControls(self.controller, device_id)
            self.right_layout.addWidget(self.controls)
            self.controls.busy_changed.connect(lambda _busy: self.refresh())
        if self.isVisible():
            self.controller.focus_settings(device_id)
        self.refresh()
        self.refresh_frame(device_id)

    def focus_card(self, spec: dict) -> None:
        self.selector.select(spec["device_id"])
        if not self.controller.states.get(self.device_id, {}).get("controlsLocked"):
            for ch, check in self.channel_checks.items():
                check.blockSignals(True)
                check.setChecked(ch in spec["channels"])
                check.blockSignals(False)
            self.channels_changed()

    def channels_changed(self) -> None:
        self.choices[(self.device_id, self.mode)] = self.channels()
        self.plot.set_channels(self.channels())
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        if task.get("running"):
            self.transition = True
            self.generation += 1
            generation, device, session = self.generation, self.device_id, self.session_id

            def drained(_value):
                self.transition = False
                if (generation, device, session) == (
                    self.generation,
                    self.device_id,
                    self.session_id,
                ) and self.channels():
                    self.start()
                self.refresh()

            self.controller.watch(service.stop(self.device_id), drained, self.transition_failed)
        self.refresh()

    def transition_failed(self, error) -> None:
        self.transition = False
        self.status.setText(str(error))
        self.refresh()

    def toggle_monitoring(self) -> None:
        if self.controller.reconnect_if_needed(self.device_id):
            return
        if (
            self.controller.runtime.monitoring.snapshots().get(self.device_id, {}).get("running")
            or self.transition
        ):
            self.generation += 1
            self.controller.watch(self.controller.runtime.monitoring.stop(self.device_id))
        else:
            self.start()

    def start(self, *, once=False) -> None:
        if not self.controls or not self.channels():
            return
        generation, device = self.generation, self.device_id

        def begin(error=""):
            if error or (generation, device) != (self.generation, self.device_id):
                return
            try:
                self.controller.ensure_applicable(device)
                self.timing.commit()
                self.controller.start_monitoring(
                    device,
                    (AcquisitionRequest("scalar", self.channels(), points=1),),
                    **self.timing.options(),
                    mode=self.mode,
                    once=once,
                )
            except Exception as exc:
                self.controller.message.emit(str(exc))
            self.refresh()

        if not all(self.controls.current_settings(ch) for ch in self.controls.panels):
            self.controls.read_parameters(begin)
        else:
            begin()

    def record(self) -> None:
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        try:
            self.timing.commit()
            self.controller.set_recording(self.device_id, not task.get("recording"), mode=self.mode)
        except Exception as exc:
            self.controller.message.emit(str(exc))
        self.refresh()

    def save(self) -> None:
        frame = self.plot.displayed_frame
        if frame is not None:
            try:
                self.controller.watch(
                    self.controller.runtime.monitoring.export_displayed_frame(
                        self.device_id, frame, self.mode
                    ),
                    lambda path: self.controller.message.emit(f"原始数据已保存 · 分析 CSV 已完成：{path}"),
                )
            except Exception as exc:
                self.controller.message.emit(str(exc))
        self.refresh()

    def pin(self) -> None:
        if self.device_id and self.channels():
            for source in data_sources(config_from_record(self.controller.records[self.device_id])):
                self.add_card(self.device_id, source.key, self.channels())

    def refresh(self) -> None:
        if not self.isVisible():
            return
        if (self.selector.device_id, self.controller.mode) != (self.device_id, self.mode):
            self.select_device(self.selector.device_id)
            return
        state = self.controller.states.get(self.device_id, {})
        session = state.get("sessionId", "")
        if session and session != self.session_id:
            self.generation += 1
            self.session_id = session
            self.plot.reset()
        task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
        connected = state.get("connectionState") in {"connected", "busy"}
        busy = self.device_id in self.controller.group_devices or bool(state.get("controlPending"))
        locked = bool(state.get("controlsLocked")) or recording_phase(task) != "idle"
        self.connection.setText(
            "已连接" if connected else state_label(state.get("connectionState", "disconnected"))
        )
        self.start_button.setText("暂停监测" if task.get("running") or self.transition else "开始监测")
        self.start_button.setEnabled(
            (connected or bool(task.get("running")))
            and not busy
            and (bool(self.channels()) or bool(task.get("running")) or self.transition)
            and not (task.get("pending") and not task.get("running") and not self.transition)
        )
        self.selector.setEnabled(not busy and not self.transition)
        for check in self.channel_checks.values():
            check.setEnabled(connected and not busy and not locked and not self.transition)
        self.timing.refresh(task)
        refresh_reconnect_button(
            self.start_button,
            self.controller,
            self.device_id,
            task,
            busy=busy or self.transition,
            can_cancel=self.transition,
        )
        self.record_button.refresh(task, connected=connected, busy=busy or self.transition, locked=locked)
        update_recording_frame(self.chart, task)
        self.once_action.setEnabled(
            connected
            and bool(self.channels())
            and not busy
            and not task.get("running")
            and not task.get("pending")
            and not locked
            and not self.transition
        )
        self.save_action.setEnabled(
            self.plot.displayed_frame is not None
            and not task.get("display_saving")
            and not task.get("capture_saving")
        )
        self.status.setText(
            task.get("error")
            or task.get("recording_notice")
            or task.get("notice")
            or recording_save_notice(task)
            or (
                "记录收尾中 · 正在排空已接收数据"
                if recording_phase(task) == "draining"
                else "监测中 · 每个有效帧均记录"
                if task.get("recording")
                else "监测中 · 未保存数据"
                if task.get("running")
                else "未开始 / 已暂停 · 保留最后显示的数据"
            )
        )
        if self.controls:
            self.controls.show_measurements(
                self.plot.displayed_frame, self.channels(), paused=not task.get("running")
            )

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible():
            return
        frame = self.controller.runtime.monitoring.latest(device_id, "scalar")
        session = self.controller.states.get(device_id, {}).get("sessionId")
        if frame and frame.session_id == session and self.plot.show_frame(frame, self.channels()):
            self.controller.runtime.monitoring.displayed(device_id, frame)
            if self.controls:
                self.controls.show_measurements(self.plot.displayed_frame, self.channels())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.controller.focus_settings(self.device_id)
        if self.controls:
            self.controls.sync(self.device_id)
        self.refresh()
        self.refresh_frame(self.device_id)

    def hideEvent(self, event) -> None:  # noqa: N802
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
        super().hideEvent(event)
