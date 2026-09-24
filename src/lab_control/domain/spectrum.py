"""Spectrum drafts and software markers. No instrument or GUI dependencies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from lab_control.domain.instruments import CapabilitySet, DeviceCommand, ValidationError

FREQUENCY = ("start_frequency", "stop_frequency", "center_frequency", "span")
NUMERIC = (*FREQUENCY, "reference_level", "attenuation", "rbw", "vbw")
AUTOMATIC = {"attenuation": "attenuation_auto", "rbw": "rbw_auto", "vbw": "vbw_auto"}
SETTINGS = (*NUMERIC, *AUTOMATIC.values())


def frequency_draft(values: dict[str, Any], changed: str, low: float, high: float) -> dict[str, float]:
    """Clamp the edited pair as a whole, preserving a center/span draft at the band edges."""
    if changed in ("start_frequency", "stop_frequency"):
        start, stop = (float(values[k]) for k in FREQUENCY[:2])
        start, stop = max(low, min(high, start)), max(low, min(high, stop))
        if start > stop:
            if changed == "start_frequency":
                start = stop
            else:
                stop = start
    else:
        center = max(low, min(high, float(values["center_frequency"])))
        span = max(0.0, min(high - low, float(values["span"])))
        if changed == "center_frequency":
            center = max(low + span / 2, min(high - span / 2, center))
        else:
            span = min(span, 2 * min(center - low, high - center))
        start, stop = center - span / 2, center + span / 2
    return dict(
        start_frequency=start, stop_frequency=stop, center_frequency=(start + stop) / 2, span=stop - start
    )


def prepare_changes(
    current: dict[str, Any], target: dict[str, Any], caps: CapabilitySet
) -> list[DeviceCommand]:
    """Plan only changed writes, with legal intermediate frequency and attenuation states."""
    for name, value in target.items():
        caps.get(name).validate({"value": value})
    if target["start_frequency"] > target["stop_frequency"]:
        raise ValidationError("起始频率不能高于终止频率")
    changed = {k for k, v in target.items() if current.get(k) != v}
    order = ["start_frequency", "stop_frequency"]
    if target["start_frequency"] > current["stop_frequency"]:
        order.reverse()
    # Lower the level before reducing attenuation; raise it only after attenuation is settled.
    lowering = target.get("reference_level", 0) < current.get("reference_level", 0)
    if lowering:
        order.append("reference_level")
    order.extend(("attenuation_auto", "attenuation"))
    if not lowering:
        order.append("reference_level")
    order.extend(("rbw_auto", "rbw", "vbw_auto", "vbw"))
    return [
        DeviceCommand(name, {"value": target[name]})
        for name in order
        if name in changed and not (name in AUTOMATIC and target.get(AUTOMATIC[name], False))
    ]


@dataclass(frozen=True)
class SpectrumMarker:
    number: int
    frequency: float
    tracking: bool = False
    half_width: float = 1.0

    def __post_init__(self) -> None:
        if self.number < 1 or not math.isfinite(self.frequency) or self.frequency < 0:
            raise ValueError("标记频率必须是有限非负数")
        if not math.isfinite(self.half_width) or self.half_width <= 0:
            raise ValueError("搜索半宽必须为有限正数")


@dataclass(frozen=True)
class MarkerReading:
    status: str
    position: float | None = None
    sample_frequency: float | None = None
    amplitude: float | None = None


def locate_marker(marker: SpectrumMarker, x, y, axis_unit: str = "Hz") -> MarkerReading:
    if axis_unit != "Hz":
        return MarkerReading("时间轴 · 已暂停")
    x, y = np.asarray(x), np.asarray(y)
    if x.ndim != 1 or y.ndim != 1 or x.size != y.size or not x.size:
        return MarkerReading("无有效数据")
    finite = np.isfinite(x) & np.isfinite(y)
    if not finite.any():
        return MarkerReading("无有效数据")
    x, y = x[finite], y[finite]
    if marker.frequency < x.min() or marker.frequency > x.max():
        return MarkerReading("范围外")
    if marker.tracking:
        inside = np.abs(x - marker.frequency) <= marker.half_width
        if not inside.any():
            return MarkerReading("范围内无采样点")
        x, y = x[inside], y[inside]
        candidates = np.flatnonzero(y == y.max())
        index = int(candidates[np.argmin(np.abs(x[candidates] - marker.frequency))])
        position = float(x[index])
    else:
        index = int(np.argmin(np.abs(x - marker.frequency)))
        position = marker.frequency
    return MarkerReading("正常", position, float(x[index]), float(y[index]))
