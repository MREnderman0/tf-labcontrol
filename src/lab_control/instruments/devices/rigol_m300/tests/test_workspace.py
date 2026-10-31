from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import Measurement, MeasurementBatch
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.pages.monitoring.page import MonitoringPage


def test_scan_metadata_reaches_channel_selector_plot_and_recording_request(qtbot, tmp_path):
    config = ConnectionConfig("mock://scan", "rigol.m300", "mock", expected_serial="TEST")
    app = ApplicationRuntime((config,), root=tmp_path, hardware=False)
    instrument = SimulatedInstrument(config)
    instrument.caps = replace(instrument.caps, channels=(1, *range(401, 433)))
    instrument.capability_details = {
        "acquisition_channel_selection": "scan_list",
        "acquisition_channels": [428],
    }
    app.sessions.factory = lambda _config: instrument
    owner = QWidget()
    qtbot.addWidget(owner)
    controller = None
    try:
        device = app.add(config)
        app.connect(device).result()
        controller = WorkspaceController(app, owner)
        page = MonitoringPage(controller, "multimeter", lambda *_: None, owner)
        qtbot.addWidget(page)
        page.resize(1400, 900)
        page.setStyleSheet(
            (Path(__file__).parents[4] / "ui/styles/card_workspace.qss").read_text(encoding="utf-8")
        )
        page.show()
        owner.show()
        page.selector.select(device, 428)
        assert page.selector.channel.currentData() == 428
        assert page.channels() == (428,) and not page.channel_checks
        dashboard = MonitoringDashboard(controller, owner)
        qtbot.addWidget(dashboard)
        grouped, confirmation = dashboard._requests()
        assert grouped[0][1][0].channels == (428,)
        assert grouped[0][1][0].confirmed and confirmation
        scan = page.scan_page
        assert scan is not None and scan.isVisible()
        assert page.splitter.isHidden() and not page.form.editors
        assert "CH 428" == scan.channel_checks[428].text()
        request = scan.request(scan.channels(), confirmed=True)
        assert request.channels == (428,)
        measurement = Measurement("temperature", "°C", (25.35,), 428)
        frame = MonitorFrame(1, 0, request, MeasurementBatch(instrument.identity, (measurement,), {}))
        scan.plot.show_frame(frame, scan.channels())
        scan.cards[428].update_measurement(measurement)
        assert "25.35" in scan.cards[428].value.text()
        assert scan.cards[428].unit.text() == "°C"
        assert 428 in scan.plot.curves
        assert not hasattr(scan, "form")
        instrument.capability_details["acquisition_channels"] = list(range(401, 420))
        app.sessions.submit(device, "refresh_capabilities").result()
        page.refresh()
        assert page.channels() == tuple(range(401, 420))
        assert tuple(scan.cards) == tuple(range(401, 420))
        assert "19 路" in scan.channel_title.text()
        grouped, _ = dashboard._requests()
        assert grouped[0][1][0].channels == tuple(range(401, 420))
        instrument.capability_details["acquisition_channels"] = []
        app.sessions.submit(device, "refresh_capabilities").result()
        assert dashboard._requests()[0] == []
        page.refresh()
        assert not scan.start_button.isEnabled()
        qtbot.wait(20)
    finally:
        if controller:
            controller.close()
        app.close()
