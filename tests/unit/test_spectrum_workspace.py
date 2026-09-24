import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import Parameter, ValidationError
from lab_control.domain.spectrum import SpectrumMarker, frequency_draft, locate_marker, prepare_changes
from lab_control.instruments.registry import describe


def test_linked_frequency_and_disjoint_apply_order():
    caps = describe(ConnectionConfig("mock://test", "rigol.dsa815", "mock"))
    initial = dict(start_frequency=100, stop_frequency=200, center_frequency=150, span=100)
    result = frequency_draft({**initial, "center_frequency": 990}, "center_frequency", 0, 1000)
    assert result == dict(start_frequency=900, stop_frequency=1000, center_frequency=950, span=100)
    assert [c.operation for c in prepare_changes(initial, result, caps)] == [
        "stop_frequency",
        "start_frequency",
    ]
    assert [c.operation for c in prepare_changes(result, initial, caps)] == [
        "start_frequency",
        "stop_frequency",
    ]
    result = frequency_draft({**initial, "span": 500}, "span", 0, 1000)
    assert result["span"] == 300 and result["start_frequency"] == 0


def test_discrete_bandwidth_rejected_and_auto_precedes_manual():
    caps = describe(ConnectionConfig("mock://test", "rigol.dsa815", "mock"))
    with pytest.raises(ValidationError):
        Parameter(unit="Hz", choices=("1000", "3000")).validate("value", 2000)
    current = dict(start_frequency=0, stop_frequency=1e6, rbw_auto=True, rbw=1000)
    target = {**current, "rbw_auto": False, "rbw": 3000}
    assert [c.operation for c in prepare_changes(current, target, caps)] == ["rbw_auto", "rbw"]
    assert not prepare_changes(target, {**target, "rbw_auto": True, "rbw": 1000}, caps)[-1].operation == "rbw"


def test_fixed_and_peak_markers_full_trace_and_ties():
    x = np.arange(10001, dtype=float)
    y = np.full(x.size, -90.0)
    y[5001] = -10
    y[5010] = -10
    fixed = locate_marker(SpectrumMarker(1, 5000.7), x, y)
    assert fixed.position == 5000.7 and fixed.sample_frequency == 5001 and fixed.amplitude == -10
    marker = SpectrumMarker(2, 5000, True, 100)
    assert locate_marker(marker, x, y).position == 5001
    y[5099] = -5
    assert locate_marker(marker, x, y).position == 5099
    assert marker.frequency == 5000
    y[5101] = 0
    assert locate_marker(marker, x, y).position == 5099


def test_marker_empty_out_of_range_and_zero_span():
    marker = SpectrumMarker(1, 4, True, 1)
    assert locate_marker(marker, [], []).position is None
    assert locate_marker(marker, [0, 1], [2, 3]).status == "范围外"
    assert locate_marker(marker, [0, 1], [2, 3], "s").status == "时间轴 · 已暂停"
    assert locate_marker(marker, [4], [float("nan")]).status == "无有效数据"


@pytest.mark.parametrize("frequency,width", [(float("nan"), 1), (1, 0), (1, float("inf"))])
def test_invalid_marker_definition(frequency, width):
    with pytest.raises(ValueError):
        SpectrumMarker(1, frequency, half_width=width)
