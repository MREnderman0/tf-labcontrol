import pytest
from pydantic import ValidationError as ParameterValidationError

from lab_control.domain.instruments import ValidationError
from lab_control.domain.metrics import MetricInput, MetricParameters, MetricValue
from lab_control.domain.optical_power_metrics import (
    ReferencePowerParameters,
    process_optical_power_fluctuation,
    process_optical_power_reference_deviation,
)


def power(channel, value, unit="W", axis=None):
    return {"channel": channel, "quantity": "power", "values": value, "unit": unit, "axis": axis}


def source(*frames):
    return MetricInput(
        "test", {}, True, lambda: ({"measurements": frame} for frame in frames), lambda: None, lambda *_: None
    )


def test_channel_statistics_use_linear_power_and_signed_extremes():
    data = source(
        (power(1, -30, "dBm"), power(9, 1e-6)),
        (power(1, [3e-6]), power(9, [1e-6])),
        (power(1, 2e-6), power(9, 1e-6)),
    )
    fluctuation = process_optical_power_fluctuation(data, MetricParameters())
    assert fluctuation == {
        "ch1.fluctuation_percent": MetricValue(pytest.approx(100)),
        "ch9.fluctuation_percent": MetricValue(0),
    }
    deviation = process_optical_power_reference_deviation(
        data, ReferencePowerParameters(reference_power_w={"1": 2e-6, "9": 2e-6})
    )
    assert deviation == {
        "ch1.min_deviation_percent": MetricValue(pytest.approx(-50)),
        "ch1.max_deviation_percent": MetricValue(pytest.approx(50)),
        "ch9.min_deviation_percent": MetricValue(pytest.approx(-50)),
        "ch9.max_deviation_percent": MetricValue(pytest.approx(-50)),
    }


def test_fluctuation_requires_two_readings_and_positive_mean():
    data = source((power(1, 0), power(9, 1e-6)), (power(1, 0),))
    assert process_optical_power_fluctuation(data, MetricParameters()) == {
        "ch1.fluctuation_percent": MetricValue(None, "unavailable"),
        "ch9.fluctuation_percent": MetricValue(None, "unavailable"),
    }


@pytest.mark.parametrize("references", [{"1": 1e-6}, {"1": 1e-6, "9": 1e-6, "10": 1e-6}])
def test_references_must_match_recorded_channels(references):
    with pytest.raises(ValidationError, match="一一对应"):
        process_optical_power_reference_deviation(
            source((power(1, 1e-6), power(9, 1e-6))), ReferencePowerParameters(reference_power_w=references)
        )


@pytest.mark.parametrize("reference", [0, -1, float("inf")])
def test_reference_requires_finite_positive_watts(reference):
    with pytest.raises(ParameterValidationError):
        ReferencePowerParameters(reference_power_w={"1": reference})


@pytest.mark.parametrize("sample", [power(1, [1, 2]), power(1, 1, axis=[0]), power(1, 1, "dB")])
def test_profile_rejects_non_scalar_or_unsupported_power(sample):
    with pytest.raises(ValidationError):
        process_optical_power_fluctuation(source((sample,)), MetricParameters())
