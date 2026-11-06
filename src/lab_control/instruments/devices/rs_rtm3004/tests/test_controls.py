import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError, ValidationError
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport


def device(exchanges):
    wire = MockTransport(exchanges)
    wire.open()
    return build_scpi_adapter(ConnectionConfig("mock://rtm", "rs.rtm3004"), wire), wire


def test_range_rejection_precedes_write_and_retains_instrument_limits():
    adapter, wire = device([("CHAN2:SCAL? MIN", b"0.005"), ("CHAN2:SCAL? MAX", b"100")])
    with pytest.raises(ValidationError):
        adapter.apply(DeviceCommand("scale", {"value": 0.001}, 2))
    assert wire.history == ["CHAN2:SCAL? MIN", "CHAN2:SCAL? MAX"]
    spec = adapter.capabilities_for_channel(2).get("scale").parameters["value"]
    assert (spec.minimum, spec.maximum, spec.range_source) == (0.005, 100, "instrument")


def test_write_once_readback_and_error_check():
    adapter, wire = device(
        [
            ("CHAN2:SCAL? MIN", b"0.005"),
            ("CHAN2:SCAL? MAX", b"100"),
            ("CHAN2:SCAL 0.5", b""),
            ("*OPC?", b"1"),
            ("CHAN2:SCAL?", b"0.5"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    assert adapter.apply(DeviceCommand("scale", {"value": 0.5}, 2)).outcome == "success"
    assert not wire.exchanges
    assert wire.history.count("CHAN2:SCAL 0.5") == 1


def test_initialization_is_explicit_preserves_backup_and_does_not_start_acquisition():
    adapter, wire = device(
        [
            ("FORM?", b"CSV,0"),
            ("FORM:BORD?", b"MSBF"),
            ("FORM REAL,32", b""),
            ("FORM?", b"REAL,32"),
            ("FORM:BORD LSBF", b""),
            ("FORM:BORD?", b"LSBF"),
        ]
    )
    assert wire.history == []
    adapter.prepare_acquisition()
    assert adapter.acquisition_preparation["before"] == {"format": "CSV,0", "byte_order": "MSBF"}
    assert not wire.exchanges
    assert not any(command in {"RUN", "SING", "STOP"} for command in wire.history)


def test_initialization_mismatch_is_not_replayed():
    adapter, wire = device(
        [("FORM?", b"ASC,0"), ("FORM:BORD?", b"MSBF"), ("FORM REAL,32", b""), ("FORM?", b"ASC,0")]
    )
    with pytest.raises(DeviceError) as caught:
        adapter.prepare_acquisition()
    assert caught.value.code == "configuration_uncertain"
    assert wire.history.count("FORM REAL,32") == 1
    assert adapter.acquisition_preparation["after"]["format"] == "ASC,0"


def test_unqualified_memory_write_is_disabled_before_io():
    from lab_control.domain.instruments import Unsupported

    adapter, wire = device([])
    assert not adapter.capabilities().get("record_length").writable
    with pytest.raises(Unsupported):
        adapter.apply(DeviceCommand("record_length", {"value": 5000}))
    assert wire.history == []


def test_run_mode_requires_a_running_state_not_only_sweep_readback():
    adapter, wire = device(
        [
            ("TRIG:A:MODE AUTO", b""),
            ("RUN", b""),
            ("ACQ:STAT?", b"COMP"),
            ("TRIG:A:MODE?", b"AUTO"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    result = adapter.apply(DeviceCommand("trigger_mode", {"value": "AUTO"}))
    assert result.outcome == "rejected"
    assert wire.history.count("RUN") == 1


def test_deprecated_profile_imports_still_resolve():
    from lab_control.instruments.oscilloscopes import BOOL, RTM_SOURCE, rs_scope

    assert BOOL.kind == "boolean" and RTM_SOURCE.startswith("https://")
    assert rs_scope().adapter_id == "rs.rtm3004"
