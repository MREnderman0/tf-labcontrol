"""Overview-local device dialog. Presentation only; no device I/O."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from lab_control.ui.widgets.category_cards import CategoryCard


def _scroll(parent: QWidget) -> QScrollArea:
    area = QScrollArea(parent)
    area.setObjectName("devicePoolScroll")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setMinimumSize(0, 0)
    area.viewport().setObjectName("devicePoolViewport")
    return area


class DevicePool(QFrame):
    categoryRequested = Signal(str)

    def __init__(self, overview: QWidget) -> None:
        super().__init__(overview)
        self.setObjectName("devicePoolOverlay")
        self.overview = overview
        self.cards: tuple[CategoryCard, ...] = ()
        self._return_focus: QWidget | None = None
        self._narrow: bool | None = None
        self.panel = QFrame(self)
        self.panel.setObjectName("devicePoolDialog")
        self.panel.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        shadow = QGraphicsDropShadowEffect(self.panel)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 125))
        self.panel.setGraphicsEffect(shadow)
        layout = QVBoxLayout(self.panel)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(14)
        self.heading = QHBoxLayout()
        layout.addLayout(self.heading)
        self.filters = QGridLayout()
        self.filters.setHorizontalSpacing(10)
        self.filters.setVerticalSpacing(8)
        layout.addLayout(self.filters)
        self.pages = QStackedWidget()
        self.pages.setObjectName("devicePoolPages")
        self.pages.setMinimumSize(0, 0)
        self.pages.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.pages, 1)
        wide = QWidget()
        wide.setObjectName("devicePoolWide")
        self.wide_layout = QHBoxLayout(wide)
        self.wide_layout.setContentsMargins(0, 0, 0, 0)
        self.wide_layout.setSpacing(16)
        self.detail_scroll = _scroll(wide)
        self.pages.addWidget(wide)
        self.body_scroll = _scroll(self.pages)
        self.body = QWidget()
        self.body.setObjectName("devicePoolBody")
        self.narrow_layout = QVBoxLayout(self.body)
        self.narrow_layout.setContentsMargins(0, 0, 6, 0)
        self.narrow_layout.setSpacing(14)
        self.body_scroll.setWidget(self.body)
        self.pages.addWidget(self.body_scroll)
        self.footer = QVBoxLayout()
        self.footer.setSpacing(8)
        layout.addLayout(self.footer)
        self.list_widget: QWidget | None = None
        self.details_widget: QWidget | None = None
        self.filter_widgets: tuple[QWidget, ...] = ()
        self.action_widgets: tuple[QWidget, ...] = ()
        self.action_grid = QGridLayout()
        self.action_grid.setSpacing(8)
        self.action_grid.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.footer.addLayout(self.action_grid)
        self._actions_signature: tuple = ()
        app = QApplication.instance()
        assert app is not None
        app.installEventFilter(self)
        self.hide()

    @property
    def scroll_area(self) -> QScrollArea:
        return self.body_scroll if self._narrow else self.detail_scroll

    def set_content(self, devices: QWidget, details: QWidget, filters: tuple[QWidget, ...]) -> None:
        self.list_widget, self.details_widget, self.filter_widgets = devices, details, filters
        self.position_in_overview()

    def present(self, trigger: QWidget | None = None) -> None:
        if not self.isVisible():
            self._return_focus = trigger or QApplication.focusWidget()
        self.position_in_overview()
        self.show()
        self.raise_()
        target = self.list_widget.focusProxy() if self.list_widget is not None else None
        if target is None or not target.isVisible() or not target.isEnabled():
            target = self.filter_widgets[0] if self.filter_widgets else self.panel
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_actions(self, widgets: tuple[QWidget, ...]) -> None:
        self.action_widgets = widgets
        for index, widget in enumerate(widgets):
            self.action_grid.addWidget(widget, 0, index)
            widget.show()
        self.reflow_actions()

    def reflow_actions(self) -> None:
        widgets = tuple(widget for widget in self.action_widgets if not widget.isHidden())
        largest = max((widget.sizeHint().width() for widget in widgets), default=1)
        columns = max(1, min(len(widgets), (self.panel.width() - 40 + 8) // (largest + 8)))
        signature = (widgets, columns)
        if signature == self._actions_signature:
            return
        self._actions_signature = signature
        while self.action_grid.count():
            self.action_grid.takeAt(0)
        for index, widget in enumerate(widgets):
            self.action_grid.addWidget(widget, index // columns, index % columns)

    def position_in_overview(self) -> None:
        self.setGeometry(self.overview.rect())
        width, height = min(960, max(0, self.width() - 48)), min(640, max(0, self.height() - 48))
        self.panel.setGeometry((self.width() - width) // 2, (self.height() - height) // 2, width, height)
        self.reflow_actions()
        narrow = width < 760
        if self.list_widget is not None and self.details_widget is not None and narrow != self._narrow:
            focused = QApplication.focusWidget()
            self.wide_layout.removeWidget(self.list_widget)
            self.narrow_layout.removeWidget(self.list_widget)
            self.narrow_layout.removeWidget(self.details_widget)
            self.detail_scroll.takeWidget()
            if narrow:
                self.list_widget.setMinimumWidth(0)
                self.list_widget.setMaximumWidth(16777215)
                self.list_widget.setFixedHeight(132)
                self.narrow_layout.addWidget(self.list_widget)
                self.narrow_layout.addWidget(self.details_widget)
                self.pages.setCurrentIndex(1)
            else:
                self.list_widget.setMinimumHeight(0)
                self.list_widget.setMaximumHeight(16777215)
                self.list_widget.setFixedWidth(264)
                self.wide_layout.insertWidget(0, self.list_widget)
                self.detail_scroll.setWidget(self.details_widget)
                self.wide_layout.addWidget(self.detail_scroll, 1)
                self.pages.setCurrentIndex(0)
            self.list_widget.show()
            self.details_widget.show()
            self._narrow = narrow
            if focused and self.panel.isAncestorOf(focused) and self.isVisible():
                focused.setFocus()
        for index, widget in enumerate(self.filter_widgets):
            row, column = (0, index) if width >= 600 else ((0, 0) if index == 0 else (1, index - 1))
            self.filters.addWidget(widget, row, column, 1, 2 if width < 600 and index == 0 else 1)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(5, 8, 10, 172))

    def hideEvent(self, event) -> None:  # noqa: N802
        popup = QApplication.activePopupWidget()
        if popup and self.isAncestorOf(popup):
            popup.close()
        super().hideEvent(event)
        target, self._return_focus = self._return_focus, None
        if target and target.isVisible() and target.isEnabled():
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _outside_press(self, position: QPoint) -> None:
        # Only exposed card regions switch category. Never replay backdrop clicks
        # into scan/add-resource buttons or other underlying controls.
        for card in self.cards:
            widget: QWidget | None = card
            while widget is not None and widget is not self.overview:
                if not widget.isVisible() or not widget.rect().contains(widget.mapFromGlobal(position)):
                    break
                widget = widget.parentWidget()
            if widget is self.overview:
                self._return_focus = card
                self.categoryRequested.emit(card.category)
                return
        self.hide()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.overview and event.type() == QEvent.Type.Resize:
            self.position_in_overview()
        if not self.isVisible() or not isinstance(watched, QWidget):
            return False
        if QApplication.activePopupWidget() or QApplication.activeModalWidget():
            return False
        if watched.window() is not self.overview.window():
            return False
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.hide()
                return True
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and self.panel.isAncestorOf(watched):
                forward = (
                    event.key() == Qt.Key.Key_Tab
                    and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                )
                candidate = watched
                while True:
                    next_candidate = (
                        candidate.nextInFocusChain() if forward else candidate.previousInFocusChain()
                    )
                    if next_candidate is None:
                        return True
                    candidate = next_candidate
                    if candidate is watched:
                        return True
                    if (
                        self.panel.isAncestorOf(candidate)
                        and candidate.isVisible()
                        and candidate.isEnabled()
                        and candidate.focusPolicy() & Qt.FocusPolicy.TabFocus
                    ):
                        candidate.setFocus(Qt.FocusReason.TabFocusReason)
                        return True
        if event.type() == QEvent.Type.MouseButtonPress:
            position = event.globalPosition().toPoint()
            local = self.mapFromGlobal(position)
            if self.rect().contains(local) and not self.panel.geometry().contains(local):
                if event.button() == Qt.MouseButton.LeftButton:
                    self._outside_press(position)
                return True
        return False


class DeviceRowDelegate(QStyledItemDelegate):
    """Device cards using the existing tree's data and selection model."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(0, 88)

    def paint(self, painter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(1, 2, -1, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setBrush(QColor("#30291f" if selected else "#202628" if hovered else "#191e20"))
        painter.setPen(QColor("#886538" if selected else "#303739"))
        painter.drawRoundedRect(rect, 7, 7)
        name, _, model = str(index.data() or "").partition("\n")
        font = option.font
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#f1e8d9" if selected else "#e2e6e4"))
        painter.drawText(
            rect.adjusted(12, 9, -10, 0),
            Qt.AlignmentFlag.AlignTop,
            painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, rect.width() - 24),
        )
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor("#97a3a6"))
        painter.drawText(
            rect.adjusted(12, 29, -10, 0),
            Qt.AlignmentFlag.AlignTop,
            painter.fontMetrics().elidedText(model, Qt.TextElideMode.ElideRight, rect.width() - 24),
        )
        status_index = index.siblingAtColumn(3)
        status = str(status_index.data() or "").removeprefix("● ")
        foreground = status_index.data(Qt.ItemDataRole.ForegroundRole)
        color = foreground.color() if foreground else QColor("#97a3a6")
        pill = QRect(
            rect.x() + 12,
            rect.y() + 53,
            min(rect.width() - 24, painter.fontMetrics().horizontalAdvance(status) + 18),
            22,
        )
        fill = QColor(color)
        fill.setAlpha(22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, 4, 4)
        painter.setPen(color)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, status)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QColor("#e3a252"))
            painter.drawLine(rect.left() + 1, rect.top() + 15, rect.left() + 1, rect.bottom() - 15)
        painter.restore()


class DeviceDetails(QFrame):
    """Stable, selectable fields instead of replacing a text document on refresh."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("devicePoolDetails")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(12)
        self.title = QLabel("选择一台设备")
        self.title.setObjectName("poolDeviceName")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("poolCaption")
        self.subtitle.setTextFormat(Qt.TextFormat.PlainText)
        self.subtitle.setWordWrap(True)
        self.subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.subtitle)
        self.problem = QLabel()
        self.problem.setObjectName("poolConnectionError")
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        self.problem.setWordWrap(True)
        self.problem.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.problem.hide()
        layout.addWidget(self.problem)
        self.values: dict[str, QLabel] = {}
        self.groups: list[QFrame] = []
        for title, fields in (
            (
                "设备身份",
                (
                    ("manufacturer", "厂家"),
                    ("model", "型号"),
                    ("serial", "序列号"),
                    ("firmware", "固件"),
                    ("confidence", "身份可信度"),
                    ("acceptance", "实机验收"),
                ),
            ),
            (
                "连接信息",
                (
                    ("connection", "软件连接"),
                    ("task", "当前任务"),
                    ("driver", "驱动状态"),
                    ("adapter", "适配器"),
                    ("resource", "主连接资源"),
                    ("aliases", "备用资源"),
                ),
            ),
            ("功能槽位", (("mode", "工作模式"), ("slot", "当前槽位"))),
            (
                "发现记录",
                (
                    ("presence", "当前发现"),
                    ("identity_state", "身份状态"),
                    ("seen", "最近发现"),
                    ("probe", "身份探测"),
                    ("error", "最近错误"),
                    ("readback", "最近状态读回"),
                ),
            ),
        ):
            group = QFrame()
            group.setObjectName("poolInfoCard")
            grid = QGridLayout(group)
            grid.setContentsMargins(14, 12, 14, 14)
            grid.setHorizontalSpacing(20)
            grid.setVerticalSpacing(10)
            heading = QLabel(title)
            heading.setObjectName("poolGroupTitle")
            grid.addWidget(heading, 0, 0, 1, 2)
            row, column = 1, 0
            for key, caption in fields:
                full = key in {"resource", "aliases", "error", "readback"}
                cell = QWidget()
                cell.setObjectName("poolInfoCell")
                cell_layout = QVBoxLayout(cell)
                cell_layout.setContentsMargins(0, 0, 0, 0)
                cell_layout.setSpacing(3)
                label = QLabel(caption)
                label.setObjectName("poolCaption")
                cell_layout.addWidget(label)
                value = QLabel("—")
                value.setObjectName("poolField")
                value.setTextFormat(Qt.TextFormat.PlainText)
                value.setWordWrap(True)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                value.setMinimumWidth(0)
                value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                cell_layout.addWidget(value)
                self.values[key] = value
                grid.addWidget(cell, row, column, 1, 2 if full else 1)
                if full or column == 1:
                    row, column = row + 1, 0
                else:
                    column = 1
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            self.groups.append(group)
            layout.addWidget(group)
        layout.addStretch()

    def set_fields(self, title: str, subtitle: str, fields: dict[str, str]) -> None:
        if self.title.text() != title:
            self.title.setText(title)
        if self.subtitle.text() != subtitle:
            self.subtitle.setText(subtitle)
        error = fields.get("error", "")
        problem = (
            ""
            if error in {"", "无"}
            else (
                "连接失败：VISA 未能打开设备。详细原因见下方“最近错误”。"
                if "VI_ERROR_RSRC_NFOUND" in error
                else "异常：" + error[:180]
            )
        )
        if self.problem.text() != problem:
            self.problem.setText(problem)
        self.problem.setToolTip(error if problem else "")
        self.problem.setVisible(bool(problem))
        for group in self.groups:
            group.setVisible(bool(fields))
        for key, value in fields.items():
            text = str(value)
            if self.values[key].text() != text:
                self.values[key].setText(text)
