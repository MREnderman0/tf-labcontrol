"""Fixed instrument grid; decimation affects the display, never the saved frame."""

from dataclasses import replace

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTransform

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
        self._restoring_grid = False
        self.preview_targets: dict[tuple[int, str], tuple[float, int | None]] = {}
        self.preview_session = ""
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(16)
        self.preview_timer.timeout.connect(self.render_preview)
        self.actual_meta = ""
        self.plot.getViewBox().sigStateChanged.connect(self._keep_instrument_view)

    def reset(self) -> None:
        self.preview_timer.stop()
        self.preview_targets.clear()
        self.instrument_view = False
        self.grid_range = None
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
        if self.preview_session != frame.session_id:
            self.preview_targets.clear()
        self.preview_targets = {
            key: target
            for key, target in self.preview_targets.items()
            if target[1] is None or target[1] > frame.configuration_version
        }
        if not self.instrument_view:
            self.reset()
            self.instrument_view = True
        self.plot_item.one_shot_fit = True
        self.plot_item.hideButtons()
        self.last_token = token
        bounds = (
            first.time_center - 5 * first.seconds_per_div,
            first.time_center + 5 * first.seconds_per_div,
        )
        # Establish the microsecond viewport before pyqtgraph computes downsampling.
        if bounds != self.grid_range:
            self.grid_range = bounds
            self.fit_current()
        self._keep_instrument_view()
        self.plot.hideAxis("left")
        self.plot.setLabel("bottom", "时间", units="s")
        self.plot.showGrid(x=False, y=False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setMenuEnabled(False)
        self.value.hide()
        if not self.grid_lines:
            for angle, count in ((90, 11), (0, 9)):
                for index in range(count):
                    alpha = 110 if index == count // 2 else 78
                    pen = pg.mkPen((165, 185, 189, alpha), width=1, style=Qt.PenStyle.DashLine)
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
            curve.setTransform(QTransform())
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
        self.actual_meta = self.meta.text()
        if self.preview_targets:
            self.render_preview()

    def preview_parameter(self, channel: int, operation: str, value: float) -> None:
        """Estimated viewport only: never replace the immutable exported frame."""
        if not self.instrument_view or self.displayed_frame is None or not np.isfinite(value):
            return
        if operation not in {"scale", "offset", "timebase", "time_position"}:
            return
        if operation in {"scale", "timebase"} and value <= 0:
            return
        self.preview_session = self.displayed_frame.session_id
        self.preview_targets[channel, operation] = (value, None)
        if not self.preview_timer.isActive():
            self.preview_timer.start()

    def commit_preview(self, version: int) -> None:
        self.preview_targets = {
            key: (value, version if pending is None else pending)
            for key, (value, pending) in self.preview_targets.items()
        }
        self.render_preview()

    def clear_preview(self) -> None:
        if not self.preview_targets:
            self.fit_current()
            return
        self.preview_timer.stop()
        self.preview_targets.clear()
        self.render_preview()

    def render_preview(self) -> None:
        if not self.instrument_view or self.displayed_frame is None:
            return
        specs = []
        used = False
        for measurement in self.displayed_frame.batch.measurements:
            original = ScopeDisplay.parse(measurement.metadata["scope_display"])
            values = {}
            for (ch, operation), (value, _version) in self.preview_targets.items():
                if operation == "scale" and ch == measurement.channel:
                    values["units_per_div"] = value
                elif operation == "offset" and ch == measurement.channel and original.vertical_offset_sign:
                    values["reference_value"] = original.vertical_offset_sign * value
                elif operation == "timebase":
                    values["seconds_per_div"] = value
                elif operation == "time_position" and original.horizontal_position_sign:
                    values["time_center"] = original.horizontal_position_sign * value
            used |= bool(values)
            try:
                spec = replace(
                    original,
                    units_per_div=values.get("units_per_div", original.units_per_div),
                    reference_value=values.get("reference_value", original.reference_value),
                    seconds_per_div=values.get("seconds_per_div", original.seconds_per_div),
                    time_center=values.get("time_center", original.time_center),
                )
            except ValueError:
                return
            specs.append((measurement, spec))
            curve = self.curves.get((measurement.channel, measurement.quantity))
            if curve is not None:
                # Affine scene transform reuses the existing curve/decimation;
                # mouse movement does not allocate or rewrite full sample arrays.
                transform = QTransform()
                transform.translate(0, (original.reference_value - spec.reference_value) / spec.units_per_div)
                transform.scale(1, original.units_per_div / spec.units_per_div)
                curve.setTransform(transform)
        if not specs:
            return
        first = specs[0][1]
        self.grid_range = (
            first.time_center - 5 * first.seconds_per_div,
            first.time_center + 5 * first.seconds_per_div,
        )
        self.fit_current()
        for line, x in zip(self.grid_lines[:11], np.linspace(*self.grid_range, 11), strict=True):
            line.setPos(float(x))
        signature = tuple((m.channel, spec.units_per_div, m.unit) for m, spec in specs)
        if signature != self.legend_signature:
            legend = self.plot.plotItem.legend
            legend.clear()
            for m, spec in specs:
                color = COLORS[(m.channel - 1) % len(COLORS)]
                legend.addItem(
                    self.curves[m.channel, m.quantity],
                    f'<span style="color:{color}">CH{m.channel}  {engineering(spec.units_per_div, m.unit, custom_scale=True)}/格</span>',
                )
            self.legend_signature = signature
        self.meta.setText(
            (
                "拖动预览 · 估算，松手应用"
                if any(v is None for _, v in self.preview_targets.values())
                else "预览 · 估算，等待实测新帧"
            )
            + f" · {engineering(first.seconds_per_div, 's', custom_scale=True)}/格 · 基于上一帧，保存仍用原始数据"
            if used
            else self.actual_meta
        )

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

    def _keep_instrument_view(self, *_args) -> None:
        if not self.instrument_view or not self.grid_range or self._restoring_grid:
            return
        view = self.plot.getViewBox()
        x, y = view.viewRange()
        if any(view.autoRangeEnabled()) or tuple(x) != self.grid_range or tuple(y) != (-4, 4):
            self.fit_current()

    def fit_current(self) -> None:
        if self.grid_range:
            self._restoring_grid = True
            try:
                self.plot.setRange(xRange=self.grid_range, yRange=(-4, 4), padding=0, disableAutoRange=True)
            finally:
                self._restoring_grid = False
        else:
            super().fit_current()
