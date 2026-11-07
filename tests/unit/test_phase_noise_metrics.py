import pytest

from lab_control.domain.instruments import ValidationError
from lab_control.domain.metrics import MetricInput, MetricValue
from lab_control.domain.phase_noise_metrics import PhaseNoiseOffsetParameters, process_phase_noise_at_offset


def sample(**fields):
    return {
        "quantity": "phase_noise",
        "unit": "dBc/Hz",
        "axis_unit": "Hz",
        "axis": [1, 10],
        "values": [-90, -108],
        **fields,
    }


def process(offset, *measurements):
    source = MetricInput(
        "test", {}, True, lambda: iter(({"measurements": measurements},)), lambda: None, lambda *_: None
    )
    return process_phase_noise_at_offset(source, PhaseNoiseOffsetParameters(offset_hz=offset))["phase_noise"]


@pytest.mark.parametrize(("offset", "expected"), [(1, -90), (5.5, -99), (10, -108)])
def test_phase_noise_interpolates_in_hz_and_uses_measured_points(offset, expected):
    assert process(offset, sample(quantity="noise_floor", values=[-150, -150]), sample()) == MetricValue(
        expected
    )


@pytest.mark.parametrize("offset", [0.5, 12])
def test_outside_measured_offsets_is_unavailable(offset):
    assert process(offset, sample()) == MetricValue(None, "unavailable")


@pytest.mark.parametrize("fields", [{"axis": [1, 1]}, {"unit": "dBm"}, {"quantity": "noise_floor"}])
def test_invalid_or_missing_phase_noise_trace(fields):
    with pytest.raises(ValidationError):
        process(1, sample(**fields))


@pytest.mark.parametrize("final,complete", [(True, True), (True, False), (False, True)])
def test_finite_phase_noise_uses_only_the_complete_final_frame(final, complete):
    frames = [
        {"configuration": {"final": False}, "measurements": [sample(values=[5, 6])]},
        {"configuration": {"final": final, "complete": complete}, "measurements": [sample()]},
    ]
    source = MetricInput(
        "run", {"kind": "continuous"}, True, lambda: iter(frames), lambda: None, lambda *_: None
    )
    params = PhaseNoiseOffsetParameters(offset_hz=10)
    if final and complete:
        assert process_phase_noise_at_offset(source, params)["phase_noise"] == MetricValue(-108)
    else:
        with pytest.raises(ValidationError):
            process_phase_noise_at_offset(source, params)


def test_legacy_single_frame_continuous_record_still_works():
    frame = {"configuration": {}, "measurements": [sample()]}
    source = MetricInput(
        "legacy", {"kind": "continuous"}, True, lambda: iter([frame]), lambda: None, lambda *_: None
    )
    result = process_phase_noise_at_offset(source, PhaseNoiseOffsetParameters(offset_hz=10))
    assert result["phase_noise"] == MetricValue(-108)
