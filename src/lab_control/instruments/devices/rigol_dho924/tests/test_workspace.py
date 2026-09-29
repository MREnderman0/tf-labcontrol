import pytest

from lab_control.ui.real_window import RealWindow

from .test_sessions import runtime as runtime


@pytest.fixture
def page(runtime, qapp, qtbot):
    app, key, peer = runtime
    window = RealWindow(app)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", "oscilloscope", key)
    page = window.stack.currentWidget()
    qtbot.waitUntil(lambda: len(page.controls.values) > 25 and not page.controls.running, timeout=6000)
    try:
        yield page, app, key, peer
    finally:
        window.timer.stop()
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_discrete_points_memory_menu_and_live_control(page, qtbot):
    page, app, key, peer = page
    assert page.memory_action.isVisible()
    assert "10k" in page.controls.points_range.text()
    before = list(peer.history)
    assert page.controls.points.findData(2500) == -1
    page.controls.points.committed.emit(2500)
    assert peer.history == before
    page.controls.points.choose(page.controls.points.findData(100000))
    qtbot.waitUntil(lambda: not page.controls.running)
    assert peer.state[":ACQ:MDEP"] == 100000
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    assert "屏幕波形" in page.plot.meta.text()
    assert all(len(curve.getData()[0]) > 0 for curve in page.plot.curves.values())
    page.read_memory()
    qtbot.waitUntil(lambda: not page.memory_busy, timeout=6000)
    assert app.monitoring.snapshots()[key]["running"]
    assert peer.history.count(":STOP") == 1
    assert not app.sessions.snapshots()[0]["controlGroup"]
    app.monitoring.set_recording(key, True)
    page.controller.poll()
    page.refresh()
    assert not page.memory_action.isEnabled() and not page.controls.points.isEnabled()
