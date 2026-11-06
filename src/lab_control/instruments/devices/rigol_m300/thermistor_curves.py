"""Versioned, user-supplied sensor curves. R is always measured in ohms."""

from __future__ import annotations

import math
from bisect import bisect_right

from lab_control.domain.instruments import DeviceError

MF501 = "mf501-v1"
S02 = "4813-s02-v1"
CURVES = {
    MF501: {
        "version": 1,
        "source": "MF501+热敏电阻阻温及温阻计算公式.doc",
        "source_sha256": "320d2c94cddf93fb479acf7609f4807d370115512b937d66201e44c58746280d",
    },
    S02: {
        "version": 1,
        "source": "4813-S02+(1)(1).xlsx · Therm Data!B2,B8:F11,C19:C24",
        "source_sha256": "65d5f86672e91fd0742da68282e9e01d6b8062d75795f5a7b99815a8ffd5730e",
    },
}
BANDS = (
    (0.0, 0.0033536166, 0.000253772, 8.5433271e-7, -8.7912262e-8),
    (681.6, 0.0033530481, 0.0002542023, 1.1431163e-6, -6.9383563e-8),
    (3599.0, 0.003354017, 0.00025617244, 2.1400943e-6, -7.2405219e-8),
    (32770.0, 0.003357042, 0.00025214848, 3.3743283e-6, -6.4957311e-8),
)
UNITS = {"C": "°C", "F": "°F", "K": "K"}


def temperature(resistance: float, curve: str, unit: str = "C") -> float:
    """Reject invalid/out-of-document-range values; never extrapolate silently."""
    if curve not in CURVES or unit not in UNITS:
        raise DeviceError("M300 未配置有效的电阻换温曲线或单位", "not_configured")
    if not math.isfinite(resistance) or resistance <= 0 or resistance >= 9e37:
        raise DeviceError("M300 换温需要有效的正电阻值（ohm）", "overload")
    try:
        if curve == MF501:
            a, b, c = -6.01188, 4622.53337, -86421.72414
            value = 2 * c / (-b + math.sqrt(b * b - 4 * c * (a - math.log(resistance)))) - 273.15
            valid = -30 - 1e-9 <= value <= 60 + 1e-9
        else:
            band = bisect_right([row[0] for row in BANDS], resistance) - 1
            _, a, b, c, d = BANDS[band]
            x = math.log(resistance / 10000)
            inverse_kelvin = a + b * x + c * x * x + d * x * x * x
            value = 1 / inverse_kelvin - 273.15
            valid = inverse_kelvin > 0 and -50 < value < 150.001
        if not valid or not math.isfinite(value):
            raise ValueError("outside specified curve range")
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise DeviceError("M300 电阻换温结果超出文档有效范围", "overload") from exc
    return value if unit == "C" else value * 1.8 + 32 if unit == "F" else value + 273.15


def provenance(resistance: float, curve: str) -> dict:
    return {"resistance_ohm": resistance, "curve_id": curve, **CURVES[curve]}
