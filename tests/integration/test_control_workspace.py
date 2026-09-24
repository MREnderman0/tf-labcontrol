import json

import pytest
from PySide6.QtWidgets import QMessageBox, QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.controls import CATEGORY_LABELS, MEASUREMENT_CATEGORIES, OPERATION_LABELS
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.demo.workspace import create_demo_runtime
from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    DeviceCommand,
    DeviceError,
    Parameter,
    ParameterReading,
)
from lab_control.instruments.registry import PROFILES, SDK_ADAPTERS, describe
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.editors import ArrayEditor, ParameterEditor
from lab_control.ui.pages.device_control.form import OperationEditor
from lab_control.ui.pages.monitoring.dashboard import ResponsiveGrid
from lab_control.ui.pages.monitoring.page import MonitoringPage
from lab_control.ui.real_window import RealWindow


def shutdown(window):
    window.timer.stop()
    window.controller.close()
    window.runtime.close()
    window.closing = True
    window.bridge.worker.shutdown()


def test_every_registered_capability_has_ui_label_and_placement():
    for adapter in (*PROFILES, *SDK_ADAPTERS):
        caps = describe(ConnectionConfig("mock://metadata", adapter, "mock"))
        assert caps.category in CATEGORY_LABELS
        for cap in caps.operations:
            assert cap.name in OPERATION_LABELS, (adapter, cap.name)


def test_mock_pico_time_axis_global_power_outputs_and_optical_spectrum():
    pico = SimulatedInstrument(ConnectionConfig("mock://pico", "pico.6404e", "mock"))
    request = AcquisitionRequest("waveform", points=32, initiate=True, confirmed=True)
    first = pico.acquire(request).measurements[0]
    assert first.axis[-1] > first.axis[0]
    pico.apply(DeviceCommand("timebase", {"value": 11}))
    assert pico.acquire(request).measurements[0].axis[-1] > first.axis[-1]
    power = SimulatedInstrument(ConnectionConfig("mock://power", "keithley.2231a", "mock"))
    power.apply(DeviceCommand("all_outputs", {"value": True}, confirmed=True))
    assert all(m.values[0] > 0 for m in power.acquire(AcquisitionRequest()).measurements)
    optical = SimulatedInstrument(ConnectionConfig("mock://optical", "sim.optical_spectrometer", "mock"))
    measurement = optical.acquire(AcquisitionRequest("spectrum", points=32)).measurements[0]
    assert measurement.axis_unit == "nm" and measurement.axis[0] == 1400 and measurement.axis[-1] == 1600


def test_units_boolean_readback_and_utf8_array_import(qtbot, tmp_path):
    editor = ParameterEditor(Parameter(unit="Hz", minimum=0, maximum=1e9))
    qtbot.addWidget(editor)
    editor.input.setText("2.5e1")
    editor.unit.setCurrentIndex(editor.unit.findText("MHz"))
    assert editor.value() == 25e6
    boolean = ParameterEditor(Parameter("boolean"))
    qtbot.addWidget(boolean)
    boolean.set_value("OFF")
    assert boolean.value() is False
    boolean.set_value("ON")
    assert boolean.value() is True
    table = ArrayEditor({"samples": Parameter("array", minimum=-1, maximum=1)})
    qtbot.addWidget(table)
    path = tmp_path / "波形样点.csv"
    path.write_text("归一化样点\n-1\n0\n0.5\n1\n", encoding="utf-8-sig")
    table.load_file(path)
    assert table.values() == {"samples": [-1, 0, 0.5, 1]}
    assert table.preview.listDataItems()
    path.write_text("1\nnan\n", encoding="utf-8")
    with pytest.raises(DeviceError):
        table.load_file(path)
    assert len(table.model.rows) == 4  # A rejected import does not lose existing draft data.


def test_draft_is_not_overwritten_and_cancel_sends_nothing(qtbot, tmp_path, monkeypatch):
    config = ConnectionConfig("mock://control", "rigol.dg5504pro", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device).result()
        parent = QWidget()
        qtbot.addWidget(parent)
        controller = WorkspaceController(runtime, parent)
        editor = OperationEditor(controller, device, controller.capabilities(device).get("frequency"), 1)
        qtbot.addWidget(editor)
        qtbot.keyClicks(editor.editors["value"].input, "2e3")
        assert editor.dirty  # Protect a field while the user is still typing.
        editor.show_reading(ParameterReading("frequency", 1, 1000, "instrument"))
        assert editor.parameters() == {"value": 2000}
        assert "1000" in editor.readback.text()
        editor.applied(ApplyResult("partial", "frequency", {"value": 2000}, error="second step failed"))
        assert editor.dirty and "1000" in editor.readback.text()
        restored = OperationEditor(controller, device, editor.cap, 1)
        qtbot.addWidget(restored)
        assert restored.parameters() == {"value": 2000} and restored.dirty
        output = OperationEditor(controller, device, controller.capabilities(device).get("output"), 1)
        qtbot.addWidget(output)
        output.editors["value"].input.setChecked(True)
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
        output.apply()
        assert runtime.sessions.sessions[device].configuration_version == 0
        assert not runtime.sessions.sessions[device].adapter._value(1, "output")
        controller.close()


def test_responsive_grid_reuses_cards_without_column_limit(qtbot):
    grid = ResponsiveGrid()
    qtbot.addWidget(grid)
    cards = [QWidget() for _ in range(9)]
    for card in cards:
        card.setMinimumHeight(180)
    grid.set_items(cards)
    grid.show()
    for width in (320, 790, 1700, 1100, 450):
        grid.resize(width, 650)
        qtbot.waitUntil(lambda: grid.columns == max(1, (grid.viewport().width() + 16) // 376))
        assert grid.items == cards
        for index, card in enumerate(cards):
            assert grid.grid.itemAtPosition(index // grid.columns, index % grid.columns).widget() is card
            assert card.geometry().right() <= grid.viewport().width()
        assert grid.horizontalScrollBar().maximum() == 0
        if width == 1700:
            assert grid.columns >= 4


def test_shared_demo_pages_layout_modes_and_live_tasks(qtbot, tmp_path):
    runtime = create_demo_runtime(tmp_path)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        assert len(runtime.inventory.all()) == 24
        assert not runtime.monitoring.snapshots()
        for category in CATEGORY_LABELS:
            window.navigate("设备管理", category)
            page = window.stack.currentWidget()
            assert page.selector.combo.count() >= 2
            if category not in MEASUREMENT_CATEGORIES:
                caps = window.controller.capabilities(page.selector.device_id)
                if category == "signal_generator":
                    names = {
                        name
                        for panel in [*page.panels.values(), *page.advanced_groups]
                        for name in panel.editors
                    } | {"output"}
                    assert names == {c.name for c in caps.operations}
                elif category == "power_supply":
                    assert set(page.panels) == set(caps.channels)
                    assert all(set(panel.editors) == {"voltage", "current"} for panel in page.panels.values())
                else:
                    assert set(page.form.editors) == {c.name for c in caps.operations}
            window.navigate("实时监测", category)
            monitor = window.stack.currentWidget()
            assert isinstance(monitor, MonitoringPage)
            if category in MEASUREMENT_CATEGORIES:
                caps = window.controller.capabilities(monitor.selector.device_id)
                assert set(monitor.form.editors) == {c.name for c in caps.operations}
        window.navigate("实时监测")
        dashboard = window.dashboard
        spec = next(s for s in dashboard.specs if s["source"] == "waveform")
        device = spec["device_id"]
        dashboard.add(device, "waveform", (2,), "CH2")
        card = dashboard.specs[-1]
        dashboard.card_action("open", card["id"])
        assert window.stack.currentWidget().channels() == (2,)
        runtime.monitoring.start(device, (AcquisitionRequest("waveform", (1, 2), 32),), interval=0.05)
        qtbot.waitUntil(lambda: runtime.monitoring.latest(device, "waveform") is not None)
        original = runtime.sessions.sessions[device].session_id
        window.navigate("实时监测")
        widgets = dict(dashboard.cards)
        for width in (1100, 1900, 760, 1450):
            window.resize(width, 900)
            window.toggle_sidebar()
            qtbot.wait(20)
            if width == 760:
                window.sidebar.setFixedWidth(196)
                qtbot.wait(20)
                assert window.width() == max(760, window.minimumWidth())
                assert dashboard.grid.columns >= 1
        assert dashboard.cards == widgets
        saved = runtime.repository.load_workspace("monitor_layout/electronic")
        assert "columns" not in json.dumps(saved)
        window.mode.setCurrentIndex(1)
        assert window.controller.mode == "optical"
        assert runtime.monitoring.snapshots()[device]["running"]
        assert runtime.sessions.sessions[device].session_id == original
        assert dashboard.specs != saved
        window.mode.setCurrentIndex(0)
        assert dashboard.specs == saved
        runtime.monitoring.set_recording(device, True)
        with pytest.raises(DeviceError, match="记录"):
            window.controller.set_mode("optical")
        runtime.monitoring.stop(device).result(timeout=3)
    finally:
        shutdown(window)
