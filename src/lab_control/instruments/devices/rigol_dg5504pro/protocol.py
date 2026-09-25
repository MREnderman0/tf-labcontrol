"""Pure DG5000 Pro parsing/limits; no driver or hardware access."""

from __future__ import annotations

import csv
import re

from lab_control.domain.instruments import DeviceError, ValidationError
from lab_control.instruments.base import number

FREQUENCIES = {"SIN": 500e6, "SQU": 170e6, "RAMP": 5e6, "PULS": 120e6}


def state(raw: bytes | str | float) -> bool:
    text = raw.decode().strip().upper() if isinstance(raw, bytes) else str(raw).strip().upper()
    if text not in {"ON", "OFF", "1", "0", "1.0", "0.0"}:
        raise DeviceError(f"Invalid boolean response: {text!r}", "parse_error")
    return text in {"ON", "1", "1.0"}


def load_value(raw: bytes) -> float | str:
    text = raw.decode().strip().upper()
    if text in {"INF", "INFINITY", "HIGHZ", "HZ"}:
        return "INF"
    value = number(text)
    return "INF" if value >= 1e30 else value


def trigger_value(raw: bytes) -> str:
    value = raw.decode().strip().upper()
    return {"IMM": "INT", "BUS": "MAN"}.get(value, value)


def validate_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,23}", name):
        raise ValidationError("Waveform name: ASCII letters/digits/underscore, maximum 24")


def catalog(raw: bytes) -> dict[str, int | None]:
    fields = next(csv.reader([raw.decode().strip()], skipinitialspace=True))
    if len(fields) < 2 or not all(item.strip().isdigit() for item in fields[:2]):
        raise DeviceError("Invalid DG file catalog", "parse_error")
    result: dict[str, int | None] = {}
    for entry in fields[2:]:
        parts = entry.rsplit(",", 2)
        if len(parts) != 3:
            raise DeviceError("Malformed DG catalog entry", "parse_error")
        name, kind, size = parts
        if not name or name.casefold() in {key.casefold() for key in result}:
            raise DeviceError("Invalid/duplicate DG catalog entry", "parse_error")
        if kind == "FOLD" and not size:
            result[name] = None
        elif size.isdigit():
            result[name] = int(size)
        else:
            raise DeviceError("Invalid DG file size", "parse_error")
    return result


def amplitude_envelope(frequency: float, waveform: str, load: float | str) -> tuple[float, float]:
    if load not in ("INF", 50.0):
        raise ValidationError("Verified DG voltage limits require 50 ohm or HighZ load")
    factor = 1.0 if load == "INF" else 0.5
    maximum = (
        2
        if waveform == "NOIS"
        else 20
        if frequency <= 100e6
        else 10
        if frequency <= 250e6
        else 4
        if frequency <= 350e6
        else 2
    )
    return 0.002 * factor, maximum * factor
