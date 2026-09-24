"""Compact spectrum draft editor and one serialized apply transaction."""

from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.domain.spectrum import (
    AUTOMATIC,
    FREQUENCY,
    NUMERIC,
    SETTINGS,
    frequency_draft,
    prepare_changes,
)
from lab_control.ui.pages.device_control.form import OperationEditor
from lab_control.ui.pages.monitoring.spectrum_markers import frequency_text


class SpectrumControls(QFrame):
    busy_changed = Signal(bool)

    def __init__(self, controller, device_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumControls")
        self.controller, self.device_id = controller, device_id
        self.mode = controller.mode
        self.editors: dict[str, OperationEditor] = {}
        self.running = False
        self.link_busy = False
        self.last_frequency = ""
        self.pending: list[DeviceCommand] = []
        self.completed: list[str] = []
        self.read_pending: list[str] = []
        self.auto_running = False
        self.session_id = ""
        self.verified: dict[str, Any] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.health = QLabel("同步中", self)
        self.health.setObjectName("spectrumHealth")
        layout.addWidget(self.health)
        self.scroller = QScrollArea(self)
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(self.scroller)
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(2, 0, 6, 8)
        self.body_layout.setSpacing(10)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(12)
        self.body_layout.addLayout(grid)
        caps = {c.name: c for c in controller.capabilities(device_id).operations}
        for name in SETTINGS:
            if name not in caps:
                continue
            op = OperationEditor(controller, device_id, caps[name], 1, self, compact=True, actions=False)
            op.enable_auto_sync()
            self.editors[name] = op
            field = op.editors["value"]
            field.unit.setMinimumWidth(0)
            fit = getattr(field.unit, "fit_contents", None)
            if callable(fit):
                fit(52)
            else:
                field.unit.setFixedWidth(64)
            field.input.setMinimumWidth(0)
            field.changed.connect(self.changed)
            if name in NUMERIC:
                index = NUMERIC.index(name)
                grid.addWidget(op, index // 2, index % 2, Qt.AlignmentFlag.AlignTop)
                title = op.findChild(QLabel, "controlTitle")
                if title:
                    title.setMinimumHeight(22)
                if title and name in {"rbw", "vbw", "span"}:
                    title.setText({"rbw": "RBW", "vbw": "VBW", "span": "扫宽"}[name])
            else:
                op.hide()
            if name in FREQUENCY:
                if isinstance(field.input, QLineEdit):
                    field.input.textEdited.connect(lambda _text, n=name: self.frequency_started(n))
                    field.input.editingFinished.connect(
                        lambda n=name: QTimer.singleShot(0, lambda: self.link(n))
                    )
                field.unit.currentIndexChanged.connect(
                    lambda _i, n=name: QTimer.singleShot(0, lambda: self.link(n))
                )
        for name, automatic in AUTOMATIC.items():
            if name in self.editors and automatic in self.editors:
                check = cast(QCheckBox, self.editors[automatic].editors["value"].input)
                check.setText("自动")
                item = cast(QVBoxLayout, self.editors[name].layout()).itemAt(0)
                assert item is not None
                heading = cast(QHBoxLayout, item.layout())
                heading.addWidget(check)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.marker_slot = QVBoxLayout()
        self.body_layout.addLayout(self.marker_slot)
        self.body_layout.addStretch()
        self.scroller.setWidget(body)
        layout.addWidget(self.scroller, 1)
        self.status = QLabel(self)
        self.status.setObjectName("inputFeedback")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("应用修改", self)
        self.apply_button.setObjectName("primaryButton")
        self.auto_button = QPushButton("自动设置", self)
        for button in (self.apply_button, self.auto_button):
            button.setFixedHeight(36)
            buttons.addWidget(button, 1)
        layout.addLayout(buttons)
        self.apply_button.clicked.connect(self.apply)
        self.auto_button.clicked.connect(self.auto_setup)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def frequency_started(self, name: str) -> None:
        self.last_frequency = name

    def changed(self) -> None:
        if not self.link_busy and not self.running:
            self.refresh_buttons()

    def target(self) -> dict[str, Any]:
        return {name: op.editors["value"].value() for name, op in self.editors.items()}

    def current(self) -> dict[str, Any]:
        return {
            name: op.current_value["value"]
            for name, op in self.editors.items()
            if op._reading_valid and op.current_value is not None
        }

    def link(self, changed: str) -> None:
        if self.link_busy or self.running or not all(k in self.editors for k in FREQUENCY):
            return
        op = self.editors[changed]
        field = op.editors["value"]
        if field.unit.view().isVisible() or not op.dirty:
            return
        if any(self.editors[k].editing() for k in FREQUENCY if k != changed):
            return
        self.link_busy = True
        try:
            field.finish_editing(force=True)
            values = {name: self.editors[name].editors["value"].value() for name in FREQUENCY}
            start = self.editors["start_frequency"].editors["value"].spec
            stop = self.editors["stop_frequency"].editors["value"].spec
            if start.minimum is None or stop.maximum is None:
                return
            linked = frequency_draft(values, changed, start.minimum, stop.maximum)
            adjusted = linked[changed] != values[changed]
            for name, value in linked.items():
                editor = self.editors[name]
                number = editor.editors["value"]
                factor = number.unit.currentData() or 1
                number.restore({"value": f"{value / factor:.12g}", "factor": factor})
                editor.save_draft()
            if adjusted:
                field.feedback.setText("已按有效频率范围调整")
        except DeviceError:
            pass  # Empty first-sync fields are not drafts and must never become zero.
        finally:
            self.link_busy = False
        self.refresh_buttons()

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        frames = self.controller.runtime.sessions.readback_frames(self.device_id)
        if not self.running:
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
        for name, automatic in AUTOMATIC.items():
            if name in self.editors and automatic in self.editors:
                auto_field = cast(QCheckBox, self.editors[automatic].editors["value"].input)
                enabled = not auto_field.isChecked() and self.editors[name].cap.writable
                self.editors[name].editors["value"].setEnabled(enabled and not self.running)
                if auto_field.isChecked():
                    op = self.editors[name]
                    if op._reading_valid:
                        value = op.current_value["value"]
                        unit_field = op.editors["value"]
                        op.readback.setText(
                            f"自动采用：{value / (unit_field.unit.currentData() or 1):.8g} {unit_field.unit.currentText()}"
                        )
                        op.readback.show()
        for op in self.editors.values():
            field = op.editors["value"]
            spec = field.spec
            if spec.kind not in {"number", "integer"}:
                continue
            unit = field.unit.currentText()
            factor = field.unit.currentData() or 1

            def bound(value):
                if value is None:
                    return "未确认"
                return frequency_text(value) if spec.unit == "Hz" else f"{value / factor:g}"

            source = {
                "model": "型号范围",
                "instrument": "实机范围",
                "experiment": "实验限制",
                "unavailable": "未确认 / 已过期",
            }.get(spec.range_source, spec.range_source)
            text = f"可输入范围：{bound(spec.minimum)} ～ {bound(spec.maximum)}"
            text += ("" if spec.unit == "Hz" else " " + unit) + "\n" + source
            if field.range_label.text() != text:
                field.range_label.setText(text)
        health, reason = self.controller.runtime.monitoring.settings_status(
            self.device_id, tuple((1, k) for k in self.editors)
        )
        self.health.setText(
            "正在自动设置…"
            if self.running and self.auto_running
            else "正在应用…"
            if self.running
            else "待应用"
            if self.dirty()
            else health
        )
        self.health.setToolTip(reason)
        self.refresh_buttons()

    def dirty(self) -> bool:
        return any(
            op.dirty
            for name, op in self.editors.items()
            if name not in AUTOMATIC
            or not cast(QCheckBox, self.editors[AUTOMATIC[name]].editors["value"].input).isChecked()
        )

    def refresh_buttons(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        available = (
            state.get("connectionState") in {"connected", "busy"}
            and not self.running
            and not state.get("capabilityStale", True)
            and self.mode == self.controller.mode
            and self.device_id not in self.controller.group_devices
        )
        self.apply_button.setEnabled(available and self.dirty() and len(self.current()) == len(self.editors))
        caps = self.controller.capabilities(self.device_id)
        auto = next((c for c in caps.operations if c.name == "auto_setup"), None)
        self.auto_button.setEnabled(bool(available and auto and auto.available and auto.writable))
        self.auto_button.setToolTip(
            "执行仪器 Auto Tune" if auto and auto.available else "当前型号未核实整机 Auto 接口"
        )

    def apply(self) -> None:
        try:
            for op in self.editors.values():
                op.editors["value"].finish_editing(force=True)
            if self.last_frequency:
                self.link(self.last_frequency)
            target = self.target()
            current = self.current()
            if len(current) != len(self.editors):
                raise DeviceError("等待有效读回后再应用")
            # Automatic numeric displays are not manual requests, including saved manual drafts.
            for name, auto in AUTOMATIC.items():
                if target.get(auto):
                    target[name] = current[name]
            self.pending = prepare_changes(current, target, self.controller.capabilities(self.device_id))
            if self.pending:
                self.begin(False)
                self.next_command()
        except Exception as exc:
            self.status.setText(str(exc))

    def auto_setup(self) -> None:
        if self.running:
            return
        if (
            self.dirty()
            and QMessageBox.question(
                self,
                "替换未应用修改",
                "自动设置成功后，将用仪器实际结果替换当前未应用修改。继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.pending = [DeviceCommand("auto_setup")]
        self.begin(True)
        self.next_command()

    def begin(self, auto: bool) -> None:
        self.controller.ensure_applicable(self.device_id)
        self.session_id = self.controller.states[self.device_id]["sessionId"]
        self.completed, self.verified = [], {}
        self.auto_running, self.running = auto, True
        self.controller.begin_group(self.device_id)
        self.scroller.setEnabled(False)
        self.busy_changed.emit(True)
        self.refresh()

    def valid_session(self) -> bool:
        state: dict[str, Any] = next(
            (
                s
                for s in self.controller.runtime.sessions.snapshots()
                if s["stableDeviceId"] == self.device_id
            ),
            {},
        )
        return state.get("sessionId") == self.session_id

    def next_command(self) -> None:
        if not self.valid_session():
            self.finish("会话已改变，停止后续操作；草稿已保留")
            return
        if not self.pending:
            self.read_pending = list(self.editors)
            self.read_next()
            return
        command = self.pending[0]
        try:
            if command.operation != "auto_setup":
                op = self.editors[command.operation]
                op.refresh_enabled()
                actual = op.parameters()
                if actual != command.parameters:
                    self.finish("范围已变化，请核对修正后的草稿，再次应用")
                    return
            self.controller.apply(
                self.device_id,
                command,
                self.applied,
                lambda exc: self.finish(str(exc), failed=command.operation),
                group_member=True,
            )
        except Exception as exc:
            self.finish(str(exc), failed=command.operation)

    def applied(self, result) -> None:
        if not self.valid_session():
            self.finish("会话已改变，已丢弃旧结果")
        elif result.outcome != "success":
            self.finish(result.error or result.outcome, failed=self.pending[0].operation)
        else:
            self.completed.append(self.pending.pop(0).operation)
            self.next_command()

    def read_next(self) -> None:
        if not self.valid_session():
            self.finish("会话已改变，已丢弃旧结果")
            return
        if not self.read_pending:
            if self.auto_running:
                for op in self.editors.values():
                    op.dirty = False
                    self.controller.runtime.repository.save_workspace(op.key, None)
            self.finish("自动设置完成 · 已核对读回" if self.auto_running else "修改已应用 · 已核对读回")
            return
        names, self.read_pending = self.read_pending[:4], self.read_pending[4:]
        self.controller.watch(
            self.controller.runtime.sessions.submit(self.device_id, "read_settings", (tuple(names), 1)),
            self.read_done,
            lambda exc: self.finish("设置已执行，读回未确认：" + str(exc)),
        )

    def read_done(self, readings) -> None:
        from lab_control.domain.readback import usable_reading

        for reading in readings:
            op = self.editors[reading.operation]
            if not usable_reading(reading, op.cap.parameters):
                self.finish("设置已执行，读回未确认；草稿已保留")
                return
            self.verified[reading.operation] = reading
        self.read_next()

    def finish(self, text: str, *, failed: str = "") -> None:
        self.running = False
        self.scroller.setEnabled(True)
        self.controller.end_group(self.device_id)
        self.busy_changed.emit(False)
        if failed:
            done = "、".join(self.completed) or "无"
            remaining = "、".join(c.operation for c in self.pending if c.operation != failed) or "无"
            text += f" · 已完成：{done} · 失败：{failed} · 未执行：{remaining}"
        self.status.setText(text)
        self.refresh()
