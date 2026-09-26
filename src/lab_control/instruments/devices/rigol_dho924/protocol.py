"""DHO924 scalar and WORD/preamble contract (DHO800900 PG, chapter 3.28)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal

import numpy as np

from lab_control.domain.instruments import DeviceError, Measurement
from lab_control.transport.base import block_payload

POINTS_MAX = 10_000_000
FRAME_BYTES_MAX = 256 * 1024 * 1024


def token(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").strip().strip('"').upper()
    except UnicodeError as exc:
        raise DeviceError("Invalid scalar encoding", "parse_error") from exc
    if not value or any(c in value for c in "\r\n;"):
        raise DeviceError("Invalid scalar boundary", "parse_error")
    return value


def numeric(raw: bytes) -> float:
    try:
        value = float(token(raw))
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        return value
    except ValueError as exc:
        raise DeviceError("Invalid finite scalar", "parse_error") from exc


def integer(raw: bytes) -> int:
    value = numeric(raw)
    if int(value) != value:
        raise DeviceError("Expected integer response", "parse_error")
    return int(value)


def boolean(raw: bytes) -> bool:
    value = token(raw)
    if value not in {"0", "1", "ON", "OFF"}:
        raise DeviceError("Invalid boolean", "parse_error")
    return value in {"1", "ON"}


def equivalent(a, b) -> bool:
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        return isinstance(b, (int, float)) and math.isclose(a, b, rel_tol=5e-7, abs_tol=1e-15)
    return a == b


def decimal_rounding(text: str) -> float:
    """Half the last printed decimal unit, after a finite scalar was validated."""
    exponent = Decimal(text).as_tuple().exponent
    if not isinstance(exponent, int):
        raise DeviceError("Nonfinite decimal precision", "parse_error")
    return float(Decimal(10) ** exponent) / 2


@dataclass(frozen=True)
class Preamble:
    format: int
    type: int
    points: int
    count: int
    x_increment: float
    x_origin: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float

    @classmethod
    def parse(cls, raw: bytes) -> Preamble:
        fields = token(raw).split(",")
        if len(fields) != 10:
            raise DeviceError("Preamble requires ten fields", "parse_error")
        values = [numeric(v.encode("ascii")) for v in fields]
        if any(int(v) != v for v in values[:4]):
            raise DeviceError("Noninteger preamble header", "parse_error")
        result = cls(*(int(v) for v in values[:4]), *values[4:])
        if (
            result.format not in (0, 1, 2)
            or result.type not in (0, 1, 2)
            or not 1 <= result.points <= POINTS_MAX
            or not 1 <= result.count <= 65536
            or result.x_increment <= 0
            or result.y_increment <= 0
            or result.x_reference != 0
        ):
            raise DeviceError("Unsupported/invalid waveform preamble", "parse_error")
        return result

    def signature(self) -> tuple:
        # Acquisition timestamp is not supplied by this protocol. Do not infer
        # new triggers from hashes; horizontal origin remains separately checked.
        return (
            self.format,
            self.type,
            self.points,
            self.count,
            self.x_increment,
            self.y_increment,
            self.y_origin,
            self.y_reference,
        )

    def check_screen(
        self, raw: bytes, *, scale: float, offset: float, timebase: float, position: float
    ) -> None:
        """Bind descriptor-visible configuration to the original settings snapshot.

        Gain uses its returned decimal precision; integer YORIGIN may truncate
        one count. Horizontal origin allows one screen sample of normal jitter.
        No fresh scalar queries and no cached gain are used to decode samples.
        """
        fields = token(raw).split(",")
        expected_gain = scale / 7500
        differences = (
            abs(self.x_increment - timebase / 100) > decimal_rounding(fields[4]) + abs(timebase) * 1e-14,
            abs(self.y_increment - expected_gain) > decimal_rounding(fields[7]) + abs(expected_gain) * 1e-12,
            abs(self.y_origin - offset / expected_gain) > 1 + abs(self.y_origin) * 5e-7,
            abs(self.x_origin - (position - 5 * timebase))
            > self.x_increment + decimal_rounding(fields[5]) + abs(position) * 5e-7,
        )
        if any(differences):
            raise DeviceError("Screen descriptor differs from settings snapshot", "configuration_changed")


def waveform(raw: bytes, preamble: Preamble, channel: int, unit: str, *, start: int = 1) -> Measurement:
    payload = block_payload(raw)
    if preamble.format != 1 or len(payload) != preamble.points * 2 or start != 1:
        raise DeviceError("WORD length/format/window differs from preamble", "parse_error")
    # Qualified against ASCII on the same stopped waveform by the hardware entry.
    codes = np.frombuffer(payload, dtype="<u2")
    values = (codes.astype(np.float64) - preamble.y_origin - preamble.y_reference) * preamble.y_increment
    axis = (
        np.arange(codes.size, dtype=np.float64) - preamble.x_reference
    ) * preamble.x_increment + preamble.x_origin
    if (
        not np.isfinite(values).all()
        or not np.isfinite(axis).all()
        or (axis.size > 1 and not np.all(np.diff(axis) > 0))
    ):
        raise DeviceError("Nonfinite or nonmonotonic waveform", "parse_error")
    return Measurement(
        "waveform",
        unit,
        values,
        channel,
        axis,
        "s",
        metadata={
            "preamble": asdict(preamble),
            "sample_encoding": "WORD_LE_U16",
            "actual_points": codes.size,
            "acquisition_key": None,
        },
    )
