"""Real keyboard edits and category reflow in the shared monitoring workspace."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.pages.monitoring.timing import (
    MonitoringTiming,
    monitoring_options,
    save_monitoring_interval,
)


@pytest.fixture
def workspace(qtbot, tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        meters = []
        for index in range(4):
            key = runtime.add(ConnectionConfig(f"mock://editing/{index}", "joinwit.jw8105", "mock"))
            runtime.connect(key).result(3)
            meters.append(key)
        other = runtime.add(ConnectionConfig("mock://editing/source", "rs.sma100b", "mock"))
        runtime.connect(other).result(3)
        window = QWidget()
        qtbot.addWidget(window)
        controller = WorkspaceController(runtime, window)
        controller.timer.stop()
        runtime.monitoring.enable_settings_sync(False)
        layout = QVBoxLayout(window)
        dashboard = MonitoringDashboard(controller)
        timing = MonitoringTiming(controller)
        timing.set_fixed_interval()
        timing.bind(meters[0], "general")
        layout.addWidget(timing)
        layout.addWidget(dashboard)
        window.resize(1450, 900)
        window.show()
        qtbot.wait(50)
        try:
            yield window, controller, dashboard, timing, meters
        finally:
            controller.close()


@pytest.mark.parametrize("view", ["overview", "detail"])
@pytest.mark.parametrize("finish", ["blur", "enter"])
@pytest.mark.parametrize("running", [False, True])
def test_interval_draft_survives_refresh_and_only_commits_after_editing(
    workspace, qtbot, view, finish, running, monkeypatch
):
    window, controller, dashboard, timing, meters = workspace
    runtime, key = controller.runtime, meters[0]
    writes = []
    monkeypatch.setattr(
        runtime.sessions.sessions[key].adapter, "apply", lambda command: writes.append(command)
    )
    editor = dashboard.cards[key].interval if view == "overview" else timing.interval
    editor.setValue(2)
    if running:
        runtime.monitoring.start(key, (AcquisitionRequest("scalar", quantity="power"),), interval=2)
    changes = []
    controller.monitoring_interval_changed.connect(lambda *args: changes.append(args))
    editor.setFocus()
    qtbot.waitUntil(editor.hasFocus)
    editor.selectAll()
    qtbot.keyClicks(editor, "0")
    assert editor.cleanText() == "0" and editor.value() == 2
    controller.poll()
    assert editor.cleanText() == "0" and editor.hasFocus(), "dashboard refresh replaced the draft"
    timing.refresh(runtime.monitoring.snapshots().get(key, {}))
    assert editor.cleanText() == "0" and editor.hasFocus(), "detail refresh replaced the draft"
    # A second card's accepted edit also must not replace this draft.
    dashboard.cards[meters[1]].interval.setValue(3)
    assert editor.cleanText() == "0" and editor.hasFocus()
    changes.clear()
    qtbot.keyClicks(editor, ".5")
    qtbot.wait(120)
    controller.poll()
    timing.refresh(runtime.monitoring.snapshots().get(key, {}))
    assert editor.cleanText() == "0.5" and editor.value() == 2
    assert monitoring_options(controller, key, "general")["interval"] == 2
    assert not changes and not writes
    if finish == "blur":
        dashboard.sample_button.setFocus()
    else:
        qtbot.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: monitoring_options(controller, key, "general")["interval"] == 0.5)
    assert editor.value() == timing.interval.value() == dashboard.cards[key].interval.value() == 0.5
    assert runtime.repository.load_workspace(f"monitor-timing/general/{key}")["interval"] == 0.5
    assert [change for change in changes if change[0] == key] == [(key, 0.5)]
    assert not writes
    if running:
        assert runtime.monitoring.snapshots()[key]["interval"] == 0.5
        assert not runtime.monitoring.snapshots()[key]["recording"]
        runtime.monitoring.stop(key).result(3)
    else:
        assert not runtime.monitoring.tasks
    assert not runtime.repository.data_root.exists()


def test_categories_start_new_rows_and_devices_wrap_at_three(workspace, qtbot):
    window, _, dashboard, _, meters = workspace
    group = dashboard.groups["optical_power_meter"]
    qtbot.waitUntil(lambda: group.grid.columns == 3)
    positions = [group.grid.layout_grid.getItemPosition(i)[:2] for i in range(4)]
    assert positions == [(0, 0), (0, 1), (0, 2), (1, 0)]
    assert {card.spec["device_id"] for card in group.grid.items} == set(meters)
    assert dashboard.grid.columns == 1
    assert [dashboard.grid.grid.getItemPosition(i)[:2] for i in range(2)] == [(0, 0), (1, 0)]
    source = dashboard.groups["signal_generator"]
    assert group.y() >= source.y() + source.height()
    window.resize(2200, 900)
    qtbot.wait(50)
    assert group.grid.columns == 3
    window.resize(700, 900)
    qtbot.waitUntil(lambda: group.grid.columns == 1)
    window.resize(1450, 900)
    qtbot.waitUntil(lambda: group.grid.columns == 3)
    assert dashboard.grid.horizontalScrollBar().maximum() == 0


def test_overview_changes_cadence_only_for_the_explicit_interval_edit(workspace):
    _, controller, dashboard, _, meters = workspace
    for key in meters:
        save_monitoring_interval(controller, key, "general", 2, cadence="fastest")
    changed, other = meters[:2]
    dashboard.cards[other].enabled.setChecked(False)
    assert all(monitoring_options(controller, key, "general")["cadence"] == "fastest" for key in meters)
    dashboard.cards[changed].interval.setValue(3)
    assert monitoring_options(controller, changed, "general")["cadence"] == "interval"
    assert all(monitoring_options(controller, key, "general")["cadence"] == "fastest" for key in meters[1:])
    assert not controller.runtime.monitoring.tasks


@pytest.mark.parametrize("view", ["overview", "detail"])
@pytest.mark.parametrize("draft, expected", [("0", 0.5), ("99", 60), ("-1", 0.5), ("abc", 2), ("", 2)])
def test_interval_bounds_and_invalid_text_are_resolved_on_blur(workspace, qtbot, view, draft, expected):
    _, controller, dashboard, timing, meters = workspace
    key = meters[0]
    editor = dashboard.cards[key].interval if view == "overview" else timing.interval
    editor.setValue(2)
    editor.setFocus()
    qtbot.waitUntil(editor.hasFocus)
    editor.selectAll()
    qtbot.keyClick(editor, Qt.Key.Key_Backspace)
    qtbot.keyClicks(editor, draft)
    assert editor.cleanText() == draft and editor.value() == 2
    assert monitoring_options(controller, key, "general")["interval"] == 2
    dashboard.sample_button.setFocus()
    qtbot.waitUntil(lambda: editor.value() == expected)
    assert editor.cleanText() == f"{expected:.2f}"
    assert monitoring_options(controller, key, "general")["interval"] == expected
