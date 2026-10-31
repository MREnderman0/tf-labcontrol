import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.devices.kplusk_fxe.adapter import KpluskFxeAdapter
from lab_control.instruments.devices.kplusk_fxe.tests.test_adapter import BinaryReport, FakeLibrary
from lab_control.storage.recording import read_frames
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.frequency_counter import FrequencyCounterMonitoringPage


class StreamingLibrary(FakeLibrary):
    count = 4
    sequence = 0

    def get_kk_report(self, source):
        if self.binary_reports:
            return super().get_kk_report(source)
        time.sleep(0.01)
        self.sequence += 1
        return BinaryReport(
            tuple(float(i + self.sequence) for i in range(self.count)), device_ms=self.sequence * 1000
        )


def connected_runtime(tmp_path):
    config = ConnectionConfig("mock://frequency", "kplusk.fxe", "mock")
    runtime = ApplicationRuntime((config,), root=tmp_path, hardware=False)
    library = StreamingLibrary()
    runtime.sessions.factory = lambda c: KpluskFxeAdapter(
        replace(c, transport="kplusk", resource="192.0.2.10"), library
    )
    device = runtime.add(config)
    runtime.connect(device, configure_acquisition=False).result(3)
    return runtime, device, library


def test_four_channels_ui_empty_selection_and_changed_capabilities(qtbot, tmp_path):
    runtime, device, library = connected_runtime(tmp_path)
    parent = QWidget()
    qtbot.addWidget(parent)
    controller = WorkspaceController(runtime, parent)
    controller.timer.stop()
    try:
        page = FrequencyCounterMonitoringPage(controller)
        qtbot.addWidget(page)
        page.select_device(device)
        assert tuple(page.channel_checks) == (1, 2, 3, 4)
        assert page.channels() == (1, 2, 3, 4)
        for check in page.channel_checks.values():
            check.setChecked(False)
        page.select_device(device)
        assert page.channels() == () and not page.toggle_button.isEnabled()
        page.channel_checks[1].setChecked(True)
        page.channel_checks[4].setChecked(True)
        # Consume initial cached frame, then simulate a different reported count.
        request = AcquisitionRequest(kind="scalar", channels=(1,), quantity="frequency")
        runtime.sessions.submit(device, "acquire", request).result(3)
        library.count = 2
        runtime.sessions.submit(device, "acquire", request).result(3)
        controller.poll()
        assert tuple(page.channel_checks) == (1, 2)
        assert page.channels() == (1,)
        assert "CH 4" in page.status.text()
    finally:
        controller.close()
        runtime.close()


def test_registered_readonly_acceptance_uses_real_services_with_fake_library(qtbot, tmp_path, monkeypatch):
    from lab_control.instruments.devices.kplusk_fxe import adapter as model
    from lab_control.instruments.devices.kplusk_fxe.hardware import readonly

    config = ConnectionConfig("mock://acceptance", "kplusk.fxe", "mock")

    def simulated_runtime(configs, root=None):
        runtime = ApplicationRuntime(configs, root=tmp_path, hardware=False)
        disconnect = runtime.sessions.disconnect

        def delayed_disconnect(*args, **kwargs):
            disconnect(*args, **kwargs)
            # Give the real scheduler time to mark the old task disconnected.
            # Intentional disconnect/reconnect must not fail the acceptance runner.
            time.sleep(0.1)

        runtime.sessions.disconnect = delayed_disconnect
        return runtime

    monkeypatch.setattr(readonly, "ApplicationRuntime", simulated_runtime)
    monkeypatch.setattr(
        model, "create", lambda c: KpluskFxeAdapter(replace(c, resource="192.0.2.10"), StreamingLibrary())
    )
    result = readonly.verify(
        config,
        tmp_path / "hardware-tests/KPLUSK-FXE-UNKNOWN/test",
        duration=1,
        input_condition="fixture; no hardware",
    )
    assert result["checks_passed"], result.get("error")
    assert result["stages"]["channel_selection"]["channels"] == (1, 3)
    assert result["stages"]["recording"]["committed"] > 0
    assert result["stages"]["physical_output"]["state"] == "not_tested"


def test_monitoring_records_every_frame_and_displays_selected_channels(qtbot, tmp_path):
    runtime, device, _ = connected_runtime(tmp_path)
    parent = QWidget()
    qtbot.addWidget(parent)
    controller = WorkspaceController(runtime, parent)
    controller.timer.stop()
    try:
        page = FrequencyCounterMonitoringPage(controller)
        qtbot.addWidget(page)
        page.select_device(device)
        page.show()
        page.channel_checks[2].setChecked(False)
        page.channel_checks[4].setChecked(False)
        request = AcquisitionRequest(kind="scalar", channels=page.channels(), quantity="frequency")
        runtime.monitoring.start(device, (request,), interval=0.05, recording=True)
        qtbot.waitUntil(lambda: runtime.monitoring.snapshots()[device]["sequence"] >= 5, timeout=5000)
        runtime.monitoring.stop(device).result(15)
        page.refresh_frame(device)
        assert page.plot.displayed_frame is not None
        assert {key[0] for key in page.plot.curves} == {1, 3}
        recorder = runtime.monitoring.tasks[device].recorder
        paths = recorder.finish().result(15)
        frames = [frame for path in paths for frame in read_frames(Path(path))]
        assert len(frames) >= 5
        assert len(frames) == recorder.snapshot()["committed"] == recorder.snapshot()["received"]
        assert not runtime.monitoring.snapshots()[device]["error"]
        assert runtime.sessions.capability_snapshot(device).channels.keys() == {1, 2, 3, 4}
    finally:
        controller.close()
        runtime.close()
