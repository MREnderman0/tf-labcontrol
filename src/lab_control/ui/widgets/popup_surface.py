"""Shared popup chrome for choices and action menus, in logical pixels."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QMenu

ROW_HEIGHT = 34
SURFACE_PADDING = 6
SHADOW_MARGIN = 8
POPUP_MARGIN = SURFACE_PADDING + SHADOW_MARGIN


def paint_popup_surface(widget) -> None:
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    panel = widget.rect().adjusted(SHADOW_MARGIN, SHADOW_MARGIN, -SHADOW_MARGIN, -SHADOW_MARGIN)
    painter.setPen(Qt.PenStyle.NoPen)
    for spread in range(SHADOW_MARGIN, 0, -1):
        painter.setBrush(QColor(0, 0, 0, 5 + (SHADOW_MARGIN - spread) * 2))
        painter.drawRoundedRect(panel.adjusted(-spread, -spread, spread, spread), 8 + spread, 8 + spread)
    painter.setBrush(QColor("#1d2325"))
    painter.setPen(QPen(QColor("#505957"), 1))
    painter.drawRoundedRect(panel, 8, 8)


class WorkspaceMenu(QMenu):
    """QAction semantics with the same surface and cancellation policy as choices."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("workspaceActionMenu")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)

    def paintEvent(self, event) -> None:  # noqa: N802
        paint_popup_surface(self)
        super().paintEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        super().mousePressEvent(event)
