"""CoreMorrow XMT compatibility adapter.

The selected asset answers the vendor's configuration and raw channel queries,
but the response does not contain a trustworthy model or serial number.  The
adapter therefore keeps identity conservative while exposing only the bounded,
wire-verified non-negative voltage command and its explicit readback.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    MeasurementBatch,
    ParameterReading,
    Quality,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import ScpiAdapter, identity_from_scpi

from .profile import capability_schema, configured_channels
from .protocol import (
    DEFAULT_ADDRESS,
    READ_DATA_COMMAND,
    READ_MULT_CHANNEL_VOLTAGE_COMMAND,
    READ_POWER_CONFIG_COMMAND,
    XmtFrame,
    build_read,
    build_single_point,
    parse_frame,
    parse_mult_channel_voltage,
    parse_power_config,
    parse_raw_channel,
)
from .waveform import WaveformControl


def _option(config: ConnectionConfig, key: str, default: str) -> str:
    values = [item.split("=", 1)[1] for item in config.options if item.lower().startswith(f"{key}=")]
    if len(values) > 1:
        raise ValidationError(f"coremorrow.xmt accepts at most one {key}= option")
    return values[0] if values else default


class CoreMorrowXmtAdapter(ScpiAdapter):
    """Model-owned XMT adapter with bounded voltage control and readback."""

    READBACK_SETTLE_TIMEOUT = 1.0
    READBACK_SETTLE_INTERVAL = 0.05

    def __init__(self, transport, profile, config: ConnectionConfig) -> None:
        super().__init__(transport, profile, config)
        try:
            self.address = int(_option(config, "address", str(DEFAULT_ADDRESS)), 0)
            self.data_command = int(_option(config, "data_command", str(READ_DATA_COMMAND)), 0)
        except ValueError as exc:
            raise ValidationError("coremorrow.xmt address and data_command must be integers") from exc
        if not 0 <= self.address <= 0xFF:
            raise ValidationError("coremorrow.xmt address must be an unsigned byte")
        if self.data_command not in {5, 6}:
            raise ValidationError("coremorrow.xmt data_command must be 5 or 6")
        self.channels = configured_channels(config)
        self.power_config: dict[str, object] | None = None
        self.last_frames: list[str] = []
        self.waveform = WaveformControl(self)

    def bind_control_state(self, load, save) -> None:
        self.waveform.bind(load, save)

    def capabilities_for_channel(self, channel: int) -> CapabilitySet:
        return self.capabilities()

    def release_control(self) -> None:
        self.waveform.release()

    def capabilities(self) -> CapabilitySet:
        self.capability_details["control_state"] = self.waveform.snapshot()
        channels = self.channels
        if self.power_config is not None:
            count = self.power_config["channel_count"]
            if isinstance(count, int):
                channels = tuple(range(1, count + 1))
        operations = capability_schema(self.config).operations
        if self.waveform.paused and not self.waveform.normalizing:
            operations = tuple(
                replace(
                    cap,
                    readable=False,
                    writable=False,
                    reason="波形运行状态未确认或有波形输出；请先停止波形并进入常规。",
                )
                if cap.name in {"voltage", "measured_voltage", "raw_channel_data"}
                else cap
                for cap in operations
            )
        return CapabilitySet("power_supply", channels, operations)

    def _write_frame(self, payload: bytes) -> None:
        writer = getattr(self.transport, "write_frame", None)
        if callable(writer):
            writer(payload)
        else:
            self.transport.write(payload)
        self.last_frames.append(payload.hex(" "))

    def _read_voltages(self) -> tuple[float, ...]:
        frame = self._query(READ_MULT_CHANNEL_VOLTAGE_COMMAND)
        return parse_mult_channel_voltage(frame, len(self.channels))

    def _query(self, b3: int, b4: int = 0, payload: bytes = b"") -> XmtFrame:
        query = build_read(self.address, b3, b4, payload)
        query_frame = getattr(self.transport, "query_frame", None)
        if callable(query_frame):
            raw = query_frame(query)
        else:
            # The shared MockTransport contract is text/bytes based.  Keep
            # this fallback limited to scripted tests; production factories
            # always provide query_frame and never reinterpret text replies.
            raw = self.transport.query(query.hex(" ").upper())
            if not isinstance(raw, bytes):
                raw = bytes(raw)
            try:
                raw = bytes.fromhex(raw.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise DeviceError("XMT mock response is not hexadecimal", "parse_error") from exc
        frame = parse_frame(raw, address=self.address, command_b3=b3, command_b4=b4)
        self.last_frames.extend((query.hex(" "), raw.hex(" ")))
        return frame

    def identify(self) -> DeviceIdentity:
        # The shared MockTransport identity contract is text based.  Accept
        # only this scripted fallback; production serial/VISA transports use
        # the binary configuration frame below.
        if not callable(getattr(self.transport, "query_frame", None)):
            raw = self.transport.query(self.profile.identity_query)
            try:
                text = raw.decode("ascii")
            except UnicodeDecodeError:
                text = ""
            if text.count(",") >= 1:
                identity = identity_from_scpi(raw)
                if (
                    identity.manufacturer.casefold() != "coremorrow"
                    or identity.model != self.profile.models[0]
                ):
                    raise DeviceError(
                        "CoreMorrow mock identity differs from the pending model contract",
                        "identity_conflict",
                    )
                self.identity = identity
                return identity
        frame = self._query(READ_POWER_CONFIG_COMMAND)
        self.power_config = parse_power_config(frame)
        channel_count = self.power_config["channel_count"]
        if not isinstance(channel_count, int):
            raise DeviceError("XMT channel count is not an integer", "parse_error")
        channels = channel_count
        if channels < 1 or channels > 8:
            raise DeviceError("XMT power configuration reported an invalid channel count", "parse_error")
        self.channels = tuple(range(1, channels + 1))
        identity = DeviceIdentity(
            "CoreMorrow",
            "XMT-compatible controller (exact model pending)",
            confidence="protocol_only",
            raw=frame.raw.hex(" "),
        )
        self.identity = identity
        return identity

    def _channel(self, channel: int) -> int:
        if type(channel) is not int or channel not in self.channels:
            raise ValidationError(f"CoreMorrow XMT channel must be one of {self.channels}")
        return channel

    def read(self, operation: str, channel: int = 1) -> Any:
        self.capabilities().get(operation)
        if operation == "control_state":
            return self.waveform.snapshot()
        if operation == "power_config":
            frame = self._query(READ_POWER_CONFIG_COMMAND)
            self.power_config = parse_power_config(frame)
            return dict(self.power_config)
        if operation == "raw_channel_data":
            public_channel = self._channel(channel)
            frame = self._query(self.data_command, payload=bytes((public_channel - 1,)))
            return parse_raw_channel(frame, public_channel - 1)
        if operation in {"voltage", "measured_voltage"}:
            public_channel = self._channel(channel)
            values = self._read_voltages()
            return values[public_channel - 1]
        raise Unsupported(f"CoreMorrow XMT operation is not implemented: {operation}")

    def read_status(self) -> DeviceStatus:
        config = self.read("power_config")
        return DeviceStatus(
            {
                "power_config": config,
                "identity_confidence": "protocol_only",
                "model_confirmation": "pending",
                "control_state": self.read("control_state"),
            }
        )

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        readings = []
        for operation in operations:
            capability = self.capabilities().get(operation)
            target = channel if capability.channel_scoped else 1
            if operation == "control_state":
                readings.append(
                    ParameterReading(
                        operation,
                        target,
                        self.waveform.snapshot(),
                        "unavailable",
                        quality=Quality.STALE,
                        error="模式和波形参数无可靠仪器读回",
                    )
                )
                continue
            readings.append(ParameterReading(operation, target, self.read(operation, target), "instrument"))
        return tuple(readings)

    def _settled_voltage_readback(self, channel: int, requested: float) -> float:
        """Poll readback after one write without replaying the write command."""
        deadline = time.monotonic() + self.READBACK_SETTLE_TIMEOUT
        last = self.read("voltage", channel)
        while not math.isclose(requested, last, rel_tol=0.0, abs_tol=0.01):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.READBACK_SETTLE_INTERVAL, remaining))
            last = self.read("voltage", channel)
        return last

    def apply(self, command: DeviceCommand) -> ApplyResult:
        capability = self.capabilities().get(command.operation)
        # Validate the command shape before the readback path.  The generic
        # control harness deliberately sends an unknown parameter to every
        # capability and must not cause a device query in response.
        parameters = capability.validate(command.parameters)
        if not capability.channel_scoped and command.channel != 1:
            raise ValidationError("This command applies to the whole instrument; use channel=1")
        if command.operation == "control_state":
            return ApplyResult("success", command.operation, parameters, self.read("control_state"), 0)
        if command.operation == "standard_waveform":
            return self.waveform.start(self._channel(command.channel), parameters)
        if command.operation == "waveform_stop":
            return self.waveform.stop(self._channel(command.channel))
        if command.operation == "normal_mode":
            return self.waveform.normal()
        if command.operation == "power_config":
            # ``apply`` is used by the generic fixture harness for every
            # package.  Treating this read-only operation as a read keeps the
            # harness honest: it consumes no setting command and reports the
            # validated configuration as readback.
            return ApplyResult("success", command.operation, parameters, self.read("power_config"), 0)
        if command.operation == "raw_channel_data":
            self._channel(command.channel)
            return ApplyResult(
                "success", command.operation, parameters, self.read("raw_channel_data", command.channel), 0
            )
        if command.operation == "voltage":
            if self.waveform.paused and not self.waveform.normalizing:
                raise ValidationError("请先停止波形并进入常规模式，再设置直流电压。")
            channel = self._channel(command.channel)
            requested = parameters["value"]
            # Save the setting before a write.  A failed write/readback is
            # reported as uncertain; this path never retries or silently
            # restores a command whose completion is unknown.
            original = self.read("voltage", channel)
            payload = build_single_point(self.address, channel - 1, requested)
            try:
                self._write_frame(payload)
            except DeviceError as exc:
                return ApplyResult(
                    "uncertain",
                    command.operation,
                    parameters,
                    completed_steps=0,
                    error=f"XMT voltage write uncertain; original={original!r}; {exc}",
                )
            try:
                readback = self._settled_voltage_readback(channel, requested)
            except DeviceError as exc:
                return ApplyResult(
                    "uncertain",
                    command.operation,
                    parameters,
                    completed_steps=1,
                    error=f"XMT voltage write sent but readback failed; original={original!r}; {exc}",
                )
            if not math.isclose(requested, readback, rel_tol=0.0, abs_tol=0.01):
                return ApplyResult(
                    "rejected",
                    command.operation,
                    parameters,
                    readback,
                    1,
                    "XMT voltage readback did not settle to request; "
                    f"original={original!r}; last_readback={readback!r}",
                )
            self.configuration[f"{channel}.voltage"] = parameters
            return ApplyResult("success", command.operation, parameters, readback, 1)
        raise Unsupported(f"CoreMorrow XMT operation is not writable: {command.operation}")

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        raise Unsupported(
            "Calibrated acquisition is disabled until the exact CoreMorrow model and raw-data scaling are confirmed"
        )
