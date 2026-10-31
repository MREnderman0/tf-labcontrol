"""Focused phase-noise workspace; uses capabilities and recorded-run contracts."""

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.ui.pages.monitoring.phase_controls import FrequencySetting
from lab_control.ui.pages.monitoring.recording_state import update_recording_frame
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.parameter_combo import ParameterComboBox


class LogAxis(pg.AxisItem):
    def logTickStrings(self, values, scale, spacing):  # noqa: N802
        # Plain scientific notation avoids missing superscript glyphs and crowded minor labels.
        return [
            (f"1e{round(value)}" if abs(value) > 3 else f"{10 ** round(value):g}")
            if abs(value - round(value)) < 1e-7
            else ""
            for value in values
        ]


class PhaseNoiseMonitoringPage(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.device_id = ""
        self.last_event = None
        self.starting = False
        self.paths: dict[str, str] = {}
        self.outcomes: dict[str, str] = {}
        self.editors: list[FrequencySetting] = []
        self.run_tokens: dict[str, int] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "phase_noise_analyzer", self, compact=True)
        header.addWidget(self.selector, 1)
        self.start_button = QPushButton("开始采集")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("停止采集")
        self.stop_button.clicked.connect(self.stop)
        header.addWidget(self.start_button)
        header.addWidget(self.stop_button)
        layout.addLayout(header)
        body = QHBoxLayout()
        layout.addLayout(body, 1)
        self.chart_frame = QFrame()
        self.chart_frame.setObjectName("spectrumChart")
        charts = QVBoxLayout(self.chart_frame)
        body.addWidget(self.chart_frame, 1)
        self.pn_plot = self.plot("相位噪声", "偏移频率", "Hz", "单边带相噪", "dBc/Hz", False)
        self.stability_plot = self.plot("频率稳定度", "平均时间 τ", "s", "分数频率偏差", "", True)
        charts.addWidget(self.pn_plot, 1)
        charts.addWidget(self.stability_plot, 1)
        self.curves = {
            "phase_noise": self.pn_plot.plot(pen=pg.mkPen("#e3a252", width=2), name="相噪"),
            "noise_floor": self.pn_plot.plot(pen=pg.mkPen("#6e8492", width=1), name="估计底噪"),
            "adev": self.stability_plot.plot(
                pen=pg.mkPen("#e3a252", width=2), symbol="o", symbolSize=5, name="ADEV · 重叠"
            ),
            "mdev": self.stability_plot.plot(
                pen=pg.mkPen("#69c9d4", width=2), symbol="s", symbolSize=5, name="MDEV"
            ),
        }
        self.side = QFrame()
        self.side.setObjectName("operationCard")
        self.side.setMinimumWidth(290)
        self.side.setMaximumWidth(340)
        controls = QVBoxLayout(self.side)
        title = QLabel("采集设置")
        title.setObjectName("controlTitle")
        controls.addWidget(title)
        form = QFormLayout()
        self.rate = ParameterComboBox()
        self.duration = QSpinBox()
        self.duration.setRange(10, 86400)
        self.duration.setValue(60)
        self.duration.setSuffix(" 秒")
        form.addRow("相位采样率", self.rate)
        form.addRow("采集时长", self.duration)
        controls.addLayout(form)
        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        self.estimate.setObjectName("mutedText")
        controls.addWidget(self.estimate)
        self.rate.currentIndexChanged.connect(self.estimate_samples)
        self.duration.valueChanged.connect(self.estimate_samples)
        self.frequency_panel = QWidget()
        self.frequency_layout = QVBoxLayout(self.frequency_panel)
        self.frequency_layout.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self.frequency_panel)
        note = QLabel(
            "接好 DUT 与参考信号后开始。\n相噪读取仪器频谱；ADEV / MDEV 在结束后由同一段连续相位数据计算。"
        )
        note.setWordWrap(True)
        note.setObjectName("mutedText")
        controls.addWidget(note)
        controls.addStretch()
        self.progress = QProgressBar()
        self.progress.setObjectName("phaseProgress")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        controls.addWidget(self.progress)
        self.status = QLabel("选择已连接的相噪仪")
        self.status.setWordWrap(True)
        controls.addWidget(self.status)
        self.folder = QPushButton("打开数据目录")
        self.folder.clicked.connect(self.open_folder)
        controls.addWidget(self.folder)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.side)
        scroll.setMinimumWidth(310)
        scroll.setMaximumWidth(360)
        body.addWidget(scroll)
        self.notice = QLabel("原始相位、相噪和稳定度结果随采集自动保存；到时自动停止。")
        self.notice.setObjectName("mutedText")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    @staticmethod
    def plot(title, xlabel, xunit, ylabel, yunit, log_y):
        axes = {"bottom": LogAxis(orientation="bottom")}
        if log_y:
            axes["left"] = LogAxis(orientation="left")
        chart = pg.PlotWidget(background="#131c21", axisItems=axes)
        chart.setTitle(title, color="#edf0ee", size="12pt")
        chart.setLabel("bottom", xlabel, units=xunit)
        chart.setLabel("left", ylabel, units=yunit)
        chart.showGrid(x=True, y=True, alpha=0.2)
        chart.setLogMode(x=True, y=log_y)
        chart.getAxis("bottom").enableAutoSIPrefix(False)
        chart.getAxis("left").enableAutoSIPrefix(False)
        chart.addLegend(offset=(10, 10))
        chart.setMinimumSize(340, 180)
        return chart

    def select_device(self, device_id):
        if self.device_id == device_id and self.editors:
            return
        self.device_id = device_id
        self.last_event = None
        for curve in self.curves.values():
            curve.clear()
        for editor in self.editors:
            self.frequency_layout.removeWidget(editor)
            editor.deleteLater()
        self.editors.clear()
        self.rate.clear()
        self.supported = False
        if device_id:
            caps = self.controller.capabilities(device_id)
            try:
                caps.get("timed_phase")
                sample = caps.get("sample_rate")
                for rate in sample.parameters["value"].choices:
                    self.rate.addItem(f"{int(float(rate)):,} 次/秒", int(float(rate)))
                self.rate.setCurrentIndex(max(0, self.rate.findData(100)))
                self.supported = True
            except DeviceError:
                self.status.setText("当前连接暂不支持定时相位采集；本机 USB 托管连接可用。")
            for name, label in (("input_frequency", "DUT 输入频率"), ("reference_frequency", "参考频率")):
                try:
                    caps.get(name)
                    editor = FrequencySetting(self.controller, device_id, name, label)
                except DeviceError:
                    continue
                self.frequency_layout.addWidget(editor)
                self.editors.append(editor)
        self.estimate_samples()
        if self.isVisible():
            self.focus_settings()
        self.refresh()

    def focus_settings(self):
        self.controller.focus_settings(
            self.device_id, ((1, "input_frequency"), (1, "reference_frequency"), (1, "sample_rate"))
        )

    def estimate_samples(self):
        rate = self.rate.currentData() or 1
        count = self.duration.value() * rate + 1
        self.estimate.setText(
            f"采样间隔 {1 / rate:g} s · 预计 {count:,} 点\n时长从首个有效相位点开始计算。"
            + ("\n超过 4,000,000 点，请缩短时长。" if count > 4_000_000 else "")
        )

    def start(self):
        if not self.device_id or self.starting:
            return
        key = self.device_id
        token = self.run_tokens.get(key, 0) + 1
        try:
            self.controller.ensure_applicable(key)
            request = AcquisitionRequest(
                "timed_phase",
                points=1000,
                initiate=True,
                confirmed=True,
                sample_rate=self.rate.currentData(),
                duration=self.duration.value(),
            )
            if any(editor.dirty or editor.busy for editor in self.editors):
                raise DeviceError("请先应用频率设置，再开始采集", "task_busy")
            self.controller.clear_recorded_frame(key)
            future = self.controller.runtime.sessions.start_acquisition(
                key, request, mode=self.controller.mode
            )
            self.starting = True
            self.run_tokens[key] = token
            self.last_event = None
            self.outcomes[key] = "正在初始化，等待首个有效相位点…"
            for curve in self.curves.values():
                curve.clear()
            self.progress.setValue(0)
            self.controller.watch(
                future,
                lambda paths: self.finished(key, paths, token),
                lambda exc: self.failed(key, exc, token),
            )
        except Exception as exc:
            self.failed(key, exc)
        self.refresh()

    def stop(self):
        if self.device_id:
            self.controller.runtime.sessions.stop_acquisition(self.device_id)
            self.outcomes[self.device_id] = "正在停止仪器并保存已收到的数据…"
            self.refresh()

    def finished(self, key, paths, token=None):
        if token is not None and token != self.run_tokens.get(key):
            return
        self.starting = False
        if paths:
            folder = Path(paths[0]).parent
            self.paths[key] = str(folder.parent if folder.name == "_raw" else folder)
        event = self.controller.recorded_frame(key)
        config = event["batch"].configuration if event else {}
        if config.get("final"):
            text = "采集完成" if config.get("complete") else "已提前停止 · 部分时段结果"
            if not config.get("phase_noise_available"):
                text += "\n本次尚未取得有效相噪频谱"
            if not config.get("stability_available"):
                text += "\n样本不足，未计算稳定度"
            self.outcomes[key] = text + "\n已保存原始数据与计算结果"
        else:
            self.outcomes[key] = "采集已停止；查看记录状态，结果可能不完整"
        self.refresh()

    def failed(self, key, exc, token=None):
        if token is not None and token != self.run_tokens.get(key):
            return
        self.starting = False
        self.outcomes[key] = "采集未完成：" + str(exc)
        self.refresh()

    def refresh(self):
        if not self.isVisible():
            return
        state = self.controller.states.get(self.device_id, {})
        active = state.get("activeTask") == "continuous_acquisition"
        if active:
            self.starting = False
        busy = active or self.starting
        recorder = state.get("recordingState", {})
        update_recording_frame(
            self.chart_frame,
            {"recording": active and recorder.get("accepting", False), "recording_state": recorder},
        )
        if recorder.get("folder"):
            self.paths[self.device_id] = recorder["folder"]
        self.selector.setEnabled(not busy)
        self.rate.setEnabled(not busy)
        self.duration.setEnabled(not busy)
        self.frequency_panel.setEnabled(
            not busy
            and not state.get("controlsLocked")
            and state.get("connectionState") in {"connected", "busy"}
        )
        self.start_button.setEnabled(
            bool(
                self.device_id
                and self.supported
                and not busy
                and state.get("connectionState") == "connected"
                and not state.get("controlPending")
                and not any(editor.dirty or editor.busy for editor in self.editors)
            )
        )
        self.stop_button.setEnabled(busy)
        self.folder.setEnabled(self.device_id in self.paths)
        if state.get("recovery"):
            self.status.setText("通信中断 · 正在自动重连，恢复后继续采集")
        elif self.device_id in self.outcomes:
            self.status.setText(self.outcomes[self.device_id])
        elif self.supported:
            self.status.setText("准备就绪 · 等待开始")
        self.refresh_frame(self.device_id)

    def refresh_frame(self, key):
        if not self.isVisible() or key != self.device_id:
            return
        event = self.controller.recorded_frame(key)
        if event is None or event is self.last_event:
            return
        self.last_event = event
        batch = event["batch"]
        config = batch.configuration
        elapsed, duration = config.get("elapsed", 0), config.get("requested_duration", 1)
        self.progress.setValue(round(min(1, elapsed / duration) * 1000))
        if not config.get("final"):
            self.outcomes[key] = (
                f"采集中 · {elapsed:g} / {duration:g} 秒\n{config.get('samples', 0):,} 点 · {config.get('sample_rate', 0):g} 次/秒"
            )
            self.status.setText(self.outcomes[key])
        elif not self.outcomes.get(key, "").startswith(("采集完成", "已提前停止", "采集未完成")):
            self.starting = False
            self.outcomes[key] = "仪器已停止 · 正在完成数据保存…"
            self.status.setText(self.outcomes[key])
        if event.get("path"):
            folder = Path(event["path"]).parent
            self.paths[key] = str(folder.parent if folder.name == "_raw" else folder)
        for measurement in batch.measurements:
            curve = self.curves.get(measurement.quantity)
            if curve is None:
                continue
            x, y = np.asarray(measurement.axis), np.asarray(measurement.values)
            if x.shape != y.shape or not np.isfinite(x).all() or not np.isfinite(y).all():
                continue
            mask = x > 0
            if measurement.quantity in ("adev", "mdev"):
                mask &= y > 0  # Zero deviations are preserved on disk, undefined on logarithmic axes.
            curve.setData(x[mask], y[mask])

    def open_folder(self):
        path = self.paths.get(self.device_id)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.focus_settings()
        self.refresh()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
