"""Per-channel temperature extrema owned by the monitoring service."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from lab_control.domain.instruments import Quality
from lab_control.domain.rolling_extrema import WINDOW_SECONDS, append_extrema, expire_extrema


@dataclass(frozen=True)
class TemperaturePoint:
    at: float
    value: float


@dataclass(frozen=True)
class TemperatureSnapshot:
    channel: int
    unit: str
    count: int
    minimum: float | None
    maximum: float | None

    @property
    def peak_to_peak(self) -> float | None:
        return None if self.minimum is None or self.maximum is None else self.maximum - self.minimum


@dataclass
class _Channel:
    unit: str
    points: deque = field(default_factory=deque)
    minima: deque = field(default_factory=deque)
    maxima: deque = field(default_factory=deque)


class TemperatureWindow:
    """All access uses MonitoringService.lock; no UI or sample-count limit."""

    def __init__(self):
        self.channels: dict[int, _Channel] = {}
        self.frame = None
        self.frozen_at = None

    def add(self, frame) -> bool:
        if frame.request.kind != "scalar" or not math.isfinite(frame.received_at):
            return False
        if self.frame and (
            frame.sequence <= self.frame.sequence or frame.received_at < self.frame.received_at
        ):
            return False
        measurements = frame.batch.measurements
        if not measurements or len({m.channel for m in measurements}) != len(measurements):
            return False
        if frame.request.channels != (1,) and {m.channel for m in measurements} != set(
            frame.request.channels
        ):
            return False
        values = []
        for m in measurements:
            if (
                m.quantity != "temperature"
                or m.unit not in {"°C", "°F", "K"}
                or m.quality not in {Quality.GOOD, Quality.SIMULATED}
            ):
                return False
            array = np.asarray(m.values)
            if array.size != 1 or array.dtype.kind not in "iuf":
                return False
            value = float(array.reshape(-1)[0])
            if not math.isfinite(value):
                return False
            values.append(value)
        for m, value in zip(measurements, values, strict=True):
            history = self.channels.get(m.channel)
            if history is None or history.unit != m.unit:
                history = self.channels[m.channel] = _Channel(m.unit)
            append_extrema(
                history.points,
                history.minima,
                history.maxima,
                TemperaturePoint(frame.received_at, value),
                lambda p: p.value,
            )
        self.frame = frame
        self.advance(frame.received_at)
        return True

    def advance(self, now: float, *, stopped=False) -> None:
        if stopped and self.frozen_at is None:
            self.frozen_at = now
        end = now if self.frozen_at is None else self.frozen_at
        for history in self.channels.values():
            expire_extrema(history.points, history.minima, history.maxima, end - WINDOW_SECONDS)

    def snapshot(self, now: float, *, stopped=False) -> tuple[TemperatureSnapshot, ...]:
        self.advance(now, stopped=stopped)
        return tuple(
            TemperatureSnapshot(
                ch,
                h.unit,
                len(h.points),
                h.minima[0].value if h.minima else None,
                h.maxima[0].value if h.maxima else None,
            )
            for ch, h in sorted(self.channels.items())
        )
