"""Compact E00 waveform editor and presentation-only state resolution."""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QLineEdit, QPushButton

from lab_control.domain.instruments import Capability, DeviceError, ValidationError, utc_now
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


def resolve_mode(state: dict, stored: dict) -> str:
    """Only explicit instrument mode wins over saved presentation/command records."""
    channels = list(state.get("channels", {}).values())
    verified = [ch for ch in channels if ch.get("source") == "instrument"]
    if any(ch.get("mode") == "waveform" or ch.get("running") is True for ch in verified):
        return "waveform"
    if state.get("source") == "instrument" and state.get("mode") in {"normal", "waveform"}:
        return state["mode"]
    if len(channels) >= 3 and len(channels) == len(verified) and all(ch.get("mode") == "normal" for ch in verified):
        return "normal"
    # A shutdown restoration command is not a new user page preference.
    # Neither command timestamps nor successful transport prove current mode.
    if stored.get("mode") in {"normal", "waveform"}:
        return str(stored["mode"])
    if state.get("last_mode") in {"normal", "waveform"}:
        return state["last_mode"]
    return "normal"


class CoreMorrowWaveformChannel(QFrame):
    def __init__(self, page: Any, channel: int, capability: Capability) -> None:
        super().__init__(page.body)
        self.page, self.channel, self.capability = page, channel, capability
        self.key = f"coremorrow-e00/waveform/{page.device_id}/{channel}"
        self.repository = page.controller.runtime.repository
        self.setObjectName("coremorrowChannel")
        self.setProperty("waveform", True)
        self.revision = 0
        self.hydrating = True
        self.busy = False
        self.last_error = ""
        self.compact: bool | None = None
        grid = QGridLayout(self)
        self.grid = grid
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        title = QLabel(f"CH {channel}")
        title.setObjectName("coremorrowChannelTitle")
        grid.addWidget(title, 1, 0)
        self.waveform = WorkspaceComboBox()
        for text, value in (("正弦波", "SIN"), ("方波", "SQUARE"), ("三角波", "TRIANGLE"), ("锯齿波", "SAWTOOTH")):
            self.waveform.addItem(text, value)
        self.inputs: dict[str, QLineEdit] = {}
        self.parameter_sources = dict.fromkeys(capability.parameters, "默认草稿，当前未确认")
        grid.addWidget(QLabel("波形"), 0, 1)
        grid.addWidget(self.waveform, 1, 1)
        for column, (name, label, default) in enumerate(
            (("frequency", "频率 / Hz", "1"), ("amplitude", "峰峰值 / V", "0.2"), ("offset", "偏移 / V", "0.1")), 2
        ):
            field = QLineEdit(default)
            field.setObjectName("sourceValue")
            field.setMinimumWidth(55)
            field.setMaximumWidth(180)
            spec = capability.parameters[name]
            field.setToolTip(f"{spec.minimum:g} ～ {spec.maximum:g} {spec.unit}")
            self.inputs[name] = field
            grid.addWidget(QLabel(label), 0, column)
            grid.addWidget(field, 1, column)
            grid.setColumnStretch(column, 1)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primaryButton")
        self.stop_button = QPushButton("停止")
        grid.addWidget(self.send_button, 1, 5)
        grid.addWidget(self.stop_button, 1, 6)
        self.status = QLabel("上次记录，当前状态未确认")
        self.status.setObjectName("coremorrowWaveStatus")
        self.status.setWordWrap(True)
        grid.addWidget(self.status, 2, 1, 1, 6)
        self.envelope = QLabel("输出范围：偏移 ± 峰峰值/2，须在 0 ～ 120 V 内")
        self.envelope.setObjectName("mutedText")
        self.envelope.setWordWrap(True)
        grid.addWidget(self.envelope, 3, 1, 1, 6)
        stored = self.repository.load_workspace(self.key, {})
        self.restore(stored.get("parameters", {}) if isinstance(stored, dict) else {}, source="上次记录，当前未确认")
        self.hydrating = False
        self.waveform.currentIndexChanged.connect(lambda: self.edited("waveform"))
        for name, field in self.inputs.items():
            field.textChanged.connect(lambda _text, name=name: self.edited(name))
        self.send_button.clicked.connect(self.send)
        self.stop_button.clicked.connect(lambda: page.command("waveform_stop", channel, {}))

    def reflow(self, available_width: int) -> None:
        compact = available_width < 660
        if compact == self.compact:
            return
        self.compact = compact
        for widget in (self.send_button, self.stop_button, self.status, self.envelope):
            self.grid.removeWidget(widget)
        self.grid.addWidget(self.send_button, 3 if compact else 1, 3 if compact else 5)
        self.grid.addWidget(self.stop_button, 3 if compact else 1, 4 if compact else 6)
        self.grid.addWidget(self.status, 2, 1, 1, 4 if compact else 6)
        self.grid.addWidget(self.envelope, 4 if compact else 3, 1, 1, 4 if compact else 6)

    def restore(self, parameters: dict, source: str = "上次记录，当前未确认") -> None:
        self.hydrating = True
        index = self.waveform.findData(parameters.get("waveform"))
        if index >= 0:
            self.waveform.setCurrentIndex(index)
            self.parameter_sources["waveform"] = source
        for name, field in self.inputs.items():
            value = parameters.get(name)
            if type(value) in (str, float, int):
                field.setText(str(value))
                self.parameter_sources[name] = source
        self.hydrating = False
        self.update_sources()

    def update_sources(self) -> None:
        self.waveform.setToolTip(self.parameter_sources["waveform"])
        for name, field in self.inputs.items():
            spec = self.capability.parameters[name]
            field.setToolTip(f"{spec.minimum:g} ～ {spec.maximum:g} {spec.unit}\n{self.parameter_sources[name]}")

    def edited(self, name: str) -> None:
        if self.hydrating:
            return
        self.parameter_sources[name] = "用户草稿"
        self.update_sources()
        self.save()

    def save(self) -> None:
        if self.hydrating:
            return
        self.revision += 1
        self.repository.save_workspace(self.key, {
            "schema_version": 1, "updated_at": utc_now(),
            "parameters": {"waveform": self.waveform.currentData(), **{k: f.text() for k, f in self.inputs.items()}},
        })

    def send(self) -> None:
        try:
            values = {"waveform": self.waveform.currentData(), **{k: float(f.text()) for k, f in self.inputs.items()}}
            values = self.capability.validate(values)
            if any(not math.isfinite(values[k]) for k in self.inputs):
                raise ValidationError("请输入有效数值")
            if values["offset"] - values["amplitude"] / 2 < 0 or values["offset"] + values["amplitude"] / 2 > 120:
                raise ValidationError("波形上下沿必须在 0 ～ 120 V 内（偏移 ± 峰峰值/2）")
            self.last_error = ""
            self.save()
            self.page.command("standard_waveform", self.channel, values)
        except (ValueError, DeviceError) as exc:
            self.last_error = f"未发送：{exc}"
            self.status.setText(self.last_error)

    def update_state(self, state: dict, available: bool) -> None:
        channel = state.get("channels", {}).get(str(self.channel), {})
        source = channel.get("source")
        running = channel.get("running") if source == "instrument" else channel.get("commanded_running")
        if source == "instrument":
            text = "仪器读回：波形运行中" if running is True else "仪器读回：已停止" if running is False else "仪器运行状态未确认"
            if any(value != "仪器读回" for value in self.parameter_sources.values()):
                text += "；未读回参数沿用记录 / 草稿（悬停查看来源）"
        else:
            text = (
                "波形命令已发送；实际输出未独立确认" if running is True else
                "停止命令已发送；实际输出未独立确认" if running is False else
                "上次记录，当前状态未确认；发送前请先停止波形"
            )
            last = channel.get("last_command", {}).get("last_result", {})
            if running is None and last.get("outcome") in {"pending", "uncertain", "rejected", "partial"}:
                text = "上次操作未完成或失败，当前状态未确认：" + str(last.get("error") or "执行结果未知")
        self.status.setText(self.last_error or text)
        self.send_button.setEnabled(available and running is False)
        self.stop_button.setEnabled(available)
        self.waveform.setEnabled(available and running is not True)
        for field in self.inputs.values():
            field.setEnabled(available and running is not True)
