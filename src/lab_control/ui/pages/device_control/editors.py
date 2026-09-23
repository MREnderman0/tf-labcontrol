from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
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
from lab_control.domain.instruments import Parameter, ValidationError

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
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.unit = QComboBox()
        self.unit.setFixedWidth(100)
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
        layout.addWidget(self.input, 1)
        if spec.kind in {"number", "integer"}:
            for label, factor in UNIT_CHOICES.get(spec.unit, ((spec.unit or "数值", 1),)):
                self.unit.addItem(label, factor)
            self.unit.setCurrentIndex(max(0, self.unit.findData(1)))
            self.unit.currentIndexChanged.connect(self.changed)
            layout.addWidget(self.unit)
        self.setToolTip(
            f"有效范围：{spec.minimum if spec.minimum is not None else '不限'} ～ "
            f"{spec.maximum if spec.maximum is not None else '不限'} {spec.unit}"
        )

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
        else:
            value = self.input.text().strip()
        return self.spec.validate("value", value)

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

    def __init__(self, names: tuple[str, ...], parent=None) -> None:
        super().__init__(parent)
        self.names = names
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
            parameter.validate(name, [row[index] for row in rows])
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
