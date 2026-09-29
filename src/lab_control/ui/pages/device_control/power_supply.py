"""All-channel power controls. Device I/O stays in the shared session scheduler."""

from __future__ import annotations

import math
from typing import cast

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.settings_sync import SettingsSync
from lab_control.domain.instruments import DeviceCommand, Quality
from lab_control.domain.readback import boolean_value
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.signal_source import ParameterGroup, newer_token
from lab_control.ui.widgets.device_selector import DeviceSelector


class PowerOutput(QWidget):
    def __init__(
        self, controller, device_id: str, operation: str, channel: int, parent=None, *, allow_cached=False
    ):
        super().__init__(parent)
        self.controller, self.device_id = controller, device_id
        self.operation, self.channel = operation, channel
        self.allow_cached = allow_cached
        self.value: bool | None = None
        self.busy = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.state = QLabel(self)
        self.state.setObjectName("powerOutputState")
        self.state.setWordWrap(True)
        self.button = QPushButton("输出未知", self)
        self.button.setObjectName("powerOutputButton")
        self.button.setProperty("buttonSize", "panel")
        self.button.setFixedWidth(112)
        self.button.clicked.connect(self.toggle)
        row.addWidget(self.state)
        row.addWidget(self.button)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        frame = self.controller.runtime.sessions.readback_frames(self.device_id).get(
            (self.channel, self.operation)
        )
        health, reason = self.controller.runtime.monitoring.settings_status(
            self.device_id, ((self.channel, self.operation),)
        )
        self.value = (
            boolean_value(frame.reading.value)
            if frame
            and SettingsSync.current(frame, state)
            and frame.reading.quality in {Quality.GOOD, Quality.SIMULATED}
            else None
        )
        label = "整机输出" if self.operation == "all_outputs" else "输出"
        actual = "开启" if self.value is True else "关闭" if self.value is False else "未知"
        # Channel headers already show synchronization health. Repeating it
        # beside both footer buttons expands cards while staggered reads arrive.
        suffix = f" · {health}" if self.operation == "all_outputs" and health != "正常" else ""
        self.state.setText(f"{label} · {actual}" + suffix)
        self.state.setToolTip(f"{health} · {reason}")
        self.button.setText(
            "关闭输出" if self.value is True else "开启输出" if self.value is False else "输出未知"
        )
        cap = (
            next(
                (
                    c
                    for c in self.controller.capabilities(self.device_id, self.channel).operations
                    if c.name == self.operation
                ),
                None,
            )
            if self.device_id in self.controller.records
            else None
        )
        self.button.setEnabled(
            bool(
                cap
                and cap.available
                and cap.writable
                and (health == "正常" or (self.allow_cached and health == "同步已暂停"))
                and self.value is not None
                and not self.busy
                and not state.get("controlPending")
                and not state.get("controlsLocked")
                and not state.get("capabilityStale", True)
                and self.device_id not in self.controller.group_devices
                and state.get("activeTask") != "continuous_acquisition"
            )
        )

    def toggle(self) -> None:
        if not self.button.isEnabled() or self.value is None:
            return
        target = not self.value
        session = self.controller.states.get(self.device_id, {})
        version = (session.get("sessionId"), session.get("configurationVersion"))
        scope = "整机全部输出" if self.operation == "all_outputs" else f"CH {self.channel} 输出"
        record = self.controller.records[self.device_id]
        if (
            QMessageBox.question(
                self,
                "确认输出操作",
                f"{record['identity'].get('model')} · {scope}\n{'开启' if target else '关闭'}输出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        # The modal dialog may outlive a disconnect, reconnect or parameter write.
        self.refresh()
        session = self.controller.states.get(self.device_id, {})
        if (
            not self.button.isEnabled()
            or self.value == target
            or version != (session.get("sessionId"), session.get("configurationVersion"))
        ):
            return
        self.busy = True
        self.refresh()
        try:
            self.submit(DeviceCommand(self.operation, {"value": target}, self.channel, True))
        except Exception as exc:
            self.failed(exc)

    def submit(self, command: DeviceCommand) -> None:
        self.controller.apply(self.device_id, command, self.completed, self.failed)

    def completed(self, result) -> None:
        if not shiboken6.isValid(self):
            return
        self.busy = False
        if result.outcome != "success":
            self.controller.message.emit(result.error or result.outcome)
        self.controller.runtime.monitoring.refresh_settings(self.device_id)
        self.refresh()

    def failed(self, error: Exception) -> None:
        if shiboken6.isValid(self):
            self.busy = False
            self.controller.message.emit(str(error))
            self.refresh()


class PowerChannel(ParameterGroup):
    def __init__(self, controller, device_id: str, channel: int, *, channel_output: bool, parent=None):
        schema = controller.capabilities(device_id, channel)
        caps = [c for name in ("voltage", "current") for c in schema.operations if c.name == name]
        super().__init__(controller, device_id, channel, caps, columns=2, parent=parent)
        self.setObjectName("powerChannel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = cast(QVBoxLayout, self.layout())
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        self.fields.setHorizontalSpacing(16)
        header = QHBoxLayout()
        title = QLabel(f"CH {channel}", self)
        title.setObjectName("powerChannelTitle")
        header.addWidget(title)
        header.addStretch()
        self.footer_layout.removeWidget(self.count)
        self.count.setObjectName("powerSyncState")
        header.addWidget(self.count)
        layout.insertLayout(0, header)
        self.measurements: dict[str, QLabel] = {}
        for name, editor in self.editors.items():
            field_title = editor.findChild(QLabel, "controlTitle")
            if field_title:
                field_title.setText("电压设置" if name == "voltage" else "电流限值")
            field = editor.editors["value"]
            field.input.setObjectName("powerValue")
            field.input.setMinimumWidth(0)
            if isinstance(field.input, QLineEdit):
                field.input.setPlaceholderText("同步中")
            field.unit.setMinimumWidth(0)
            if fit_contents := getattr(field.unit, "fit_contents", None):
                fit_contents(62)
            field.unit.setFixedWidth(62)
            measurement = QLabel(self)
            measurement.setObjectName("powerMeasured")
            measurement.setWordWrap(True)
            cast(QVBoxLayout, field.layout()).insertWidget(1, measurement)
            self.measurements[name] = measurement
            field.changed.connect(self.refresh)
        self.output = PowerOutput(controller, device_id, "output", channel, self) if channel_output else None
        if self.output:
            self.footer_layout.addWidget(self.output, 0, 0)
        self.apply_button.setProperty("buttonSize", "panel")
        self.reflow_fields()
        self.refresh()

    def reflow_fields(self) -> None:
        # Channel cards and their two basic fields never collapse into a single column.
        self.fields.setColumnStretch(0, 1)
        self.fields.setColumnStretch(1, 1)
        self.visible_columns = 2

    def resizeEvent(self, event) -> None:  # noqa: N802
        QWidget.resizeEvent(self, event)
        self.update_row_heights()

    def update_row_heights(self) -> None:
        # A wrapped range adds height inside the nested field, before the outer grid can cache it.
        for editor in self.editors.values():
            for field in editor.editors.values():
                height = field.heightForWidth(field.width())
                if height >= 0 and field.minimumHeight() != height:
                    field.setMinimumHeight(height)
                    if layout := editor.layout():
                        layout.invalidate()
        super().update_row_heights()

    def sync_keys(self) -> tuple[tuple[int, str], ...]:
        schema = self.controller.capabilities(self.device_id, self.channel)
        return tuple(
            (self.channel if c.channel_scoped else 1, c.name)
            for c in schema.operations
            if c.name
            in {"voltage", "current", "measured_voltage", "measured_current", "output", "all_outputs"}
            and c.available
            and c.readable
        )

    def refresh(self) -> None:
        super().refresh()
        if not hasattr(self, "measurements"):
            return
        state = self.controller.states.get(self.device_id, {})
        frames = self.controller.runtime.sessions.readback_frames(self.device_id)
        for name, label in self.measurements.items():
            operation = "measured_" + name
            frame = frames.get((self.channel, operation))
            health, _ = self.controller.runtime.monitoring.settings_status(
                self.device_id, ((self.channel, operation),)
            )
            value = frame.reading.value if frame else None
            if isinstance(value, dict):
                value = value.get("value")
            valid = (
                frame is not None
                and SettingsSync.current(frame, state)
                and frame.reading.quality in {Quality.GOOD, Quality.SIMULATED}
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
            field = self.editors[name].editors["value"]
            # Keep the source on its own line instead of wrapping its last character on small screens.
            bounds, separator, source = field.range_label.toolTip().rpartition(" · ")
            range_text = f"{bounds}\n来源：{source}" if separator else field.range_label.toolTip()
            if field.range_label.text() != range_text:
                field.range_label.setText(range_text)
            display = (
                f"{cast(float, value) / (field.unit.currentData() or 1):.6g} {field.unit.currentText()}"
                if valid
                else "—"
            )
            text = ("实测电压：" if name == "voltage" else "实测电流：") + display
            if health != "正常":
                text += " · " + health
            if text != label.text():
                label.setText(text)
            label.setToolTip(
                f"{frame.reading.timestamp}\n原始读回：{frame.reading.value}" if frame else "等待实际测量值"
            )


class PowerSupplyPage(QWidget):
    def __init__(self, controller: WorkspaceController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.device_id = ""
        self.mode = controller.mode
        self.panels: dict[int, PowerChannel] = {}
        self.output: PowerOutput | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        self.header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "power_supply", self, compact=True)
        self.header.addWidget(self.selector, 1)
        self.connection = QLabel(self)
        self.connection.setObjectName("powerSyncState")
        self.header.addWidget(self.connection)
        layout.addLayout(self.header)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        layout.addWidget(self.scroll_area, 1)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 3, 0)
        self.grid.setSpacing(12)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setWidget(self.body)
        self.empty = QLabel("请先连接电源，再选择设备。", self)
        self.empty.setObjectName("mutedText")
        self.grid.addWidget(self.empty, 0, 0, 1, 2)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.settings_changed.connect(self.update_readings)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if (device_id, self.controller.mode) == (self.device_id, self.mode):
            return
        if self.device_id in self.controller.group_devices:
            self.selector.select(self.device_id)
            return
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
        self.device_id, self.mode = device_id, self.controller.mode
        for panel in self.panels.values():
            self.grid.removeWidget(panel)
            panel.hide()
            panel.deleteLater()
        self.panels.clear()
        if self.output:
            self.header.removeWidget(self.output)
            self.output.hide()
            self.output.deleteLater()
            self.output = None
        self.empty.setVisible(not device_id)
        if device_id:
            schema = self.controller.capabilities(device_id)
            master = any(c.name == "all_outputs" and c.available for c in schema.operations)
            if master:
                self.output = PowerOutput(self.controller, device_id, "all_outputs", 1, self)
                self.header.addWidget(self.output)
            for index, channel in enumerate(schema.channels):
                channel_output = not master and any(
                    c.name == "output" and c.available for c in schema.operations
                )
                panel = PowerChannel(
                    self.controller, device_id, channel, channel_output=channel_output, parent=self.body
                )
                self.panels[channel] = panel
                self.grid.addWidget(panel, index // 2, index % 2, Qt.AlignmentFlag.AlignTop)
        self.refresh()
        if self.isVisible():
            self.controller.focus_settings(device_id)
            self.update_readings(device_id)

    def update_readings(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible():
            return
        state = self.controller.states.get(device_id, {})
        if state.get("controlPending") or state.get("controlGroup"):
            return
        frames = self.controller.runtime.sessions.readback_frames(device_id)
        for panel in self.panels.values():
            for name, editor in panel.editors.items():
                frame = frames.get((panel.channel, name))
                if not frame or not SettingsSync.current(frame, state):
                    editor._reading_valid = False
                    continue
                token = (frame.session_id, frame.configuration_version, frame.sequence)
                if newer_token(token, editor.sync_token):
                    old = editor.last_reading
                    unchanged = (
                        editor._reading_valid
                        and old is not None
                        and editor.sync_token is not None
                        and token[:2] == editor.sync_token[:2]
                        and (old.value, old.quality, old.error)
                        == (frame.reading.value, frame.reading.quality, frame.reading.error)
                    )
                    editor.sync_token = token
                    if unchanged:
                        editor.last_reading = frame.reading
                    else:
                        editor.show_reading(frame.reading)
            panel.refresh()

    def refresh(self) -> None:
        if not self.isVisible():
            return
        if self.mode != self.controller.mode:
            self.select_device(self.selector.device_id)
        state = self.controller.states.get(self.device_id, {})
        connected = state.get("connectionState") in {"connected", "busy"}
        self.connection.setText("已连接" if connected else "未连接")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.controller.focus_settings(self.device_id)
        self.update_readings(self.device_id)
        self.refresh()

    def hideEvent(self, event) -> None:  # noqa: N802
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
        super().hideEvent(event)
