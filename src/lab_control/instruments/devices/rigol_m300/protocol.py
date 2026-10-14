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
    if any(value != value or value in (float("inf"), float("-inf")) for value in values):
        raise DeviceError("Non-finite M300 measurement", "overload")
    return values


def configuration(raw: bytes) -> str:
    value = token(raw)
    if not value:
        raise DeviceError("Empty M300 channel configuration", "parse_error")
    return value
