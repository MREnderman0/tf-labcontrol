import math

import numpy as np
import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.microchip_53100a.stability import deviations


def test_quadratic_time_error_has_known_allan_and_modified_allan_deviation():
    rate, a = 10, 2e-11
    time = np.arange(1025) / rate
    for result in deviations(a * time**2, rate):
        np.testing.assert_allclose(result.values, math.sqrt(2) * a * result.axis, rtol=1e-9)
        assert result.metadata["estimator"] == "overlapping"


def test_matches_independent_nested_sum_definitions_and_counts():
    phase = np.random.default_rng(47).normal(size=83) * 1e-9
    rate = 100
    results = {m.quantity: m for m in deviations(phase, rate)}
    for name, result in results.items():
        for index, tau in enumerate(result.axis):
            m = round(tau * rate)
            if name == "adev":
                terms = [
                    (phase[j + 2 * m] - 2 * phase[j + m] + phase[j]) ** 2 for j in range(len(phase) - 2 * m)
                ]
                expected = math.sqrt(sum(terms) / len(terms) / (2 * tau * tau))
            else:
                terms = [
                    sum(phase[i + 2 * m] - 2 * phase[i + m] + phase[i] for i in range(j, j + m)) ** 2
                    for j in range(len(phase) - 3 * m + 1)
                ]
                expected = math.sqrt(sum(terms) / len(terms) / (2 * m * m * tau * tau))
            assert result.values[index] == pytest.approx(expected, rel=1e-12)
            assert result.metadata["terms"][index] == len(terms)
    assert results["adev"].values[0] == pytest.approx(results["mdev"].values[0])


def test_short_and_linear_records_are_not_filled_with_fake_deviations():
    assert deviations([0, 1, 2], 1) == ()
    for result in deviations(np.arange(20, dtype=float), 1):
        assert np.all(result.values == 0)


@pytest.mark.parametrize("phase,rate", [([0, np.nan, 1], 1), ([0, 1, 2], 0), ([[1, 2]], 1)])
def test_rejects_invalid_stability_input(phase, rate):
    with pytest.raises(DeviceError):
        deviations(phase, rate)
