from __future__ import annotations

import ctypes as ct
import math
from typing import Any

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
    Unsupported,
    ValidationError,
)
from lab_control.transport.sdk import VendorLibrary

SOURCE = "https://github.com/Thorlabs/Light_Analysis_Examples/blob/main/Python/Thorlabs%20PMxxx%20Power%20Meters/TLPMX_dll/TLPMX.py"
U32, I16, U16, F64, PTR = ct.c_uint32, ct.c_int16, ct.c_uint16, ct.c_double, ct.c_void_p


class PowerMeterAdapter:
    adapter_id = "thorlabs.pm100d"

    def __init__(self, config: ConnectionConfig, library: Any = None) -> None:
        self.config = config
        self.sdk = VendorLibrary(config.sdk_path, library)
        self.handle = U32()
        self.prefix = "TLPMX" if config.transport == "tlpmx" else "PM100D"
        self.opened = False
        self.configuration: dict[str, Any] = {}
        self.identity: DeviceIdentity | None = None
        try:
            self.call(
                "init",
                [ct.c_char_p, U16, U16, PTR],
                config.resource.encode("ascii"),
                1,
                0,
                ct.byref(self.handle),
                session=False,
            )
            self.opened = True
        except Exception:
            self.sdk.close()
            raise

    def call(
        self, name: str, types: list[Any], *args: Any, session: bool = True, channel: bool = False
    ) -> None:
        if session:
            types, args = [U32, *types], (self.handle, *args)
        if channel and self.prefix == "TLPMX":
            types, args = [*types, U16], (*args, 1)
        result = self.sdk.function(self.prefix + "_" + name, types)(*args)
        if result < 0:
            raise DeviceError(f"{self.prefix}_{name} returned {result:#x}", "sdk_error")

    def identify(self) -> DeviceIdentity:
        buffers = [ct.create_string_buffer(256) for _ in range(4)]
        self.call("identificationQuery", [PTR] * 4, *buffers)
        identity = DeviceIdentity(*(buffer.value.decode("ascii") for buffer in buffers))
        if identity.model.upper() != "PM100D" or "THORLABS" not in identity.manufacturer.upper():
            raise DeviceError("SDK device is not a Thorlabs PM100D", "identity_conflict")
        self.identity = identity
        return identity

    def sensor(self) -> dict[str, Any]:
        buffers = [ct.create_string_buffer(256) for _ in range(3)]
        flags = [I16() for _ in range(3)]
        self.call("getSensorInfo", [PTR] * 6, *buffers, *(ct.byref(flag) for flag in flags), channel=True)
        name, serial, message = (buffer.value.decode("ascii", errors="replace") for buffer in buffers)
        if not name:
            raise DeviceError("Power meter sensor is absent", "sensor_missing")
        return {
            "model": name,
            "serial": serial,
            "message": message,
            "type": flags[0].value,
            "subtype": flags[1].value,
            "flags": flags[2].value,
        }

    def _get_double(self, name: str, attribute: bool = False, attribute_code: int = 0) -> float:
        value = F64()
        self.call(
            name,
            [I16, PTR] if attribute else [PTR],
            *((attribute_code, ct.byref(value)) if attribute else (ct.byref(value),)),
            channel=True,
        )
        return value.value

    def capabilities(self) -> CapabilitySet:
        return self.capability_schema(self.config)

    @staticmethod
    def capability_schema(config: ConnectionConfig) -> CapabilitySet:
        operations = (
            Capability(
                "wavelength",
                {"value": Parameter(unit="nm", minimum=200, maximum=2500)},
                True,
                True,
                channel_scoped=True,
                source=SOURCE,
                section="setWavelength/getWavelength",
            ),
            Capability(
                "autorange",
                {"value": Parameter("boolean")},
                writable=True,
                source=SOURCE,
                section="setPowerAutoRange",
            ),
            Capability(
                "range",
                {"value": Parameter(unit="W", minimum=1e-12, maximum=10)},
                writable=True,
                source=SOURCE,
                section="setPowerRange",
            ),
            Capability(
                "averaging_time",
                {"value": Parameter(unit="s", minimum=0.001, maximum=10)},
                writable=True,
                source=SOURCE,
                section="setAvgTime",
            ),
            Capability(
                "unit",
                {"value": Parameter("enum", choices=("W", "DBM"))},
                writable=True,
                source=SOURCE,
                section="setPowerUnit",
            ),
            Capability(
                "zero", writable=True, requires_confirmation=True, source=SOURCE, section="startDarkAdjust"
            ),
            Capability("sensor", readable=True, source=SOURCE, section="getSensorInfo"),
            Capability("power", readable=True, source=SOURCE, section="measPower"),
        )
        return CapabilitySet("optical_power_meter", (1,), operations)

    def read_status(self) -> DeviceStatus:
        return DeviceStatus(
            {"sensor": self.sensor(), "wavelength_nm": self._get_double("getWavelength", True)}
        )

    def read_settings(self, operations: tuple[str, ...], channel: int = 1):
        from lab_control.domain.instruments import ParameterReading
        from lab_control.instruments.settings import cached_reading

        if channel != 1:
            raise ValidationError("PM100D has one sensor channel")
        result = []
        for name in operations:
            self.capabilities().get(name)
            if name in {"sensor", "wavelength", "power"}:
                value = (
                    self.sensor()
                    if name == "sensor"
                    else self._get_double("getWavelength", True)
                    if name == "wavelength"
                    else self.acquire(AcquisitionRequest()).measurements[0].values[0]
                )
                result.append(ParameterReading(name, channel, value, "instrument"))
            else:
                result.append(cached_reading(name, channel, self.configuration))
        return tuple(result)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        cap = self.capabilities().get(command.operation)
        values = cap.validate(command.parameters)
        if not cap.writable:
            raise Unsupported("Read-only capability")
        if command.channel != 1:
            raise ValidationError("PM100D has one sensor channel")
        if cap.requires_confirmation and not command.confirmed:
            raise DeviceError(
                "Zeroing requires a covered sensor and explicit confirmation", "confirmation_required"
            )
        self.sensor()
        if command.operation == "wavelength":
            minimum = self._get_double("getWavelength", True, 1)
            maximum = self._get_double("getWavelength", True, 2)
            if not minimum <= values["value"] <= maximum:
                raise ValidationError(f"Sensor wavelength range is {minimum}..{maximum} nm")
        mapping = {
            "wavelength": ("setWavelength", F64),
            "autorange": ("setPowerAutoRange", I16),
            "range": ("setPowerRange", F64),
            "averaging_time": ("setAvgTime", F64),
            "unit": ("setPowerUnit", I16),
        }
        try:
            if command.operation == "zero":
                self.call("startDarkAdjust", [], channel=True)
            else:
                name, dtype = mapping[command.operation]
                value = values["value"]
                if command.operation == "unit":
                    value = 0 if value == "W" else 1
                self.call(name, [dtype], value, channel=True)
            self.configuration[command.operation] = values
            return ApplyResult("success", command.operation, values, completed_steps=1)
        except DeviceError as exc:
            return ApplyResult("uncertain", command.operation, values, error=str(exc))

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind != "scalar" or request.channels != (1,):
            raise Unsupported("PM100D supports one scalar sensor channel")
        self.sensor()
        if self.identity is None:
            raise DeviceError("Identify device first", "identity_required")
        unit = I16()
        self.call("getPowerUnit", [PTR], ct.byref(unit), channel=True)
        value = self._get_double("measPower")
        if not math.isfinite(value) or abs(value) >= 1e37:
            raise DeviceError("Power sensor reading is overrange/non-finite", "overload")
        return MeasurementBatch(
            self.identity,
            (Measurement("power", "W" if unit.value == 0 else "dBm", [value]),),
            dict(self.configuration),
        )

    def close(self) -> None:
        try:
            if self.opened:
                self.call("close", [])
                self.opened = False
        finally:
            self.sdk.close()


def enumerate_tlpmx(config: ConnectionConfig) -> tuple[str, ...]:
    sdk = VendorLibrary(config.sdk_path)
    try:
        count = U32()
        result = sdk.function("TLPMX_findRsrc", [U32, PTR])(0, ct.byref(count))
        if result < 0 or count.value > 1000:
            raise DeviceError("TLPMX enumeration failed", "sdk_error")
        resources = []
        for index in range(count.value):
            name = ct.create_string_buffer(1024)
            result = sdk.function("TLPMX_getRsrcName", [U32, U32, PTR])(0, index, name)
            if result < 0:
                raise DeviceError("TLPMX resource lookup failed", "sdk_error")
            resources.append(name.value.decode("ascii"))
        return tuple(resources)
    finally:
        sdk.close()
