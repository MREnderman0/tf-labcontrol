"""Keep wheel gestures for scrolling, never for changing form values."""

from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDial,
    QSlider,
    QWidget,
)


def scroll_input_parent(widget: QWidget, event: QWheelEvent) -> None:
    """Forward once to the enclosing page without leaking across a dialog."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            viewport = parent.viewport()
            forwarded = QWheelEvent(
                viewport.mapFromGlobal(event.globalPosition().toPoint()),
                event.globalPosition(),
                event.pixelDelta(),
                event.angleDelta(),
                event.buttons(),
                event.modifiers(),
                event.phase(),
                event.inverted(),
            )
            QApplication.sendEvent(viewport, forwarded)
            break
        if parent.isWindow():
            break
        parent = parent.parentWidget()
    event.accept()


class InputWheelGuard(QObject):
    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel or not isinstance(watched, QWidget):
            return False
        widget: QWidget | None = watched
        while widget is not None:
            # Open choice lists, text areas, plots and scrollbars retain their
            # own scrolling. A spin box's embedded line edit has no such boundary.
            if isinstance(widget, QAbstractScrollArea):
                return False
            if isinstance(widget, (QAbstractSpinBox, QComboBox, QSlider, QDial)):
                scroll_input_parent(widget, event)
                return True
            if widget.isWindow():
                break
            widget = widget.parentWidget()
        return False


def install_input_wheel_guard() -> InputWheelGuard:
    """One app-wide filter also covers dialogs and controls created later."""
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("Create QApplication before installing the input policy")
    guard = getattr(app, "_input_wheel_guard", None)
    if guard is None:
        guard = InputWheelGuard(app)
        app.installEventFilter(guard)
        cast(Any, app)._input_wheel_guard = guard
    return guard
