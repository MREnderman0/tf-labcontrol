import math
import threading
import time
from dataclasses import replace

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.storage.recording import read_frames
from lab_control.ui.pages.monitoring.optical_power import (
    OpticalPowerMonitoringPage,
    power_to_watts,
)
from lab_control.ui.real_window import RealWindow


def wait_for(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("optical power workspace did not reach the expected state")


def test_power_conversion_preserves_unit_semantics():
    assert power_to_watts(1, "W") == 1
    assert math.isclose(power_to_watts(-30, "dBm"), 1e-6)
    assert power_to_watts(float("nan"), "W") is None
    assert power_to_watts(1, "dBV") is None


def test_jw8105_all_channel_monitoring_records_and_filters(qapp, qtbot, tmp_path, monkeypatch):
    config = ConnectionConfig("mock://jw8105-workspace", "joinwit.jw8105", "mock")
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    device = runtime.add(config)
    runtime.connect(device).result(5)
    adapter = runtime.sessions.sessions[device].adapter
    acquire = adapter.acquire
    settings_reads = []
    read_settings = adapter.read_settings

    def track_settings(operations, channel=1):
        settings_reads.append((operations, channel))
        return read_settings(operations, channel)

    monkeypatch.setattr(adapter, "read_settings", track_settings)

    def dBm_acquire(request):
        batch = acquire(request)
        return replace(
            batch,
            measurements=tuple(
                replace(
                    measurement,
                    unit="dBm",
                    values=30 + 10 * math.log10(float(measurement.values[0])),
                )
                for measurement in batch.measurements
            ),
        )

    monkeypatch.setattr(adapter, "acquire", dBm_acquire)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        window.navigate("设备管理", "optical_power_meter", device)
        page = window.stack.currentWidget()
        assert isinstance(page, OpticalPowerMonitoringPage)
        assert window.current_module == "实时监测"
        assert runtime.monitoring.settings_sync.focus == device
        assert runtime.monitoring.settings_sync.advanced == ()
        assert not hasattr(page, "parameter_card")
        assert set(page.channel_checks) == set(range(1, 17))
        assert all(check.isChecked() for check in page.channel_checks.values())
        assert page.start_button.isEnabled()

        page.timing.interval.setValue(0.5)
        page.start()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None, timeout=8000)
        task = runtime.monitoring.tasks[device]
        assert not task.recording and task.record_interval is None
        assert task.requests[0].quantity == "power"
        assert task.requests[0].channels == tuple(range(1, 17))
        assert len(page.plot.curves) == 16
        assert sum(card.isVisible() for card in page.cards.values()) == 16
        measurement = page.plot.displayed_frame.batch.measurements[0]
        assert measurement.unit == "dBm"
        assert math.isclose(
            power_to_watts(measurement.values, measurement.unit),
            float(page.cards[measurement.channel].value.text().split()[0]),
            rel_tol=1e-5,
        )
        # Monitoring stays active while the page is hidden. Returning to it
        # must replay the bounded service history instead of painting only
        # the newest frame and losing the elapsed trend.
        points_before_hide = len(page.plot.curves[1].xData)
        sequence_before_hide = runtime.monitoring.tasks[device].sequence
        window.navigate("设备管理")
        qtbot.waitUntil(
            lambda: runtime.monitoring.tasks[device].sequence >= sequence_before_hide + 3,
            timeout=8000,
        )
        sequence_while_hidden = runtime.monitoring.tasks[device].sequence
        window.navigate("实时监测", "optical_power_meter", device)
        qtbot.waitUntil(
            lambda: (
                page.plot.displayed_frame is not None and len(page.plot.curves[1].xData) > points_before_hide
            ),
            timeout=8000,
        )
        assert runtime.monitoring.tasks[device].sequence >= sequence_while_hidden
        assert page.plot.displayed_frame.sequence >= sequence_while_hidden
        # Recording is owned by the monitoring service, so changing pages
        # must not pause or thin the binary frame stream.
        page.record()
        qtbot.waitUntil(lambda: runtime.monitoring.tasks[device].recording, timeout=8000)
        qtbot.waitUntil(
            lambda: runtime.monitoring.tasks[device].recorder.snapshot()["committed"] >= 1,
            timeout=8000,
        )
        committed_before_hide = runtime.monitoring.tasks[device].recorder.snapshot()["committed"]
        sequence_before_record_hide = runtime.monitoring.tasks[device].sequence
        window.navigate("设备管理")
        qtbot.waitUntil(
            lambda: runtime.monitoring.tasks[device].sequence >= sequence_before_record_hide + 3,
            timeout=8000,
        )
        qtbot.waitUntil(
            lambda: runtime.monitoring.tasks[device].recorder.snapshot()["committed"] > committed_before_hide,
            timeout=8000,
        )
        window.navigate("实时监测", "optical_power_meter", device)
        qtbot.waitUntil(lambda: page.isVisible(), timeout=3000)
        page.record()
        qtbot.waitUntil(lambda: not runtime.monitoring.tasks[device].recording, timeout=8000)
        page.channel_checks[8].setChecked(False)
        saved_channels = runtime.repository.load_workspace("monitoring_channels/optical_power_meter")
        channel_key = page._channel_key(device)
        assert saved_channels[channel_key] == [1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16]
        page.select_device("")
        page.select_device(device)
        assert page.channels() == (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16)
        assert page.readout_grid.itemAt(0).widget() is page.cards[1]
        assert page.readout_grid.itemAt(1).widget() is page.cards[2]
        assert page.readout_grid.itemAtPosition(0, 1).widget() is page.cards[2]
        assert page.readout_grid.itemAtPosition(1, 0).widget() is page.cards[3]
        assert page.readout_scroll.horizontalScrollBarPolicy().name == "ScrollBarAlwaysOff"
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None, timeout=8000)
        assert page.cards[1].value.toolTip() == page.cards[1].value.text()
        assert page.record_button.text() == "开始记录"
        page.record()
        qtbot.waitUntil(lambda: runtime.monitoring.tasks[device].recording, timeout=8000)
        qtbot.waitUntil(
            lambda: runtime.monitoring.tasks[device].recorder.snapshot()["committed"] >= 1,
            timeout=8000,
        )
        assert page.record_button.text() == "停止记录"
        assert page.chart.property("recordingPhase") == "recording"
        page.timing.interval.setValue(5)
        page.timing.commit()
        assert runtime.monitoring.tasks[device].interval == 5

        page.channel_checks[8].setChecked(False)
        qtbot.waitUntil(
            lambda: (
                runtime.monitoring.tasks[device].requests[0].channels
                == (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16)
            ),
            timeout=8000,
        )
        assert runtime.monitoring.tasks[device].running
        assert runtime.monitoring.tasks[device].recording
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None, timeout=8000)
        assert not page.cards[8].isVisible()
        assert 8 not in page.plot.curves

        for check in page.channel_checks.values():
            check.setChecked(False)
        qtbot.waitUntil(lambda: runtime.monitoring.tasks[device].requests == (), timeout=8000)
        assert page.channels() == ()
        assert runtime.monitoring.snapshots()[device]["running"]
        assert all(not card.isVisible() for card in page.cards.values())
        page.channel_checks[1].setChecked(True)
        qtbot.waitUntil(lambda: runtime.monitoring.tasks[device].requests[0].channels == (1,), timeout=8000)
        assert runtime.monitoring.tasks[device].running
        page.record()
        qtbot.waitUntil(lambda: not runtime.monitoring.tasks[device].recording, timeout=8000)
        page.toggle_monitoring()
        qtbot.waitUntil(lambda: not runtime.monitoring.snapshots()[device]["running"], timeout=8000)
        runtime.monitoring.stop(device).result(8)
        previous_origin = page.plot.time_origin
        page.start()
        qtbot.waitUntil(
            lambda: page.plot.displayed_frame is not None and page.plot.time_origin != previous_origin,
            timeout=8000,
        )
        assert page.plot.curves[1].xData[0] == 0
        page.toggle_monitoring()
        qtbot.waitUntil(lambda: not runtime.monitoring.snapshots()[device]["running"], timeout=8000)
        runtime.monitoring.stop(device).result(8)
        paths = list(runtime.repository.data_root.glob("*/**/*_continuous_*/_raw/segment-*.sqlite3"))
        assert paths
        saved = list(read_frames(paths[0]))
        assert saved and saved[0]["measurements"][0]["unit"] == "dBm"
        assert settings_reads == []
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_request_refresh_discards_inflight_frame_without_stopping_monitoring(tmp_path, monkeypatch):
    config = ConnectionConfig("mock://jw8105-refresh", "joinwit.jw8105", "mock")
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    device = runtime.add(config)
    runtime.connect(device).result(5)
    adapter = runtime.sessions.sessions[device].adapter
    acquire = adapter.acquire
    started = threading.Event()
    release = threading.Event()
    calls = []

    def delayed_acquire(request):
        calls.append(request)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return acquire(request)

    monkeypatch.setattr(adapter, "acquire", delayed_acquire)
    try:
        runtime.monitoring.start(
            device,
            (AcquisitionRequest("scalar", channels=tuple(range(1, 17)), points=1, quantity="power"),),
            interval=0.05,
            record_interval=None,
            mode="optical",
            recording=True,
        )
        assert started.wait(5)
        task = runtime.monitoring.tasks[device]
        assert task.running and task.recording
        runtime.monitoring.replace_requests(
            device,
            (AcquisitionRequest("scalar", channels=(1,), points=1, quantity="power"),),
        )
        assert task.running and task.recording
        assert task.requests[0].channels == (1,)
        release.set()
        wait_for(lambda: len(calls) >= 2)
        wait_for(
            lambda: runtime.monitoring.latest(device, "scalar", "power") is not None,
        )
        frame = runtime.monitoring.latest(device, "scalar", "power")
        assert frame is not None and frame.request.channels == (1,)
        wait_for(lambda: task.recorder.snapshot()["committed"] >= 1)
        runtime.monitoring.stop(device).result(8)
        paths = list(runtime.repository.data_root.glob("*/**/*_continuous_*/_raw/segment-*.sqlite3"))
        assert paths
        saved = list(read_frames(paths[0]))
        assert saved
        assert all(
            {measurement["channel"] for measurement in frame["measurements"]} <= {1} for frame in saved
        )
    finally:
        runtime.close()
