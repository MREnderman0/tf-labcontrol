from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from lab_control.demo.store import DemoStore
from lab_control.domain.models import LabMode
from lab_control.ui.widgets.mode_selector import ModeSegmentedControl


class SoftwareSettingsPage(QFrame):
    mode_requested = Signal(LabMode)

    def __init__(self, store: DemoStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.store = store
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 24, 30, 34)
        layout.setSpacing(14)

        eyebrow = QLabel("系统设置 / 软件设置")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("软件设置")
        title.setObjectName("pageTitle")
        description = QLabel("管理工作台显示和实验运行模式。通信参数与数据存储位于其他设置页。")
        description.setObjectName("pageDescription")
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addSpacing(12)

        self.mode_control = ModeSegmentedControl()
        self.mode_control.mode_requested.connect(self.mode_requested)
        layout.addWidget(self.mode_control)

        note = QFrame()
        note.setObjectName("settingsNote")
        note_layout = QVBoxLayout(note)
        note_layout.setContentsMargins(16, 14, 16, 14)
        note_title = QLabel("设计演示边界")
        note_title.setObjectName("panelTitle")
        note_text = QLabel("当前模式切换只更新界面中的演示状态，不会连接设备、发送命令或保存配置。")
        note_text.setObjectName("mutedText")
        note_text.setWordWrap(True)
        note_layout.addWidget(note_title)
        note_layout.addWidget(note_text)
        layout.addWidget(note)
        layout.addStretch(1)
        self.store.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.mode_control.set_mode(self.store.mode)
