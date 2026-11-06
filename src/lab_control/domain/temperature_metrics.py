"""Temperature stability on normalized, single-channel scalar frames."""

from __future__ import annotations

import math

import numpy as np

from lab_control.domain.instruments import ValidationError
from lab_control.domain.metrics import (
    MetricDefinition,
    MetricInput,
    MetricParameters,
    MetricProfile,
    MetricValue,
)


def process_temperature_stability(
    source: MetricInput, _parameters: MetricParameters
) -> dict[str, MetricValue]:
    minimum: float | int = math.inf
    maximum: float | int = -math.inf
    channel: int | None = None
    count = 0

    for frame in source.frames():
        source.check_cancel()
        samples = frame["measurements"]
        if len(samples) != 1:
            raise ValidationError("温控稳定性要求每帧只有一个通道的读数")
        sample = samples[0]
        if channel is None:
            channel = sample["channel"]
        elif sample["channel"] != channel:
            raise ValidationError("温控稳定性要求所有读数来自同一通道")

        values = np.asarray(sample["values"])
        if sample.get("axis") is not None or values.size != 1 or values.dtype.kind not in "fiu":
            raise ValidationError("温控稳定性要求每帧为一个无横轴的实数标量")
        reading = int(values.item()) if values.dtype.kind in "iu" else float(values.item())
        if not math.isfinite(reading):
            raise ValidationError("温控稳定性读数必须为有限值")
        minimum = min(minimum, reading)
        maximum = max(maximum, reading)
        count += 1

    source.progress(count, count)
    peak_to_peak = maximum - minimum
    if count < 2 or not math.isfinite(peak_to_peak):
        return {"peak_to_peak": MetricValue(None, "unavailable")}
    return {"peak_to_peak": MetricValue(peak_to_peak)}


TEMPERATURE_STABILITY_PROFILE = MetricProfile(
    id="temperature.stability",
    version="1",
    description="单通道温控稳定性",
    parameters=MetricParameters,
    metrics=(MetricDefinition("peak_to_peak", "", "最大读数减最小读数"),),
    process=process_temperature_stability,
    supports_multiple_recordings=True,
)
