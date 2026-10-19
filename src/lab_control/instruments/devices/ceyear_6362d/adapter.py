"""Ceyear 6362D controls and complete TRA wavelength/power frames."""

from __future__ import annotations

import hashlib
import math
import re
import time
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

from . import authentication, protocol

WAVELENGTHS = ("start_wavelength", "stop_wavelength", "center_wavelength", "wavelength_span", "rbw")
FRAME_SETTINGS = (
    "axis_mode",
    "amplitude_unit",
    *WAVELENGTHS,
    "sensitivity",
    "sweep_points",
    "points_auto",
    "sweep_mode",
    "trace_mode",
    "transfer_format",
    "wavelength_medium",
    "wavelength_offset",
    "reference_offset",
    "marker_scan_range",
)


def agrees(name: str, expected: Any, actual: Any) -> bool:
    if isinstance(expected, (bool, str)):
        return type(expected) is type(actual) and expected == actual
    # Strict reply round-off only; no unverified device quantization allowance.
    return isinstance(actual, (float, int)) and math.isclose(
        expected, actual, rel_tol=0, abs_tol=1e-6 if name in WAVELENGTHS else 1e-12
    )


class Ceyear6362DAdapter(ScpiAdapter):
    acquisition_revision = "ceyear-6362d-b11-v1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_format: str | None = None
        self.format_attempts: list[str] = []
        self.acquisition_preparation: dict[str, Any] = {}

    def identify(self):
        authentication.prepare_identity(self.transport, self.config)
        return super().identify()

    def _version(self) -> None:
        if self.identity is None:
            raise DeviceError("Identify 6362D before control or acquisition", "identity_required")
        if not self.identity.key:
            raise DeviceError("6362D requires a trustworthy serial number", "identity_required")
        match = re.fullmatch(r"(?:V(?:ersion)?\s*)?(\d+)\.(\d+)\.(\d+)", self.identity.firmware, re.I)
        if not match or tuple(map(int, match.groups())) < (2, 1, 0):
            raise Unsupported("6362D B.11 commands require confirmed software >=2.1.0")

    def read(self, operation: str, channel: int = 1) -> Any:
        self._version()
        if operation in WAVELENGTHS and super().read("axis_mode", channel) != "WAVELENGTH":
            raise Unsupported("6362D v1 wavelength parameters require wavelength axis mode")
        return super().read(operation, channel)

    def _snapshot(self) -> dict[str, Any]:
        result = {}
        for op in FRAME_SETTINGS:
            result[op] = self.read(op)
        if result["axis_mode"] != "WAVELENGTH" or result["amplitude_unit"] != "dBm":
            raise Unsupported("6362D v1 acquisition requires wavelength / dBm; settings are not changed")
        if result["wavelength_span"] <= 0:
            raise Unsupported("6362D zero-span/time acquisition is not qualified")
        return result

    def prepare_acquisition(self) -> None:
        self.acquisition_preparation = {"status": "blocked", "writes": []}
        self._version()
        assert self.identity is not None
        if self.identity.firmware not in protocol.BINARY_QUALIFICATIONS:
            raise Unsupported(
                "6362D binary transfer requires an archived qualification for this software version"
            )
        self.initial_format = self.read("transfer_format")
        self.acquisition_preparation["before"] = self.initial_format
        if self.initial_format != "REAL,64":
            self.acquisition_preparation["writes"].append(":FORMAT:DATA REAL,64")
            self.set_transfer_format("REAL,64")
        self.acquisition_preparation.update(status="verified", after="REAL,64")

    def set_transfer_format(self, fmt: str) -> None:
        """Model-owned initialization/acceptance operation, never called by identify."""
        self._version()
        if fmt not in {"ASCII", "REAL,32", "REAL,64"}:
            raise ValidationError("Unsupported 6362D data format")
        self.format_attempts.append(fmt)
        self.transport.write(":FORMAT:DATA " + fmt)
        if self.read("transfer_format") != fmt:
            raise DeviceError("6362D format readback differs; write not repeated", "readback_mismatch")

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        self._version()
        op = command.operation
        value: Any = parameters.get("value")
        mode = self.read("sweep_mode")
        if mode == "AUTO":
            raise Unsupported(
                "6362D AUTO cannot be stopped reliably with ABORT; change mode on the panel first"
            )
        if op in WAVELENGTHS:
            if self.read("axis_mode") != "WAVELENGTH" or self.read("marker_scan_range"):
                raise Unsupported(
                    "Wavelength controls require ordinary wavelength sweep without marker limits"
                )
            start, stop = self.read("start_wavelength"), self.read("stop_wavelength")
            if op == "start_wavelength" and value >= stop or op == "stop_wavelength" and value <= start:
                raise ValidationError("Start wavelength must be below stop wavelength")
            if op in {"center_wavelength", "wavelength_span"}:
                center = value if op == "center_wavelength" else self.read("center_wavelength")
                span = value if op == "wavelength_span" else self.read("wavelength_span")
                if center - span / 2 < 600 or center + span / 2 > 1700:
                    raise ValidationError("Center/span would exceed 600..1700 nm")
        if op == "sweep_points" and self.read("points_auto"):
            raise ValidationError("Disable automatic sample points before changing manual points")
        if op in {"single", "repeat"}:
            if self.read("trace_mode") != "WRITE":
                raise Unsupported("6362D v1 initiated acquisition requires TRA WRITE mode")
            if self.read("axis_mode") != "WAVELENGTH":
                raise Unsupported("6362D v1 initiated acquisition requires wavelength mode")

    def wait_sweep(self) -> None:
        deadline = time.monotonic() + self.config.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 0.1:
                raise DeviceError("6362D single sweep exceeded completion deadline", "timeout")
            query = getattr(self.transport, "query_timeout", None)
            raw = (
                query(":STATUS:OPERATION:CONDITION?", min(remaining, 2.0))
                if query
                else self.transport.query(":STATUS:OPERATION:CONDITION?")
            )
            state = protocol.mapped(raw, {0: 0, 1: 1})
            if state == 1:
                return
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def apply(self, command: DeviceCommand) -> ApplyResult:
        spec, parameters = self.validate_command(command)
        self.preflight(command, parameters)
        completed = 0
        try:
            for payload in self.encode(
                command.operation, spec, self._context(command.channel, parameters), parameters
            ):
                self.transport.write(payload)
                completed += 1
            if command.operation == "single":
                self.wait_sweep()
            actual = self.read(command.operation) if spec.query else None
            if command.operation in {"single", "repeat", "stop"}:
                mode = self.read("sweep_mode")
                expected = {"single": "SINGLE", "repeat": "REPEAT"}.get(command.operation)
                if expected and mode != expected:
                    raise DeviceError("6362D scan mode readback differs", "readback_mismatch")
                actual = {"mode": mode, "command_sent": True}
                # CONDITION=1 also occurs during REPEAT: never call it an idle flag.
                actual["running_state"] = "single_completed" if command.operation == "single" else "unknown"
            if "value" in parameters and not agrees(command.operation, parameters["value"], actual):
                return ApplyResult(
                    "rejected", command.operation, parameters, actual, completed, "6362D readback mismatch"
                )
            self.configuration[f"1.{command.operation}"] = {"value": actual}
            return ApplyResult("success", command.operation, parameters, actual, completed)
        except DeviceError as exc:
            return ApplyResult(
                "partial" if completed else "uncertain",
                command.operation,
                parameters,
                completed_steps=completed,
                error=f"{exc.code}: {exc}",
            )

    def acquisition_readings(self, batch: MeasurementBatch) -> tuple[ParameterReading, ...]:
        return tuple(
            ParameterReading(op, 1, value, "instrument", batch.settings_timestamps[op])
            for op, value in batch.configuration.items()
            if op in self.profile.commands
        )

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        self._version()
        if request.kind != "spectrum" or request.channels != (1,):
            raise ValidationError("6362D v1 supports spectrum from TRA (channel 1) only")
        assert self.identity is not None
        if request.initiate:
            result = self.apply(DeviceCommand("single", confirmed=request.confirmed))
            if result.outcome != "success":
                raise DeviceError(result.error, "acquisition_failed")
        before = self._snapshot()
        timestamp = utc_now()
        count = self.read("trace_points")
        if not 1 <= count <= 50001:
            raise DeviceError("TRA has no complete trace", "no_data")
        fmt = before["transfer_format"]
        if fmt == "REAL,32":
            raise Unsupported("6362D REAL32 acquisition is not hardware qualified; no automatic format change")
        order = protocol.BINARY_QUALIFICATIONS.get(self.identity.firmware)
        if fmt != "ASCII" and order is None:
            raise Unsupported("Binary byte order for this 6362D software has not been qualified")
        raw = []
        width = 8 if fmt == "REAL,64" else 4
        for axis in ("X", "Y"):
            query = f":TRACE:DATA:{axis}? TRA"
            # The 6362D B.11 endpoint has qualified fixed-size raw replies on
            # some commands and IEEE blocks on others.  The transport helper
            # consumes either framing using the known point count; it never
            # waits for an idle timeout to infer a frame boundary.
            flexible_query = getattr(self.transport, "query_binary_flexible", None)
            fixed_query = getattr(self.transport, "query_fixed", None)
            binary_query = getattr(self.transport, "query_binary", None)
            raw.append(
                flexible_query(query, count * width)
                if fmt != "ASCII" and flexible_query
                else fixed_query(query, count * width)
                if fmt != "ASCII" and fixed_query
                else binary_query(query, 50001 * width)
                if fmt != "ASCII" and binary_query
                else self.transport.query(query)
            )
        # Decode before configuration comparison so broken frames remain faults.
        x = protocol.wavelength_axis(raw[0], count, fmt, order)
        y = protocol.values(raw[1], count, fmt, order)
        after = self._snapshot()
        count_after = self.read("trace_points")
        changes = {k: (v, after[k]) for k, v in before.items() if v != after[k]}
        if count != count_after:
            changes["trace_points"] = (count, count_after)
        if changes:
            raise DeviceError(f"6362D configuration changed during frame: {changes}", "configuration_changed")
        measurement = Measurement(
            "spectrum",
            "dBm",
            y,
            1,
            x,
            "nm",
            metadata={
                "trace": "TRA",
                "trace_mode": before["trace_mode"],
                "new_scan_verified": bool(request.initiate),
                "configuration_scope": "current instrument settings; historical sweep settings are not embedded in trace replies",
                "points": count,
                "transfer_format": fmt,
                "byte_order": order,
                "x_sha256": hashlib.sha256(raw[0]).hexdigest(),
                "y_sha256": hashlib.sha256(raw[1]).hexdigest(),
                "requested_points": request.points,
                "full_trace": True,
            },
        )
        return MeasurementBatch(
            self.identity,
            (measurement,),
            before,
            validation="full",
            settings_timestamps={key: timestamp for key in before},
            performance={"configuration_source": "instrument", "configuration_query_mode": "separate"},
        )
