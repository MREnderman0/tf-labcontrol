"""Presentation of the existing recording lifecycle; no instrument or storage I/O."""

from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QToolButton, QWidget


def recording_phase(task: dict[str, Any]) -> str:
    if task.get("recording"):
        return "recording"
    recorder = task.get("recording_state", {})
    return "draining" if recorder and not recorder.get("binary_done", False) else "idle"


def _property(widget: QWidget, name: str, value: Any) -> None:
    if widget.property(name) != value:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


def update_recording_frame(frame: QWidget, task: dict[str, Any]) -> None:
    phase = recording_phase(task)
    _property(frame, "recordingFrame", True)
    _property(frame, "recordingPhase", phase)
    hint = {
        "recording": "本设备正在记录；保存范围以当前记录任务为准。",
        "draining": "本设备正在收尾：等待已接收数据写入二进制记录。",
        "idle": "",
    }[phase]
    if frame.toolTip() != hint:
        frame.setToolTip(hint)


def _icon(stop: bool) -> QIcon:
    icon = QIcon()
    for ratio in (1, 2, 3):
        pixmap = QPixmap(16 * ratio, 16 * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f1bab5" if stop else "#de7773"))
        if stop:
            painter.drawRoundedRect(QRectF(3, 3, 10, 10), 1.5, 1.5)
        else:
            painter.drawEllipse(QRectF(3, 3, 10, 10))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class RecordingButton(QToolButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("recordButton")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(14, 14))
        self.setFixedSize(108, 30)
        self.icons = {False: _icon(False), True: _icon(True)}
        self.refresh({}, connected=False, busy=False, locked=False)

    def refresh(self, task: dict[str, Any], *, connected: bool, busy: bool, locked: bool) -> None:
        phase = recording_phase(task)
        _property(self, "recordingPhase", phase)
        text = {"idle": "开始记录", "recording": "停止记录", "draining": "收尾中…"}[phase]
        self.setText(text)
        self.setAccessibleName(text)
        self.setIcon(self.icons[phase != "idle"])
        self.setEnabled(
            bool(connected and task.get("running") and not busy and phase != "draining")
            and (phase == "recording" or not locked)
        )
        self.setToolTip(
            "等待二进制记录收尾；完成后可再次记录，CSV 转换在后台继续。"
            if phase == "draining"
            else "停止记录并继续监测"
            if phase == "recording"
            else "逐帧保存监测数据"
            if self.isEnabled()
            else "开始监测且当前操作完成后可记录"
        )
