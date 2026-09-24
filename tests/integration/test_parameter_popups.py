import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.registry import PROFILES
from lab_control.ui.pages.device_control.editors import UNIT_CHOICES
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.parameter_combo import ParameterComboBox


@pytest.fixture
def styled(qapp):
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    return qapp


@pytest.mark.parametrize(
    "items",
    [[label for label, _ in UNIT_CHOICES[unit]] for unit in ("Vpp", "Hz", "s")]
    + [["正弦波", "方波", "脉冲", "斜坡", "噪声", "直流", "任意波形"]],
)
@pytest.mark.parametrize("at_bottom", [False, True])
def test_every_popup_row_is_complete_and_selectable(styled, qtbot, items, at_bottom):
    window = QWidget()
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    combo = ParameterComboBox(window)
    combo.addItems(items)
    combo.setCurrentIndex(len(items) - 1)
    combo.setFixedWidth(90)
    layout.addWidget(combo)
    window.show()
    screen = window.screen().availableGeometry()
    window.move(screen.left() + 40, screen.bottom() - 90 if at_bottom else screen.top() + 50)
    qtbot.wait(10)
    for index in range(combo.count()):
        combo.showPopup()
        qtbot.wait(10)
        view = combo.view()
        assert screen.contains(view.window().geometry())
        for row in range(combo.count()):
            rect = view.visualRect(combo.model().index(row, 0))
            assert rect.height() == 28
            assert view.viewport().rect().contains(rect)
            assert rect.width() >= view.fontMetrics().horizontalAdvance(combo.itemText(row)) + 16
        if at_bottom:
            assert view.window().geometry().bottom() < combo.mapToGlobal(QPoint(0, 0)).y()
        target = view.visualRect(combo.model().index(index, 0))
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=target.center())
        assert combo.currentIndex() == index
        assert not view.isVisible()


def test_long_menu_scrolls_and_fits_screen(styled, qtbot):
    window = QWidget()
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    combo = ParameterComboBox(window)
    combo.addItems([f"选项 {i} — 较长的枚举说明" for i in range(20)])
    combo.setFixedWidth(90)
    layout.addWidget(combo)
    window.show()
    screen = window.screen().availableGeometry()
    window.move(screen.right() - window.width(), screen.top() + 40)
    qtbot.wait(10)
    combo.showPopup()
    qtbot.wait(10)
    view = combo.view()
    assert screen.contains(view.window().geometry())
    assert view.viewport().height() == 10 * 28
    assert view.verticalScrollBar().maximum() > 0
    last = combo.model().index(19, 0)
    view.scrollTo(last)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(last).center())
    assert combo.currentIndex() == 19


class WindowShows(QObject):
    def __init__(self):
        super().__init__()
        self.events = []

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            text = watched.text() if isinstance(watched, (QLabel, QPushButton)) else ""
            self.events.append((type(watched).__name__, text))
        return False


@pytest.fixture
def cold_source(styled, qtbot, tmp_path, monkeypatch):
    if not {"tek.afg31252", "rs.sma100b"}.issubset(PROFILES):
        pytest.skip("This cross-model UI fixture requires the optional RF and AWG packages")
    runtime = ApplicationRuntime((), root=tmp_path / "mock", hardware=False)
    ids = []
    writes = []
    for model in ("tek.afg31252", "rs.sma100b", "rigol.dg5504pro"):
        key = runtime.add(ConnectionConfig(f"mock://popup/{model}", model, "mock", identity_confirmed=True))
        runtime.connect(key).result()
        monkeypatch.setattr(runtime.sessions.sessions[key].adapter, "apply", writes.append)
        ids.append(key)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: not window.controller.jobs)
    try:
        yield window, runtime, ids, writes
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_first_source_device_and_tab_switches_never_show_independent_widgets(cold_source, styled, qtbot):
    window, _, ids, writes = cold_source
    shows = WindowShows()
    styled.installEventFilter(shows)
    try:
        for key in ids + ids[:1]:
            window.navigate("设备管理", "signal_generator", key)
            qtbot.waitUntil(lambda: not window.controller.jobs)
            page = window.stack.currentWidget()
            for index in range(page.tabs.count()):
                page.tabs.setCurrentIndex(index)
                qtbot.wait(10)
        assert shows.events == []
        assert writes == []
    finally:
        styled.removeEventFilter(shows)


@pytest.mark.parametrize("width,height", [(1440, 900), (1100, 760), (1024, 600), (800, 560)])
def test_unit_selection_keeps_draft_semantics_without_writes(cold_source, qtbot, width, height):
    window, runtime, ids, writes = cold_source
    window.navigate("设备管理", "signal_generator", ids[0])
    window.fit_to_screen(QRect(0, 0, width, height))
    qtbot.waitUntil(lambda: not window.controller.jobs)
    page = window.stack.currentWidget()
    panel = page.panels[1]
    editor = panel.editors["amplitude"]
    field = editor.editors["value"]
    assert not panel.apply_button.isEnabled()
    field.input.setFocus()
    field.input.selectAll()
    qtbot.keyClicks(field.input, "500")
    qtbot.mouseClick(field.unit, Qt.MouseButton.LeftButton)
    qtbot.wait(20)
    assert field.input.text() == "500"  # Do not clamp in Vpp before choosing mVpp.
    view = field.unit.view()
    assert view.viewport().height() == 56
    target = field.unit.model().index(0, 0)
    qtbot.mouseMove(view.viewport(), view.visualRect(target).center())
    qtbot.wait(600)  # Let Qt's opening-click/double-click guard expire.
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(target).center())
    qtbot.wait(30)
    assert field.unit.currentIndex() == 0, (field.input.text(), view.visualRect(target), view.isVisible())
    assert field.value() == 0.5
    assert panel.apply_button.isEnabled()
    assert runtime.repository.load_workspace(editor.key)["fields"]["value"]["factor"] == 1e-3
    assert page.last_columns == 2
    assert window.sidebar.width() == 232
    assert writes == []
