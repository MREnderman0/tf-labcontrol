"""Frequency controls with explicit automatic/manual state and actual readback."""

import math
from typing import Any

from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from lab_control.domain.instruments import DeviceCommand
from lab_control.domain.readback import usable_reading


class FrequencySetting(QFrame):
    def __init__(self, controller, device_id, operation, label):
        super().__init__()
        self.controller, self.device_id, self.operation = controller, device_id, operation
        self.busy = self.dirty = False
        self.token = None
        self.setObjectName("phaseFrequency")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.addWidget(QLabel(label))
        self.auto = QCheckBox("自动识别")
        self.auto.setChecked(True)
        self.value = QDoubleSpinBox()
        spec = controller.capabilities(device_id).get(operation).parameters["value"]
        self.value.setRange(spec.minimum / 1e6, spec.maximum / 1e6)
        self.value.setDecimals(6)
        self.value.setValue(10)
        self.value.setSuffix(" MHz")
        self.value.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.auto)
        row.addWidget(self.value, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.readback = QLabel("待读回")
        self.readback.setWordWrap(True)
        self.readback.setObjectName("mutedText")
        self.apply_button = QPushButton("应用")
        self.apply_button.setProperty("buttonSize", "compact")
        row.addWidget(self.readback, 1)
        row.addWidget(self.apply_button)
        layout.addLayout(row)
        self.auto.toggled.connect(self.changed)
        self.value.valueChanged.connect(self.changed)
        self.apply_button.clicked.connect(self.apply)
        controller.settings_changed.connect(self.read)

    def changed(self, *_):
        self.dirty = True
        self.value.setEnabled(not self.auto.isChecked())
        self.readback.setText("待应用")

    def apply(self):
        parameters: dict[str, Any] = {"automatic": self.auto.isChecked()}
        if not self.auto.isChecked():
            parameters["value"] = self.value.value() * 1e6
        self.busy = True
        self.apply_button.setEnabled(False)
        try:
            self.controller.apply(
                self.device_id, DeviceCommand(self.operation, parameters), self.applied, self.failed
            )
        except Exception as exc:
            self.failed(exc)

    def applied(self, result):
        self.busy = False
        self.apply_button.setEnabled(True)
        if result.outcome != "success":
            self.failed(result.error or "设置读回未确认")
            return
        self.dirty = False
        self.show_readback(result.readback)

    def failed(self, error):
        self.busy = False
        self.apply_button.setEnabled(True)
        self.readback.setText("未确认：" + str(error))

    def read(self, key):
        if key != self.device_id or self.busy:
            return
        frame = self.controller.runtime.sessions.readback_frames(key).get((1, self.operation))
        if frame is not None and frame.sequence != self.token:
            self.token = frame.sequence
            spec = self.controller.capabilities(key).get(self.operation)
            if usable_reading(frame.reading, spec.parameters):
                self.show_readback(frame.reading.value)
            else:
                self.readback.setText("读回未知")

    def show_readback(self, value):
        if not isinstance(value, dict) or type(value.get("automatic")) is not bool:
            self.readback.setText("读回未知")
            return
        automatic, frequency = value["automatic"], value.get("value")
        mhz = float(frequency) / 1e6 if isinstance(frequency, (int, float)) else None
        if not automatic and (not isinstance(frequency, (int, float)) or not math.isfinite(frequency)):
            self.readback.setText("读回未知")
            return
        if not self.dirty:
            self.auto.blockSignals(True)
            self.value.blockSignals(True)
            self.auto.setChecked(automatic)
            if not automatic:
                assert mhz is not None
                self.value.setValue(mhz)
            self.value.setEnabled(not automatic)
            self.auto.blockSignals(False)
            self.value.blockSignals(False)
        self.readback.setText(
            ("待应用 · " if self.dirty else "当前：") + ("自动" if automatic else f"{mhz:g} MHz")
        )
