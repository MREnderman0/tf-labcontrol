"""Deterministic simulation, isolated from all physical transports."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np

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
    Measurement,
    MeasurementBatch,
    Parameter,
    ParameterReading,
    Quality,
    Unsupported,
)

SIMULATED_CATEGORIES: dict[str, dict[str, tuple[str, float, float]]] = {
    "temperature_controller": {
        "setpoint": ("degC", -80, 200),
        "ramp": ("degC/min", 0.01, 20),
        "pid_p": ("", 0, 100),
    },
    "electronic_load": {"current": ("A", 0, 30), "voltage": ("V", 0, 60), "power": ("W", 0, 300)},
    "pump_driver": {"current": ("A", 0, 5), "temperature": ("degC", 10, 40)},
    "optical_spectrometer": {"start_wavelength": ("nm", 200, 2000), "stop_wavelength": ("nm", 200, 2000)},
    "optical_power_meter": {"wavelength": ("nm", 200, 2000)},
    "phase_noise_analyzer": {"carrier": ("Hz", 1, 200e6)},
}


class SimulatedInstrument:
    def __init__(self, config: ConnectionConfig) -> None:
        from lab_control.instruments.registry import PROFILES, SDK_ADAPTERS, describe

        self.config, self.adapter_id = config, config.adapter
        self.closed = False
        self.sample_index = 0
        self.values: dict[str, Any] = {}
        profile = PROFILES.get(config.adapter)
        if profile or config.adapter in SDK_ADAPTERS:
            maker, model = (
                (profile.manufacturer[0], profile.models[0])
                if profile
                else {
                    "thorlabs.pm100d": ("Thorlabs", "PM100D"),
                    "pico.6404e": ("Pico Technology", "6404E"),
                    "microchip.53100a": ("Microchip", "53100A"),
                }[config.adapter]
            )
            self.identity = DeviceIdentity(
                maker,
                model,
                config.expected_serial or config.resource,
                "SIMULATED",
                confidence="simulated",
            )
            schema = describe(config)
            self.caps = replace(
                schema, operations=tuple(replace(cap, verification="simulated") for cap in schema.operations)
            )
        else:
            category = config.adapter.removeprefix("sim.")
            if category not in SIMULATED_CATEGORIES:
                raise Unsupported("Unknown simulation profile")
            self.identity = DeviceIdentity("SIMULATED", category, config.resource, "SIMULATED", "simulated")
            self.caps = CapabilitySet(
                category,
                (1,),
                tuple(
                    Capability(
                        name,
                        {"value": Parameter(unit=unit, minimum=low, maximum=high)},
                        readable=True,
                        writable=True,
                        verification="simulated",
                        source="simulation only",
                    )
                    for name, (unit, low, high) in SIMULATED_CATEGORIES[category].items()
                ),
            )
        defaults = {
            "frequency": 1000,
            "amplitude": 1,
            "power": -20,
            "offset": 0,
            "voltage": 1,
            "current": 0.1,
            "timebase": 0.001,
            "scale": 0.5,
            "probe_attenuation": 1,
            "record_length": 2000,
            "center_frequency": 1e6,
            "span": 1e6,
            "start_frequency": 0,
            "stop_frequency": 2e6,
            "rbw": 1000,
            "vbw": 1000,
            "wavelength": 1550,
            "integration": 1,
            "setpoint": 25,
            "sample_interval": 1e-6,
            "averages": 4,
            "points": 1001,
            "start_wavelength": 1400,
            "stop_wavelength": 1600,
        }
        if self.adapter_id == "pico.6404e":
            defaults["timebase"] = 10  # Mock timebase index, not a hardware GetTimebase result.
        for cap in self.caps.operations:
            if not cap.writable or not cap.parameters:
                continue
            for channel in self.caps.channels if cap.channel_scoped else (1,):
                values = {}
                for name, param in cap.parameters.items():
                    value: Any
                    if param.kind == "boolean":
                        value = cap.name in {"channel_enabled", "autorange"} or name == "enabled"
                        if cap.name in {"output", "all_outputs"}:
                            value = False
                    elif param.kind == "enum":
                        preferred = {
                            "waveform": "SIN",
                            "function": "DCV",
                            "coupling": "DC",
                            "range": "1",
                            "direction": "RISING",
                            "s_parameter": "S21",
                        }.get(name if name != "value" else cap.name, "")
                        value = next(
                            (v for v in param.choices if preferred and v.startswith(preferred)),
                            param.choices[0],
                        )
                    elif param.kind == "array":
                        value = [0.0] * 32
                    elif param.kind in {"number", "integer"}:
                        value = defaults.get(cap.name if name == "value" else name, 0)
                        value = max(value, param.minimum) if param.minimum is not None else value
                        value = min(value, param.maximum) if param.maximum is not None else value
                    else:
                        value = "DEMO"
                    values[name] = value
                self.values[f"{channel}.{cap.name}"] = values

    def identify(self) -> DeviceIdentity:
        return self.identity

    def capabilities(self) -> CapabilitySet:
        return self.caps

    def read_status(self) -> DeviceStatus:
        return DeviceStatus({"simulated": True, **self.values})

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        result = []
        for name in operations:
            cap = self.caps.get(name)
            target = channel if cap.channel_scoped else 1
            value = self.values.get(f"{target}.{name}")
            if name.startswith("measured_"):
                value = self._value(target, name.removeprefix("measured_"), 0)
            result.append(ParameterReading(name, target, value, "simulated", quality=Quality.SIMULATED))
        return tuple(result)

    def _value(self, channel: int, name: str, default: Any = 0) -> Any:
        value = self.values.get(f"{channel}.{name}", self.values.get(f"1.{name}", default))
        return value.get("value", default) if isinstance(value, dict) else value

    def apply(self, command: DeviceCommand) -> ApplyResult:
        cap = self.caps.get(command.operation)
        if command.channel not in self.caps.channels or (not cap.channel_scoped and command.channel != 1):
            raise DeviceError("Invalid simulated channel", "invalid_parameter")
        values = cap.validate(command.parameters)
        if not cap.writable:
            raise Unsupported("Read-only operation")
        if cap.requires_confirmation and not command.confirmed:
            raise DeviceError("Confirmation required", "confirmation_required")
        self.values[f"{command.channel}.{command.operation}"] = values
        if command.operation == "all_outputs":
            for channel in self.caps.channels:
                key = f"{channel}.output"
                if key in self.values:
                    self.values[key] = dict(values)
        return ApplyResult("success", command.operation, values, values, 1)

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if self.closed:
            raise DeviceError("Mock device is disconnected", "disconnected")
        self.sample_index += 1
        result = []
        for channel in request.channels:
            x: Any = np.arange(request.points, dtype=float) * 1e-6
            values = np.sin(2 * np.pi * 1000 * x)
            unit, axis_unit, quantity = "V", "s", request.quantity or request.kind
            if request.kind in {"waveform", "streaming"} and self.caps.category == "oscilloscope":
                duration = self._value(1, "timebase", 0.001) * 10
                if self.adapter_id == "pico.6404e":
                    interval = (
                        self._value(1, "sample_interval", 1e-6)
                        if request.kind == "streaming"
                        else 2 ** min(int(self._value(1, "timebase", 10)), 20) * 1e-9
                    )
                    duration = max(1, request.points - 1) * interval
                x = np.linspace(0, duration, request.points)
                amplitude = self._value(channel, "scale", 0.5)
                offset = self._value(channel, "offset", 0)
                channel_setup = self.values.get(f"{channel}.channel", {})
                amplitude = float(channel_setup.get("range", amplitude))
                offset = channel_setup.get("offset", offset)
                values = amplitude * np.sin(2 * np.pi * 1000 * x + channel * 0.2) - offset
            elif request.kind == "scalar" and self.caps.category == "power_supply":
                for name, unit in (("voltage", "V"), ("current", "A")):
                    enabled = self._value(channel, "output", self._value(1, "all_outputs", False))
                    value = self._value(channel, name, 1) if enabled else 0
                    result.append(Measurement(name, unit, [value], channel, quality=Quality.SIMULATED))
                continue
            elif request.kind == "scalar" and self.caps.category != "phase_noise_analyzer":
                unit, quantity, value = {
                    "optical_power_meter": (
                        "W",
                        "power",
                        0.001 * (1550 / self._value(1, "wavelength", 1550)),
                    ),
                    "multimeter": ("V", "reading", 1.25),
                    "temperature_controller": ("degC", "temperature", self._value(1, "setpoint", 25)),
                    "electronic_load": ("A", "current", self._value(1, "current", 0.1)),
                    "pump_driver": ("A", "current", self._value(1, "current", 0.1)),
                }.get(self.caps.category, ("V", "reading", 1.25))
                if self.caps.category == "optical_power_meter" and self._value(1, "unit", "W") == "DBM":
                    value, unit = 10 * np.log10(value / 0.001), "dBm"
                if self.caps.category == "multimeter":
                    unit, value = {
                        "OHM": ("ohm", 1000),
                        "OHMF": ("ohm", 1000),
                        "DCI": ("A", 0.01),
                        "ACI": ("A", 0.01),
                        "FREQ": ("Hz", 1000),
                        "PER": ("s", 0.001),
                    }.get(self._value(1, "function", "DCV"), ("V", 1.25))
                values, x, axis_unit = (
                    np.array([value + abs(value) * 0.002 * np.sin(self.sample_index / 8)]),
                    None,
                    "",
                )
            elif request.kind == "s_parameters":
                x = np.linspace(
                    self._value(1, "start_frequency", 1e6),
                    self._value(1, "stop_frequency", 10e6),
                    request.points,
                )
                values = 0.7 * np.exp(1j * np.linspace(0, 4 * np.pi, request.points))
                unit, axis_unit = "ratio", "Hz"
            elif request.kind == "spectrum" and self.caps.category != "optical_spectrometer":
                center, span = self._value(1, "center_frequency", 1e6), self._value(1, "span", 1e6)
                x = np.linspace(center - span / 2, center + span / 2, request.points)
                values = -95 + 65 * np.exp(-(((x - center) / (max(span, 1) * 0.05)) ** 2))
                unit, axis_unit = "dBm", "Hz"
                if span == 0:
                    x = np.linspace(0, self._value(1, "sweep_time", 0.1), request.points)
                    values = -35 + 2 * np.sin(2 * np.pi * 30 * x)
                    axis_unit = "s"
            elif self.caps.category == "phase_noise_analyzer":
                x = np.geomspace(1, 1e6, request.points)
                values = -75 - 10 * np.log10(x)
                unit, axis_unit = "dBc/Hz", "Hz"
                if request.quantity == "stability":
                    unit, axis_unit, values = "", "s", 1e-11 / np.sqrt(x)
            elif self.caps.category == "optical_spectrometer":
                x = np.linspace(
                    self._value(1, "start_wavelength", 1400),
                    self._value(1, "stop_wavelength", 1600),
                    request.points,
                )
                values = np.exp(-(((x - 1550) / 8) ** 2))
                unit, axis_unit = "a.u.", "nm"
            result.append(Measurement(quantity, unit, values, channel, x, axis_unit, Quality.SIMULATED))
        return MeasurementBatch(
            self.identity,
            tuple(result),
            dict(self.values),
        )

    def close(self) -> None:
        self.closed = True
