import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError, ValidationError
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import ieee_block

from .state import StateTransport


def adapter(profile_id, exchanges):
    transport = StateTransport(exchanges)
    transport.open()
    return build_scpi_adapter(ConnectionConfig("mock://fixture", profile_id), transport), transport


def test_timeout_not_replayed_and_failed_readback_not_success():
    device, transport = adapter(
        "tek.afg31252", [("SOUR1:FREQ 1000", b""), ("SOUR1:FREQ?", DeviceError("timeout", "timeout"))]
    )
    result = device.apply(DeviceCommand("frequency", {"value": 1000}))
    assert result.outcome == "partial" and result.completed_steps == 1
    assert transport.history.count("SOUR1:FREQ 1000") == 1
    device, _ = adapter(
        "tek.afg31252", [("SOUR1:FREQ 1000", b""), ("SOUR1:FREQ?", b"800"), ("SYST:ERR?", b"0,No error")]
    )
    assert device.apply(DeviceCommand("frequency", {"value": 1000})).outcome == "rejected"


def test_tek_upload_big_endian_and_sample_bounds():
    samples = [-1.0] * 16 + [1.0] * 16
    payload = (
        b"DATA:DATA EMEM1," + ieee_block(np.array([0] * 16 + [16382] * 16, dtype=">u2").tobytes()) + b"\n"
    )
    device, transport = adapter("tek.afg31252", [(payload, b""), ("SYST:ERR?", b"0,No error")])
    assert (
        device.apply(DeviceCommand("upload_waveform", {"samples": samples}, confirmed=True)).outcome
        == "success"
    )
    assert not transport.exchanges
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("upload_waveform", {"samples": [0, 2]}, confirmed=True))


def test_amplitude_units_and_envelope_are_checked_before_any_write():
    device, transport = adapter("tek.afg31252", [("SOUR1:VOLT:UNIT?", b"VPP"), ("SOUR1:VOLT:OFFS?", b"4")])
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("amplitude", {"value": 4}))
    assert not transport.exchanges
    assert all(str(cmd).endswith("?") for cmd in transport.history)
