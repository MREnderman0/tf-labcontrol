from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.controls import PARAMETER_LABELS
from lab_control.domain.instruments import DeviceError, Parameter, ValidationError

UNIT_CHOICES = {
    "Hz": (("Hz", 1), ("kHz", 1e3), ("MHz", 1e6), ("GHz", 1e9)),
    "V": (("µV", 1e-6), ("mV", 1e-3), ("V", 1)),
    "Vpp": (("mVpp", 1e-3), ("Vpp", 1)),
    "V/div": (("mV/div", 1e-3), ("V/div", 1)),
    "A": (("µA", 1e-6), ("mA", 1e-3), ("A", 1)),
    "W": (("nW", 1e-9), ("µW", 1e-6), ("mW", 1e-3), ("W", 1)),
    "s": (("ns", 1e-9), ("µs", 1e-6), ("ms", 1e-3), ("s", 1)),
}


class ParameterEditor(QWidget):
    changed = Signal()

    def __init__(self, spec: Parameter, parent=None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.feedback: QLabel
        self._editing_number = False
        self._last_factor = 1.0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(layout)
        self.unit = QComboBox()
        self.unit.setFixedWidth(100)
        self.unit.installEventFilter(self)
        self.input: QCheckBox | QComboBox | QLineEdit
        if spec.kind == "boolean":
            self.input = QCheckBox("开启")
            self.input.stateChanged.connect(self.changed)
        elif spec.kind == "enum":
            self.input = QComboBox()
            for value in spec.choices:
                self.input.addItem(value, value)
            self.input.currentIndexChanged.connect(self.changed)
        else:
            self.input = QLineEdit()
            self.input.setPlaceholderText("输入目标值" if spec.kind == "text" else "例如 1e3")
            self.input.textEdited.connect(self.changed)
            self.input.textEdited.connect(lambda: self.feedback.clear())
            self.input.textChanged.connect(self.number_edited)
            self.input.editingFinished.connect(lambda: QTimer.singleShot(0, self.finish_editing))
        layout.addWidget(self.input, 1)
        if spec.kind in {"number", "integer"}:
            for label, factor in UNIT_CHOICES.get(spec.unit, ((spec.unit or "数值", 1),)):
                self.unit.addItem(label, factor)
            self.unit.setCurrentIndex(max(0, self.unit.findData(1)))
            self.unit.currentIndexChanged.connect(self.unit_changed)
            layout.addWidget(self.unit)
        self.range_label = QLabel()
        self.range_label.setObjectName("rangeHint")
        self.range_label.setWordWrap(True)
        self.feedback = QLabel()
        self.feedback.setObjectName("inputFeedback")
        self.feedback.setWordWrap(True)
        outer.addWidget(self.range_label)
        outer.addWidget(self.feedback)
        self.update_range_label()

    def update_range_label(self) -> None:
        numeric = self.spec.kind in {"number", "integer"}
        self.range_label.setVisible(numeric)
        if not numeric:
            return
        factor = self.unit.currentData() or 1
        low = f"{self.spec.minimum / factor:.12g}" if self.spec.minimum is not None else "下限未确认"
        high = f"{self.spec.maximum / factor:.12g}" if self.spec.maximum is not None else "上限未确认"
        source = {
            "model": "型号范围",
            "model_channel": "型号 / 通道范围",
            "instrument": "实机范围",
            "experiment": "实验限制",
            "unavailable": "未确认 / 已过期",
        }.get(self.spec.range_source, self.spec.range_source)
        readable = ""
        if (
            self.spec.unit == "Hz"
            and factor == 1
            and self.spec.minimum is not None
            and self.spec.maximum is not None
        ):

            def frequency_label(value: float) -> str:
                label, scale = next(
                    ((label, scale) for label, scale in reversed(UNIT_CHOICES["Hz"]) if abs(value) >= scale),
                    ("Hz", 1),
                )
                return f"{value / scale:.12g} {label}"

            readable = f"（{frequency_label(self.spec.minimum)} ～ {frequency_label(self.spec.maximum)}）"
        self.range_label.setText(
            f"可输入范围：{low} ～ {high} {self.unit.currentText()}{readable} · {source}"
        )
        self.setToolTip(self.range_label.text())

    def set_spec(self, spec: Parameter) -> None:
        if self.spec == spec:
            return
        self.spec = spec
        self.update_range_label()
        if not self.input.hasFocus() and not self.unit.hasFocus() and not self.unit.view().isVisible():
            self.finish_editing()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.unit and event.type() == QEvent.Type.FocusOut:
            QTimer.singleShot(0, self.finish_editing)
        return super().eventFilter(watched, event)

    def unit_changed(self) -> None:
        factor = self.unit.currentData() or 1
        if not self._editing_number and isinstance(self.input, QLineEdit):
            try:
                old_value = float(self.input.text())
                if math.isfinite(old_value):
                    self.input.setText(f"{old_value * self._last_factor / factor:.12g}")
            except ValueError:
                pass
        self._last_factor = factor
        self.update_range_label()
        # The value belongs to the newly selected unit. Do not clamp on the old-unit focus-out.
        self.finish_editing(force=True)
        self.changed.emit()

    def number_edited(self) -> None:
        self._editing_number = True

    def finish_editing(self, *, force: bool = False) -> None:
        if self.spec.kind not in {"number", "integer"}:
            return
        focused = QApplication.focusWidget()
        if not force and (focused is self.unit or self.unit.view().isVisible()):
            return
        if isinstance(self.input, QLineEdit) and not self.input.text().strip():
            self.feedback.setText("请输入有效数值")
            return
        try:
            self.value()
        except DeviceError as exc:
            self.feedback.setText(str(exc))

    def value(self) -> Any:
        if isinstance(self.input, QCheckBox):
            value: Any = self.input.isChecked()
        elif isinstance(self.input, QComboBox):
            value = self.input.currentData()
        elif self.spec.kind in {"number", "integer"}:
            try:
                value = float(self.input.text()) * (self.unit.currentData() or 1)
            except ValueError as exc:
                raise ValidationError("请输入有效数值，可使用科学计数法") from exc
            if not math.isfinite(value):
                raise ValidationError("请输入有限数值，不接受 NaN 或无穷大")
            if self.spec.kind == "integer" and not value.is_integer():
                raise ValidationError("请输入整数")
            low, high = self.spec.minimum, self.spec.maximum
            if low is not None and high is not None and low > high:
                raise ValidationError("当前限制没有可用输入范围")
            bounded = max(low, value) if low is not None else value
            bounded = min(high, bounded) if high is not None else bounded
            if bounded != value:
                displayed = bounded / (self.unit.currentData() or 1)
                self.input.setText(f"{displayed:.12g}")
                boundary = "上限" if bounded < value else "下限"
                self.feedback.setText(f"已调整至{boundary} {displayed:.12g} {self.unit.currentText()}")
                self.changed.emit()
            value = bounded
        else:
            value = self.input.text().strip()
        result = self.spec.validate("value", value)
        self._editing_number = False
        return result

    def draft(self) -> dict[str, Any]:
        value = (
            self.input.isChecked()
            if isinstance(self.input, QCheckBox)
            else self.input.currentData()
            if isinstance(self.input, QComboBox)
            else self.input.text()
        )
        return {"value": value, "factor": self.unit.currentData() or 1}

    def restore(self, draft: dict[str, Any]) -> None:
        value = draft.get("value")
        self.input.blockSignals(True)
        self.unit.blockSignals(True)
        if isinstance(self.input, QCheckBox):
            self.input.setChecked(bool(value))
        elif isinstance(self.input, QComboBox):
            index = self.input.findData(value)
            if index >= 0:
                self.input.setCurrentIndex(index)
        else:
            self.input.setText("" if value is None else str(value))
        self.unit.setCurrentIndex(max(0, self.unit.findData(draft.get("factor", 1))))
        self.input.blockSignals(False)
        self.unit.blockSignals(False)
        self._last_factor = self.unit.currentData() or 1
        self._editing_number = False
        self.update_range_label()

    def set_value(self, value: Any) -> None:
        if self.spec.kind == "boolean":
            if isinstance(value, str):
                normalized = value.strip().upper()
                if normalized not in {"0", "1", "OFF", "ON", "FALSE", "TRUE"}:
                    return
                value = normalized in {"1", "ON", "TRUE"}
            elif value not in (0, 1, False, True):
                return
        if self.spec.kind in {"number", "integer"} and isinstance(value, (int, float)):
            self.restore({"value": f"{value:.12g}", "factor": 1})
        else:
            self.restore({"value": value, "factor": 1})


class ArrayModel(QAbstractTableModel):
    edited = Signal()
    corrected = Signal(str)

    def __init__(self, names: tuple[str, ...], parent=None) -> None:
        super().__init__(parent)
        self.names = names
        self.parameters: dict[str, Parameter] = {}
        self.rows: list[list[float]] = []

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(self.names)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole}:
            return str(self.rows[index.row()][index.column()])
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                PARAMETER_LABELS.get(self.names[section], self.names[section])
                if orientation == Qt.Orientation.Horizontal
                else section + 1
            )
        return None

    def flags(self, index):
        return super().flags(index) | Qt.ItemFlag.ItemIsEditable

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        try:
            number = float(value)
            if not math.isfinite(number):
                return False
            spec = self.parameters.get(self.names[index.column()])
            if spec:
                if spec.minimum is not None and spec.maximum is not None and spec.minimum > spec.maximum:
                    return False
                bounded = max(spec.minimum, number) if spec.minimum is not None else number
                bounded = min(spec.maximum, bounded) if spec.maximum is not None else bounded
                if bounded != number:
                    self.corrected.emit(
                        f"第 {index.row() + 1} 行已调整至{'上限' if bounded < number else '下限'} {bounded:g}"
                    )
                number = bounded
            self.rows[index.row()][index.column()] = number
        except (ValueError, IndexError):
            return False
        self.dataChanged.emit(index, index)
        self.edited.emit()
        return True

    def replace_rows(self, rows: list[list[float]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()
        self.edited.emit()


class ArrayEditor(QWidget):
    changed = Signal()

    def __init__(self, parameters: dict[str, Parameter], parent=None) -> None:
        super().__init__(parent)
        self.parameters = parameters
        self.path = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        for label, action in (
            ("导入 CSV / TXT", self.choose_file),
            ("添加行", self.add_row),
            ("删除选中行", self.remove_row),
        ):
            button = QPushButton(label)
            button.clicked.connect(action)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.model = ArrayModel(tuple(parameters), self)
        self.model.parameters = parameters
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(210)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.preview = pg.PlotWidget(background="#151c1f")
        self.preview.setMinimumHeight(140)
        self.preview.setMaximumHeight(180)
        self.preview.setLabel("bottom", "数据行 / 样点")
        self.preview.setTitle("导入数据预览 · 非实测输出", size="10pt")
        layout.addWidget(self.preview)
        self.info = QLabel("支持纯数值或带一行表头的 UTF-8 CSV；列顺序与表格一致。")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.model.edited.connect(self._updated)
        self.model.corrected.connect(lambda text: QTimer.singleShot(0, lambda: self.info.setText(text)))
        self.range_label = QLabel()
        self.range_label.setObjectName("rangeHint")
        self.range_label.setWordWrap(True)
        layout.addWidget(self.range_label)
        self.set_parameters(parameters)

    def set_parameters(self, parameters: dict[str, Parameter]) -> None:
        self.parameters = parameters
        self.model.parameters = parameters
        self.range_label.setText(
            "可输入范围："
            + "；".join(
                f"{PARAMETER_LABELS.get(name, name)}：{spec.minimum if spec.minimum is not None else '下限未确认'} ～ "
                f"{spec.maximum if spec.maximum is not None else '上限未确认'} {spec.unit} · "
                + {"model": "型号范围", "instrument": "实机范围", "experiment": "实验限制"}.get(
                    spec.range_source, spec.range_source
                )
                for name, spec in parameters.items()
            )
        )
        # Imported experiments are validated as a batch; never rewrite their rows silently.
        if not self.path:
            self.finish_manual_rows()

    def finish_manual_rows(self) -> None:
        if self.path:
            return
        current = self.table.currentIndex()
        editing = self.table.state() == self.table.State.EditingState
        for row, values in enumerate(self.model.rows):
            for column, value in enumerate(values):
                if editing and current.row() == row and current.column() == column:
                    continue
                spec = self.parameters[self.model.names[column]]
                if (spec.minimum is not None and value < spec.minimum) or (
                    spec.maximum is not None and value > spec.maximum
                ):
                    self.model.setData(self.model.index(row, column), value)

    def _updated(self) -> None:
        self.preview.clear()
        if self.model.rows:
            data = np.asarray(self.model.rows)
            step = max(1, len(data) // 2000)
            for index in range(len(self.parameters)):
                self.preview.plot(np.arange(0, len(data), step), data[::step, index], pen=pg.intColor(index))
        self.info.setText(f"{len(self.model.rows)} 行 · {self.path or '手动编辑'}")
        self.changed.emit()

    def load_file(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8-sig")
        rows = []
        for index, row in enumerate(csv.reader(text.splitlines())):
            if not row or not any(item.strip() for item in row):
                continue
            if len(row) == 1 and len(self.parameters) > 1:
                row = row[0].split()
            try:
                values = [float(item.strip()) for item in row]
            except ValueError:
                if index == 0:
                    continue
                raise ValidationError(f"文件第 {index + 1} 行不是数值") from None
            if len(values) != len(self.parameters) or not all(math.isfinite(v) for v in values):
                raise ValidationError(f"文件第 {index + 1} 行的列数或数值无效")
            rows.append(values)
        for index, (name, parameter) in enumerate(self.parameters.items()):
            for line, numeric_row in enumerate(rows, start=1):
                try:
                    parameter.validate(name, [numeric_row[index]])
                except DeviceError as exc:
                    raise ValidationError(f"数据第 {line} 行 {name}：{exc}；未修改导入数据") from exc
        self.path = str(path)
        self.model.replace_rows(rows)

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入数组数据", "", "数值文件 (*.csv *.txt)")
        if path:
            try:
                self.load_file(Path(path))
            except Exception as exc:
                self.info.setText(str(exc))

    def add_row(self) -> None:
        self.model.replace_rows([*self.model.rows, [0.0] * len(self.parameters)])

    def remove_row(self) -> None:
        row = self.table.currentIndex().row()
        if 0 <= row < len(self.model.rows):
            self.model.replace_rows(self.model.rows[:row] + self.model.rows[row + 1 :])

    def values(self) -> dict[str, Any]:
        return {
            name: parameter.validate(name, [row[index] for row in self.model.rows])
            for index, (name, parameter) in enumerate(self.parameters.items())
        }
