from __future__ import annotations

import math
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import mode_applicable
from lab_control.application.controls import COMMON, OPERATION_LABELS, PARAMETER_LABELS
from lab_control.domain.instruments import Capability, DeviceCommand, ParameterReading
from lab_control.domain.readback import normalized_value, usable_reading
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.editors import ArrayEditor, FeedbackLabel, ParameterEditor

DATA_OPERATIONS = {"block", "streaming", "phase_noise", "am_noise", "stability", "data_stream", "power"}
SOURCE_LABELS = {
    "model": "型号固定值",
    "instrument": "仪器读回",
    "session_cache": "本会话设置记录",
    "simulated": "模拟读回",
    "unavailable": "未知 / 无读取接口",
}


def concise(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 8:
        return f"{len(value)} 项：{value[:3]} …"
    if isinstance(value, dict):
        return "；".join(f"{PARAMETER_LABELS.get(k, k)}: {concise(v)}" for k, v in value.items())
    if isinstance(value, float):
        return f"{value:.9g}"
    return "未知" if value is None else str(value)


class OperationEditor(QFrame):
    def __init__(
        self,
        controller: WorkspaceController,
        device_id: str,
        cap: Capability,
        channel: int,
        parent=None,
        *,
        compact: bool = False,
        actions: bool = True,
        simplified: bool = False,
    ) -> None:
        super().__init__(parent)
        self.compact = compact
        self.show_actions = actions
        self.setObjectName("compactOperation" if compact else "operationCard")
        self.controller, self.device_id, self.cap = controller, device_id, cap
        self.channel = channel if cap.channel_scoped else 1
        self.mode = controller.mode
        self.dirty = False
        self.busy = False
        self.current_value: Any = None
        self.auto_sync = False
        self.last_reading: ParameterReading | None = None
        self._reading_valid = False
        self._edit_revision = 0
        self._submitted_revision = 0
        self._apply_error = False
        self.sync_token: tuple[str, int, int] | None = None
        self.data_only = not cap.writable and cap.name in DATA_OPERATIONS
        self.editors: dict[str, ParameterEditor] = {}
        self.array_editor: ArrayEditor | None = None
        self.key = f"draft/{self.mode}/{device_id}/{self.channel}/{cap.name}"
        layout = QVBoxLayout(self)
        if compact:
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(3)
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading = QHBoxLayout()
        title = QLabel(OPERATION_LABELS.get(cap.name, cap.name) + (" · 只读" if not cap.writable else ""))
        title.setObjectName("controlTitle")
        heading.addWidget(title)
        heading.addStretch()
        if not compact:
            heading.addWidget(QLabel(f"CH {self.channel}" if cap.channel_scoped else "整机"))
        layout.addLayout(heading)
        form = QFormLayout()
        for name, parameter in cap.parameters.items():
            if parameter.kind != "array":
                label = (
                    OPERATION_LABELS.get(cap.name, cap.name)
                    if name == "value" and len(cap.parameters) == 1
                    else PARAMETER_LABELS.get(name, name)
                )
                editor = ParameterEditor(parameter, self, label=label, simplified=simplified)
                self.editors[name] = editor
                editor.changed.connect(self.save_draft)
                if compact and name == "value":
                    layout.addWidget(editor)
                    if isinstance(editor.input, QComboBox):
                        labels = {
                            "SIN": "正弦波",
                            "SINE": "正弦波",
                            "SQU": "方波",
                            "SQUARE": "方波",
                            "PULS": "脉冲",
                            "PULSE": "脉冲",
                            "RAMP": "斜坡",
                            "NOIS": "噪声",
                            "NOISE": "噪声",
                            "DC": "直流",
                            "INT": "内部参考",
                            "EXT": "外部参考",
                        }
                        if cap.name in {"waveform", "reference_clock"}:
                            for index in range(editor.input.count()):
                                value = editor.input.itemData(index)
                                editor.input.setItemText(index, labels.get(value, value))
                    if parameter.kind in {"number", "integer"}:
                        editor.input.setObjectName("sourceValue")
                else:
                    form.addRow(PARAMETER_LABELS.get(name, name), editor)
        if compact:
            form.setContentsMargins(0, 0, 0, 0)
        if form.rowCount():
            layout.addLayout(form)
        arrays = {k: v for k, v in cap.parameters.items() if v.kind == "array"}
        if arrays:
            self.array_editor = ArrayEditor(arrays)
            layout.addWidget(self.array_editor)
        self.readback = QLabel("当前值：尚未读取", self)
        if self.data_only:
            self.readback.setText("在上方选择对应数据项目，使用“单次读取”或“开始监测”获取数据。")
        self.readback.setWordWrap(True)
        self.readback.setObjectName("readback")
        self.readback.setVisible(not compact or controller.runtime.hardware)
        layout.addWidget(self.readback)
        self.range_note = QLabel(self)
        self.range_note.setObjectName("rangeHint")
        self.range_note.setWordWrap(True)
        layout.addWidget(self.range_note)
        actions_row = QHBoxLayout()
        self.status = FeedbackLabel(self)
        self.status.setText("" if cap.available else cap.reason)
        self.status.setWordWrap(True)
        self.status.setObjectName("operationStatus")
        actions_row.addWidget(self.status, 1)
        self.read_button = QPushButton("读取", self)
        self.read_button.setVisible(not self.data_only)
        self.read_button.clicked.connect(self.read)
        self.apply_button = QPushButton("应用" if cap.parameters else "执行", self)
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply)
        self.apply_button.setVisible(cap.writable)
        if compact:
            self.read_button.setVisible(actions and cap.readable)
            self.apply_button.setVisible(actions and cap.writable)
        actions_row.addWidget(self.read_button)
        actions_row.addWidget(self.apply_button)
        layout.addLayout(actions_row)
        draft = controller.runtime.repository.load_workspace(self.key)
        if draft:
            for name, value in draft.get("fields", {}).items():
                if name in self.editors:
                    self.editors[name].restore(value)
            if self.array_editor and draft.get("rows"):
                self.array_editor.path = draft.get("array_path", "")
                self.array_editor.model.replace_rows(draft["rows"])
            self.dirty = True
            self.status.setText("已恢复未提交草稿")
        if self.array_editor:
            self.array_editor.changed.connect(self.save_draft)
        if draft:
            for editor in self.editors.values():
                editor.finish_editing(force=True)
            if self.array_editor:
                self.array_editor.finish_manual_rows()
        controller.refreshed.connect(self.refresh_enabled)
        self.refresh_enabled()

    def save_draft(self) -> None:
        self._edit_revision += 1
        if any(not editor.input_is_valid() for editor in self.editors.values()):
            return
        self.dirty = not self.matches_current()
        self.status.setText("待应用" if self.dirty else "")
        if self.auto_sync:
            self.render_auto_reading()
        if not self.dirty:
            self.controller.runtime.repository.save_workspace(self.key, None)
            return
        self.controller.runtime.repository.save_workspace(
            self.key,
            {
                "fields": {name: editor.draft() for name, editor in self.editors.items()},
                "rows": self.array_editor.model.rows if self.array_editor else [],
                "array_path": self.array_editor.path if self.array_editor else "",
            },
        )

    def matches_current(self) -> bool:
        """Compare in base units without validation/clamping while the user is typing."""
        if self.current_value is None or self.array_editor or not self.editors:
            return False
        values = self.current_value if isinstance(self.current_value, dict) else {"value": self.current_value}
        for name, editor in self.editors.items():
            draft = editor.draft()
            if name not in values:
                return False
            if editor.spec.kind in {"number", "integer"}:
                try:
                    value = float(draft["value"]) * draft["factor"]
                    # Readback is displayed to 12 significant digits; unit-only changes are not writes.
                    if not math.isfinite(value) or not math.isclose(
                        value, float(values[name]), rel_tol=1e-12
                    ):
                        return False
                except (TypeError, ValueError, OverflowError):
                    return False
            elif draft["value"] != values[name]:
                return False
        return True

    def refresh_enabled(self) -> None:
        if self.device_id in self.controller.records:
            schema = self.controller.capabilities(self.device_id, self.channel)
            current = next((cap for cap in schema.operations if cap.name == self.cap.name), self.cap)
            if current != self.cap:
                self.cap = current
                for name, editor in self.editors.items():
                    editor.set_spec(current.parameters[name])
                if self.array_editor:
                    self.array_editor.set_parameters(
                        {name: spec for name, spec in current.parameters.items() if spec.kind == "array"}
                    )
        state = self.controller.states.get(self.device_id, {})
        details = state.get("capabilityDetails", {})
        self.range_note.setText(
            f"RF 输出限幅：{details['rf_output_limit_dbm']:+g} dBm · 功率偏移：{details.get('power_offset_db', 0):g} dB\n"
            "可设范围不代表保证输出功率；此操作不修改 RF 限幅。"
            if self.cap.name == "power" and "rf_output_limit_dbm" in details
            else self.cap.reason
        )
        self.range_note.setVisible(bool(self.range_note.text()))
        available = self.cap.available and state.get("connectionState") in {"connected", "busy"}
        available &= state.get("activeTask") != "continuous_acquisition" and not self.busy
        available &= not state.get("controlsLocked")
        self.read_button.setEnabled(available)
        record = self.controller.records.get(self.device_id, {})
        self.apply_button.setEnabled(
            bool(
                available
                and self.cap.writable
                and record
                and mode_applicable(record, self.controller.mode)
                and self.controller.mode == self.mode
                and not state.get("controlPending")
                and not state.get("capabilityStale", True)
                and self.device_id not in self.controller.group_devices
            )
        )
        self.apply_button.setVisible(self.cap.writable and (not self.compact or self.show_actions))
        for editor in self.editors.values():
            editor.setEnabled(self.cap.available and self.cap.writable and not state.get("controlsLocked"))
        if self.auto_sync:
            if self.sync_token and self.sync_token[:2] != (
                state.get("sessionId"),
                state.get("configurationVersion"),
            ):
                self._reading_valid = False
            self.reconcile_reading()

    def enable_auto_sync(self) -> None:
        self.auto_sync = True
        self.read_button.hide()
        self.readback.hide()

    def editing(self) -> bool:
        return any(
            (isinstance(e.input, QLineEdit) and e.input.hasFocus())
            or (isinstance(e.input, QComboBox) and e.input.view().isVisible())
            or e.unit.hasFocus()
            or e.unit.view().isVisible()
            for e in self.editors.values()
        )

    def reconcile_reading(self) -> None:
        if not self._reading_valid or self.last_reading is None or self.busy:
            return
        if not self.editing():
            if self.dirty and self.matches_current():
                self.dirty = False
                self.controller.runtime.repository.save_workspace(self.key, None)
            if not self.dirty and not self.matches_current():
                values = (
                    self.current_value
                    if isinstance(self.current_value, dict)
                    else {"value": self.current_value}
                )
                for name, value in values.items():
                    field = self.editors.get(name)
                    if field is None:
                        continue
                    if field.spec.kind in {"number", "integer"}:
                        factor = field.unit.currentData() or 1
                        if (
                            isinstance(field.input, QLineEdit)
                            and not field.input.text()
                            and field.spec.unit == "Hz"
                        ):
                            factor = next((f for f in (1e9, 1e6, 1e3) if abs(value) >= f), 1)
                        field.restore({"value": f"{value / factor:.12g}", "factor": factor})
                    else:
                        field.set_value(value)
            if not self._apply_error:
                self.status.setText("待应用" if self.dirty else "")
        self.render_auto_reading()

    def render_auto_reading(self) -> None:
        if self.last_reading is None:
            return
        reading = self.last_reading
        display = concise(reading.value)
        field = self.editors.get("value")
        raw = reading.value.get("value") if isinstance(reading.value, dict) else reading.value
        if field and self._reading_valid:
            value = normalized_value(raw, field.spec)
            if field.spec.kind in {"number", "integer"}:
                display = f"{value / (field.unit.currentData() or 1):.12g} {field.unit.currentText()}"
            elif isinstance(field.input, QComboBox):
                index = field.input.findData(value)
                display = field.input.itemText(index) if index >= 0 else str(raw)
        self.readback.setText(("仪器当前值：" if self._reading_valid else "读回待核实：") + display)
        self.readback.setToolTip(f"原始读回：{concise(reading.value)}\n{reading.timestamp}\n{reading.error}")
        self.readback.setVisible(self.dirty or not self._reading_valid or not self.matches_current())

    def read(self) -> None:
        if self.controller.states.get(self.device_id, {}).get("controlsLocked"):
            return
        try:
            self.busy = True
            self.refresh_enabled()
            self.controller.watch(
                self.controller.runtime.sessions.submit(
                    self.device_id, "read_settings", ((self.cap.name,), self.channel)
                ),
                lambda values: self.show_reading(values[0]),
                self.failed,
            )
        except Exception as exc:
            self.failed(exc)

    def show_reading(self, reading: ParameterReading) -> None:
        if not shiboken6.isValid(self):
            return
        self.busy = False
        if self.auto_sync:
            self.last_reading = reading
            self._reading_valid = usable_reading(reading, self.cap.parameters)
            if self._reading_valid:
                values = reading.value if isinstance(reading.value, dict) else {"value": reading.value}
                self.current_value = {
                    name: normalized_value(values[name], spec) for name, spec in self.cap.parameters.items()
                }
            self.reconcile_reading()
            self.render_auto_reading()
            return
        display = concise(reading.value)
        self.current_value = reading.value
        raw = reading.value.get("value") if isinstance(reading.value, dict) else reading.value
        spec = self.cap.parameters.get("value")
        if self.compact and spec and isinstance(raw, (int, float)) and not isinstance(raw, bool):
            factor, unit = 1.0, spec.unit
            if spec.unit == "Hz":
                factor, unit = next(
                    ((f, u) for f, u in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")) if abs(raw) >= f),
                    (1, "Hz"),
                )
            display = f"{raw / factor:.12g} {unit}"
        self.readback.setText(
            f"{SOURCE_LABELS.get(reading.source, reading.source)}：{display}"
            + ("" if self.compact else "\n" + reading.timestamp)
        )
        self.readback.setToolTip(reading.timestamp)
        self.readback.setVisible(not self.compact or reading.source != "simulated")
        if not self.dirty and reading.value is not None:
            values = reading.value if isinstance(reading.value, dict) else {"value": reading.value}
            for name, value in values.items():
                if name in self.editors:
                    self.editors[name].set_value(value)
                    editor = self.editors[name]
                    if self.compact and isinstance(value, (int, float)) and editor.spec.unit == "Hz":
                        factor = next((scale for scale in (1e9, 1e6, 1e3) if abs(value) >= scale), 1)
                        editor.restore({"value": f"{value / factor:.12g}", "factor": factor})
        self.refresh_enabled()

    def parameters(self) -> dict[str, Any]:
        for editor in self.editors.values():
            editor.finish_editing(force=True)
        values = {name: editor.value() for name, editor in self.editors.items()}
        if self.array_editor:
            values.update(self.array_editor.values())
        return self.cap.validate(values)

    def apply(self) -> None:
        try:
            values = self.parameters()
            confirmed = False
            if self.cap.requires_confirmation:
                record = self.controller.records[self.device_id]
                answer = QMessageBox.question(
                    self,
                    "确认设备操作",
                    f"设备：{record['displayName'] or record['identity'].get('model') or record['adapterId']}\n"
                    f"资源：{record['config']['resource']}\n"
                    f"作用范围：{'CH ' + str(self.channel) if self.cap.channel_scoped else '整机'}\n"
                    f"操作：{OPERATION_LABELS.get(self.cap.name, self.cap.name)}\n目标：{concise(values)}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                confirmed = True
            self.busy = True
            self._submitted_revision = self._edit_revision
            self.status.setText("等待当前读取完成 / 正在应用…")
            self.refresh_enabled()
            self.controller.apply(
                self.device_id,
                DeviceCommand(self.cap.name, values, self.channel, confirmed),
                self.applied,
                self.failed,
            )
        except Exception as exc:
            self.failed(exc)

    def applied(self, result) -> None:
        if not shiboken6.isValid(self):
            return
        self.busy = False
        self._apply_error = result.outcome != "success"
        labels = {
            "success": "已应用",
            "partial": "部分执行",
            "rejected": "设备拒绝 / 读回不符",
            "uncertain": "结果不确定",
        }
        self.status.setText(
            labels.get(result.outcome, result.outcome) + (" · " + result.error if result.error else "")
        )
        if result.readback is not None:
            if result.outcome == "success":
                self.current_value = result.readback
            self.readback.setText(
                "本次操作返回值："
                + concise(result.readback)
                + ("" if self.compact else "\n" + result.timestamp)
            )
            self.readback.setToolTip(result.timestamp)
        elif result.outcome == "success":
            self.readback.setText("本会话设置已受理；设备未提供该操作的即时读回\n" + result.timestamp)
        if result.outcome == "success":
            if not self.auto_sync or self._submitted_revision == self._edit_revision:
                self.dirty = False
                self.controller.runtime.repository.save_workspace(self.key, None)
        if self.auto_sync:
            self._reading_valid = False
            self.last_reading = None
            self.readback.hide()
            self.controller.runtime.monitoring.refresh_settings(self.device_id)
        self.refresh_enabled()

    def failed(self, error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        self.busy = False
        self._apply_error = True
        self.status.setText(str(error))
        self.refresh_enabled()


class ControlForm(QWidget):
    def __init__(
        self,
        controller: WorkspaceController,
        parent=None,
        *,
        capability_filter: Callable[[Capability, Any], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.capability_filter = capability_filter
        self.device_id = ""
        self.channel = 1
        self.editors: dict[str, OperationEditor] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.refresh_button = refresh = QPushButton("刷新实机范围与选件")
        refresh.clicked.connect(self.refresh_capabilities)
        layout.addWidget(refresh)
        self.snapshot_status = QLabel()
        self.snapshot_status.setObjectName("rangeHint")
        self.snapshot_status.setWordWrap(True)
        layout.addWidget(self.snapshot_status)
        controller.refreshed.connect(self.show_snapshot_status)
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self.read_visible)
        layout.addWidget(self.tabs)
        self.groups: list[list[str]] = []

    def show_snapshot_status(self) -> None:
        self.refresh_button.setEnabled(
            not self.controller.states.get(self.device_id, {}).get("controlsLocked")
        )
        snapshot = self.controller.runtime.sessions.capability_snapshot(self.device_id)
        self.snapshot_status.setText(
            f"范围快照 v{snapshot.version} · {snapshot.timestamp} · "
            + ("已过期：" + snapshot.error if snapshot.stale else "已更新")
            if snapshot
            else "尚未连接 · 使用型号文档范围，实机动态范围未确认"
        )

    def refresh_capabilities(self) -> None:
        if not self.device_id:
            return
        try:
            self.controller.watch(
                self.controller.runtime.sessions.submit(self.device_id, "refresh_capabilities")
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def set_device(self, device_id: str, channel: int = 1) -> None:
        if (device_id, channel, self.controller.mode) == (
            self.device_id,
            self.channel,
            getattr(self, "mode", ""),
        ):
            return
        self.device_id, self.channel, self.mode = device_id, channel, self.controller.mode
        self.tabs.blockSignals(True)
        while self.tabs.count():
            page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            if page is not None:
                page.deleteLater()
        self.editors, self.groups = {}, [[], [], []]
        if device_id:
            schema = self.controller.capabilities(device_id, channel)
            layouts = []
            for title in ("常用", "高级", "状态与诊断"):
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setMinimumWidth(300)
                body = QWidget()
                group_layout = QVBoxLayout(body)
                group_layout.setContentsMargins(4, 10, 12, 10)
                scroll.setWidget(body)
                layouts.append(group_layout)
                self.tabs.addTab(scroll, title)
            for cap in schema.operations:
                if self.capability_filter and not self.capability_filter(cap, schema):
                    continue
                group = 2 if not cap.writable else 0 if cap.name in COMMON else 1
                editor = OperationEditor(self.controller, device_id, cap, channel)
                layouts[group].addWidget(editor)
                self.editors[cap.name] = editor
                self.groups[group].append(cap.name)
            for group_layout in layouts:
                group_layout.addStretch()
        self.tabs.blockSignals(False)
        self.show_snapshot_status()
        self.read_visible()

    def read_visible(self, *_args) -> None:
        if not self.device_id or not self.isVisible():
            return
        state = self.controller.states.get(self.device_id, {})
        if (
            state.get("connectionState") not in {"connected", "busy"}
            or state.get("activeTask") == "continuous_acquisition"
            or state.get("controlsLocked")
        ):
            return
        for name in self.groups[self.tabs.currentIndex()]:
            editor = self.editors[name]
            if editor.cap.available and not editor.data_only:
                editor.read()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.read_visible()
