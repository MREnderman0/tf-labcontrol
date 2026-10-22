"""One cached snapshot per poll, with explicit refresh and safe GUI shutdown."""

from concurrent.futures import Future
from unittest.mock import Mock

import pytest

from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def workspace(qtbot, tmp_path):
    runtime = ApplicationRuntime((), root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    runtime.monitoring.enable_settings_sync(False)
    try:
        yield window, runtime
    finally:
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()


def test_poll_reads_once_and_rendering_uses_cache(workspace, monkeypatch):
    window, runtime = workspace
    inventory = Mock(wraps=runtime.inventory.all)
    sessions = Mock(wraps=runtime.sessions.snapshots)
    monkeypatch.setattr(runtime.inventory, "all", inventory)
    monkeypatch.setattr(runtime.sessions, "snapshots", sessions)
    window.controller.poll()
    assert inventory.call_count == sessions.call_count == 1
    window.controller._deliver_jobs()
    window._overview.refresh()
    window._connected_categories()
    window._submenu_categories("设备管理")
    assert inventory.call_count == sessions.call_count == 1
    window.refresh()
    assert inventory.call_count == sessions.call_count == 2


def test_finished_job_refreshes_before_callback(workspace, monkeypatch):
    window, runtime = workspace
    result = Future()
    observed = []
    window.controller.watch(result, lambda _: observed.append(window.controller.states))
    monkeypatch.setattr(runtime.sessions, "snapshots", lambda: ({"stableDeviceId": "fresh"},))
    result.set_result(None)
    window.controller._deliver_jobs()
    assert observed == [{"fresh": {"stableDeviceId": "fresh"}}]


@pytest.mark.parametrize("failed", [False, True])
def test_job_finished_during_snapshot_waits_for_fresh_delivery(workspace, monkeypatch, qtbot, failed):
    window, runtime = workspace
    result = Future()
    observed = []
    version = 1
    reads = 0

    def completed(value):
        observed.append((value, window.controller.states["device"]["configurationVersion"]))

    window.controller.watch(result, completed, completed)

    def snapshots():
        nonlocal version, reads
        reads += 1
        snapshot = ({"stableDeviceId": "device", "configurationVersion": version},)
        if not result.done():
            # A worker may publish and finish after this poll copied the old state.
            version = 2
            if failed:
                result.set_exception(ValueError("operation failed"))
            else:
                result.set_result("operation completed")
        return snapshot

    monkeypatch.setattr(runtime.sessions, "snapshots", snapshots)
    window.controller.poll()
    assert reads == 1
    assert observed == []
    assert len(window.controller.jobs) == 1

    # The wakeup queued by Future completion must deliver exactly once, after refresh.
    qtbot.waitUntil(lambda: len(observed) == 1)
    assert reads == 2
    assert len(observed) == 1
    assert observed[0][1] == 2
    if failed:
        assert isinstance(observed[0][0], ValueError)
    else:
        assert observed[0][0] == "operation completed"
    assert not window.controller.jobs
    window.controller._deliver_jobs()
    assert reads == 2 and len(observed) == 1


def test_poll_delivers_previously_finished_jobs_after_one_snapshot(workspace, monkeypatch):
    window, runtime = workspace
    result = Future()
    observed = []
    window.controller.watch(result, lambda _: observed.append(window.controller.states))
    result.set_result(None)
    inventory = Mock(wraps=runtime.inventory.all)
    sessions = Mock(return_value=({"stableDeviceId": "fresh"},))
    monkeypatch.setattr(runtime.inventory, "all", inventory)
    monkeypatch.setattr(runtime.sessions, "snapshots", sessions)

    window.controller.poll()
    assert observed == [{"fresh": {"stableDeviceId": "fresh"}}]
    assert inventory.call_count == sessions.call_count == 1
    window.controller._deliver_jobs()
    assert len(observed) == 1
    assert inventory.call_count == sessions.call_count == 1


def test_panel_shutdown_failure_still_closes_runtime(workspace, monkeypatch, qtbot):
    window, runtime = workspace
    calls = []

    def broken_panel():
        calls.append("storage")
        raise OSError("panel shutdown failed")

    monkeypatch.setattr(window.storage_panel, "shutdown", broken_panel)
    monkeypatch.setattr(window.environment_panel, "shutdown", lambda: calls.append("environment"))
    window.show()
    window.close()
    qtbot.waitUntil(lambda: runtime.closed and window.closing, timeout=5000)
    assert calls == ["storage", "environment"]
    assert "panel shutdown failed" in window.message.text()
