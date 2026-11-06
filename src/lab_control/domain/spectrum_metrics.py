"""Peak, signal-to-noise estimate and spur rejection for one RF sweep."""

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

_PEAK_EXCLUSION_HZ = 2_000_000


def process_spectrum_peak_and_snr(
    source: MetricInput, _parameters: MetricParameters
) -> dict[str, MetricValue]:
    frames = source.frames()
    frame = next(frames, None)
    if frame is None or len(frame["measurements"]) != 1 or next(frames, None) is not None:
        raise ValidationError("频谱峰值和信噪比每次接收一帧单条扫频轨迹")

    source.check_cancel()
    trace = frame["measurements"][0]
    frequencies = np.asarray(trace.get("axis"))
    powers = np.asarray(trace["values"])
    if (
        trace["quantity"] != "spectrum"
        or trace["unit"] != "dBm"
        or trace.get("axis_unit") != "Hz"
        or powers.ndim != 1
        or powers.size == 0
        or powers.shape != frequencies.shape
        or powers.dtype.kind not in "fiu"
        or frequencies.dtype.kind not in "fiu"
        or not np.isfinite(frequencies).all()
        or not np.isfinite(powers).all()
        or np.any(frequencies[1:] <= frequencies[:-1])
    ):
        raise ValidationError("频谱指标需要按递增 Hz 频率轴保存的有效 dBm 扫频轨迹")

    peak_index = int(np.argmax(powers))
    peak_frequency = float(frequencies[peak_index])
    peak_power = float(powers[peak_index])
    outside_peak = powers[np.abs(frequencies - peak_frequency) > _PEAK_EXCLUSION_HZ]
    snr = MetricValue(None, "unavailable")
    spur_rejection = MetricValue(None, "unavailable")
    if outside_peak.size:
        with np.errstate(over="ignore", invalid="ignore"):
            snr_db = peak_power - float(np.mean(outside_peak, dtype=np.float64))
        spur_rejection_db = peak_power - float(np.max(outside_peak))
        if math.isfinite(snr_db):
            snr = MetricValue(snr_db)
        if math.isfinite(spur_rejection_db):
            spur_rejection = MetricValue(spur_rejection_db)

    source.progress(1, 1)
    return {
        "peak_frequency": MetricValue(peak_frequency),
        "peak_power": MetricValue(peak_power),
        "snr": snr,
        "spur_rejection": spur_rejection,
    }


SPECTRUM_PEAK_AND_SNR_PROFILE = MetricProfile(
    id="spectrum.peak_and_snr",
    version="1",
    description="频谱主峰、信噪比和杂散抑制比",
    parameters=MetricParameters,
    metrics=(
        MetricDefinition("peak_frequency", "Hz", "整条扫频轨迹最高点的频率"),
        MetricDefinition("peak_power", "dBm", "整条扫频轨迹最高点的功率"),
        MetricDefinition("snr", "dB", "峰值减去距主峰超过 2 MHz 的 dBm 读数均值"),
        MetricDefinition("spur_rejection", "dB", "峰值减去距主峰超过 2 MHz 的最高 dBm 读数"),
    ),
    process=process_spectrum_peak_and_snr,
)
