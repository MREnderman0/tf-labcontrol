"""All-channel optical power meter monitoring workspace."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import replace
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.acquisition_pipeline import validate_displayed_batch
from lab_control.application.catalog import mode_applicable
from lab_control.application.monitoring import MonitorFrame
from lab_control.application.power_window import PowerWindowSnapshot
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, Measurement
from lab_control.domain.power import power_to_watts as power_to_watts
from lab_control.domain.power import watts_to_dbm
from lab_control.ui.controller import WorkspaceController, state_label
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.pages.monitoring.optical_power_readout import PowerMeterReadoutRow, readout_header
from lab_control.ui.pages.monitoring.power_plot import CHANNEL_COLORS
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.timing import MonitoringTiming
from lab_control.ui.widgets.device_selector import DeviceSelector


def _scalar(value: Any) -> float | None:
    try:
        values = np.asarray(value).reshape(-1)
        if values.size != 1 or not math.isfinite(float(values[0])):
            return None
        return float(values[0])
    except (TypeError, ValueError, IndexError):
        return None


class OpticalPowerPlot(QWidget):
    """A single, bounded trend with one curve per selected channel."""

    quantity = "power"
    display_unit = "W"
    axis_label = "功率"
    nonnegative = True

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(background="#101719")
        self.plot.showGrid(x=True, y=True, alpha=0.16)
        self.plot.setLabel("left", self.axis_label, units=self.display_unit)
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.plot.setLabel("bottom", "采集经过时间", units="s")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.getPlotItem().hideButtons()
        self.plot.setXRange(0, 600, padding=0)
        layout.addWidget(self.plot, 1)
        self.history: dict[int, deque[tuple[float, float]]] = {}
        self.curves: dict[int, Any] = {}
        self.channels: tuple[int, ...] = ()
        self.displayed_frame: MonitorFrame | None = None
        self.session_id = ""
        self.last_token: tuple[Any, ...] | None = None
        self.time_origin: float | None = None
        self.latest_time = 0.0
        self.follow_range = (0.0, 600.0)

    def reset(self) -> None:
        self.plot.clear()
        self.plot.setXRange(0, 600, padding=0)
        self.history.clear()
        self.curves.clear()
        self.displayed_frame = None
        self.session_id = ""
        self.last_token = None
        self.time_origin = None
        self.latest_time = 0.0
        self.follow_range = (0.0, 600.0)

    def set_channels(self, channels: tuple[int, ...]) -> None:
        self.channels = tuple(channels)
        for channel, curve in self.curves.items():
            curve.setVisible(channel in self.channels)
        if self.displayed_frame:
            measurements = tuple(
                m for m in self.displayed_frame.batch.measurements if m.channel in self.channels
            )
            self.displayed_frame = None if not measurements else self.displayed_frame

    def show_frame(self, frame, channels: tuple[int, ...]) -> bool:
        if self.session_id and self.session_id != frame.session_id:
            self.reset()
        token = (frame.session_id, frame.sequence, frame.received_at)
        if token == self.last_token:
            return False
        wanted = tuple(
            measurement
            for measurement in frame.batch.measurements
            if (not self.quantity or measurement.quantity == self.quantity)
            and measurement.channel in channels
        )
        if len(wanted) != len(channels) or {m.channel for m in wanted} != set(channels):
            return False
        candidate = replace(frame, batch=replace(frame.batch, measurements=wanted))
        try:
            validate_displayed_batch(candidate.batch)
        except DeviceError:
            return False
        watts: dict[int, float] = {}
        for measurement in wanted:
            raw = _scalar(measurement.values)
            value = self.display_value(raw, measurement.unit) if raw is not None else None
            if value is None:
                return False
            watts[measurement.channel] = value
        self.session_id, self.last_token = frame.session_id, token
        self.latest_time = frame.received_at
        if self.time_origin is None:
            self.time_origin = frame.received_at
        elapsed = frame.received_at - self.time_origin
        if np.allclose(self.plot.viewRange()[0], self.follow_range, rtol=0, atol=1e-6):
            self.follow_range = (max(0.0, elapsed - 600), max(600.0, elapsed))
            self.plot.setXRange(*self.follow_range, padding=0)
        self.channels = tuple(channels)
        for channel, value in watts.items():
            self.history.setdefault(channel, deque(maxlen=1200)).append((frame.received_at, value))
        for channel, history in self.history.items():
            while history and self.latest_time - history[0][0] > 600:
                history.popleft()
            if channel not in self.curves:
                self.curves[channel] = self.plot.plot(
                    pen=pg.mkPen(CHANNEL_COLORS[(channel - 1) % len(CHANNEL_COLORS)], width=1.8),
                    name=f"CH {channel}",
                )
            if history:
                x, y = np.asarray(history, dtype=float).T
                self.curves[channel].setData(
                    x - self.time_origin,
                    y,
                    connect="finite",
                    symbol="o" if len(x) == 1 else None,
                    symbolSize=5,
                )
            self.curves[channel].setVisible(channel in self.channels)
        values = [
            value
            for channel, history in self.history.items()
            if channel in self.channels
            for _, value in history
        ]
        if values:
            low, high = min(values), max(values)
            margin = max((high - low) * 0.1, abs(low) * 0.05, 1e-15)
            self.plot.setYRange(self.lower_limit(low - margin), high + margin, padding=0)
        self.displayed_frame = candidate
        return True

    def display_value(self, value: float, unit: str) -> float | None:
        return power_to_watts(value, unit)

    def lower_limit(self, value: float) -> float:
        return max(0.0, value) if self.nonnegative else value

    def fit_current(self) -> None:
        values = [
            value
            for channel, history in self.history.items()
            if channel in self.channels
            for _, value in history
        ]
        if values:
            low, high = min(values), max(values)
            margin = max((high - low) * 0.1, abs(low) * 0.05, 1e-15)
            self.plot.setYRange(self.lower_limit(low - margin), high + margin, padding=0)


class RollingPowerPlot(OpticalPowerPlot):
    """Render the service's complete power window, including acquisition gaps."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.window_context = ""
        self.render_key: tuple[Any, ...] | None = None
        self.plot.setLabel("bottom", "采集经过时间", units="min")
        self.reset()

    def reset(self) -> None:
        super().reset()
        self.window_context = ""
        self.render_key = None
        self.follow_range = (0.0, 30.0)
        self.plot.setXRange(*self.follow_range, padding=0)

    def set_display_unit(self, unit: str) -> None:
        self.display_unit = unit
        self.nonnegative = unit == "W"
        self.plot.setLabel("left", "功率", units=unit)

    def show_window(self, window: PowerWindowSnapshot, channels: tuple[int, ...]) -> None:
        if self.window_context and self.window_context != window.context_id:
            self.reset()
        self.window_context = window.context_id
        self.time_origin = window.origin
        self.latest_time = window.now
        self.channels = channels
        if window.origin is not None:
            elapsed = (window.now - window.origin) / 60.0
            if np.allclose(self.plot.viewRange()[0], self.follow_range, rtol=0, atol=1e-6):
                self.follow_range = (max(0.0, elapsed - 30), max(30.0, elapsed))
                self.plot.setXRange(*self.follow_range, padding=0)
        key = (window.context_id, window.revision, channels, self.display_unit)
        if self.render_key == key:
            return
        self.render_key = key
        self.history.clear()
        for history in window.channels:
            channel = history.channel
            if channel not in self.curves:
                self.curves[channel] = self.plot.plot(
                    pen=pg.mkPen(CHANNEL_COLORS[(channel - 1) % len(CHANNEL_COLORS)], width=1.8),
                    name=f"CH {channel}",
                )
            x, y = [], []
            previous_segment = None
            for point in history.points:
                at = (point.at - window.origin) / 60.0 if window.origin is not None else 0.0
                if previous_segment is not None and point.segment != previous_segment:
                    x.append(at)
                    y.append(float("nan"))
                previous_segment = point.segment
                value = point.watts if self.display_unit == "W" else watts_to_dbm(point.watts)
                x.append(at)
                y.append(value if value is not None else float("nan"))
            self.history[channel] = deque(zip(x, y))
            self.curves[channel].setData(
                x, y, connect="finite", symbol="o" if len(x) == 1 else None, symbolSize=5
            )
            self.curves[channel].setVisible(channel in channels)
        self.fit_current()
        self.displayed_frame = window.frame
        if window.frame:
            self.session_id = window.frame.session_id
            self.last_token = (window.frame.session_id, window.frame.sequence, window.frame.received_at)

    def fit_current(self) -> None:
        values = [
            value
            for channel, history in self.history.items()
            if channel in self.channels
            for _, value in history
            if math.isfinite(value)
        ]
        if values:
            low, high = min(values), max(values)
            margin = max((high - low) * 0.1, abs(low) * 0.05, 1e-15 if self.nonnegative else 0.01)
            self.plot.setYRange(self.lower_limit(low - margin), high + margin, padding=0)


class PowerMeterReadoutCard(QFrame):
    # Two cards must fit in the compact readout pane, including the grid gap
    # and the scroll area's margins.
    display_unit = "W"
    WIDTH = 168
    HEIGHT = 96

    def __init__(self, channel: int, parent=None) -> None:
        super().__init__(parent)
        self.channel = channel
        self.setObjectName("powerMeterReadoutCard")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        heading = self.heading = QHBoxLayout()
        self.channel_label = QLabel(f"CH {channel}")
        self.channel_label.setObjectName("powerMeterChannel")
        heading.addWidget(self.channel_label)
        heading.addStretch()
        self.status = QLabel("等待数据")
        self.status.setObjectName("powerMeterStatus")
        heading.addWidget(self.status)
        layout.addLayout(heading)
        self.value = QLabel(f"— {self.display_unit}")
        self.value.setObjectName("powerMeterValue")
        self.value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.value.setMinimumWidth(0)
        self.value.setWordWrap(False)
        layout.addWidget(self.value)
        self.meta = QLabel("尚未采集")
        self.meta.setObjectName("powerMeterMeta")
        layout.addWidget(self.meta)
        color = CHANNEL_COLORS[(channel - 1) % len(CHANNEL_COLORS)]
        self.channel_label.setStyleSheet(f"color: {color};")

    def display_value(self, value: float, unit: str) -> float | None:
        return power_to_watts(value, unit)

    def update_measurement(self, measurement: Measurement | None, timestamp: Any = None) -> None:
        if measurement is None:
            self.value.setText(f"— {self.display_unit}")
            self.value.setToolTip(f"— {self.display_unit}")
            self.setToolTip("")
            self.status.setText("等待数据")
            self.meta.setText("尚未采集")
            return
        raw = _scalar(measurement.values)
        watts = self.display_value(raw, measurement.unit) if raw is not None else None
        quality = getattr(measurement.quality, "value", str(measurement.quality))
        if watts is None:
            self.value.setText(f"— {self.display_unit}")
            self.value.setToolTip("")
            self.status.setText("不可用")
        else:
            shown = f"{watts:.6g} {self.display_unit}"
            self.value.setText(shown)
            # Keep the compact six-significant-digit label stable while
            # retaining the full converted and device-returned values for
            # inspection without changing the card geometry.
            full = f"{watts:.17g} {self.display_unit}"
            if full != shown:
                full += f" · 原始 {raw:.17g} {measurement.unit}"
            # The label keeps the compact text for legacy accessibility and
            # snapshot callers; hovering the fixed card exposes the complete
            # converted value and the original instrument value.
            self.value.setToolTip(shown)
            self.setToolTip(full)
            self.status.setText("正常" if quality in {"good", "simulated"} else quality)
        if isinstance(timestamp, str):
            shown_time = timestamp.replace("T", " ").split("+", 1)[0]
        elif timestamp is not None:
            shown_time = f"{timestamp:.2f} s"
        else:
            shown_time = ""
        self.meta.setText(f"{measurement.unit} · {shown_time}" if shown_time else measurement.unit)


class OpticalPowerMonitoringPage(QWidget):
    category = "optical_power_meter"
    category_name = "光功率计"
    trend_title = "功率趋势（W）"
    quantity = "power"
    rolling_power = True
    plot_type: type[OpticalPowerPlot] = RollingPowerPlot
    card_type = PowerMeterReadoutCard

    def __init__(
        self, controller: WorkspaceController, add_card, parent=None, *, manage_selection=True
    ) -> None:
        super().__init__(parent)
        self.controller, self.add_card = controller, add_card
        self.device_id, self.mode = "", "general"
        self.transition = False
        self.generation = 0
        self.channel_checks: dict[int, QCheckBox] = {}
        self.cards: dict[int, PowerMeterReadoutCard | PowerMeterReadoutRow] = {}
        stored_unit = controller.runtime.repository.load_workspace(
            "monitoring_display/optical_power_meter", {}
        )
        self.display_unit = (
            "dBm" if isinstance(stored_unit, dict) and stored_unit.get("unit") == "dBm" else "W"
        )
        stored_channels = controller.runtime.repository.load_workspace(
            f"monitoring_channels/{self.category}", {}
        )
        self.channel_preferences: dict[str, list[int]] = (
            dict(stored_channels) if isinstance(stored_channels, dict) else {}
        )
        self.latest_frame: MonitorFrame | None = None
        self.monitoring_context_id = ""
        layout = self.main_layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, self.category, self, compact=True)
        header.addWidget(self.selector, 1)
        self.connection = QLabel()
        self.connection.setObjectName("sourceConnectionState")
        header.addWidget(self.connection)
        self.timing = MonitoringTiming(controller, self)
        self.timing.set_fixed_interval(True)
        header.addWidget(self.timing)
        self.start_button = QPushButton("开始监测")
        self.start_button.clicked.connect(self.toggle_monitoring)
        header.addWidget(self.start_button)
        layout.addLayout(header)
        self.empty = QLabel(f"请先从总览连接{self.category_name}，再选择设备。")
        self.empty.setObjectName("mutedText")
        layout.addWidget(self.empty)
        self.channel_bar = QWidget(self)
        self.channel_row = QHBoxLayout(self.channel_bar)
        self.channel_row.setContentsMargins(0, 0, 0, 0)
        self.channel_title = QLabel("显示与采集通道")
        self.channel_row.addWidget(self.channel_title)
        self.channel_row.addStretch()
        layout.addWidget(self.channel_bar)
        self.channel_bar.setVisible(not self.rolling_power)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        chart = self.chart = QFrame()
        chart.setObjectName("powerMeterWorkspace")
        chart_layout = QVBoxLayout(chart)
        chart_layout.setContentsMargins(12, 10, 12, 10)
        title_row = self.chart_actions = QHBoxLayout()
        title = self.trend_label = QLabel(self.trend_title)
        title.setObjectName("powerMeterTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        self.record_button = RecordingButton(chart)
        self.record_button.clicked.connect(self.record)
        title_row.addWidget(self.record_button)
        fit = QPushButton("适应曲线")
        fit.clicked.connect(self._fit)
        title_row.addWidget(fit)
        chart_layout.addLayout(title_row)
        self.plot = self.plot_type()
        if isinstance(self.plot, RollingPowerPlot):
            self.plot.set_display_unit(self.display_unit)
            self.trend_label.setText(f"功率趋势（{self.display_unit}）· 30 min")
        chart_layout.addWidget(self.plot, 1)
        self.status = QLabel("监测未开始")
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        chart_layout.addWidget(self.status)
        self.splitter.addWidget(chart)
        right = QFrame()
        right.setObjectName("powerMeterReadoutList" if self.rolling_power else "powerMeterReadoutPanel")
        right_layout = QVBoxLayout(right)
        if self.rolling_power:
            right_layout.setContentsMargins(10, 8, 10, 8)
        else:
            right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(4)
        readout_title = QLabel("实时光功率" if self.rolling_power else "实时读数")
        readout_title.setObjectName("powerMeterTitle")
        right_layout.addWidget(readout_title)
        self.readout_heading = readout_header(right) if self.rolling_power else None
        if self.readout_heading is not None:
            right_layout.addWidget(self.readout_heading)
            right.setMinimumWidth(360)
        self.readout_scroll = QScrollArea()
        self.readout_scroll.setWidgetResizable(True)
        self.readout_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.readout_body = QWidget()
        self.readout_body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.readout_grid = QGridLayout(self.readout_body)
        self.readout_grid.setContentsMargins(0, 0, 0, 0)
        self.readout_grid.setSpacing(0 if self.rolling_power else 8)
        self.readout_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.readout_scroll.setWidget(self.readout_body)
        self.readout_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.readout_scroll.viewport().installEventFilter(self)
        right_layout.addWidget(self.readout_scroll, 1)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 0 if self.rolling_power else 2)
        self.splitter.setSizes([850, 380])
        layout.addWidget(self.splitter, 1)
        if manage_selection:
            self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id if manage_selection else "")

    def available_channels(self, device_id: str) -> tuple[int, ...]:
        return tuple(self.controller.capabilities(device_id).channels)

    def request(self, channels: tuple[int, ...], *, confirmed=False) -> AcquisitionRequest:
        return AcquisitionRequest(
            kind="scalar", channels=channels, points=1, quantity=self.quantity, confirmed=confirmed
        )

    def confirm_acquisition(self) -> bool:
        return True

    def _fit(self) -> None:
        self.plot.fit_current()

    def channels(self) -> tuple[int, ...]:
        return tuple(channel for channel, check in self.channel_checks.items() if check.isChecked())

    def _channel_key(self, device_id: str) -> str:
        record = self.controller.records.get(device_id, {})
        slot = record.get("slots", {}).get("general", "")
        return f"slot:general:{slot}" if slot else f"device:{device_id}"

    def _selected_channels(self, device_id: str, channels: tuple[int, ...]) -> tuple[int, ...]:
        key = self._channel_key(device_id)
        saved = self.channel_preferences.get(key)
        if isinstance(saved, (list, tuple)):
            saved_set = set(saved)
            selected = tuple(channel for channel in channels if channel in saved_set)
            if selected or not saved:
                return selected
        task = self.controller.runtime.monitoring.snapshots().get(device_id, {})
        if task.get("running"):
            with self.controller.runtime.monitoring.lock:
                current = self.controller.runtime.monitoring.tasks.get(device_id)
                requested = (
                    tuple(
                        sorted(
                            {
                                channel
                                for request in current.requests
                                if request.kind == "scalar"
                                for channel in request.channels
                            }
                        )
                    )
                    if current
                    else ()
                )
            if requested:
                return tuple(channel for channel in channels if channel in requested)
        return channels

    def _save_channels(self) -> None:
        if not self.device_id:
            return
        self.channel_preferences[self._channel_key(self.device_id)] = list(self.channels())
        self.controller.runtime.repository.save_workspace(
            f"monitoring_channels/{self.category}", self.channel_preferences
        )

    def _reflow_readout_cards(self) -> None:
        if self.rolling_power:
            for index, (channel, card) in enumerate(sorted(self.cards.items())):
                self.readout_grid.addWidget(card, index, 0)
                card.show()
                if isinstance(card, PowerMeterReadoutRow):
                    card.set_selected(self.channel_checks[channel].isChecked())
            if self.readout_heading is not None:
                self.readout_heading.setContentsMargins(
                    0, 0, max(0, self.readout_scroll.width() - self.readout_scroll.viewport().width()), 0
                )
            return
        selected = set(self.channels())
        for channel, card in self.cards.items():
            card.setVisible(channel in selected)
        while self.readout_grid.count():
            self.readout_grid.takeAt(0)
        columns = 2 if self.readout_scroll.viewport().width() >= PowerMeterReadoutCard.WIDTH * 2 + 8 else 1
        for index, channel in enumerate(sorted(selected)):
            readout_card = self.cards.get(channel)
            if readout_card is not None:
                self.readout_grid.addWidget(readout_card, index // columns, index % columns)
        for column in range(2):
            self.readout_grid.setColumnStretch(column, 1 if column < columns else 0)
        self.readout_body.adjustSize()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.readout_scroll.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self, self._reflow_readout_cards)
        return super().eventFilter(watched, event)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.hide()
                widget.deleteLater()

    def select_device(self, device_id: str) -> None:
        if self.transition:
            return
        self.device_id, self.mode = device_id, "general"
        self.controller.focus_settings(device_id)
        self.generation += 1
        self.latest_frame = None
        self.monitoring_context_id = ""
        self.plot.reset()
        self.timing.bind(device_id, self.mode)
        self.empty.setVisible(not device_id)
        self.splitter.setVisible(bool(device_id))
        self._clear_layout(self.channel_row)
        self.channel_title = QLabel("显示与采集通道")
        self.channel_row.addWidget(self.channel_title)
        self.channel_title.setVisible(not self.rolling_power)
        self.channel_checks = {}
        while self.readout_grid.count():
            self.readout_grid.takeAt(0)
        # Hidden channels also belong to this page and must retire on a switch.
        for card in self.cards.values():
            card.hide()
            card.deleteLater()
        self.cards = {}
        if device_id:
            channels = self.available_channels(device_id)
            selected = self._selected_channels(device_id, channels)
            for channel in channels:
                card = (
                    PowerMeterReadoutRow(channel, self.readout_body)
                    if self.rolling_power
                    else self.card_type(channel, self.readout_body)
                )
                if isinstance(card, PowerMeterReadoutRow):
                    check = card.check
                    card.update_window(None, self.display_unit)
                    card.value.clicked.connect(self.toggle_display_unit)
                else:
                    check = QCheckBox(f"CH {channel}")
                    self.channel_row.addWidget(check)
                check.setChecked(channel in selected)
                check.toggled.connect(self.channels_changed)
                self.channel_checks[channel] = check
                # Reflow changes visibility before inserting selected channels.
                # Parent from construction so this can never become a window.
                self.cards[channel] = card
        self.channel_row.addStretch()
        self.plot.set_channels(self.channels())
        self._reflow_readout_cards()
        self.refresh()
        QTimer.singleShot(0, self, lambda device_id=device_id: self.refresh_frame(device_id))

    def channels_changed(self) -> None:
        channels = self.channels()
        self.latest_frame = None
        if not self.rolling_power:
            self.plot.reset()
        self.plot.set_channels(channels)
        for channel, card in self.cards.items():
            if channel not in channels and isinstance(card, PowerMeterReadoutCard):
                card.update_measurement(None)
        self._save_channels()
        self._reflow_readout_cards()
        if self.transition:
            return
        task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
        if not task.get("running"):
            self.refresh()
            return
        try:
            request = (self.request(channels),) if channels else ()
            self.controller.runtime.monitoring.replace_requests(self.device_id, request)
        except Exception as exc:
            self.controller.message.emit(str(exc))
        self.refresh()

    def start(self) -> None:
        if not self.device_id or not self.channels():
            return
        try:
            record = self.controller.records.get(self.device_id)
            if not record or not mode_applicable(record, "general"):
                raise DeviceError("此设备不支持监测", "unsupported")
            if not self.confirm_acquisition():
                return
            self.timing.commit()
            options = self.timing.options()
            self.controller.start_monitoring(
                self.device_id,
                (self.request(self.channels(), confirmed=True),),
                **options,
                mode=self.mode,
            )
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def toggle_monitoring(self) -> None:
        if self.controller.reconnect_if_needed(self.device_id):
            return
        task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
        if task.get("running") or task.get("pending"):
            self.controller.watch(
                self.controller.runtime.monitoring.stop(self.device_id), lambda _result: self.refresh()
            )
        else:
            self.start()

    def record(self) -> None:
        service = self.controller.runtime.monitoring
        try:
            task = service.snapshots().get(self.device_id, {})
            self.controller.set_recording(self.device_id, not task.get("recording"), mode=self.mode)
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def toggle_display_unit(self) -> None:
        if not isinstance(self.plot, RollingPowerPlot):
            return
        self.display_unit = "dBm" if self.display_unit == "W" else "W"
        self.controller.runtime.repository.save_workspace(
            "monitoring_display/optical_power_meter", {"unit": self.display_unit}
        )
        self.plot.set_display_unit(self.display_unit)
        self.trend_label.setText(f"功率趋势（{self.display_unit}）· 30 min")
        self.refresh_frame(self.device_id)

    def refresh_frame(self, device_id: str) -> None:
        if self.rolling_power:
            if device_id != self.device_id or not self.isVisible():
                return
            service = self.controller.runtime.monitoring
            window = service.power_window(device_id)
            histories = {h.channel: h for h in window.channels} if window else {}
            if window is not None and isinstance(self.plot, RollingPowerPlot):
                self.plot.show_window(window, self.channels())
                self.latest_frame = window.frame
                if window.frame:
                    service.displayed(device_id, window.frame)
            for channel, card in self.cards.items():
                if isinstance(card, PowerMeterReadoutRow):
                    card.update_window(histories.get(channel), self.display_unit)
            return
        if device_id != self.device_id or not self.isVisible() or not self.channels():
            return
        service = self.controller.runtime.monitoring
        frames = service.recent(device_id, "scalar", self.quantity)
        last_token = self.plot.last_token
        if last_token and frames:
            # The service keeps the complete bounded replay window. Only feed
            # frames that arrived after the last painted one; this keeps the
            # normal frame-ready path O(1) while still catching up after a
            # hidden page becomes visible.
            frames = tuple(
                frame
                for frame in frames
                if frame.session_id != last_token[0] or frame.sequence > last_token[1]
            )
        if not frames:
            latest = service.latest(device_id, "scalar", self.quantity)
            frames = (latest,) if latest else ()
        accepted = False
        for frame in frames:
            accepted = self.plot.show_frame(frame, self.channels()) or accepted
        if not accepted:
            return
        displayed_frame = self.plot.displayed_frame
        if displayed_frame is None:
            return
        self.latest_frame = displayed_frame
        service.displayed(device_id, displayed_frame)
        measurements = {m.channel: m for m in displayed_frame.batch.measurements}
        for channel, card in self.cards.items():
            if channel in self.channels() and isinstance(card, PowerMeterReadoutCard):
                card.update_measurement(measurements.get(channel), displayed_frame.batch.timestamp)

    def refresh(self) -> None:
        device_id = self.device_id
        state = self.controller.states.get(device_id, {})
        task = self.controller.runtime.monitoring.snapshots().get(device_id, {})
        context_id = task.get("monitoring_context_id", "")
        if context_id and context_id != self.monitoring_context_id:
            # A stopped task is replaced with a fresh monitoring context on
            # restart. Start its elapsed-time axis from zero instead of
            # appending the new run to the previous run's trend.
            self.plot.reset()
            self.latest_frame = None
            self.monitoring_context_id = context_id
        draining = recording_phase(task) == "draining"
        self.timing.bind(device_id, "general")
        self.timing.refresh(task)
        connected = state.get("connectionState") in {"connected", "busy"}
        applicable = bool(
            self.controller.records.get(device_id)
            and mode_applicable(self.controller.records[device_id], "general")
        )
        self.connection.setText(
            "已连接" if connected else state_label(state.get("connectionState", "disconnected"))
        )
        self.start_button.setText(
            "收尾中…" if draining else "停止监测" if task.get("running") else "开始监测"
        )
        self.start_button.setEnabled(
            bool(
                (connected or task.get("running"))
                and applicable
                and not draining
                and not self.transition
                and (task.get("running") or self.channels())
            )
        )
        for check in self.channel_checks.values():
            check.setEnabled(bool(connected and applicable and not draining and not self.transition))
        refresh_reconnect_button(
            self.start_button, self.controller, self.device_id, task, busy=self.transition
        )
        self.record_button.refresh(
            task,
            connected=connected,
            busy=bool(self.transition or (not self.channels() and not task.get("recording"))),
            locked=bool(state.get("controlsLocked")),
        )
        self.status.setText(
            task.get("error")
            or task.get("recording_notice")
            or task.get("notice")
            or recording_save_notice(task)
            or (
                "记录收尾中 · 正在排空已接收数据"
                if recording_phase(task) == "draining"
                else "采集已暂停 · 监测仍运行"
                if task.get("running") and not self.channels()
                else "采集中 · 原始数据逐帧保存"
                if task.get("recording")
                else "监测中"
                if task.get("running")
                else "监测已停止 · 保留最终结果"
                if self.rolling_power and task.get("sequence")
                else "监测未开始"
            )
        )
        update_recording_frame(self.chart, task)
        if self.isVisible():
            self.refresh_frame(device_id)

    def focus_card(self, spec: dict[str, Any]) -> None:
        self.selector.select(spec["device_id"])
        channels = set(spec.get("channels", ()))
        for channel, check in self.channel_checks.items():
            check.blockSignals(True)
            check.setChecked(channel in channels)
            check.blockSignals(False)
        self.channels_changed()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        orientation = Qt.Orientation.Vertical if self.width() < 900 else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([430, 300] if orientation == Qt.Orientation.Vertical else [850, 380])

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.controller.focus_settings(self.device_id)
        QTimer.singleShot(0, self, lambda: self.refresh_frame(self.device_id))

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
