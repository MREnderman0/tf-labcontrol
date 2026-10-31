"""Each workspace waits for the same stopped-task drain, independently of CSV."""

import threading

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import DeviceError
from lab_control.storage.recording_workers import CsvConversionSupervisor
from lab_control.ui.real_window import RealWindow


@pytest.mark.parametrize(
    "adapter,category",
    [
        ("rigol.dsa815", "spectrum_analyzer"),
        ("rs.rtm3004", "oscilloscope"),
        ("gwinstek.gpd4303s", "power_supply"),
        ("joinwit.jw8105", "optical_power_meter"),
        ("ceyear.6362d", "optical_spectrometer"),
        ("keysight.53220a", "frequency_counter"),
        ("rigol.dg5504pro", "signal_generator"),
        ("rigol.m300", "multimeter"),
        ("rs.znb3004", "network_analyzer"),
    ],
)
@pytest.mark.parametrize("slow_stage", ["read", "write"])
def test_single_device_start_disabled_until_stop_drains(
    qtbot, tmp_path, monkeypatch, adapter, category, slow_stage
):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)

    def instrument(config):
        result = SimulatedInstrument(config)
        if adapter == "rigol.m300":
            result.capability_details = {
                "acquisition_channel_selection": "scan_list",
                "acquisition_channels": [1],
            }
        return result

    runtime.sessions.factory = instrument
    key = runtime.add(ConnectionConfig("mock://stop-controls", adapter, "mock"))
    runtime.connect(key).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", category, key)
    page = window.stack.currentWidget()
    if getattr(page, "scan_page", None) is not None:
        page = page.scan_page
    button = (
        page.buttons["start"]
        if hasattr(page, "buttons")
        else page.toggle_button
        if hasattr(page, "toggle_button")
        else page.start_button
    )
    service = runtime.monitoring
    entered, release, release_csv = threading.Event(), threading.Event(), threading.Event()
    factory = runtime.repository.create_recording
    convert = CsvConversionSupervisor.run

    def hold_csv(supervisor):
        supervisor.recording.thread.join()
        assert release_csv.wait(15)
        convert(supervisor)

    def create(*args, **kwargs):
        recorder = factory(*args, **kwargs)
        append = recorder.segments.append

        def write(sequence, batch):
            entered.set()
            assert release.wait(15)
            return append(sequence, batch)

        recorder.segments.append = write
        return recorder

    try:
        runtime.monitoring.enable_settings_sync(False)
        qtbot.waitUntil(lambda: not runtime.sessions.sessions[key].pending)
        qtbot.waitUntil(button.isEnabled, timeout=5000)
        grouped, _ = window.dashboard._requests()
        requests = next(requests for device, requests, *_ in grouped if device == key)
        if slow_stage == "read":
            instance = runtime.sessions.sessions[key].adapter
            method = "read_settings" if requests[0].kind == "status" else "acquire"
            acquire = getattr(instance, method)

            def read(*args, **kwargs):
                entered.set()
                assert release.wait(15)
                return acquire(*args, **kwargs)

            monkeypatch.setattr(instance, method, read)
        else:
            monkeypatch.setattr(runtime.repository, "create_recording", create)
        monkeypatch.setattr(CsvConversionSupervisor, "run", hold_csv)
        service.start(key, requests, interval=1, recording=True, record_interval=None)
        qtbot.waitUntil(entered.is_set, timeout=5000)
        window.controller.poll()
        # Drive the device page's stop control, including the generic split buttons.
        stop_button = page.buttons["stop"] if hasattr(page, "buttons") else button
        assert stop_button.isEnabled()
        stop_button.click()
        window.controller.poll()
        assert service.snapshots()[key]["stopping"]
        assert button.text() == "正在停止…" and not button.isEnabled()
        if hasattr(page, "buttons"):
            assert not page.buttons["once"].isEnabled()
            assert not page.buttons["record"].isEnabled()
        previous = service.tasks[key]
        button.click()
        assert service.tasks[key] is previous and not previous.running
        with pytest.raises(DeviceError) as caught:
            service.start(key, requests)
        assert caught.value.code == "task_busy"
        release.set()
        qtbot.waitUntil(lambda: not service.snapshots()[key]["stopping"], timeout=5000)
        window.controller.poll()
        qtbot.waitUntil(button.isEnabled, timeout=5000)
        assert not previous.recorder.finished.done()
        assert "开始监测" == button.text()
        button.click()
        qtbot.waitUntil(lambda: service.snapshots()[key]["running"], timeout=5000)
        assert service.tasks[key] is not previous
    finally:
        release.set()
        release_csv.set()
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.bridge.worker.shutdown(wait=True)
        window.closing = True
        window.close()
