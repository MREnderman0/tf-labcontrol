"""FPH spectrum trace protocol, independent of other spectrum analyzers."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from lab_control.domain.instruments import DeviceError, Measurement
from lab_control.instruments.base import number
from lab_control.transport.base import block_payload

POINTS = 711
UNITS = {"DBM": "dBm", "DBMV": "dBmV", "DBUV": "dBuV", "V": "V", "W": "W"}
RBW = tuple(float(m * 10**e) for e in range(2, 7) for m in (1, 3) if m * 10**e <= 1e6)
VBW = tuple(float(m * 10**e) for e in range(1, 7) for m in (1, 3) if m * 10**e <= 1e6)


def token(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").strip().strip('"').upper()
    except UnicodeError as exc:
        raise DeviceError("Invalid FPH text response", "parse_error") from exc
    if not value:
        raise DeviceError("Empty FPH response", "parse_error")
    return value


def choice(raw: bytes, choices: tuple[str, ...]) -> str:
    value = token(raw)
    value = {"ASCII": "ASC", "REAL,32": "REAL", "VOLT": "V", "WATT": "W"}.get(value, value)
    if value not in choices:
        raise DeviceError(f"Unexpected FPH enum {value!r}", "parse_error")
    return value


def boolean(raw: bytes) -> bool:
    return choice(raw, ("0", "1", "ON", "OFF")) in {"1", "ON"}


def scalar(raw: bytes) -> float:
    value = number(raw)
    # The manufacturer's INF/NAN sentinel values are finite IEEE floats.
    if abs(value) >= 9e37:
        raise DeviceError("FPH returned an unavailable numeric value", "parse_error")
    return value


def integer(raw: bytes) -> int:
    value = scalar(raw)
    if not value.is_integer():
        raise DeviceError("Expected an FPH integer", "parse_error")
    return int(value)


def trace_values(raw: bytes, config: dict[str, Any]) -> np.ndarray:
    payload = block_payload(raw) if raw.startswith(b"#") else raw
    try:
        if config["transfer_format"] == "ASC" and not raw.startswith(b"#"):
            values = np.array([float(v) for v in payload.decode("ascii").strip().split(",")])
        elif config["transfer_format"] == "REAL" and raw.startswith(b"#"):
            if config["byte_order"] not in {"NORM", "SWAP"} or len(payload) != POINTS * 4:
                raise ValueError("invalid REAL32 byte order or length")
            # V1.40 returns little endian even when FORM:BORD? reports NORM.
            # Explicit firmware context, never a decode-and-retry heuristic.
            order = config.get("binary_encoding", config["byte_order"])
            if order not in {"NORM", "SWAP"}:
                raise ValueError("unqualified binary encoding")
            values = np.frombuffer(payload, dtype=">f4" if order == "NORM" else "<f4").copy()
        else:
            raise ValueError("reply disagrees with selected transfer format")
        if values.size != POINTS or config["sweep_points"] != POINTS:
            raise ValueError("expected 711 display trace values")
        if not np.isfinite(values).all() or np.any(np.abs(values) >= 9e37):
            raise ValueError("unavailable/nonfinite trace values")
    except (ValueError, UnicodeError, KeyError) as exc:
        raise DeviceError(f"Invalid FPH trace: {exc}", "parse_error") from exc
    return values


def trace(raw: bytes, config: dict[str, Any]) -> Measurement:
    values = trace_values(raw, config)
    try:
        start, stop, center, span, duration = (
            float(config[k])
            for k in ("start_frequency", "stop_frequency", "center_frequency", "span", "sweep_time")
        )
        if not all(np.isfinite(v) for v in (start, stop, center, span, duration)):
            raise ValueError("nonfinite axis")
        if not 0 <= start <= stop <= config["maximum_frequency"]:
            raise ValueError("frequency endpoints outside the installed hardware range")
        if abs(stop - start - span) > 1 or abs((start + stop) / 2 - center) > 0.5:
            raise ValueError("inconsistent frequency configuration")
        if config["measurement_mode"] != "SAN" or config["frequency_offset"] != 0:
            raise ValueError("only ordinary, unshifted spectrum mode is qualified")
        if span == 0:
            if duration <= 0:
                raise ValueError("invalid zero-span duration")
            axis, axis_unit = np.linspace(0, duration, POINTS), "s"
        else:
            axis, axis_unit = np.linspace(start, stop, POINTS), "Hz"
        unit = UNITS[config["amplitude_unit"]]
    except (KeyError, ValueError, TypeError) as exc:
        raise DeviceError(f"Invalid FPH trace context: {exc}", "parse_error") from exc
    return Measurement(
        "spectrum",
        unit,
        values,
        axis=axis,
        axis_unit=axis_unit,
        metadata={
            "trace": 1,
            "points": POINTS,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "point_count_source": "FPH manual revision 21, p.358: fixed 711 display points",
            "detector_note": "Auto peak transfers positive peaks only",
            "settings": dict(config),
        },
    )
