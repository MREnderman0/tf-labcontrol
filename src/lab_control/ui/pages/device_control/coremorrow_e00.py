"""Compact E00 normal/waveform controls with read-only state hydration.

Normal steps submit one existing voltage command; waveform commands and the
durable restoration lifecycle remain in the independent device package.
"""

from __future__ import annotations

import math
from typing import cast

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLCDNumber,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.settings_sync import SettingsSync
from lab_control.domain.instruments import Capability, DeviceCommand, Quality, utc_now
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.power_supply import PowerSupplyPage
from lab_control.ui.pages.device_control.signal_source import ParameterGroup, newer_token
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

from .coremorrow_waveform import CoreMorrowWaveformChannel, resolve_mode


class CoreMorrowE00Channel(ParameterGroup):
    """One compact E00 channel, retaining ParameterGroup's write workflow."""

    MIN_VOLTAGE = 0.0
    MAX_VOLTAGE = 120.0

    def __init__(
        self,
        controller: WorkspaceController,
        device_id: str,
        channel: int,
        voltage: Capability,
        parent=None,
    ) -> None:
        super().__init__(
            controller,
            device_id,
            channel,
            [voltage],
            columns=1,
            follow_readback=False,
            parent=parent,
        )
        self.voltage_cap = voltage
        self.setObjectName("coremorrowChannel")
        self._step_key = f"coremorrow-e00/step/{device_id}/{channel}"

        # ParameterGroup already owns the editor, validation, group lock and
        # apply callback.  Reflow it into a compact left column beside the
        # actual readback display.
        outer = cast(QVBoxLayout, self.layout())
        outer.removeItem(self.fields)
        header = QHBoxLayout()
        title = QLabel(f"CH {channel}")
        title.setObjectName("coremorrowChannelTitle")
        header.addWidget(title)
        header.addStretch()
        self.channel_status = QLabel("等待读回")
        self.channel_status.setObjectName("coremorrowChannelStatus")
        header.addWidget(self.channel_status)
        outer.insertLayout(0, header)

        step_row = QHBoxLayout()
        step_row.setContentsMargins(0, 0, 0, 0)
        direction_label = QLabel("方向")
        direction_label.setObjectName("coremorrowControlLabel")
        step_row.addWidget(direction_label)
        self.direction = WorkspaceComboBox(self)
        self.direction.setObjectName("coremorrowDirection")
        self.direction.addItem("正+", 1)
        self.direction.addItem("负−", -1)
        saved_direction = controller.runtime.repository.load_workspace(
            f"coremorrow-e00/direction/{device_id}/{channel}", 1
        )
        self.direction.setCurrentIndex(1 if saved_direction == -1 else 0)
        self.direction.currentIndexChanged.connect(
            lambda: controller.runtime.repository.save_workspace(
                f"coremorrow-e00/direction/{device_id}/{channel}", self.direction.currentData()
            )
        )
        step_row.addWidget(self.direction)
        step_label = QLabel("步长")
        step_label.setObjectName("coremorrowControlLabel")
        step_row.addWidget(step_label)
        self.step_value = QDoubleSpinBox(self)
        self.step_value.setObjectName("coremorrowStep")
        self.step_value.setRange(0.0, self.MAX_VOLTAGE)
        self.step_value.setDecimals(4)
        self.step_value.setSingleStep(0.1)
        self.step_value.setValue(self._load_step())
        self.step_value.setSuffix(" V")
        step_row.addWidget(self.step_value)
        self.step_button = QPushButton("步进", self)
        self.step_button.setObjectName("coremorrowStepButton")
        self.step_button.clicked.connect(self.step)
        step_row.addWidget(self.step_button)
        step_row.addStretch()

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(4)
        left.addLayout(self.fields)
        left.addLayout(step_row)
        main = QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(12)
        main.addLayout(left, 1)

        actual = QVBoxLayout()
        actual.setContentsMargins(0, 0, 0, 0)
        actual.setSpacing(2)
        actual_title = QLabel("实际读回")
        actual_title.setObjectName("coremorrowActualTitle")
        actual.addWidget(actual_title, 0, Qt.AlignmentFlag.AlignHCenter)
        self.actual_display = QLCDNumber(self)
        self.actual_display.setObjectName("coremorrowActual")
        self.actual_display.setDigitCount(8)
        self.actual_display.setSegmentStyle(QLCDNumber.SegmentStyle.Flat)
        self.actual_display.setFixedWidth(145)
        self.actual_display.display("----")
        actual.addWidget(self.actual_display)
        self.actual_unit = QLabel("V")
        self.actual_unit.setObjectName("coremorrowActualUnit")
        actual.addWidget(self.actual_unit, 0, Qt.AlignmentFlag.AlignHCenter)
        self.actual_detail = QLabel("等待设备读回")
        self.actual_detail.setObjectName("coremorrowActualDetail")
        self.actual_detail.setWordWrap(True)
        actual.addWidget(self.actual_detail, 0, Qt.AlignmentFlag.AlignHCenter)
        main.addLayout(actual, 0)
        outer.insertLayout(1, main)

        self.apply_button.setText("应用修改")
        self.apply_button.setFixedWidth(100)
        editor = self.editors["voltage"]
        title_label = editor.findChild(QLabel, "controlTitle")
        if title_label is not None:
            # Writability changes after explicit stop; the channel status shows
            # the current lock, rather than retaining the construction-time label.
            title_label.setText("电压设定")
        editor.readback.hide()
        field = editor.editors["value"]
        field.input.setMinimumWidth(70)
        field.unit.setFixedWidth(58)
        self.direction.setFixedWidth(68)
        self.step_value.setFixedWidth(88)
        self.step_button.setFixedWidth(64)
        editor.editors["value"].changed.connect(self.refresh)
        self.step_value.valueChanged.connect(self._save_step)
        self.refresh()

    def _load_step(self) -> float:
        stored = self.controller.runtime.repository.load_workspace(self._step_key, 0.1)
        try:
            value = float(stored)
        except (TypeError, ValueError):
            return 0.1
        return value if math.isfinite(value) and 0 <= value <= self.MAX_VOLTAGE else 0.1

    def _save_step(self, value: float) -> None:
        if math.isfinite(value):
            self.controller.runtime.repository.save_workspace(self._step_key, float(value))

    @property
    def editor(self):
        return self.editors["voltage"]

    def _draft_voltage(self) -> float | None:
        field = self.editor.editors.get("value")
        if field is None or not field.input_is_valid():
            return None
        try:
            value = float(field.draft()["value"]) * (field.unit.currentData() or 1)
            return value if math.isfinite(value) else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _limits(self) -> tuple[float, float]:
        spec = self.voltage_cap.parameters.get("value")
        return (
            float(spec.minimum) if spec and spec.minimum is not None else self.MIN_VOLTAGE,
            float(spec.maximum) if spec and spec.maximum is not None else self.MAX_VOLTAGE,
        )

    def step(self) -> None:
        base = self._draft_voltage()
        step = float(self.step_value.value())
        low, high = self._limits()
        if base is None:
            self.message("请输入有效的电压目标后再步进")
            return
        if not math.isfinite(step) or step <= 0:
            self.message("步长必须大于 0 V")
            return
        target = base + (1 if self.direction.currentData() == 1 else -1) * step
        if target < low or target > high:
            self.message(f"步进目标 {target:.12g} V 超出范围 {low:g} ～ {high:g} V")
            return
        field = self.editor.editors["value"]
        factor = field.unit.currentData() or 1
        field.restore({"value": f"{target / factor:.12g}", "factor": factor})
        self.editor.save_draft()
        # A step is an explicit user command even when the calculated target
        # happens to equal the most recent readback.
        self.editor.dirty = True
        self.editor.status.setText("待应用")
        self.apply()

    def refresh(self) -> None:
        super().refresh()
        if not hasattr(self, "step_button"):
            return
        state = self.controller.states.get(self.device_id, {})
        frame = self.controller.runtime.sessions.readback_frames(self.device_id).get(
            (self.channel, "voltage")
        )
        health, reason = self.controller.runtime.monitoring.settings_status(
            self.device_id, ((self.channel, "voltage"),)
        )
        value = frame.reading.value if frame else None
        valid = (
            frame is not None
            and SettingsSync.current(frame, state)
            and frame.reading.quality in {Quality.GOOD, Quality.SIMULATED}
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
        if valid and frame is not None and isinstance(value, (float, int)):
            self.actual_display.display(f"{float(value):.4f}")
            self.channel_status.setText("正常" if health == "正常" else health)
            self.actual_detail.setText(f"实际读回：{float(value):.6g} V")
            self.actual_display.setToolTip(f"{frame.reading.timestamp}\n原始读回：{frame.reading.value}")
        else:
            self.actual_display.display("----")
            self.channel_status.setText(health)
            self.actual_detail.setText(reason or "等待设备读回")
            self.actual_display.setToolTip(reason)
        self.step_button.setEnabled(
            bool(
                self.editor.cap.available
                and self.editor.cap.writable
                and self._draft_voltage() is not None
                and not self.running
                and not state.get("controlPending")
                and not state.get("controlsLocked")
                and self.device_id not in self.controller.group_devices
            )
        )
        control = state.get("capabilityDetails", {}).get("control_state", {})
        if control.get("polling_paused"):
            self.channel_status.setText("读回已暂停")
            self.actual_display.display("----")
            self.actual_detail.setText(
                f"历史读回：{float(value):.4f} V"
                if isinstance(value, (float, int))
                else "波形状态未确认，暂无当前读回"
            )
            self.apply_button.setEnabled(False)
            self.step_button.setEnabled(False)


class CoreMorrowE00Page(PowerSupplyPage):
    """E00-only management page; ordinary power supplies use PowerSupplyPage."""

    def __init__(self, controller: WorkspaceController, parent=None) -> None:
        # Keep compatibility with callers that treated the management power
        # page as a PowerSupplyPage while using an independent E00 layout.
        QWidget.__init__(self, parent)
        self.controller = controller
        self.device_id = ""
        self.mode = controller.mode
        self.panels: dict[int, CoreMorrowE00Channel] = {}  # type: ignore[assignment]
        self.wave_panels: dict[int, CoreMorrowWaveformChannel] = {}
        self.control_mode = "normal"
        self.control_state: dict = {}
        self._control_version: tuple | None = None
        self.initializing = False
        self.command_pending = False
        self._seen_session = ""
        self._request_serial = 0
        self._hydrating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self.header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "power_supply", self, compact=True, surface="management")
        self.header.addWidget(self.selector, 1)
        self.connection = QLabel(self)
        self.connection.setObjectName("powerSyncState")
        self.header.addWidget(self.connection)
        layout.addLayout(self.header)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("控制模式"))
        self.mode_selector = WorkspaceComboBox()
        self.mode_selector.addItem("常规模式", "normal")
        self.mode_selector.addItem("标准波形模式", "waveform")
        self.mode_selector.setMinimumWidth(150)
        mode_row.addWidget(self.mode_selector)
        self.mode_status = QLabel("当前模式未确认")
        self.mode_status.setObjectName("mutedText")
        self.mode_status.setWordWrap(True)
        mode_row.addWidget(self.mode_status, 1)
        self.normal_button = QPushButton("停止波形并进入常规")
        self.normal_button.clicked.connect(lambda: self.command("normal_mode", 1, {}))
        mode_row.addWidget(self.normal_button)
        layout.addLayout(mode_row)
        self.mode_selector.currentIndexChanged.connect(self.change_control_mode)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll_area, 1)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 3, 0)
        self.rows.setSpacing(6)
        self.rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setWidget(self.body)
        self.empty = QLabel("请先连接芯明天 E00，再选择设备。", self)
        self.empty.setObjectName("mutedText")
        self.rows.addWidget(self.empty)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.settings_changed.connect(self.update_readings)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if (device_id, self.controller.mode) == (self.device_id, self.mode):
            return
        if self.device_id in self.controller.group_devices or self.command_pending:
            self.selector.select(self.device_id)
            return
        if not device_id and self.controller.states.get(self.device_id, {}).get("connectionState") in {
            "error",
            "degraded",
        }:
            # Keep failed-operation evidence and recovery controls visible. A new
            # connection is required before the session will permit another write.
            return
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
        self.device_id, self.mode = device_id, self.controller.mode
        self._request_serial += 1
        self._seen_session = ""
        self.initializing = False
        for panel in (*self.panels.values(), *self.wave_panels.values()):
            self.rows.removeWidget(panel)
            panel.hide()
            panel.deleteLater()
        self.panels.clear()
        self.wave_panels.clear()
        self.empty.setVisible(not device_id)
        if device_id:
            schema = self.controller.capabilities(device_id)
            voltage = next((c for c in schema.operations if c.name == "voltage"), None)
            wave_cap = next((c for c in schema.operations if c.name == "standard_waveform"), None)
            if voltage is not None:
                for channel in schema.channels:
                    panel = CoreMorrowE00Channel(
                        self.controller, device_id, channel, voltage, parent=self.body
                    )
                    self.panels[channel] = panel
                    self.rows.addWidget(panel)
                    if wave_cap is not None:
                        wave_panel = CoreMorrowWaveformChannel(self, channel, wave_cap)
                        self.wave_panels[channel] = wave_panel
                        self.rows.addWidget(wave_panel)
        self.control_state = {}
        self._control_version = None
        self._present_mode(resolve_mode({}, self._saved_mode()))
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
            editor = panel.editor
            frame = frames.get((panel.channel, "voltage"))
            if not frame or not SettingsSync.current(frame, state):
                editor._reading_valid = False
                panel.refresh()
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
        session_id = state.get("sessionId", "")
        if connected and session_id and session_id != self._seen_session and not self.initializing:
            self.request_state()
        details = state.get("capabilityDetails", {}).get("control_state")
        version = (session_id, state.get("configurationVersion"), state.get("capabilityVersion"))
        if isinstance(details, dict) and version != self._control_version:
            self.control_state = details
            self._control_version = version
        available = bool(
            connected
            and not self.initializing
            and not self.command_pending
            and not state.get("controlPending")
            and not state.get("controlsLocked")
            and self.device_id not in self.controller.group_devices
        )
        self.mode_selector.setEnabled(available)
        self.normal_button.setEnabled(available)
        self.normal_button.setToolTip("停止后恢复已保存的直流值；通信异常时须先重新连接。")
        for panel in self.wave_panels.values():
            panel.reflow(self.scroll_area.viewport().width() - 3)
            panel.update_state(self.control_state, available)

    def _saved_mode(self) -> dict:
        saved = self.controller.runtime.repository.load_workspace(f"coremorrow-e00/mode/{self.device_id}", {})
        return saved if isinstance(saved, dict) else {}

    def _save_mode(self) -> None:
        if self.device_id:
            self.controller.runtime.repository.save_workspace(
                f"coremorrow-e00/mode/{self.device_id}",
                {"schema_version": 1, "mode": self.control_mode, "updated_at": utc_now()},
            )

    def _present_mode(self, mode: str) -> None:
        self.control_mode = mode
        self._hydrating = True
        self.mode_selector.setCurrentIndex(1 if mode == "waveform" else 0)
        self._hydrating = False
        for panel in self.panels.values():
            panel.setVisible(mode == "normal")
        for wave_panel in self.wave_panels.values():
            wave_panel.reflow(self.scroll_area.viewport().width() - 3)
            wave_panel.setVisible(mode == "waveform")

    def request_state(self) -> None:
        if not self.device_id or self.initializing:
            return
        device_id = self.device_id
        session_id = self.controller.states.get(device_id, {}).get("sessionId", "")
        if not session_id:
            return
        self._seen_session = session_id
        self._request_serial += 1
        token = self._request_serial
        self.initializing = True
        self.mode_status.setText("正在读取仪器状态…")
        self.mode_selector.setEnabled(False)
        self.normal_button.setEnabled(False)
        self.body.setEnabled(False)
        revisions = {ch: panel.revision for ch, panel in self.wave_panels.items()}

        def finish(status=None, error=None):
            if not shiboken6.isValid(self) or token != self._request_serial or device_id != self.device_id:
                return
            if self.controller.states.get(device_id, {}).get("sessionId") != session_id:
                self.initializing = False
                self.body.setEnabled(True)
                return
            self.initializing = False
            self.body.setEnabled(True)
            data = status.values.get("control_state", {}) if status else {}
            self.control_state = data if isinstance(data, dict) else {}
            current = self.controller.states.get(device_id, {})
            self._control_version = (
                session_id,
                current.get("configurationVersion"),
                current.get("capabilityVersion"),
            )
            self._present_mode(resolve_mode(self.control_state, self._saved_mode()))
            for ch, panel in self.wave_panels.items():
                channel_state = self.control_state.get("channels", {}).get(str(ch), {})
                if channel_state.get("source") == "instrument" and panel.revision == revisions[ch]:
                    parameters = channel_state.get("parameters", {})
                    if isinstance(parameters, dict):
                        panel.restore(parameters, source="仪器读回")
                        panel.save()
            verified = self.control_state.get("source") == "instrument"
            self.mode_status.setText("仪器状态已读回" if verified else "上次记录 / 默认值，当前状态未确认")
            last_result = self.control_state.get("last_result", {})
            if not verified and last_result.get("outcome") in {"pending", "uncertain", "rejected", "partial"}:
                self.mode_status.setText(
                    "上次操作未完成或失败，当前状态未确认：" + str(last_result.get("error") or "执行结果未知")
                )
            if error:
                self.mode_status.setToolTip(str(error))
            self.refresh()

        try:
            self.controller.watch(
                self.controller.runtime.sessions.submit(device_id, "status"),
                lambda status: finish(status),
                lambda error: finish(error=error),
            )
        except Exception as exc:
            finish(error=exc)

    def change_control_mode(self) -> None:
        if self._hydrating or not self.device_id:
            return
        mode = self.mode_selector.currentData()
        if mode == "normal" and (
            self.control_state.get("polling_paused") or self.control_state.get("restore_available")
        ):
            self._present_mode(self.control_mode)
            self.command("normal_mode", 1, {})
            return
        self._present_mode(mode)
        self._save_mode()

    def command(self, operation: str, channel: int, parameters: dict) -> None:
        if self.command_pending or self.initializing or not self.device_id:
            return
        device_id = self.device_id
        session_id = self.controller.states.get(device_id, {}).get("sessionId")
        self.command_pending = True
        self.mode_status.setText("正在执行…")
        self.refresh()

        def complete(result=None, error=None):
            if not shiboken6.isValid(self) or self.device_id != device_id:
                return
            self.command_pending = False
            if self.controller.states.get(device_id, {}).get("sessionId") != session_id:
                return
            if error or result.outcome != "success":
                message = str(error or result.error or result.outcome)
                self.mode_status.setText("执行失败 / 状态未确认：" + message)
                if channel in self.wave_panels:
                    self.wave_panels[channel].last_error = message
            elif operation == "normal_mode":
                # The device is now back on its restored DC output, while the
                # user is returned to the waveform editor so another explicit
                # waveform command does not require a second mode click.
                self._present_mode("waveform")
                self._save_mode()
                restored = result.adaptation.get("restored", False)
                self.mode_status.setText(
                    (
                        "直流值已恢复并读回核实；已返回标准波形模式"
                        if restored
                        else "停止命令已发送；无原直流快照，请明确设定电压；已返回标准波形模式"
                    )
                )
            else:
                if channel in self.wave_panels:
                    self.wave_panels[channel].last_error = ""
                self._save_mode()
                self.mode_status.setText("命令已发送；实际输出未独立确认")
            self.controller.poll()
            self.update_readings(device_id)
            self.refresh()

        try:
            self.controller.apply(
                device_id,
                DeviceCommand(operation, parameters, channel, True),
                lambda result: complete(result),
                lambda error: complete(error=error),
            )
        except Exception as exc:
            complete(error=exc)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.controller.focus_settings(self.device_id)
        self.update_readings(self.device_id)
        self.refresh()
        if self.device_id and not self.initializing:
            self.request_state()

    def hideEvent(self, event) -> None:  # noqa: N802
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")
        super().hideEvent(event)
