"""Physical display coordinates, independent of instrument offset conventions."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Mapping

from lab_control.domain.instruments import DeviceError

COLORS = ("#e3a252", "#69c9d4", "#b5d979", "#cda8e8")
CONTROL_NAMES = (
    "channel_enabled",
    "scale",
    "offset",
    "timebase",
    "time_position",
    "trigger_source",
    "trigger_level",
    "trigger_slope",
    "trigger_mode",
)


@dataclass(frozen=True)
class ScopeDisplay:
    """Eight vertical and ten horizontal divisions. Values remain in physical units."""

    units_per_div: float
    reference_value: float
    seconds_per_div: float
    time_center: float
    unit: str = "V"
    version: int = 1
    vertical_offset_sign: int | None = None
    horizontal_position_sign: int | None = None

    def __post_init__(self) -> None:
        numbers = (self.units_per_div, self.reference_value, self.seconds_per_div, self.time_center)
        if (
            self.version != 1
            or self.vertical_offset_sign not in {None, -1, 1}
            or self.horizontal_position_sign not in {None, -1, 1}
            or not self.unit
            or not all(math.isfinite(v) for v in numbers)
            or self.units_per_div <= 0
            or self.seconds_per_div <= 0
            or not math.isfinite(self.time_center + 5 * self.seconds_per_div)
            or not math.isfinite(self.time_center - 5 * self.seconds_per_div)
            or self.time_center + 5 * self.seconds_per_div <= self.time_center - 5 * self.seconds_per_div
        ):
            raise ValueError("Invalid oscilloscope display coordinates")

    def metadata(self) -> dict:
        return asdict(self)

    @classmethod
    def parse(cls, value: Mapping) -> ScopeDisplay:
        try:
            return cls(**value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DeviceError("Invalid oscilloscope display metadata", "parse_error") from exc


def engineering(value: float, unit: str, *, custom_scale: bool = False) -> str:
    if custom_scale and not is_coarse(value):
        return "自定义"
    if value == 0:
        return f"0 {unit}"
    exponent = max(-12, min(12, 3 * math.floor(math.log10(abs(value)) / 3)))
    prefix = {-12: "p", -9: "n", -6: "μ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G", 12: "T"}[exponent]
    return f"{value / 10**exponent:.4g} {prefix}{unit}"


def is_coarse(value: float) -> bool:
    if value <= 0:
        return False
    mantissa = value / 10 ** math.floor(math.log10(value))
    return any(math.isclose(mantissa, n, rel_tol=1e-9) for n in (1, 2, 5, 10))


def coarse_steps(low: float, high: float) -> tuple[float, ...]:
    if not 0 < low <= high or not math.isfinite(high):
        return ()
    return tuple(
        n * 10.0**e
        for e in range(math.floor(math.log10(low)) - 1, math.ceil(math.log10(high)) + 1)
        for n in (1, 2, 5)
        if low <= n * 10.0**e <= high
    )
