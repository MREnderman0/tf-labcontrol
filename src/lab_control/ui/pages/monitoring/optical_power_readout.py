"""Compact optical-power rows sharing column geometry with their fixed header."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QGridLayout, QLabel, QPushButton, QSizePolicy, QWidget

from lab_control.application.power_window import PowerChannelSnapshot
from lab_control.domain.power import power_span, watts_to_dbm
from lab_control.ui.pages.monitoring.power_plot import CHANNEL_COLORS


def row_layout(widget: QWidget) -> QGridLayout:
    layout = QGridLayout(widget)
    layout.setContentsMargins(4, 0, 4, 0)
    layout.setHorizontalSpacing(8)
    layout.setVerticalSpacing(0)
    layout.setColumnMinimumWidth(0, 28)
    layout.setColumnMinimumWidth(1, 46)
    layout.setColumnStretch(2, 1)
    layout.setColumnStretch(3, 1)
    return layout


def readout_header(parent=None) -> QWidget:
    header = QWidget(parent)
    header.setObjectName("powerReadoutHeader")
    layout = row_layout(header)
    for column, text in enumerate(("监测", "通道", "功率", "峰峰值 · 30 min")):
        label = QLabel(text)
        label.setObjectName("powerReadoutHeading")
        label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter
            | (Qt.AlignmentFlag.AlignRight if column >= 2 else Qt.AlignmentFlag.AlignLeft)
        )
        layout.addWidget(label, 0, column)
    header.setFixedHeight(26)
    return header


class PowerMeterReadoutRow(QFrame):
    def __init__(self, channel: int, parent=None) -> None:
        super().__init__(parent)
        self.channel = channel
        self.selected = True
        self._history: PowerChannelSnapshot | None = None
        self._unit = "W"
        self.setObjectName("powerMeterReadoutRow")
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = row_layout(self)
        self.check = QCheckBox()
        self.check.setAccessibleName(f"CH {channel} 参与监测")
        self.check.setToolTip("参与此通道的监测与记录；取消后保留最近 30 分钟历史")
        layout.addWidget(self.check, 0, 0)
        self.channel_label = QLabel(f"CH {channel}")
        self.channel_label.setObjectName("powerReadoutChannel")
        layout.addWidget(self.channel_label, 0, 1)
        self.value = QPushButton("— W")
        self.value.setObjectName("powerReadoutValue")
        self.value.setCursor(Qt.CursorShape.PointingHandCursor)
        self.value.setAccessibleName(f"CH {channel} 功率；点击切换全部读数和曲线单位")
        self.value.setMinimumWidth(0)
        self.value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.value, 0, 2)
        self.peak = QLabel("— W")
        self.peak.setObjectName("powerReadoutPeak")
        self.peak.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.peak.setMinimumWidth(0)
        self.peak.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.peak, 0, 3)
        self.set_selected(True)

    def set_selected(self, selected: bool) -> None:
        if self.property("participating") == selected:
            return
        self.selected = selected
        self.setProperty("participating", selected)
        color = CHANNEL_COLORS[(self.channel - 1) % len(CHANNEL_COLORS)] if selected else "#788286"
        self.channel_label.setStyleSheet(f"color: {color};")
        for widget in (self, self.value, self.peak):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()
        self.update_window(self._history, self._unit)

    def update_window(self, history: PowerChannelSnapshot | None, unit: str) -> None:
        self._history, self._unit = history, unit
        peak_unit = "W" if unit == "W" else "dB"
        if not self.selected:
            self.value.setText("未监测")
            self.peak.setText("—")
            self.value.setToolTip("未监测 · 历史按最近 30 分钟保留\n点击切换全部读数及曲线的 W / dBm 显示")
            self.peak.setToolTip("重新勾选后继续使用仍在窗口内的有效样本")
            return
        watts = history.points[-1].watts if history and history.points else None
        value = watts if unit == "W" else watts_to_dbm(watts) if watts is not None else None
        span = power_span(history.minimum, history.maximum, unit) if history else None
        self.value.setText(self.format_value(value, unit))
        self.peak.setText(self.format_value(span, peak_unit))
        updated = (
            history.timestamp.replace("T", " ")
            if history and history.timestamp
            else "最近 30 分钟内无有效数据"
            if history
            else "尚未采集"
        )
        raw = (
            f"\n原始 {history.raw_value:.17g} {history.raw_unit}"
            if history and history.raw_value is not None
            else ""
        )
        full = f"{value:.17g} {unit}" if value is not None else f"— {unit}"
        self.value.setToolTip(f"{full}{raw}\n更新时间：{updated}\n点击同步切换全部读数和曲线：W / dBm")
        formula = "最大功率 − 最小功率" if unit == "W" else "10 × log10(最大功率 / 最小功率)"
        peak_full = f"{span:.17g} {peak_unit}" if span is not None else f"— {peak_unit}"
        count = len(history.points) if history else 0
        self.peak.setToolTip(
            f"{peak_full}\n{formula}\n最近 30 分钟内的 {count} 个有效样本；不足 30 分钟使用本轮已有数据"
        )

    @staticmethod
    def format_value(value: float | None, unit: str) -> str:
        if value is None:
            return f"— {unit}"
        return f"{value:.6g} {unit}" if unit == "W" else f"{value:.3f} {unit}"
