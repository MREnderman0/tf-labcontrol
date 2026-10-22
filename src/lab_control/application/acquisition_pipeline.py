"""Compatibility exports for frame contracts and application cadence policy."""

from __future__ import annotations

from lab_control.domain.frames import (
    FrozenDict as FrozenDict,
)
from lab_control.domain.frames import (
    batch_bytes as batch_bytes,
)
from lab_control.domain.frames import (
    immutable_batch as immutable_batch,
)
from lab_control.domain.frames import (
    validate_displayed_batch as validate_displayed_batch,
)
from lab_control.domain.frames import (
    validate_recorded_batch as validate_recorded_batch,
)
from lab_control.domain.instruments import ValidationError


def validate_cadence(interval: float, cadence: str | None = None, *, minimum: float = 0.05) -> None:
    # Omitted cadence is the legacy API, whose shorter intervals remain supported.
    if cadence not in {None, "fastest", "interval"}:
        raise ValidationError("Unknown acquisition cadence")
    lower = minimum if cadence is None else 0.5
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not lower <= interval <= 60:
        raise ValidationError(f"Acquisition interval must be {lower}..60 s")
