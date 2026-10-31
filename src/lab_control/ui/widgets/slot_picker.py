"""Named slot choices with creation and deletion inside the shared popup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.bulk_connection import device_label
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox, _ChoiceDelegate

if TYPE_CHECKING:
    from lab_control.application.runtime import ApplicationRuntime


def _delete_rect(rect: QRect) -> QRect:
    return QRect(rect.right() - 57, rect.top() + 4, 52, rect.height() - 8)


class _SlotDelegate(_ChoiceDelegate):
    def paint(self, painter, option, index) -> None:
        combo = self.combo
        assert isinstance(combo, SlotComboBox)
        if index.data(Qt.ItemDataRole.UserRole) not in combo.deletable:
            super().paint(painter, option, index)
            return
        text_option = QStyleOptionViewItem(option)
        text_option.rect = option.rect.adjusted(0, 0, -62, 0)
        super().paint(painter, text_option, index)
        painter.save()
        painter.setPen(QColor("#75514a"))
        painter.setBrush(QColor("#302526"))
        painter.drawRoundedRect(_delete_rect(option.rect), 4, 4)
        painter.setPen(QColor("#ee9690"))
        painter.drawText(_delete_rect(option.rect), Qt.AlignmentFlag.AlignCenter, "删除")
        painter.restore()


class SlotComboBox(WorkspaceComboBox):
    createRequested = Signal(str)
    deleteRequested = Signal(str)
    refreshRequested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.deletable: set[str] = set()
        self._delete_pressed = ""
        header = QWidget()
        header.setObjectName("slotChoiceHeader")
        layout = QVBoxLayout(header)
        layout.setContentsMargins(4, 4, 4, 8)
        layout.setSpacing(6)
        self.name_input = QLineEdit()
        self.name_input.setObjectName("newSlotName")
        self.name_input.setPlaceholderText("输入新槽位名称，按回车创建")
        self.name_input.setAccessibleName("新功能槽位名称")
        self.name_input.returnPressed.connect(self._create)
        layout.addWidget(self.name_input)
        self.feedback = QLabel("未绑定的槽位可删除；同名槽位直接选用。")
        self.feedback.setObjectName("mutedText")
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.feedback)
        self.set_popup_header(header)
        self.view().setItemDelegate(_SlotDelegate(self, self.view()))
        self.view().viewport().installEventFilter(self)
        self.view().installEventFilter(self)
        self.name_input.installEventFilter(self)

    def _create(self) -> None:
        self.createRequested.emit(self.name_input.text())

    def showPopup(self) -> None:  # noqa: N802
        if not self._popup.isVisible():
            self.refreshRequested.emit()
        super().showPopup()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.view().viewport() and event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
        ):
            if event.button() == Qt.MouseButton.LeftButton:
                point = event.position().toPoint()
                index = self.view().indexAt(point)
                slot = index.data(Qt.ItemDataRole.UserRole)
                on_delete = slot in self.deletable and _delete_rect(self.view().visualRect(index)).contains(
                    point
                )
                if event.type() == QEvent.Type.MouseButtonPress:
                    self._delete_pressed = slot if on_delete else ""
                    if on_delete:
                        return True
                elif self._delete_pressed:
                    pressed, self._delete_pressed = self._delete_pressed, ""
                    if on_delete and slot == pressed:
                        self.deleteRequested.emit(slot)
                    return True
        if event.type() == QEvent.Type.KeyPress:
            if watched is self.view():
                if event.key() == Qt.Key.Key_Delete:
                    slot = self.view().currentIndex().data(Qt.ItemDataRole.UserRole)
                    if slot in self.deletable:
                        self.deleteRequested.emit(slot)
                    return True
                if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                    self.name_input.setFocus()
                    return True
            elif watched is self.name_input:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    # Consume Return so it cannot also accept the parent dialog.
                    self._create()
                    return True
                if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Tab):
                    self.view().setFocus()
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self.hidePopup()
                    return True
        return super().eventFilter(watched, event)


class SlotBindingDialog(QDialog):
    def __init__(self, runtime: ApplicationRuntime, device_id: str, category: str, mode: str, parent=None):
        super().__init__(parent)
        self.runtime, self.device_id, self.category, self.mode = runtime, device_id, category, mode
        self.setWindowTitle("功能槽位")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        hint = QLabel("选择或新建槽位；已占用槽位可确认替换。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.combo = SlotComboBox(self)
        self.combo.createRequested.connect(self._create)
        self.combo.deleteRequested.connect(self._delete)
        self.combo.refreshRequested.connect(self.refresh)
        layout.addWidget(self.combo)
        note = QLabel("新建和删除立即保存，点击“确定”应用绑定。")
        note.setObjectName("mutedText")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.refresh()
        record = next(r for r in runtime.inventory.all() if r["id"] == device_id)
        self.combo.setCurrentIndex(self.combo.findData(record.get("slots", {}).get(mode, "")))

    def refresh(self) -> None:
        owners = {
            r["slots"][self.mode]: r
            for r in self.runtime.inventory.all()
            if r.get("slots", {}).get(self.mode)
        }
        entries = [("解除绑定", "")]
        self.combo.deletable = set()
        for slot, label in self.runtime.repository.function_slots.definitions(
            self.category, self.mode
        ).items():
            owner = owners.get(slot)
            suffix = f"（已绑定：{device_label(owner)}）" if owner else "（空闲）"
            entries.append((label + suffix, slot))
            if owner is None:
                self.combo.deletable.add(slot)
        self.combo.replace_items(entries)
        if self.combo.currentIndex() < 0:
            self.combo.setCurrentIndex(0)

    def _create(self, name: str) -> None:
        try:
            slot = self.runtime.create_slot(self.category, name, self.mode)
        except Exception as exc:
            self.combo.feedback.setText(str(exc))
            return
        self.combo.hidePopup()
        self.refresh()
        self.combo.setCurrentIndex(self.combo.findData(slot))
        self.combo.name_input.clear()
        self.combo.feedback.setText("未绑定的槽位可删除；同名槽位直接选用。")

    def _delete(self, slot: str) -> None:
        try:
            self.runtime.delete_slot(slot, self.mode)
        except Exception as exc:
            self.combo.feedback.setText(str(exc))
        self.combo.hidePopup()
        self.refresh()
        self.combo.showPopup()


def choose_slot(
    parent, runtime: ApplicationRuntime, device_id: str, category: str, mode: str
) -> tuple[str, bool]:
    dialog = SlotBindingDialog(runtime, device_id, category, mode, parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    slot = dialog.combo.currentData() or ""
    dialog.deleteLater()
    return slot, accepted
