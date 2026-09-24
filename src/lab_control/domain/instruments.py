"""Qt-independent contracts used by hardware, simulation, CLI and the desktop."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import numpy as np


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class DeviceError(Exception):
    def __init__(self, message: str, code: str = "device_error") -> None:
        super().__init__(message)
        self.code = code


class ValidationError(DeviceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "invalid_parameter")


class Unsupported(DeviceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "unsupported")


class Quality(StrEnum):
    GOOD = "good"
    SIMULATED = "simulated"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    PARSE_ERROR = "parse_error"
    OVERLOAD = "overload"
    SENSOR_MISSING = "sensor_missing"
    STALE = "stale"


@dataclass(frozen=True)
class DeviceIdentity:
    manufacturer: str
    model: str
    serial: str = ""
    firmware: str = ""
    confidence: str = "exact"
    raw: str = ""

    @property
    def key(self) -> str | None:
        if not self.serial.strip() or self.serial.strip().upper() in {"0", "N/A", "UNKNOWN"}:
            return None
        return "|".join((self.manufacturer.upper().strip(), self.model.upper().strip(), self.serial.strip()))


@dataclass(frozen=True)
class Parameter:
    kind: str = "number"
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    required: bool = True
    range_source: str = "model"

    def validate(self, name: str, value: Any) -> Any:
        if self.kind in {"number", "integer"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValidationError(f"{name}: expected finite {self.kind} ({self.unit})")
            if self.kind == "integer" and int(value) != value:
                raise ValidationError(f"{name}: expected integer")
            if self.minimum is not None and value < self.minimum:
                raise ValidationError(f"{name}: below {self.minimum} {self.unit}")
            if self.maximum is not None and value > self.maximum:
                raise ValidationError(f"{name}: above {self.maximum} {self.unit}")
            if self.choices and value not in tuple(float(v) for v in self.choices):
                raise ValidationError(f"{name}: expected a supported discrete setting")
            return int(value) if self.kind == "integer" else float(value)
        if self.kind == "boolean":
            if type(value) is not bool:
                raise ValidationError(f"{name}: expected true or false")
            return value
        if self.kind == "enum":
            if not isinstance(value, str) or value.upper() not in self.choices:
                raise ValidationError(f"{name}: expected one of {self.choices}")
            return value.upper()
        if self.kind == "array":
            try:
                arr = np.asarray(value, dtype=float)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{name}: expected numeric array") from exc
            if arr.ndim != 1 or not arr.size or arr.size > 4_000_000 or not np.isfinite(arr).all():
                raise ValidationError(f"{name}: invalid or excessive array")
            if self.minimum is not None and np.any(arr < self.minimum):
                raise ValidationError(f"{name}: below {self.minimum}")
            if self.maximum is not None and np.any(arr > self.maximum):
                raise ValidationError(f"{name}: above {self.maximum}")
            return arr.tolist()
        if not isinstance(value, str) or not value or any(c in value for c in '\r\n;"'):
            raise ValidationError(f"{name}: invalid text")
        return value


@dataclass(frozen=True)
class Capability:
    name: str
    parameters: dict[str, Parameter] = field(default_factory=dict)
    readable: bool = False
    writable: bool = False
    requires_confirmation: bool = False
    channel_scoped: bool = False
    source: str = ""
    section: str = ""
    verification: str = "documented_pending_hardware"
    option: str = ""
    available: bool = True
    reason: str = ""

    def validate(self, parameters: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(parameters, dict):
            raise ValidationError("Parameters must be a JSON object/mapping")
        extra = parameters.keys() - self.parameters.keys()
        if extra:
            raise ValidationError(f"{self.name}: unexpected parameters {sorted(extra)}")
        result = {}
        for name, spec in self.parameters.items():
            if name not in parameters:
                if spec.required:
                    raise ValidationError(f"{self.name}: missing {name}")
            else:
                result[name] = spec.validate(name, parameters[name])
        return result


@dataclass(frozen=True)
class CapabilitySet:
    category: str
    channels: tuple[int, ...]
    operations: tuple[Capability, ...]

    def get(self, name: str) -> Capability:
        for item in self.operations:
            if item.name == name:
                if not item.available:
                    raise Unsupported(f"{name}: {item.reason or 'option not available'}")
                return item
        raise Unsupported(f"{self.category}: {name} is not implemented for this model")


@dataclass(frozen=True)
class CapabilitySnapshot:
    identity: DeviceIdentity
    channels: dict[int, CapabilitySet]
    options: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now)
    version: int = 1
    stale: bool = False
    error: str = ""


@dataclass(frozen=True)
class DeviceCommand:
    operation: str
    parameters: dict[str, Any] = field(default_factory=dict)
    channel: int = 1
    confirmed: bool = False


@dataclass(frozen=True)
class ApplyResult:
    outcome: str
    operation: str
    requested: dict[str, Any]
    readback: Any = None
    completed_steps: int = 0
    error: str = ""
    timestamp: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class DeviceStatus:
    values: dict[str, Any]
    timestamp: str = field(default_factory=utc_now)
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParameterReading:
    operation: str
    channel: int
    value: Any = None
    source: str = "unavailable"
    timestamp: str = field(default_factory=utc_now)
    quality: Quality = Quality.GOOD
    error: str = ""


@dataclass(frozen=True)
class AcquisitionRequest:
    kind: str = "scalar"
    channels: tuple[int, ...] = (1,)
    points: int = 1000
    quantity: str = ""
    initiate: bool = False
    confirmed: bool = False

    def validate(self) -> None:
        if not self.channels or any(type(c) is not int or c < 1 for c in self.channels):
            raise ValidationError("channels must be positive integers")
        if len(set(self.channels)) != len(self.channels):
            raise ValidationError("Repeated acquisition channels are not allowed")
        if type(self.points) is not int or not 1 <= self.points <= 4_000_000:
            raise ValidationError("points must be in [1, 4000000]")
        if self.initiate and not self.confirmed:
            raise DeviceError("Starting acquisition requires explicit confirmation", "confirmation_required")


@dataclass(frozen=True)
class Measurement:
    quantity: str
    unit: str
    values: Any
    channel: int = 1
    axis: Any = None
    axis_unit: str = ""
    quality: Quality = Quality.GOOD
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MeasurementBatch:
    identity: DeviceIdentity
    measurements: tuple[Measurement, ...]
    configuration: dict[str, Any]
    timestamp: str = field(default_factory=utc_now)
    time_basis: str = "host_utc_at_read; waveform axes from instrument"
    adapter_version: str = "0.2.0"


class InstrumentAdapter(Protocol):
    adapter_id: str

    def identify(self) -> DeviceIdentity: ...
    def capabilities(self) -> CapabilitySet: ...
    def read_status(self) -> DeviceStatus: ...
    def read_settings(
        self, operations: tuple[str, ...], channel: int = 1
    ) -> tuple[ParameterReading, ...]: ...
    def apply(self, command: DeviceCommand) -> ApplyResult: ...
    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch: ...
    def close(self) -> None: ...


def jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, np.generic):
        return jsonable(value.item())
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
