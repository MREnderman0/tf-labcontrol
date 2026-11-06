"""Per-channel voltage statistics for one normalized oscilloscope frame."""

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
    channel_metric_name,
)


def process_voltage_waveform_statistics(
    source: MetricInput, _parameters: MetricParameters
) -> dict[str, MetricValue]:
    result: dict[str, MetricValue] = {}
    frames = source.frames()
    frame = next(frames, None)
    if frame is None or not frame["measurements"]:
        raise ValidationError("电压波形统计需要一帧非空波形")
    for sample in frame["measurements"]:
        source.check_cancel()
        channel = sample["channel"]
        mean_name = channel_metric_name(channel, "mean")
        if type(channel) is not int or channel < 1 or mean_name in result:
            raise ValidationError("电压波形帧的通道无效或重复")
        values = np.asarray(sample["values"])
        axis = np.asarray(sample.get("axis"))
        if (
            sample["quantity"] not in {"voltage", "waveform"}
            or sample["unit"] != "V"
            or sample.get("axis_unit") != "s"
            or values.ndim != 1
            or values.size == 0
            or values.shape != axis.shape
            or values.dtype.kind not in "fiu"
            or axis.dtype.kind not in "fiu"
            or not np.isfinite(axis).all()
        ):
            raise ValidationError("电压波形需要与秒轴等长的伏特样本数组")
        minimum, maximum = float(np.min(values)), float(np.max(values))
        if not (math.isfinite(minimum) and math.isfinite(maximum)):
            raise ValidationError("电压波形样本必须为有限值")
        with np.errstate(over="ignore", invalid="ignore"):
            mean = float(np.mean(values, dtype=np.float64))
        scale = max(abs(minimum), abs(maximum))
        rms = std = 0.0
        if scale:
            # Scale before squaring to keep finite amplitudes within the float range.
            normalized = values.astype(np.float64, copy=False) / scale
            rms = scale * math.sqrt(float(np.dot(normalized, normalized)) / values.size)
            std = scale * float(np.std(normalized, ddof=0))
        metrics = {
            "mean": mean,
            "peak_to_peak": maximum - minimum if values.size >= 2 else None,
            "rms": rms,
            "std": std,
        }
        for name, value in metrics.items():
            result[channel_metric_name(channel, name)] = (
                MetricValue(value)
                if value is not None and math.isfinite(value)
                else MetricValue(None, "unavailable")
            )
    if next(frames, None) is not None:
        raise ValidationError("电压波形统计每次接收一帧，分次提交各次采集")
    source.progress(1, 1)
    return result


VOLTAGE_WAVEFORM_STATISTICS_PROFILE = MetricProfile(
    id="voltage.waveform_statistics",
    version="2",
    description="逐通道整段电压波形统计",
    parameters=MetricParameters,
    metrics=(
        MetricDefinition("mean", "V", "整段波形样本的算术平均值"),
        MetricDefinition("peak_to_peak", "V", "整段波形最大值减最小值"),
        MetricDefinition("rms", "V", "整段波形的均方根，包含直流分量"),
        MetricDefinition("std", "V", "整段波形相对均值的总体标准差（ddof=0）"),
    ),
    process=process_voltage_waveform_statistics,
    per_channel_metrics=True,
)
