"""Service publication, hidden-page replay and readout/plot unit integration."""

import math
import time
from concurrent.futures import Future

import numpy as np
import pytest
from PySide6.QtCore import Qt

from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring_state import MonitorTask
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, Measurement, MeasurementBatch
from lab_control.ui.pages.monitoring.optical_power import RollingPowerPlot
from lab_control.ui.real_window import RealWindow


def publish(runtime, device, task, at, values, *, unit="W", revision=None, gap=False):
    session = runtime.sessions.sessions[device]
    request = AcquisitionRequest("scalar", channels=tuple(values), points=1, quantity="power")
    batch = MeasurementBatch(
        session.identity,
        tuple(Measurement("power", unit, value, channel) for channel, value in values.items()),
        {
            "_session_configuration_version": session.configuration_version,
            **({"_acquisition_gap": {}} if gap else {}),
        },
        performance={"received_at_monotonic": at},
    )
    task.pending = Future()
    task.pending.set_result(batch)
    task.pending_request = request
    task.pending_revision = task.request_revision if revision is None else revision
    task.last_started_at = time.monotonic()
    runtime.monitoring.scheduler.finish_batch(device, task, session.session_id)
    task.next_at = math.inf


@pytest.fixture
def workspace(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    device = runtime.add(ConnectionConfig("mock://power-window", "joinwit.jw8105", "mock"))
    runtime.connect(device).result(5)
    runtime.repository.save_workspace("monitoring_channels/optical_power_meter", {f"device:{device}": [1, 2]})
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.timer.stop()
    window.controller.timer.stop()
    window.navigate("实时监测", "optical_power_meter", device)
    page = window.stack.currentWidget()
    task = MonitorTask((page.request((1, 2)),), "general", 60)
    task.next_at = math.inf
    with runtime.monitoring.lock:
        runtime.monitoring.tasks[device] = task
    try:
        yield runtime, device, task, window, page
    finally:
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
        window.storage_panel.shutdown()
        window.environment_panel.shutdown()


def test_hidden_page_keeps_30_minutes_and_unit_switch_preserves_window(workspace, qtbot):
    runtime, device, task, window, page = workspace
    assert isinstance(page.plot, RollingPowerPlot)
    window.navigate("设备管理")
    origin = time.perf_counter() - 1700
    with runtime.monitoring.lock:
        for index in range(3401):
            publish(runtime, device, task, origin + index * 0.5, {1: 1e-3 if index == 0 else 1e-6, 2: 1e-5})
        assert len(task.frame_history[("scalar", "power")]) == 1200
    window.navigate("实时监测", "optical_power_meter", device)
    page.refresh()
    assert len(page.plot.curves[1].xData) == 3401
    assert page.cards[1].peak.text() == "0.000999 W"
    assert page.cards[2].peak.text() == "0 W"
    assert page.plot.plot.getAxis("bottom").labelUnits == "min"
    # A user's zoom is not reset when units change; all retained points convert.
    page.plot.plot.setXRange(2, 9, padding=0)
    before_x = page.plot.curves[1].xData.copy()
    before_requests = task.requests
    qtbot.mouseClick(page.cards[1].value, Qt.MouseButton.LeftButton)
    assert page.display_unit == "dBm" and page.plot.display_unit == "dBm"
    assert page.cards[1].value.text() == "-30.000 dBm"
    assert page.cards[2].value.text() == "-20.000 dBm"
    assert page.cards[1].peak.text() == "30.000 dB"
    assert page.cards[2].peak.text() == "0.000 dB"
    assert page.plot.plot.viewRange()[0] == pytest.approx([2, 9])
    assert np.array_equal(page.plot.curves[1].xData, before_x)
    assert page.plot.curves[1].yData[-1] == pytest.approx(-30)
    assert task.requests == before_requests
    assert runtime.repository.load_workspace("monitoring_display/optical_power_meter") == {"unit": "dBm"}
    page.select_device("")
    page.select_device(device)
    page.refresh()
    assert page.display_unit == "dBm" and page.cards[1].peak.text() == "30.000 dB"
    qtbot.mouseClick(page.cards[2].value, Qt.MouseButton.LeftButton)
    assert page.cards[1].peak.text() == "0.000999 W"


def test_channel_pause_revision_reconnect_and_all_unchecked_keep_history(workspace):
    runtime, device, task, _window, page = workspace
    origin = time.perf_counter() - 20
    with runtime.monitoring.lock:
        publish(runtime, device, task, origin, {1: 1, 2: 2})
        page.refresh()
        old_revision = task.request_revision
        page.channel_checks[1].setChecked(False)
        task.next_at = math.inf
        assert page.cards[1].isVisible() and page.cards[1].value.text() == "未监测"
        publish(runtime, device, task, origin + 1, {1: 999, 2: 999}, revision=old_revision)
        assert task.sequence == 1  # In-flight obsolete requests do not affect extrema.
        publish(runtime, device, task, origin + 2, {2: 3})
        page.channel_checks[1].setChecked(True)
        task.next_at = math.inf
        publish(runtime, device, task, origin + 3, {1: 4, 2: 5})
        page.refresh_frame(device)
        assert page.cards[1].peak.text() == "3 W"
        assert page.cards[2].peak.text() == "3 W"
        assert np.isnan(page.plot.curves[1].yData).sum() == 1
        assert np.isnan(page.plot.curves[2].yData).sum() == 0
        runtime.sessions.sessions[device].session_id = "recovered-session"
        publish(runtime, device, task, origin + 4, {1: 6, 2: 8}, gap=True)
        page.refresh_frame(device)
        assert page.cards[1].peak.text() == "5 W"
        assert np.isnan(page.plot.curves[1].yData).sum() == 2
        assert np.isnan(page.plot.curves[2].yData).sum() == 1
        for check in page.channel_checks.values():
            check.setChecked(False)
        assert task.running and not task.requests
        assert all(card.isVisible() and card.value.text() == "未监测" for card in page.cards.values())
        assert not any(curve.isVisible() for curve in page.plot.curves.values())
        page.channel_checks[1].setChecked(True)
        task.next_at = math.inf
        assert page.cards[1].peak.text() == "5 W"
    runtime.monitoring.stop(device).result(5)
    snapshot = runtime.monitoring.power_window(device)
    page.refresh()
    assert runtime.monitoring.power_window(device) == snapshot
    page.start()
    with runtime.monitoring.lock:
        assert runtime.monitoring.tasks[device] is not task
        assert runtime.monitoring.tasks[device].monitoring_context_id != snapshot.context_id


def test_log_zero_shows_no_infinite_readout_or_curve_point(workspace):
    runtime, device, task, _window, page = workspace
    origin = time.perf_counter() - 2
    with runtime.monitoring.lock:
        publish(runtime, device, task, origin, {1: 0, 2: 1e-6})
        publish(runtime, device, task, origin + 1, {1: 1e-6, 2: 0})
        page.refresh()
        page.toggle_display_unit()
    assert page.cards[1].value.text() == "-30.000 dBm"
    assert page.cards[1].peak.text() == "— dB"
    assert page.cards[2].value.text() == "— dBm"
    assert page.cards[2].peak.text() == "— dB"
    assert not np.isinf(page.plot.curves[1].yData).any()
    assert np.isnan(page.plot.curves[1].yData[0])


def test_device_switch_isolates_windows_and_retains_each_run(workspace):
    runtime, first, first_task, _window, page = workspace
    second = runtime.add(ConnectionConfig("mock://power-window-peer", "joinwit.jw8105", "mock"))
    runtime.connect(second).result(5)
    request = page.request((1, 2))
    second_task = MonitorTask((request,), "general", 60)
    second_task.next_at = math.inf
    now = time.perf_counter()
    with runtime.monitoring.lock:
        runtime.monitoring.tasks[second] = second_task
        publish(runtime, first, first_task, now - 2, {1: 1, 2: 2})
        publish(runtime, first, first_task, now - 1, {1: 4, 2: 3})
        publish(runtime, second, second_task, now - 2, {1: 10, 2: 20})
        publish(runtime, second, second_task, now - 1, {1: 12, 2: 21})
        page.select_device(second)
        page.refresh()
        assert page.cards[1].value.text() == "12 W" and page.cards[1].peak.text() == "2 W"
        page.select_device(first)
        page.refresh()
        assert page.cards[1].value.text() == "4 W" and page.cards[1].peak.text() == "3 W"


def test_live_automatic_reconnect_continues_same_power_window(tmp_path, qtbot):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        device = runtime.add(ConnectionConfig("mock://power-window-reconnect", "joinwit.jw8105", "mock"))
        runtime.connect(device).result(5)
        adapter = runtime.sessions.sessions[device].adapter
        original = adapter.acquire
        calls = 0

        def interrupted(request):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DeviceError("simulated interrupted optical power read", "timeout")
            return original(request)

        adapter.acquire = interrupted
        request = AcquisitionRequest("scalar", channels=(1, 2), points=1, quantity="power")
        runtime.monitoring.start(device, (request,), interval=0.05, record_interval=None)
        task = runtime.monitoring.tasks[device]
        context = task.monitoring_context_id
        qtbot.waitUntil(lambda: task.sequence >= 4, timeout=8000)
        runtime.monitoring.stop(device).result(8)
        snapshot = runtime.monitoring.power_window(device)
        assert snapshot.context_id == context
        assert len(snapshot.channels[0].points) >= 4
        segments = [point.segment for point in snapshot.channels[0].points]
        assert segments[0] == 0 and segments[1:] == [1] * (len(segments) - 1)
        assert snapshot.origin == snapshot.channels[0].points[0].at
