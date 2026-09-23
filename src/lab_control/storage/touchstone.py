"""Touchstone 1.x RI export; never invent missing S parameters or calibration."""

from pathlib import Path
from typing import Mapping

import numpy as np

from lab_control.domain.instruments import Measurement, Quality, ValidationError


def export_touchstone(path: Path, traces: Mapping[str, Measurement], reference_ohm: float = 50) -> Path:
    """Export one-port S11, or a complete two-port set ordered S11,S21,S12,S22."""
    keys = ("S11",) if set(traces) == {"S11"} else ("S11", "S21", "S12", "S22")
    if set(traces) != set(keys):
        raise ValidationError("Touchstone requires S11 or all four S11/S21/S12/S22 traces")
    suffix = ".s1p" if len(keys) == 1 else ".s2p"
    if path.suffix.lower() != suffix or not np.isfinite(reference_ohm) or reference_ohm <= 0:
        raise ValidationError(f"Use {suffix} and a positive reference impedance")
    first = traces[keys[0]]
    axis = np.asarray(first.axis, dtype=float)
    if axis.ndim != 1 or not axis.size or not np.all(np.isfinite(axis)) or np.any(np.diff(axis) <= 0):
        raise ValidationError("A strictly increasing finite frequency axis is required")
    columns = [axis]
    for key in keys:
        trace = traces[key]
        values = np.asarray(trace.values, dtype=complex)
        if (
            trace.quality not in {Quality.GOOD, Quality.SIMULATED}
            or trace.axis_unit != "Hz"
            or trace.unit != "ratio"
        ):
            raise ValidationError("Only valid complex ratio data on a Hz axis can be exported")
        if (
            values.shape != axis.shape
            or not np.array_equal(axis, trace.axis)
            or not np.all(np.isfinite(values))
        ):
            raise ValidationError("All S parameter traces must share one complete frequency grid")
        columns.extend((values.real, values.imag))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("! TF LabControl export. Consult the accompanying configuration/calibration snapshot.\n")
        stream.write(f"# Hz S RI R {reference_ohm:.12g}\n")
        np.savetxt(stream, np.column_stack(columns), fmt="%.15g")
    return path
