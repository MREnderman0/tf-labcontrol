import pytest

from lab_control.domain.instruments import ValidationError
from lab_control.domain.metrics import MetricInput, MetricParameters, MetricValue
from lab_control.domain.spectrum_metrics import process_spectrum_peak_and_snr


def process(frequencies, powers, **fields):
    trace = {
        "quantity": "spectrum",
        "unit": "dBm",
        "axis_unit": "Hz",
        "axis": frequencies,
        "values": powers,
    }
    source = MetricInput(
        "test", {}, True, lambda: iter(({"measurements": [trace | fields]},)), lambda: None, lambda *_: None
    )
    return process_spectrum_peak_and_snr(source, MetricParameters())


def test_snr_and_spur_rejection_use_points_strictly_outside_two_megahertz():
    assert process([7e6, 8e6, 9e6, 10e6, 11e6, 12e6, 13e6], [-90, -40, -30, -20, -35, -45, -70]) == {
        "peak_frequency": MetricValue(10e6),
        "peak_power": MetricValue(-20),
        "snr": MetricValue(60),
        "spur_rejection": MetricValue(50),
    }


def test_tied_peaks_use_lower_frequency_and_preserve_peak_without_noise_points():
    assert process([9e6, 10e6, 11e6], [-20, -20, -80]) == {
        "peak_frequency": MetricValue(9e6),
        "peak_power": MetricValue(-20),
        "snr": MetricValue(None, "unavailable"),
        "spur_rejection": MetricValue(None, "unavailable"),
    }


@pytest.mark.parametrize(
    "fields", [{"axis_unit": "s"}, {"unit": "W"}, {"axis": [2, 1]}, {"values": [-10, float("nan")]}]
)
def test_rejects_unsuitable_spectrum_trace(fields):
    with pytest.raises(ValidationError):
        process([9e6, 10e6], [-20, -30], **fields)
