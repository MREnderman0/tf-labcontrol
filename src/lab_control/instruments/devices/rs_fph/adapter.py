"""FPH02 controls and passive, context-aware spectrum acquisition."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    DeviceCommand,
    DeviceError,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
    utc_now,
)
from lab_control.instruments.base import ScpiAdapter
from lab_control.transport.diagnostics import parsing

from .protocol import POINTS, RBW, UNITS, VBW, boolean, trace, trace_values

FRAME_SETTINGS = (
    "start_frequency",
    "stop_frequency",
    "center_frequency",
    "span",
    "sweep_time",
    "amplitude_unit",
    "transfer_format",
    "byte_order",
    "rbw",
    "vbw",
    "rbw_auto",
    "vbw_auto",
    "reference_level",
    "attenuation",
    "attenuation_auto",
    "sweep_time_auto",
    "detector",
    "detector_auto",
    "trace_mode",
    "trigger_source",
    "frequency_offset",
    "reference_offset",
    "measurement_mode",
    "preamplifier",
)
FREQUENCY = ("start_frequency", "stop_frequency", "center_frequency", "span", "marker_frequency")


def tolerance(operation: str, expected: Any) -> float:
    """Per-quantity acceptance tolerances; discrete settings compare exactly."""
    if operation in FREQUENCY:
        return 0.5  # One-Hz requested grid, half a step.
    if operation == "reference_level":
        return 0  # No unverified level quantization tolerance.
    if operation == "sweep_time":
        return 0.5e-6  # V1.40 SCPI seconds reply has six decimal places.
    return 0


def agrees(operation: str, expected: Any, actual: Any) -> bool:
    if isinstance(expected, (bool, str)) or expected is None:
        return type(expected) is type(actual) and expected == actual
    return isinstance(actual, (int, float)) and math.isclose(
        expected, actual, rel_tol=0, abs_tol=tolerance(operation, expected) + 1e-12
    )


class FphAdapter(ScpiAdapter):
    supports_live_acquisition = True
    supports_stream_acquisition = True
    acquisition_revision = "fph02-v140-acquisition-v1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._acquisition_context: dict[str, Any] | None = None
        self._context_timestamp = ""
        self._context_key: tuple[str, str] = ("", "")

    @property
    def maximum_frequency(self) -> float:
        # FPH02 specifications p.4: queried B3 extends 2 to 3 GHz; B3+B4 to 4 GHz.
        return (
            4e9
            if {"B3", "B4"} <= set(self.detected_options)
            else 3e9
            if "B3" in self.detected_options
            else 2e9
        )

    def identify(self):
        identity = super().identify()
        if not identity.serial.startswith("1321.1111K02/"):
            self.identity = None
            raise Unsupported("This FPH contract is qualified for FPH02 (1321.1111K02) only")
        return identity

    def invalidate_acquisition_context(self, reason: str) -> None:
        self._acquisition_context = None
        self._context_timestamp = ""
        self._context_key = ("", "")

    def read(self, operation: str, channel: int = 1) -> Any:
        self._context(channel, {})
        if operation == "sweep_points":
            # SWE:POIN? is broken in V1.40; fixed in V1.90 release notes.
            value: Any = POINTS
        else:
            if operation in {"marker_frequency", "marker_value"} and not self.read("marker_enabled"):
                # FPH marker coordinate queries can implicitly enable the marker.
                raise Unsupported("Disabled FPH marker coordinates cannot be read without changing state")
            value = super().read(operation, channel)
        self.capability_details[operation] = value
        return value

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        self._context(channel, {})
        readings = []
        for operation in operations:
            if operation in {"marker_frequency", "marker_value"} and not self.read("marker_enabled"):
                readings.append(ParameterReading(operation, channel, error="标记关闭，查询坐标会开启标记"))
            else:
                readings.extend(super().read_settings((operation,), channel))
        return tuple(readings)

    def refresh_capabilities(self) -> None:
        self.invalidate_acquisition_context("refresh")
        self.detected_options = tuple(v.strip() for v in self.read("options").split(",") if v.strip())
        for op in FREQUENCY:
            self.dynamic_parameters[op] = replace(
                self.profile.commands[op].capability.parameters["value"],
                maximum=self.maximum_frequency,
                range_source="instrument",
            )
        self.read("amplitude_unit")
        self.read("span")
        self.read("measurement_mode")

    def capabilities(self):
        caps = super().capabilities()
        unit = self.capability_details.get("amplitude_unit")
        if unit and unit != "DBM":
            caps = replace(
                caps,
                operations=tuple(
                    replace(
                        cap,
                        writable=False,
                        reason="参考电平写入仅验收 dBm；先选择 dBm 单位",
                        parameters={
                            "value": replace(
                                cap.parameters["value"],
                                unit=UNITS.get(unit, unit),
                                minimum=None,
                                maximum=None,
                            )
                        },
                    )
                    if cap.name == "reference_level"
                    else cap
                    for cap in caps.operations
                ),
            )
        if self.capability_details.get("span") == 0:
            caps = replace(
                caps,
                operations=tuple(
                    replace(
                        cap,
                        writable=False,
                        reason="零跨度下标记为时间坐标",
                        parameters={"value": replace(cap.parameters["value"], unit="s", maximum=1000)},
                    )
                    if cap.name == "marker_frequency"
                    else cap
                    for cap in caps.operations
                ),
            )
        return caps

    def prepare_acquisition(self) -> None:
        if self.identity is None:
            raise DeviceError("Identify before selecting transfer format", "identity_required")
        self.invalidate_acquisition_context("connection")
        before = {op: self.read(op) for op in ("transfer_format", "byte_order")}
        self.acquisition_preparation = {
            "before": before,
            "status": "pending",
            "format_write_attempted": False,
        }
        try:
            if before["transfer_format"] != "REAL":
                self.acquisition_preparation["format_write_attempted"] = True
                self.transport.write("FORM REAL,32")
                after = {op: self.read(op) for op in before}
                self.acquisition_preparation["after"] = after
                self._check_error()
                if after != {**before, "transfer_format": "REAL"}:
                    raise DeviceError("FPH REAL32 initialization readback differs", "readback_mismatch")
            self.acquisition_preparation["status"] = "ready"
        except Exception:
            self.acquisition_preparation["status"] = "failed"
            raise  # No ASCII fallback and no replay after a failed write/read.

    def _check_error(self) -> None:
        error = self.read("errors")
        if not re.match(r"^[+]?0(?:\s|,|$)", error):
            raise DeviceError(error, "instrument_error")

    def validate_command(self, command: DeviceCommand):
        spec, parameters = super().validate_command(command)
        value: Any = parameters.get("value")
        if command.operation in {"rbw", "vbw"} and value not in (RBW if command.operation == "rbw" else VBW):
            raise ValidationError("FPH bandwidth requires a qualified 1-3-10 value")
        if command.operation in FREQUENCY[:-1] and value % 1:
            raise ValidationError("FPH qualified frequency input uses a 1 Hz grid")
        if command.operation == "attenuation" and value % 5:
            raise ValidationError("FPH attenuation requires a 5 dB step")
        if command.operation == "span" and 0 < value < 10:
            raise ValidationError("FPH span must be zero or at least 10 Hz")
        return spec, parameters

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        op = command.operation
        value: Any = parameters.get("value")
        if self.read("measurement_mode") != "SAN":
            raise Unsupported("Select Spectrum Analyzer mode before using this contract")
        if op in FREQUENCY:
            if self.read("frequency_offset") != 0:
                raise Unsupported("Frequency writes with an offset are not qualified")
            super().preflight(command, parameters)
            if op in {"center_frequency", "span"}:
                center = value if op == "center_frequency" else self.read("center_frequency")
                span = value if op == "span" else self.read("span")
                if not span / 2 <= center <= self.maximum_frequency - span / 2:
                    raise ValidationError("FPH center/span combination exceeds the qualified range")
            if self.read("trigger_source") not in {"IMM", "EXT"}:
                raise Unsupported("Change to a qualified trigger source before changing the axis")
        if op in {"rbw", "vbw", "attenuation", "sweep_time", "detector"} and self.read(op + "_auto"):
            raise ValidationError(f"Disable {op}_auto before changing its manual setting")
        if op == "sweep_time_auto" and value and self.read("span") == 0:
            raise ValidationError("FPH zero span requires manual sweep time")
        if op in {"reference_level", "attenuation"}:
            if self.read("amplitude_unit") != "DBM" or self.read("reference_offset") != 0:
                raise Unsupported("Level writes require dBm and zero reference offset")
            if self.read("preamplifier"):
                raise Unsupported("Level writes with the preamplifier enabled are not qualified")
            if not self.read("attenuation_auto"):
                level = value if op == "reference_level" else self.read("reference_level")
                attenuation = value if op == "attenuation" else self.read("attenuation")
                if level - attenuation > -10:
                    raise ValidationError(
                        "Qualified manual envelope requires reference level - attenuation <= -10 dBm"
                    )
        if op in {"marker_frequency", "marker_peak"}:
            if not self.read("marker_enabled") or self.read("marker_frequency_mode") != "FREQ":
                raise ValidationError("Enable marker 1 in frequency display mode first")
            if self.read("span") == 0:
                raise Unsupported("Only frequency-domain marker controls are qualified")
            if any(boolean(self.transport.query(f"CALC:DELT{n}:STAT?")) for n in range(2, 7)):
                raise Unsupported("Active delta markers can be converted by marker controls")
            if op == "marker_frequency" and not self.read("start_frequency") <= value <= self.read(
                "stop_frequency"
            ):
                raise ValidationError("Marker frequency lies outside the current trace")
        if op == "single":
            if self.read("continuous") or self.read("trigger_source") != "IMM":
                raise ValidationError("Single acquisition requires single mode and immediate trigger")
            if self.read("sweep_time") * max(1, self.read("sweep_count")) >= self.config.timeout - 1:
                raise ValidationError("Sweep duration/count exceeds the configured completion timeout")

    def apply(self, command: DeviceCommand) -> ApplyResult:
        spec, parameters = self.validate_command(command)
        self.invalidate_acquisition_context("setting")
        self.preflight(command, parameters)
        completed = 0
        try:
            for payload in self.encode(
                command.operation, spec, self._context(command.channel, parameters), parameters
            ):
                self.transport.write(payload)
                completed += 1
            if command.operation == "single" and self.transport.query("*OPC?").strip() != b"1":
                raise DeviceError("Invalid FPH single-sweep completion", "parse_error")
            actual = self.read(command.operation) if spec.query else None
            self._check_error()
            expected = parameters.get("value")
            matches = expected is None or agrees(command.operation, expected, actual)
            if command.operation == "marker_frequency":
                matches = abs(expected - actual) <= self.read("span") / (POINTS - 1) / 2 + 0.5
            if not matches:
                return ApplyResult(
                    "rejected",
                    command.operation,
                    parameters,
                    actual,
                    completed,
                    "Readback differs at the qualified setting resolution",
                )
            self.configuration[f"{command.channel}.{command.operation}"] = {"value": actual}
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
        from lab_control.domain.spectrum import SETTINGS

        if batch.validation == "cached":
            return ()
        return tuple(
            ParameterReading(op, 1, batch.configuration[op], "instrument", batch.settings_timestamps[op])
            for op in SETTINGS
            if op in batch.configuration
        )

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        try:
            return self._acquire(request)
        except Exception:
            self.invalidate_acquisition_context("acquisition_error")
            raise

    def _acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.channels != (1,):
            raise ValidationError("FPH spectrum acquisition accepts only channel 1")
        if request.kind != "spectrum":
            raise Unsupported("Use spectrum acquisition for FPH")
        if self.identity is None:
            raise DeviceError("Identify before acquisition", "identity_required")
        context_key = (request.consistency, request.context_id)
        if context_key != self._context_key:
            self.invalidate_acquisition_context("new_run")
            self._context_key = context_key
        if request.initiate:
            for cmd in (
                DeviceCommand("continuous", {"value": False}, confirmed=True),
                DeviceCommand("single", confirmed=True),
            ):
                result = self.apply(cmd)
                if result.outcome != "success":
                    raise DeviceError(result.error, "acquisition_failed")
        cached = (
            request.consistency in {"live", "stream"}
            and not request.initiate
            and self._acquisition_context is not None
        )
        if cached:
            before = dict(self._acquisition_context or {})
        else:
            before = {op: self.read(op) for op in FRAME_SETTINGS}
            before.update(sweep_points=POINTS, maximum_frequency=self.maximum_frequency)
            before["binary_encoding"] = "SWAP" if self.identity.firmware == "V1.40" else before["byte_order"]
            before["binary_encoding_source"] = (
                "FPH02 V1.40 fixed little endian; frozen ASCII/REAL32 acceptance"
                if self.identity.firmware == "V1.40"
                else "FORM:BORD readback"
            )
            if before["measurement_mode"] != "SAN" or before["frequency_offset"] != 0:
                raise Unsupported("Only ordinary, unshifted spectrum traces are qualified")
        raw = self.transport.query("TRAC:DATA? TRACE1")
        if not cached:
            after = {op: self.read(op) for op in FRAME_SETTINGS}
            changes = {op: (before[op], after[op]) for op in FRAME_SETTINGS if before[op] != after[op]}
            if changes:
                trace_values(raw, before)  # Truncated/malformed replies remain real faults.
                raise DeviceError(
                    f"FPH configuration changed during trace read: {changes}", "configuration_changed"
                )
        with parsing():
            measurement = trace(raw, before)
        if not cached:
            self._acquisition_context = dict(before)
            self._context_timestamp = utc_now()
            self._context_key = context_key
        return MeasurementBatch(
            self.identity,
            (measurement,),
            before,
            validation="cached" if cached else "full",
            settings_timestamps={op: self._context_timestamp for op in before},
            performance={
                "logical_queries": 1 if cached else len(FRAME_SETTINGS) * 2 + 1,
                "configuration_query_mode": "cached" if cached else "separate",
                "configuration_source": "run_snapshot" if cached else "instrument",
                "acquisition_context_id": request.context_id,
            },
        )
