"""Read-only K+K FXE frequency monitoring workspace."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import mode_applicable
from lab_control.application.controls import DataSource
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    recording_save_notice,
)
from lab_control.ui.pages.monitoring.timing import MonitoringTiming
from lab_control.ui.widgets.device_selector import DeviceSelector


class FrequencyCounterMonitoringPage(QWidget):
    """Display and record K+K frequency reports without changing the device."""

    source = DataSource("frequency", "频率", "scalar", "frequency", "frequency")

    def __init__(self, controller, add_card=None, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.add_card = controller, add_card
        self.device_id = ""
        self.channel_checks: dict[int, QCheckBox] = {}
        self.preferences = controller.runtime.repository.load_workspace(
            "monitoring_channels/frequency_counter", {}
        )
        if not isinstance(self.preferences, dict):
            self.preferences = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "frequency_counter", self, compact=True)
        header.addWidget(self.selector, 1)
        self.connection = QLabel()
        self.connection.setObjectName("sourceConnectionState")
        header.addWidget(self.connection)
        self.timing = MonitoringTiming(controller, self)
        header.addWidget(self.timing)
        self.toggle_button = QPushButton("开始监测")
        self.toggle_button.clicked.connect(self.toggle_monitoring)
        header.addWidget(self.toggle_button)
        layout.addLayout(header)

        self.empty = QLabel("请先从总览连接 K+K 计数器，再选择设备。")
        self.empty.setObjectName("mutedText")
        layout.addWidget(self.empty)

        self.channel_frame = QFrame()
        self.channel_frame.setObjectName("operationCard")
        channel_layout = QVBoxLayout(self.channel_frame)
        channel_layout.addWidget(QLabel("选择显示与记录通道（设备最多 24 路）"))
        self.channel_grid = QGridLayout()
        channel_layout.addLayout(self.channel_grid)
        layout.addWidget(self.channel_frame)

        self.chart = QFrame()
        self.chart.setObjectName("spectrumChart")
        chart_layout = QVBoxLayout(self.chart)
        title_row = QHBoxLayout()
        title = QLabel("频率趋势（Hz）")
        title.setObjectName("powerMeterTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        self.record_button = RecordingButton(self.chart)
        self.record_button.clicked.connect(self.record)
        title_row.addWidget(self.record_button)
        self.save_button = QPushButton("保存当前帧")
        self.save_button.clicked.connect(self.save)
        title_row.addWidget(self.save_button)
        chart_layout.addLayout(title_row)
        self.plot = LivePlot()
        chart_layout.addWidget(self.plot, 1)
        self.status = QLabel("监测未开始；只读采集不会修改 K+K 参数。")
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        chart_layout.addWidget(self.status)
        layout.addWidget(self.chart, 1)

        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def _key(self, device_id: str) -> str:
        record = self.controller.records.get(device_id, {})
        slot = record.get("slots", {}).get("general", "")
        return f"slot:general:{slot}" if slot else f"device:{device_id}"

    def channels(self) -> tuple[int, ...]:
        return tuple(channel for channel, check in self.channel_checks.items() if check.isChecked())

    def _save_channels(self) -> None:
        if self.device_id:
            self.preferences[self._key(self.device_id)] = list(self.channels())
            self.controller.runtime.repository.save_workspace(
                "monitoring_channels/frequency_counter", self.preferences
            )

    def _clear_channels(self) -> None:
        while self.channel_grid.count():
            item = self.channel_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def select_device(self, device_id: str) -> None:
        self.device_id = device_id
        self._clear_channels()
        self.channel_checks = {}
        saved = self.preferences.get(self._key(device_id), []) if device_id else []
        saved_set = set(saved) if isinstance(saved, (list, tuple)) else set()
        channels = tuple(self.controller.capabilities(device_id).channels) if device_id else ()
        for index, channel in enumerate(channels):
            check = QCheckBox(f"CH {channel}")
            check.setChecked(channel in saved_set if saved_set else True)
            check.toggled.connect(self.channels_changed)
            self.channel_checks[channel] = check
            self.channel_grid.addWidget(check, index // 8, index % 8)
        self.plot.reset()
        self.plot.setVisible(bool(device_id))
        self.channel_frame.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        self.timing.bind(device_id, "general")
        self.refresh()
        QTimer.singleShot(0, lambda: self.refresh_frame(device_id))

    def channels_changed(self) -> None:
        channels = self.channels()
        self.plot.reset()
        self._save_channels()
        if self.device_id and self.controller.runtime.monitoring.snapshots().get(self.device_id, {}).get("running"):
            try:
                requests = (
                    (AcquisitionRequest(kind="scalar", channels=channels, points=1, quantity="frequency"),)
                    if channels
                    else ()
                )
                self.controller.runtime.monitoring.replace_requests(self.device_id, requests)
            except Exception as exc:
                self.controller.message.emit(str(exc))
        self.refresh()

    def start(self) -> None:
        if not self.device_id or not self.channels():
            return
        try:
            record = self.controller.records.get(self.device_id)
            if not record or not mode_applicable(record, "general"):
                raise DeviceError("此设备不支持频率监测", "unsupported")
            self.timing.commit()
            self.controller.runtime.monitoring.start(
                self.device_id,
                (AcquisitionRequest(kind="scalar", channels=self.channels(), points=1, quantity="frequency"),),
                **self.timing.options(),
                mode="general",
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))
        self.refresh()

    def toggle_monitoring(self) -> None:
        task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
        if task.get("running") or task.get("pending"):
            self.controller.watch(self.controller.runtime.monitoring.stop(self.device_id), lambda _result: self.refresh())
        else:
            self.start()

    def record(self) -> None:
        try:
            task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
            self.controller.runtime.monitoring.set_recording(
                self.device_id, not task.get("recording"), mode="general"
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))
        self.refresh()

    def save(self) -> None:
        frame = self.plot.displayed_frame
        if frame is None:
            return
        try:
            future = self.controller.runtime.monitoring.export_displayed_frame(
                self.device_id, frame, "general"
            )
            self.controller.watch(future, lambda path: self.controller.message.emit(f"原始数据已保存 · 分析 CSV 已完成：{path}"))
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def focus_card(self, spec: dict) -> None:
        self.selector.select(spec["device_id"])
        wanted = set(spec.get("channels", ()))
        for channel, check in self.channel_checks.items():
            check.setChecked(channel in wanted)

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible() or not self.channels():
            return
        service = self.controller.runtime.monitoring
        frames = service.recent(device_id, "scalar", "frequency")
        if not frames:
            latest = service.latest(device_id, "scalar", "frequency")
            frames = (latest,) if latest else ()
        for frame in frames:
            self.plot.show_frame(frame, self.source, self.channels())
        if self.plot.displayed_frame is not None:
            service.displayed(device_id, self.plot.displayed_frame)

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
        connected = state.get("connectionState") in {"connected", "busy"}
        self.connection.setText("已连接" if connected else state.get("connectionState", "未连接"))
        draining = recording_phase(task) == "draining"
        self.toggle_button.setText("收尾中…" if draining else "停止监测" if task.get("running") else "开始监测")
        self.toggle_button.setEnabled(bool(connected and self.channels() and not draining))
        self.record_button.refresh(
            task,
            connected=connected,
            busy=draining,
            locked=bool(state.get("controlsLocked")),
        )
        self.save_button.setEnabled(self.plot.displayed_frame is not None and not task.get("display_saving"))
        self.status.setText(
            task.get("error")
            or task.get("recording_notice")
            or recording_save_notice(task)
            or ("监测中 · 正在逐帧记录" if task.get("recording") else "监测中 · 未保存数据" if task.get("running") else "监测未开始；只读采集不会修改 K+K 参数。")
        )
        self.timing.refresh(task)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
        self.refresh_frame(self.device_id)
