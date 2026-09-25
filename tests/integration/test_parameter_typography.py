import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QStyle, QStyleOptionComboBox, QVBoxLayout

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import Capability, Parameter
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.editors import UNIT_CHOICES, ParameterEditor
from lab_control.ui.pages.device_control.form import OperationEditor


@pytest.fixture
def numeric(qapp, qtbot):
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    parent = QFrame()
    parent.setObjectName("compactOperation")
    parent.resize(440, 110)
    qtbot.addWidget(parent)
    layout = QVBoxLayout(parent)
    editor = ParameterEditor(Parameter(unit="Vpp", minimum=0, maximum=10), parent, label="幅度")
    editor.input.setObjectName("sourceValue")
    layout.addWidget(editor)
    parent.show()
    editor.input.setFocus()
    qtbot.wait(20)
    yield editor


def test_typing_clear_and_undo_keep_height_cursor_and_typography(numeric, qtbot):
    field = numeric.input
    height, unit_height = field.height(), numeric.unit.height()
    assert field.placeholderText() == "请输入幅度"
    assert field.font().pixelSize() == 13
    assert numeric.unit.font().pixelSize() == 13
    assert height == unit_height
    qtbot.keyClicks(field, "0.25")
    qtbot.wait(10)
    assert field.font().pixelSize() == 16
    assert field.font().weight() == QFont.Weight.Normal
    assert field.cursorPosition() == 4
    assert (field.height(), numeric.unit.height()) == (height, unit_height)
    field.selectAll()
    qtbot.keyClick(field, Qt.Key.Key_Backspace)
    assert field.text() == ""
    assert field.font().pixelSize() == 13
    qtbot.keyClick(field, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert field.text() == "0.25"
    assert field.font().pixelSize() == 16
    assert field.height() == height
    assert numeric.value() == 0.25


def test_readback_and_blocked_draft_restore_refresh_style_without_edits(numeric, qtbot):
    edits = []
    numeric.changed.connect(lambda: edits.append(numeric.draft()))
    height = numeric.input.height()
    numeric.set_value(0)
    assert numeric.input.text() == "0"
    assert numeric.input.font().pixelSize() == 16
    numeric.restore({"value": None, "factor": 1})
    assert numeric.input.font().pixelSize() == 13
    numeric.restore({"value": "500", "factor": 1e-3})
    assert numeric.input.font().pixelSize() == 16
    assert numeric.value() == 0.5
    qtbot.wait(20)
    assert numeric.input.height() == height
    assert edits == []


@pytest.mark.parametrize("unit", [*UNIT_CHOICES, "mV RMS/√Hz"])
def test_longest_unit_fits_closed_and_open_at_small_width(numeric, qtbot, unit):
    parent = numeric.parentWidget()
    editor = ParameterEditor(Parameter(unit=unit), parent)
    parent.layout().addWidget(editor)
    parent.resize(260, 180)
    editor.show()
    qtbot.wait(30)
    combo = editor.unit
    for stylesheet in ("", "QComboBox { font-size: 20px; padding-left: 12px; padding-right: 12px; }"):
        combo.setStyleSheet(stylesheet)
        qtbot.wait(30)
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        rect = combo.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, combo
        )
        assert (
            rect.width()
            >= max(combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count())) + 4
        )
        assert editor.input.width() > 0
        combo.showPopup()
        qtbot.wait(10)
        view = combo.view()
        assert (
            view.viewport().width()
            >= max(view.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count())) + 40
        )
        combo.hidePopup()


@pytest.mark.parametrize("kind,hint", [("number", "数值"), ("integer", "整数"), ("text", "内容")])
def test_unnamed_parameter_has_no_example_number(qtbot, kind, hint):
    editor = ParameterEditor(Parameter(kind))
    qtbot.addWidget(editor)
    assert editor.input.placeholderText() == "请输入" + hint


def test_operation_and_field_names_are_reused_without_device_specific_rules(qtbot, tmp_path):
    parent = QFrame()
    qtbot.addWidget(parent)
    with ApplicationRuntime((), root=tmp_path, hardware=False) as runtime:
        key = runtime.add(ConnectionConfig("mock://typography", "rs.sma100b", "mock"))
        controller = WorkspaceController(runtime, parent)
        try:
            for operation, expected in (
                ("frequency", "频率"),
                ("power", "功率"),
                ("sweep_start", "扫频起始频率"),
            ):
                editor = OperationEditor(
                    controller, key, Capability(operation, {"value": Parameter()}), 1, parent
                )
                assert editor.editors["value"].input.placeholderText() == "请输入" + expected
            multi = OperationEditor(
                controller,
                key,
                Capability("ui_fixture", {"name": Parameter("text"), "duration": Parameter()}),
                1,
                parent,
            )
            assert multi.editors["name"].input.placeholderText() == "请输入文件 / 列表名称"
            assert multi.editors["duration"].input.placeholderText() == "请输入时长 / s"
        finally:
            controller.close()
