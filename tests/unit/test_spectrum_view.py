from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.acquisition_pipeline import immutable_batch
from lab_control.application.controls import DataSource
from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.pages.monitoring.spectrum_markers import SpectrumPlot


def frame(y=(-90.0, -10.0), *, version=0, config=None, unit="dBm", axis_unit="Hz", session="one", x=None):
    batch = MeasurementBatch(
        DeviceIdentity("Mock", "Spectrum", "fixture"),
        (
            Measurement(
                "spectrum",
                unit,
                np.array(y),
                axis=np.array(x if x is not None else [100.0, 200.0]),
                axis_unit=axis_unit,
            ),
        ),
        config or {"span": 100},
        validation="cached",
    )
    return MonitorFrame(
        1, version, AcquisitionRequest("spectrum"), immutable_batch(batch), session_id=session
    )


@pytest.mark.parametrize("plot_class", [LivePlot, SpectrumPlot])
def test_spectrum_holds_axis_then_refits_once_on_settings_or_manual_fit(qtbot, plot_class):
    plot = plot_class()
    qtbot.addWidget(plot)
    plot.show()
    source = DataSource("spectrum", "频谱", "spectrum")
    first = frame()
    plot.show_frame(first, source, (1,))
    assert plot.plot.viewRange()[1] == pytest.approx([-98, -2])
    plot.show_frame(frame((-160, 40)), source, (1,))
    qtbot.wait(30)
    assert plot.plot.viewRange()[1] == pytest.approx([-98, -2])
    assert plot.plot.getViewBox().state["autoRange"] == [False, False]
    plot.fit_current()
    assert plot.plot.viewRange()[1] == pytest.approx([-180, 60])
    plot.show_frame(frame((-70, -60)), source, (1,))
    assert plot.plot.viewRange()[1] == pytest.approx([-180, 60])
    plot.show_frame(frame((-70, -60), version=1), source, (1,))
    assert plot.plot.viewRange()[1] == pytest.approx([-71, -59])
    plot.plot.setYRange(-100, 0, padding=0)
    plot.plot.setXRange(120, 140, padding=0)
    # Recording/refresh timestamps must not reset a manual viewport.
    changed = frame((-150, 20), version=1, config={"span": 100, "monitor_record_session": "new"})
    plot.show_frame(
        replace(changed, batch=replace(changed.batch, settings_timestamps={"span": "new-time"})), source, (1,)
    )
    assert plot.plot.viewRange()[1] == pytest.approx([-100, 0])
    assert plot.plot.viewRange()[0] == pytest.approx([120, 140])
    # Same session version after a front-panel refresh, but different actual settings.
    plot.show_frame(frame((-90, -40), version=1, config={"span": 100, "rbw": 3000}), source, (1,))
    assert plot.plot.viewRange()[1] == pytest.approx([-95, -35])


def test_invalid_frame_does_not_consume_fit_and_units_zero_span_reconnect_reset(qtbot):
    plot = LivePlot()
    qtbot.addWidget(plot)
    plot.show()
    source = DataSource("spectrum", "频谱", "spectrum")
    original = frame()
    plot.show_frame(original, source, (1,))
    plot.show_frame(frame((np.nan, 2), version=1), source, (1,))
    assert plot.displayed_frame.batch.timestamp == original.batch.timestamp
    assert plot.plot.viewRange()[1] == pytest.approx([-98, -2])
    plot.show_frame(frame((0, 0), version=1, unit="V", axis_unit="s", x=[0, 0.2]), source, (1,))
    assert plot.plot.viewRange()[1][0] < 0 < plot.plot.viewRange()[1][1]
    assert plot.plot.viewRange()[0] == pytest.approx([-0.004, 0.204])
    plot.show_frame(
        frame((1, 2), version=1, unit="V", axis_unit="s", x=[0, 0.2], session="two"), source, (1,)
    )
    assert plot.plot.viewRange()[1] == pytest.approx([0.9, 2.1])
    plot.reset()
    assert plot.displayed_frame is None


def test_snapshot_retains_selected_full_arrays_and_other_plot_autorange(qtbot):
    plot = LivePlot()
    qtbot.addWidget(plot)
    source = DataSource("magnitude", "网络分析", "s_parameters")
    original = frame(y=np.arange(10001), x=np.arange(10001))
    original = replace(
        original,
        batch=replace(
            original.batch,
            measurements=(
                original.batch.measurements[0],
                replace(original.batch.measurements[0], channel=2),
            ),
        ),
    )
    plot.show_frame(original, source, (2,))
    assert plot.plot.getViewBox().state["autoRange"] == [True, True]
    assert len(plot.displayed_frame.batch.measurements) == 1
    assert plot.displayed_frame.batch.measurements[0].channel == 2
    assert plot.displayed_frame.batch.measurements[0].values.size == 10001
    assert not plot.displayed_frame.batch.measurements[0].values.flags.writeable
