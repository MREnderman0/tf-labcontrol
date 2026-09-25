import threading

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.real_window import RealWindow


def test_pool_disconnect_during_background_read_drains_before_close(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://pool-read-drain", "rs.sma100b", "mock"))
    runtime.connect(key).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.close()
    adapter = runtime.sessions.sessions[key].adapter
    original = adapter.read_settings
    started, release = threading.Event(), threading.Event()
    calls = []

    def read(names, channel):
        started.set()
        assert release.wait(5)
        result = original(names, channel)
        calls.append("read_finished")
        return result

    adapter.read_settings = read
    adapter.release_control = lambda: calls.append("release")
    adapter.close = lambda: calls.append("close")
    try:
        future = runtime.sessions.try_submit_idle(key, "read_settings", (("frequency",), 1), quiet=True)
        assert started.wait(2)
        window.open_category("signal_generator")
        window.tree.setCurrentItem(window.rows[key])
        window.refresh()
        assert window.action_buttons["disconnect"].isEnabled()
        window.action_buttons["disconnect"].click()
        qtbot.waitUntil(lambda: runtime.sessions.sessions[key].closing)
        assert calls == [] and not future.done()
        release.set()
        qtbot.waitUntil(lambda: key not in runtime.sessions.sessions)
        future.result()
        assert calls == ["read_finished", "release", "close"]
    finally:
        release.set()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()


@pytest.mark.parametrize("flag", ["controlPending", "controlGroup", "monitoring", "recording"])
def test_pool_background_marker_does_not_unlock_active_work(qtbot, tmp_path, flag):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://pool-active-work", "rs.sma100b", "mock"))
    runtime.connect(key).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.close()
    try:
        window.open_category("signal_generator")
        window.tree.setCurrentItem(window.rows[key])
        window.states[key].update(connectionState="busy", backgroundRead=True, pending=1, **{flag: True})
        window.refresh_details()
        assert not window.action_buttons["disconnect"].isEnabled()
        assert not window.action_buttons["reconnect"].isEnabled()
    finally:
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()
