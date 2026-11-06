"""Validated RTM3004 REAL32 blocks and waveform coordinates."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import numpy as np

from lab_control.domain.instruments import DeviceError, Measurement
from lab_control.transport.base import block_payload

POINTS_MAX = 4_000_000


def decimal_rounding(raw):
    """Half the last decimal place explicitly supplied by the instrument."""
    try:
        value = Decimal(raw.decode("ascii").strip() if isinstance(raw, bytes) else raw)
        if not value.is_finite():
            raise ValueError("non-finite coordinate")
        return float(Decimal(5).scaleb(int(value.as_tuple().exponent) - 1))
    except (ValueError, UnicodeError, InvalidOperation, OverflowError) as exc:
        raise DeviceError("Invalid coordinate precision", "parse_error") from exc


@dataclass(frozen=True)
class Header:
    start: float
    stop: float
    points: int
    tolerance: float

    @classmethod
    def parse(cls, raw):
        try:
            tokens = raw.decode("ascii").strip().split(",")
            start, stop, count, width = map(float, tokens)
            # Firmware 01.400 returns this complete sentinel for a channel with
            # no acquired record (e.g. enabled while the instrument is stopped).
            if (start, stop, count, width) == (0, 0, 0, 0):
                raise DeviceError("RTM channel has no acquired waveform", "waveform_unavailable")
            if (
                not np.isfinite([start, stop, count, width]).all()
                or not 2 <= count <= POINTS_MAX
                or count != int(count)
                or width != 1
                or stop <= start
            ):
                raise ValueError("invalid bounds/count or envelope/peak samples")
            tolerance = max(decimal_rounding(x) for x in tokens[:2])
            return cls(start, stop, int(count), tolerance)
        except (ValueError, UnicodeError, InvalidOperation, OverflowError) as exc:
            raise DeviceError(f"Invalid RTM waveform header {raw!r}: {exc}", "parse_error") from exc


def payload_values(raw):
    """Validate the complete bounded block before classifying any header change."""
    payload = block_payload(raw)
    if len(payload) % 4 or not 2 <= len(payload) // 4 <= POINTS_MAX:
        raise DeviceError("Invalid REAL32 sample count", "parse_error")
    values = np.frombuffer(payload, dtype="<f4").astype(float)
    if not np.isfinite(values).all():
        raise DeviceError("Non-finite waveform", "parse_error")
    return values


def growing_header(before, after, values, origin, increment, increment_after, precision):
    """Accept only a growing visible prefix on an unchanged, independently read time grid."""
    if after.points <= before.points or not before.points <= len(values) <= after.points:
        return None
    if increment <= 0 or increment != increment_after:
        return None
    for head in (before, after):
        tolerance = head.tolerance + precision
        if abs(head.start - origin) > tolerance:
            return None
        if abs(head.stop - (origin + (head.points - 1) * increment)) > tolerance:
            return None
    return Header(
        origin, origin + (len(values) - 1) * increment, len(values), max(before.tolerance, after.tolerance)
    )


def rollover_header(before, after, values, origin, increment, origin_after, increment_after, precision):
    """A restarted sweep may retain either complete endpoint record, never infer an intermediate one."""
    if after.points >= before.points or len(values) not in {before.points, after.points}:
        return None
    if increment <= 0 or increment != increment_after or origin != origin_after:
        return None
    for head in (before, after):
        tolerance = head.tolerance + precision
        if abs(head.start - origin) > tolerance:
            return None
        if abs(head.stop - (origin + (head.points - 1) * increment)) > tolerance:
            return None
    return before if len(values) == before.points else after


def waveform(raw, header, origin, increment, channel, *, coordinate_error=0.0):
    values = payload_values(raw)
    if values.size != header.points:
        raise DeviceError("REAL32 byte count differs from waveform header", "parse_error")
    if not np.isfinite([origin, increment]).all() or increment <= 0:
        raise DeviceError("Non-finite waveform or invalid increment", "parse_error")
    axis = origin + np.arange(header.points) * increment
    # Header and XINC/XOR are rounded decimal replies. This bounds their
    # exported precision, independently of the vertical range.
    tolerance = header.tolerance + coordinate_error
    if abs(axis[0] - header.start) > tolerance or abs(axis[-1] - header.stop) > tolerance:
        raise DeviceError("Waveform coordinates disagree with header", "parse_error")
    return Measurement(
        "voltage",
        "V",
        values,
        channel,
        axis,
        "s",
        metadata={
            "header": [header.start, header.stop, header.points, 1],
            "encoding": "IEEE754 REAL32 little endian",
        },
    )
