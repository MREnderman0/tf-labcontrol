"""SDG6032X-E response grammar; reject truncated, non-finite and wrong-channel data."""

from __future__ import annotations

import re

from lab_control.domain.instruments import DeviceError, Unsupported
from lab_control.instruments.base import number


def body(raw: bytes, header: str) -> str:
    try:
        text = raw.decode("ascii").strip()
    except UnicodeError as exc:
        raise DeviceError("Non-ASCII SDG response", "parse_error") from exc
    if " " in text:
        prefix, text = text.split(None, 1)
        if prefix.upper() != header.upper():
            raise DeviceError(f"Expected {header}, received {prefix}", "parse_error")
    if not text:
        raise DeviceError("Empty SDG response", "parse_error")
    return text


def fields(raw: bytes, header: str) -> dict[str, str]:
    parts = [part.strip() for part in body(raw, header).split(",")]
    if len(parts) % 2 or not all(parts):
        raise DeviceError("Incomplete SDG composite response", "parse_error")
    result = dict(zip((v.upper() for v in parts[::2]), parts[1::2], strict=True))
    if len(result) != len(parts) // 2:
        raise DeviceError("Duplicate SDG field", "parse_error")
    return result


def required(values: dict[str, str], name: str) -> str:
    try:
        return values[name]
    except KeyError as exc:
        raise DeviceError(f"Missing {name} in SDG response", "parse_error") from exc


def numeric(value: str, suffix: str = "") -> float:
    if suffix and value.upper().endswith(suffix.upper()):
        value = value[: -len(suffix)]
    return number(value)


def output(raw: bytes, channel: int) -> tuple[str, float | str]:
    text = body(raw, f"C{channel}:OUTP")
    state, separator, rest = text.partition(",")
    if state.upper() not in {"ON", "OFF"} or not separator:
        raise DeviceError("Invalid SDG output response", "parse_error")
    load = required(fields(rest.encode(), ""), "LOAD").upper()
    return state.upper(), "HZ" if load in {"HZ", "HIZ"} else numeric(load)


FIELDS = {"waveform": "WVTP", "frequency": "FRQ", "amplitude": "AMP", "offset": "OFST", "phase": "PHSE"}
UNITS = {"frequency": "HZ", "amplitude": "V", "offset": "V"}
WAVEFORMS = {"SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "ARB", "DC", "PRBS", "MULTPULSE", "SEQUENCE", "IQ"}


def basic(raw: bytes, channel: int) -> dict[str, str]:
    result = fields(raw, f"C{channel}:BSWV")
    waveform = required(result, "WVTP").upper()
    if waveform not in WAVEFORMS:
        raise DeviceError(f"Unknown SDG waveform: {waveform}", "parse_error")
    for name in ("frequency", "amplitude", "offset", "phase"):
        key = FIELDS[name]
        if waveform in {"SINE", "SQUARE", "RAMP"} or (waveform == "DC" and name == "offset"):
            numeric(required(result, key), UNITS.get(name, ""))
        elif key in result:
            numeric(result[key], UNITS.get(name, ""))
    return result


def basic_value(values: dict[str, str], operation: str):
    key = FIELDS[operation]
    if key not in values:
        raise Unsupported(f"{operation} is unavailable for {values['WVTP']}")
    return values[key] if operation == "waveform" else numeric(values[key], UNITS.get(operation, ""))


def decode_basic(raw: bytes, operation: str):
    # Used only for apply readback; the transport is owned by one serial worker.
    match = re.match(rb"C([12]):BSWV\s", raw.strip(), re.I)
    return basic_value(basic(raw, int(match[1]) if match else 1), operation)
