"""Fixed instrument grid; decimation affects the display, never the saved frame."""

from dataclasses import replace

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt

from lab_control.application.acquisition_pipeline import validate_displayed_batch
from lab_control.domain.instruments import DeviceError
from lab_control.domain.oscilloscope import COLORS, ScopeDisplay, engineering
from lab_control.ui.pages.monitoring.plot import LivePlot


class OscilloscopePlot(LivePlot):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.grid_lines: list = []
        self.grid_range: tuple | None = None
        self.instrument_view = False
        self.legend_signature: tuple | None = None

    def reset(self) -> None:
        super().reset()
        self.grid_lines.clear()
        self.grid_range = None
        self.instrument_view = False
        self.legend_signature = None

    def show_frame(self, frame, source, channels) -> None:
        wanted = tuple(m for m in frame.batch.measurements if m.channel in channels)
        if not wanted:
            return
        candidate = replace(frame, batch=replace(frame.batch, measurements=wanted))
        try:
            validate_displayed_batch(candidate.batch)
        except DeviceError:
            return
        if not any("scope_display" in m.metadata for m in wanted):
            if self.instrument_view:
                self.reset()
            self.plot.showAxis("left")
            self.plot.showGrid(x=True, y=True, alpha=0.16)
            self.plot.setMouseEnabled(x=True, y=True)
            self.plot.setMenuEnabled(True)
            super().show_frame(frame, source, channels)
            return
        try:
            displays = [ScopeDisplay.parse(m.metadata.get("scope_display", {})) for m in wanted]
            first = displays[0]
            for m, spec in zip(wanted, displays, strict=True):
                if m.axis is None or m.axis_unit != "s" or m.unit != spec.unit:
                    raise DeviceError("Waveform/display units differ", "parse_error")
                if (spec.seconds_per_div, spec.time_center) != (first.seconds_per_div, first.time_center):
                    raise DeviceError("Waveform horizontal displays differ", "parse_error")
            with np.errstate(over="ignore", invalid="ignore"):
                coordinates = [
                    (np.asarray(m.values) - spec.reference_value) / spec.units_per_div
                    for m, spec in zip(wanted, displays, strict=True)
                ]
            if any(not np.isfinite(y).all() for y in coordinates):
                raise DeviceError("Non-finite display coordinates", "parse_error")
        except DeviceError:
            return
        token = (frame.session_id, frame.sequence, frame.configuration_version, frame.received_at, channels)
        if token == self.last_token:
            return
        if not self.instrument_view:
            self.reset()
            self.instrument_view = True
        self.last_token = token
        bounds = (
            first.time_center - 5 * first.seconds_per_div,
            first.time_center + 5 * first.seconds_per_div,
        )
        # Establish the microsecond viewport before pyqtgraph computes downsampling.
        if bounds != self.grid_range:
            self.grid_range = bounds
            self.fit_current()
        self.plot.hideAxis("left")
        self.plot.setLabel("bottom", "时间", units="s")
        self.plot.showGrid(x=False, y=False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setMenuEnabled(False)
        self.value.hide()
        if not self.grid_lines:
            pen = pg.mkPen((165, 185, 189, 42), width=1, style=Qt.PenStyle.DashLine)
            for angle, count in ((90, 11), (0, 9)):
                for _ in range(count):
                    line = pg.InfiniteLine(angle=angle, pen=pen, movable=False)
                    line.setZValue(-10)
                    self.plot.addItem(line, ignoreBounds=True)
                    self.grid_lines.append(line)
        for line, x in zip(self.grid_lines[:11], np.linspace(*self.grid_range, 11), strict=True):
            line.setPos(float(x))
        for line, grid_y in zip(self.grid_lines[11:], range(-4, 5), strict=True):
            line.setPos(grid_y)
        used = set()
        legend = self.plot.plotItem.legend
        signature = tuple(
            (m.channel, spec.units_per_div, m.unit) for m, spec in zip(wanted, displays, strict=True)
        )
        update_legend = signature != self.legend_signature
        if update_legend:
            legend.clear()
            self.legend_signature = signature
        for m, spec, y in zip(wanted, displays, coordinates, strict=True):
            key = (m.channel, m.quantity)
            color = COLORS[(m.channel - 1) % len(COLORS)]
            if key not in self.curves:
                curve = self.plot.plot(pen=pg.mkPen(color, width=1.2))
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self.curves[key] = curve
            curve = self.curves[key]
            curve.setData(
                m.axis,
                y,
                connect="finite",
                symbol="o" if len(m.values) == 1 else None,
                symbolSize=5,
            )
            curve.show()
            if update_legend:
                legend.addItem(
                    curve,
                    f'<span style="color:{color}">CH{m.channel}  {engineering(spec.units_per_div, m.unit, custom_scale=True)}/格</span>',
                )
            used.add(key)
        for key, curve in self.curves.items():
            if key not in used:
                curve.hide()
        self.meta.setText(
            f"{engineering(first.seconds_per_div, 's', custom_scale=True)}/格 · "
            + " / ".join(f"CH{m.channel} {len(m.values)} 点" for m in wanted)
            + (
                {"screen": " · 屏幕波形", "memory": " · 完整存储波形"}.get(
                    wanted[0].metadata.get("waveform_extent"), ""
                )
            )
            + f" · 配置 {frame.configuration_version} · {frame.batch.validation}"
        )
        self.displayed_frame = candidate

    def restrict_channels(self, channels: tuple[int, ...]) -> None:
        """Drop explicitly disabled traces without changing the old frame's scales."""
        if self.displayed_frame is None:
            return
        remaining = tuple(m.channel for m in self.displayed_frame.batch.measurements if m.channel in channels)
        if len(remaining) == len(self.displayed_frame.batch.measurements):
            return
        if remaining:
            from lab_control.application.controls import DataSource

            self.show_frame(self.displayed_frame, DataSource("waveform", "时域波形", "waveform"), remaining)
        else:
            for curve in self.curves.values():
                curve.hide()
            self.plot.plotItem.legend.clear()
            self.legend_signature = None
            self.displayed_frame = None
            self.last_token = None
            self.meta.setText("所选通道已关闭 · 无显示波形")

    def fit_current(self) -> None:
        if self.grid_range:
            self.plot.setRange(xRange=self.grid_range, yRange=(-4, 4), padding=0, disableAutoRange=True)
        else:
            super().fit_current()
