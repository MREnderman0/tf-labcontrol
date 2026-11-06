import json
from collections import deque
from pathlib import Path

import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.devices.custom_optical_comb_pump.adapter import (
    ADAPTER_ID,
    OUTPUT_OFF,
    OpticalCombPumpAdapter,
)

CONTROL = json.loads((Path(__file__).parent / "fixtures/control.json").read_text(encoding="utf-8"))
SETTINGS_REQUEST = bytes.fromhex(CONTROL["readback_request_hex"])
OUTPUT_OFF_REQUEST = bytes.fromhex(CONTROL["request_hex"])
OUTPUT_OFF_ACK = bytes.fromhex(CONTROL["echo_hex"])
SETTINGS_ON = bytes.fromhex(
    "48 01 00 0F 42 40 01 53 "
    "48 12 00 00 5B 68 01 53 "
    "48 F2 00 00 00 01 01 53 "
    "48 0A 00 00 07 D0 01 53 "
    "48 F3 00 00 00 01 01 53 "
    "48 11 00 0F 42 40 01 53"
)
SETTINGS_OFF = SETTINGS_ON[:16] + OUTPUT_OFF_REQUEST + SETTINGS_ON[24:]


class FakeWire:
    def __init__(self, *exchanges):
        self.exchanges = deque(exchanges)
        self.closed = False
        self.history = []

    def query_fixed(self, request, count):
        self.history.append(request)
        expected, size, reply = self.exchanges.popleft()
        assert (request, count) == (expected, size)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


def adapter(wire):
    return OpticalCombPumpAdapter(ConnectionConfig("ASRL99::INSTR", ADAPTER_ID), wire)


def test_identity_is_manual_and_grouped_settings_have_the_documented_units():
    wire = FakeWire((SETTINGS_REQUEST, 48, SETTINGS_ON), (SETTINGS_REQUEST, 48, SETTINGS_ON))
    pump = adapter(wire)
    identity = pump.identify()
    assert identity.key is None and identity.confidence == "manual_protocol"
    assert pump.read_status().values == {
        "current": 1.0,
        "temperature": 23.4,
        "output": True,
        "thermistor_beta": 2000,
        "pump_lowpass": True,
        "pump_current_limit": 1.0,
    }
    assert not wire.exchanges


@pytest.mark.parametrize(
    "reply",
    [
        SETTINGS_ON[:-1],
        SETTINGS_ON[:6] + b"\x02" + SETTINGS_ON[7:],
        SETTINGS_ON[8:16] + SETTINGS_ON[:8] + SETTINGS_ON[16:],
        SETTINGS_ON[:21] + b"\x02" + SETTINGS_ON[22:],
    ],
)
def test_invalid_settings_close_the_session_without_retry(reply):
    wire = FakeWire((SETTINGS_REQUEST, 48, reply))
    with pytest.raises(DeviceError) as error:
        adapter(wire).identify()
    assert error.value.code == "manual_reconnect_required"
    assert wire.closed and wire.history == [SETTINGS_REQUEST]


@pytest.mark.parametrize(
    ("ack", "readback", "outcome"),
    [
        (OUTPUT_OFF_ACK, SETTINGS_OFF, "success"),
        (OUTPUT_OFF_ACK, SETTINGS_ON, "rejected"),
        (b"invalid", None, "uncertain"),
        (DeviceError("timeout", "timeout"), None, "uncertain"),
        (OUTPUT_OFF_ACK, SETTINGS_OFF[:-1], "uncertain"),
    ],
)
def test_shutdown_requires_confirmation_and_readback_and_never_replays(ack, readback, outcome):
    wire = FakeWire((OUTPUT_OFF_REQUEST, 8, ack))
    if readback is not None:
        wire.exchanges.append((SETTINGS_REQUEST, 48, readback))
    pump = adapter(wire)
    with pytest.raises(DeviceError, match="需要确认"):
        pump.apply(DeviceCommand(OUTPUT_OFF))
    with pytest.raises(DeviceError, match="仅允许关闭"):
        pump.apply(DeviceCommand("output", {"value": True}, confirmed=True))
    with pytest.raises(DeviceError, match="仅支持 CH 1"):
        pump.apply(DeviceCommand(OUTPUT_OFF, channel=2, confirmed=True))
    assert not wire.history
    result = pump.apply(DeviceCommand(OUTPUT_OFF, confirmed=True))
    assert result.outcome == outcome and wire.history.count(OUTPUT_OFF_REQUEST) == 1
    assert wire.closed == (outcome == "uncertain")


def test_individual_passive_read_preserves_ambiguous_wire_values():
    request = bytes.fromhex("48 14 00 00 00 00 01 53")
    reply = bytes.fromhex("48 14 00 00 5B 68 01 53")
    wire = FakeWire((request, 8, reply))
    pump = adapter(wire)
    with pytest.raises(DeviceError, match="被动标量"):
        pump.acquire(AcquisitionRequest(initiate=True, confirmed=True))
    (value,) = pump.acquire(AcquisitionRequest(quantity="pump_ld_temperature_raw")).measurements
    assert value.values == [23400] and value.unit == "raw"
    assert value.metadata["wire_value"] == 23400
