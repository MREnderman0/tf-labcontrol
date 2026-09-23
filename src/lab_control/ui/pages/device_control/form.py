from __future__ import annotations

from typing import Any

import shiboken6
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import mode_applicable
from lab_control.application.controls import COMMON, OPERATION_LABELS, PARAMETER_LABELS
from lab_control.domain.instruments import Capability, DeviceCommand, ParameterReading
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.editors import ArrayEditor, ParameterEditor

DATA_OPERATIONS = {"block", "streaming", "phase_noise", "am_noise", "stability", "data_stream", "power"}
SOURCE_LABELS = {
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
        self, controller: WorkspaceController, device_id: str, cap: Capability, channel: int, parent=None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("operationCard")
        self.controller, self.device_id, self.cap = controller, device_id, cap
        self.channel = channel if cap.channel_scoped else 1
        self.mode = controller.mode
        self.dirty = False
        self.busy = False
        self.data_only = not cap.writable and cap.name in DATA_OPERATIONS
        self.editors: dict[str, ParameterEditor] = {}
        self.array_editor: ArrayEditor | None = None
        self.key = f"draft/{self.mode}/{device_id}/{self.channel}/{cap.name}"
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel(OPERATION_LABELS.get(cap.name, cap.name))
        title.setObjectName("controlTitle")
        heading.addWidget(title)
        heading.addStretch()
        heading.addWidget(QLabel(f"CH {self.channel}" if cap.channel_scoped else "整机"))
        layout.addLayout(heading)
        form = QFormLayout()
        for name, parameter in cap.parameters.items():
            if parameter.kind != "array":
                editor = ParameterEditor(parameter)
                self.editors[name] = editor
                editor.changed.connect(self.save_draft)
                form.addRow(PARAMETER_LABELS.get(name, name), editor)
        layout.addLayout(form)
        arrays = {k: v for k, v in cap.parameters.items() if v.kind == "array"}
        if arrays:
            self.array_editor = ArrayEditor(arrays)
            layout.addWidget(self.array_editor)
        self.readback = QLabel("当前值：尚未读取")
        if self.data_only:
            self.readback.setText("在上方选择对应数据项目，使用“单次读取”或“开始监测”获取数据。")
        self.readback.setWordWrap(True)
        self.readback.setObjectName("readback")
        layout.addWidget(self.readback)
        self.range_note = QLabel()
        self.range_note.setObjectName("rangeHint")
        self.range_note.setWordWrap(True)
        layout.addWidget(self.range_note)
        actions = QHBoxLayout()
        self.status = QLabel("" if cap.available else cap.reason)
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.read_button = QPushButton("读取")
        self.read_button.setVisible(not self.data_only)
        self.read_button.clicked.connect(self.read)
        self.apply_button = QPushButton("应用" if cap.parameters else "执行")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply)
        self.apply_button.setVisible(cap.writable)
        actions.addWidget(self.read_button)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
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
        self.dirty = True
        self.status.setText("待应用")
        self.controller.runtime.repository.save_workspace(
            self.key,
            {
                "fields": {name: editor.draft() for name, editor in self.editors.items()},
                "rows": self.array_editor.model.rows if self.array_editor else [],
                "array_path": self.array_editor.path if self.array_editor else "",
            },
        )

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
            f"RF 输出限幅：{details['rf_output_limit_dbm']:g} dBm · 功率偏移：{details.get('power_offset_db', 0):g} dB\n"
            "可设范围不代表保证输出功率；此操作不修改 RF 限幅。"
            if self.cap.name == "power" and "rf_output_limit_dbm" in details
            else ""
        )
        self.range_note.setVisible(bool(self.range_note.text()))
        available = self.cap.available and state.get("connectionState") in {"connected", "busy"}
        available &= state.get("activeTask") != "continuous_acquisition" and not self.busy
        self.read_button.setEnabled(available)
        record = self.controller.records.get(self.device_id, {})
        self.apply_button.setEnabled(
            bool(
                available
                and record
                and mode_applicable(record, self.controller.mode)
                and self.controller.mode == self.mode
                and not state.get("controlPending")
                and not state.get("capabilityStale", True)
            )
        )
        for editor in self.editors.values():
            editor.setEnabled(self.cap.available)

    def read(self) -> None:
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
        self.readback.setText(
            f"{SOURCE_LABELS.get(reading.source, reading.source)}：{concise(reading.value)}\n{reading.timestamp}"
        )
        if not self.dirty and reading.value is not None:
            values = reading.value if isinstance(reading.value, dict) else {"value": reading.value}
            for name, value in values.items():
                if name in self.editors:
                    self.editors[name].set_value(value)
        self.refresh_enabled()

    def parameters(self) -> dict[str, Any]:
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
            self.readback.setText("本次操作返回值：" + concise(result.readback) + "\n" + result.timestamp)
        elif result.outcome == "success":
            self.readback.setText("本会话设置已受理；设备未提供该操作的即时读回\n" + result.timestamp)
        if result.outcome == "success":
            self.dirty = False
            self.controller.runtime.repository.save_workspace(self.key, None)
        self.refresh_enabled()

    def failed(self, error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        self.busy = False
        self.status.setText(str(error))
        self.refresh_enabled()


class ControlForm(QWidget):
    def __init__(self, controller: WorkspaceController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.device_id = ""
        self.channel = 1
        self.editors: dict[str, OperationEditor] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        refresh = QPushButton("刷新实机范围与选件")
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
        ):
            return
        for name in self.groups[self.tabs.currentIndex()]:
            editor = self.editors[name]
            if editor.cap.available and not editor.data_only:
                editor.read()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.read_visible()
