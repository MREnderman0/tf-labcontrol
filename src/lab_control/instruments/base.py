from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    Capability,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    MeasurementBatch,
    Parameter,
    ParameterReading,
    Unsupported,
    ValidationError,
)
from lab_control.transport.base import Transport


def number(text: bytes | str) -> float:
    text = text.decode("ascii", errors="strict") if isinstance(text, bytes) else text
    try:
        value = float(text.strip())
    except ValueError as exc:
        raise DeviceError(f"Non-numeric response: {text[:120]!r}", "parse_error") from exc
    if not math.isfinite(value):
        raise DeviceError("Non-finite reading", "overload")
    return value


def text_value(raw: bytes) -> Any:
    text = raw.decode("ascii").strip().strip('"')
    try:
        return number(text)
    except DeviceError:
        return text


def identity_from_scpi(raw: bytes) -> DeviceIdentity:
    text = raw.decode("ascii", errors="strict").strip()
    parts = [part.strip() for part in text.split(",")]
    if len(parts) < 2 or not all(parts[:2]):
        raise DeviceError(f"Invalid identity response: {text[:120]}", "parse_error")
    return DeviceIdentity(
        parts[0], parts[1], parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "", raw=text
    )


@dataclass(frozen=True)
class CommandSpec:
    capability: Capability
    writes: tuple[str, ...] = ()
    query: str = ""
    select: tuple[str, ...] = ()
    decode: Callable[[bytes], Any] = text_value
    compare: bool = True


@dataclass(frozen=True)
class Profile:
    adapter_id: str
    manufacturer: tuple[str, ...]
    models: tuple[str, ...]
    category: str
    channels: tuple[int, ...]
    source: str
    revision: str
    commands: dict[str, CommandSpec]
    status: tuple[str, ...] = ()
    identity_query: str = "*IDN?"
    error_query: str = "SYST:ERR?"
    acquisition: str = ""
    channel_limits: dict[int, dict[str, float]] = field(default_factory=dict)


def parameter(unit: str = "", low: float | None = None, high: float | None = None) -> Parameter:
    return Parameter(unit=unit, minimum=low, maximum=high)


def enum(*choices: str) -> Parameter:
    return Parameter("enum", choices=tuple(choices))


def command(
    name: str,
    path: str,
    param: Parameter | None = None,
    *,
    read: bool = True,
    scoped: bool = True,
    confirm: bool = False,
    query: str | None = None,
    select: tuple[str, ...] = (),
    option: str = "",
) -> CommandSpec:
    return CommandSpec(
        Capability(
            name,
            {"value": param} if param else {},
            readable=read,
            writable=True,
            requires_confirmation=confirm,
            channel_scoped=scoped,
            section=path,
            option=option,
        ),
        (path + (" {value}" if param else ""),),
        query if query is not None else (path + "?" if read else ""),
        select,
    )


def read_command(name: str, query: str, unit: str = "", scoped: bool = True) -> CommandSpec:
    return CommandSpec(
        Capability(name, readable=True, channel_scoped=scoped, section=query), query=query, compare=False
    )


class ScpiAdapter:
    connection_commands: tuple[str, ...] = ()

    def __init__(self, transport: Transport, profile: Profile, config: ConnectionConfig) -> None:
        self.transport, self.profile, self.config = transport, profile, config
        self.adapter_id = profile.adapter_id
        self.identity: DeviceIdentity | None = None
        self.configuration: dict[str, Any] = {}
        self.dynamic_parameters: dict[str, Parameter] = {}
        self.detected_options: tuple[str, ...] = ()
        self.capability_details: dict[str, Any] = {}
        self.capability_error = ""

    def identify(self) -> DeviceIdentity:
        for payload in self.connection_commands:
            self.transport.write(payload)
        raw = self.transport.query(self.profile.identity_query)
        if self.profile.identity_query == "ID?":
            model = raw.decode("ascii").strip().strip('"')
            if re.sub(r"\s", "", model).upper() in {"HP3458A", "KEYSIGHT3458A", "AGILENT3458A"}:
                model = "3458A"
            identity = DeviceIdentity("Keysight", model, confidence="model_only", raw=model)
        else:
            identity = identity_from_scpi(raw)

        def normalize(value: str) -> str:
            return re.sub(r"[\s_-]", "", value).upper()

        if normalize(identity.model) not in {normalize(model) for model in self.profile.models}:
            raise DeviceError(
                f"Expected {self.profile.models}; received {identity.model}", "identity_conflict"
            )
        if not any(
            normalize(maker) in normalize(identity.manufacturer) for maker in self.profile.manufacturer
        ):
            raise DeviceError("Manufacturer does not match adapter", "identity_conflict")
        self.identity = identity
        return identity

    def capabilities(self) -> CapabilitySet:
        operations = []
        for spec in self.profile.commands.values():
            cap = spec.capability
            if cap.name in self.dynamic_parameters:
                cap = replace(cap, parameters={**cap.parameters, "value": self.dynamic_parameters[cap.name]})
                if self.capability_error:
                    cap = replace(
                        cap,
                        available=False,
                        reason="实机范围已过期：" + self.capability_error,
                        parameters={
                            **cap.parameters,
                            "value": replace(cap.parameters["value"], range_source="unavailable"),
                        },
                    )
            missing = bool(cap.option and cap.option not in (*self.config.options, *self.detected_options))
            operations.append(
                replace(
                    cap,
                    source=self.profile.source,
                    available=cap.available and not missing,
                    reason=f"missing option {cap.option}" if missing else cap.reason,
                )
            )
        return CapabilitySet(self.profile.category, self.profile.channels, tuple(operations))

    def capabilities_for_channel(self, channel: int) -> CapabilitySet:
        """Cached channel schema; model overrides must not perform I/O here."""
        self._context(channel, {})
        return self.capabilities()

    def _context(self, channel: int, parameters: dict[str, Any]) -> dict[str, Any]:
        if type(channel) is not int or channel not in self.profile.channels:
            raise ValidationError(f"Invalid channel {channel}")
        result: dict[str, Any] = {"ch": channel}
        for key, value in parameters.items():
            if isinstance(value, bool):
                result[key] = "ON" if value else "OFF"
            elif isinstance(value, float):
                result[key] = f"{value:.12g}"
            else:
                result[key] = value
        return result

    def read(self, operation: str, channel: int = 1) -> Any:
        cap = self.capabilities().get(operation)
        spec = self.profile.commands[operation]
        if not cap.readable or not spec.query:
            raise Unsupported(f"{operation} is not readable")
        context = self._context(channel, {})
        for selector in spec.select:
            self.transport.write(selector.format(**context))
        return spec.decode(self.transport.query(spec.query.format(**context)))

    def read_status(self) -> DeviceStatus:
        values = {}
        for operation in self.profile.status:
            spec = self.profile.commands[operation]
            channels = self.profile.channels if spec.capability.channel_scoped else (1,)
            for channel in channels:
                values[f"{channel}.{operation}"] = self.read(operation, channel)
        return DeviceStatus(values)

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        from lab_control.instruments.settings import cached_reading

        readings = []
        for name in operations:
            cap = self.capabilities().get(name)
            target = channel if cap.channel_scoped else 1
            if cap.readable:
                readings.append(ParameterReading(name, target, self.read(name, target), "instrument"))
            else:
                readings.append(cached_reading(name, target, self.configuration))
        return tuple(readings)

    def validate_command(self, command: DeviceCommand) -> tuple[CommandSpec, dict[str, Any]]:
        cap = self.capabilities().get(command.operation)
        if not cap.writable:
            raise Unsupported(f"{command.operation} is read-only")
        parameters = cap.validate(command.parameters)
        self._context(command.channel, parameters)
        if not cap.channel_scoped and command.channel != 1:
            raise ValidationError("This command applies to the whole instrument; use channel=1")
        if cap.requires_confirmation and not command.confirmed:
            raise DeviceError("Explicit confirmation required for this action", "confirmation_required")
        value = parameters.get("value")
        limits = dict(self.profile.channel_limits.get(command.channel, {}))
        for name, limit in self.config.limits.items():
            limits[name] = min(limits.get(name, float("inf")), limit)
        if (
            isinstance(value, (float, int))
            and command.operation in limits
            and value > limits[command.operation]
        ):
            raise ValidationError(
                f"{command.operation} exceeds configured/channel limit {limits[command.operation]}"
            )
        return self.profile.commands[command.operation], parameters

    def apply(self, command: DeviceCommand) -> ApplyResult:
        spec, parameters = self.validate_command(command)
        self.preflight(command, parameters)
        context = self._context(command.channel, parameters)
        completed = 0
        try:
            for selector in spec.select:
                self.transport.write(selector.format(**context))
            for payload in self.encode(command.operation, spec, context, parameters):
                self.transport.write(payload)
                completed += 1
            readback = spec.decode(self.transport.query(spec.query.format(**context))) if spec.query else None
            if self.profile.error_query:
                error = self.transport.query(self.profile.error_query).decode("ascii").strip()
                if not re.match(r"^[+]?0(?:\s|,|$)", error):
                    return ApplyResult("rejected", command.operation, parameters, readback, completed, error)
            expected = parameters.get("value")
            if spec.compare and expected is not None and readback is not None:
                if isinstance(expected, bool):
                    matches = str(readback).upper() in (
                        {"1", "1.0", "ON"} if expected else {"0", "0.0", "OFF"}
                    )
                elif isinstance(expected, (int, float)) and isinstance(readback, (int, float)):
                    matches = math.isclose(expected, readback, rel_tol=1e-5, abs_tol=1e-9)
                else:
                    expected_text, actual_text = str(expected).upper(), str(readback).upper()
                    parameter_spec = spec.capability.parameters.get("value")
                    matches = expected_text == actual_text or bool(
                        parameter_spec
                        and parameter_spec.kind == "enum"
                        and actual_text.startswith(expected_text)
                    )
                if not matches:
                    return ApplyResult(
                        "rejected",
                        command.operation,
                        parameters,
                        readback,
                        completed,
                        "Readback differs from request; device may quantize or reject this value",
                    )
            self.configuration[f"{command.channel}.{command.operation}"] = parameters
            return ApplyResult("success", command.operation, parameters, readback, completed)
        except DeviceError as exc:
            return ApplyResult(
                "partial" if completed else "uncertain",
                command.operation,
                parameters,
                completed_steps=completed,
                error=str(exc),
            )

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        """Read correlated limits on the same worker before issuing any setting writes."""
        opposite = {"start_frequency": "stop_frequency", "stop_frequency": "start_frequency"}.get(
            command.operation
        )
        if opposite in self.profile.commands:
            other = number(str(self.read(opposite, command.channel)))
            value = parameters["value"]
            if (command.operation == "start_frequency" and value > other) or (
                command.operation == "stop_frequency" and value < other
            ):
                raise ValidationError("Sweep start cannot exceed sweep stop; set endpoints in a valid order")

    def encode(
        self, operation: str, spec: CommandSpec, context: dict[str, Any], parameters: dict[str, Any]
    ) -> tuple[bytes | str, ...]:
        return tuple(item.format(**context) for item in spec.writes)

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        from lab_control.instruments.acquisition import acquire

        request.validate()
        if any(channel not in self.profile.channels for channel in request.channels):
            raise ValidationError("Invalid acquisition channel")
        if self.identity is None:
            raise DeviceError("Identify the instrument before acquisition", "identity_required")
        values = acquire(self, request)
        return MeasurementBatch(self.identity, values, dict(self.configuration))

    def close(self) -> None:
        self.transport.close()


class UnknownDeviceAdapter:
    adapter_id = "unknown"

    def __init__(self, identity: DeviceIdentity) -> None:
        self.identity = identity

    def identify(self) -> DeviceIdentity:
        return self.identity

    def capabilities(self) -> CapabilitySet:
        return CapabilitySet("unknown", (), ())

    def read_status(self) -> DeviceStatus:
        return DeviceStatus({"reason": "Identity metadata only; select a verified model adapter"})

    def apply(self, command: DeviceCommand) -> ApplyResult:
        raise Unsupported("Unknown devices cannot receive commands")

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        raise Unsupported("Unknown devices have no safe settings queries")

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        raise Unsupported("Unknown device acquisition is disabled")

    def close(self) -> None:
        pass
