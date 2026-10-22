"""Task-oriented signal source controls, backed by the existing session worker."""

from __future__ import annotations

from typing import Any, cast

import shiboken6
from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.controls import OPERATION_LABELS
from lab_control.domain.instruments import Capability, DeviceCommand, DeviceError
from lab_control.domain.readback import boolean_value
from lab_control.ui.controller import WorkspaceController, display_connection_state
from lab_control.ui.pages.device_control.form import OperationEditor, concise
from lab_control.ui.pages.device_control.source_settings import SourceSettingsDialog, settings_icon
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

MAIN = ("waveform", "frequency", "power", "amplitude", "offset")
PRIMARY_CONTROLS = (*MAIN, "phase")
EXTRA = ("phase", "load", "pulse_duty", "pulse_width", "ramp_symmetry", "amplitude_unit")
ORDER = ("waveform", "load", "amplitude_unit", "frequency", "power", "amplitude", "offset", "phase")


def newer_token(token: tuple[str, int, int], previous: tuple[str, int, int] | None) -> bool:
    return previous is None or token[:2] != previous[:2] or token[2] > previous[2]


class TaskTabs(QTabWidget):
    """Size only the selected task; upload tables must not stretch unrelated tasks."""

    def sizeHint(self) -> QSize:  # noqa: N802
        page = self.currentWidget()
        if page is None:
            return QSize(0, 0)
        hint = page.sizeHint()
        if page.hasHeightForWidth():
            hint.setHeight(page.heightForWidth(max(1, self.width() - 4)))
        return QSize(hint.width(), hint.height() + self.tabBar().sizeHint().height() + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, 0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        page = self.currentWidget()
        if page is None:
            return 0
        height = (
            page.heightForWidth(max(1, width - 4)) if page.hasHeightForWidth() else page.sizeHint().height()
        )
        return height + self.tabBar().sizeHint().height() + 4


class ParameterGroup(QFrame):
    def __init__(
        self,
        controller: WorkspaceController,
        device_id: str,
        channel: int,
        caps: list[Capability],
        *,
        columns: int = 1,
        simplified: bool = False,
        follow_readback: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.controller, self.device_id, self.channel = controller, device_id, channel
        self.editors: dict[str, OperationEditor] = {}
        self.pending: list[tuple[str, dict[str, Any]]] = []
        self.completed: list[str] = []
        self.running = False
        self.reading = False
        self.columns = columns
        self.simplified = simplified
        self.follow_readback = follow_readback
        self.visible_columns = 0
        self.context_hidden: set[str] = set()
        self.success_report = False
        self.setObjectName("sourceGroup")
        self.setProperty("simplified", simplified)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*(20, 18, 20, 18) if simplified else (12, 10, 12, 10))
        layout.setSpacing(16 if simplified else 8)
        self.fields = QGridLayout()
        self.fields.setContentsMargins(0, 0, 0, 0)
        self.fields.setHorizontalSpacing(18 if simplified else 8)
        self.fields.setVerticalSpacing(18 if simplified else 8)
        self.fields.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addLayout(self.fields)
        for index, cap in enumerate(caps):
            standalone = not cap.parameters or any(p.kind == "array" for p in cap.parameters.values())
            editor = OperationEditor(
                controller,
                device_id,
                cap,
                channel,
                parent=self,
                compact=not standalone,
                actions=standalone,
                simplified=simplified,
                follow_readback=follow_readback,
            )
            editor.enable_auto_sync()
            self.editors[cap.name] = editor
            self.fields.addWidget(editor, index // columns, index % columns)
        self.status = QLabel()
        self.status.setObjectName("inputFeedback")
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        self.footer_layout = QGridLayout()
        self.apply_button = QPushButton("应用修改")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.setProperty("buttonSize", "dialog")
        self.apply_button.setFixedWidth(110)
        self.apply_button.clicked.connect(self.apply)
        self.count = QLabel()
        self.count.setObjectName("mutedText")
        self.footer_layout.addWidget(self.count, 0, 0)
        self.footer_layout.addWidget(self.apply_button, 0, 1)
        self.footer_layout.setColumnStretch(0, 1)
        self.compact_actions = False
        layout.addLayout(self.footer_layout)
        layout.addStretch()
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        controller.refreshed.connect(self.refresh)
        self.reflow_fields()
        self.refresh()

    def reflow_fields(self) -> None:
        columns = self.columns if self.simplified or self.width() >= 720 else 1
        if columns == self.visible_columns:
            return
        while self.fields.count():
            self.fields.takeAt(0)
        row, column = 0, 0
        for name, editor in self.editors.items():
            if name in self.context_hidden:
                continue
            span = columns if editor.array_editor or (self.simplified and name == "waveform") else 1
            if span > 1 and column:
                row, column = row + 1, 0
            self.fields.addWidget(editor, row, column, 1, span, Qt.AlignmentFlag.AlignTop)
            column += span
            if column >= columns:
                row, column = row + 1, 0
        self.fields.setColumnStretch(0, 1)
        self.fields.setColumnStretch(1, 1 if columns > 1 else 0)
        self.visible_columns = columns

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        # Dynamic feedback can invalidate a nested QFormLayout's height cache after its grid parent.
        return max(super().heightForWidth(width), self.minimumSizeHint().height())

    def update_row_heights(self) -> None:
        heights: dict[int, int] = {}
        for index in range(self.fields.count()):
            item = self.fields.itemAt(index)
            editor = item.widget() if item else None
            if editor is None or editor.isHidden():
                continue
            row, _, _, _ = cast(tuple[int, int, int, int], self.fields.getItemPosition(index))
            # SetMinimumSize on a child's layout can update its hard minimum before
            # the grid's height-for-width cache catches up after text/unit changes.
            height = max(
                editor.minimumHeight(),
                editor.minimumSizeHint().height(),
                editor.heightForWidth(editor.width()),
            )
            heights[row] = max(heights.get(row, 0), height)
        for row in range(self.fields.rowCount()):
            height = heights.get(row, 0)
            if self.fields.rowMinimumHeight(row) != height:
                self.fields.setRowMinimumHeight(row, height)

    def event(self, event) -> bool:
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest and hasattr(self, "fields"):
            self.update_row_heights()
        return result

    def set_waveform(self, waveform: str) -> None:
        hidden = {
            name
            for name in self.editors
            if (name in {"pulse_width", "pulse_duty"} and waveform not in {"PULS", "PULSE"})
            or (name == "ramp_symmetry" and waveform != "RAMP")
        }
        if hidden == self.context_hidden:
            return
        self.context_hidden = hidden
        for name, editor in self.editors.items():
            editor.setVisible(name not in hidden)
        self.visible_columns = 0
        self.reflow_fields()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.reflow_fields()
        self.update_row_heights()
        compact = self.width() < 350
        if compact != self.compact_actions:
            self.compact_actions = compact
            for widget in (self.count, self.apply_button):
                self.footer_layout.removeWidget(widget)
            self.footer_layout.addWidget(self.count, 0, 0, 1, 2 if compact else 1)
            self.footer_layout.addWidget(self.apply_button, 1 if compact else 0, 1)
            self.footer_layout.setColumnStretch(0, 1 if not compact else 0)

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        connected = state.get("connectionState") in {"connected", "busy"}
        busy = self.device_id in self.controller.group_devices
        allowed = connected and not busy and not state.get("controlPending")
        allowed &= state.get("activeTask") != "continuous_acquisition"
        allowed &= not state.get("controlsLocked")
        dirty = sum(
            e.dirty and e.cap.writable and bool(e.editors)
            for name, e in self.editors.items()
            if name not in self.context_hidden
        )
        health, reason = self.controller.runtime.monitoring.settings_status(self.device_id, self.sync_keys())
        self.count.setText(f"{dirty} 项待应用" if dirty else health)
        self.count.setToolTip(reason)
        if self.success_report and health == "正常" and not dirty:
            self.message("")
            self.success_report = False
        self.apply_button.setEnabled(bool(allowed and dirty and not state.get("capabilityStale", True)))
        for editor in self.editors.values():
            for field in editor.editors.values():
                field.setEnabled(
                    editor.cap.available
                    and editor.cap.writable
                    and not busy
                    and not state.get("controlsLocked")
                )

    def sync_keys(self) -> tuple[tuple[int, str], ...]:
        return tuple(
            (editor.channel, name)
            for name, editor in self.editors.items()
            if editor.cap.available
            and editor.cap.readable
            and not editor.array_editor
            and name not in self.context_hidden
        )

    def read(self) -> None:
        self.controller.runtime.monitoring.refresh_settings(self.device_id)

    def show_readings(self, readings) -> None:
        if not shiboken6.isValid(self):
            return
        self.reading = False
        for reading in readings:
            editor = self.editors.get(reading.operation)
            if editor:
                editor.show_reading(reading)

    def message(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def failed_read(self, error: Exception) -> None:
        if shiboken6.isValid(self):
            self.reading = False
            self.message(str(error))

    def apply(self) -> None:
        if self.running or self.device_id in self.controller.group_devices:
            return
        self.pending, self.completed = [], []
        try:
            self.controller.ensure_applicable(self.device_id)
            selected = [
                e
                for name, e in self.editors.items()
                if name not in self.context_hidden
                and e.dirty
                and e.cap.writable
                and e.editors
                and not e.array_editor
            ]
            # Validate every proposed value before the first write; output is a separate control.
            prepared = [(e.cap.name, e.parameters()) for e in selected]
            if not prepared:
                return
            confirmations = [e for e in selected if e.cap.requires_confirmation]
            if confirmations:
                description = "\n".join(
                    OPERATION_LABELS.get(e.cap.name, e.cap.name) + "：" + concise(e.parameters())
                    for e in confirmations
                )
                record = self.controller.records[self.device_id]
                if (
                    QMessageBox.question(
                        self,
                        "确认设备操作",
                        f"{record['identity'].get('model')} · CH {self.channel}\n"
                        + record["config"]["resource"]
                        + "\n"
                        + description,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    != QMessageBox.StandardButton.Yes
                ):
                    return
            prepared.sort(key=lambda item: ORDER.index(item[0]) if item[0] in ORDER else len(ORDER))
            self.pending, self.completed = prepared, []
            self.running = True
            self.controller.begin_group(self.device_id)
            self.controller.refreshed.emit()
            self.next_command()
        except Exception as exc:
            self.finish(str(exc))

    def next_command(self) -> None:
        if not self.pending:
            self.finish("全部修改已应用")
            return
        name, expected = self.pending[0]
        try:
            # Previous writes may change ranges. Correct the draft, but never silently send the correction.
            for pending_name, values in self.pending:
                editor = self.editors[pending_name]
                editor.refresh_enabled()
                actual = editor.parameters()
                if actual != values or not editor.cap.available:
                    self.finish("范围或可用性已更新；请核对修正后的草稿，再次应用剩余参数")
                    return
            editor = self.editors[name]
            editor._submitted_revision = editor._edit_revision
            editor.busy = True
            self.message("正在应用：" + OPERATION_LABELS.get(name, name))
            self.controller.apply(
                self.device_id,
                DeviceCommand(name, expected, editor.channel, editor.cap.requires_confirmation),
                self.applied,
                lambda exc: self.finish(str(exc), failed=name),
                group_member=True,
            )
        except Exception as exc:
            self.finish(str(exc))

    def applied(self, result) -> None:
        name, _ = self.pending[0]
        self.editors[name].applied(result)
        if result.outcome != "success":
            self.finish(result.error or result.outcome, failed=name)
            return
        self.completed.append(name)
        self.pending.pop(0)
        self.next_command()

    def finish(self, reason: str, *, failed: str | None = None) -> None:
        self.running = False
        for editor in self.editors.values():
            editor.busy = False
        self.success_report = not failed and not self.pending
        self.controller.end_group(self.device_id)
        done = "、".join(OPERATION_LABELS.get(name, name) for name in self.completed)
        waiting = "、".join(OPERATION_LABELS.get(name, name) for name, _ in self.pending if name != failed)
        self.message(
            reason
            + (f" · 已完成：{done}" if done else "")
            + (f" · 失败：{OPERATION_LABELS.get(failed, failed)}" if failed else "")
            + (f" · 未执行：{waiting}" if waiting else "")
        )
        self.controller.refreshed.emit()


class OutputControl(QWidget):
    def __init__(
        self,
        controller: WorkspaceController,
        device_id: str,
        channel: int,
        *,
        rf: bool,
        embedded: bool = False,
    ) -> None:
        super().__init__()
        self.controller, self.device_id, self.channel, self.rf = controller, device_id, channel, rf
        self.embedded = embedded
        self.value: bool | None = None
        self.sync_token: tuple[str, int, int] | None = None
        self.channel_status: tuple[str, str] = ("同步中", "等待通道参数与输出状态")
        self.busy = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.state = QLabel()
        self.state.setObjectName("outputState")
        self.state.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.button = QPushButton("输出控制")
        self.button.setObjectName("sourceOutputButton" if embedded else "")
        self.button.setProperty("buttonSize", "dialog")
        self.button.clicked.connect(self.toggle)
        self.button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row.addWidget(self.state)
        row.addWidget(self.button)
        if not embedded:
            row.addStretch()
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def set_value(self, value: Any) -> None:
        if not shiboken6.isValid(self):
            return
        self.value = boolean_value(value)
        self.refresh()

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        capability = next(
            (
                cap
                for cap in (
                    self.controller.capabilities(self.device_id, self.channel).operations
                    if self.device_id in self.controller.records
                    else ()
                )
                if cap.name == "output"
            ),
            None,
        )
        output_health = self.controller.runtime.monitoring.settings_status(
            self.device_id, ((self.channel, "output"),)
        )
        health, reason = self.channel_status or output_health
        self.state.setText(
            ("输出" if self.embedded else "RF " if self.rf else f"CH {self.channel} ")
            + ("开启" if self.value is True else "关闭" if self.value is False else "输出未知")
            + " · "
            + health
        )
        self.state.setToolTip(reason)
        if self.embedded and self.state.property("outputActive") != bool(self.value):
            self.state.setProperty("outputActive", bool(self.value))
            self.state.style().unpolish(self.state)
            self.state.style().polish(self.state)
        self.button.setText(
            "关闭输出" if self.value is True else "开启输出" if self.value is False else "输出控制"
        )
        self.button.setEnabled(
            capability is not None
            and capability.available
            and capability.writable
            and self.value is not None
            and output_health[0] == "正常"
            and state.get("connectionState") in {"connected", "busy"}
            and not self.busy
            and not state.get("controlPending")
            and not state.get("controlsLocked")
            and self.device_id not in self.controller.group_devices
            and state.get("activeTask") != "continuous_acquisition"
        )
        self.button.setToolTip(capability.reason if capability else "此型号没有可用的输出控制")

    def read(self) -> None:
        self.controller.runtime.monitoring.refresh_settings(self.device_id)

    def toggle(self) -> None:
        if self.busy or self.device_id in self.controller.group_devices or not self.button.isEnabled():
            return
        if self.value is None:
            self.read()
            return
        record = self.controller.records[self.device_id]
        target = not self.value
        if (
            QMessageBox.question(
                self,
                "确认输出操作",
                f"{record['identity'].get('model')} · CH {self.channel}\n"
                + record["config"]["resource"]
                + f"\n{'开启' if target else '关闭'}输出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.busy = True
            self.refresh()
            self.controller.apply(
                self.device_id,
                DeviceCommand("output", {"value": target}, self.channel, True),
                self.applied,
                self.failed,
            )
        except Exception as exc:
            self.failed(exc)

    def applied(self, result) -> None:
        if not shiboken6.isValid(self):
            return
        self.busy = False
        self.set_value(result.readback)
        if result.outcome != "success":
            self.state.setText("输出待核实 · " + (result.error or result.outcome))
        self.read()

    def failed(self, error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        self.busy = False
        self.value = None
        self.refresh()
        self.state.setText("输出待核实")
        self.state.setToolTip(str(error))


class SignalSourcePage(QWidget):
    def __init__(self, controller: WorkspaceController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.device_id = ""
        self.mode = controller.mode
        self.panels: dict[int, ParameterGroup] = {}
        self.outputs: dict[int, OutputControl] = {}
        self.advanced_groups: list[ParameterGroup] = []
        self.channel_cards: list[QWidget] = []
        self.last_columns = 0
        self.settings_dialog: SourceSettingsDialog | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        toolbar = QHBoxLayout()
        self.selector = DeviceSelector(controller, "signal_generator", compact=True)
        self.selector.info.hide()
        self.selector.channel.hide()
        self.selector.combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        toolbar.addWidget(self.selector, 1)
        self.connection_label = QLabel("未选择", self)
        self.connection_label.setObjectName("sourceConnectionState")
        toolbar.addWidget(self.connection_label)
        self.refresh_button = QPushButton("刷新范围")
        self.refresh_button.clicked.connect(self.refresh_ranges)
        toolbar.addWidget(self.refresh_button)
        self.monitor_button = monitor = QPushButton("实时监测 ↗", self)
        monitor.clicked.connect(
            lambda: controller.navigate.emit("实时监测", "signal_generator", self.device_id)
        )
        toolbar.addWidget(monitor)
        monitor.hide()
        self.settings_button = QPushButton("高级设置", self)
        self.settings_button.setObjectName("sourceSettingsButton")
        self.settings_button.setIcon(settings_icon())
        self.settings_button.setIconSize(QSize(18, 18))
        self.settings_button.clicked.connect(self.open_settings)
        self.settings_button.show()
        toolbar.addWidget(self.settings_button)
        layout.addLayout(toolbar)
        self.range_status = QLabel("范围尚未确认", self)
        self.range_status.setObjectName("mutedText")
        self.range_status.hide()
        self.output_bar = QGridLayout()
        layout.addLayout(self.output_bar)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll_area, 1)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(12)
        self.empty = QLabel("暂无选中的已连接信号源。请先在设备总览连接设备，再从上方选择。")
        self.empty.setWordWrap(True)
        self.empty.setObjectName("mutedText")
        self.body_layout.addWidget(self.empty)
        self.grid = QGridLayout()
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.body_layout.addLayout(self.grid)
        self.tabs = TaskTabs()
        self.advanced_channel = WorkspaceComboBox(self.tabs)
        self.tabs.setCornerWidget(self.advanced_channel)
        self.tabs.hide()
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.tabs.currentChanged.connect(self.read_advanced)
        self.body_layout.addWidget(self.tabs)
        self.body_layout.addStretch()
        self.scroll_area.setWidget(self.body)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.settings_changed.connect(self.update_readings)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if device_id and not self.controller.has_connected_session(device_id):
            device_id = ""
        if (device_id, self.controller.mode) == (self.device_id, self.mode):
            return
        if (
            device_id
            and self.device_id in self.controller.group_devices
            and self.controller.has_connected_session(self.device_id)
        ):
            self.selector.select(self.device_id)
            self.controller.message.emit("当前设备正在应用参数，完成后再切换设备")
            return
        if self.settings_dialog is not None:
            self.settings_dialog.reject()
        self.empty.setVisible(not device_id)
        self.tabs.hide()
        self.settings_button.show()
        self.monitor_button.hide()
        if not device_id and self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
        self.device_id, self.mode = device_id, self.controller.mode
        self.tabs.blockSignals(True)
        self.advanced_channel.blockSignals(True)
        self.advanced_channel.clear()
        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            if widget is not None:
                widget.deleteLater()
        for area in (self.grid, self.output_bar):
            while area.count():
                item = area.takeAt(0)
                widget = item.widget() if item else None
                if widget is not None:
                    widget.deleteLater()
        self.panels, self.outputs, self.channel_cards, self.advanced_groups = {}, {}, [], []
        if device_id:
            schema = self.controller.capabilities(device_id)
            self.rf = any(cap.name == "power" for cap in schema.operations)
            for ch in schema.channels:
                self.advanced_channel.addItem(f"CH {ch}", ch)
                caps = self.controller.capabilities(device_id, ch).operations
                main = [cap for name in PRIMARY_CONTROLS for cap in caps if cap.name == name]
                card = QFrame()
                card.setObjectName("channelPanel")
                card.setProperty("simplified", True)
                card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(0, 0, 0, 0)
                card_layout.setSpacing(0)
                heading = QLabel("射频信号" if self.rf else f"CH {ch}")
                heading.setObjectName("channelTitle")
                heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
                output = OutputControl(self.controller, device_id, ch, rf=self.rf, embedded=True)
                self.outputs[ch] = output
                header = QFrame(card)
                header.setObjectName("sourceChannelHeader")
                header_layout = QHBoxLayout(header)
                header_layout.setContentsMargins(20, 14, 20, 14)
                header_layout.addWidget(heading)
                header_layout.addStretch()
                header_layout.addWidget(output)
                card_layout.addWidget(header)
                group = ParameterGroup(
                    self.controller,
                    device_id,
                    ch,
                    main,
                    columns=2,
                    simplified=True,
                )
                card_layout.addWidget(group)
                self.panels[ch] = group
                self.channel_cards.append(card)
        self.advanced_channel.blockSignals(False)
        self.tabs.blockSignals(False)
        self.last_columns = 0
        self.reflow()
        self.refresh()
        if self.isVisible():
            self.read_main()

    def read_main(self) -> None:
        self.read_advanced()
        self.update_readings(self.device_id)

    def open_settings(self) -> None:
        if not self.device_id:
            return
        if self.settings_dialog is not None:
            self.settings_dialog.raise_()
            return
        dialog = SourceSettingsDialog(self.controller, self.device_id, self)
        self.settings_dialog = dialog
        dialog.finished.connect(self.settings_closed)
        dialog.show()
        dialog.adjustSize()
        position = self.window().frameGeometry().center() - dialog.rect().center()
        screen = self.window().screen().availableGeometry()
        position.setX(max(screen.left(), min(position.x(), screen.right() - dialog.width() + 1)))
        position.setY(max(screen.top(), min(position.y(), screen.bottom() - dialog.height() + 1)))
        dialog.move(position)
        self.read_advanced()
        self.controller.runtime.monitoring.refresh_settings(self.device_id)

    def settings_closed(self, *_args) -> None:
        self.settings_dialog = None
        self.read_advanced()

    def visible_sync_keys(self) -> tuple[tuple[int, str], ...]:
        keys = tuple(key for panel in self.panels.values() for key in panel.sync_keys())
        if self.settings_dialog is not None and self.settings_dialog.context_valid():
            keys += self.settings_dialog.sync_keys()
        return tuple(dict.fromkeys(keys))

    def read_advanced(self, *_args) -> None:
        self.tabs.updateGeometry()
        if self.isVisible():
            self.controller.focus_settings(self.device_id, self.visible_sync_keys())
            self.update_readings(self.device_id)

    def update_readings(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible():
            return
        state = self.controller.states.get(device_id, {})
        if state.get("controlPending") or state.get("controlGroup"):
            return
        frames = self.controller.runtime.sessions.readback_frames(device_id)
        editors = [editor for panel in self.panels.values() for editor in panel.editors.values()]
        advanced = self.tabs.currentWidget()
        if isinstance(advanced, ParameterGroup):
            editors += list(advanced.editors.values())
        for editor in editors:
            frame = frames.get((editor.channel, editor.cap.name))
            if not frame or (frame.session_id, frame.configuration_version) != (
                state.get("sessionId"),
                state.get("configurationVersion"),
            ):
                editor._reading_valid = False
                continue
            token = (frame.session_id, frame.configuration_version, frame.sequence)
            if newer_token(token, editor.sync_token):
                old = editor.last_reading
                unchanged = (
                    editor._reading_valid
                    and editor.sync_token is not None
                    and token[:2] == editor.sync_token[:2]
                    and old is not None
                    and (old.value, old.source, old.quality, old.error)
                    == (frame.reading.value, frame.reading.source, frame.reading.quality, frame.reading.error)
                )
                editor.sync_token = token
                if unchanged:
                    # Keep freshness/raw timestamps without redisplaying identical values.
                    editor.last_reading = frame.reading
                else:
                    editor.show_reading(frame.reading)
        for channel, output in self.outputs.items():
            frame = frames.get((channel, "output"))
            if frame and (frame.session_id, frame.configuration_version) == (
                state.get("sessionId"),
                state.get("configurationVersion"),
            ):
                token = (frame.session_id, frame.configuration_version, frame.sequence)
                if newer_token(token, output.sync_token):
                    output.sync_token = token
                    value = boolean_value(frame.reading.value)
                    if value != output.value:
                        output.set_value(value)
            elif state.get("sessionId") != (output.sync_token or (None,))[0]:
                output.set_value(None)

    def refresh_ranges(self) -> None:
        if self.device_id:
            try:
                self.controller.watch(
                    self.controller.runtime.sessions.submit(self.device_id, "refresh_capabilities")
                )
            except DeviceError as exc:
                self.controller.message.emit(str(exc))

    def refresh(self) -> None:
        if self.mode != self.controller.mode:
            self.select_device(self.selector.device_id)
            return
        if self.isVisible():
            self.update_readings(self.device_id)
        state = self.controller.states.get(self.device_id, {})
        self.refresh_button.setEnabled(bool(self.device_id) and not state.get("controlsLocked"))
        self.connection_label.setText(
            {"connected": "已连接", "busy": "已连接 · 忙碌", "degraded": "连接异常", "error": "连接异常"}.get(
                display_connection_state(state), "未连接" if self.device_id else "未选择"
            )
        )
        self.monitor_button.setEnabled(bool(self.device_id))
        busy = self.device_id in self.controller.group_devices
        self.settings_button.setEnabled(bool(self.device_id) and not busy and not state.get("controlsLocked"))
        self.selector.combo.setEnabled(not busy)
        if hasattr(self, "advanced_channel"):
            self.advanced_channel.setEnabled(not busy)
            main = self.panels.get(self.advanced_channel.currentData())
            waveform = main.editors.get("waveform") if main else None
            if waveform and waveform.current_value:
                value = waveform.current_value
                value = value.get("value", "") if isinstance(value, dict) else str(value)
                for panel in self.advanced_groups:
                    panel.set_waveform(value)
        if self.isVisible():
            self.controller.focus_settings(self.device_id, self.visible_sync_keys())
        if self.isVisible():
            advanced = self.tabs.currentWidget()
            for channel, output in self.outputs.items():
                panel = self.panels[channel]
                keys = panel.sync_keys() + ((channel, "output"),)
                groups = [panel]
                if isinstance(advanced, ParameterGroup) and advanced.channel == channel:
                    keys += advanced.sync_keys()
                    groups.append(advanced)
                health = self.controller.runtime.monitoring.settings_status(self.device_id, keys)
                if health[0] == "正常" and any(e.dirty for group in groups for e in group.editors.values()):
                    health = ("待应用", "保留未提交草稿；其他参数继续自动同步")
                output.channel_status = health
                output.refresh()
        snapshot = self.controller.runtime.sessions.capability_snapshot(self.device_id)
        self.range_status.setText(
            "范围已过期 · 写入前需重新核实"
            if snapshot and snapshot.stale
            else "范围已更新 · 各参数下方显示有效范围与来源"
            if snapshot
            else "未连接 · 显示型号文档范围，实机范围待确认"
        )
        details = getattr(self, "details", None)
        if (
            isinstance(details, (QPlainTextEdit, QTextEdit))
            and self.tabs.currentWidget() is details
            and self.isVisible()
        ):
            record = self.controller.records.get(self.device_id, {})
            identity = record.get("identity", {})
            text = (
                f"型号：{identity.get('model', '')}\n序列号：{identity.get('serial', '')}\n"
                f"资源：{record.get('config', {}).get('resource', '')}\n"
                + (
                    f"范围版本：{snapshot.version}\n更新时间：{snapshot.timestamp}\n"
                    f"选件：{', '.join(snapshot.options) or '无 / 未报告'}\n"
                    f"其他读回：{concise(snapshot.details)}\n{snapshot.error}"
                    if snapshot
                    else "尚无实机会话快照"
                )
            )
            frames = self.controller.runtime.sessions.readback_frames(self.device_id)
            text += "\n原始读回（含最后一次读取时间）：\n" + "\n".join(
                f"CH {channel} {name}：{concise(frame.reading.value)} · {frame.reading.timestamp}"
                for (channel, name), frame in sorted(frames.items())
            )
            if details.toPlainText() != text:
                details.setPlainText(text)
        self.refresh_button.setToolTip(self.range_status.text())
        self.refresh_button.setText("范围已过期 ↻" if snapshot and snapshot.stale else "刷新范围")

    def reflow(self) -> None:
        for panel in [*self.panels.values(), *self.advanced_groups]:
            panel.reflow_fields()
        columns = 2 if len(self.channel_cards) > 1 else 1
        if columns == self.last_columns:
            return
        while self.grid.count():
            self.grid.takeAt(0)
        for index, card in enumerate(self.channel_cards):
            self.grid.addWidget(card, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1 if columns == 2 else 0)
        self.last_columns = columns

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        QTimer.singleShot(0, self.reflow)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.read_main()

    def hideEvent(self, event) -> None:  # noqa: N802
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
        super().hideEvent(event)
