"""Device-wide source settings with transient, explicitly applied drafts."""

from __future__ import annotations

import math

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from lab_control.domain.instruments import DeviceCommand
from lab_control.domain.readback import normalized_value, usable_reading
from lab_control.ui.controller import WorkspaceController

# Deliberate allow list: adding a capability never automatically exposes it here.
SETTING_SECTIONS = (("reference_clock", "参考源", (("INT", "内部参考"), ("EXT", "外部参考"))),)


def settings_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    for index in range(32):
        angle = index * math.tau / 32
        radius = 26 if index % 4 in (1, 2) else 21
        x, y = 32 + radius * math.cos(angle), 32 + radius * math.sin(angle)
        path.moveTo(x, y) if index == 0 else path.lineTo(x, y)
    path.closeSubpath()
    path.addEllipse(23, 23, 18, 18)
    path.setFillRule(Qt.FillRule.OddEvenFill)
    painter.fillPath(path, QColor("#c3cac8"))
    painter.end()
    return QIcon(pixmap)


class SourceSettingsDialog(QDialog):
    def __init__(self, controller: WorkspaceController, device_id: str, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.device_id = controller, device_id
        self.mode = controller.mode
        self.session_id = controller.states.get(device_id, {}).get("sessionId")
        self.busy = False
        self.current: dict[str, str | None] = {}
        self.drafts: dict[str, str] = {}
        self.choices: dict[str, dict[str, QPushButton]] = {}
        self.current_labels: dict[str, QLabel] = {}
        self.choice_groups: dict[str, QButtonGroup] = {}
        self.setObjectName("sourceSettingsDialog")
        self.setWindowTitle("高级设置")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(18)
        header = QHBoxLayout()
        title = QLabel("高级设置", self)
        title.setObjectName("sourceSettingsTitle")
        header.addWidget(title)
        header.addStretch()
        self.close_button = QPushButton("×", self)
        self.close_button.setObjectName("sourceSettingsClose")
        self.close_button.setAccessibleName("关闭高级设置")
        self.close_button.setFixedSize(30, 30)
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        model = controller.records.get(device_id, {}).get("identity", {}).get("model", "信号源")
        description = QLabel(f"{model}  ·  以下设置作用于整台设备", self)
        description.setObjectName("mutedText")
        layout.addWidget(description)
        for name, label, options in SETTING_SECTIONS:
            section = QFrame(self)
            section.setObjectName("sourceSettingsSection")
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(16, 16, 16, 16)
            section_layout.setSpacing(12)
            section_layout.addWidget(QLabel(label, section))
            current = QLabel("当前：未读取", section)
            current.setObjectName("mutedText")
            self.current_labels[name] = current
            section_layout.addWidget(current)
            row = QHBoxLayout()
            row.setSpacing(8)
            self.choices[name] = {}
            group = QButtonGroup(section)
            group.setExclusive(True)
            self.choice_groups[name] = group
            for value, text in options:
                choice = QPushButton(text, section)
                choice.setObjectName("referenceChoice")
                choice.setCheckable(True)
                choice.setMinimumHeight(42)
                choice.clicked.connect(lambda checked=False, n=name, v=value: self.choose(n, v))
                group.addButton(choice)
                row.addWidget(choice, 1)
                self.choices[name][value] = choice
            section_layout.addLayout(row)
            layout.addWidget(section)
        self.status = QLabel(self)
        self.status.setObjectName("inputFeedback")
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button = QPushButton("应用", self)
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply)
        for button in (self.cancel_button, self.apply_button):
            button.setFixedSize(84, 38)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        layout.addLayout(footer)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def capability(self, name):
        if self.device_id not in self.controller.records:
            return None
        return next(
            (cap for cap in self.controller.capabilities(self.device_id).operations if cap.name == name), None
        )

    def sync_keys(self):
        return tuple(
            (1, name)
            for name in self.choices
            if (cap := self.capability(name)) and cap.available and cap.readable and not cap.channel_scoped
        )

    def context_valid(self) -> bool:
        state = self.controller.states.get(self.device_id, {})
        return bool(
            self.controller.mode == self.mode
            and state.get("sessionId") == self.session_id
            and self.controller.has_connected_session(self.device_id)
            and getattr(self.parent(), "device_id", self.device_id) == self.device_id
        )

    def choose(self, name: str, value: str) -> None:
        self.drafts[name] = value
        self.message("")
        self.refresh()

    def message(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def refresh(self) -> None:
        if not shiboken6.isValid(self):
            return
        state = self.controller.states.get(self.device_id, {})
        valid = self.context_valid()
        idle = valid and not self.busy and not state.get("controlPending")
        idle &= state.get("connectionState") in {"connected", "busy"}
        idle &= self.device_id not in self.controller.group_devices
        idle &= state.get("activeTask") != "continuous_acquisition"
        idle &= not state.get("controlsLocked")
        frames = self.controller.runtime.sessions.readback_frames(self.device_id)
        ready = bool(idle and not state.get("capabilityStale", True))
        changed = False
        for name, choices in self.choices.items():
            cap = self.capability(name)
            available = bool(
                cap and cap.available and cap.readable and cap.writable and not cap.channel_scoped
            )
            health, reason = self.controller.runtime.monitoring.settings_status(self.device_id, ((1, name),))
            frame = frames.get((1, name))
            fresh = bool(
                valid
                and cap
                and frame
                and health == "正常"
                and (frame.session_id, frame.configuration_version)
                == (self.session_id, state.get("configurationVersion"))
                and usable_reading(frame.reading, cap.parameters)
            )
            current = None
            if fresh and cap and frame:
                raw = frame.reading.value
                raw = raw.get("value") if isinstance(raw, dict) else raw
                current = normalized_value(raw, cap.parameters["value"])
            self.current[name] = current
            self.current_labels[name].setText(
                "此型号未开放参考源设置"
                if not available
                else "当前：" + choices[current].text()
                if current in choices
                else "当前：未读取"
                if not frame
                else "当前：待核实"
            )
            self.current_labels[name].setToolTip((cap.reason if cap else "") or reason)
            selected = self.drafts.get(name, current)
            group = self.choice_groups[name]
            group.setExclusive(False)
            for value, button in choices.items():
                button.setChecked(value == selected)
                button.setEnabled(bool(idle and available))
            group.setExclusive(True)
            if name in self.drafts and selected != current:
                changed = True
                ready &= available and fresh
        self.apply_button.setEnabled(bool(ready and changed))
        self.apply_button.setText("应用中…" if self.busy else "应用")
        self.cancel_button.setEnabled(not self.busy)
        self.close_button.setEnabled(not self.busy)
        if not valid:
            self.message("设备连接或模式已变化，请关闭后重新打开设置。")

    def apply(self) -> None:
        self.refresh()
        if not self.apply_button.isEnabled():
            return
        # Only one operation is currently registered. No implicit batch or replay.
        name, value = next((n, v) for n, v in self.drafts.items() if v != self.current.get(n))
        cap = self.capability(name)
        if cap is None:
            return
        try:
            if (
                cap.requires_confirmation
                and QMessageBox.question(
                    self,
                    "确认设备操作",
                    f"将整台设备设置为{self.choices[name][value].text()}？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            self.busy = True
            self.message("")
            self.refresh()
            self.controller.apply(
                self.device_id, DeviceCommand(name, {"value": value}, 1, True), self.applied, self.failed
            )
        except Exception as exc:
            self.failed(exc)

    def applied(self, result) -> None:
        if not shiboken6.isValid(self):
            return
        self.busy = False
        if result.outcome == "success":
            self.drafts.pop(result.operation, None)
            raw = result.readback
            raw = raw.get("value") if isinstance(raw, dict) else raw
            cap = self.capability(result.operation)
            actual = normalized_value(raw, cap.parameters["value"]) if cap else None
            label = self.choices[result.operation].get(actual) if isinstance(actual, str) else None
            self.message(f"已应用：{label.text()}" if label else "设置已受理，读回待核实")
        else:
            self.message("应用未完成：" + (result.error or result.outcome))
        self.controller.runtime.monitoring.refresh_settings(self.device_id)
        self.refresh()

    def failed(self, error: Exception) -> None:
        if shiboken6.isValid(self):
            self.busy = False
            self.message("应用失败：" + str(error))
            self.refresh()

    def reject(self) -> None:
        if not self.busy:
            self.drafts.clear()
            super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.busy:
            event.ignore()
        else:
            self.drafts.clear()
            super().closeEvent(event)
