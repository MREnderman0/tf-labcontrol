"""Wheel gestures scroll pages without mutating focused or unfocused inputs."""

import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDial,
    QDialog,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.input_wheel_guard import install_input_wheel_guard
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


def wheel(widget, *, pixel=0, modifiers=Qt.KeyboardModifier.NoModifier):
    pos = widget.rect().center()
    event = QWheelEvent(
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        QPoint(0, pixel),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.ScrollUpdate if pixel else Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)
    return event


def configure(widget):
    if isinstance(widget, QComboBox):
        widget.addItems(["first", "second", "third"])
        widget.setCurrentIndex(1)
        return widget.currentIndex, widget.currentIndexChanged
    if isinstance(widget, QDateEdit):
        widget.setDate(QDate(2026, 1, 15))
        return widget.date, widget.dateChanged
    if isinstance(widget, QLineEdit):
        widget.setText("unchanged")
        return widget.text, widget.textChanged
    widget.setRange(0, 100)
    widget.setValue(50)
    return widget.value, widget.valueChanged


@pytest.mark.parametrize(
    "kind", [QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox, WorkspaceComboBox, QSlider, QDial, QLineEdit]
)
@pytest.mark.parametrize("focused", [False, True])
def test_wheel_never_changes_input_even_with_focus(qapp, qtbot, kind, focused):
    first = install_input_wheel_guard()
    assert install_input_wheel_guard() is first
    window = QWidget()
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    edit = kind(window)
    getter, changed = configure(edit)
    layout.addWidget(edit)
    other = QLineEdit(window)
    layout.addWidget(other)
    window.show()
    window.activateWindow()
    (edit if focused else other).setFocus()
    qtbot.wait(10)
    before = getter()
    changes = []
    changed.connect(lambda *values: changes.append(values))
    for modifiers in (
        Qt.KeyboardModifier.NoModifier,
        Qt.KeyboardModifier.ControlModifier,
        Qt.KeyboardModifier.ShiftModifier,
    ):
        for pixel in (0, -21):
            wheel(edit, pixel=pixel, modifiers=modifiers)
    assert getter() == before
    assert changes == []


@pytest.mark.parametrize("child", [False, True])
def test_numeric_input_forwards_wheel_to_page_and_preserves_keyboard(qapp, qtbot, child):
    install_input_wheel_guard()
    page = QScrollArea()
    page.resize(360, 240)
    page.setWidgetResizable(True)
    qtbot.addWidget(page)
    body = QWidget()
    layout = QVBoxLayout(body)
    edit = QDoubleSpinBox(body)
    edit.setValue(10)
    layout.addWidget(edit)
    label = QLabel("Long page", body)
    label.setMinimumHeight(1500)
    layout.addWidget(label)
    page.setWidget(body)
    page.show()
    qtbot.wait(20)
    edit.setFocus()
    assert wheel(edit.lineEdit() if child else edit).isAccepted()
    assert page.verticalScrollBar().value() > 0
    assert edit.value() == 10
    qtbot.keyClick(edit, Qt.Key.Key_Up)
    assert edit.value() == 11
    edit.lineEdit().selectAll()
    qtbot.keyClicks(edit.lineEdit(), "12")
    qtbot.keyClick(edit, Qt.Key.Key_Return)
    assert edit.value() == 12


def test_filter_covers_later_dialogs_and_keeps_expanded_choice_scrolling(qapp, qtbot):
    install_input_wheel_guard()
    dialog = QDialog()
    qtbot.addWidget(dialog)
    layout = QVBoxLayout(dialog)
    edit = QSpinBox(dialog)
    edit.setValue(10)
    layout.addWidget(edit)
    combo = WorkspaceComboBox(dialog)
    combo.addItems([f"Choice {i}" for i in range(30)])
    layout.addWidget(combo)
    dialog.show()
    qtbot.wait(20)
    wheel(edit.lineEdit())
    assert edit.value() == 10
    combo.showPopup()
    qtbot.wait(20)
    try:
        wheel(combo.view().viewport())
        assert combo.view().verticalScrollBar().value() > 0
        assert combo.currentIndex() == 0
    finally:
        combo.hidePopup()


def test_scrollbar_remains_scrollable(qapp, qtbot):
    install_input_wheel_guard()
    page = QScrollArea()
    qtbot.addWidget(page)
    label = QLabel("Long page")
    label.setMinimumHeight(1500)
    page.setWidget(label)
    page.resize(300, 200)
    page.show()
    qtbot.wait(20)
    bar = page.verticalScrollBar()
    wheel(bar)
    assert bar.value() > 0


def test_main_window_installs_guard_and_overview_wheel_cannot_save_interval(
    qapp, qtbot, tmp_path, monkeypatch
):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = runtime.add(ConnectionConfig("mock://wheel-meter", "thorlabs.pm100d", "mock"))
        runtime.connect(key).result(3)
        window = RealWindow(runtime)
        qtbot.addWidget(window)
        window.timer.stop()
        runtime.monitoring.enable_settings_sync(False)
        window.show()
        window.navigate("实时监测")
        try:
            assert window._input_wheel_guard is qapp._input_wheel_guard
            editor = window.dashboard.cards[key].interval
            editor.setValue(2)
            editor.setFocus()
            qtbot.waitUntil(editor.hasFocus)
            changes = []
            window.controller.monitoring_interval_changed.connect(lambda *args: changes.append(args))
            monkeypatch.setattr(runtime.monitoring, "set_intervals", lambda *a, **k: changes.append((a, k)))
            for target in (editor, editor.lineEdit()):
                wheel(target)
                wheel(target, pixel=-21)
            window.controller.poll()
            assert changes == []
            assert editor.value() == 2
            assert runtime.repository.load_workspace(f"monitor-timing/general/{key}")["interval"] == 2
        finally:
            window.closing = True
            window.hide()
            window.controller.close()
            window.bridge.worker.shutdown()
