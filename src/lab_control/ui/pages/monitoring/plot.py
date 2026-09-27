from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from datetime import datetime
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from lab_control.application.acquisition_pipeline import validate_displayed_batch
from lab_control.application.controls import DataSource
from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import DeviceError
from lab_control.ui.pages.monitoring.trace_view import TraceView


class MonitoringPlotItem(pg.PlotItem):
    """Trace fit uses the application's one-shot policy, not continuous autorange."""

    fit_requested = Signal()
    one_shot_fit = False

    def autoBtnClicked(self) -> None:  # noqa: N802
        if self.one_shot_fit:
            self.fit_requested.emit()
        else:
            super().autoBtnClicked()


class LivePlot(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.value = QLabel("—")
        self.value.setObjectName("liveValue")
        self.value.setWordWrap(True)
        layout.addWidget(self.value)
        self.plot_item = MonitoringPlotItem()
        self.plot_item.fit_requested.connect(self.fit_current)
        self.plot = pg.PlotWidget(background="#131c21", plotItem=self.plot_item)
        self.plot.showGrid(x=True, y=True, alpha=0.16)
        self.plot.addLegend(offset=(8, 8))
        self.plot.setMinimumSize(150, 170)
        layout.addWidget(self.plot, 1)
        self.meta = QLabel("尚无数据 · 选择设备后开始监测")
        self.meta.setWordWrap(True)
        self.meta.setObjectName("mutedText")
        layout.addWidget(self.meta)
        self.curves: dict[tuple[int, str], Any] = {}
        self.history: dict[tuple[int, str], deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=1200)
        )
        self.last_token: tuple | None = None
        self.unit_signature: tuple[str, str] | None = None
        self.axis_signature: tuple | None = None
        self.displayed_frame: MonitorFrame | None = None
        self.trace_view = TraceView(self.plot)

    def reset(self) -> None:
        self.plot.clear()
        self.curves.clear()
        self.history.clear()
        self.last_token = None
        self.unit_signature = None
        self.axis_signature = None
        self.displayed_frame = None
        self.trace_view.reset()
        self.plot_item.one_shot_fit = False
        self.plot_item.showButtons()
        self.plot.enableAutoRange()
        self.value.setText("—")
        self.meta.setText("尚无数据")

    def show_frame(self, frame: MonitorFrame, source: DataSource, channels: tuple[int, ...]) -> None:
        token = (
            frame.session_id,
            frame.sequence,
            frame.received_at,
            frame.batch.timestamp,
            frame.configuration_version,
            source.key,
            channels,
        )
        if token == self.last_token:
            return
        wanted = [
            m
            for m in frame.batch.measurements
            if m.channel in channels and (not source.measurement or m.quantity == source.measurement)
        ]
        candidate = replace(frame, batch=replace(frame.batch, measurements=tuple(wanted)))
        valid = True
        try:
            validate_displayed_batch(candidate.batch)
        except DeviceError:
            # A failed update must not replace the valid snapshot still on screen.
            if any(np.asarray(m.values).size for m in wanted):
                return
            valid = False  # Preserve the existing "unknown" scalar display.
        self.last_token = token
        stable_trace = source.kind in {"spectrum", "waveform"}
        self.plot_item.one_shot_fit = stable_trace
        if stable_trace and valid:
            # Fit raw arrays first: peak decimation at the initial seconds-wide
            # viewport can turn a microsecond trace into zero display points.
            points = [(np.asarray(m.axis), np.asarray(m.values)) for m in wanted if m.axis is not None]
            self.trace_view.update(
                candidate, source.key, channels, points, waveform=source.kind == "waveform"
            )
        used = set()
        values_text = []
        for measurement in wanted:
            values = np.asarray(measurement.values)
            key = (measurement.channel, measurement.quantity)
            if not values.size:
                text = measurement.metadata.get("display_value")
                values_text.append("未知" if text is None else str(text))
                continue
            unit = measurement.unit
            if np.iscomplexobj(values):
                if source.view == "phase":
                    values, unit = np.angle(values, deg=True), "°"
                else:
                    with np.errstate(divide="ignore"):
                        values = 20 * np.log10(np.abs(values))
                    unit = "dB"
            signature = (unit, measurement.axis_unit)
            if self.unit_signature is not None and self.unit_signature != signature:
                self.history.clear()
            self.unit_signature = signature
            if measurement.axis is None:
                now = datetime.fromisoformat(frame.batch.timestamp).timestamp()
                history = self.history[key]
                history.append((now, float(values[-1])))
                while history and now - history[0][0] > 600:
                    history.popleft()
                x, y = np.asarray(history).T
                x = x - now
                axis_label, axis_unit = "相对当前时间", "s"
            else:
                x, y = np.asarray(measurement.axis), values
                axis_unit = measurement.axis_unit
                axis_label = {"s": "时间", "Hz": "频率", "nm": "波长"}.get(axis_unit, "坐标")
            if key not in self.curves:
                color = ("#e3a252", "#69c9d4", "#b5d979", "#cda8e8")[(measurement.channel - 1) % 4]
                curve = self.plot.plot(pen=pg.mkPen(color, width=1), name=f"CH {measurement.channel}")
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self.curves[key] = curve
            self.curves[key].setData(
                x, y, connect="finite", symbol="o" if len(x) == 1 else None, symbolSize=5
            )
            self.curves[key].show()
            used.add(key)
            log_x = source.quantity in {"phase_noise", "am_noise", "stability"} and bool(np.all(x > 0))
            axis_signature = (
                axis_label,
                axis_unit,
                source.label,
                unit,
                log_x,
                source.quantity == "stability",
            )
            if axis_signature != self.axis_signature:
                self.plot.setLabel("bottom", axis_label, units=axis_unit or None)
                self.plot.setLabel("left", source.label, units=unit or None)
                self.plot.setLogMode(x=log_x, y=source.quantity == "stability")
                self.axis_signature = axis_signature
            values_text.append(f"CH {measurement.channel}  {values[-1]:.6g} {unit}")
        for key, curve in self.curves.items():
            if key not in used:
                curve.hide()
        self.value.setText("   ·   ".join(values_text) or "没有所选通道的数据")
        self.value.setVisible(source.view == "number" or any(len(m.values) <= 1 for m in wanted))
        quality = " / ".join(sorted({m.quality.value for m in wanted}))
        count = (
            " · 实收 " + " / ".join(f"CH {m.channel}: {len(m.values)} 点" for m in wanted)
            if stable_trace
            else ""
        )
        complete = (
            " · 按仪器完整波形读取"
            if frame.batch.performance.get("requested_points_is_not_a_hardware_limit")
            else ""
        )
        self.meta.setText(
            f"{frame.batch.timestamp} · {quality} · 配置版本 {frame.configuration_version}{count}{complete}"
        )
        self.displayed_frame = candidate if valid else None

    def fit_current(self) -> None:
        if self.trace_view.active:
            self.trace_view.fit()
        else:
            self.plot.autoRange()
