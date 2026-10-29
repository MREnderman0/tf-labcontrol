"""M300 MC3132 inputs; explicit scan acquisition with full configuration checks."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    DeviceStatus,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import ScpiAdapter

from .protocol import boolean, csv_numbers, measurement_kind, scan_channels


class M300Adapter(ScpiAdapter):
    supports_live_acquisition = True
    supports_stream_acquisition = False
    acquisition_revision = "m300-channel-scalar-v2"
    capability_revision = 0

    def _remember(self, name: str, value: Any) -> None:
        if self.capability_details.get(name) != value:
            self.capability_details[name] = value
            self.capability_revision += 1

    def refresh_capabilities(self) -> None:
        """Queries only, including when connect skips acquisition initialization."""
        channels = [1]  # Instrument-wide controls use the common channel=1 convention.
        try:
            for slot in (100, 200, 300, 400, 500):
                identity = self.read(f"slot_{slot}")
                if identity.split(",")[1].strip() == "MC3132":
                    channels.extend(range(slot + 1, slot + 33))
            self.read("dmm_installed")
            self.read("scan_list")
            self.profile = replace(self.profile, channels=tuple(channels))
            self._remember("acquisition_channel_selection", "scan_list")
            self.capability_error = ""
        except DeviceError as exc:
            self.capability_error = str(exc)
            raise

    def capabilities_for_channel(self, channel: int) -> CapabilitySet:
        schema = super().capabilities_for_channel(channel)
        return replace(
            schema,
            operations=tuple(
                replace(cap, available=False, reason="请选择已识别的 MC3132 物理输入通道")
                if channel == 1 and cap.channel_scoped
                else cap
                for cap in schema.operations
            ),
        )

    def _input(self, channel: int) -> None:
        self._context(channel, {})
        if channel == 1:
            raise ValidationError("Select an identified MC3132 input, e.g. slot 4 channel 28 = 428")

    def _temperature(self, operation: str, channel: int) -> None:
        if operation in {"temperature_sensor", "temperature_unit", "thermistor_type"}:
            if self.read("function", channel) != "TEMP":
                raise Unsupported("先将此通道的测量功能设置为 TEMP")
            if operation == "thermistor_type" and self.read("temperature_sensor", channel) != "THER":
                raise Unsupported("先将此通道的温度传感器设置为 THER")

    def read(self, operation: str, channel: int = 1) -> Any:
        cap = self.capabilities().get(operation)
        if cap.channel_scoped:
            self._input(channel)
            self._temperature(operation, channel)
        value = super().read(operation, channel)
        self._remember(f"{channel}.{operation}" if cap.channel_scoped else operation, value)
        if operation == "scan_list":
            self._remember("acquisition_channels", list(scan_channels(value)))
        return value

    def read_status(self) -> DeviceStatus:
        values, errors = {}, []
        for operation in self.profile.status:
            try:
                values[operation] = self.read(operation)
            except DeviceError as exc:
                values[operation] = None
                errors.append(f"{operation}: {exc.code}: {exc}")
        return DeviceStatus(values, errors=tuple(errors))

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        readings = []
        for operation in operations:
            target = channel
            try:
                cap = self.capabilities().get(operation)
                target = channel if cap.channel_scoped else 1
                readings.append(
                    ParameterReading(operation, target, self.read(operation, target), "instrument")
                )
            except DeviceError as exc:
                readings.append(ParameterReading(operation, target, error=f"{exc.code}: {exc}"))
        return tuple(readings)

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        if self.capabilities().get(command.operation).channel_scoped:
            self._input(command.channel)
            self._temperature(command.operation, command.channel)
        if command.operation == "scan_list":
            channels = scan_channels(parameters["value"])
            if any(ch == 1 or ch not in self.profile.channels for ch in channels):
                raise ValidationError("Scan list contains an unidentified/unsupported input")
        if command.operation == "dmm_state" and parameters.get("value") and not self.read("dmm_installed"):
            raise Unsupported("M300 DMM module is not installed")

    def apply(self, command: DeviceCommand) -> ApplyResult:
        result = super().apply(command)
        if result.outcome == "success":
            self._remember(f"{command.channel}.{command.operation}", result.readback)
            if command.operation == "scan_list":
                self._remember("acquisition_channels", list(scan_channels(result.readback)))
        return result

    def _scan_snapshot(self) -> dict[str, Any]:
        scan = self.read("scan_list")
        channels = scan_channels(scan)
        size = self.read("scan_size")
        if len(channels) != size:
            raise DeviceError("M300 scan list/size disagree", "parse_error")
        config = {}
        for channel in channels:
            self._input(channel)
            value = self.read("configuration", channel)
            unit = self.read("temperature_unit", channel) if value.startswith("TEMP ") else ""
            quantity, display_unit = measurement_kind(value, unit)
            config[str(channel)] = dict(configuration=value, quantity=quantity, unit=display_unit)
        return dict(
            scan_list=scan,
            channels=channels,
            configuration=config,
            trigger_source=self.read("trigger_source"),
            trigger_count=self.read("trigger_count"),
            dmm_state=self.read("dmm_state"),
            monitor_state=self.read("monitor_state"),
            format={
                key: boolean(self.transport.query(f"FORM:READ:{key}?"))
                for key in ("UNIT", "TIME", "CHAN", "ALAR")
            },
        )

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind not in {"scalar", "measurement"}:
            raise Unsupported("M300 acquisition kind must be scalar")
        if not request.confirmed:
            raise DeviceError(
                "READ? starts a scan and replaces instrument reading memory; confirmation required",
                "confirmation_required",
            )
        if self.identity is None:
            raise DeviceError("Identify before acquisition", "identity_required")
        before = self._scan_snapshot()
        channels = before["channels"]
        if not channels:
            raise DeviceError("M300 scan list is empty; no measurement was started", "not_configured")
        if request.channels not in {(1,), channels}:
            raise ValidationError("Requested channels must match the instrument scan list")
        units = {(c["quantity"], c["unit"]) for c in before["configuration"].values()}
        if len(units) != 1:
            raise Unsupported("同一次监测的扫描通道须使用相同测量功能和单位")
        if before["trigger_source"] != "IMM" or before["trigger_count"] != 1:
            raise DeviceError("Single-scan acquisition requires IMM and trigger count 1", "not_configured")
        if not before["dmm_state"] or before["monitor_state"] or any(before["format"].values()):
            raise DeviceError("Requires DMM on, monitor off, and reading format fields off", "not_configured")
        values = csv_numbers(self.transport.query("READ?"))
        if len(values) != len(channels):
            raise DeviceError(
                f"M300 returned {len(values)} values for {len(channels)} scan channels", "parse_error"
            )
        after = self._scan_snapshot()
        if before != after:
            raise DeviceError(
                "M300 configuration changed during scan; frame discarded", "configuration_changed"
            )
        measurements = []
        for channel, value in zip(channels, values, strict=True):
            config = before["configuration"][str(channel)]
            metadata = {"configuration": config["configuration"]}
            if config["quantity"] == "temperature":
                metadata["conversion"] = (
                    "M300 built-in thermistor curve; sensor curve must be verified independently"
                )
            measurements.append(
                Measurement(config["quantity"], config["unit"], (value,), channel, metadata=metadata)
            )
        return MeasurementBatch(
            self.identity,
            tuple(measurements),
            before,
            validation="full",
            performance={"configuration_query_mode": "before_after"},
        )
