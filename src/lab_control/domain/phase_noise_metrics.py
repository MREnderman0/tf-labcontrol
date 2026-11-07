"""Read an interpolated offset from a saved phase-noise trace."""

from __future__ import annotations

import math

import numpy as np
from pydantic import Field

from lab_control.domain.instruments import ValidationError
from lab_control.domain.metrics import (
    MetricDefinition,
    MetricInput,
    MetricParameters,
    MetricProfile,
    MetricValue,
)


class PhaseNoiseOffsetParameters(MetricParameters):
    offset_hz: float = Field(gt=0)


def process_phase_noise_at_offset(
    source: MetricInput, parameters: PhaseNoiseOffsetParameters
) -> dict[str, MetricValue]:
    frames = source.frames()
    frame = next(frames, None)
    if frame is None or not source.complete:
        raise ValidationError("相位噪声偏频取值需要完整记录")
    if source.metadata.get("kind") == "continuous" and "final" in frame.get("configuration", {}):
        for following in frames:
            source.check_cancel()
            if frame["configuration"].get("final") is True:
                raise ValidationError("相噪记录在结束帧后仍有数据")
            frame = following
        config = frame["configuration"]
        if config.get("final") is not True or config.get("complete") is not True:
            raise ValidationError("相噪记录缺少完整的结束帧")
    elif next(frames, None) is not None:
        raise ValidationError("单帧相噪记录包含多帧")
    source.check_cancel()
    traces = [sample for sample in frame["measurements"] if sample["quantity"] == "phase_noise"]
    if len(traces) != 1:
        raise ValidationError("记录必须包含一条相噪曲线")
    trace = traces[0]
    offsets = np.asarray(trace.get("axis"))
    levels = np.asarray(trace["values"])
    if (
        trace["unit"] != "dBc/Hz"
        or trace.get("axis_unit") != "Hz"
        or levels.ndim != 1
        or levels.size == 0
        or levels.shape != offsets.shape
        or levels.dtype.kind not in "fiu"
        or offsets.dtype.kind not in "fiu"
        or not np.isfinite(offsets).all()
        or not np.isfinite(levels).all()
        or offsets[0] <= 0
        or np.any(offsets[1:] <= offsets[:-1])
    ):
        raise ValidationError("相噪曲线必须使用严格递增的正偏频 Hz 和有限的 dBc/Hz 值")
    value = float(np.interp(parameters.offset_hz, offsets, levels, left=math.nan, right=math.nan))
    source.progress(1, 1)
    return {"phase_noise": MetricValue(value) if math.isfinite(value) else MetricValue(None, "unavailable")}


PHASE_NOISE_AT_OFFSET_PROFILE = MetricProfile(
    id="phase_noise.at_offset",
    version="1",
    description="读取指定偏频的相位噪声",
    parameters=PhaseNoiseOffsetParameters,
    metrics=(MetricDefinition("phase_noise", "dBc/Hz", "指定偏频处的相噪值，按 Hz 轴线性插值"),),
    process=process_phase_noise_at_offset,
)
