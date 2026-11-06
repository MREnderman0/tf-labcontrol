"""Opt-in model contracts for finite capture plans and small offline results."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Annotated, Any, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from lab_control.domain.instruments import AcquisitionRequest, ValidationError

MAX_EXPERIMENT_RECORDINGS = 4096
ExperimentRecordingIds = Annotated[
    list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]],
    Field(min_length=1, max_length=MAX_EXPERIMENT_RECORDINGS),
]


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


def channel_metric_name(channel: int, name: str) -> str:
    return f"ch{channel}.{name}"


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
    recording_ids: tuple[str, ...] = ()


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
    per_channel_metrics: bool = False
    supports_multiple_recordings: bool = False

    def result_metrics(self, channels: list[int]) -> tuple[MetricDefinition, ...]:
        if not self.per_channel_metrics:
            return self.metrics
        if not 1 <= len(channels) * len(self.metrics) <= 128:
            raise ValidationError("所选通道的结果指标数量必须在 1 至 128 之间")
        definitions = tuple(
            replace(metric, name=channel_metric_name(channel, metric.name))
            for channel in channels
            for metric in self.metrics
        )
        if any(len(metric.name) > 64 for metric in definitions):
            raise ValidationError("展开通道后的指标名称不能超过 64 个字符")
        return definitions

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
