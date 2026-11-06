import math

import pytest

from lab_control.domain.instruments import ValidationError
from lab_control.domain.metrics import MetricInput, MetricParameters, MetricValue
from lab_control.domain.voltage_waveform_metrics import process_voltage_waveform_statistics


def waveform(channel, values, quantity="waveform"):
    return {
        "channel": channel,
        "quantity": quantity,
        "values": values,
        "unit": "V",
        "axis": [index * 1e-5 for index in range(len(values))],
        "axis_unit": "s",
    }


def process(*frames):
    data = MetricInput(
        "test", {}, True, lambda: ({"measurements": frame} for frame in frames), lambda: None, lambda *_: None
    )
    return process_voltage_waveform_statistics(data, MetricParameters())


def test_voltage_statistics_use_all_samples_on_each_channel():
    result = process((waveform(2, [-2] * 19 + [18]), waveform(4, [4, 4], "voltage")))
    assert result["ch2.mean"] == MetricValue(-1)
    assert result["ch2.peak_to_peak"] == MetricValue(20)
    assert result["ch2.rms"].value == pytest.approx(math.sqrt(20))
    assert result["ch2.std"].value == pytest.approx(math.sqrt(19))
    assert result["ch4.mean"] == MetricValue(4)
    assert result["ch4.peak_to_peak"] == MetricValue(0)
    assert result["ch4.rms"] == MetricValue(4)
    assert result["ch4.std"] == MetricValue(0)


def test_single_voltage_sample_has_no_peak_to_peak():
    assert process((waveform(1, [-2]),)) == {
        "ch1.mean": MetricValue(-2),
        "ch1.peak_to_peak": MetricValue(None, "unavailable"),
        "ch1.rms": MetricValue(2),
        "ch1.std": MetricValue(0),
    }


@pytest.mark.parametrize("magnitude", [0.0, 1e-200, 1e200])
def test_rms_and_std_preserve_scale(magnitude):
    result = process((waveform(1, [-magnitude, magnitude]),))
    assert result["ch1.rms"] == result["ch1.std"] == MetricValue(magnitude)


def test_separate_waveform_frames_are_not_combined():
    with pytest.raises(ValidationError, match="一帧"):
        process((waveform(1, [0, 1]),), (waveform(1, [1, 2]),))


@pytest.mark.parametrize("fields", [{"axis": [0]}, {"unit": "A"}, {"values": [0, float("nan")]}])
def test_rejects_invalid_voltage_waveform(fields):
    with pytest.raises(ValidationError):
        process((waveform(1, [0, 1]) | fields,))
