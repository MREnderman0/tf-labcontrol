import math

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError, DeviceIdentity, ValidationError
from lab_control.instruments.devices.tek_afg31252.adapter import (
    Afg31252Adapter,
    afg31252,
    pm_degrees,
    waveform_value,
)
from lab_control.instruments.registry import PROFILES, match
from lab_control.transport.base import ieee_block

from .state import StateTransport


def device(exchanges):
    transport = StateTransport(exchanges)
    transport.open()
    return Afg31252Adapter(transport, afg31252(), ConnectionConfig("mock://test", "tek.afg31252")), transport


def test_exact_identity_and_registry_profile():
    assert match(DeviceIdentity("TEKTRONIX", "AFG31252", "TEST")) == "tek.afg31252"
    assert not match(DeviceIdentity("TEKTRONIX", "AFG31000", "TEST"))
    assert PROFILES["tek.afg31252"].commands["pm_deviation"].decode is pm_degrees


@pytest.mark.parametrize("ch", [1, 2])
def test_noise_mapping_and_channel_command(ch):
    adapter, transport = device(
        [
            (f"SOUR{ch}:FUNC PRNOISE", b""),
            (f"SOUR{ch}:FUNC?", b"PRN\n"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    result = adapter.apply(DeviceCommand("waveform", {"value": "NOIS"}, ch))
    assert result.outcome == "success" and result.readback == "NOIS"
    assert not transport.exchanges


@pytest.mark.parametrize("ch,degrees", [(1, 10), (2, 20)])
def test_pm_explicit_degrees_and_radian_readback(ch, degrees):
    adapter, transport = device(
        [
            (f"SOUR{ch}:PM:DEV {degrees}DEG", b""),
            (f"SOUR{ch}:PM:DEV?", str(math.radians(degrees)).encode()),
            ("SYST:ERR?", b"0,No error"),
        ]
    )
    result = adapter.apply(DeviceCommand("pm_deviation", {"value": degrees}, ch))
    assert result.outcome == "success" and result.readback == pytest.approx(degrees)
    assert not transport.exchanges


def test_noise_decoder_preserves_other_waveforms():
    assert waveform_value(b"PRNOISE\n") == "NOIS"
    assert waveform_value(b"EMEM2\n") == "EMEM2"


@pytest.mark.parametrize(
    "waveform,actual,outcome",
    [
        ("SIN", 0.50004, "success"),
        ("SIN", 0.5005, "rejected"),
        ("SQU", 0.5005, "success"),
        ("SQU", 0.502, "rejected"),
    ],
)
def test_phase_resolution_preserves_actual_readback(waveform, actual, outcome):
    adapter, transport = device(
        [
            ("SOUR2:FUNC?", waveform.encode()),
            ("SOUR2:PHAS 0.5", b""),
            ("SOUR2:PHAS?", str(actual).encode()),
            ("SYST:ERR?", b"0,No error"),
        ]
    )
    result = adapter.apply(DeviceCommand("phase", {"value": 0.5}, 2))
    assert result.outcome == outcome
    assert result.readback == actual
    assert transport.history.count("SOUR2:PHAS 0.5") == 1


@pytest.mark.parametrize(
    "raw,error,outcome",
    [
        (b"0.17453", b"0,No error", "success"),
        (b"0.18", b"0,No error", "rejected"),
        (b"0.17453", b'-221,"Settings conflict"', "rejected"),
    ],
)
def test_pm_rounding_does_not_hide_mismatch_or_instrument_error(raw, error, outcome):
    adapter, _ = device(
        [
            ("SOUR1:PM:DEV 10DEG", b""),
            ("SOUR1:PM:DEV?", raw),
            ("SYST:ERR?", error),
        ]
    )
    result = adapter.apply(DeviceCommand("pm_deviation", {"value": 10}, 1))
    assert result.outcome == outcome
    assert result.readback == math.degrees(float(raw))


@pytest.mark.parametrize("ch", [1, 2])
def test_binary_backup_keeps_newline_code_and_big_endian(ch):
    payload = np.array([0, 10, 13, 8191, 16382], dtype=">u2").tobytes()
    adapter, transport = device([(f"DATA:DATA? EMEM{ch}", ieee_block(payload) + b"\n")])
    assert adapter.read_edit_memory(ch) == payload
    assert not transport.exchanges


@pytest.mark.parametrize("raw", [b"#14\x00\x01", b"#13abc", ieee_block(b"\xff\xff")])
def test_incomplete_or_invalid_memory_backup_rejected(raw):
    adapter, _ = device([("DATA:DATA? EMEM1", raw)])
    with pytest.raises(DeviceError):
        adapter.read_edit_memory(1)


def test_restore_checks_outputs_and_verifies_original_codes():
    payload = np.array([0, 8191, 16382], dtype=">u2").tobytes()
    adapter, transport = device(
        [
            ("OUTP1?", b"0"),
            ("OUTP2?", b"0"),
            (b"DATA:DATA EMEM2," + ieee_block(payload) + b"\n", b""),
            ("SYST:ERR?", b"0,No error"),
            ("DATA:DATA? EMEM2", ieee_block(payload)),
        ]
    )
    adapter.restore_edit_memory(2, payload)
    assert not transport.exchanges


def test_restore_refuses_enabled_output_before_any_write():
    adapter, transport = device([("OUTP1?", b"1")])
    with pytest.raises(ValidationError):
        adapter.restore_edit_memory(1, b"\x00\x00")
    assert transport.history == ["OUTP1?"]


def test_restore_mismatch_is_reported():
    adapter, _ = device(
        [
            ("OUTP1?", b"0"),
            ("OUTP2?", b"0"),
            (b"DATA:DATA EMEM1," + ieee_block(b"\x00\x01") + b"\n", b""),
            ("SYST:ERR?", b"0,No error"),
            ("DATA:DATA? EMEM1", ieee_block(b"\x00\x02")),
        ]
    )
    with pytest.raises(DeviceError, match="differs"):
        adapter.restore_edit_memory(1, b"\x00\x01")


def test_timeout_never_replays_write():
    adapter, transport = device(
        [
            ("SOUR2:PM:DEV 10DEG", b""),
            ("SOUR2:PM:DEV?", DeviceError("timeout", "timeout")),
        ]
    )
    result = adapter.apply(DeviceCommand("pm_deviation", {"value": 10}, 2))
    assert result.outcome == "partial"
    assert transport.history.count("SOUR2:PM:DEV 10DEG") == 1


@pytest.mark.parametrize(
    "command",
    [
        DeviceCommand("pm_deviation", {"value": 181}),
        DeviceCommand("amplitude", {"value": 11}),
        DeviceCommand("waveform", {"value": "SIN"}, 3),
        DeviceCommand("output", {"value": True}),
    ],
)
def test_invalid_value_channel_or_confirmation_has_no_io(command):
    adapter, transport = device([])
    with pytest.raises(DeviceError):
        adapter.apply(command)
    assert not transport.history
