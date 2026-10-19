"""6362D B.11 response decoding. No transport or hardware access."""

from __future__ import annotations

import re
from typing import Any

import numpy as np

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import block_payload

RESOLUTIONS = (0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0)
SENSITIVITIES = {2: "MID", 3: "HIGH1", 4: "HIGH2", 5: "HIGH3", 6: "NORMAL"}
SWEEP_MODES = {1: "SINGLE", 2: "REPEAT", 3: "AUTO", 4: "SEGMENT"}
POWER_UNITS = {0: "dBm", 1: "W", 2: "dBm/nm", 3: "W/nm"}
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?\Z")

# Filled only from an archived, frozen ASCII/REAL64 hardware comparison.
# Keys are exact *IDN? software versions, never inferred from a product family.
BINARY_QUALIFICATIONS: dict[str, str] = {"2.2.1": "<"}


def token(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").strip().strip('"').upper()
    except UnicodeError as exc:
        raise DeviceError("6362D non-ASCII scalar response", "parse_error") from exc
    if not value:
        raise DeviceError("6362D empty response", "parse_error")
    return value


def scalar(raw: bytes) -> float:
    value = token(raw)
    if not NUMBER.fullmatch(value):
        raise DeviceError(f"6362D invalid numeric response: {value[:80]}", "parse_error")
    result = float(value)
    if not np.isfinite(result) or abs(result) >= 9e30:
        raise DeviceError("6362D unavailable numeric value", "parse_error")
    return result


def integer(raw: bytes) -> int:
    value = scalar(raw)
    if not value.is_integer():
        raise DeviceError("6362D expected an integer", "parse_error")
    return int(value)


def mapped(raw: bytes, choices: dict[int, Any]) -> Any:
    value = integer(raw)
    if value not in choices:
        raise DeviceError(f"6362D unknown enum value {value}", "parse_error")
    return choices[value]


def boolean(raw: bytes) -> bool:
    return mapped(raw, {0: False, 1: True})


def nanometers(raw: bytes) -> float:
    # The query returns SI metres. The manual's 0.02 nm RBW example contains
    # an inconsistent exponent; never copy that example as a scale factor.
    return scalar(raw) * 1e9


def data_format(raw: bytes) -> str:
    value = token(raw).replace(" ", "")
    value = {"ASC": "ASCII", "REAL": "REAL,64"}.get(value, value)
    if value not in {"ASCII", "REAL,32", "REAL,64"}:
        raise DeviceError(f"6362D unknown transfer format {value}", "parse_error")
    return value


def values(raw: bytes, count: int, fmt: str, order: str | None) -> np.ndarray:
    if not 1 <= count <= 50001:
        raise DeviceError("6362D empty or excessive trace point count", "parse_error")
    if fmt == "ASCII":
        payload = block_payload(raw) if raw.startswith(b"#") else raw
        fields = payload.strip().split(b",")
        if len(fields) != count:
            raise DeviceError("6362D ASCII trace point count mismatch", "parse_error")
        result = np.asarray([scalar(item) for item in fields], dtype=np.float64)
    else:
        if fmt not in {"REAL,32", "REAL,64"} or order not in {"<", ">"}:
            raise DeviceError("6362D binary byte order is not qualified", "unsupported")
        width = 8 if fmt == "REAL,64" else 4
        # B.11 SOCKET replies from the 6362D are fixed-length binary payloads
        # without an IEEE header.  Keep accepting a complete IEEE block for
        # VISA/back-end fixtures, but never infer a length from a timeout.
        payload = block_payload(raw) if raw.startswith(b"#") else raw
        if len(payload) != count * width:
            raise DeviceError("6362D binary trace byte count mismatch", "parse_error")
        result = np.frombuffer(payload, dtype=f"{order}f{width}").astype(np.float64)
    if not np.isfinite(result).all() or np.any(np.abs(result) >= 9e30):
        raise DeviceError("6362D invalid trace values", "parse_error")
    return result


def wavelength_axis(raw: bytes, count: int, fmt: str, order: str | None) -> np.ndarray:
    result = values(raw, count, fmt, order) * 1e9
    if np.any(result < 600 - 1e-6) or np.any(result > 1700 + 1e-6):
        raise DeviceError("6362D wavelength axis outside 600..1700 nm", "parse_error")
    if count > 1 and not np.all(np.diff(result) > 0):
        raise DeviceError("6362D wavelength axis is not strictly increasing", "parse_error")
    return result
