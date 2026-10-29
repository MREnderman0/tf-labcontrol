import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport, ieee_block

from ..protocol import boolean, csv_numbers, module_identity, scan_list


def adapter(exchanges=()):
    transport = MockTransport(list(exchanges))
    transport.open()
    result = build_scpi_adapter(ConnectionConfig("mock://m300", "rigol.m300"), transport)
    result.identity = DeviceIdentity("RIGOL TECHNOLOGIES", "M300", "SERIAL")
    return result, transport


def test_protocol_parsers_are_strict():
    assert boolean(b"1\n") is True
    assert scan_list(ieee_block(b"(@401,501)")) == "(@401,501)"
    assert module_identity(b"RIGOL TECHNOLOGIES,MC3132,SERIAL,1.0")
    assert csv_numbers(b"+1.0E+00,-2.0E+00") == (1.0, -2.0)
    with pytest.raises(DeviceError):
        scan_list(b"not-a-scan-list")


def test_identity_and_read_only_status():
    a, transport = adapter(
        [
            ("*IDN?", b"RIGOL TECHNOLOGIES,M300,SERIAL,1.0"),
            ("ROUT:SCAN?", b"#13(@)"),
            ("ROUT:SCAN:SIZE?", b"0"),
            ("INST:DMM:INST?", b"1"),
            ("INST:DMM?", b"1"),
            ("TRIG:SOUR?", b"IMM"),
            ("TRIG:TIM?", b"10"),
            ("ROUT:MON:STAT?", b"0"),
            ("SYST:CTYP? 100", b"RIGOL TECHNOLOGIES,0,0,0"),
            ("SYST:CTYP? 200", b"RIGOL TECHNOLOGIES,0,0,0"),
            ("SYST:CTYP? 300", b"RIGOL TECHNOLOGIES,0,0,0"),
            ("SYST:CTYP? 400", b"RIGOL TECHNOLOGIES,MC3132,SERIAL4,1.0"),
            ("SYST:CTYP? 500", b"RIGOL TECHNOLOGIES,MC3065,SERIAL5,1.0"),
            ("SYST:ERR?", b"0,No error"),
        ]
    )
    assert a.identify().model == "M300"
    status = a.read_status()
    assert status.values["scan_size"] == 0
    assert status.values["slot_400"].endswith("MC3132,SERIAL4,1.0")
    assert not transport.history.count("*RST")


def test_trigger_interval_write_is_read_back_and_error_checked():
    a, transport = adapter(
        [
            ("TRIG:TIM 11", b""),
            ("TRIG:TIM?", b"11.000000"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    result = a.apply(DeviceCommand("trigger_interval", {"value": 11.0}))
    assert result.outcome == "success" and result.readback == 11.0
    assert transport.history == ["TRIG:TIM 11", "TRIG:TIM?", "SYST:ERR?"]
