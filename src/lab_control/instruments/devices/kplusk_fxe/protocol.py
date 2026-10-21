"""Read-only binary frequency report decoding for the K+K FXE library."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any

from lab_control.domain.instruments import DeviceError, ValidationError

RT_NONE = 0
RT_ERROR = 1
RT_PHASE = 3
RT_DOUBLE = 4
MAX_CHANNELS = 24


@dataclass(frozen=True)
class FrequencyReport:
    """One complete K+K frequency report, with one value per device channel."""

    header: int
    device_ms: int
    values: tuple[float, ...]

    @property
    def interval_ms(self) -> int:
        # The FXE header is mmmmiiiippss0000.  The interval nibble uses the
        # same 1, 2, 5 ... sequence documented by K+K.
        intervals = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)
        index = (self.header & 0x0F00) >> 8
        return intervals[index] if index < len(intervals) else 0


@dataclass(frozen=True)
class PhaseReport:
    """One cumulative K+K phase report, measured in input periods."""

    header: int
    device_ms: int
    values: tuple[float, ...]

    @property
    def interval_ms(self) -> int:
        intervals = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)
        index = (self.header & 0x0F00) >> 8
        return intervals[index] if index < len(intervals) else 0


def _report_type(report: Any) -> int:
    value = report.get_report_type()
    return int(getattr(value, "value", value))


def parse_frequency_report(report: Any) -> FrequencyReport | None:
    """Decode a vendor ``KK_Report`` without importing the vendor module.

    Non-frequency reports are deliberately ignored so the caller can drain
    timestamp, status and heartbeat reports before the next measurement.
    """

    report_type = _report_type(report)
    if report_type == RT_NONE:
        return None
    if report_type == RT_ERROR:
        text = getattr(report, "get_error_text", lambda: "K+K report error")() or "K+K report error"
        raise DeviceError(str(text), "device_error")
    if report_type != RT_DOUBLE:
        return None
    header = int(report.get_header())
    if not 0x2000 <= header < 0x4000:
        return None
    count = int(report.get_channel_count())
    if not 1 <= count <= MAX_CHANNELS:
        raise DeviceError(f"K+K frequency report channel count is invalid: {count}", "parse_error")
    content = report.get_content()
    if content is None or len(content) < count * 8:
        raise DeviceError("K+K frequency report is truncated", "parse_error")
    values = struct.unpack("<" + "d" * count, bytes(content[: count * 8]))
    if not all(math.isfinite(value) for value in values):
        raise DeviceError("K+K frequency report contains a non-finite value", "parse_error")
    device_ms = int(getattr(report, "get_device_ms", lambda: -1)())
    return FrequencyReport(header, device_ms, tuple(values))


def parse_phase_report(report: Any) -> PhaseReport | None:
    """Decode an official ``RT_PHASE`` report without importing K+K types.

    FXE phase reports contain one little-endian signed integer period count and
    one little-endian fractional period per channel.  The cumulative values
    can be differenced over the device timestamp to obtain a read-only Hz
    estimate when a device is currently configured for phase-average output.
    """

    if _report_type(report) != RT_PHASE:
        return None
    header = int(report.get_header())
    if not 0 <= header < 0x2000:
        return None
    count = int(report.get_channel_count())
    if not 1 <= count <= MAX_CHANNELS:
        raise DeviceError(f"K+K phase report channel count is invalid: {count}", "parse_error")
    phases = getattr(report, "get_phases", None)
    if callable(phases):
        values = tuple(float(value.to_float()) for value in phases())
    else:
        content = report.get_content()
        if content is None or len(content) < count * 16:
            raise DeviceError("K+K phase report is truncated", "parse_error")
        values = tuple(
            float(high) + float(low)
            for high, low in (
                struct.unpack_from("<qd", bytes(content), index * 16) for index in range(count)
            )
        )
    if len(values) != count or not all(math.isfinite(value) for value in values):
        raise DeviceError("K+K phase report contains a non-finite value", "parse_error")
    device_ms = int(getattr(report, "get_device_ms", lambda: -1)())
    return PhaseReport(header, device_ms, values)


def validate_channels(channels: tuple[int, ...]) -> tuple[int, ...]:
    if not channels or any(type(channel) is not int or not 1 <= channel <= MAX_CHANNELS for channel in channels):
        raise ValidationError(f"K+K channels must be unique integers in 1..{MAX_CHANNELS}")
    if len(set(channels)) != len(channels):
        raise ValidationError("K+K channels must not be repeated")
    return channels
