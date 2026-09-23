"""Task-oriented signal source controls, backed by the existing session worker."""

from __future__ import annotations

from typing import Any, cast

import shiboken6
from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.controls import OPERATION_LABELS
from lab_control.domain.instruments import Capability, DeviceCommand, DeviceError
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.form import OperationEditor, concise
from lab_control.ui.widgets.device_selector import DeviceSelector

MAIN = ("waveform", "frequency", "power", "amplitude", "offset")
EXTRA = ("phase", "load", "pulse_duty", "pulse_width", "ramp_symmetry", "amplitude_unit")
ORDER = ("waveform", "load", "amplitude_unit", "frequency", "power", "amplitude", "offset", "phase")


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
        self.visible_columns = 0
        self.context_hidden: set[str] = set()
        self.setObjectName("sourceGroup")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self.fields = QGridLayout()
        self.fields.setContentsMargins(0, 0, 0, 0)
        self.fields.setSpacing(8)
        self.fields.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addLayout(self.fields)
        for index, cap in enumerate(caps):
            standalone = not cap.parameters or any(p.kind == "array" for p in cap.parameters.values())
            editor = OperationEditor(
                controller, device_id, cap, channel, parent=self, compact=not standalone, actions=standalone
            )
            self.editors[cap.name] = editor
            self.fields.addWidget(editor, index // columns, index % columns)
        self.status = QLabel()
        self.status.setObjectName("inputFeedback")
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        self.footer_layout = QGridLayout()
        self.read_button = QPushButton("读取当前值")
        self.apply_button = QPushButton("应用修改")
        self.apply_button.setObjectName("primaryButton")
        for button in (self.read_button, self.apply_button):
            button.setFixedSize(110, 38)
        self.read_button.clicked.connect(self.read)
        self.apply_button.clicked.connect(self.apply)
        self.count = QLabel()
        self.count.setObjectName("mutedText")
        self.footer_layout.addWidget(self.count, 0, 0)
        self.footer_layout.addWidget(self.read_button, 0, 1)
        self.footer_layout.addWidget(self.apply_button, 0, 2)
        self.footer_layout.setColumnStretch(0, 1)
        self.compact_actions = False
        layout.addLayout(self.footer_layout)
        layout.addStretch()
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def reflow_fields(self) -> None:
        columns = self.columns if self.width() >= 720 else 1
        if columns == self.visible_columns:
            return
        while self.fields.count():
            self.fields.takeAt(0)
        row, column = 0, 0
        for name, editor in self.editors.items():
            if name in self.context_hidden:
                continue
            span = columns if editor.array_editor else 1
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
            height = max(editor.minimumSizeHint().height(), editor.heightForWidth(editor.width()))
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
            for widget in (self.count, self.read_button, self.apply_button):
                self.footer_layout.removeWidget(widget)
            self.footer_layout.addWidget(self.count, 0, 0, 1, 2 if compact else 1)
            self.footer_layout.addWidget(self.read_button, 1 if compact else 0, 0 if compact else 1)
            self.footer_layout.addWidget(self.apply_button, 1 if compact else 0, 1 if compact else 2)
            self.footer_layout.setColumnStretch(0, 1 if not compact else 0)

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        connected = state.get("connectionState") in {"connected", "busy"}
        busy = self.device_id in self.controller.group_devices
        allowed = connected and not busy and not state.get("controlPending")
        allowed &= state.get("activeTask") != "continuous_acquisition"
        dirty = sum(
            e.dirty and e.cap.writable and bool(e.editors)
            for name, e in self.editors.items()
            if name not in self.context_hidden
        )
        self.count.setText(
            f"{dirty} 项待应用"
            if dirty
            else "已读取当前设置"
            if any(e.current_value is not None for e in self.editors.values())
            else "尚未读取当前设置"
        )
        self.apply_button.setEnabled(bool(allowed and dirty and not state.get("capabilityStale", True)))
        self.read_button.setEnabled(bool(allowed and not self.reading))
        for editor in self.editors.values():
            for field in editor.editors.values():
                field.setEnabled(editor.cap.available and editor.cap.writable and not busy)

    def read(self) -> None:
        if self.reading:
            return
        names = tuple(
            name
            for name, editor in self.editors.items()
            if editor.cap.available and editor.cap.readable and name not in self.context_hidden
        )
        if not names:
            return
        try:
            self.reading = True
            self.controller.watch(
                self.controller.runtime.sessions.submit(
                    self.device_id, "read_settings", (names, self.channel)
                ),
                self.show_readings,
                self.failed_read,
            )
        except Exception as exc:
            self.failed_read(exc)

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
            self.controller.group_devices.add(self.device_id)
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
        self.controller.group_devices.discard(self.device_id)
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
    def __init__(self, controller: WorkspaceController, device_id: str, channel: int, *, rf: bool) -> None:
        super().__init__()
        self.controller, self.device_id, self.channel, self.rf = controller, device_id, channel, rf
        self.value: bool | None = None
        self.busy = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.state = QLabel()
        self.state.setObjectName("outputState")
        self.state.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.button = QPushButton("读取输出状态")
        self.button.clicked.connect(self.toggle)
        self.button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row.addWidget(self.state)
        row.addWidget(self.button)
        row.addStretch()
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def set_value(self, value: Any) -> None:
        if not shiboken6.isValid(self):
            return
        if isinstance(value, dict):
            value = value.get("value")
        token = str(value).upper()
        self.value = (
            True if token in {"TRUE", "ON", "1"} else False if token in {"FALSE", "OFF", "0"} else None
        )
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
        self.state.setText(
            ("RF " if self.rf else f"CH {self.channel} ")
            + ("ON" if self.value is True else "OFF" if self.value is False else "未读取")
        )
        self.button.setText(
            "关闭输出" if self.value is True else "开启输出" if self.value is False else "读取状态"
        )
        self.button.setEnabled(
            capability is not None
            and capability.available
            and (capability.readable if self.value is None else capability.writable)
            and state.get("connectionState") in {"connected", "busy"}
            and not self.busy
            and not state.get("controlPending")
            and self.device_id not in self.controller.group_devices
            and state.get("activeTask") != "continuous_acquisition"
        )
        self.button.setToolTip(capability.reason if capability else "此型号没有可用的输出控制")

    def read(self) -> None:
        if self.busy:
            return
        try:
            self.busy = True
            self.controller.watch(
                self.controller.runtime.sessions.submit(
                    self.device_id, "read_settings", (("output",), self.channel)
                ),
                self.read_completed,
                self.failed,
            )
        except Exception as exc:
            self.failed(exc)

    def read_completed(self, readings) -> None:
        self.busy = False
        self.set_value(readings[0].value)

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
        elif result.readback is None:
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        toolbar = QHBoxLayout()
        self.selector = DeviceSelector(controller, "signal_generator", compact=True)
        self.selector.info.hide()
        self.selector.channel.hide()
        self.selector.combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        toolbar.addWidget(self.selector, 1)
        self.refresh_button = QPushButton("刷新范围")
        self.refresh_button.clicked.connect(self.refresh_ranges)
        toolbar.addWidget(self.refresh_button)
        monitor = QPushButton("实时监测 ↗")
        monitor.clicked.connect(
            lambda: controller.navigate.emit("实时监测", "signal_generator", self.device_id)
        )
        toolbar.addWidget(monitor)
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
        self.empty = QLabel("尚未选择信号源。请先在设备总览扫描或添加设备，再从上方选择。")
        self.empty.setWordWrap(True)
        self.empty.setObjectName("mutedText")
        self.body_layout.addWidget(self.empty)
        self.grid = QGridLayout()
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.body_layout.addLayout(self.grid)
        self.tabs = TaskTabs()
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.tabs.currentChanged.connect(self.read_advanced)
        self.body_layout.addWidget(self.tabs)
        self.body_layout.addStretch()
        self.scroll_area.setWidget(self.body)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if (device_id, self.controller.mode) == (self.device_id, self.mode):
            return
        if self.device_id in self.controller.group_devices:
            self.selector.select(self.device_id)
            self.controller.message.emit("当前设备正在应用参数，完成后再切换设备")
            return
        self.empty.setVisible(not device_id)
        self.device_id, self.mode = device_id, self.controller.mode
        self.tabs.blockSignals(True)
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
            self.advanced_channel = QComboBox()
            for ch in schema.channels:
                self.advanced_channel.addItem(f"CH {ch}", ch)
                caps = self.controller.capabilities(device_id, ch).operations
                main = [cap for name in MAIN for cap in caps if cap.name == name]
                if self.rf:
                    main += [cap for cap in caps if cap.name == "reference_clock"]
                card = QFrame()
                card.setObjectName("channelPanel")
                card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(0, 0, 0, 0)
                heading = QLabel(("射频信号 · 连续 / 扫频 / 列表" if self.rf else f"CH {ch} · 基本信号"))
                heading.setObjectName("channelTitle")
                heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
                card_layout.addWidget(heading)
                group = ParameterGroup(self.controller, device_id, ch, main, columns=2 if self.rf else 1)
                card_layout.addWidget(group)
                self.panels[ch] = group
                self.channel_cards.append(card)
                output = OutputControl(self.controller, device_id, ch, rf=self.rf)
                self.outputs[ch] = output
                self.output_bar.addWidget(output, 0, len(self.outputs) - 1)
            self.advanced_channel.currentIndexChanged.connect(self.build_advanced)
            self.tabs.setCornerWidget(self.advanced_channel)
            self.build_advanced()
        self.tabs.blockSignals(False)
        self.last_columns = 0
        self.reflow()
        self.refresh()
        if self.isVisible():
            self.read_main()

    def build_advanced(self) -> None:
        self.tabs.blockSignals(True)
        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            if widget is not None:
                widget.deleteLater()
        self.advanced_groups = []
        channel = self.advanced_channel.currentData() or 1
        buckets: dict[str, list[Capability]] = {}
        for cap in self.controller.capabilities(self.device_id, channel).operations:
            name = cap.name
            if name in MAIN or name == "output" or (self.rf and name == "reference_clock"):
                continue
            group = (
                "通道设置"
                if name in EXTRA
                else "扫频"
                if "sweep" in name or name == "frequency_mode"
                else "列表 / 任意波形"
                if name.startswith(("list", "arb", "upload", "load_waveform"))
                else "突发 / 触发"
                if "burst" in name or "trigger" in name
                else "调制"
                if name.startswith(("am_", "fm_", "pm_", "pulse_", "modulation"))
                else "设备设置"
            )
            buckets.setdefault(group, []).append(cap)
        for label in ("通道设置", "扫频", "调制", "突发 / 触发", "列表 / 任意波形", "设备设置"):
            if label in buckets:
                panel = ParameterGroup(self.controller, self.device_id, channel, buckets[label], columns=2)
                self.advanced_groups.append(panel)
                self.tabs.addTab(panel, label)
        self.details = QTextBrowser()
        self.details.setMinimumHeight(160)
        self.tabs.addTab(self.details, "设备详情")
        self.tabs.blockSignals(False)
        self.refresh()
        if self.isVisible():
            self.read_advanced()

    def read_main(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        if state.get("connectionState") not in {"connected", "busy"}:
            return
        for panel in self.panels.values():
            panel.read()
        for output in self.outputs.values():
            output.read()
        self.read_advanced()

    def read_advanced(self, *_args) -> None:
        self.tabs.updateGeometry()
        panel = self.tabs.currentWidget()
        if self.isVisible() and isinstance(panel, ParameterGroup):
            state = self.controller.states.get(self.device_id, {})
            if state.get("connectionState") in {"connected", "busy"}:
                panel.read()

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
        busy = self.device_id in self.controller.group_devices
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
        snapshot = self.controller.runtime.sessions.capability_snapshot(self.device_id)
        self.range_status.setText(
            "范围已过期 · 写入前需重新核实"
            if snapshot and snapshot.stale
            else "范围已更新 · 各参数下方显示有效范围与来源"
            if snapshot
            else "未连接 · 显示型号文档范围，实机范围待确认"
        )
        if hasattr(self, "details"):
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
            if self.details.toPlainText() != text:
                self.details.setPlainText(text)
        self.refresh_button.setToolTip(self.range_status.text())
        self.refresh_button.setText("范围已过期 ↻" if snapshot and snapshot.stale else "刷新范围")

    def reflow(self) -> None:
        available_columns = self.scroll_area.viewport().width() // 205
        output_columns = (
            4 if len(self.outputs) >= 4 and available_columns >= 4 else 2 if len(self.outputs) >= 2 else 1
        )
        for output in self.outputs.values():
            self.output_bar.removeWidget(output)
        for index, output in enumerate(self.outputs.values()):
            self.output_bar.addWidget(output, index // output_columns, index % output_columns)
        for index in range(4):
            self.output_bar.setColumnStretch(index, int(index < output_columns))
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
