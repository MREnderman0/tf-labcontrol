"""Read-only CoreMorrow XMT compatibility adapter.

The selected asset answers the vendor's configuration and raw channel queries,
but the response does not contain a trustworthy model or serial number.  The
adapter therefore exposes protocol-level reads only and rejects all writes and
calibrated acquisitions until the exact controller model is confirmed.
"""

from __future__ import annotations

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
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import ScpiAdapter, identity_from_scpi

from .profile import capability_schema, configured_channels
from .protocol import (
    DEFAULT_ADDRESS,
    READ_DATA_COMMAND,
    READ_POWER_CONFIG_COMMAND,
    XmtFrame,
    build_read,
    parse_frame,
    parse_power_config,
    parse_raw_channel,
)


def _option(config: ConnectionConfig, key: str, default: str) -> str:
    values = [item.split("=", 1)[1] for item in config.options if item.lower().startswith(f"{key}=")]
    if len(values) > 1:
        raise ValidationError(f"coremorrow.xmt accepts at most one {key}= option")
    return values[0] if values else default


class CoreMorrowXmtAdapter(ScpiAdapter):
    """Strict read-only adapter for the observed XMT frame family."""

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

    def capabilities(self) -> CapabilitySet:
        channels = self.channels
        if self.power_config is not None:
            count = self.power_config["channel_count"]
            if isinstance(count, int):
                channels = tuple(range(1, count + 1))
        return CapabilitySet("multimeter", channels, capability_schema(self.config).operations)

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
                if identity.manufacturer.casefold() != "coremorrow" or identity.model != self.profile.models[0]:
                    raise DeviceError("CoreMorrow mock identity differs from the pending model contract", "identity_conflict")
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
        if operation == "power_config":
            frame = self._query(READ_POWER_CONFIG_COMMAND)
            self.power_config = parse_power_config(frame)
            return dict(self.power_config)
        if operation == "raw_channel_data":
            public_channel = self._channel(channel)
            frame = self._query(self.data_command, payload=bytes((public_channel - 1,)))
            return parse_raw_channel(frame, public_channel - 1)
        raise Unsupported(f"CoreMorrow XMT operation is not implemented: {operation}")

    def read_status(self) -> DeviceStatus:
        config = self.read("power_config")
        return DeviceStatus(
            {
                "power_config": config,
                "identity_confidence": "protocol_only",
                "model_confirmation": "pending",
            }
        )

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        return tuple(
            ParameterReading(operation, channel if operation == "raw_channel_data" else 1, self.read(operation, channel))
            for operation in operations
        )

    def apply(self, command: DeviceCommand) -> ApplyResult:
        capability = self.capabilities().get(command.operation)
        # Validate the command shape before the readback path.  The generic
        # control harness deliberately sends an unknown parameter to every
        # capability and must not cause a device query in response.
        parameters = capability.validate(command.parameters)
        if not capability.channel_scoped and command.channel != 1:
            raise ValidationError("This command applies to the whole instrument; use channel=1")
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
        raise Unsupported(
            "CoreMorrow XMT is protocol-only and read-only until the exact model, ranges and recovery procedure are confirmed"
        )

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        raise Unsupported(
            "Calibrated acquisition is disabled until the exact CoreMorrow model and raw-data scaling are confirmed"
        )

