"""CoreMorrow XMT serial framing and read-only payload helpers.

The installed CoreMorrow serial library was observed on the selected COM6
asset.  Frames use ``0xAA, address, total_length, B3, B4, payload, xor``;
the length includes every byte in the frame and the final byte is the XOR of
all preceding bytes.  This module contains no write-command encoders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from lab_control.domain.instruments import DeviceError, ValidationError

HEADER = 0xAA
DEFAULT_ADDRESS = 0x01
READ_DATA_COMMAND = 0x05
READ_POWER_CONFIG_COMMAND = 0x50
READ_MCU_NUMBER_COMMAND = 0x64


@dataclass(frozen=True)
class XmtFrame:
    address: int
    command_b3: int
    command_b4: int
    payload: bytes
    raw: bytes


def _byte(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 0xFF:
        raise ValidationError(f"{name} must be an unsigned byte")
    return value


def checksum(body: bytes) -> int:
    value = 0
    for item in body:
        value ^= item
    return value


def build_read(address: int, command_b3: int, command_b4: int = 0, payload: bytes = b"") -> bytes:
    """Build a verified read request.

    Only read requests are exposed here.  Callers cannot accidentally encode
    a setting command through this protocol helper.
    """

    _byte(address, "address")
    _byte(command_b3, "command_b3")
    _byte(command_b4, "command_b4")
    if not isinstance(payload, bytes) or len(payload) > 248:
        raise ValidationError("XMT request payload is too large")
    total_length = 6 + len(payload)
    body = bytes((HEADER, address, total_length, command_b3, command_b4)) + payload
    return body + bytes((checksum(body),))


def parse_frame(
    raw: bytes,
    *,
    address: int | None = None,
    command_b3: int | None = None,
    command_b4: int | None = None,
) -> XmtFrame:
    if not isinstance(raw, bytes) or len(raw) < 6:
        raise DeviceError("XMT frame is truncated", "parse_error")
    if raw[0] != HEADER:
        raise DeviceError("XMT frame header mismatch", "parse_error")
    if raw[2] != len(raw):
        raise DeviceError("XMT frame length mismatch", "parse_error")
    if checksum(raw[:-1]) != raw[-1]:
        raise DeviceError("XMT frame XOR checksum mismatch", "parse_error")
    actual_address, actual_b3, actual_b4 = raw[1], raw[3], raw[4]
    if address is not None and actual_address != _byte(address, "address"):
        raise DeviceError("XMT response address differs from request", "identity_conflict")
    if command_b3 is not None and actual_b3 != _byte(command_b3, "command_b3"):
        raise DeviceError("XMT response B3 differs from request", "parse_error")
    if command_b4 is not None and actual_b4 != _byte(command_b4, "command_b4"):
        raise DeviceError("XMT response B4 differs from request", "parse_error")
    return XmtFrame(actual_address, actual_b3, actual_b4, raw[5:-1], raw)


def read_framed(read_exact: Callable[[int], bytes]) -> bytes:
    """Read one total-length-delimited XMT frame."""

    prefix = read_exact(3)
    if len(prefix) != 3 or prefix[0] != HEADER:
        raise DeviceError("XMT response does not start with 0xAA", "parse_error")
    length = prefix[2]
    if length < 6:
        raise DeviceError("XMT response length is below the protocol minimum", "parse_error")
    remainder = read_exact(length - 3)
    if len(remainder) != length - 3:
        raise DeviceError("XMT response ended before its declared length", "timeout")
    return prefix + remainder


def parse_power_config(frame: XmtFrame) -> dict[str, object]:
    """Decode the stable fields documented by the vendor header.

    Unknown/reserved bytes remain in ``raw_payload``; they are deliberately
    not interpreted as model or serial information.
    """

    if frame.command_b3 != READ_POWER_CONFIG_COMMAND or len(frame.payload) != 55:
        raise DeviceError("XMT power configuration payload must contain 55 bytes", "parse_error")
    payload = frame.payload
    sensor = payload[15]
    return {
        "cpu_type": payload[0],
        "channel_count": payload[1],
        "configuration_revision": payload[2],
        "configuration_code": payload[3],
        "port_flags": payload[5],
        "sensor_type": chr(sensor) if 32 <= sensor < 127 else f"0x{sensor:02X}",
        "bandwidth_code": payload[16],
        "da_resolution_bits": payload[17],
        "ad_resolution_bits": payload[18],
        "raw_payload": payload.hex(" "),
    }


def parse_raw_channel(frame: XmtFrame, channel: int) -> dict[str, object]:
    if frame.command_b3 not in {5, 6} or len(frame.payload) != 5:
        raise DeviceError("XMT raw channel payload must contain five bytes", "parse_error")
    if frame.payload[0] != channel:
        raise DeviceError("XMT raw channel echo differs from request", "parse_error")
    return {
        "channel": channel + 1,
        "command_b3": frame.command_b3,
        "raw_value": frame.payload[1:].hex(" "),
        "raw_payload": frame.payload.hex(" "),
    }

