from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from lab_control.domain.models import LabMode


class ModeBadge(QFrame):
    """Read-only mode indicator used in the global top bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modeBadge")
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 3, 9, 3)
        layout.setSpacing(5)
        self.dot = QLabel("●")
        self.dot.setObjectName("modeBadgeDot")
        self.label = QLabel()
        self.label.setObjectName("modeBadgeLabel")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_mode(LabMode.ELECTRONIC)

    def set_mode(self, mode: LabMode) -> None:
        self.label.setText(mode.label)
        self.setProperty("mode", mode.value)
        self.style().unpolish(self)
        self.style().polish(self)


class DemoBadge(QFrame):
    """Small read-only marker for the in-memory design shell."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("demoBadge")
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 3, 9, 3)
        layout.setSpacing(5)
        dot = QLabel("●")
        dot.setObjectName("demoBadgeDot")
        label = QLabel("演示版")
        label.setObjectName("demoBadgeLabel")
        layout.addWidget(dot)
        layout.addWidget(label)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)


class ModeSegmentedControl(QFrame):
    """Reusable two-option mode control for settings pages."""

    mode_requested = Signal(LabMode)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modeSegmentedControl")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        title = QLabel("运行模式")
        title.setObjectName("settingTitle")
        hint = QLabel("仅在设置中修改")
        hint.setObjectName("settingHint")
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(hint)
        layout.addLayout(heading)

        options = QHBoxLayout()
        options.setSpacing(8)
        self.electronic_button = self._make_button("电子学模式", "电", LabMode.ELECTRONIC)
        self.optical_button = self._make_button("光学模式", "光", LabMode.OPTICAL)
        options.addWidget(self.electronic_button)
        options.addWidget(self.optical_button)
        layout.addLayout(options)

        self.description = QLabel()
        self.description.setObjectName("settingDescription")
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.set_mode(LabMode.ELECTRONIC)

    def _make_button(self, text: str, marker: str, mode: LabMode) -> QPushButton:
        button = QPushButton(f"{marker}   {text}")
        button.setObjectName("modeOption")
        button.setCheckable(True)
        button.clicked.connect(lambda checked=False, value=mode: self.mode_requested.emit(value))
        return button

    def set_mode(self, mode: LabMode) -> None:
        self.electronic_button.blockSignals(True)
        self.optical_button.blockSignals(True)
        self.electronic_button.setChecked(mode is LabMode.ELECTRONIC)
        self.optical_button.setChecked(mode is LabMode.OPTICAL)
        self.electronic_button.blockSignals(False)
        self.optical_button.blockSignals(False)
        self.description.setText(
            "当前模式决定设备可见性、功能槽位和监测数据。切换只在空闲时执行。"
            if mode is LabMode.ELECTRONIC
            else "当前模式展示光学设备和光学测量配置，电子学设备会暂时隐藏。"
        )
