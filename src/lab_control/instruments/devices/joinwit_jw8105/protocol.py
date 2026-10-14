"""JW8105 binary protocol from the supplied V23.02.02 communication PDF.

The document is retained as user-supplied evidence in the model package.  The
helpers below are deliberately strict: a malformed frame, wrong address or
wrong response command is a device error and is never silently retried.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable

from lab_control.domain.instruments import DeviceError, ValidationError

HEADER = 0x7B
TAIL = 0x7D
DEFAULT_ADDRESS = 0x01
BAUD_RATE = 115200
CHANNELS = tuple(range(1, 9))  # Local channels in each of the two addressed modules.
MAX_WAVELENGTH_NM = 1625.0
MIN_WAVELENGTH_NM = 850.0
MAX_DISPLAY_WAVELENGTHS = 32


@dataclass(frozen=True)
class Jw8105Frame:
    address: int
    command: int
    data: bytes
    raw: bytes


def _address(address: int) -> int:
    if type(address) is not int or not 1 <= address <= 0xFF:
        raise ValidationError("JW8105 module address must be in [1, 255]")
    return address


def build_frame(address: int, command: int, data: bytes = b"") -> bytes:
    """Build one complete JW8105 frame, including checksum and tail."""

    _address(address)
    if type(command) is not int or not 0 <= command <= 0xFFFF:
        raise ValidationError("JW8105 command must be a 16-bit integer")
    if not isinstance(data, bytes) or len(data) > 200:
        raise ValidationError("JW8105 frame data must be at most 200 bytes")
    body = bytes((HEADER, address, 5 + len(data))) + command.to_bytes(2, "big") + data
    checksum = (-sum(body)) & 0xFF
    return body + bytes((checksum, TAIL))


def parse_frame(
    raw: bytes,
    *,
    address: int | None = None,
    command: int | None = None,
) -> Jw8105Frame:
    """Validate and decode a complete frame returned by the instrument."""

    if not isinstance(raw, bytes) or len(raw) < 7:
        raise DeviceError("JW8105 frame is truncated", "parse_error")
    if raw[0] != HEADER or raw[-1] != TAIL:
        raise DeviceError("JW8105 frame header/tail mismatch", "parse_error")
    if raw[2] != len(raw) - 2:
        raise DeviceError("JW8105 frame length mismatch", "parse_error")
    if sum(raw[:-1]) & 0xFF:
        raise DeviceError("JW8105 checksum mismatch", "parse_error")
    actual_address = raw[1]
    actual_command = int.from_bytes(raw[3:5], "big")
    if address is not None and actual_address != _address(address):
        raise DeviceError(
            f"JW8105 response address {actual_address:#x} differs from {address:#x}",
            "identity_conflict",
        )
    if command is not None and actual_command != command:
        raise DeviceError(
            f"JW8105 response command {actual_command:#06x} differs from {command:#06x}",
            "parse_error",
        )
    return Jw8105Frame(actual_address, actual_command, raw[5:-2], raw)


def signed_le(data: bytes, *, scale: float, width: int) -> float:
    if len(data) != width:
        raise DeviceError("JW8105 signed value has an unexpected width", "parse_error")
    return int.from_bytes(data, "little", signed=True) / scale


def unsigned_le(data: bytes, *, scale: float, width: int) -> float:
    if len(data) != width:
        raise DeviceError("JW8105 unsigned value has an unexpected width", "parse_error")
    return int.from_bytes(data, "little", signed=False) / scale


def powers_16(data: bytes) -> tuple[float, ...]:
    if len(data) != 16:
        raise DeviceError("JW8105 16-bit power payload must contain 8 channels", "parse_error")
    return tuple(signed_le(data[index : index + 2], scale=100, width=2) for index in range(0, len(data), 2))


def powers_32(data: bytes) -> tuple[float, ...]:
    if len(data) != 32:
        raise DeviceError("JW8105 32-bit power payload must contain 8 channels", "parse_error")
    return tuple(signed_le(data[index : index + 4], scale=1000, width=4) for index in range(0, len(data), 4))


def wavelengths(data: bytes) -> tuple[float, ...]:
    if not data or (len(data) - 1) % 2 or data[0] > MAX_DISPLAY_WAVELENGTHS:
        raise DeviceError("JW8105 display wavelength payload is invalid", "parse_error")
    count = data[0]
    if len(data) != 1 + count * 2:
        raise DeviceError("JW8105 display wavelength count mismatch", "parse_error")
    return tuple(
        unsigned_le(data[offset : offset + 2], scale=1, width=2) for offset in range(1, len(data), 2)
    )


def build_wavelength(value: float) -> bytes:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("JW8105 wavelength must be numeric")
    if not MIN_WAVELENGTH_NM <= float(value) <= MAX_WAVELENGTH_NM:
        raise ValidationError(f"JW8105 wavelength must be in [{MIN_WAVELENGTH_NM}, {MAX_WAVELENGTH_NM}] nm")
    scaled = round(float(value) * 100)
    return struct.pack("<I", scaled)


def read_framed(read_exact: Callable[[int], bytes]) -> bytes:
    """Read exactly one length-delimited frame from a byte transport."""

    prefix = read_exact(3)
    if len(prefix) != 3 or prefix[0] != HEADER:
        raise DeviceError("JW8105 response does not start with 0x7B", "parse_error")
    length = prefix[2]
    if length < 5:
        raise DeviceError("JW8105 response length is below the protocol minimum", "parse_error")
    remainder = read_exact(length - 1)
    if len(remainder) != length - 1:
        raise DeviceError("JW8105 response ended before its declared length", "parse_error")
    return prefix + remainder
