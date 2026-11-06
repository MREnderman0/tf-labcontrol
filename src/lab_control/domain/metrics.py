"""Opt-in model contracts for finite capture plans and small offline results."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict

from lab_control.domain.instruments import AcquisitionRequest, ValidationError


class MetricParameters(BaseModel):
    """Model parameter classes inherit strict, closed input validation."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


@dataclass(frozen=True)
class CapturePlan:
    request: AcquisitionRequest
    mode: Literal["single", "count", "finite"] = "single"
    count: int | None = None
    interval: float = 0.5

    def validate(self) -> None:
        self.request.validate()
        if self.mode not in {"single", "count", "finite"}:
            raise ValidationError("Unknown capture plan mode")
        if not math.isfinite(self.interval) or not 0.05 <= self.interval <= 60:
            raise ValidationError("Capture interval must be 0.05..60 seconds")
        if self.mode == "count":
            if type(self.count) is not int or not 1 <= self.count <= 1_000_000:
                raise ValidationError("Count capture requires a finite positive frame count")
        elif self.count is not None:
            raise ValidationError("Only count capture accepts count")
        if self.mode == "finite" and self.request.duration is None:
            raise ValidationError("Finite model capture requires an explicit duration")


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    unit: str
    description: str


@dataclass(frozen=True)
class MetricValue:
    value: float | int | None
    quality: Literal["good", "simulated", "unavailable"] = "good"


@dataclass(frozen=True)
class MetricInput:
    """Reopenable raw frames, with no Runtime, Qt, file-writing or instrument API."""

    recording_id: str
    metadata: dict[str, Any]
    complete: bool
    frames: Callable[[], Iterator[dict[str, Any]]]
    check_cancel: Callable[[], None]
    progress: Callable[[int, int], None]


@dataclass(frozen=True)
class MetricProfile:
    id: str
    version: str
    description: str
    parameters: type[MetricParameters]
    metrics: tuple[MetricDefinition, ...]
    process: Callable[[MetricInput, Any], dict[str, MetricValue]]
    capture_parameters: type[MetricParameters] | None = None
    capture: Callable[[Any], CapturePlan] | None = None
    supports_partial: bool = False

    def __post_init__(self) -> None:
        names = [m.name for m in self.metrics]
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.id) or not self.version:
            raise ValueError("Metric profile requires a stable ID and version")
        if not names or len(names) > 128 or len(names) != len(set(names)):
            raise ValueError("Metric names must be nonempty and unique (maximum 128)")
        if any(not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name) for name in names):
            raise ValueError("Invalid metric name")
        if (self.capture is None) != (self.capture_parameters is None):
            raise ValueError("Capture planner and parameter model must be declared together")
