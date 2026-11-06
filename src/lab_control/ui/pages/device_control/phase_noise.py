"""One-click finite phase capture in device management."""

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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.storage.layout import raw_folder
from lab_control.storage.recording import read_frames
from lab_control.ui.pages.monitoring.recording_state import update_recording_frame
from lab_control.ui.widgets.device_selector import DeviceSelector

SAMPLE_RATE = 1000
DURATION = 60


def load_results(folder: Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Read full committed captures off the device worker, including hidden-page samples."""
    phase_x, phase_y = [], []
    curves = {}
    previous_epoch = None
    count = 0
    for path in sorted(raw_folder(folder).glob("segment-*.sqlite3")):
        for frame in read_frames(path):
            epoch = frame["configuration"].get("acquisition_epoch", 0)
            for measurement in frame["measurements"]:
                quantity = measurement["quantity"]
                if quantity not in {"phase_difference", "phase_noise", "noise_floor", "adev", "mdev"}:
                    continue
                x, y = np.asarray(measurement["axis"]), np.asarray(measurement["values"])
                if x.shape != y.shape or not np.isfinite(x).all() or not np.isfinite(y).all():
                    raise ValueError("保存的曲线包含无效坐标或数据")
                if quantity == "phase_difference":
                    count += x.size
                    if count > 4_000_000:
                        raise ValueError("相位记录超过支持的点数上限")
                    if previous_epoch is not None and epoch != previous_epoch:
                        phase_x.append(np.array([np.nan]))
                        phase_y.append(np.array([np.nan]))
                    previous_epoch = epoch
                    phase_x.append(x)
                    phase_y.append(y)
                else:
                    curves[quantity] = (x, y)
    if phase_x:
        curves["phase_difference"] = (np.concatenate(phase_x), np.concatenate(phase_y))
    return curves


class LogAxis(pg.AxisItem):
    def logTickStrings(self, values, scale, spacing):  # noqa: N802
        # Plain scientific notation avoids missing superscript glyphs and crowded minor labels.
        return [
            (f"1e{round(value)}" if abs(value) > 3 else f"{10 ** round(value):g}")
            if abs(value - round(value)) < 1e-7
            else ""
            for value in values
        ]


class PhaseNoisePage(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.device_id = ""
        self.last_event = None
        self.starting: set[str] = set()
        self.paths: dict[str, str] = {}
        self.outcomes: dict[str, str] = {}
        self.results: dict[str, dict] = {}
        self.run_tokens: dict[str, int] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        header = QHBoxLayout()
        self.selector = DeviceSelector(
            controller, "phase_noise_analyzer", self, compact=True, surface="management"
        )
        header.addWidget(self.selector, 1)
        self.start_button = QPushButton("一键采集 · 1 分钟")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("提前停止")
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
        self.phase_plot = self.plot("原始相位", "时间", "s", "相位差", "s", False, log_x=False)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.phase_plot, "原始相位")
        self.tabs.addTab(self.pn_plot, "相位噪声")
        charts.addWidget(self.tabs, 1)
        charts.addWidget(self.stability_plot, 1)
        self.curves = {
            "phase_difference": self.phase_plot.plot(pen=pg.mkPen("#e3a252", width=1), connect="finite"),
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
        title = QLabel("单设备采集")
        title.setObjectName("controlTitle")
        controls.addWidget(title)
        form = QFormLayout()
        for label, value in (
            ("DUT 频率", "Auto"),
            ("参考频率", "Auto"),
            ("相位采样率", "1,000 点/秒"),
            ("采集时长", "1 分钟（60 秒）"),
        ):
            form.addRow(label, QLabel(value))
        controls.addLayout(form)
        self.estimate = QLabel("预计 60,001 点，覆盖完整 60 秒。\n时长从首个有效相位点开始计算。")
        self.estimate.setWordWrap(True)
        self.estimate.setObjectName("mutedText")
        controls.addWidget(self.estimate)
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
        self.folder = QPushButton("打开单设备采集目录")
        self.folder.clicked.connect(self.open_folder)
        controls.addWidget(self.folder)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.side)
        scroll.setMinimumWidth(310)
        scroll.setMaximumWidth(360)
        body.addWidget(scroll)
        self.notice = QLabel("自动保存原始相位、相噪、ADEV 和 MDEV；结束后显示结果，CSV 在后台生成。")
        self.notice.setObjectName("mutedText")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    @staticmethod
    def plot(title, xlabel, xunit, ylabel, yunit, log_y, *, log_x=True):
        axes = {"bottom": LogAxis(orientation="bottom")} if log_x else {}
        if log_y:
            axes["left"] = LogAxis(orientation="left")
        chart = pg.PlotWidget(background="#131c21", axisItems=axes)
        chart.setTitle(title, color="#edf0ee", size="12pt")
        chart.setLabel("bottom", xlabel, units=xunit)
        chart.setLabel("left", ylabel, units=yunit)
        chart.showGrid(x=True, y=True, alpha=0.2)
        chart.setLogMode(x=log_x, y=log_y)
        chart.getAxis("bottom").enableAutoSIPrefix(False)
        if log_y:
            chart.getAxis("left").enableAutoSIPrefix(False)
        chart.addLegend(offset=(10, 10))
        chart.setMinimumSize(340, 180)
        return chart

    def select_device(self, device_id):
        self.device_id, self.last_event = device_id, None
        self.supported = False
        if device_id:
            try:
                caps = self.controller.capabilities(device_id)
                caps.get("timed_phase")
                caps.get("sample_rate").parameters["value"].validate("sample_rate", SAMPLE_RATE)
                self.supported = True
            except DeviceError:
                pass
        self.progress.setValue(0)
        self.show_results()
        self.refresh()

    def start(self):
        if not self.device_id or self.device_id in self.starting:
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
                sample_rate=SAMPLE_RATE,
                duration=DURATION,
            )
            self.controller.clear_recorded_frame(key)
            future = self.controller.runtime.sessions.start_acquisition(
                key, request, mode=self.controller.mode
            )
            self.results.pop(key, None)
            self.paths.pop(key, None)
            self.starting.add(key)
            self.run_tokens[key] = token
            self.last_event = None
            self.outcomes[key] = "正在自动设置，等待首个有效相位点…"
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
        self.starting.discard(key)
        if paths:
            folder = Path(paths[0]).parent
            self.paths[key] = str(folder.parent if folder.name == "_raw" else folder)
        event = self.controller.recorded_frame(key)
        config = event["batch"].configuration if event else {}
        if config.get("final"):
            text = "采集完成" if config.get("complete") else "已提前停止 · 部分时段结果"
            if config.get("has_data_gaps"):
                text += "\n存在通信缺口；ADEV / MDEV 仅对应最后连续数据段"
            if not config.get("phase_noise_available"):
                text += "\n本次尚未取得有效相噪频谱"
            if not config.get("stability_available"):
                text += "\n样本不足，未计算稳定度"
            self.outcomes[key] = text + "\n原始数据与计算结果已保存 · 正在加载曲线"
        else:
            self.outcomes[key] = "采集已停止；查看记录状态，结果可能不完整"
        if key in self.paths:
            future = self.controller.runtime.processing.read_worker.submit(
                load_results, Path(self.paths[key])
            )
            self.controller.watch(
                future,
                lambda curves: self.results_loaded(key, curves, token),
                lambda exc: self.result_failed(key, exc, token),
            )
        self.refresh()

    def failed(self, key, exc, token=None):
        if token is not None and token != self.run_tokens.get(key):
            return
        self.starting.discard(key)
        self.outcomes[key] = "采集未完成：" + str(exc)
        self.refresh()

    def refresh(self):
        if not self.isVisible():
            return
        state = self.controller.states.get(self.device_id, {})
        active = state.get("activeTask") == "continuous_acquisition"
        if active:
            self.starting.discard(self.device_id)
        busy = active or self.device_id in self.starting
        recorder = state.get("recordingState", {})
        update_recording_frame(
            self.chart_frame,
            {"recording": active and recorder.get("accepting", False), "recording_state": recorder},
        )
        if recorder.get("folder"):
            self.paths[self.device_id] = recorder["folder"]
        self.selector.setEnabled(not busy)
        self.start_button.setEnabled(
            bool(
                self.device_id
                and self.supported
                and not busy
                and state.get("connectionState") == "connected"
                and not state.get("controlPending")
                and not state.get("controlsLocked")
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
            self.starting.discard(key)
            self.outcomes[key] = "仪器已停止 · 正在完成数据保存…"
            self.status.setText(self.outcomes[key])
        if event.get("path"):
            folder = Path(event["path"]).parent
            self.paths[key] = str(folder.parent if folder.name == "_raw" else folder)

    def results_loaded(self, key, curves, token):
        if token != self.run_tokens.get(key):
            return
        self.results[key] = curves
        self.outcomes[key] = self.outcomes[key].replace("正在加载曲线", "结果已显示")
        if key == self.device_id:
            self.show_results()
        self.refresh()

    def result_failed(self, key, exc, token):
        if token == self.run_tokens.get(key):
            self.outcomes[key] += "\n结果显示失败：" + str(exc)
            self.refresh()

    def show_results(self):
        results = self.results.get(self.device_id, {})
        for quantity, curve in self.curves.items():
            curve.clear()
            if quantity not in results:
                continue
            x, y = results[quantity]
            if quantity != "phase_difference":
                mask = x > 0
                if quantity in {"adev", "mdev"}:
                    mask &= y > 0  # Zero deviations remain in the files.
                x, y = x[mask], y[mask]
            curve.setData(x, y)

    def open_folder(self):
        path = self.paths.get(self.device_id)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.show_results()
        self.refresh()
