import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError

from ..profile import profile
from ..protocol import trigger_setup
from .samples import binary, snapshot_exchanges
from .test_adapter import device


def test_trigger_source_edit_preserves_holdoff_and_confirms_complete_readback():
    adapter, wire = device(
        [
            ("TRSE?", b"TRSE EDGE,SR,C1,HT,TI,HV,2e-6"),
            ("TRSE EDGE,SR,C3", b""),
            ("*OPC?", b"1"),
            ("TRSE?", b"TRSE EDGE,SR,C3,HT,TI,HV,2e-6"),
            ("TRSE?", b"TRSE EDGE,SR,C3,HT,TI,HV,2e-6"),
        ]
    )
    result = adapter.apply(DeviceCommand("trigger_source", {"value": "C3"}))
    assert result.outcome == "success" and result.readback == "C3"
    assert [c for c in wire.history if "?" not in c] == ["TRSE EDGE,SR,C3"]


@pytest.mark.parametrize("setup", [b"TRSE GLIT,SR,C1,HT,OFF", b"TRSE EDGE,SR,EX,HT,OFF"])
def test_advanced_or_external_trigger_disables_edits_and_never_forces_edge(setup):
    adapter, wire = device([("TRSE?", setup)])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("trigger_source", {"value": "C2"}))
    assert wire.history == ["TRSE?"]
    cap = adapter.capabilities_for_channel(1).get("trigger_source")
    assert cap.readable and not cap.writable


def test_trigger_unexpected_holdoff_change_is_reported_without_compensation():
    adapter, wire = device(
        [
            ("TRSE?", b"EDGE,SR,C1,HT,TI,HV,2e-6"),
            ("TRSE EDGE,SR,C2", b""),
            ("*OPC?", b"1"),
            ("TRSE?", b"EDGE,SR,C2,HT,OFF"),
            ("TRSE?", b"EDGE,SR,C2,HT,OFF"),
        ]
    )
    assert adapter.apply(DeviceCommand("trigger_source", {"value": "C2"})).outcome == "rejected"
    assert sum("?" not in cmd for cmd in wire.history) == 1


@pytest.mark.parametrize("raw", [b"EDGE", b"EDGE,SR,C1,SR,C2", b"EDGE,HT,OFF", b"EDGE,SR,"])
def test_invalid_trigger_structure(raw):
    with pytest.raises(DeviceError):
        trigger_setup(raw)


def test_display_uses_probe_tip_scale_and_offset_from_own_snapshot_no_extra_queries():
    snapshot = snapshot_exchanges(
        profile(), overrides={"1.scale": b".05 V", "1.offset": b".1 V", "1.time_position": b"2e-6 S"}
    )
    adapter, wire = device(snapshot + [("C1:WF? ALL", binary())] + snapshot + [("C1:WF? ALL", binary())])
    req = AcquisitionRequest("waveform", consistency="live", context_id="grid")
    first = adapter.acquire(req)
    count = len(wire.history)
    second = adapter.acquire(req)
    grid = second.measurements[0].metadata["scope_display"]
    assert grid["units_per_div"] == 0.05 and grid["reference_value"] == -0.1
    assert grid["time_center"] == -2e-6 and grid["seconds_per_div"] == 0.001
    assert grid == first.measurements[0].metadata["scope_display"]
    assert wire.history[count:] == ["C1:WF? ALL"]
    assert second.settings_timestamps == first.settings_timestamps


def test_cached_channel_ranges_follow_coupling_probe_and_successful_write():
    adapter, wire = device(
        [
            ("C2:CPL?", b"D1M"),
            ("C2:ATTN?", b"10"),
            ("C2:VDIV?", b".2 V"),
            ("C2:OFST?", b"0 V"),
            ("C2:CPL D50", b""),
            ("*OPC?", b"1"),
            ("C2:CPL?", b"D50"),
        ]
    )
    assert adapter.apply(DeviceCommand("coupling", {"value": "D50"}, 2)).outcome == "success"
    before = list(wire.history)
    cap = adapter.capabilities_for_channel(2).get("scale")
    assert cap.parameters["value"].minimum == 0.01 and cap.parameters["value"].maximum == 10
    assert adapter.capabilities_for_channel(1).get("scale").parameters["value"].minimum == 0.001
    assert wire.history == before


@pytest.mark.parametrize("actual", ["SINGLE", "STOP"])
def test_single_mode_accepts_waiting_or_already_stopped_without_claiming_new_trace(actual):
    adapter, wire = device([("TRMD SINGLE", b""), ("TRMD?", actual.encode())])
    result = adapter.apply(DeviceCommand("trigger_mode", {"value": "SINGLE"}, confirmed=True))
    assert result.outcome == "success" and result.operation == "trigger_mode"
    assert result.readback == actual and wire.history == ["TRMD SINGLE", "TRMD?"]
