"""Shared choices with a stationary popup and explicit selection.

Avoid QComboBox's private menu container, its edge scrollers and post-show
geometry adjustments. Model/data/signals remain the normal Qt interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionComboBox,
    QVBoxLayout,
    QWidget,
)

from lab_control.ui.widgets.input_wheel_guard import scroll_input_parent
from lab_control.ui.widgets.popup_surface import POPUP_MARGIN, ROW_HEIGHT, paint_popup_surface


class _ChoiceDelegate(QStyledItemDelegate):
    def __init__(self, combo: WorkspaceComboBox, parent: QWidget) -> None:
        super().__init__(parent)
        self.combo = combo

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        # The popup sizes to text up to the screen edge; rows must then fit the
        # viewport, including when very long labels need elision.
        return QSize(0, ROW_HEIGHT)

    def paint(self, painter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        active = option.state & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
        chosen = index.row() == self.combo.currentIndex()
        if (active or chosen) and enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#3b3023" if chosen else "#303638"))
            painter.drawRoundedRect(rect.adjusted(0, 1, 0, -1), 5, 5)
        if chosen:
            painter.setPen(QPen(QColor("#e3a252" if enabled else "#788286"), 1.5))
            path = QPainterPath()
            x, y = rect.right() - 16, rect.center().y()
            path.moveTo(x - 4, y)
            path.lineTo(x - 1, y + 3)
            path.lineTo(x + 5, y - 3)
            painter.drawPath(path)
        painter.setFont(option.font)
        painter.setPen(QColor("#788286" if not enabled else "#e3a252" if chosen else "#e7e9e8"))
        text = option.fontMetrics.elidedText(
            str(index.data() or ""), Qt.TextElideMode.ElideRight, rect.width() - 40
        )
        painter.drawText(rect.adjusted(12, 0, -28, 0), Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


class _ChoiceList(QListView):
    def __init__(self, combo: WorkspaceComboBox, parent: QWidget) -> None:
        super().__init__(parent)
        self.combo = combo
        self.setObjectName("workspaceChoices")
        self.viewport().setObjectName("workspaceChoicesViewport")
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setAutoScroll(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setItemDelegate(_ChoiceDelegate(combo, self))
        self.clicked.connect(combo._commit)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not event.pixelDelta().isNull():
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - event.pixelDelta().y())
        else:
            super().wheelEvent(event)
        # Consume even at either end; never scroll the underlying page.
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.combo._commit(self.currentIndex())
        elif event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.combo.hidePopup()
        else:
            super().keyPressEvent(event)


class _ChoicePopup(QFrame):
    def __init__(self, combo: WorkspaceComboBox) -> None:
        super().__init__(combo, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.combo = combo
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("workspaceChoicePopup")
        self.list = _ChoiceList(combo, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*(POPUP_MARGIN,) * 4)
        layout.setSpacing(0)
        layout.addWidget(self.list)

    def paintEvent(self, event) -> None:  # noqa: N802
        paint_popup_surface(self)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.combo._popup_closed()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Qt clears this flag between mouse presses, so renew it for each cancel.
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        if not self.rect().contains(event.position().toPoint()):
            self.combo.hidePopup()
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.accept()


class WorkspaceComboBox(QComboBox):
    """Application choices; replace_items handles asynchronous list refreshes."""

    popupClosed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaxVisibleItems(8)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pending: tuple[tuple[str, Any], ...] | None = None
        self._popup = _ChoicePopup(self)
        self._popup.list.setModel(self.model())
        self._popup_header: QWidget | None = None
        self._opening = False
        self._listening = False
        self._fit_minimum: int | None = None
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_contents)
        self.model().modelAboutToBeReset.connect(self._model_changed)
        self.model().rowsAboutToBeRemoved.connect(self._model_changed)
        self.model().rowsAboutToBeInserted.connect(self._model_changed)
        self.model().dataChanged.connect(self._model_changed)
        for signal in (
            self.model().rowsInserted,
            self.model().rowsRemoved,
            self.model().modelReset,
            self.model().dataChanged,
        ):
            signal.connect(self._schedule_fit)

    def fit_contents(self, minimum_width: int = 100) -> None:
        """Size a compact unit selector from its actual style text rectangle."""
        self._fit_minimum = minimum_width
        self._schedule_fit()

    def _schedule_fit(self, *_args) -> None:
        if self._fit_minimum is not None:
            self._fit_timer.start(0)

    def _fit_contents(self) -> None:
        if self._fit_minimum is None:
            return
        self.ensurePolished()
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        text_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, self
        )
        text_width = max(
            (self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0
        )
        self.setFixedWidth(max(self._fit_minimum, text_width + self.width() - text_rect.width() + 4))

    def event(self, event) -> bool:
        result = super().event(event)
        if hasattr(self, "_fit_timer") and event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.Polish,
            QEvent.Type.Show,
            QEvent.Type.DevicePixelRatioChange,
        ):
            self._schedule_fit()
        return result

    def view(self) -> QListView:
        return self._popup.list

    def set_popup_header(self, widget: QWidget) -> None:
        """Place an optional editor above the choices, within the popup geometry."""
        self._popup_header = widget
        layout = self._popup.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.insertWidget(0, widget)

    def _model_changed(self, *args) -> None:
        # Do not apply a deferred replacement from inside a model mutation signal.
        self._pending = None
        if self._popup.isVisible():
            self.hidePopup()

    def showPopup(self) -> None:  # noqa: N802
        if not self.isEnabled() or not self.count() or self._popup.isVisible():
            return
        view = self.view()
        view.ensurePolished()
        anchor = self.mapToGlobal(QPoint())
        screen = (QApplication.screenAt(anchor) or self.screen()).availableGeometry().adjusted(2, 2, -2, -2)
        below = max(0, screen.bottom() + 1 - (anchor.y() + self.height() + 3))
        above = max(0, anchor.y() - 3 - screen.top())
        chrome = 2 * POPUP_MARGIN
        if self._popup_header is not None:
            self._popup_header.ensurePolished()
            chrome += self._popup_header.sizeHint().height()
        desired_rows = min(self.count(), self.maxVisibleItems())
        downward = below >= desired_rows * ROW_HEIGHT + chrome or below >= above
        space = below if downward else above
        rows = min(desired_rows, max(1, (space - chrome) // ROW_HEIGHT))
        height = rows * ROW_HEIGHT + chrome
        scroll_width = view.verticalScrollBar().sizeHint().width() if rows < self.count() else 0
        text_width = max(view.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count()))
        width = min(screen.width(), max(self.width(), text_width + 44 + chrome + scroll_width))
        x = max(screen.left(), min(anchor.x(), screen.right() + 1 - width))
        y = anchor.y() + self.height() + 3 if downward else anchor.y() - 3 - height
        y = max(screen.top(), min(y, screen.bottom() + 1 - height))
        view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
            if rows < self.count()
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._opening = True
        self._popup.setGeometry(x, y, width, height)
        if layout := self._popup.layout():
            layout.activate()
        view.setCurrentIndex(self.model().index(self.currentIndex(), self.modelColumn()))
        view.scrollTo(view.currentIndex(), QAbstractItemView.ScrollHint.EnsureVisible)
        self._popup.show()
        view.setFocus(Qt.FocusReason.PopupFocusReason)
        self._opening = False
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._listening = True
        self.update()

    def hidePopup(self) -> None:  # noqa: N802
        self._popup.hide()
        super().hidePopup()

    def _popup_closed(self) -> None:
        app = QApplication.instance()
        if self._listening and app is not None:
            app.removeEventFilter(self)
        self._listening = False
        self.update()
        pending, self._pending = self._pending, None
        if pending is not None:
            self.replace_items(pending)
        self.popupClosed.emit()

    def _commit(self, index: QModelIndex) -> None:
        if not index.isValid() or not index.flags() & Qt.ItemFlag.ItemIsEnabled:
            return
        row = index.row()
        if self._pending is not None and index.data(Qt.ItemDataRole.UserRole) not in [
            key for _, key in self._pending
        ]:
            self.hidePopup()
            return
        # Apply while the editor still sees an active popup: closing first can
        # finalize a numeric draft in its old unit before the unit-change signal.
        self.setCurrentIndex(row)
        self.hidePopup()
        self.activated.emit(self.currentIndex())
        self.textActivated.emit(self.currentText())

    def replace_items(self, entries: Sequence[tuple[str, Any]], *, select_first: bool = False) -> None:
        """Preserve item data identity; defer a valid refresh until the popup closes.

        Removed keys cancel immediately, including a row merely under the pointer. Missing selection
        stays unselected; select_first is for initial population only.
        """
        items = tuple(entries)
        current = tuple((self.itemText(i), self.itemData(i)) for i in range(self.count()))
        if items == current:
            self._pending = None
            return
        old_key, old_text, old_row = self.currentData(), self.currentText(), self.currentIndex()
        keys = [key for _, key in items]
        if self._popup.isVisible():
            valid = all(key in keys for _, key in current)
            if valid:
                self._pending = items
                return
            self._pending = None
            self.hidePopup()
        blocked = self.blockSignals(True)
        self.clear()
        for label, key in items:
            self.addItem(label, key)
        row = self.findData(old_key) if old_row >= 0 else -1
        if row < 0 and select_first and items:
            row = next((i for i, (_, key) in enumerate(items) if key), 0)
        self.setCurrentIndex(row)
        self.blockSignals(blocked)
        if not blocked:
            if row != old_row or self.currentData() != old_key:
                self.currentIndexChanged.emit(row)
            if self.currentText() != old_text:
                self.currentTextChanged.emit(self.currentText())

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if self._popup.isVisible() and not self._opening:
            inside = isinstance(watched, QWidget) and (
                watched is self._popup or self._popup.isAncestorOf(watched)
            )
            if event.type() == QEvent.Type.Wheel and not inside:
                event.accept()
                return True
            if event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
                # Child layout passes (e.g. a validation label changing height)
                # must not cancel selection or finalize a draft in the old unit.
                if watched is self.window():
                    self.hidePopup()
            elif event.type() == QEvent.Type.Hide:
                if isinstance(watched, QWidget) and (watched is self or watched.isAncestorOf(self)):
                    self.hidePopup()
            elif event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
                self.hidePopup()
        return super().eventFilter(watched, event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        scroll_input_parent(self, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (
            Qt.Key.Key_Space,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Down,
            Qt.Key.Key_Up,
        ):
            self.showPopup()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.hidePopup()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = "#e3a252" if self._popup.isVisible() else "#aeb5b5" if self.isEnabled() else "#596164"
        painter.setPen(QPen(QColor(color), 1.5))
        x, y = self.width() - 16, self.height() / 2
        direction = -1 if self._popup.isVisible() else 1
        path = QPainterPath()
        path.moveTo(x - 4, y - direction * 2)
        path.lineTo(x, y + direction * 2)
        path.lineTo(x + 4, y - direction * 2)
        painter.drawPath(path)


def choose_item(parent: QWidget, title: str, label: str, items: Sequence[str]) -> tuple[str, bool]:
    """Application-owned choice dialog, replacing Qt's native getItem combo."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(380)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(label, dialog))
    combo = WorkspaceComboBox(dialog)
    combo.addItems(list(items))
    layout.addWidget(combo)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog
    )
    buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(items))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    value = combo.currentText()
    dialog.deleteLater()
    return value, accepted
