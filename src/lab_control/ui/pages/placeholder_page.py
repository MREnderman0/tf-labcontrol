from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class PlaceholderPage(QFrame):
    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("placeholderPage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 30, 34, 34)
        layout.setSpacing(12)

        eyebrow = QLabel("设计占位区域")
        eyebrow.setObjectName("eyebrow")
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        description_label = QLabel(description)
        description_label.setObjectName("pageDescription")
        description_label.setWordWrap(True)

        panel = QFrame()
        panel.setObjectName("placeholderPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 22, 22, 22)
        panel_layout.setSpacing(10)
        panel_title = QLabel("这里将放置对应的功能工作区")
        panel_title.setObjectName("panelTitle")
        panel_text = QLabel("当前只保留信息架构和视觉占位，后续确认页面布局后再接入真实服务。")
        panel_text.setObjectName("mutedText")
        panel_text.setWordWrap(True)
        panel_layout.addWidget(panel_title)
        panel_layout.addWidget(panel_text)
        panel_layout.addStretch(1)

        layout.addWidget(eyebrow)
        layout.addWidget(title_label)
        layout.addWidget(description_label)
        layout.addSpacing(18)
        layout.addWidget(panel, 1)
