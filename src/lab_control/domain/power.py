"""Pure scalar optical-power conversions; acquisition values remain untouched."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def power_to_watts(value: Any, unit: str) -> float | None:
    try:
        values = np.asarray(value).reshape(-1)
        if values.size != 1:
            return None
        raw = float(values[0])
        if not math.isfinite(raw):
            return None
        normalized = str(unit).strip().lower()
        if normalized == "w":
            watts = raw
        elif normalized == "dbm":
            watts = 10 ** ((raw - 30.0) / 10.0)
            if watts == 0:  # Underflow is not a measured zero.
                return None
        else:
            return None
        return watts if math.isfinite(watts) and watts >= 0 else None
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def watts_to_dbm(watts: float) -> float | None:
    return 30.0 + 10.0 * math.log10(watts) if math.isfinite(watts) and watts > 0 else None


def power_span(minimum: float | None, maximum: float | None, unit: str) -> float | None:
    if minimum is None or maximum is None:
        return None
    if unit == "W":
        return maximum - minimum
    low, high = watts_to_dbm(minimum), watts_to_dbm(maximum)
    return high - low if low is not None and high is not None else None
