from __future__ import annotations

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, Unsupported
from lab_control.instruments.devices.coremorrow_xmt import DEFINITION
from lab_control.instruments.devices.coremorrow_xmt.adapter import CoreMorrowXmtAdapter
from lab_control.instruments.devices.coremorrow_xmt.protocol import (
    build_read,
    parse_frame,
    parse_power_config,
    parse_raw_channel,
    read_framed,
)

CONFIG_FRAME = bytes.fromhex(
    "aa013d5000000303062e07580100006458585858520a1010ffffffffffffffcfffffffffffffffffffffffffffffffffffffffffffffffff00000058e4"
)
RAW_FRAME = bytes.fromhex("AA 01 0B 05 00 00 80 21 22 08 2E")


class MockTransport:
    resource = "mock://coremorrow"

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.history: list[bytes] = []

    def query_frame(self, payload: bytes) -> bytes:
        self.history.append(payload)
        return self.replies.pop(0)

    def close(self) -> None:
        pass


def config(**kwargs) -> ConnectionConfig:
    return ConnectionConfig("COM6", "coremorrow.xmt", "serial", **kwargs)


def test_read_frame_and_xor_checksum() -> None:
    assert build_read(1, 0x50) == bytes.fromhex("AA 01 06 50 00 FD")
    frame = parse_frame(CONFIG_FRAME, address=1, command_b3=0x50, command_b4=0)
    assert parse_power_config(frame)["channel_count"] == 3


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


def test_capability_schema_is_conservative() -> None:
    schema = DEFINITION.capability_schema(config())
    assert schema.category == "multimeter"
    assert schema.channels == (1, 2, 3)
    assert {item.name for item in schema.operations} == {"power_config", "raw_channel_data"}
    assert all(not item.writable for item in schema.operations)

