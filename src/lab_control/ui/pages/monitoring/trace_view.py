"""Fit complete traces before display decimation; preserve a stable user viewport."""

import json

import numpy as np

from lab_control.domain.instruments import jsonable


class TraceView:
    def __init__(self, plot) -> None:
        self.plot = plot
        self.reset()

    def reset(self) -> None:
        self.active = False
        self.configuration_key: tuple | None = None
        self.axis_key: tuple | None = None
        self.points: list[tuple[np.ndarray, np.ndarray]] = []

    @staticmethod
    def bounds(points) -> tuple[float, float, float, float] | None:
        bounds = []
        for x, y in points:
            valid = np.isfinite(x) & np.isfinite(y)
            if valid.any():
                bounds.append(
                    (
                        float(x[valid].min()),
                        float(x[valid].max()),
                        float(y[valid].min()),
                        float(y[valid].max()),
                    )
                )
        if not bounds:
            return None
        return (
            min(b[0] for b in bounds),
            max(b[1] for b in bounds),
            min(b[2] for b in bounds),
            max(b[3] for b in bounds),
        )

    @staticmethod
    def padded(low: float, high: float, fraction: float) -> tuple[float, float]:
        span = high - low
        if span == 0:
            span = max(abs(low) * 0.02, 1e-12)
            low, high = low - span / 2, high + span / 2
        return low - span * fraction, high + span * fraction

    def update(self, frame, source_key, channels, points, *, waveform: bool = False) -> None:
        if not points:
            return
        self.active = True
        self.points = points
        configuration = {
            k: v for k, v in frame.batch.configuration.items() if not k.startswith(("_", "monitor_"))
        }
        units = tuple((m.channel, m.unit, m.axis_unit) for m in frame.batch.measurements)
        # Waveform trigger origins may jitter each sweep. Actual horizontal
        # changes are represented by the validated configuration, not each x[0].
        x_bounds = (
            None
            if waveform
            else (min(float(x.min()) for x, _ in points), max(float(x.max()) for x, _ in points))
        )
        axis_key = (source_key, channels, units, x_bounds)
        configuration_key = (
            frame.batch.identity.key,
            frame.session_id,
            frame.configuration_version,
            axis_key,
            json.dumps(jsonable(configuration), sort_keys=True),
        )
        self.plot.disableAutoRange()
        changed = configuration_key != self.configuration_key
        fit_x = axis_key != self.axis_key or (waveform and changed)
        if not (fit_x or changed):
            return
        bounds = self.bounds(points)
        if bounds is None:
            return
        if fit_x:
            self.plot.setXRange(*self.padded(bounds[0], bounds[1], 0.02), padding=0)
            self.axis_key = axis_key
        if changed:
            self.plot.setYRange(*self.padded(bounds[2], bounds[3], 0.1), padding=0)
            self.configuration_key = configuration_key

    def fit(self) -> None:
        bounds = self.bounds(self.points)
        if bounds is not None:
            self.plot.disableAutoRange()
            self.plot.setXRange(*self.padded(bounds[0], bounds[1], 0.02), padding=0)
            self.plot.setYRange(*self.padded(bounds[2], bounds[3], 0.1), padding=0)
