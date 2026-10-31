"""Exercise the complete scan UI with isolated instruments and real storage."""

import json
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QMessageBox

from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import AcquisitionRequest, Measurement, MeasurementBatch
from lab_control.storage.recording import read_frames
from lab_control.ui.pages.monitoring.page import MonitoringPage
from lab_control.ui.pages.monitoring.scan import ScanReadoutCard, ScanTrendPlot
from lab_control.ui.real_window import RealWindow

CHANNELS = tuple(range(401, 420))


def scan_instrument(config):
    instrument = SimulatedInstrument(config)
    instrument.caps = replace(instrument.caps, channels=(1, *range(401, 433)))
    instrument.capability_details = {
        "acquisition_channel_selection": "scan_list",
        "acquisition_channels": list(CHANNELS),
    }
    return instrument


def close_window(window, runtime):
    window.timer.stop()
    window.controller.close()
    runtime.close()
    window.closing = True
    window.bridge.worker.shutdown()


def test_scan_curve_selection_keeps_full_recording_and_snapshot(qtbot, tmp_path, monkeypatch):
    config = ConnectionConfig("mock://scan-workspace", "rigol.m300", "mock")
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    instrument = scan_instrument(config)
    runtime.sessions.factory = lambda _config: instrument
    device = runtime.add(config)
    runtime.connect(device).result(5)
    requests, writes, saved_frames, saved_results = [], [], [], []
    acquire = instrument.acquire

    def temperature(request):
        requests.append(request)
        batch = acquire(request)
        return replace(
            batch,
            measurements=tuple(
                Measurement("temperature", "°C", (-3 + i * 0.2,), ch) for i, ch in enumerate(request.channels)
            ),
        )

    monkeypatch.setattr(instrument, "acquire", temperature)
    monkeypatch.setattr(instrument, "apply", lambda command: writes.append(command))
    export = runtime.monitoring.export_displayed_frame

    def capture_export(device_id, frame, mode):
        saved_frames.append(frame)
        result = export(device_id, frame, mode)
        saved_results.append(result)
        return result

    monkeypatch.setattr(runtime.monitoring, "export_displayed_frame", capture_export)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        window.navigate("设备管理", "multimeter", device)
        outer = window.stack.currentWidget()
        assert isinstance(outer, MonitoringPage)
        page = outer.scan_page
        assert page is not None and page.isVisible()
        assert window.current_module == "实时监测"
        assert "multimeter" not in window._submenu_categories("设备管理")
        assert outer.form.editors == {} and outer.splitter.isHidden()
        assert tuple(page.cards) == CHANNELS
        assert page.splitter.widget(1).objectName() == "scanReadoutList"
        assert not page.channel_bar.isVisible()
        assert page.readout_grid.itemAtPosition(0, 0).widget() is page.cards[401]
        assert page.readout_grid.itemAtPosition(0, 1).widget() is page.cards[402]
        assert page.readout_grid.itemAtPosition(9, 0).widget() is page.cards[419]
        assert page.readout_grid.itemAtPosition(9, 1) is None
        assert all(card.height() == 28 and not hasattr(card, "peak") for card in page.cards.values())
        assert not hasattr(page, "form")
        assert runtime.monitoring.settings_sync.advanced == ()

        monkeypatch.setattr(
            QMessageBox, "question", lambda *_: pytest.fail("Starting M300 monitoring must not open a dialog")
        )
        page.timing.interval.setValue(0.5)
        page.start_button.click()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None, timeout=8000)
        task = runtime.monitoring.tasks[device]
        assert task.requests[0].channels == CHANNELS and task.requests[0].confirmed
        assert len(page.plot.curves) == 19
        assert page.cards[401].unit.text() == "°C"
        assert page.plot.plot.viewRange()[1][0] < -3

        page.record()
        qtbot.waitUntil(lambda: task.recording and task.recorder.snapshot()["committed"] >= 1, timeout=8000)
        committed = task.recorder.snapshot()["committed"]
        page.channel_checks[401].setChecked(False)
        assert not page.plot.curves[401].isVisible()
        assert page.cards[401].isVisible()
        assert page.cards[401].unit.text() == "°C"
        assert page.channel_checks[401].parentWidget() is page.cards[401]
        assert task.requests[0].channels == CHANNELS
        for check in page.channel_checks.values():
            check.setChecked(False)
        assert page.displayed_channels() == () and page.channels() == CHANNELS
        assert page.plot.displayed_frame is not None and page.save_button.isEnabled()
        assert all(not curve.isVisible() for curve in page.plot.curves.values())
        qtbot.waitUntil(lambda: task.recorder.snapshot()["committed"] > committed, timeout=8000)
        assert task.recording and task.requests[0].channels == CHANNELS
        page.save()
        assert len(saved_frames) == 1
        assert tuple(m.channel for m in saved_frames[0].batch.measurements) == CHANNELS
        qtbot.waitUntil(
            lambda: not runtime.monitoring.snapshots()[device].get("display_saving"), timeout=8000
        )
        folder = saved_results[0].result(8)
        snapshot = json.loads((folder / "_raw" / "snapshot.json").read_text(encoding="utf-8"))
        assert tuple(m["channel"] for m in snapshot["measurements"]) == CHANNELS

        before_hide = task.sequence
        window.navigate("设备管理")
        qtbot.waitUntil(lambda: task.sequence > before_hide + 1, timeout=8000)
        window.navigate("实时监测", "multimeter", device)
        qtbot.waitUntil(lambda: page.plot.displayed_frame.sequence >= task.sequence, timeout=8000)
        assert all(not check.isChecked() for check in page.channel_checks.values())
        page.channel_checks[419].setChecked(True)
        assert page.plot.curves[419].isVisible()
        assert len(page.plot.history[419]) > 1
        page.toggle_monitoring()
        qtbot.waitUntil(lambda: not runtime.monitoring.snapshots()[device]["running"], timeout=8000)
        runtime.monitoring.stop(device).result(8)
        paths = list(runtime.repository.data_root.glob("*/**/*_continuous_*/_raw/segment-*.sqlite3"))
        assert paths
        frames = [frame for path in paths for frame in read_frames(path)]
        assert frames and len(frames) > 1
        assert all(tuple(m["channel"] for m in frame["measurements"]) == CHANNELS for frame in frames)
        assert all(m["unit"] == "°C" for frame in frames for m in frame["measurements"])
        assert all(request.channels == CHANNELS for request in requests)
        assert not writes
    finally:
        close_window(window, runtime)


def test_switch_scan_and_multimeter_preserves_management_controls(qtbot, tmp_path):
    scan_config = ConnectionConfig("mock://scan-switch", "rigol.m300", "mock")
    meter_config = ConnectionConfig("mock://meter-switch", "keysight.3458a", "mock")
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    instrument = scan_instrument(scan_config)
    runtime.sessions.factory = lambda config: (
        instrument if config.adapter == "rigol.m300" else SimulatedInstrument(config)
    )
    scan_id, meter_id = runtime.add(scan_config), runtime.add(meter_config)
    runtime.connect(scan_id).result(5)
    runtime.connect(meter_id).result(5)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        assert "multimeter" in window._submenu_categories("设备管理")
        window.navigate("实时监测", "multimeter", scan_id)
        outer = window.stack.currentWidget()
        scan = outer.scan_page
        assert scan.isVisible() and not outer.form.editors
        # The selector inside the scan view switches the same category page.
        scan.selector.select(meter_id)
        qtbot.waitUntil(lambda: outer.form.device_id == meter_id)
        assert outer.splitter.isVisible() and scan.isHidden()
        assert outer.form.editors and not scan.device_id
        outer.selector.select(scan_id)
        qtbot.waitUntil(lambda: scan.isVisible())
        assert not outer.form.editors and outer.splitter.isHidden()
        assert tuple(scan.cards) == CHANNELS
        window.navigate("设备管理", "multimeter", meter_id)
        management = window.stack.currentWidget()
        assert window.current_module == "设备管理"
        assert management.selector.device_id == meter_id
        assert management.go_monitor.isEnabled()
        assert management.selector.combo.findData(scan_id) == -1
        qtbot.wait(20)
    finally:
        close_window(window, runtime)


@pytest.mark.parametrize(
    ("quantity", "unit", "value"),
    [
        ("temperature", "°C", -12.5),
        ("temperature", "°F", 65.0),
        ("resistance", "Ω", 5000.0),
        ("voltage", "V", -0.05),
    ],
)
def test_scan_plot_native_units_and_rejects_incomplete_frame(qtbot, quantity, unit, value):
    instrument = scan_instrument(ConnectionConfig("mock://plot", "rigol.m300", "mock"))
    plot, card = ScanTrendPlot(), ScanReadoutCard(401)
    qtbot.addWidget(plot)
    qtbot.addWidget(card)
    request = AcquisitionRequest("scalar", channels=CHANNELS, points=1, confirmed=True)
    batch = MeasurementBatch(
        instrument.identity, tuple(Measurement(quantity, unit, (value,), ch) for ch in CHANNELS), {}
    )
    frame = MonitorFrame(1, 0, request, batch)
    plot.set_channels(CHANNELS)
    assert plot.show_frame(frame, CHANNELS)
    card.update_measurement(batch.measurements[0])
    assert card.unit.text() == unit and str(value).rstrip("0").rstrip(".") in card.value.text()
    assert plot.display_unit == unit
    accepted = plot.displayed_frame
    for measurements in (
        batch.measurements[:-1],
        (replace(batch.measurements[0], values=(float("nan"),)), *batch.measurements[1:]),
        (replace(batch.measurements[0], unit="invalid"), *batch.measurements[1:]),
    ):
        assert not plot.show_frame(
            replace(frame, sequence=2, batch=replace(batch, measurements=measurements)), CHANNELS
        )
        assert plot.displayed_frame is accepted
    changed = replace(
        batch,
        measurements=tuple(
            replace(m, quantity="resistance", unit="Ω", values=(6000.0,)) for m in batch.measurements
        ),
    )
    assert plot.show_frame(replace(frame, sequence=3, batch=changed), CHANNELS)
    assert plot.display_unit == "Ω"
    assert len(plot.history[401]) == (2 if (quantity, unit) == ("resistance", "Ω") else 1)
