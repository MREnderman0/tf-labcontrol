"""DSA800 wire formats. No I/O, fallback values, or partial trace acceptance."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from lab_control.domain.instruments import DeviceError, Measurement
from lab_control.instruments.base import number
from lab_control.transport.base import block_payload

UNITS = {"DBM": "dBm", "DBMV": "dBmV", "DBUV": "dBuV", "V": "V", "W": "W"}
RBW = tuple(float(m * 10**e) for e in range(1, 7) for m in (1, 3) if m * 10**e <= 1e6)
VBW = tuple(float(m * 10**e) for e in range(7) for m in (1, 3) if m * 10**e <= 3e6)


def token(raw: bytes) -> str:
    try:
        return raw.decode("ascii").strip().strip('"').upper()
    except UnicodeError as exc:
        raise DeviceError("Invalid DSA815 text response", "parse_error") from exc


def option_table(raw: bytes) -> str:
    # The option type column follows the panel language (GBK on Chinese firmware).
    try:
        value = raw.decode("gb18030").strip()
    except UnicodeError as exc:
        raise DeviceError("Invalid DSA815 option table encoding", "parse_error") from exc
    rows = value.splitlines()
    if (
        len(rows) < 2
        or len(rows[0].split("|")) != 4
        or "OPTION" not in rows[0].upper()
        or "ACTIVE" not in rows[0].upper()
    ):
        raise DeviceError("Missing DSA815 option table header", "parse_error")
    for row in rows[1:]:
        fields = [f.strip() for f in row.split("|")]
        if len(fields) != 4 or not fields[0].isdigit() or fields[-1] not in {"Y", "N"}:
            raise DeviceError("Invalid DSA815 option table row", "parse_error")
    return value


def boolean(raw: bytes) -> bool:
    value = token(raw)
    if value not in {"0", "1", "OFF", "ON"}:
        raise DeviceError("Invalid DSA815 boolean", "parse_error")
    return value in {"1", "ON"}


def integer(raw: bytes) -> int:
    value = number(raw)
    if not value.is_integer():
        raise DeviceError("Expected DSA815 integer", "parse_error")
    return int(value)


def choice(raw: bytes, choices: tuple[str, ...]) -> str:
    value = token(raw)
    aliases = {"BLANK": "BLAN", "ASCII": "ASC", "REAL,32": "REAL"}
    value = aliases.get(value, value)
    if value not in choices:
        raise DeviceError(f"Unexpected DSA815 enum: {value!r}", "parse_error")
    return value


def trace_values(raw: bytes, config: dict[str, Any]) -> np.ndarray:
    payload = block_payload(raw) if raw.startswith(b"#") else raw
    try:
        if config["transfer_format"] == "ASC":
            values = np.asarray([float(v) for v in payload.decode("ascii").strip().split(",")])
        elif config["transfer_format"] == "REAL" and raw.startswith(b"#"):
            if len(payload) % 4:
                raise ValueError("unaligned REAL32 payload")
            values = np.frombuffer(payload, dtype=">f4" if config["byte_order"] == "NORM" else "<f4").copy()
        else:
            raise ValueError("unexpected trace format")
        points = config["sweep_points"]
        if type(points) is not int or points != 601 or values.size != points:
            raise ValueError("trace point count differs from sweep points")
        if not np.isfinite(values).all():
            raise ValueError("non-finite trace values")
    except (ValueError, KeyError, UnicodeError) as exc:
        raise DeviceError(f"Invalid DSA815 trace: {exc}", "parse_error") from exc
    return values


def trace(raw: bytes, config: dict[str, Any]) -> Measurement:
    values = trace_values(raw, config)
    points = config["sweep_points"]
    try:
        start, stop, span = (config[k] for k in ("start_frequency", "stop_frequency", "span"))
        if not all(np.isfinite(config[k]) for k in ("center_frequency", "span", "sweep_time")):
            raise ValueError("non-finite axis configuration")
        if not 0 <= start <= stop <= 1.5e9 or abs(stop - start - span) > 1:
            raise ValueError("inconsistent frequency axis")
        if abs((start + stop) / 2 - config["center_frequency"]) > 0.5:
            raise ValueError("inconsistent center frequency")
        if span == 0:
            duration = config["sweep_time"]
            if not np.isfinite(duration) or not 20e-6 <= duration <= 7500:
                raise ValueError("invalid zero-span sweep time")
            axis, axis_unit = np.linspace(0, duration, points), "s"
        else:
            axis, axis_unit = np.linspace(start, stop, points), "Hz"
        unit = UNITS[config["amplitude_unit"]]
    except (ValueError, KeyError, UnicodeError) as exc:
        raise DeviceError(f"Invalid DSA815 trace: {exc}", "parse_error") from exc
    return Measurement(
        "spectrum",
        unit,
        values,
        axis=axis,
        axis_unit=axis_unit,
        metadata={
            "trace": 1,
            "point_count_source": "DSA815 fixed 601 display points; User Guide 2-33 and 5-7",
            "points": points,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "settings": dict(config),
        },
    )
