from concurrent.futures import Future

import pytest
from PySide6.QtCore import QPoint, Qt

from lab_control.domain.instruments import Parameter
from lab_control.ui.pages.monitoring.oscilloscope_controls import ScopeSlider

from .test_oscilloscope_workspace import scope as scope


@pytest.mark.parametrize("division,grain", [(0.02, None), (1e-6, 1e-9), (0.05, 0.001)])
def test_coarse_fine_keyboard_relative_drag_and_fixed_endpoints(qtbot, division, grain):
    field = ScopeSlider("偏移", "V")
    qtbot.addWidget(field)
    field.resize(520, 80)
    field.show()
    spec = Parameter("number", "V", -division * 100, division * 100, resolution=grain)
    original = division * 0.037
    field.configure(original, spec, division)
    assert field.step >= division * 0.1 * (1 - 1e-12)
    field.set_fine(True)
    field.configure(original, spec, division)
    assert field.step >= division * 0.01 * (1 - 1e-12) and (not grain or field.step >= grain)
    changes = []
    field.committed.connect(changes.append)
    slider = field.slider
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(60, 8))
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(60, 8))
    assert changes == [] and field.actual == original
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(60, 8))
    field.fine_move(0.1)
    assert not field.dirty
    field.fine_move(100)
    assert 0 < field.value() - original <= division + division * 1e-9
    assert changes == []
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(160, 8))
    assert len(changes) == 1
    field.configure(changes[-1], spec, division)
    assert (field.low, field.high) == (spec.minimum, spec.maximum)
    base = field.actual
    qtbot.keyClick(slider, Qt.Key.Key_Right)
    qtbot.keyClick(slider, Qt.Key.Key_PageUp)
    assert len(changes) == 1
    qtbot.waitUntil(lambda: len(changes) == 2)
    assert changes[-1] == pytest.approx(base + 11 * field.step)


def test_fixed_footer_preference_and_no_instrument_write(scope, qtbot, monkeypatch):
    _, page, runtime, _, adapter = scope
    monkeypatch.setattr(adapter, "apply", lambda _: pytest.fail("Toggle cannot write hardware"))
    controls = page.controls
    footer_top = controls.fine_button.mapTo(controls, QPoint()).y()
    controls.scroller.verticalScrollBar().setValue(controls.scroller.verticalScrollBar().maximum())
    assert controls.fine_button.mapTo(controls, QPoint()).y() == footer_top
    assert not controls.fine_button.isChecked()
    controls.fine_button.click()
    assert runtime.repository.load_workspace(controls.fine_key) is True
    assert controls.sliders[1, "offset"].step == pytest.approx(controls.values[1, "scale"] * 0.01)
    assert controls.sliders[1, "time_position"].step == pytest.approx(controls.values[1, "timebase"] * 0.01)
    assert not controls.sliders[1, "scale"].fine


def test_future_completion_delivered_without_polling(scope, qtbot):
    window, _, _, _, _ = scope
    window.timer.stop()
    window.controller.timer.stop()
    future = Future()
    values = []
    window.controller.watch(future, values.append)
    future.set_result(42)
    qtbot.waitUntil(lambda: values == [42])
    window.controller._deliver_jobs()
    assert values == [42]


def test_normal_groove_press_waits_for_release_once(qtbot):
    field = ScopeSlider("偏移", "V")
    qtbot.addWidget(field)
    field.resize(520, 80)
    field.configure(0, Parameter("number", "V", -10, 10), 1)
    field.show()
    changes = []
    field.committed.connect(changes.append)
    position = QPoint(field.slider.width() - 4, 8)
    qtbot.mousePress(field.slider, Qt.MouseButton.LeftButton, pos=position)
    qtbot.wait(300)
    assert changes == []
    qtbot.mouseRelease(field.slider, Qt.MouseButton.LeftButton, pos=position)
    assert len(changes) == 1
