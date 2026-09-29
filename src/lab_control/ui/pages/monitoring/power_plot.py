"""Two scalar trends sharing one immutable acquisition and a monotonic time axis."""

from collections import defaultdict, deque
from dataclasses import replace
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import QVBoxLayout, QWidget

from lab_control.application.acquisition_pipeline import validate_displayed_batch
from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import DeviceError

CHANNEL_COLORS = ("#e3a252", "#69c9d4", "#b5d979", "#cda8e8")


class _TimeAxis(pg.AxisItem):
    def __init__(self):
        super().__init__(orientation="bottom")

    def boundingRect(self):  # noqa: N802
        # Grid-enabled AxisItem bounds otherwise hide the left endpoint's "0" label.
        return super().boundingRect().adjusted(-16, 0, 0, 0)


class PowerPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.plots: dict[str, Any] = {}
        for quantity, title, unit in (("voltage", "电压", "V"), ("current", "电流", "A")):
            plot = pg.PlotWidget(background="#101719", axisItems={"bottom": _TimeAxis()})
            plot.setMinimumSize(180, 130)
            plot.showGrid(x=True, y=True, alpha=0.16)
            plot.setLabel("left", title, units=unit)
            plot.getAxis("left").enableAutoSIPrefix(False)
            plot.getAxis("left").setWidth(60)
            plot.setLabel("bottom", "采集经过时间", units="s")
            plot.getAxis("bottom").enableAutoSIPrefix(False)
            plot.disableAutoRange()
            plot.getPlotItem().hideButtons()
            plot.setXRange(0, 600, padding=0)
            layout.addWidget(plot, 1)
            self.plots[quantity] = plot
        self.plots["current"].setXLink(self.plots["voltage"])
        self.history: dict[tuple[int, str], deque] = defaultdict(lambda: deque(maxlen=1200))
        self.curves: dict[tuple[int, str], Any] = {}
        self.displayed_frame: MonitorFrame | None = None
        self.session_id = ""
        self.channels: tuple[int, ...] = ()
        self.last_token: tuple | None = None
        self.latest_time = 0.0
        self.time_origin: float | None = None
        self.follow_range = (0.0, 600.0)
        self.fitted: set[str] = set()

    def reset(self) -> None:
        for plot in self.plots.values():
            plot.clear()
            plot.disableAutoRange()
        self.history.clear()
        self.curves.clear()
        self.fitted.clear()
        self.displayed_frame = None
        self.last_token = None
        self.session_id = ""
        self.latest_time = 0
        self.time_origin = None
        self.follow_range = (0.0, 600.0)
        self.plots["voltage"].setXRange(*self.follow_range, padding=0)

    def set_channels(self, channels: tuple[int, ...]) -> None:
        self.channels = channels
        for (channel, _), curve in self.curves.items():
            curve.setVisible(channel in channels)
        if self.displayed_frame:
            measurements = tuple(m for m in self.displayed_frame.batch.measurements if m.channel in channels)
            self.displayed_frame = (
                replace(
                    self.displayed_frame, batch=replace(self.displayed_frame.batch, measurements=measurements)
                )
                if measurements
                else None
            )

    def show_frame(self, frame: MonitorFrame, channels: tuple[int, ...]) -> bool:
        if self.session_id and self.session_id != frame.session_id:
            self.reset()
        token = (frame.session_id, frame.sequence, frame.received_at)
        if token == self.last_token:
            return False
        wanted = tuple(
            m for m in frame.batch.measurements if m.channel in channels and m.quantity in self.plots
        )
        candidate = replace(frame, batch=replace(frame.batch, measurements=wanted))
        try:
            validate_displayed_batch(candidate.batch)
            if not wanted or any(
                len(m.values) != 1 or m.unit != ("V" if m.quantity == "voltage" else "A") for m in wanted
            ):
                return False
            if {(m.channel, m.quantity) for m in wanted} != {(ch, q) for ch in channels for q in self.plots}:
                return False
        except DeviceError:
            return False
        self.session_id, self.last_token = frame.session_id, token
        self.latest_time = frame.received_at
        if self.time_origin is None:
            self.time_origin = frame.received_at
        elapsed = frame.received_at - self.time_origin
        # Follow the last ten minutes only while the user has not zoomed/panned.
        # The origin remains the first valid frame, including after a pause.
        plot = self.plots["voltage"]
        if np.allclose(plot.viewRange()[0], self.follow_range, rtol=0, atol=1e-6):
            self.follow_range = (max(0.0, elapsed - 600), max(600.0, elapsed))
            plot.setXRange(*self.follow_range, padding=0)
        self.set_channels(channels)
        for measurement in wanted:
            key = (measurement.channel, measurement.quantity)
            self.history[key].append((frame.received_at, float(measurement.values[0])))
        for key, history in self.history.items():
            while history and self.latest_time - history[0][0] > 600:
                history.popleft()
            if key not in self.curves:
                self.curves[key] = self.plots[key[1]].plot(
                    pen=pg.mkPen(CHANNEL_COLORS[(key[0] - 1) % 4], width=1.6),
                    name=f"CH {key[0]}",
                )
            if history:
                x, y = np.asarray(history).T
                self.curves[key].setData(
                    x - self.time_origin,
                    y,
                    connect="finite",
                    symbol="o" if len(x) == 1 else None,
                    symbolSize=5,
                )
            else:
                self.curves[key].setData([], [])
            self.curves[key].setVisible(key[0] in channels)
        self._fit_y(expand_only=True)
        self.displayed_frame = candidate
        return True

    def _fit_y(self, *, expand_only: bool) -> None:
        if self.time_origin is None:
            return
        for quantity, plot in self.plots.items():
            left, right = plot.viewRange()[0]
            values = [
                value
                for (ch, q), history in self.history.items()
                if q == quantity and ch in self.channels
                for timestamp, value in history
                if (
                    timestamp == self.latest_time
                    if expand_only
                    else left <= timestamp - self.time_origin <= right
                )
            ]
            if not values:
                continue
            low, high = min(values), max(values)
            margin = max((high - low) * 0.1, abs(low) * 0.01, 0.001)
            bounds = (low - margin, high + margin)
            if expand_only and quantity in self.fitted:
                before = plot.viewRange()[1]
                if before[0] <= low and high <= before[1]:
                    continue
                bounds = (min(before[0], bounds[0]), max(before[1], bounds[1]))
            plot.setYRange(*bounds, padding=0)
            self.fitted.add(quantity)

    def fit_current(self) -> None:
        self._fit_y(expand_only=False)
