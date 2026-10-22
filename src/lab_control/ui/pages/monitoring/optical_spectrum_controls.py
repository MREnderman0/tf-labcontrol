"""Four-setting optical spectrometer draft editor."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.domain.spectrum import OPTICAL_SETTINGS, prepare_optical_changes
from lab_control.ui.pages.device_control.form import OperationEditor


class OpticalSpectrumControls(QFrame):
    """Keep instrument settings separate from the software sampling interval."""

    busy_changed = Signal(bool)

    def __init__(self, controller, device_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("opticalSpectrumControls")
        self.controller, self.device_id, self.mode = controller, device_id, "general"
        self.editors: dict[str, OperationEditor] = {}
        self.running = False
        self.refresh_only = False
        self.pending: list[DeviceCommand] = []
        self.completed: list[str] = []
        self.read_pending: list[str] = []
        self.session_id = ""
        self.verified: dict[str, Any] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        self.health = QLabel("同步中", self)
        self.health.setObjectName("spectrumHealth")
        heading.addWidget(self.health, 1)
        self.refresh_button = QPushButton("刷新参数", self)
        self.refresh_button.setToolTip("读取仪器当前四项光学参数，保留未应用草稿。")
        self.refresh_button.clicked.connect(self.refresh_parameters)
        heading.addWidget(self.refresh_button)
        layout.addLayout(heading)
        self.scroller = QScrollArea(self)
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(self.scroller)
        grid = QGridLayout(body)
        grid.setContentsMargins(2, 0, 6, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(12)
        caps = {cap.name: cap for cap in controller.capabilities(device_id).operations}
        labels = {
            "start_wavelength": "起始波长",
            "stop_wavelength": "结束波长",
            "rbw": "分辨率 RBW",
            "sensitivity": "灵敏度",
        }
        for index, name in enumerate(OPTICAL_SETTINGS):
            cap = caps.get(name)
            if cap is None:
                continue
            op = OperationEditor(
                controller, device_id, cap, 1, body, compact=True, actions=False, workspace_mode=self.mode
            )
            self.editors[name] = op
            title = op.findChild(QLabel, "controlTitle")
            if title:
                title.setText(labels[name])
                title.setMinimumHeight(22)
            field = op.editors.get("value")
            if field:
                field.input.setMinimumWidth(0)
                field.unit.setMinimumWidth(0)
                # The wavelength range/source hint is intentionally allowed to
                # wrap in the narrow right column. Reserve its two-line height
                # so a wrapped label never paints over the current-value line.
                field.range_label.setMinimumHeight(30)
            op.enable_auto_sync()
            if field:
                field.changed.connect(self.refresh_buttons)
            grid.addWidget(op, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(2, 1)
        self.scroller.setWidget(body)
        layout.addWidget(self.scroller, 1)
        self.status = QLabel(self)
        self.status.setObjectName("inputFeedback")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("应用修改", self)
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def current(self) -> dict[str, Any]:
        return {
            name: op.current_value["value"]
            for name, op in self.editors.items()
            if op._reading_valid and isinstance(op.current_value, dict) and "value" in op.current_value
        }

    def target(self) -> dict[str, Any]:
        return {name: op.editors["value"].value() for name, op in self.editors.items()}

    def _valid_session(self) -> bool:
        state = self.controller.states.get(self.device_id, {})
        return state.get("sessionId") == self.session_id

    def apply(self) -> None:
        try:
            for op in self.editors.values():
                op.editors["value"].finish_editing(force=True)
            current, target = self.current(), self.target()
            if len(current) != len(self.editors):
                raise DeviceError("等待四项有效读回后再应用")
            self.pending = prepare_optical_changes(
                current, target, self.controller.capabilities(self.device_id)
            )
            if self.pending:
                self._begin(False)
                self._next_command()
            else:
                self.status.setText("参数未变化")
        except Exception as exc:
            self.status.setText(str(exc))

    def refresh_parameters(self) -> None:
        try:
            self._begin(False)
            self.refresh_only = True
            self.read_pending = list(self.editors)
            self._read_next()
        except Exception as exc:
            self.status.setText(str(exc))

    def _begin(self, _auto: bool) -> None:
        self.controller.ensure_applicable(self.device_id, workspace_mode=self.mode)
        self.controller.begin_group(self.device_id)
        self.session_id = self.controller.states[self.device_id]["sessionId"]
        self.completed, self.verified = [], {}
        self.running = True
        self.refresh_only = False
        self.scroller.setEnabled(False)
        self.busy_changed.emit(True)
        self.refresh()

    def _next_command(self) -> None:
        if not self._valid_session():
            self.finish("会话已改变，停止后续操作；草稿已保留")
            return
        if not self.pending:
            self.read_pending = list(self.editors)
            self._read_next()
            return
        command = self.pending[0]
        try:
            op = self.editors[command.operation]
            if op.parameters() != command.parameters:
                self.finish("范围已变化，请核对修正后的草稿，再次应用")
                return
            self.controller.apply(
                self.device_id,
                command,
                self._applied,
                lambda exc: self.finish(str(exc), failed=command.operation),
                group_member=True,
                workspace_mode=self.mode,
            )
        except Exception as exc:
            self.finish(str(exc), failed=command.operation)

    def _applied(self, result) -> None:
        if not self._valid_session():
            self.finish("会话已改变，已丢弃旧结果")
        elif result.outcome != "success":
            self.finish(result.error or result.outcome, failed=self.pending[0].operation)
        else:
            self.completed.append(self.pending.pop(0).operation)
            self._next_command()

    def _read_next(self) -> None:
        if not self._valid_session():
            self.finish("会话已改变，已丢弃旧结果")
            return
        if not self.read_pending:
            self.finish("参数已刷新 · 草稿已保留" if self.refresh_only else "修改已应用 · 已核对读回")
            return
        names, self.read_pending = self.read_pending[:4], self.read_pending[4:]
        self.controller.watch(
            self.controller.runtime.sessions.submit(self.device_id, "read_settings", (tuple(names), 1)),
            self._read_done,
            lambda exc: self.finish(
                ("参数读取失败：" if self.refresh_only else "设置已执行，读回未确认：") + str(exc)
            ),
        )

    def _read_done(self, readings) -> None:
        from lab_control.domain.readback import usable_reading

        for reading in readings:
            op = self.editors[reading.operation]
            if not usable_reading(reading, op.cap.parameters):
                self.finish("读回未确认；草稿已保留")
                return
            self.verified[reading.operation] = reading
        self._read_next()

    def finish(self, text: str, *, failed: str = "") -> None:
        self.running = False
        self.scroller.setEnabled(True)
        self.controller.end_group(self.device_id)
        self.busy_changed.emit(False)
        if failed:
            done = "、".join(self.completed) or "无"
            rest = (
                "、".join(command.operation for command in self.pending if command.operation != failed)
                or "无"
            )
            text += f" · 已完成：{done} · 失败：{failed} · 未执行：{rest}"
        self.status.setText(text)
        self.refresh()

    def refresh_buttons(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        locked = bool(state.get("controlsLocked"))
        available = (
            state.get("connectionState") in {"connected", "busy"}
            and not self.running
            and not locked
            and not state.get("capabilityStale", True)
            and self.device_id not in self.controller.group_devices
        )
        self.apply_button.setEnabled(
            available
            and len(self.current()) == len(self.editors)
            and any(op.dirty for op in self.editors.values())
        )
        self.refresh_button.setEnabled(available)
        self.busy_changed.emit(self.running)

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        if not self.running:
            frames = self.controller.runtime.sessions.readback_frames(self.device_id)
            for name, op in self.editors.items():
                frame = frames.get((1, name))
                if frame is None or (frame.session_id, frame.configuration_version) != (
                    state.get("sessionId"),
                    state.get("configurationVersion"),
                ):
                    continue
                token = (frame.session_id, frame.configuration_version, frame.sequence)
                if op.sync_token != token:
                    op.sync_token = token
                    op.show_reading(frame.reading)
        health, reason = self.controller.runtime.monitoring.settings_status(
            self.device_id, tuple((1, name) for name in self.editors)
        )
        self.health.setText("采集中 · 参数已锁定" if state.get("controlsLocked") else health)
        self.health.setToolTip(reason)
        self.status.setText("正在应用…" if self.running and not self.refresh_only else self.status.text())
        self.refresh_buttons()
