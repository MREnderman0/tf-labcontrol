"""The approved historical card layout, updated from application snapshots only."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class CategoryCard(QFrame):
    clicked = Signal(str)

    def __init__(self, category: str, title: str, description: str, parent=None) -> None:
        super().__init__(parent)
        self.category = category
        self.setObjectName("categoryCard")
        self.setFixedHeight(150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 11)
        layout.setSpacing(6)
        header = QHBoxLayout()
        icon = QLabel("●")
        icon.setObjectName("cardIcon")
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        self.visible_count = QLabel()
        self.visible_count.setObjectName("cardVisibility")
        header.addWidget(icon)
        header.addWidget(heading)
        header.addStretch()
        header.addWidget(self.visible_count)
        layout.addLayout(header)
        description_label = QLabel(description)
        description_label.setObjectName("cardDescription")
        layout.addWidget(description_label)
        summary = QHBoxLayout()
        summary.setSpacing(9)
        self.online_count = QLabel()
        self.online_count.setObjectName("cardValue")
        summary.addWidget(self.online_count)
        online_labels = QVBoxLayout()
        online_labels.setSpacing(0)
        caption = QLabel("在线设备")
        caption.setObjectName("metricCaption")
        self.discovered_count = QLabel()
        self.discovered_count.setObjectName("cardMeta")
        online_labels.addWidget(caption)
        online_labels.addWidget(self.discovered_count)
        summary.addLayout(online_labels)
        summary.addStretch()
        layout.addLayout(summary)
        metrics = QHBoxLayout()
        metrics.setSpacing(14)
        self.metrics: dict[str, QLabel] = {}
        for key, title in (("disconnected", "未连接"), ("errors", "异常"), ("bound", "已绑定")):
            cell = QVBoxLayout()
            cell.setSpacing(1)
            caption = QLabel(title)
            caption.setObjectName("metricCaption")
            number = QLabel()
            number.setObjectName("metricValue")
            self.metrics[key] = number
            cell.addWidget(caption)
            cell.addWidget(number)
            metrics.addLayout(cell)
        layout.addLayout(metrics)
        for label in self.findChildren(QLabel):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.update_counts(0, 0, 0, 0)

    def update_counts(self, discovered: int, online: int, errors: int, bound: int) -> None:
        self.visible_count.setText(f"可见 {discovered} 台")
        self.discovered_count.setText(f"发现 {discovered} 台")
        self.online_count.setText(f"{online:02d}")
        for key, value in {"disconnected": discovered - online, "errors": errors, "bound": bound}.items():
            self.metrics[key].setText(str(value))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.category)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.clicked.emit(self.category)
            event.accept()
        else:
            super().keyPressEvent(event)


class CategoryCardGrid(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("categoryGrid")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("categoryGridBody")
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(28)
        self.grid.setVerticalSpacing(20)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(body)
        self.cards: tuple[CategoryCard, ...] = ()
        self.columns = 0

    def set_cards(self, cards: tuple[CategoryCard, ...]) -> None:
        if self.cards != cards:
            for card in self.cards:
                self.grid.removeWidget(card)
                card.hide()
            self.cards = cards
            self.columns = 0
        self.reflow()

    def reflow(self) -> None:
        columns = max(1, min(3, (self.viewport().width() + 28) // 338))
        if columns == self.columns:
            return
        for card in self.cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
            card.show()
        for column in range(3):
            self.grid.setColumnStretch(column, int(column < columns))
        self.columns = columns

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.reflow()
