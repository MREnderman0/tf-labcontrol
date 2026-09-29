import threading

import pytest

from lab_control.domain.instruments import DeviceCommand

from .test_control_cache import prepare_auto
from .test_sessions import runtime as runtime
from .test_workspace import page as page


def test_verified_control_skips_page_rereads_and_resumes(page, qtbot):
    page, app, key, peer = page
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    old = page.plot.displayed_frame
    old_time = app.sessions.readback_frames(key)[2, "scale"].reading.timestamp
    peer.history.clear()
    page.controls.submit([DeviceCommand("offset", {"value": 0.01})])
    qtbot.waitUntil(lambda: not page.controls.running)
    qtbot.waitUntil(lambda: page.plot.displayed_frame.configuration_version > old.configuration_version)
    assert app.monitoring.snapshots()[key]["running"]
    assert page.plot.displayed_frame.batch.validation == "cached"
    unrelated = [
        q
        for q in peer.history
        if "?" in q
        and q not in {"*OPC?", ":CHAN1:OFFS?", ":SYST:ERR?", ":TRIG:EDGE:LEV?", ":WAV:PRE?", ":WAV:DATA?"}
    ]
    assert unrelated == []
    frame = app.sessions.readback_frames(key)[2, "scale"]
    assert frame.reading.timestamp == old_time and frame.reading.source == "control_cache"
    assert page.controls.values[2, "scale"] == 0.1


@pytest.mark.parametrize("monitor,channels", [(True, (1, 2)), (False, (1,)), (True, ())])
def test_auto_refreshes_channels_and_resumes_only_previous_monitor(page, qtbot, monitor, channels):
    page, app, key, peer = page
    prepare_auto(peer, channels=channels)
    if monitor:
        page.start()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    assert page.controls.auto_button.isEnabled()
    page.controls.auto_button.click()
    assert page.controls.auto_button.text() == "自动设置中…"
    qtbot.waitUntil(lambda: not page.controls.running)
    assert peer.history.count(":AUToset") == 1
    assert page.controls.channels() == channels
    assert bool(app.monitoring.snapshots().get(key, {}).get("running")) == bool(monitor and channels)
    assert page.controls.values[1, "timebase"] == 2e-6
    if monitor and channels:
        app.monitoring.set_recording(key, True)
        page.controller.poll()
        assert not page.controls.auto_button.isEnabled() and not page.controls.fine_button.isEnabled()
        app.monitoring.set_recording(key, False)
        qtbot.waitUntil(lambda: not app.sessions.snapshots()[0]["controlsLocked"])


def test_rejected_auto_verifies_and_resumes_without_retry(page, qtbot):
    page, app, key, peer = page
    prepare_auto(peer, enabled=False)
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    previous = page.plot.displayed_frame
    page.controls.auto_button.click()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert app.monitoring.snapshots()[key]["running"]
    assert ":AUToset" not in peer.history and "未完成" in page.controls.status.text()
    qtbot.waitUntil(lambda: page.plot.displayed_frame.configuration_version > previous.configuration_version)
    assert "实际参数已核对" in page.controls.status.text()


@pytest.mark.parametrize("cancel", [False, True])
def test_executed_auto_rejection_reads_actual_state_and_respects_stop(page, qtbot, monkeypatch, cancel):
    page, app, key, peer = page
    prepare_auto(peer)
    original = peer.query
    entered, release = threading.Event(), threading.Event()

    def query(command):
        if command == ":SYST:ERR?":
            entered.set()
            assert release.wait(4)
            return b'-200,"Command execute failed"'
        return original(command)

    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    peer.history.clear()
    monkeypatch.setattr(peer, "query", query)
    page.controls.auto_button.click()
    try:
        qtbot.waitUntil(entered.is_set)
        if cancel:
            page.toggle_monitoring()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert peer.history.count(":AUToset") == 1
    assert page.controls.values[1, "timebase"] == 2e-6
    assert app.monitoring.snapshots()[key]["running"] is not cancel
    assert "Command execute failed" in page.controls.status.text()


def test_auto_transport_failure_does_not_resume(page, qtbot):
    page, app, key, peer = page
    prepare_auto(peer)
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    peer.history.clear()
    peer.fail = "*OPC?"
    page.controls.auto_button.click()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert not app.monitoring.snapshots()[key]["running"]
    assert peer.history.count(":AUToset") == 1
    assert "timeout" in page.controls.status.text()
