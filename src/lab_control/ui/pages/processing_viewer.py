"""Offline plots with a latest-request-only frame loader and full-record scalar views."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.processing import ProcessingService
from lab_control.domain.oscilloscope import COLORS, ScopeDisplay, engineering
from lab_control.storage.processing_results import ResultReader
from lab_control.ui.widgets.publication_figure import prepare_publication_font
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

CHANNEL_PALETTE = (*COLORS, "#86accf", "#d3a8b6", "#95c7af", "#beb5db")
CHANNEL_STYLES = (Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine, Qt.PenStyle.DotLine)


class ProcessingViewer(QWidget):
    export_requested = Signal(object)

    def __init__(self, service: ProcessingService, parent=None):
        super().__init__(parent)
        prepare_publication_font()
        self.service = service
        self.reader: ResultReader | None = None
        self.info: dict[str, Any] = {}
        self.future: Future | None = None
        self.pending: tuple | None = None
        self.token = 0
        self.loading_token = 0
        self.loading_kind = ""
        self._fit_pending = True
        self._drawing = False
        self.scope_specs: dict[str, ScopeDisplay] = {}
        self._scope_signature: tuple = ()
        self.plot_data: list[tuple[str, np.ndarray, np.ndarray]] = []
        self.cursor_data: list[tuple[str, np.ndarray, np.ndarray]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        self.group = WorkspaceComboBox()
        self.group.setMinimumWidth(150)
        self.group.currentIndexChanged.connect(self._group_changed)
        tools.addWidget(self.group)
        self.watts = QCheckBox("以 W 显示")
        self.watts.toggled.connect(lambda: self.request(fit=True))
        tools.addWidget(self.watts)
        self.scope_view = QCheckBox("示波器刻度")
        self.scope_view.setToolTip("按采集时各通道的量程和偏移显示；取消勾选查看统一物理单位坐标")
        self.scope_view.setChecked(True)
        self.scope_view.hide()
        self.scope_view.toggled.connect(lambda: self.request(fit=True))
        tools.addWidget(self.scope_view)
        fit = QPushButton("适应全部数据")
        fit.clicked.connect(self.fit_all)
        tools.addWidget(fit)
        tools.addStretch()
        self.figure_width = WorkspaceComboBox()
        self.figure_width.addItem("双栏 183 mm", 183)
        self.figure_width.addItem("单栏 89 mm", 89)
        tools.addWidget(self.figure_width)
        self.figure_format = WorkspaceComboBox()
        self.figure_format.addItems(["PNG", "SVG", "PDF"])
        tools.addWidget(self.figure_format)
        self.export_button = QPushButton("导出论文插图")
        self.export_button.setToolTip(
            "Nature 风格：白底、Arial 字体、简洁坐标轴；PNG 600 dpi，SVG/PDF 为矢量图"
        )
        self.export_button.clicked.connect(self.export_current)
        tools.addWidget(self.export_button)
        layout.addLayout(tools)
        split = QSplitter()
        self.channels = QListWidget()
        self.channels.setMaximumWidth(220)
        self.channels.itemChanged.connect(lambda _item: self.request(fit=True))
        split.addWidget(self.channels)
        self.plot = pg.PlotWidget(background="#131c21")
        self.plot.disableAutoRange()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend(offset=(8, 8), brush=pg.mkBrush("#131c21"), pen=pg.mkPen("#334148"))
        self.plot.setMinimumSize(300, 260)
        self.plot.getViewBox().sigXRangeChanged.connect(self._range_changed)
        self.plot.scene().sigMouseMoved.connect(self._cursor)
        split.addWidget(self.plot)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.timeline_row = QWidget()
        timeline = QHBoxLayout(self.timeline_row)
        timeline.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.previous = QPushButton("上一帧")
        self.previous.clicked.connect(lambda: self.slider.setValue(self.slider.value() - 1))
        timeline.addWidget(self.previous)
        self.slider.setAccessibleName("采集帧时间线")
        self.slider.valueChanged.connect(lambda: self.request())
        timeline.addWidget(self.slider, 1)
        self.next = QPushButton("下一帧")
        self.next.clicked.connect(lambda: self.slider.setValue(self.slider.value() + 1))
        timeline.addWidget(self.next)
        layout.addWidget(self.timeline_row)
        self.status = QLabel("选择已处理的设备")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.cursor_text = QLabel("移动光标查看曲线读数；拖动图表平移，滚轮缩放。")
        self.cursor_text.setTextFormat(Qt.TextFormat.PlainText)
        self.cursor_text.setWordWrap(True)
        self.cursor_text.setObjectName("mutedText")
        layout.addWidget(self.cursor_text)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(40)
        self.range_timer = QTimer(self)
        self.range_timer.setSingleShot(True)
        self.range_timer.timeout.connect(lambda: self.request())

    def clear(self, text="设备尚未处理") -> None:
        self.range_timer.stop()
        self.token += 1
        self.pending = None
        self.reader = None
        self.info = {}
        self.plot.clear()
        self.plot_data.clear()
        self.cursor_data.clear()
        self.scope_specs.clear()
        self._scope_signature = ()
        self.scope_view.hide()
        self.channels.clear()
        self.group.clear()
        self.timeline_row.hide()
        self.status.setText(text)

    def open_result(self, folder) -> None:
        self.clear("正在加载处理结果…")
        self.reader = ResultReader(folder)
        self.token += 1
        self.pending = (self.token, "info", self.reader, ())
        self._poll()

    def _group_changed(self) -> None:
        self.channels.blockSignals(True)
        self.channels.clear()
        group = self.group.currentData()
        self.watts.blockSignals(True)
        self.watts.setChecked(bool(group and group[1] == "W"))
        self.watts.blockSignals(False)
        for series in self.info.get("series", []):
            if (series["quantity"], series.get("unit", ""), series.get("axis_unit", "")) != group:
                continue
            item = QListWidgetItem(f"CH {series.get('channel', 1)}")
            item.setData(Qt.ItemDataRole.UserRole, series)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.channels.addItem(item)
        self.channels.blockSignals(False)
        self.request(fit=True)

    def selected(self) -> list[dict]:
        return [
            self.channels.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.channels.count())
            if self.channels.item(i).checkState() == Qt.CheckState.Checked
        ]

    def fit_all(self) -> None:
        self.range_timer.stop()
        self.request(fit=True)

    def request(self, *, fit=False) -> None:
        if self.reader is None or not self.info:
            return
        self._fit_pending |= fit
        self.token += 1
        if self.info["kind"] == "scalar":
            bounds = None if self._fit_pending else tuple(self.plot.viewRange()[0])
            args = (tuple(s["id"] for s in self.selected()), bounds)
            self.pending = (self.token, "scalar", self.reader, args)
        else:
            self.pending = (self.token, "frame", self.reader, (self.slider.value(),))
        self.previous.setEnabled(self.slider.value() > self.slider.minimum())
        self.next.setEnabled(self.slider.value() < self.slider.maximum())
        self._poll()

    def _poll(self) -> None:
        if self.future is not None and self.future.done():
            future, self.future = self.future, None
            try:
                data = future.result()
                if self.loading_token == self.token:
                    self._loaded(self.loading_kind, data)
            except Exception as exc:
                if self.loading_token == self.token:
                    self.status.setText(f"读取失败：{exc}")
        if self.future is None and self.pending is not None and not self.service.closed:
            token, kind, reader, args = self.pending
            self.pending = None
            self.loading_token, self.loading_kind = token, kind
            self.future = self.service.read_worker.submit(getattr(reader, kind), *args)

    def _loaded(self, kind, data) -> None:
        if kind == "info":
            self.info = data
            self._fit_pending = True
            self.watts.setVisible(bool(data.get("power_units")))
            self.watts.setChecked(False)
            groups = list(
                dict.fromkeys(
                    (s["quantity"], s.get("unit", ""), s.get("axis_unit", "")) for s in data["series"]
                )
            )
            self.group.blockSignals(True)
            self.group.clear()
            labels = {
                "power": "功率",
                "frequency": "频率",
                "temperature": "温度",
                "waveform": "波形",
                "spectrum": "光谱",
                "voltage": "电压",
                "resistance": "电阻",
            }
            for group in groups:
                self.group.addItem(f"{labels.get(group[0], group[0])} · {group[1]}", group)
            self.group.blockSignals(False)
            self.slider.blockSignals(True)
            self.slider.setRange(1, max(1, data["frames"]))
            self.slider.setValue(1)
            self.slider.blockSignals(False)
            self.slider.setEnabled(data["frames"] > 1)
            self.timeline_row.setVisible(data["kind"] == "frames")
            if not data["frames"]:
                self.status.setText("该记录没有有效采集帧")
            else:
                self._group_changed()
            return
        points = []
        self.scope_specs = {}
        selected = self.selected()
        for series in selected:
            if kind == "scalar":
                x, y = data.get(series["id"], (np.array([]), np.array([])))
            else:
                sample = next(
                    (
                        sample
                        for sample in data["measurements"]
                        if all(
                            sample.get(key, "") == series.get(key, "")
                            for key in ("channel", "quantity", "unit", "axis_unit")
                        )
                    ),
                    None,
                )
                if sample is None:
                    continue
                y = sample["values"].reshape(-1)
                x = np.arange(y.size) if sample["axis"] is None else sample["axis"].reshape(-1)
                display = sample.get("metadata", {}).get("scope_display")
                if display and series["quantity"] == "waveform" and series.get("axis_unit") == "s":
                    spec = ScopeDisplay.parse(display)
                    if spec.unit != series["unit"]:
                        raise ValueError("波形单位与采集时示波器刻度不一致")
                    self.scope_specs[f"CH {series.get('channel', 1)}"] = spec
            power = bool(self.info.get("power_units"))
            if power and self.watts.isChecked() and series.get("unit") == "dBm":
                y = np.power(10.0, np.asarray(y) / 10.0) * 1e-3
            elif power and not self.watts.isChecked() and series.get("unit") == "W":
                with np.errstate(divide="ignore", invalid="ignore"):
                    y = np.where(np.asarray(y) > 0, 10 * np.log10(np.asarray(y) * 1e3), np.nan)
            points.append((f"CH {series.get('channel', 1)}", x, y))
        available = bool(points) and len(self.scope_specs) == len(points)
        self.scope_view.setVisible(available)
        if not available:
            self.scope_specs.clear()
        signature = tuple(self.scope_specs.items()) if self.scope_view.isChecked() else ()
        if signature != self._scope_signature:
            self._fit_pending = True
            self._scope_signature = signature
        self._draw(points)
        if kind == "frame":
            self.status.setText(
                f"帧 {data['sequence']} / {self.info['frames']} · {data['timestamp']} · "
                f"距开始 {data['elapsed']:.6g} s；时间沿用原始读回记录。"
            )
        else:
            warning = self.info.get("warning", "")
            self.status.setText(
                f"完整记录 {self.info['frames']:,} 帧 · {len(selected)} 个选中通道 · "
                f"{self.info['range'][1]:.6g} s" + (f" · 原记录提示：{warning}" if warning else "")
            )

    def _draw(self, points) -> None:
        self._drawing = True
        try:
            self.plot.clear()
            self.plot_data = points
            self.cursor_data = []
            displayed = []
            scope = bool(self.scope_specs) and self.scope_view.isChecked()
            for name, x, y in points:
                if scope:
                    spec = self.scope_specs[name]
                    displayed.append((name, x, (y - spec.reference_value) / spec.units_per_div))
                else:
                    displayed.append((name, x, y))
            # Bounds must come from the full loaded arrays, before clipToView or
            # downsampling see the previous viewport (possibly far off screen).
            if self._fit_pending and displayed:
                self._fit_points(displayed)
                self._fit_pending = False
            for i, (name, x, y) in enumerate(points):
                if self.info.get("kind") == "frames" and np.all(x[1:] >= x[:-1]):
                    self.cursor_data.append((name, x, y))
                else:
                    valid = np.isfinite(x) & np.isfinite(y)
                    order = np.argsort(x[valid], kind="stable")
                    self.cursor_data.append((name, x[valid][order], y[valid][order]))
                channel = int(name.split()[-1]) - 1
                curve = self.plot.plot(
                    x,
                    displayed[i][2],
                    pen=pg.mkPen(
                        CHANNEL_PALETTE[channel % len(CHANNEL_PALETTE)],
                        width=1.3,
                        style=CHANNEL_STYLES[(channel // len(CHANNEL_PALETTE)) % len(CHANNEL_STYLES)],
                    ),
                    name=(
                        f"{name} · {engineering(self.scope_specs[name].units_per_div, self.scope_specs[name].unit)}/格"
                        if scope
                        else name
                    ),
                    connect="finite",
                    symbol="o" if len(x) == 1 else None,
                )
                # Scalar envelopes contain irregular times and NaN gap markers;
                # pyqtgraph's clip/downsample assumes sorted, uniform X values.
                uniform = (
                    self.info.get("kind") == "frames"
                    and len(x) > 1
                    and np.isfinite(x).all()
                    and np.all(np.diff(x) > 0)
                    and np.allclose(np.diff(x), x[1] - x[0], rtol=1e-5, atol=0)
                )
                curve.setClipToView(bool(uniform))
                curve.setDownsampling(auto=bool(uniform), method="peak")
            group = self.group.currentData() or ("", "", "")
            unit = self.display_unit(group[1])
            y_prefix = not scope and unit in {"W", "V", "A", "Hz", "Ω"}
            self.plot.setLabel(
                "left",
                "垂直格（各通道独立刻度）" if scope else self.group.currentText().split(" · ")[0],
                units="" if scope else unit,
                siPrefixEnableRanges=None if y_prefix else (),
            )
            self.plot.getAxis("left").enableAutoSIPrefix(y_prefix)
            axis = "s" if self.info.get("kind") == "scalar" else group[2]
            self.plot.setLabel(
                "bottom",
                "距记录开始"
                if self.info.get("kind") == "scalar"
                else "波长"
                if axis == "nm"
                else "帧内时间"
                if axis == "s"
                else "横轴",
                units=axis,
                siPrefixEnableRanges=None if axis == "s" else (),
            )
            self.plot.getAxis("bottom").enableAutoSIPrefix(axis == "s")
        finally:
            self._drawing = False

    def _fit_points(self, points) -> None:
        valid = [
            (x[np.isfinite(x) & np.isfinite(y)], y[np.isfinite(x) & np.isfinite(y)]) for _, x, y in points
        ]
        valid = [(x, y) for x, y in valid if x.size]
        if not valid:
            return
        xlo, xhi = min(float(x.min()) for x, _ in valid), max(float(x.max()) for x, _ in valid)
        ylo, yhi = min(float(y.min()) for _, y in valid), max(float(y.max()) for _, y in valid)

        def padded(lo, hi):
            padding = (hi - lo) * 0.04 if hi > lo else max(abs(lo) * 1e-6, 1e-9)
            return lo - padding, hi + padding

        self.plot.setRange(xRange=padded(xlo, xhi), yRange=padded(ylo, yhi), padding=0, disableAutoRange=True)

    def _range_changed(self, *_args) -> None:
        if not self._drawing and self.info.get("kind") == "scalar":
            self.range_timer.start(120)

    def _cursor(self, position) -> None:
        if not self.plot.sceneBoundingRect().contains(position):
            return
        point = self.plot.getViewBox().mapSceneToView(position)
        values = []
        group = self.group.currentData() or ("", "", "")
        unit = self.display_unit(group[1])
        axis_unit = "s" if self.info.get("kind") == "scalar" else group[2]
        for label, xx, yy in self.cursor_data:
            if xx.size:
                index = min(int(np.searchsorted(xx, point.x())), xx.size - 1)
                if index and abs(xx[index - 1] - point.x()) < abs(xx[index] - point.x()):
                    index -= 1
                values.append(f"{label}: {yy[index]:.9g} {unit}")
        self.cursor_text.setText(f"X = {point.x():.9g} {axis_unit} · " + "；".join(values))

    def display_unit(self, unit: str) -> str:
        if self.info.get("power_units") and unit in {"W", "dBm"}:
            return "W" if self.watts.isChecked() else "dBm"
        return unit

    def export_current(self) -> None:
        if (
            self.reader is None
            or not self.plot_data
            or self.future is not None
            or self.pending is not None
            or self.range_timer.isActive()
        ):
            self.status.setText("请等待当前曲线加载完成后导出")
            return
        quantity, unit, axis_unit = self.group.currentData()
        labels = {
            "power": "Power",
            "frequency": "Frequency",
            "temperature": "Temperature",
            "waveform": "Voltage",
            "spectrum": "Power",
            "voltage": "Voltage",
            "resistance": "Resistance",
        }
        x_label = (
            "Elapsed time (s)"
            if self.info["kind"] == "scalar"
            else {"nm": "Wavelength (nm)", "s": "Time (s)"}.get(axis_unit, f"X ({axis_unit})")
        )
        self.export_requested.emit(
            {
                "folder": self.reader.folder,
                "quantity": quantity,
                "curves": list(self.plot_data),
                "x_label": x_label,
                "y_label": f"{labels.get(quantity, quantity)} ({self.display_unit(unit)})",
                "width_mm": self.figure_width.currentData(),
                "format": self.figure_format.currentText().lower(),
                "frame": self.slider.value() if self.info["kind"] == "frames" else None,
                "series": [series["id"] for series in self.selected()],
                "bounds": self.plot.viewRange(),
                "timestamp": self.status.text(),
            }
        )
