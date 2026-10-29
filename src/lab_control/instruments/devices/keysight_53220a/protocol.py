"""53220A response validation, including finite SCPI invalid-value sentinels."""

import math
import re
import struct

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import block_payload


def token(raw: bytes) -> str:
    try:
        return raw.decode("ascii").strip().strip('"').upper()
    except UnicodeError as exc:
        raise DeviceError("Non-ASCII configuration response", "parse_error") from exc


def numeric(raw: bytes) -> float:
    try:
        value = float(token(raw))
    except ValueError as exc:
        raise DeviceError("Invalid numeric configuration", "parse_error") from exc
    if not math.isfinite(value) or abs(value) >= 9e37:
        raise DeviceError("53220A returned an unavailable/overflow value", "overload")
    return value


def level(raw: bytes) -> float | None:
    # Auto level on an inactive input legitimately has no absolute readback.
    if token(raw) in {"+9.91000000000000E+037", "+9.91000000000000E+37"}:
        return None
    try:
        if float(token(raw)) == 9.91e37:
            return None
    except ValueError:
        pass
    return numeric(raw)


def boolean(raw: bytes) -> bool:
    value = token(raw)
    if value not in {"0", "1", "+0", "+1", "ON", "OFF"}:
        raise DeviceError("Invalid boolean response", "parse_error")
    return value in {"1", "+1", "ON"}


def frequency_channel(configuration: str) -> int | None:
    # SENS:FUNC? reports the current function, including front-panel changes.
    # CONF? only remembers the last CONF/MEAS command and may be empty at power-on.
    channel = re.fullmatch(r"FREQ(?:UENCY)?(?:\s+([12]))?", configuration)
    return int(channel[1] or 1) if channel else None


def frequency_block(raw: bytes, byte_order: str) -> float:
    payload = block_payload(raw)
    if len(payload) != 8 or byte_order not in {"NORM", "NORMAL", "SWAP", "SWAPPED"}:
        raise DeviceError("Expected one REAL,64 frequency reading", "parse_error")
    value = struct.unpack(">d" if byte_order.startswith("NORM") else "<d", payload)[0]
    if not math.isfinite(value) or abs(value) >= 9e37:
        raise DeviceError("No valid frequency reading (timeout/overflow)", "overload")
    if not 0 < value <= 350e6:
        raise DeviceError("Invalid CH1/CH2 frequency reading", "parse_error")
    return value
