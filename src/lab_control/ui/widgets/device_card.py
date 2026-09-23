from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from lab_control.domain.models import CategoryStats, DeviceCategory


class DeviceCard(QFrame):
    clicked = Signal(DeviceCategory)

    def __init__(self, stats: CategoryStats, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(150)
        self._category = stats.category

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 11)
        layout.setSpacing(6)

        header = QGridLayout()
        header.setHorizontalSpacing(7)
        icon = QLabel("●")
        icon.setObjectName("cardIcon")
        title = QLabel(stats.category.label)
        title.setObjectName("cardTitle")
        visible = QLabel(f"可见 {stats.discovered} 台")
        visible.setObjectName("cardVisibility")
        header.addWidget(icon, 0, 0)
        header.addWidget(title, 0, 1)
        header.addWidget(visible, 0, 2)
        header.setColumnStretch(1, 1)
        layout.addLayout(header)

        description = QLabel(stats.category.description)
        description.setObjectName("cardDescription")
        layout.addWidget(description)

        summary = QHBoxLayout()
        summary.setSpacing(9)
        value = QLabel(f"{stats.connected:02d}")
        value.setObjectName("cardValue")
        summary.addWidget(value)
        text = QVBoxLayout()
        text.setSpacing(0)
        online = QLabel("在线设备")
        online.setObjectName("metricCaption")
        discovered = QLabel(f"发现 {stats.discovered} 台")
        discovered.setObjectName("cardMeta")
        text.addWidget(online)
        text.addWidget(discovered)
        summary.addLayout(text)
        summary.addStretch(1)
        layout.addLayout(summary)

        metrics = QHBoxLayout()
        metrics.setSpacing(14)
        self._add_metric(metrics, "未连接", stats.disconnected)
        self._add_metric(metrics, "异常", stats.errors)
        self._add_metric(metrics, "已绑定", stats.bound)
        layout.addLayout(metrics)

    @staticmethod
    def _add_metric(layout: QHBoxLayout, label: str, value: object) -> None:
        caption = QLabel(label)
        caption.setObjectName("metricCaption")
        number = QLabel(str(value))
        number.setObjectName("metricValue")
        cell = QVBoxLayout()
        cell.setSpacing(1)
        cell.addWidget(caption)
        cell.addWidget(number)
        layout.addLayout(cell)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._category)
        super().mousePressEvent(event)
