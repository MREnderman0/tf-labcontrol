from __future__ import annotations

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, Unsupported, ValidationError
from lab_control.instruments.devices.coremorrow_xmt import DEFINITION
from lab_control.instruments.devices.coremorrow_xmt.adapter import CoreMorrowXmtAdapter
from lab_control.instruments.devices.coremorrow_xmt.protocol import (
    build_frame,
    build_read,
    build_single_point,
    encode_voltage_data,
    parse_frame,
    parse_mult_channel_voltage,
    parse_power_config,
    parse_raw_channel,
    read_framed,
)

CONFIG_FRAME = bytes.fromhex(
    "aa013d5000000303062e07580100006458585858520a1010ffffffffffffffcfffffffffffffffffffffffffffffffffffffffffffffffff00000058e4"
)
RAW_FRAME = bytes.fromhex("AA 01 0B 05 00 00 80 21 22 08 2E")
MULT_VOLTAGE_FRAME = bytes.fromhex("AA 01 13 33 00 00 00 00 09 C3 80 00 00 0C 00 00 00 10 DD")


class MockTransport:
    resource = "mock://coremorrow"

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.history: list[bytes] = []

    def write_frame(self, payload: bytes) -> None:
        self.history.append(payload)

    def query_frame(self, payload: bytes) -> bytes:
        self.history.append(payload)
        return self.replies.pop(0)

    def close(self) -> None:
        pass


def config(**kwargs) -> ConnectionConfig:
    return ConnectionConfig("COM6", "coremorrow.xmt", "serial", **kwargs)


def test_read_frame_and_xor_checksum() -> None:
    assert build_read(1, 0x50) == bytes.fromhex("AA 01 06 50 00 FD")
    assert build_single_point(1, 0, 0.25) == bytes.fromhex("AA 01 0B 00 00 00 00 00 09 C4 6D")
    assert build_single_point(1, 1, 1.0) == bytes.fromhex("AA 01 0B 00 00 01 00 01 00 00 A0")
    frame = parse_frame(CONFIG_FRAME, address=1, command_b3=0x50, command_b4=0)
    assert parse_power_config(frame)["channel_count"] == 3


def test_positive_e00_control_envelope_accepts_120_volts() -> None:
    assert encode_voltage_data(120.0) == bytes.fromhex("00 78 00 00")
    assert DEFINITION.capability_schema(config()).get("voltage").parameters["value"].maximum == 120.0


def test_multi_channel_voltage_response_decodes_signed_magnitude() -> None:
    values = parse_mult_channel_voltage(parse_frame(MULT_VOLTAGE_FRAME), 3)
    assert values == pytest.approx((0.2499, -0.0012, 0.0016))


def test_single_point_encoder_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        build_single_point(1, 0, -0.001)


def test_framed_reader_uses_total_length() -> None:
    pieces = [CONFIG_FRAME[:3], CONFIG_FRAME[3:]]

    def read_exact(count: int) -> bytes:
        piece = pieces.pop(0)
        assert len(piece) == count
        return piece

    assert read_framed(read_exact) == CONFIG_FRAME


def test_adapter_identity_is_protocol_only_and_does_not_fabricate_e09_or_serial() -> None:
    transport = MockTransport([CONFIG_FRAME])
    adapter = CoreMorrowXmtAdapter(transport, DEFINITION.profile, config())
    identity = adapter.identify()
    assert identity.manufacturer == "CoreMorrow"
    assert identity.model == "XMT-compatible controller (exact model pending)"
    assert identity.serial == ""
    assert identity.confidence == "protocol_only"
    assert transport.history == [bytes.fromhex("AA 01 06 50 00 FD")]


def test_raw_channel_read_is_framed_and_channel_scoped() -> None:
    transport = MockTransport([RAW_FRAME])
    adapter = CoreMorrowXmtAdapter(transport, DEFINITION.profile, config())
    value = adapter.read("raw_channel_data", 1)
    assert value["raw_value"] == "80 21 22 08"
    assert transport.history == [bytes.fromhex("AA 01 07 05 00 00 A9")]
    assert parse_raw_channel(parse_frame(RAW_FRAME), 0)["channel"] == 1


def test_writes_and_calibrated_acquisition_are_blocked() -> None:
    adapter = CoreMorrowXmtAdapter(MockTransport([]), DEFINITION.profile, config())
    with pytest.raises(Unsupported):
        adapter.apply(DeviceCommand("set_output", {"value": 1}, confirmed=True))


def test_voltage_write_saves_original_and_requires_explicit_readback() -> None:
    transport = MockTransport([MULT_VOLTAGE_FRAME, MULT_VOLTAGE_FRAME])
    adapter = CoreMorrowXmtAdapter(transport, DEFINITION.profile, config())
    adapter.waveform.running = dict.fromkeys(adapter.channels, False)
    result = adapter.apply(DeviceCommand("voltage", {"value": 0.25}, channel=1, confirmed=True))
    assert result.outcome == "success"
    assert result.readback == pytest.approx(0.2499)
    assert transport.history == [
        bytes.fromhex("AA 01 06 33 00 9E"),
        bytes.fromhex("AA 01 0B 00 00 00 00 00 09 C4 6D"),
        bytes.fromhex("AA 01 06 33 00 9E"),
    ]


def _voltage_frame(channel_one: float) -> bytes:
    return build_frame(
        1,
        0x33,
        payload=bytes((0,)) + encode_voltage_data(channel_one) + encode_voltage_data(0.0) * 2,
    )


def test_voltage_write_polls_stale_readback_without_replaying_write() -> None:
    transport = MockTransport([MULT_VOLTAGE_FRAME, MULT_VOLTAGE_FRAME, _voltage_frame(1.25)])
    adapter = CoreMorrowXmtAdapter(transport, DEFINITION.profile, config())
    adapter.waveform.running = dict.fromkeys(adapter.channels, False)
    adapter.READBACK_SETTLE_TIMEOUT = 0.1
    adapter.READBACK_SETTLE_INTERVAL = 0.001

    result = adapter.apply(DeviceCommand("voltage", {"value": 1.25}, channel=1, confirmed=True))

    assert result.outcome == "success"
    assert result.readback == pytest.approx(1.25)
    assert sum(item[3] == 0 for item in transport.history) == 1


def test_voltage_write_times_out_with_last_readback_and_one_write() -> None:
    transport = MockTransport([MULT_VOLTAGE_FRAME] * 20)
    adapter = CoreMorrowXmtAdapter(transport, DEFINITION.profile, config())
    adapter.waveform.running = dict.fromkeys(adapter.channels, False)
    adapter.READBACK_SETTLE_TIMEOUT = 0.003
    adapter.READBACK_SETTLE_INTERVAL = 0.001

    result = adapter.apply(DeviceCommand("voltage", {"value": 1.25}, channel=1, confirmed=True))

    assert result.outcome == "rejected"
    assert "last_readback" in (result.error or "")
    assert sum(item[3] == 0 for item in transport.history) == 1


def test_capability_schema_is_conservative() -> None:
    schema = DEFINITION.capability_schema(config())
    assert schema.category == "power_supply"
    assert schema.channels == (1, 2, 3)
    assert {item.name for item in schema.operations} == {
        "voltage",
        "measured_voltage",
        "power_config",
        "raw_channel_data",
        "control_state",
        "standard_waveform",
        "waveform_stop",
        "normal_mode",
    }
    assert next(item for item in schema.operations if item.name == "voltage").writable
