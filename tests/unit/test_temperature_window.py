"""Rolling temperatures retain every sample and accept negative values."""

from dataclasses import replace

import pytest

from lab_control.application.temperature_window import TemperatureWindow
from lab_control.domain.instruments import Measurement
from tests.unit.test_power_window import frame as power_frame


def frame(sequence, at, values, unit="°C", **kwargs):
    original = power_frame(sequence, at, values, **kwargs)
    return replace(
        original,
        request=replace(original.request, quantity=""),
        batch=replace(
            original.batch,
            measurements=tuple(Measurement("temperature", unit, [v], ch) for ch, v in values.items()),
        ),
    )


def test_boundaries_expired_extrema_and_independent_channels():
    window = TemperatureWindow()
    for seq, (at, value) in enumerate(((0, -20), (17, 3), (750, 5), (1800, 4)), 1):
        assert window.add(frame(seq, at, {401: value, 417: 2}))
    first, second = window.snapshot(1800)
    assert (first.count, first.minimum, first.maximum, first.peak_to_peak) == (4, -20, 5, 25)
    assert second.peak_to_peak == 0
    assert window.snapshot(1800.001)[0].peak_to_peak == 2
    assert window.snapshot(1817.001)[0].peak_to_peak == 1
    assert window.snapshot(4000)[0].peak_to_peak is None


def test_no_replay_cap_reconnect_freeze_and_restart():
    window = TemperatureWindow()
    for sequence in range(1, 3602):
        assert window.add(
            frame(
                sequence,
                (sequence - 1) * 0.5,
                {401: -10 if sequence == 1 else 5},
                session="reconnected" if sequence > 1200 else "first",
            )
        )
    assert window.snapshot(1800)[0].count == 3601
    assert window.snapshot(1800)[0].peak_to_peak == 15
    stopped = window.snapshot(1800, stopped=True)
    assert window.snapshot(10000, stopped=True) == stopped
    assert TemperatureWindow().snapshot(10000) == ()


def test_invalid_duplicate_and_unit_change():
    window = TemperatureWindow()
    good = frame(1, 0, {401: 20})
    assert window.add(good)
    assert not window.add(good)
    assert window.snapshot(0)[0].peak_to_peak == 0
    for value in (float("nan"), float("inf")):
        assert not window.add(frame(2, 1, {401: value}))
    assert not window.add(frame(2, 1, {401: 1}, unit="V"))
    incomplete = frame(2, 1, {401: 1, 402: 2})
    assert not window.add(
        replace(incomplete, batch=replace(incomplete.batch, measurements=incomplete.batch.measurements[:1]))
    )
    assert window.add(frame(2, 1, {401: 68}, unit="°F"))
    assert window.snapshot(1)[0].count == 1
    assert window.snapshot(1)[0].minimum == pytest.approx(68)
