from concurrent.futures import Future

import numpy as np

from .test_sessions import runtime as runtime
from .test_workspace import page as page


def test_drag_previews_without_io_and_saves_actual_frame(page, qtbot, monkeypatch):
    page, app, key, peer = page
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    app.monitoring.stop(key).result(5)
    page.controller.poll()
    page.refresh_frame(key)
    old = page.plot.displayed_frame
    before = list(peer.history)
    field = page.controls.sliders[1, "offset"]
    field.slider.setSliderDown(True)
    field.keyboard_step(2)
    qtbot.waitUntil(lambda: "估算" in page.plot.meta.text())
    assert peer.history == before
    assert page.plot.displayed_frame is old
    saved = []

    def export(_key, frame, mode):
        saved.append(frame)
        future = Future()
        future.set_result([])
        return future

    monkeypatch.setattr(app.monitoring, "export_displayed_frame", export)
    page.save()
    assert saved == [old]
    np.testing.assert_array_equal(saved[0].batch.measurements[0].values, old.batch.measurements[0].values)
    field.slider.setSliderDown(False)
    qtbot.waitUntil(lambda: not page.controls.running)
    assert peer.history.count(":CHAN1:OFFS 0.01") == 1
    assert "等待实测" in page.plot.meta.text()  # Software monitoring was stopped.
    page.start()
    qtbot.waitUntil(lambda: not page.plot.preview_targets)
    assert "估算" not in page.plot.meta.text()


def test_failed_setting_restores_view_and_horizontal_drag_is_immediate(page, qtbot):
    page, app, key, peer = page
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    app.monitoring.stop(key).result(5)
    page.controller.poll()
    page.refresh_frame(key)
    field = page.controls.sliders[1, "time_position"]
    bounds = page.plot.grid_range
    field.slider.setSliderDown(True)
    field.keyboard_step(2)
    qtbot.waitUntil(lambda: page.plot.grid_range != bounds)
    peer.fail = ":TIM:MAIN:OFFS 2e-07"
    field.slider.setSliderDown(False)
    qtbot.waitUntil(lambda: not page.controls.running)
    assert "未完成" in page.controls.status.text()
    assert not page.plot.preview_targets and page.plot.grid_range == bounds
