"""Channel positions depend on the viewport and selection, never the reading text."""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

ReadingKey = tuple[int, str]


class ScalarReadout(QFrame):
    WIDTH = 260
    HEIGHT = 64

    def __init__(self, key: ReadingKey, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("scalarReadout")
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(2)
        heading = QHBoxLayout()
        self.channel = QLabel(f"CH {key[0]}")
        self.channel.setObjectName("scalarReadoutChannel")
        heading.addWidget(self.channel, 1)
        self.unit = QLabel()
        self.unit.setObjectName("scalarReadoutUnit")
        heading.addWidget(self.unit, 1)
        self.unit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(heading)
        self.value = QLabel("—")
        self.value.setObjectName("scalarReadoutValue")
        layout.addWidget(self.value)
        for label in (self.channel, self.value, self.unit):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            label.setWordWrap(False)
        self.value.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.key = key

    def update_value(self, text: str, unit: str) -> None:
        self.value.setText(text)
        self.unit.setText(unit)
        description = f"CH {self.key[0]} · {self.key[1]} · {text} {unit}".rstrip()
        self.setToolTip(description)
        self.value.setAccessibleName(description)


class ScalarReadouts(QScrollArea):
    """Equal columns with bounded height, including placeholders for missing channels."""

    GAP = 8
    MAX_ROWS = 3

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(self.GAP)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self.body)
        self.cards: dict[ReadingKey, ScalarReadout] = {}
        self.keys: tuple[ReadingKey, ...] = ()
        self.columns = 0
        self.empty = QLabel("—", self.body)
        self.empty.setObjectName("mutedText")
        self.grid.addWidget(self.empty)
        self.viewport().installEventFilter(self)
        self.setFixedHeight(ScalarReadout.HEIGHT)

    def clear(self) -> None:
        self.show_values((), {})

    def show_values(self, keys: tuple[ReadingKey, ...], values: Mapping[ReadingKey, tuple[str, str]]) -> None:
        if keys != self.keys:
            for key in self.cards.keys() - set(keys):
                card = self.cards.pop(key)
                self.grid.removeWidget(card)
                card.hide()
                card.deleteLater()
            for key in keys:
                if key not in self.cards:
                    self.cards[key] = ScalarReadout(key, self.body)
            self.keys = keys
            self.empty.setVisible(not keys)
            self.reflow(force=True)
        for key, card in self.cards.items():
            card.update_value(*values.get(key, ("—", "")))

    def reflow(self, *, force: bool = False) -> None:
        columns = max(1, min(4, (self.viewport().width() + self.GAP) // (ScalarReadout.WIDTH + self.GAP)))
        if columns == self.columns and not force:
            return
        old_columns, self.columns = self.columns, columns
        for card in self.cards.values():
            self.grid.removeWidget(card)
        for column in range(max(columns, old_columns)):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, key in enumerate(self.keys):
            self.grid.addWidget(self.cards[key], index // columns, index % columns)
        rows = max(1, (len(self.keys) + columns - 1) // columns)
        height = rows * ScalarReadout.HEIGHT + (rows - 1) * self.GAP
        self.body.setMinimumHeight(height)
        visible_rows = min(rows, self.MAX_ROWS)
        self.setFixedHeight(visible_rows * ScalarReadout.HEIGHT + (visible_rows - 1) * self.GAP)

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self, self.reflow)
        return super().eventFilter(watched, event)
