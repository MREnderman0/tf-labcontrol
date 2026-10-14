"""M300 adapter with dynamic slot inventory and passive scan acquisition."""

from __future__ import annotations

import math
import re
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    DeviceCommand,
    DeviceError,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
    utc_now,
)
from lab_control.instruments.base import ScpiAdapter


class M300Adapter(ScpiAdapter):
    supports_live_acquisition = True
    supports_stream_acquisition = False
    acquisition_revision = "m300-scalar-v1"

    def read(self, operation: str, channel: int = 1) -> Any:
        value = super().read(operation, channel)
        self.capability_details[operation] = value
        return value

    def read_status(self):
        values = {}
        for operation in self.profile.status:
            try:
                values[operation] = self.read(operation)
            except DeviceError as exc:
                values[operation] = None
                self.capability_details[f"{operation}_error"] = f"{exc.code}: {exc}"
        from lab_control.domain.instruments import DeviceStatus

        return DeviceStatus(values, errors=tuple(
            str(self.capability_details[key])
            for key in sorted(self.capability_details)
            if key.endswith("_error")
        ))

    def read_settings(self, operations: tuple[str, ...], channel: int = 1):
        from lab_control.instruments.settings import cached_reading

        readings = []
        for operation in operations:
            cap = self.capabilities().get(operation)
            if cap.readable:
                try:
                    readings.append(ParameterReading(operation, 1, self.read(operation), "instrument"))
                except DeviceError as exc:
                    readings.append(ParameterReading(operation, 1, error=f"{exc.code}: {exc}"))
            else:
                readings.append(cached_reading(operation, 1, self.configuration))
        return tuple(readings)

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        if command.operation in {"monitor_state", "dmm_state"} and not command.confirmed:
            raise DeviceError("Changing M300 acquisition state requires explicit confirmation", "confirmation_required")
        if command.operation == "dmm_state" and parameters.get("value") and not self.read("dmm_installed"):
            raise Unsupported("M300 DMM module is not installed")

    def apply(self, command: DeviceCommand) -> ApplyResult:
        spec, parameters = self.validate_command(command)
        self.preflight(command, parameters)
        context = self._context(command.channel, parameters)
        completed = 0
        try:
            for payload in self.encode(command.operation, spec, context, parameters):
                self.transport.write(payload)
                completed += 1
            readback = self.read(command.operation) if spec.query else None
            error = self.transport.query(self.profile.error_query).decode("ascii", "replace").strip()
            if not re.match(r"^[+]?0(?:\s|,|$)", error):
                return ApplyResult("rejected", command.operation, parameters, readback, completed, error)
            expected = parameters.get("value")
            if expected is not None and readback is not None:
                if isinstance(expected, bool):
                    matches = bool(readback) is expected
                elif isinstance(expected, (int, float)) and isinstance(readback, (int, float)):
                    matches = math.isclose(float(expected), float(readback), rel_tol=0, abs_tol=1e-6)
                else:
                    matches = str(expected).upper() == str(readback).upper()
                if not matches:
                    return ApplyResult("rejected", command.operation, parameters, readback, completed, "Readback differs from request")
            self.configuration[f"1.{command.operation}"] = parameters
            return ApplyResult("success", command.operation, parameters, readback, completed)
        except DeviceError as exc:
            return ApplyResult("partial" if completed else "uncertain", command.operation, parameters, completed_steps=completed, error=f"{exc.code}: {exc}")

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.channels != (1,):
            raise ValidationError("M300 acquisition uses the current scan list")
        if request.kind not in {"scalar", "measurement"}:
            raise Unsupported("M300 acquisition kind must be scalar")
        if self.identity is None:
            raise DeviceError("Identify before acquisition", "identity_required")
        scan_size = self.read("scan_size")
        if scan_size <= 0:
            raise DeviceError("M300 scan list is empty; no measurement was started", "not_configured")
        values = self.read("measurements")
        if len(values) != scan_size:
            raise DeviceError(f"M300 returned {len(values)} values for {scan_size} scan channels", "parse_error")
        return MeasurementBatch(
            self.identity,
            (Measurement("measurement", "instrument", values, metadata={"scan_size": scan_size}),),
            {"scan_list": self.read("scan_list"), "scan_size": scan_size},
            timestamp=utc_now(),
            validation="full",
            performance={"logical_queries": 3, "configuration_query_mode": "separate"},
        )
