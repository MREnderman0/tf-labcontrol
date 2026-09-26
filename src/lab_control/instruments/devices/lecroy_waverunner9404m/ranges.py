"""MAUI CVAR limits: queried over the existing VISA session, never in a frame loop."""

from __future__ import annotations

import math
from dataclasses import replace

from lab_control.domain.instruments import DeviceError, Parameter
from lab_control.domain.oscilloscope import coarse_steps

from .protocol import numeric

POINTS_MIN = 1_000
POINTS_MAX = 10_000_000
# Software headroom for complete edge samples (real frames can exceed the
# nominal window by two points). This is not a claimed hardware limit.
EDGE_POINT_RESERVE = 32
HORIZONTAL_DIVISIONS = 10
MEMORY_QUERIES = {
    "mode": "VBS? 'return=app.Acquisition.Horizontal.SmartMemory'",
    "points": "VBS? 'return=app.Acquisition.Horizontal.NumPoints'",
    "sample_rate": "VBS? 'return=app.Acquisition.Horizontal.SamplingRate'",
}
# A frame may contain small descriptor/edge overhead beyond the memory setting.
MAX_WAVEFORM_BYTES = 2 * POINTS_MAX + 65536
MAX_FRAME_BYTES = 768 * 1024 * 1024  # decoded float64 time/value pairs, all channels
PATHS = {
    "timebase": "Horizontal.HorScale",
    "time_position": "Horizontal.HorOffset",
    "record_length": "Horizontal.MaxSamples",
    "scale": "C{ch}.VerScale",
    "offset": "C{ch}.VerOffset",
    "trigger_level": "Trigger.C{ch}.Level",
}
DEPENDENCIES = {
    "timebase",
    "time_position",
    "record_length",
    "scale",
    "offset",
    "coupling",
    "probe_attenuation",
    "channel_enabled",
}


def cvar_query(operation: str, channel: int, member: str) -> str:
    path = PATHS[operation].format(ch=channel)
    return f"VBS? 'return=app.Acquisition.{path}.{member}'"


def read_range(transport, operation: str, channel: int, spec: Parameter) -> Parameter:
    low, high, grain = (
        numeric(transport.query(cvar_query(operation, channel, member)))
        for member in ("GetMinValue", "GetMaxValue", "GetGrainValue")
    )
    if low >= high or grain < 0:
        raise DeviceError(f"Invalid CVAR range for {operation}: {low}, {high}, {grain}", "parse_error")
    # Retain the explicitly qualified software subset, rather than claiming it
    # is the instrument's physical limit.
    low = max(low, spec.minimum) if spec.minimum is not None else low
    high = min(high, spec.maximum) if spec.maximum is not None else high
    if low >= high:
        raise DeviceError(f"No qualified CVAR range for {operation}", "unsupported")
    return replace(
        spec, minimum=low, maximum=high, resolution=grain or None, range_source="instrument_cvar+software"
    )


def fixed_rate_timebase(spec: Parameter, sample_rate: float) -> Parameter:
    """Bound coarse timebase choices by the complete-frame software budget."""
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise DeviceError("Invalid fixed sample rate", "parse_error")
    budget = (POINTS_MAX - EDGE_POINT_RESERVE) / sample_rate / HORIZONTAL_DIVISIONS
    steps = coarse_steps(spec.minimum or 0, min(spec.maximum or budget, budget))
    if not steps:
        raise DeviceError("No qualified timebase within the current sampling budget", "unsupported")
    return replace(spec, maximum=steps[-1], range_source=spec.range_source + "+sampling_budget")


def plausible_adaptation(operation: str, requested: float, actual: float, spec: Parameter) -> bool:
    if spec.minimum is None or spec.maximum is None:
        return False
    if operation in {"timebase", "scale", "record_length"}:
        multipliers = (1, 2.5, 5) if operation == "record_length" else (1, 2, 5)
        candidates = [
            m * 10.0**e
            for e in range(-13, 12)
            for m in multipliers
            if spec.minimum <= m * 10.0**e <= spec.maximum
        ]
        nearest = min(candidates, key=lambda x: abs(x - requested))
        return math.isclose(actual, nearest, rel_tol=1e-6, abs_tol=1e-15)
    return bool(spec.resolution and abs(actual - requested) <= spec.resolution / 2 + 1e-15)
