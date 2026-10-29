"""Pure M300 SCPI response parsing."""

from __future__ import annotations

import re

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.base import number
from lab_control.transport.base import block_payload


def token(raw: bytes) -> str:
    try:
        value = block_payload(raw) if raw.startswith(b"#") else raw
        return value.decode("ascii", errors="strict").strip().strip('"')
    except (UnicodeError, ValueError) as exc:
        raise DeviceError("Invalid M300 text response", "parse_error") from exc


def choice(raw: bytes, choices: tuple[str, ...]) -> str:
    value = token(raw).upper()
    aliases = {"IMMEDIATE": "IMM", "TIMER": "TIM"}
    value = aliases.get(value, value)
    if value not in choices:
        raise DeviceError(f"Unexpected M300 enum: {value!r}", "parse_error")
    return value


def boolean(raw: bytes) -> bool:
    value = token(raw).upper()
    if value not in {"0", "1", "OFF", "ON"}:
        raise DeviceError("Invalid M300 boolean", "parse_error")
    return value in {"1", "ON"}


def integer(raw: bytes) -> int:
    value = number(raw)
    if not value.is_integer():
        raise DeviceError("Expected M300 integer", "parse_error")
    return int(value)


def module_identity(raw: bytes) -> str:
    value = token(raw)
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4 or not parts[1]:
        raise DeviceError("Invalid M300 slot identity", "parse_error")
    return value


def scan_list(raw: bytes) -> str:
    value = token(raw)
    if not re.fullmatch(r"\(@(?:\d{3}(?::\d{3})?(?:,\d{3}(?::\d{3})?)*)?\)", value):
        raise DeviceError("Invalid M300 scan list", "parse_error")
    return value


def csv_numbers(raw: bytes) -> tuple[float, ...]:
    value = token(raw)
    if not value:
        return ()
    try:
        values = tuple(float(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise DeviceError("Invalid M300 measurement response", "parse_error") from exc
    if any(not abs(value) < 9e36 for value in values):
        raise DeviceError("M300 measurement is non-finite, open-circuit or overloaded", "overload")
    return values


def configuration(raw: bytes) -> str:
    value = token(raw)
    if not value:
        raise DeviceError("Empty M300 channel configuration", "parse_error")
    return value


def scan_channels(raw: bytes | str) -> tuple[int, ...]:
    value = scan_list(raw.encode("ascii") if isinstance(raw, str) else raw)[2:-1]
    channels: list[int] = []
    for item in value.split(",") if value else ():
        limits = tuple(int(part) for part in item.split(":"))
        first, last = limits[0], limits[-1]
        if first > last or first // 100 != last // 100:
            raise DeviceError("Invalid M300 channel range", "parse_error")
        channels.extend(range(first, last + 1))
    if len(channels) != len(set(channels)) or channels != sorted(channels):
        raise DeviceError("M300 scan channels must be unique and ascending", "parse_error")
    return tuple(channels)


def function(raw: bytes) -> str:
    value = token(raw).upper()
    return {"VOLT:DC": "VOLT", "VOLTAGE": "VOLT", "RESISTANCE": "RES", "TEMPERATURE": "TEMP"}.get(
        value, value
    )


def measurement_kind(config: str, unit: str = "") -> tuple[str, str]:
    """Validate a complete single-channel CONF? before interpreting READ?."""
    match = re.fullmatch(r"(VOLT|RES) ([^,]+),([^,]+)", config)
    if match:
        number(match[2])
        number(match[3])
        return ("voltage", "V") if match[1] == "VOLT" else ("resistance", "ohm")
    match = re.fullmatch(r"TEMP THER,(2252|3000|5000|10000|30000),([^,]+),([^,]+)", config)
    if match:
        number(match[2])
        number(match[3])
        if unit not in {"C", "F", "K"}:
            raise DeviceError("Invalid M300 temperature unit", "parse_error")
        return "temperature", {"C": "°C", "F": "°F", "K": "K"}[unit]
    raise DeviceError("Unsupported or malformed M300 channel configuration: " + config, "parse_error")
