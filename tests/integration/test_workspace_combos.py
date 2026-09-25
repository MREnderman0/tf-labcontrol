"""Popup input regressions; all runtime tests use isolated Mock services."""

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QFont, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox, choose_item


@pytest.fixture
def choices(qapp, qtbot):
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    window = QWidget()
    window.resize(440, 400)
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    page = QScrollArea(window)
    page.setWidgetResizable(True)
    body = QWidget()
    content = QVBoxLayout(body)
    combo = WorkspaceComboBox(body)
    combo.replace_items([(f"Choice {i}", f"key-{i}") for i in range(30)], select_first=True)
    content.addWidget(combo)
    label = QLabel("Page body", body)
    label.setMinimumHeight(1600)
    content.addWidget(label)
    page.setWidget(body)
    layout.addWidget(page)
    window.show()
    qtbot.wait(30)
    yield window, page, combo
    combo.hidePopup()


def wheel(widget, *, angle=-120, pixel=0):
    pos = widget.rect().center()
    event = QWheelEvent(
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        QPoint(0, pixel),
        QPoint(0, angle),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate if pixel else Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)
    return event


@pytest.mark.parametrize("pixel", [0, 17])
def test_wheel_only_scrolls_open_list_and_never_commits_or_moves_page(choices, qtbot, pixel):
    _, page, combo = choices
    commits = []
    combo.currentIndexChanged.connect(commits.append)
    combo.showPopup()
    qtbot.wait(20)
    view = combo.view()
    rect = view.window().geometry()
    scrollbar = view.verticalScrollBar()
    for _ in range(4):
        assert wheel(view.viewport(), pixel=-pixel).isAccepted()
    assert scrollbar.value() > 0
    for edge in (scrollbar.maximum(), 0):
        scrollbar.setValue(edge)
        wheel(view.viewport(), angle=-120 if edge else 120, pixel=-pixel if edge else pixel)
        assert scrollbar.value() == edge
        # A wheel event over the underlying page is consumed while the menu owns input.
        wheel(page.viewport())
        assert page.verticalScrollBar().value() == 0
        assert view.window().geometry() == rect
    assert combo.currentData() == "key-0"
    assert commits == []
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    wheel(combo)
    assert combo.currentData() == "key-0"
    assert page.verticalScrollBar().value() > 0
    assert commits == []


def test_hover_and_edge_dwell_do_not_scroll_or_select(choices, qtbot):
    _, _, combo = choices
    combo.showPopup()
    view = combo.view()
    rect = view.window().geometry()
    bar = view.verticalScrollBar()
    bar.setValue(96)
    for y in (1, view.viewport().height() - 1):
        qtbot.mouseMove(view.viewport(), QPoint(20, y))
        qtbot.wait(350)
        assert bar.value() == 96
        assert view.window().geometry() == rect
        assert combo.currentIndex() == 0


def test_drag_scrollbar_does_not_commit(choices, qtbot):
    _, _, combo = choices
    combo.showPopup()
    view = combo.view()
    qtbot.wait(20)
    bar = view.verticalScrollBar()
    option = QStyleOptionSlider()
    bar.initStyleOption(option)
    handle = bar.style().subControlRect(
        QStyle.ComplexControl.CC_ScrollBar, option, QStyle.SubControl.SC_ScrollBarSlider, bar
    )
    qtbot.mousePress(bar, Qt.MouseButton.LeftButton, pos=handle.center())
    qtbot.mouseMove(bar, QPoint(handle.center().x(), bar.height() - 8))
    qtbot.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=QPoint(handle.center().x(), bar.height() - 8))
    assert bar.value() > 0
    assert combo.currentIndex() == 0
    assert view.isVisible()


def test_keyboard_click_and_outside_cancel(choices, qtbot):
    _, _, combo = choices
    activated = []
    combo.activated.connect(activated.append)
    combo.setFocus()
    qtbot.keyClick(combo, Qt.Key.Key_Space)
    qtbot.keyClick(combo.view(), Qt.Key.Key_Down)
    assert combo.currentIndex() == 0
    qtbot.keyClick(combo.view(), Qt.Key.Key_Escape)
    assert combo.currentIndex() == 0
    combo.showPopup()
    qtbot.keyClick(combo.view(), Qt.Key.Key_Down)
    qtbot.keyClick(combo.view(), Qt.Key.Key_Return)
    assert combo.currentIndex() == 1
    assert activated == [1]
    combo.showPopup()
    qtbot.keyClick(combo.view(), Qt.Key.Key_Down)
    qtbot.mouseClick(combo.view().window(), Qt.MouseButton.LeftButton, pos=QPoint(-10, -10))
    assert not combo.view().isVisible()
    assert combo.currentIndex() == 1
    assert activated == [1]


def test_refresh_defers_rename_and_reorder_then_commits_by_key(choices, qtbot):
    _, _, combo = choices
    combo.showPopup()
    view = combo.view()
    view.setCurrentIndex(combo.model().index(6, 0))
    view.verticalScrollBar().setValue(80)
    rect, scroll = view.window().geometry(), view.verticalScrollBar().value()
    replacement = [(f"Renamed {i}", f"key-{i}") for i in reversed(range(31))]
    combo.replace_items(replacement)
    qtbot.wait(50)
    assert view.isVisible()
    assert combo.itemText(0) == "Choice 0"
    assert view.window().geometry() == rect
    assert view.verticalScrollBar().value() == scroll
    qtbot.keyClick(view, Qt.Key.Key_Return)
    assert combo.currentData() == "key-6"
    assert combo.currentText() == "Renamed 6"
    assert combo.count() == 31


@pytest.mark.parametrize("removed", [0, 6, 9])
def test_invalidated_choice_closes_without_selecting_another_device(choices, removed):
    _, _, combo = choices
    combo.showPopup()
    combo.view().setCurrentIndex(combo.model().index(6, 0))
    combo.replace_items([(f"Choice {i}", f"key-{i}") for i in range(30) if i != removed])
    assert not combo.view().isVisible()
    assert combo.currentData() == (None if removed == 0 else "key-0")


def test_cancel_after_refresh_keeps_original_selection_and_latest_labels(choices, qtbot):
    _, _, combo = choices
    combo.showPopup()
    combo.view().setCurrentIndex(combo.model().index(6, 0))
    combo.replace_items([(f"Latest {i}", f"key-{i}") for i in reversed(range(30))])
    qtbot.keyClick(combo.view(), Qt.Key.Key_Escape)
    assert combo.currentData() == "key-0"
    assert combo.currentText() == "Latest 0"


def test_direct_model_mutation_cancels_pending_refresh_without_reentry(choices):
    _, _, combo = choices
    combo.showPopup()
    combo.replace_items([(f"Later {i}", f"key-{i}") for i in range(31)])
    combo.clear()
    assert not combo.view().isVisible()
    assert combo.count() == 0
    combo.addItem("New current model", "new")
    combo.showPopup()
    assert combo.currentData() == "new"


def test_only_window_geometry_changes_close_popup(choices, qtbot):
    window, page, combo = choices
    combo.showPopup()
    rect = combo.view().window().geometry()
    page.widget().layout().addWidget(QLabel("Updated status"))
    qtbot.wait(30)
    assert combo.view().isVisible()
    assert combo.view().window().geometry() == rect
    window.resize(window.width() + 20, window.height())
    assert not combo.view().isVisible()
    combo.showPopup()
    window.move(window.pos() + QPoint(5, 5))
    assert not combo.view().isVisible()


def test_empty_disabled_and_long_text(choices, qtbot):
    _, _, combo = choices
    combo.clear()
    combo.showPopup()
    assert not combo.view().isVisible()
    combo.addItem("Long label " * 300, "long")
    combo.setEnabled(False)
    combo.showPopup()
    assert not combo.view().isVisible()
    combo.setEnabled(True)
    combo.showPopup()
    qtbot.wait(20)
    assert combo.screen().availableGeometry().contains(combo.view().window().geometry())
    assert combo.view().viewport().rect().contains(combo.view().visualRect(combo.model().index(0, 0)))
    combo.model().item(0).setEnabled(False)
    assert not combo.view().isVisible()
    combo.showPopup()
    qtbot.keyClick(combo.view(), Qt.Key.Key_Return)
    assert combo.view().isVisible()  # Disabled row cannot be committed.


def test_reopening_shows_selected_row_without_post_show_jumps(choices, qtbot):
    _, _, combo = choices
    combo.setCurrentIndex(29)
    for _ in range(3):
        combo.showPopup()
        rect = combo.view().window().geometry()
        qtbot.wait(40)
        view = combo.view()
        assert view.window().geometry() == rect
        assert view.viewport().rect().contains(view.visualRect(combo.model().index(29, 0)))
        combo.hidePopup()


def test_choice_dialog_uses_shared_combo_and_preserves_return_contract(qapp, qtbot):
    def accept():
        dialog = qapp.activeModalWidget()
        combo = dialog.findChild(WorkspaceComboBox)
        assert combo is not None
        combo.setCurrentIndex(1)
        qtbot.mouseClick(
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok),
            Qt.MouseButton.LeftButton,
        )

    parent = QWidget()
    qtbot.addWidget(parent)
    QTimer.singleShot(10, accept)
    assert choose_item(parent, "Choose", "Slot", ["A", "B"]) == ("B", True)


def test_application_has_no_unmanaged_choice_constructors():
    root = Path(__file__).resolve().parents[2] / "src/lab_control/ui"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Name) and node.func.id == "QComboBox"), path
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr == "getItem"), path
