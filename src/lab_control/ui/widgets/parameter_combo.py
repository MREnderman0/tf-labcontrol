"""Consistent, fully sized list popups for units and parameter choices."""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QComboBox, QListView, QStyledItemDelegate, QWidget


class ParameterComboBox(QComboBox):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setProperty("parameterCombo", True)
        view = QListView(self)
        view.setUniformItemSizes(True)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setView(view)
        self.setItemDelegate(QStyledItemDelegate(view))
        self.setMaxVisibleItems(10)

    def showPopup(self) -> None:  # noqa: N802
        # The editor width is intentionally compact; the popup must fit the longest choice.
        view = self.view()
        view.ensurePolished()
        text_width = max(
            (view.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0
        )
        width = max(self.width(), text_width + 20 + view.verticalScrollBar().sizeHint().width())
        view.setMinimumWidth(min(width, self.screen().availableGeometry().width() - 4))
        super().showPopup()
        if not view.isVisible():
            return
        # Qt's native height calculation omits some stylesheet/container margins.
        # Measure the rendered rows and actual popup chrome in logical pixels.
        popup = view.window()
        screen = self.screen().availableGeometry()
        top = max(screen.top(), min(self.mapToGlobal(QPoint(0, 0)).y(), screen.bottom() + 1))
        bottom = max(screen.top(), min(self.mapToGlobal(QPoint(0, self.height())).y(), screen.bottom() + 1))
        above, below = max(0, top - screen.top()), max(0, screen.bottom() + 1 - bottom)
        rows = min(self.count(), self.maxVisibleItems())
        chrome = popup.height() - view.viewport().height()
        desired = sum(view.sizeHintForRow(i) for i in range(rows)) + chrome
        space = max(above, below)
        row_height = view.sizeHintForRow(0)
        height = min(desired, max(1, (space - chrome) // row_height) * row_height + chrome, space)
        y = bottom if below >= height else top - height
        popup.resize(popup.width(), height)
        x = max(screen.left(), min(popup.x(), screen.right() + 1 - popup.width()))
        popup.move(x, y)
        view.scrollTo(view.currentIndex())
