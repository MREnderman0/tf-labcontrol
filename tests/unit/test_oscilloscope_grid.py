from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.acquisition_pipeline import immutable_batch
from lab_control.application.controls import DataSource
from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.domain.oscilloscope import ScopeDisplay
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot

SOURCE = DataSource("waveform", "时域波形", "waveform")


def frame(span=10e-6, *, n=5002, offset=0, version=1, channels=(1, 2, 3, 4), constant=False):
    x = np.linspace(-span / 2, span / 2, n)
    measurements = tuple(
        Measurement(
            "waveform",
            "V",
            np.full(n, 0.02) if constant else np.sin(np.linspace(0, 20, n)) * ch * 0.01,
            ch,
            x,
            "s",
            metadata={"scope_display": ScopeDisplay(ch * 0.01, -offset, span / 10, 0).metadata()},
        )
        for ch in channels
    )
    return MonitorFrame(
        version,
        version,
        AcquisitionRequest("waveform", channels),
        immutable_batch(
            MeasurementBatch(
                DeviceIdentity("MOCK", "SCOPE", "FIXTURE"),
                measurements,
                {"version": version},
                validation="full",
            )
        ),
        session_id="test",
    )


@pytest.fixture
def plot(qtbot):
    widget = OscilloscopePlot()
    widget.resize(900, 500)
    qtbot.addWidget(widget)
    widget.show()
    return widget


@pytest.mark.parametrize("span", [10e-9, 10e-6, 10e-3])
def test_fixed_grid_actual_display_points_and_independent_channel_scales(plot, qtbot, span):
    sample = frame(span)
    plot.show_frame(sample, SOURCE, (1, 2, 3, 4))
    qtbot.wait(20)
    assert not plot.plot.getAxis("left").isVisible()
    assert plot.plot.viewRange()[0] == pytest.approx([-span / 2, span / 2], abs=1e-20)
    assert plot.plot.viewRange()[1] == pytest.approx([-4, 4])
    assert len(plot.grid_lines) == 20
    for measurement in sample.batch.measurements:
        curve = plot.curves[measurement.channel, "waveform"]
        assert len(curve.getData()[0]) > 1 and not curve.curve.getPath().isEmpty()
        assert np.max(curve.yData) == pytest.approx(1, abs=1e-5)
        np.testing.assert_array_equal(curve.xData, measurement.axis)
    assert plot.displayed_frame.batch is not sample.batch  # Selected immutable snapshot.
    for actual, original in zip(plot.displayed_frame.batch.measurements, sample.batch.measurements):
        np.testing.assert_array_equal(actual.values, original.values)
        assert not actual.values.flags.writeable
    assert plot.plot.getAxis("bottom").autoSIPrefixScale > 1


def test_offset_and_new_scale_use_own_frame_while_invalid_frame_preserves_grid(plot):
    old = frame(offset=0.02, constant=True)
    plot.show_frame(old, SOURCE, (1,))
    assert plot.curves[1, "waveform"].yData[0] == pytest.approx(4)
    previous = plot.displayed_frame
    bad = replace(old.batch.measurements[0], metadata={"scope_display": {"unit": "V"}})
    plot.show_frame(replace(old, sequence=2, batch=replace(old.batch, measurements=(bad,))), SOURCE, (1,))
    assert plot.displayed_frame is previous
    plot.show_frame(frame(span=20e-6, version=3, offset=0, constant=True), SOURCE, (1,))
    assert plot.curves[1, "waveform"].yData[0] == pytest.approx(2)
    assert plot.plot.viewRange()[0] == pytest.approx([-10e-6, 10e-6])


def test_stable_jitter_hidden_restore_and_single_point(plot, qtbot):
    first = frame(channels=(1,))
    plot.show_frame(first, SOURCE, (1,))
    bounds = plot.plot.viewRange()
    shifted = replace(first.batch.measurements[0], axis=first.batch.measurements[0].axis + 1e-10)
    plot.hide()
    plot.show_frame(
        replace(first, sequence=2, batch=replace(first.batch, measurements=(shifted,))), SOURCE, (1,)
    )
    plot.show()
    assert plot.plot.viewRange() == bounds
    plot.plot.setRange(xRange=(0, 1), yRange=(-1, 1), padding=0)
    plot.fit_current()
    qtbot.wait(10)
    assert plot.plot.viewRange() == bounds
    plot.reset()
    plot.show_frame(frame(n=1, channels=(1,)), SOURCE, (1,))
    assert plot.curves[1, "waveform"].opts["symbol"] == "o"


def test_legacy_missing_metadata_falls_back_but_nan_never_replaces_frame(plot):
    sample = frame(channels=(1,))
    plain = replace(sample.batch.measurements[0], metadata={})
    plot.show_frame(replace(sample, batch=replace(sample.batch, measurements=(plain,))), SOURCE, (1,))
    assert not plot.instrument_view and plot.plot.getAxis("left").isVisible()
    prior = plot.displayed_frame
    bad = replace(plain, values=np.array([float("nan")] * len(plain.values)))
    plot.show_frame(
        replace(sample, sequence=2, batch=replace(sample.batch, measurements=(bad,))), SOURCE, (1,)
    )
    assert plot.displayed_frame is prior


def test_builtin_fit_and_autorange_cannot_collapse_grid(plot, qtbot):
    sample = frame(channels=(1,))
    plot.show_frame(sample, SOURCE, (1,))
    plot.plot.plotItem.autoBtnClicked()
    plot.plot.enableAutoRange()  # Also guard keyboard/context or stale widget state.
    noise = np.random.default_rng(42).normal(0, 0.001, len(sample.batch.measurements[0].values))
    for sequence, amplitude in enumerate((0.1, 0.02, 4, 0), 2):
        m = sample.batch.measurements[0]
        changed = replace(m, values=(m.values + noise) * amplitude, axis=m.axis + sequence * 1e-12)
        plot.show_frame(
            replace(sample, sequence=sequence, batch=replace(sample.batch, measurements=(changed,))),
            SOURCE,
            (1,),
        )
        qtbot.wait(10)
        assert plot.plot.getViewBox().autoRangeEnabled() == [False, False]
        assert plot.plot.viewRange()[1] == pytest.approx([-4, 4])
        assert plot.plot.viewRange()[0] == pytest.approx([-5e-6, 5e-6])
        assert sum(-4 < line.value() < 4 for line in plot.grid_lines[11:]) == 7
        assert all(line.isVisible() and line.pen.color().alpha() >= 76 for line in plot.grid_lines[11:])
        assert plot.grid_lines[15].pen.color().alpha() > plot.grid_lines[14].pen.color().alpha()
        assert len(plot.curves[1, "waveform"].getData()[0]) > 1
        assert not plot.plot.plotItem.autoBtn.isVisible()
        np.testing.assert_array_equal(plot.displayed_frame.batch.measurements[0].values, changed.values)


def test_legacy_builtin_fit_is_once_and_reset_can_restore_instrument_grid(plot, qtbot):
    sample = frame(channels=(1,))
    m = replace(sample.batch.measurements[0], metadata={})
    plain = replace(sample, batch=replace(sample.batch, measurements=(m,)))
    plot.show_frame(plain, SOURCE, (1,))
    plot.plot.plotItem.autoBtnClicked()
    previous = plot.plot.viewRange()
    plot.show_frame(
        replace(
            plain, sequence=2, batch=replace(plain.batch, measurements=(replace(m, values=m.values * 0.01),))
        ),
        SOURCE,
        (1,),
    )
    qtbot.wait(10)
    assert plot.plot.viewRange() == previous
    assert plot.plot.getViewBox().autoRangeEnabled() == [False, False]
    plot.reset()
    plot.show_frame(replace(sample, session_id="reconnected"), SOURCE, (1,))
    assert plot.plot.viewRange()[1] == pytest.approx([-4, 4])
