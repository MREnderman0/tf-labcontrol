"""Independent DSA815 control and passive trace acquisition."""

from __future__ import annotations

import math
import re
import time
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
from lab_control.transport.scpi import scalar_query_message, scalar_response_fields

from .auto_setup import auto_setup
from .protocol import RBW, UNITS, VBW, trace, trace_values

FRAME_SETTINGS = (
    "start_frequency",
    "stop_frequency",
    "center_frequency",
    "span",
    "sweep_time",
    "sweep_points",
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
    "trace_mode",
    "trigger_source",
    "frequency_offset",
    "reference_offset",
    "measurement_mode",
)


LIVE_SETTINGS = (
    "start_frequency",
    "stop_frequency",
    "center_frequency",
    "span",
    "sweep_time",
    "amplitude_unit",
    "transfer_format",
    "byte_order",
    "frequency_offset",
    "reference_offset",
    "measurement_mode",
)


def tolerance(operation: str, expected: Any) -> float:
    # Frequency queries are integer Hz; discrete bandwidth/attenuator values are exact.
    # Decimal SCPI readbacks contain six digits after the decimal in scientific notation.
    if operation in {"rbw", "vbw", "attenuation", "marker_position"}:
        return 0
    if operation in {"start_frequency", "stop_frequency", "center_frequency", "span", "marker_frequency"}:
        return 0.5
    if operation == "reference_level":
        return 0.005
    if operation == "sweep_time":
        return max(1e-12, abs(float(expected)) * 0.0000005)
    return 0


def agrees(operation: str, expected: Any, actual: Any) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, bool) or isinstance(expected, str):
        return type(actual) is type(expected) and expected == actual
    return isinstance(actual, (int, float)) and math.isclose(
        expected, actual, rel_tol=0, abs_tol=tolerance(operation, expected) + 1e-12
    )


class Dsa815Adapter(ScpiAdapter):
    supports_live_acquisition = True
    supports_stream_acquisition = True
    acquisition_revision = "dsa815-acquisition-v6"
    _acquisition_context: dict[str, Any] | None = None
    _context_at = 0.0
    _context_timestamp = ""
    _stream_context_id = ""
    _live_context_id = ""

    def prepare_acquisition(self) -> None:
        """Verified connection default; discovery/read-only tools explicitly skip it."""
        if self.identity is None:
            raise DeviceError("Identify before acquisition setup", "identity_required")
        self.acquisition_preparation: dict[str, Any] = {
            "started_at": utc_now(),
            "status": "reading",
            "format_write_attempted": False,
        }
        info = self.acquisition_preparation
        try:
            before = {op: self.read(op) for op in ("transfer_format", "byte_order")}
            info["before"] = before
            self.invalidate_acquisition_context("connection_setup")
            if before["transfer_format"] != "REAL":
                info["format_write_attempted"] = True
                self.transport.write(":FORM:TRAC:DATA REAL,32")
                if self.transport.query("*OPC?").strip() != b"1":
                    raise DeviceError("Invalid operation-complete response", "parse_error")
                after = {op: self.read(op) for op in ("transfer_format", "byte_order")}
                info["after"] = after
                error = self.transport.query("SYST:ERR?").decode("ascii").strip()
                if not re.match(r"^[+]?0(?:\s|,|$)", error):
                    raise DeviceError(error, "configuration_failed")
                if after != {**before, "transfer_format": "REAL"}:
                    raise DeviceError("Binary format/byte order readback differs", "readback_mismatch")
            else:
                info["after"] = dict(before)
            info.update(status="ready", finished_at=utc_now())
        except Exception as exc:
            info.update(status="failed", error=str(exc), finished_at=utc_now())
            raise DeviceError(
                "DSA815 二进制传输初始化失败，未自动重试：" + str(exc),
                getattr(exc, "code", "configuration_failed"),
            ) from exc

    def grouped_frame_queries(self) -> bool:
        # Only the independently checked firmware and VXI-11 message boundary.
        # Other firmware, USB, SOCKET and custom transports retain scalar reads.
        resource = self.config.resource.upper()
        return bool(
            self.identity
            and self.identity.firmware == "00.01.19.00.02"
            and self.config.transport == "visa"
            and re.fullmatch(r"TCPIP\d*::[^:]+::INST\d+::INSTR", resource)
        )

    def _read_frame_settings(self, fields: tuple[str, ...]) -> dict[str, Any]:
        if not self.grouped_frame_queries():
            return {op: self.read(op) for op in fields}
        if fields not in (FRAME_SETTINGS, LIVE_SETTINGS):
            raise Unsupported("Only qualified DSA815 frame query groups may be combined")
        queried = tuple(op for op in fields if op != "sweep_points")
        specs = tuple(self.profile.commands[op] for op in queried)
        if any(spec.select or not spec.capability.readable for spec in specs):
            raise Unsupported("Frame queries must be readable and have no selector writes")
        payload = scalar_query_message(tuple(spec.query for spec in specs))
        raw = self.transport.query(payload)
        with parsing():
            responses = scalar_response_fields(raw, len(queried))
            values = {op: spec.decode(part) for op, spec, part in zip(queried, specs, responses, strict=True)}
        if "sweep_points" in fields:
            values["sweep_points"] = 601
        # Do not publish partially decoded groups or silently retry singly.
        self.capability_details.update(values)
        return values

    def invalidate_acquisition_context(self, reason: str) -> None:
        self._acquisition_context = None
        self._context_at = 0.0
        self._context_timestamp = ""
        self._stream_context_id = ""
        self._live_context_id = ""

    def _option_table_supported(self) -> bool:
        return not (self.config.transport == "tcp" or self.config.resource.upper().endswith("::SOCKET"))

    def read(self, operation: str, channel: int = 1) -> Any:
        if operation == "options" and not self._option_table_supported():
            # SYST:OPT? is a variable-length, multiline table (manual 2-179).
            # Raw sockets have no VISA END. A newline, idle gap, or assumed row
            # count cannot prove completeness; reject before polluting the stream.
            raise Unsupported("DSA815 选件表需要带消息结束标志的 VISA 连接；Socket 暂不支持")
        if operation == "sweep_points":
            self._context(channel, {})
            return 601
        try:
            value = super().read(operation, channel)
        except (UnicodeError, ValueError) as exc:
            raise DeviceError("Invalid DSA815 scalar response", "parse_error") from exc
        self.capability_details[operation] = value
        return value

    def refresh_capabilities(self) -> None:
        self.read("amplitude_unit")
        self.read("span")

    def read_settings(self, operations: tuple[str, ...], channel: int = 1):
        from lab_control.instruments.settings import cached_reading

        self._context(channel, {})
        readings = []
        for op in operations:
            if not self.capabilities().get(op).readable:
                readings.append(cached_reading(op, channel, self.configuration))
                continue
            if (
                op.startswith("marker_")
                and op != "marker_enabled"
                and not self.read("marker_enabled", channel)
            ):
                readings.append(
                    ParameterReading(op, channel, source="unavailable", error="标记关闭，参数不可读")
                )
            else:
                readings.append(
                    ParameterReading(
                        op, channel, self.read(op, channel), "model" if op == "sweep_points" else "instrument"
                    )
                )
        return tuple(readings)

    def capabilities(self):
        caps = super().capabilities()
        if not self._option_table_supported():
            caps = replace(
                caps,
                operations=tuple(
                    replace(cap, readable=False, reason="多行选件表需要带消息结束标志的 VISA 连接")
                    if cap.name == "options"
                    else cap
                    for cap in caps.operations
                ),
            )
        unit = self.capability_details.get("amplitude_unit", "DBM")
        if unit != "DBM":
            caps = replace(
                caps,
                operations=tuple(
                    replace(
                        cap,
                        writable=False,
                        reason="当前幅度单位仅开放读回；切回 dBm 后可调电平",
                        parameters={
                            "value": replace(
                                cap.parameters["value"], unit=UNITS[unit], minimum=None, maximum=None
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
                        reason="零跨度下仅显示标记时间坐标",
                        parameters={"value": replace(cap.parameters["value"], unit="s", maximum=7500)},
                    )
                    if cap.name == "marker_frequency"
                    else cap
                    for cap in caps.operations
                ),
            )
        return caps

    def validate_command(self, command: DeviceCommand):
        spec, parameters = super().validate_command(command)
        value: Any = parameters.get("value")
        op = command.operation
        if op in {"rbw", "vbw"} and value not in (RBW if op == "rbw" else VBW):
            raise ValidationError("DSA815 bandwidth requires a documented 1-3-10 discrete value")
        if op in {"start_frequency", "stop_frequency", "center_frequency", "span"} and value % 1:
            raise ValidationError("DSA815 frequency setting resolution is 1 Hz")
        return spec, parameters

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        op = command.operation
        value: Any = parameters.get("value")
        if op in {"start_frequency", "stop_frequency", "center_frequency", "span"}:
            if self.read("frequency_offset") != 0:
                raise Unsupported("Frequency writes with a display offset are not qualified")
            super().preflight(command, parameters)
            if op == "center_frequency":
                span = self.read("span")
                if not span / 2 <= value <= 1.5e9 - span / 2:
                    raise ValidationError("Center/span combination exceeds DSA815 frequency endpoints")
            elif op == "span":
                center = self.read("center_frequency")
                if value / 2 > min(center, 1.5e9 - center):
                    raise ValidationError("Span exceeds available frequency endpoints")
            if self.read("trigger_source") == "VID" and not (op == "span" and value == 0):
                raise ValidationError("Change video trigger before changing the frequency axis")
        if op in {"rbw", "vbw", "attenuation", "sweep_time"} and self.read(op + "_auto"):
            raise ValidationError(f"Disable {op}_auto explicitly before changing the manual value")
        if op in {"rbw", "vbw"} and self.read("detector") == "QPEAK":
            raise Unsupported("EMI/QPEAK bandwidths are outside this model contract")
        if op in {"reference_level", "attenuation"}:
            if self.read("amplitude_unit") != "DBM" or self.read("reference_offset") != 0:
                raise Unsupported("Level writes require dBm and zero reference offset")
            level = value if op == "reference_level" else self.read("reference_level")
            attenuation = value if op == "attenuation" else self.read("attenuation")
            if self.read("preamplifier"):
                raise Unsupported("Level writes with preamplifier enabled are not qualified")
            if not self.read("attenuation_auto") and level - attenuation > self.read("mixer_level"):
                raise ValidationError("Reference level minus attenuation exceeds mixer limit")
        if op == "sweep_time" and self.read("span") != 0 and value < 0.001:
            raise ValidationError("Nonzero span requires at least 1 ms sweep time")
        if op == "trigger_source" and value == "VID":
            if self.read("span") != 0 or self.read("detector") in {"RMS", "VAV"}:
                raise ValidationError("Video trigger needs zero span and a compatible detector")
        if op == "detector" and value in {"RMS", "VAV"} and self.read("trigger_source") == "VID":
            raise ValidationError("Detector is incompatible with video trigger")
        if op in {"marker_frequency", "marker_position", "marker_peak"}:
            if not self.read("marker_enabled") or self.read("marker_mode") != "POS":
                raise ValidationError("Enable the normal marker before moving it")
            if self.read("marker_tracking"):
                raise ValidationError("Disable continuous peak tracking before moving the marker")
            if op == "marker_frequency":
                if self.read("span") == 0 or self.read("marker_readout") != "FREQ":
                    raise ValidationError("Frequency marker requires a nonzero frequency axis")
                if not self.read("start_frequency") <= value <= self.read("stop_frequency"):
                    raise ValidationError("Marker frequency is outside the current axis")
        if op == "single":
            if self.read("continuous") or self.read("trigger_source") != "IMM":
                raise ValidationError("Single sweep requires single mode and immediate trigger")
            if self.read("measurement_mode") != "OFF":
                raise Unsupported("Optional measurement modes are not qualified")

    def apply(self, command: DeviceCommand) -> ApplyResult:
        self.invalidate_acquisition_context("apply")
        if command.operation == "auto_setup":
            return auto_setup(self, command)
        spec, parameters = self.validate_command(command)
        self.preflight(command, parameters)
        completed = 0
        try:
            context = self._context(command.channel, parameters)
            for payload in self.encode(command.operation, spec, context, parameters):
                self.transport.write(payload)
                completed += 1
            if command.operation == "single":
                # OPC alone is insufficient on firmware that acknowledges INIT immediately.
                deadline = time.monotonic() + self.config.timeout
                target = self.read("sweep_count")
                while self.read("sweep_current") < target:
                    if time.monotonic() >= deadline:
                        raise DeviceError("Single sweep completion timed out", "timeout")
                    time.sleep(0.05)
                if self.transport.query("*OPC?").strip() != b"1":
                    raise DeviceError("Invalid operation-complete response", "parse_error")
            readback = self.read(command.operation) if spec.query else None
            error = self.transport.query("SYST:ERR?").decode("ascii").strip()
            if not re.match(r"^[+]?0(?:\s|,|$)", error):
                return ApplyResult("rejected", command.operation, parameters, readback, completed, error)
            expected = parameters.get("value")
            matches = expected is None or agrees(command.operation, expected, readback)
            if command.operation == "marker_frequency" and expected is not None:
                # User Guide: marker resolution is span / (display points - 1).
                quantum = (
                    self.capability_details["stop_frequency"] - self.capability_details["start_frequency"]
                ) / 600
                matches = abs(expected - readback) <= quantum / 2 + 0.5
            if not matches:
                return ApplyResult(
                    "rejected",
                    command.operation,
                    parameters,
                    readback,
                    completed,
                    "Readback differs from request at documented resolution",
                )
            self.configuration[f"{command.channel}.{command.operation}"] = parameters
            return ApplyResult("success", command.operation, parameters, readback, completed)
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
            return ()  # Reused settings must not acquire a new readback timestamp.
        return tuple(
            ParameterReading(
                name,
                1,
                batch.configuration[name],
                "instrument",
                batch.settings_timestamps.get(name, batch.timestamp),
            )
            for name in SETTINGS
            if name in batch.configuration and (batch.validation != "live" or name in LIVE_SETTINGS)
        )

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        try:
            return self._acquire(request)
        except Exception:
            self.invalidate_acquisition_context("acquisition_failed")
            raise

    def _acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.channels != (1,):
            raise ValidationError("DSA815 acquisition accepts only channel 1")
        if request.kind != "spectrum":
            raise Unsupported("DSA815 acquisition kind must be spectrum")
        if self.identity is None:
            raise DeviceError("Identify before acquisition", "identity_required")
        streaming = request.consistency == "stream"
        if streaming and request.context_id != self._stream_context_id:
            self.invalidate_acquisition_context("new_recording_run")
            self._stream_context_id = request.context_id
        elif not streaming and self._stream_context_id:
            self.invalidate_acquisition_context("recording_finished")
        if request.consistency == "live" and request.context_id != self._live_context_id:
            self.invalidate_acquisition_context("new_monitoring_run")
            self._live_context_id = request.context_id
        if (
            request.consistency in {"stream", "live"}
            and not request.initiate
            and self._acquisition_context is not None
        ):
            before = dict(self._acquisition_context)
            raw = self.transport.query("TRAC:DATA? TRACE1")
            with parsing():
                measurement = trace(raw, before)
            return MeasurementBatch(
                self.identity,
                (measurement,),
                before,
                validation="cached",
                settings_timestamps={op: self._context_timestamp for op in before},
                performance={
                    "logical_queries": 1,
                    "configuration_query_mode": "cached",
                    "acquisition_context_id": request.context_id if streaming else "",
                    "configuration_source": "recording_start" if streaming else "monitoring_snapshot",
                },
            )
        if request.initiate:
            for cmd in (
                DeviceCommand("continuous", {"value": False}, confirmed=True),
                DeviceCommand("single", confirmed=True),
            ):
                result = self.apply(cmd)
                if result.outcome != "success":
                    raise DeviceError(result.error, "configuration_uncertain")
        fields = FRAME_SETTINGS
        checked_before = self._read_frame_settings(fields)
        before = {**(self._acquisition_context or {}), **checked_before}
        if before["measurement_mode"] != "OFF" or before["frequency_offset"] != 0:
            raise Unsupported("Only ordinary, unshifted spectrum traces are qualified")
        raw = self.transport.query("TRAC:DATA? TRACE1")
        after = self._read_frame_settings(fields)
        changes = {op: (checked_before[op], after[op]) for op in fields if checked_before[op] != after[op]}
        with parsing():
            if changes:
                # Only complete valid payloads qualify as configuration transitions.
                trace_values(raw, before)
                description = "; ".join(f"{op}: {old!r} -> {new!r}" for op, (old, new) in changes.items())
                raise DeviceError(
                    "DSA815 configuration changed during trace read (" + description + ")",
                    "configuration_changed",
                )
            measurement = trace(raw, before)
        timestamp = utc_now()
        self._acquisition_context = dict(before)
        self._context_at = time.monotonic()
        self._context_timestamp = timestamp
        stamps = {op: timestamp for op in before}
        return MeasurementBatch(
            self.identity,
            (measurement,),
            before,
            timestamp=timestamp,
            validation="full",
            settings_timestamps=stamps,
            performance={
                "logical_queries": 45,
                "configuration_query_mode": "grouped" if self.grouped_frame_queries() else "separate",
            },
        )
