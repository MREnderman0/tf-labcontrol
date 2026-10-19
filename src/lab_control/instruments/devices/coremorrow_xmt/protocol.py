"""CoreMorrow XMT serial framing and model-owned payload helpers.

The installed CoreMorrow serial library was observed on the selected COM6
asset.  Frames use ``0xAA, address, total_length, B3, B4, payload, xor``;
the length includes every byte in the frame and the final byte is the XOR of
all preceding bytes.  The vendor DLL wire capture established the
single-point voltage encoder used by the E00.C3-13 asset.  Negative setpoints
are deliberately not encoded here; the compatibility layer exposes the
documented non-negative 0..120 V control envelope only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from lab_control.domain.instruments import DeviceError, ValidationError

HEADER = 0xAA
DEFAULT_ADDRESS = 0x01
READ_DATA_COMMAND = 0x05
READ_POWER_CONFIG_COMMAND = 0x50
READ_MCU_NUMBER_COMMAND = 0x64
READ_MULT_CHANNEL_VOLTAGE_COMMAND = 0x33
SINGLE_POINT_COMMAND = 0x00
STANDARD_WAVE_COMMAND = 15
STANDARD_WAVE_STOP_COMMAND = 17
WAVE_CODES = {"SIN": ord("Z"), "SQUARE": ord("F"), "TRIANGLE": ord("S"), "SAWTOOTH": ord("J")}
# E00/E01 E18 interface specification: output waveform frequency up to 10 kHz.
# The official serial encoder uses two integer Hz bytes and 1e-4 Hz fractions.
WAVE_FREQUENCY_MIN = 0.0001
WAVE_FREQUENCY_MAX = 10_000.0


def validate_waveform(parameters: dict) -> dict:
    """Validate both the requested and quantized positive voltage envelope."""
    if parameters.get("waveform") not in WAVE_CODES:
        raise ValidationError("不支持的标准波形")
    for name, low, high in (("frequency", WAVE_FREQUENCY_MIN, WAVE_FREQUENCY_MAX),
                            ("amplitude", 0.0001, 120.0), ("offset", 0.0, 120.0)):
        value = parameters.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValidationError(f"{name}: 必须为有限数值")
        if not low <= value <= high:
            raise ValidationError(f"{name}: 允许范围 {low:g} ～ {high:g}")
    for values in (parameters, {k: round(parameters[k], 4) for k in ("amplitude", "offset")}):
        if values["offset"] - values["amplitude"] / 2 < 0 or values["offset"] + values["amplitude"] / 2 > 120:
            raise ValidationError("波形上下沿必须在 0 ～ 120 V 内（偏移 ± 峰峰值/2）")
    return parameters


def build_standard_waveform(address: int, channel_index: int, parameters: dict) -> bytes:
    """Vendor multi-channel open-loop voltage command; no phase/sweep writes."""
    validate_waveform(parameters)
    _byte(channel_index, "channel_index")
    frequency = round(parameters["frequency"] * 10_000)
    integer, fraction = divmod(frequency, 10_000)
    payload = (
        bytes((channel_index, WAVE_CODES[parameters["waveform"]]))
        + encode_voltage_data(parameters["amplitude"])
        + integer.to_bytes(2, "big") + fraction.to_bytes(2, "big")
        + encode_voltage_data(parameters["offset"])
    )
    return build_frame(address, STANDARD_WAVE_COMMAND, 0, payload)


def build_waveform_stop(address: int, channel_index: int) -> bytes:
    return build_frame(address, STANDARD_WAVE_STOP_COMMAND, 0, bytes((_byte(channel_index, "channel_index"),)))


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


def build_frame(address: int, command_b3: int, command_b4: int = 0, payload: bytes = b"") -> bytes:
    """Build an XMT frame with a length byte and XOR checksum."""

    _byte(address, "address")
    _byte(command_b3, "command_b3")
    _byte(command_b4, "command_b4")
    if not isinstance(payload, bytes) or len(payload) > 248:
        raise ValidationError("XMT request payload is too large")
    total_length = 6 + len(payload)
    body = bytes((HEADER, address, total_length, command_b3, command_b4)) + payload
    return body + bytes((checksum(body),))


def build_read(address: int, command_b3: int, command_b4: int = 0, payload: bytes = b"") -> bytes:
    """Build a read request."""

    return build_frame(address, command_b3, command_b4, payload)


def encode_voltage_data(value: float) -> bytes:
    """Encode a non-negative XMT voltage as integer volts plus 1e-4 volts."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError("XMT voltage must be finite")
    if value < 0:
        raise ValidationError("XMT voltage encoder only accepts non-negative values")
    integer = int(math.floor(float(value)))
    fraction = int(round((float(value) - integer) * 10_000))
    if fraction == 10_000:
        integer += 1
        fraction = 0
    if integer > 0xFF:
        raise ValidationError("XMT voltage is outside the wire encoder range")
    return bytes((0, integer, (fraction >> 8) & 0xFF, fraction & 0xFF))


def build_single_point(address: int, channel_index: int, value: float) -> bytes:
    """Build the vendor ``XMT_COMMAND_SinglePoint`` voltage command.

    ``channel_index`` is zero based in the wire protocol.  The fifth payload
    byte is reserved and remains zero, matching the official DLL capture.
    """

    _byte(channel_index, "channel_index")
    payload = bytes((channel_index,)) + encode_voltage_data(value)
    return build_frame(address, SINGLE_POINT_COMMAND, 0, payload)


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


def decode_voltage_data(data: bytes) -> float:
    """Decode the four-byte signed-magnitude used by B3=0x33 responses."""

    if not isinstance(data, bytes) or len(data) != 4:
        raise DeviceError("XMT voltage data must contain four bytes", "parse_error")
    sign = -1.0 if data[0] & 0x80 else 1.0
    integer = data[1]
    fraction = int.from_bytes(data[2:], "big") / 10_000
    value = sign * (integer + fraction)
    if not math.isfinite(value):  # pragma: no cover - defensive
        raise DeviceError("XMT voltage is non-finite", "parse_error")
    return value


def parse_mult_channel_voltage(frame: XmtFrame, count: int = 3) -> tuple[float, ...]:
    """Decode the common status byte plus up to three channel values."""

    if frame.command_b3 != READ_MULT_CHANNEL_VOLTAGE_COMMAND:
        raise DeviceError("XMT frame is not a multi-channel voltage response", "parse_error")
    if type(count) is not int or not 1 <= count <= 3 or len(frame.payload) != 1 + 4 * count:
        raise DeviceError("XMT multi-channel voltage payload has an unexpected length", "parse_error")
    return tuple(decode_voltage_data(frame.payload[1 + 4 * i : 5 + 4 * i]) for i in range(count))
