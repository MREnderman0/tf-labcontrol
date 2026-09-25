from types import SimpleNamespace

import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.rigol_dg5504pro.hardware.validate_lan import (
    OffTransport,
    differences,
    restore_steps,
    restore_value,
    validate_recovery_snapshot,
)


@pytest.mark.parametrize(
    "command",
    ["OUTP1 ON", "OUTP2 1", "*TRG", "*RST", "*CLS", "TRIG1", "SOUR1:APPL:SIN 1000", "OUTP1 OFF;OUTP2 ON"],
)
def test_off_guard_forbids_output_triggers_and_reset(tmp_path, command):
    writes = []
    guard = OffTransport(
        SimpleNamespace(resource="mock://guard", write=writes.append), tmp_path / "log.jsonl"
    )
    with pytest.raises(DeviceError):
        guard.write(command)
    assert not writes


def test_write_timeout_is_never_replayed(tmp_path):
    writes = []

    def failing(payload):
        writes.append(payload)
        raise DeviceError("simulated timeout", "timeout")

    guard = OffTransport(SimpleNamespace(resource="mock://guard", write=failing), tmp_path / "log.jsonl")
    with pytest.raises(DeviceError):
        guard.write("OUTP1 OFF")
    assert writes == ["OUTP1 OFF"]


def test_restore_failure_stops_and_keeps_actual_result():
    ran = []

    def fail():
        raise DeviceError("injected restore failure", "timeout")

    result = restore_steps(
        [("first", lambda: ran.append("first")), ("second", fail), ("third", lambda: ran.append("third"))]
    )
    assert ran == ["first"]
    assert [step["status"] for step in result] == ["passed", "failed"]
    assert "injected restore failure" in result[-1]["error"]
    assert differences({"1": {"frequency": 1000}}, {"1": {"frequency": 2000}}) == [".1.frequency"]


def test_recovery_does_not_rewrite_an_unchanged_parameter():
    from lab_control.transport.base import MockTransport

    transport = MockTransport([("SOUR1:BURS:PHAS?", b"+0.000000000000000E+00")])
    transport.open()
    assert restore_value(SimpleNamespace(transport=transport), 1, "BURS:PHAS", 0) == (
        "+0.000000000000000E+00",
        False,
    )
    assert len(transport.history) == 1


def test_recovery_writes_only_observed_difference_and_normalizes_integers():
    from lab_control.transport.base import MockTransport

    transport = MockTransport(
        [("SOUR2:BURS:NCYC?", b"5"), ("SOUR2:BURS:NCYC 1", b""), ("SOUR2:BURS:NCYC?", b"1")]
    )
    transport.open()
    assert restore_value(SimpleNamespace(transport=transport), 2, "BURS:NCYC", 1.0) == ("1", True)
    assert len(transport.history) == 3


def test_incomplete_recovery_evidence_is_rejected_before_any_io():
    with pytest.raises(DeviceError, match="Incomplete CH1"):
        validate_recovery_snapshot({"1": {}, "2": {}, "3": {}, "4": {}, "global": {}})
    with pytest.raises(DeviceError, match="four-channel"):
        validate_recovery_snapshot({})
