"""Time-bounded optical-power history owned by a monitoring task, not a widget."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from lab_control.domain.instruments import Quality
from lab_control.domain.power import power_to_watts

if TYPE_CHECKING:
    from lab_control.application.monitoring_state import MonitorFrame

POWER_WINDOW_SECONDS = 1800.0


@dataclass(frozen=True)
class PowerPoint:
    at: float
    watts: float
    segment: int


@dataclass(frozen=True)
class PowerChannelSnapshot:
    channel: int
    points: tuple[PowerPoint, ...]
    minimum: float | None
    maximum: float | None
    timestamp: str
    raw_value: float | None
    raw_unit: str


@dataclass(frozen=True)
class PowerWindowSnapshot:
    context_id: str
    origin: float | None
    now: float
    revision: int
    channels: tuple[PowerChannelSnapshot, ...]
    frame: MonitorFrame | None


@dataclass
class _Channel:
    points: deque[PowerPoint] = field(default_factory=deque)
    minima: deque[PowerPoint] = field(default_factory=deque)
    maxima: deque[PowerPoint] = field(default_factory=deque)
    segment: int = 0
    timestamp: str = ""
    raw_value: float | None = None
    raw_unit: str = ""

    def append(self, at: float, watts: float) -> None:
        point = PowerPoint(at, watts, self.segment)
        self.points.append(point)
        while self.minima and self.minima[-1].watts >= watts:
            self.minima.pop()
        while self.maxima and self.maxima[-1].watts <= watts:
            self.maxima.pop()
        self.minima.append(point)
        self.maxima.append(point)

    def expire(self, cutoff: float) -> bool:
        changed = False
        for queue in (self.points, self.minima, self.maxima):
            while queue and queue[0].at < cutoff:
                queue.popleft()
                changed = True
        return changed


class PowerWindow:
    """All access is serialized by MonitoringService.lock.

    No sample-count cap: a 30-minute window must include every valid sample,
    even when its extremum predates the scalar replay cache's 1200 frames.
    """

    def __init__(self) -> None:
        self.channels: dict[int, _Channel] = {}
        self.origin: float | None = None
        self.frame: MonitorFrame | None = None
        self.frozen_at: float | None = None
        self.revision = 0
        self._cache: tuple[PowerChannelSnapshot, ...] | None = None

    def mark_gap(self, channels: set[int] | None = None) -> None:
        for channel, history in self.channels.items():
            if channels is None or channel in channels:
                history.segment += 1

    def add(self, frame: MonitorFrame) -> bool:
        if frame.request.kind != "scalar" or frame.request.quantity != "power":
            return False
        if self.frame and (
            frame.sequence <= self.frame.sequence or frame.received_at < self.frame.received_at
        ):
            return False
        if not math.isfinite(frame.received_at):
            return False
        measurements = frame.batch.measurements
        if (
            len(measurements) != len(frame.request.channels)
            or {m.channel for m in measurements} != set(frame.request.channels)
            or any(
                m.quantity != "power" or m.quality not in {Quality.GOOD, Quality.SIMULATED}
                for m in measurements
            )
        ):
            return False
        values = [power_to_watts(m.values, m.unit) for m in measurements]
        if any(value is None for value in values):
            return False
        if self.frame and (
            frame.session_id != self.frame.session_id or "_acquisition_gap" in frame.batch.configuration
        ):
            self.mark_gap()
        if self.origin is None:
            self.origin = frame.received_at
        for measurement, watts in zip(measurements, values):
            assert watts is not None
            history = self.channels.setdefault(measurement.channel, _Channel())
            history.append(frame.received_at, watts)
            history.timestamp = frame.batch.timestamp
            # Conversion has already established that this is a finite scalar.
            history.raw_value = float(np.asarray(measurement.values).reshape(-1)[0])
            history.raw_unit = measurement.unit
        self.frame = frame
        self.revision += 1
        self._cache = None
        self.advance(frame.received_at)
        return True

    def advance(self, now: float, *, stopped: bool = False) -> float:
        if stopped and self.frozen_at is None:
            self.frozen_at = now
        end = self.frozen_at if self.frozen_at is not None else now
        changed = False
        for history in self.channels.values():
            changed = history.expire(end - POWER_WINDOW_SECONDS) or changed
        if changed:
            self.revision += 1
            self._cache = None
        return end

    def snapshot(self, context_id: str, now: float, *, stopped: bool = False) -> PowerWindowSnapshot:
        end = self.advance(now, stopped=stopped)
        if self._cache is None:
            self._cache = tuple(
                PowerChannelSnapshot(
                    channel,
                    tuple(history.points),
                    history.minima[0].watts if history.minima else None,
                    history.maxima[0].watts if history.maxima else None,
                    history.timestamp if history.points else "",
                    history.raw_value if history.points else None,
                    history.raw_unit,
                )
                for channel, history in sorted(self.channels.items())
            )
        return PowerWindowSnapshot(context_id, self.origin, end, self.revision, self._cache, self.frame)
