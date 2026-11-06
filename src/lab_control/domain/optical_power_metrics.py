"""Optical power metrics over normalized, per-channel scalar frames."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated

import numpy as np
from pydantic import Field

from lab_control.domain.instruments import ValidationError
from lab_control.domain.metrics import (
    MetricDefinition,
    MetricInput,
    MetricParameters,
    MetricProfile,
    MetricValue,
    channel_metric_name,
)
from lab_control.domain.power import power_to_watts


class ReferencePowerParameters(MetricParameters):
    reference_power_w: dict[
        Annotated[str, Field(pattern=r"^[1-9][0-9]*$")],
        Annotated[float, Field(gt=0)],
    ] = Field(min_length=1, max_length=64)


@dataclass
class _PowerStats:
    minimum: float
    maximum: float
    mean: float
    count: int = 1


def _power_statistics(source: MetricInput) -> dict[int, _PowerStats]:
    statistics: dict[int, _PowerStats] = {}
    frame_count = 0
    for frame in source.frames():
        source.check_cancel()
        seen = set()
        for sample in frame["measurements"]:
            channel = sample["channel"]
            if type(channel) is not int or channel < 1 or channel in seen:
                raise ValidationError("光功率帧的通道无效或重复")
            seen.add(channel)
            values = np.asarray(sample["values"])
            if (
                sample["quantity"] != "power"
                or sample.get("axis") is not None
                or values.dtype.kind not in "fiu"
            ):
                raise ValidationError("光功率指标要求每通道为一个无横轴的功率标量")
            watts = power_to_watts(values, sample["unit"])
            if watts is None:
                raise ValidationError("光功率读数必须为有效的 W 或 dBm 标量")
            stats = statistics.get(channel)
            if stats is None:
                statistics[channel] = _PowerStats(watts, watts, watts)
            else:
                stats.minimum = min(stats.minimum, watts)
                stats.maximum = max(stats.maximum, watts)
                stats.count += 1
                stats.mean += (watts - stats.mean) / stats.count
        frame_count += 1
    source.progress(frame_count, frame_count)
    return statistics


def _percent(numerator: float, denominator: float) -> MetricValue:
    value = numerator / denominator * 100 if denominator > 0 else math.nan
    return MetricValue(value) if math.isfinite(value) else MetricValue(None, "unavailable")


def process_optical_power_fluctuation(
    source: MetricInput, _parameters: MetricParameters
) -> dict[str, MetricValue]:
    return {
        channel_metric_name(channel, "fluctuation_percent"): (
            _percent(stats.maximum - stats.minimum, stats.mean)
            if stats.count >= 2
            else MetricValue(None, "unavailable")
        )
        for channel, stats in _power_statistics(source).items()
    }


def process_optical_power_reference_deviation(
    source: MetricInput, parameters: ReferencePowerParameters
) -> dict[str, MetricValue]:
    statistics = _power_statistics(source)
    references = parameters.reference_power_w
    if set(references) != {str(channel) for channel in statistics}:
        raise ValidationError("参考功率必须与记录中的通道一一对应")
    result = {}
    for channel, stats in statistics.items():
        reference = references[str(channel)]
        result[channel_metric_name(channel, "min_deviation_percent")] = _percent(
            stats.minimum - reference, reference
        )
        result[channel_metric_name(channel, "max_deviation_percent")] = _percent(
            stats.maximum - reference, reference
        )
    return result


OPTICAL_POWER_FLUCTUATION_PROFILE = MetricProfile(
    id="optical_power.fluctuation",
    version="1",
    description="光功率自身波动",
    parameters=MetricParameters,
    metrics=(MetricDefinition("fluctuation_percent", "%", "功率峰峰差相对平均功率的百分比"),),
    process=process_optical_power_fluctuation,
    per_channel_metrics=True,
    supports_multiple_recordings=True,
)


OPTICAL_POWER_REFERENCE_DEVIATION_PROFILE = MetricProfile(
    id="optical_power.reference_deviation",
    version="1",
    description="光功率相对参考功率的偏差范围",
    parameters=ReferencePowerParameters,
    metrics=(
        MetricDefinition("min_deviation_percent", "%", "最小有符号相对偏差"),
        MetricDefinition("max_deviation_percent", "%", "最大有符号相对偏差"),
    ),
    process=process_optical_power_reference_deviation,
    per_channel_metrics=True,
    supports_multiple_recordings=True,
)
