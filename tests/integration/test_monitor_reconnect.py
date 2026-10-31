"""Manual monitoring recovery retains device identity and never resumes an old run."""

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.real_window import RealWindow


def fail_connection(runtime, device):
    def fail():
        raise DeviceError("test connection lost", "timeout")

    runtime.sessions.sessions[device].adapter.read_status = fail
    with pytest.raises(DeviceError, match="test connection lost"):
        runtime.sessions.submit(device, "status").result(3)
    assert runtime.sessions.sessions[device].state == "degraded"


def test_manual_reconnect_keeps_leases_checks_identity_and_does_not_replay(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        device = runtime.add(ConnectionConfig("mock://manual-reconnect", "rigol.dsa815", "mock"))
        runtime.connect(device).result(3)
        session = runtime.sessions.sessions[device]
        old_adapter, old_id = session.adapter, session.session_id
        fail_connection(runtime, device)
        opened = []

        def factory(config):
            assert runtime.sessions.leases.owner(config.resource) == device
            adapter = SimulatedInstrument(config)
            adapter.apply = lambda _command: pytest.fail("Must not replay controls")
            adapter.acquire = lambda _request: pytest.fail("Must not resume sampling")
            adapter.release_control = lambda: pytest.fail("Must not send LOCAL during recovery")
            opened.append(adapter)
            return adapter

        old_adapter.release_control = lambda: pytest.fail("Must not send LOCAL on failed connection")
        runtime.sessions.factory = factory
        identity = runtime.sessions.reconnect(device).result(3)
        assert len(opened) == 1 and old_adapter.closed
        assert session.identity == identity and session.state == "connected"
        assert session.session_id != old_id
        assert not session.last_error and not session.recovery and not session.controls_locked
        assert not session.monitoring and device not in runtime.monitoring.snapshots()
        del opened[0].release_control


@pytest.mark.parametrize("failure", ["offline", "identity", "initialization"])
def test_manual_reconnect_failure_stays_retryable(tmp_path, failure):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        device = runtime.add(ConnectionConfig("mock://manual-failure", "rigol.dsa815", "mock"))
        runtime.connect(device).result(3)
        session = runtime.sessions.sessions[device]
        original_identity = session.identity
        fail_connection(runtime, device)
        calls = []

        def factory(config):
            calls.append(config)
            if failure == "offline":
                raise DeviceError("still offline", "connection_failed")
            adapter = SimulatedInstrument(config)
            if failure == "identity":
                adapter.identify = lambda: replace(original_identity, serial="OTHER-DEVICE")
            else:

                def prepare():
                    raise DeviceError("initialization uncertain", "timeout")

                adapter.prepare_acquisition = prepare
            return adapter

        runtime.sessions.factory = factory
        with pytest.raises(DeviceError):
            runtime.sessions.reconnect(device).result(3)
        assert len(calls) == 1
        assert session.state == "degraded" and session.last_error
        assert session.identity == original_identity
        assert not session.pending and not session.recovery and not session.controls_locked
        assert runtime.sessions.leases.owner(session.config.resource) == device
        runtime.sessions.factory = SimulatedInstrument
        runtime.sessions.reconnect(device).result(3)
        assert session.state == "connected"


def test_manual_reconnect_is_single_flight_and_keeps_peer_connected(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        target = runtime.add(ConnectionConfig("mock://target", "rigol.dsa815", "mock"))
        peer = runtime.add(ConnectionConfig("mock://peer", "rigol.dsa815", "mock"))
        for key in (target, peer):
            runtime.connect(key).result(3)
        fail_connection(runtime, target)
        entered, release = threading.Event(), threading.Event()

        def factory(config):
            entered.set()
            assert release.wait(3)
            return SimulatedInstrument(config)

        runtime.sessions.factory = factory
        future = runtime.sessions.reconnect(target)
        try:
            assert entered.wait(2)
            assert not future.cancel()
            with pytest.raises(DeviceError) as caught:
                runtime.sessions.reconnect(target)
            assert caught.value.code == "task_busy"
            runtime.sessions.submit(peer, "status").result(2)
            assert runtime.sessions.sessions[peer].state == "connected"
        finally:
            release.set()
        future.result(3)


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
    ],
)
def test_monitor_button_reconnects_selected_device_then_returns_to_start(qtbot, tmp_path, adapter, category):
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
    device = runtime.add(ConnectionConfig("mock://ui-reconnect", adapter, "mock"))
    runtime.connect(device).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", category, device)
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
    entered, release = threading.Event(), threading.Event()
    try:
        runtime.monitoring.enable_settings_sync(False)
        qtbot.waitUntil(lambda: not runtime.sessions.sessions[device].pending)
        fail_connection(runtime, device)
        window.controller.poll()
        assert page.selector.device_id == device
        assert button.text() == "重连" and button.isEnabled()
        assert "test connection lost" in button.toolTip()

        failures = []

        def offline(config):
            failures.append(config)
            raise DeviceError("device still offline", "connection_failed")

        runtime.sessions.factory = offline
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: "device still offline" in window.message.text())
        assert button.text() == "重连" and button.isEnabled()
        assert len(failures) == 1 and page.selector.device_id == device

        def factory(config):
            entered.set()
            assert release.wait(5)
            return instrument(config)

        runtime.sessions.factory = factory
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(entered.is_set)
        assert button.text() == "重连中…" and not button.isEnabled()
        assert page.selector.device_id == device
        release.set()
        qtbot.waitUntil(lambda: button.text() == "开始监测", timeout=5000)
        assert page.selector.device_id == device
        assert runtime.sessions.sessions[device].state == "connected"
        assert not runtime.monitoring.snapshots().get(device, {}).get("running")
        assert "设备已重新连接" in window.message.text()
    finally:
        release.set()
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.bridge.worker.shutdown(wait=True)
        window.closing = True
        window.close()


def test_active_monitor_cannot_be_manually_reconnected(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        device = runtime.add(ConnectionConfig("mock://active-reconnect", "rigol.dsa815", "mock"))
        runtime.connect(device).result(3)
        runtime.monitoring.start(device, (AcquisitionRequest("spectrum", points=32),), interval=1)
        with pytest.raises(DeviceError) as caught:
            runtime.sessions.reconnect(device)
        assert caught.value.code == "task_busy"
        assert runtime.monitoring.snapshots()[device]["running"]
        runtime.monitoring.stop(device).result(3)


@pytest.mark.parametrize(
    "task,blocked",
    [
        ({"pending": True}, True),
        ({"recording_preparing": True}, True),
        ({"recording": True}, True),
        ({"recording_state": {"binary_done": False}}, True),
        ({"recording_state": {"binary_done": True, "converted": 0, "committed": 4}}, False),
    ],
)
def test_reconnect_waits_for_binary_drain_but_not_csv(qtbot, task, blocked):
    button = QPushButton("开始监测")
    qtbot.addWidget(button)
    controller = SimpleNamespace(
        states={"device": {"connectionState": "degraded", "identity": {"model": "test"}}}
    )
    refresh_reconnect_button(button, controller, "device", task)
    assert button.text() == "重连" and button.isEnabled() is not blocked


def test_running_automatic_recovery_keeps_stop_button(qtbot):
    button = QPushButton("停止监测")
    qtbot.addWidget(button)
    controller = SimpleNamespace(
        states={"device": {"connectionState": "degraded", "identity": {"model": "test"}}}
    )
    refresh_reconnect_button(button, controller, "device", {"running": True, "recovering": True})
    assert button.text() == "停止监测" and button.isEnabled()
