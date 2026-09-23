import pytest

from lab_control.domain.instruments import Parameter, ParameterReading, Quality
from lab_control.domain.readback import boolean_value, normalized_value, usable_reading


@pytest.mark.parametrize("value", [False, 0, 0.0, "0", "0.0", "0e0", "OFF", " false ", {"value": 0.0}])
def test_false_output_is_not_unknown(value):
    assert boolean_value(value) is False


@pytest.mark.parametrize("value", [True, 1, 1.0, "1", "1.0", "1e0", "ON", " true ", {"value": 1.0}])
def test_true_output_is_not_unknown(value):
    assert boolean_value(value) is True


@pytest.mark.parametrize("value", [None, "", "NaN", float("inf"), 2, -1, "unknown", {}, []])
def test_invalid_output_never_becomes_off(value):
    assert boolean_value(value) is None


def test_enum_and_readback_quality():
    spec = Parameter("enum", choices=("SIN", "SQU"))
    assert normalized_value("SQUare", spec) == "SQU"
    assert normalized_value("noise", spec) is None
    assert normalized_value("SQUinvalid", spec) is None
    assert normalized_value("SQU", Parameter("enum", choices=("SINE", "SQUARE"))) == "SQUARE"
    assert not usable_reading(
        ParameterReading("output", 1, 0, "session_cache"), {"value": Parameter("boolean")}
    )
    assert not usable_reading(
        ParameterReading("output", 1, 0, "instrument", quality=Quality.STALE), {"value": Parameter("boolean")}
    )
