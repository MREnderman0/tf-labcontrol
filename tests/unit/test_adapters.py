import json

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    Parameter,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.acquisition import complex_trace, scaled_rigol
from lab_control.instruments.base import UnknownDeviceAdapter
from lab_control.instruments.registry import DEVICE_DEFINITIONS, PROFILES, build_scpi_adapter, match
from lab_control.transport.base import MockTransport, ieee_block


def adapter(profile_id, exchanges):
    if profile_id not in PROFILES:
        pytest.skip("Device package is not registered")
    transport = MockTransport(exchanges)
    transport.open()
    return build_scpi_adapter(ConnectionConfig("mock://fixture", profile_id), transport), transport


def test_parameter_reading_uses_verified_query_and_write_only_cache():
    profile = PROFILES["rigol.dg5504pro"]
    query = profile.commands["frequency"].query.format(ch=2)
    device, transport = adapter("rigol.dg5504pro", [(query, b"2500")])
    reading = device.read_settings(("frequency",), 2)[0]
    assert reading.value == 2500 and reading.source == "instrument" and reading.channel == 2
    write_only = next(c.name for c in device.capabilities().operations if c.writable and not c.readable)
    before = list(transport.history)
    assert device.read_settings((write_only,))[0].source == "unavailable"
    device.configuration[f"1.{write_only}"] = {"value": 5}
    cached = device.read_settings((write_only,))[0]
    assert cached.source == "session_cache" and cached.value == {"value": 5}
    assert transport.history == before


@pytest.mark.parametrize("profile", PROFILES.values(), ids=PROFILES.keys())
def test_exact_identity_and_capability_sources(profile):
    raw = (profile.manufacturer[0] + "," + profile.models[0] + ",SER123,FW1").encode()
    definition = DEVICE_DEFINITIONS.get(profile.adapter_id)
    if definition:
        fixture = json.loads((definition.root / "tests/fixtures/control.json").read_text(encoding="utf-8"))
        raw = fixture.get("identity_reply", raw.decode()).encode()
    if profile.identity_query == "ID?":
        raw = b"3458A"
    device, transport = adapter(profile.adapter_id, [(profile.identity_query, raw)])
    for payload in reversed(device.connection_commands):
        transport.exchanges.appendleft((payload, b""))
    identity = device.identify()
    assert match(identity) == profile.adapter_id
    assert all(cap.source and cap.section for cap in device.capabilities().operations)
    assert transport.history == [*device.connection_commands, profile.identity_query]
    assert not any(command in transport.history for command in ("*RST", "OUTP ON"))


@pytest.mark.parametrize("profile", PROFILES.values(), ids=PROFILES.keys())
def test_every_operation_rejects_unknown_parameters_without_io(profile):
    device, transport = adapter(profile.adapter_id, [])
    for capability in device.capabilities().operations:
        with pytest.raises(DeviceError):
            device.apply(DeviceCommand(capability.name, {"manufacturer_command": "*RST"}, confirmed=True))
    assert transport.history == []


@pytest.mark.parametrize("profile", PROFILES.values(), ids=PROFILES.keys())
def test_every_numeric_limit_and_confirmation_enforced(profile):
    device, transport = adapter(profile.adapter_id, [])
    for cap in device.capabilities().operations:
        if cap.parameters.get("value") and cap.parameters["value"].maximum is not None:
            with pytest.raises(DeviceError):
                device.apply(
                    DeviceCommand(
                        cap.name, {"value": cap.parameters["value"].maximum + 1000000}, confirmed=True
                    )
                )
        if cap.requires_confirmation and not cap.parameters:
            with pytest.raises(DeviceError):
                device.apply(DeviceCommand(cap.name))
    assert transport.history == []


def test_keithley_select_set_readback_atomic_sequence():
    device, transport = adapter(
        "keithley.2231a",
        [("INST:NSEL 2", b""), ("VOLT 2.5", b""), ("VOLT?", b"2.500"), ("SYST:ERR?", b"0,No error")],
    )
    assert device.apply(DeviceCommand("voltage", {"value": 2.5}, 2)).outcome == "success"
    assert len(transport.history) == 4
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("voltage", {"value": 6}, 3))


def test_spd_uses_channel_argument_and_no_invented_error_queue():
    device, transport = adapter("siglent.spd4306x", [("VOLT CH2,12", b""), ("VOLT:SET? CH2", b"12")])
    assert device.apply(DeviceCommand("voltage", {"value": 12}, 2)).outcome == "success"
    assert not transport.exchanges
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("voltage", {"value": 12}, 1))


def test_unknown_no_commands_and_exact_suffix():
    unknown = UnknownDeviceAdapter(DeviceIdentity("", "DS1202Z"))
    with pytest.raises(Unsupported):
        unknown.apply(DeviceCommand("output", {"value": True}, confirmed=True))
    assert match(DeviceIdentity("RIGOL", "DS1202Z")) == ""
    assert match(DeviceIdentity("RIGOL", "DS1202Z-E")) == "rigol.ds1202ze"
    assert match(DeviceIdentity("Wrong vendor", "DG5504 Pro")) == ""


def test_waveform_scaling_complex_data_and_bad_lengths():
    measurement = scaled_rigol(b"0,0,3,1,0.5,-1,0,0.1,2,128", ieee_block(bytes([130, 140, 120])), 1)
    np.testing.assert_allclose(measurement.values, [0, 1, -1])
    np.testing.assert_allclose(measurement.axis, [-1, -0.5, 0])
    s = complex_trace(b"1,2,3,4", b"1e6,2e6")
    np.testing.assert_array_equal(s.values, [1 + 2j, 3 + 4j])
    with pytest.raises(DeviceError):
        complex_trace(b"1,2,3", b"1,2")


def test_injection_nonfinite_and_option_gate():
    for value in [float("nan"), float("inf"), True]:
        with pytest.raises(ValidationError):
            Parameter().validate("value", value)
    with pytest.raises(ValidationError):
        Parameter("text").validate("value", "wave;*RST")
    device, transport = adapter("rs.sma100b", [])
    with pytest.raises(Unsupported):
        device.apply(DeviceCommand("pulse_enabled", {"value": True}, confirmed=True))
    assert transport.history == []
