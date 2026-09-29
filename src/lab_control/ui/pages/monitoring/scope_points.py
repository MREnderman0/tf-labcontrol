"""Capability-driven acquisition-length selector; programmatic refresh never writes."""

import math

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QInputDialog

from lab_control.domain.instruments import DeviceError, Parameter
from lab_control.domain.oscilloscope import engineering
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class ScopePoints(WorkspaceComboBox):
    committed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.actual = None
        self.spec = None
        self.signature = None
        self.activated.connect(self.choose)

    def configure(self, actual: int, spec: Parameter) -> None:
        signature = (actual, spec.minimum, spec.maximum, spec.choices)
        if signature == self.signature or self.view().isVisible():
            return
        self.actual, self.spec, self.signature = actual, spec, signature
        candidates = (
            [float(v) for v in spec.choices]
            if spec.choices
            else [1000.0, 10000.0, 100000.0, 1000000.0, 5000000.0, 10000000.0]
        )
        if not spec.choices:
            if spec.minimum is not None:
                candidates.append(math.ceil(spec.minimum))
            if spec.maximum is not None:
                candidates.append(math.floor(spec.maximum))
        values = set()
        for item in candidates:
            try:
                value = int(float(item))
                spec.validate("record_length", value)
                values.add(value)
            except (DeviceError, ValueError, OverflowError):
                continue
        self.blockSignals(True)
        self.clear()
        for value in sorted(values):
            self.addItem(engineering(value, "").strip(), value)
        if actual not in values:
            try:
                spec.validate("record_length", actual)
                suffix = "（当前）"
            except DeviceError:
                suffix = "（超出范围）"
            self.addItem(engineering(actual, "").strip() + suffix, actual)
        if not spec.choices:
            self.addItem("自定义…", "custom")
        self.setCurrentIndex(self.findData(actual))
        self.blockSignals(False)

    def choose(self, index: int) -> None:
        if self.spec is None or self.actual is None:
            return
        value = self.itemData(index)
        if value == "custom":
            value, accepted = self.custom_value()
            self.setCurrentIndex(self.findData(self.actual))
            if not accepted:
                return
        self.committed.emit(int(value))

    def custom_value(self) -> tuple[int, bool]:
        assert self.spec is not None and self.actual is not None
        low = max(1, math.ceil(self.spec.minimum or 1))
        high = min(2_147_483_647, math.floor(self.spec.maximum or 2_147_483_647))
        dialog = QInputDialog(self.window())
        dialog.setObjectName("sourceSettingsDialog")
        dialog.setWindowTitle("自定义采集点数")
        dialog.setMinimumWidth(320)
        dialog.setLabelText(f"仪器采集长度（点）\n可设范围：{low:,}～{high:,}")
        dialog.setInputMode(QInputDialog.InputMode.IntInput)
        dialog.setIntRange(low, high)
        dialog.setIntValue(min(high, max(low, self.actual)))
        dialog.setOkButtonText("应用")
        dialog.setCancelButtonText("取消")
        accepted = dialog.exec() == QInputDialog.DialogCode.Accepted
        value = dialog.intValue()
        dialog.deleteLater()
        return value, accepted
