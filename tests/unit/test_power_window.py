"""Exact elapsed-time windows, independent of UI cadence and replay limits."""

from dataclasses import replace

import pytest

from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.power_window import PowerWindow
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceIdentity,
    Measurement,
    MeasurementBatch,
    Quality,
)
from lab_control.domain.power import power_span, power_to_watts, watts_to_dbm


def frame(sequence, at, values, unit="W", *, session="session", configuration=None):
    return MonitorFrame(
        sequence,
        0,
        AcquisitionRequest("scalar", channels=tuple(values), points=1, quantity="power"),
        MeasurementBatch(
            DeviceIdentity("Test", "Power"),
            tuple(Measurement("power", unit, value, ch) for ch, value in values.items()),
            configuration or {},
            timestamp="2026-10-30T12:00:00+00:00",
        ),
        at,
        session,
    )


def test_window_includes_boundary_then_removes_old_extrema():
    window = PowerWindow()
    for sequence, (at, watts) in enumerate(((0, 100), (17, 1), (750, 5), (1800, 10)), 1):
        assert window.add(frame(sequence, at, {1: watts}))
    first = window.snapshot("run", 1800).channels[0]
    assert len(first.points) == 4 and first.minimum == 1 and first.maximum == 100
    assert power_span(first.minimum, first.maximum, "W") == 99
    after = window.snapshot("run", 1800.001).channels[0]
    assert [p.at for p in after.points] == [17, 750, 1800]
    assert after.maximum == 10 and after.minimum == 1
    assert power_span(after.minimum, after.maximum, "dBm") == pytest.approx(10)
    later = window.snapshot("run", 1817.001).channels[0]
    assert later.minimum == 5 and later.maximum == 10
    assert len(first.points) == 4  # Published snapshots stay immutable.


def test_history_exceeds_replay_capacity_and_channels_are_independent():
    window = PowerWindow()
    for sequence in range(1, 3602):
        window.add(frame(sequence, (sequence - 1) * 0.5, {1: 100 if sequence == 1 else 1, 2: 3}))
    first, second = window.snapshot("run", 1800).channels
    assert len(first.points) == 3601
    assert (first.minimum, first.maximum) == (1, 100)
    assert power_span(second.minimum, second.maximum, "W") == 0
    expired = window.snapshot("run", 1800.1).channels[0]
    assert len(expired.points) == 3600 and expired.maximum == 1
    assert not window.snapshot("run", 3601).channels[0].points


def test_gaps_preserve_history_and_stop_freezes_until_new_task():
    window = PowerWindow()
    window.add(frame(1, 100, {1: 1, 2: 2}))
    window.mark_gap({1})
    window.add(frame(2, 110, {2: 3}))
    window.add(frame(3, 120, {1: 4, 2: 5}))
    one, two = window.snapshot("run", 120).channels
    assert [p.segment for p in one.points] == [0, 1]
    assert [p.segment for p in two.points] == [0, 0, 0]
    window.add(frame(4, 130, {1: 7, 2: 8}, session="reconnected", configuration={"_acquisition_gap": {}}))
    assert [p.segment for p in window.snapshot("run", 130).channels[0].points] == [0, 1, 2]
    stopped = window.snapshot("run", 140, stopped=True)
    assert window.snapshot("run", 10000, stopped=True) == stopped
    assert PowerWindow().snapshot("new-run", 10000).origin is None


def test_invalid_and_repeated_frames_never_enter_window():
    window = PowerWindow()
    valid = frame(1, 100, {1: 1})
    assert window.add(valid)
    assert not window.add(valid)
    for value, unit in (
        (float("nan"), "W"),
        (float("inf"), "W"),
        (-1, "W"),
        ([1, 2], "W"),
        (1, "V"),
        (5000, "dBm"),
    ):
        assert not window.add(frame(2, 110, {1: value}, unit))
    bad = frame(2, 110, {1: 2, 2: 3})
    assert not window.add(replace(bad, batch=replace(bad.batch, measurements=bad.batch.measurements[:1])))
    assert not window.add(replace(valid, sequence=2, received_at=99))
    assert not window.add(replace(valid, sequence=2, received_at=float("nan")))
    bad_quality = replace(valid.batch.measurements[0], quality=Quality.PARSE_ERROR)
    assert not window.add(replace(valid, sequence=2, batch=replace(valid.batch, measurements=(bad_quality,))))
    assert len(window.snapshot("run", 110).channels[0].points) == 1


def test_units_zero_single_sample_and_exact_dbm_precision():
    assert power_to_watts(-30, "dBm") == pytest.approx(1e-6)
    assert watts_to_dbm(1e-6) == pytest.approx(-30)
    assert watts_to_dbm(0) is None
    assert power_span(0, 1, "dBm") is None
    assert power_span(0, 1, "W") == 1
    assert power_span(None, None, "W") is None
    window = PowerWindow()
    window.add(frame(1, 100, {1: -57.423}, "dBm"))
    channel = window.snapshot("run", 100).channels[0]
    assert power_span(channel.minimum, channel.maximum, "dBm") == 0
    window.add(frame(2, 101, {1: -57.111}, "dBm"))
    channel = window.snapshot("run", 101).channels[0]
    assert power_span(channel.minimum, channel.maximum, "dBm") == pytest.approx(0.312)
    assert channel.raw_value == -57.111 and channel.raw_unit == "dBm"
