from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.acquisition_pipeline import immutable_batch
from lab_control.application.controls import DataSource
from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.ui.pages.monitoring.plot import LivePlot

SOURCE = DataSource("waveform", "时域波形", "waveform")


def waveform(
    span=10e-6, *, n=5002, channels=(1,), version=1, config=None, shift=0.0, gain=1.0, session="one"
):
    x = np.linspace(-span / 2, span / 2, n) + shift
    measurements = tuple(
        Measurement("waveform", "V", gain * np.sin(np.linspace(0, 10, n)) + ch, ch, x, "s") for ch in channels
    )
    batch = immutable_batch(
        MeasurementBatch(
            DeviceIdentity("Mock", "Scope", "fixture"),
            measurements,
            {"timebase": span / 10} if config is None else config,
            validation="cached",
            settings_timestamps={"timebase": "original"},
            performance={"requested_points_is_not_a_hardware_limit": True},
        )
    )
    return MonitorFrame(1, version, AcquisitionRequest("waveform", channels), batch, session_id=session)


@pytest.fixture
def plot(qtbot):
    widget = LivePlot()
    widget.resize(1000, 500)
    qtbot.addWidget(widget)
    widget.show()
    return widget


@pytest.mark.parametrize("span", [1e-9, 1e-6, 1e-3])
def test_short_timebase_has_rendered_points_on_first_frame(plot, qtbot, span):
    frame = waveform(span)
    plot.show_frame(frame, SOURCE, (1,))
    qtbot.wait(20)
    curve = next(iter(plot.curves.values()))
    assert len(curve.getData()[0]) > 1
    assert plot.plot.viewRange()[0] == pytest.approx([-0.52 * span, 0.52 * span], abs=1e-20)
    assert not curve.curve.getPath().isEmpty()
    assert plot.plot.getAxis("bottom").autoSIPrefixScale > 1
    np.testing.assert_array_equal(curve.xData, frame.batch.measurements[0].axis)
    np.testing.assert_array_equal(
        plot.displayed_frame.batch.measurements[0].values, frame.batch.measurements[0].values
    )
    assert "5002 点" in plot.meta.text() and "完整波形" in plot.meta.text()
    assert not plot.displayed_frame.batch.measurements[0].values.flags.writeable


def test_multichannel_refits_only_for_configuration_and_selection(plot):
    frame = waveform(channels=(1, 2, 3, 4))
    plot.show_frame(frame, SOURCE, (1, 2, 3, 4))
    assert all(len(c.getData()[0]) > 0 for c in plot.curves.values())
    assert plot.plot.viewRange()[1][0] < 0 and plot.plot.viewRange()[1][1] > 5 - 1e-5
    plot.plot.setXRange(-2e-6, 2e-6, padding=0)
    plot.plot.setYRange(-2, 10, padding=0)
    before = plot.plot.viewRange()
    changed = waveform(
        channels=(1, 2, 3, 4),
        shift=1e-12,
        gain=3,
        config={**frame.batch.configuration, "monitor_record_session": "recording"},
    )
    plot.show_frame(
        replace(changed, batch=replace(changed.batch, settings_timestamps={"timebase": "new"})),
        SOURCE,
        (1, 2, 3, 4),
    )
    np.testing.assert_allclose(plot.plot.viewRange(), before)
    # Software/front-panel refresh changes actual settings even at the same version.
    plot.show_frame(
        waveform(channels=(1, 2, 3, 4), config={"timebase": 1e-6, "offset": 2}), SOURCE, (1, 2, 3, 4)
    )
    assert plot.plot.viewRange()[1] != before[1]
    plot.show_frame(frame, SOURCE, (4,))
    assert len(plot.displayed_frame.batch.measurements) == 1
    assert plot.plot.viewRange()[1][0] > 2
    assert not plot.curves[(1, "waveform")].isVisible()


def test_empty_decimated_view_can_fit_full_snapshot_and_survive_hide(plot, qtbot):
    frame = waveform()
    plot.show_frame(frame, SOURCE, (1,))
    plot.plot.setXRange(-1, 1, padding=0)
    plot.plot.setYRange(-10, 10, padding=0)
    plot.fit_current()
    qtbot.wait(10)
    assert len(next(iter(plot.curves.values())).getData()[0]) > 1
    assert plot.plot.viewRange()[0] == pytest.approx([-5.2e-6, 5.2e-6])
    plot.hide()
    plot.show_frame(waveform(gain=4), SOURCE, (1,))
    view = plot.plot.viewRange()
    plot.show()
    qtbot.wait(10)
    np.testing.assert_allclose(plot.plot.viewRange(), view)
    plot.show_frame(waveform(gain=4, session="reconnected"), SOURCE, (1,))
    assert plot.plot.viewRange()[1] != view[1]
    plot.show_frame(waveform(span=1e-3, version=2), SOURCE, (1,))
    assert plot.plot.viewRange()[0] == pytest.approx([-0.00052, 0.00052])


def test_invalid_frame_preserves_fit_and_units_single_point_constant(plot):
    first = waveform()
    plot.show_frame(first, SOURCE, (1,))
    bad = replace(
        first,
        configuration_version=2,
        batch=replace(
            first.batch, measurements=(replace(first.batch.measurements[0], values=np.full(5002, np.nan)),)
        ),
    )
    plot.show_frame(bad, SOURCE, (1,))
    assert plot.displayed_frame is not None and plot.displayed_frame.configuration_version == 1
    one = waveform(n=1, version=2)
    plot.show_frame(one, SOURCE, (1,))
    curve = next(iter(plot.curves.values()))
    assert curve.opts["symbol"] == "o" and len(curve.getData()[0]) == 1
    assert curve.xData[0] == one.batch.measurements[0].axis[0]  # not a scalar history
    assert plot.plot.viewRange()[1][0] < 1 < plot.plot.viewRange()[1][1]
    constant = waveform(n=20, gain=0.0, version=3)
    constant = replace(
        constant,
        batch=replace(constant.batch, measurements=(replace(constant.batch.measurements[0], unit="mV"),)),
    )
    plot.show_frame(constant, SOURCE, (1,))
    assert curve.opts["symbol"] is None
    assert plot.plot.viewRange()[1][0] < 1 < plot.plot.viewRange()[1][1]
    assert plot.plot.getAxis("left").labelUnits == "mV"
    plot.reset()
    assert plot.displayed_frame is None and not plot.trace_view.active
