"""Independent JW8105 binary-protocol adapter."""

from __future__ import annotations

from typing import Any

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import ScpiAdapter

from .profile import capability_schema, configured_channels
from .protocol import (
    DEFAULT_ADDRESS,
    MAX_DISPLAY_WAVELENGTHS,
    build_frame,
    build_wavelength,
    parse_frame,
    powers_16,
    powers_32,
    signed_le,
    wavelengths,
)

CONNECTION_COMMAND = 0x0140
POWER_COMMAND = 0x0142
WAVELENGTH_SET_COMMAND = 0x0146
WAVELENGTH_READ_COMMAND = 0x0148
DISPLAY_POWER_COMMAND = 0x0162
SAMPLING_SET_COMMAND = 0x0438
SAMPLING_READ_COMMAND = 0x043C
PDL_READ_COMMAND = 0x0440
DISPLAY_WAVELENGTHS_READ_COMMAND = 0x0730
DISPLAY_WAVELENGTHS_SET_COMMAND = 0x0732


def _module_id(config: ConnectionConfig) -> int:
    values = [
        item.split("=", 1)[1] for item in config.options if item.lower().startswith(("module=", "module_id="))
    ]
    if not values:
        return DEFAULT_ADDRESS
    if len(values) != 1:
        raise ValidationError("JW8105 requires one module=1 or module_id=1 option")
    try:
        result = int(values[0], 0)
    except ValueError as exc:
        raise ValidationError("JW8105 module option must be an integer") from exc
    if result not in (1, 2):
        raise ValidationError("JW8105 module address must be 1 or 2")
    return result


class Jw8105Adapter(ScpiAdapter):
    """Strict implementation of the supplied JW8105 V23.02.02 protocol."""

    def __init__(self, transport, profile, config: ConnectionConfig) -> None:
        super().__init__(transport, profile, config)
        self.module_id = _module_id(config)
        self.channels = configured_channels(config)
        self.connection_data = b""

    def capabilities(self) -> CapabilitySet:
        return capability_schema(self.config)

    def _query(
        self, command: int, data: bytes = b"", response: int | None = None, *, module_id: int | None = None
    ):
        address = self.module_id if module_id is None else module_id
        payload = build_frame(address, command, data)
        query_frame = getattr(self.transport, "query_frame", None)
        if callable(query_frame):
            raw = query_frame(payload)
        else:
            # The production factories always provide query_frame.  This
            # fallback keeps the shared MockTransport contract useful without
            # pretending that its text token is a wire command.
            token = self.profile.identity_query if command == CONNECTION_COMMAND else payload.hex(" ").upper()
            raw = self.transport.query(token)
            if not isinstance(raw, bytes):
                raw = bytes(raw)
            if not raw.startswith(b"{"):
                try:
                    raw = bytes.fromhex(raw.decode("ascii"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise DeviceError(
                        "JW8105 mock response is not a hexadecimal frame", "parse_error"
                    ) from exc
        expected = response if response is not None else command + 1
        return parse_frame(raw, address=address, command=expected)

    def identify(self) -> DeviceIdentity:
        frame = self._query(CONNECTION_COMMAND, response=CONNECTION_COMMAND + 1)
        if len(frame.data) != 4:
            raise DeviceError(
                "JW8105 connection response must contain four configuration bytes", "parse_error"
            )
        self.connection_data = frame.data
        # The protocol has no serial-number or firmware query.  Model identity
        # is therefore protocol-level only and never substitutes a serial.
        identity = DeviceIdentity(
            "Joinwit",
            "JW8105",
            confidence="model_only",
            raw=frame.raw.hex(" "),
        )
        self.identity = identity
        return identity

    def _channel(self, channel: int) -> int:
        if type(channel) is not int or channel not in self.channels:
            raise ValidationError(f"JW8105 channel must be one of {self.channels}")
        return channel

    def _module_channel(self, channel: int) -> tuple[int, int]:
        self._channel(channel)
        if len(self.channels) == 16:
            return (channel - 1) // 8 + 1, (channel - 1) % 8 + 1
        return self.module_id, channel

    def _powers(self, *, display: bool, module_id: int | None = None) -> tuple[float, ...]:
        command = DISPLAY_POWER_COMMAND if display else POWER_COMMAND
        frame = self._query(command, module_id=module_id)
        expected = 0x0163 if display else 0x0143
        if frame.command != expected:
            raise DeviceError("JW8105 power response command mismatch", "parse_error")
        return powers_32(frame.data) if display else powers_16(frame.data)

    def read(self, operation: str, channel: int = 1) -> Any:
        module_id, local_channel = self._module_channel(channel)
        if operation == "power":
            return self._powers(display=False, module_id=module_id)[local_channel - 1]
        if operation == "display_power":
            return self._powers(display=True, module_id=module_id)[local_channel - 1]
        if operation == "wavelength":
            frame = self._query(WAVELENGTH_READ_COMMAND)
            return signed_le(frame.data, scale=100, width=4)
        if operation == "sampling_rate":
            frame = self._query(SAMPLING_READ_COMMAND, bytes((8,)), module_id=module_id)
            if len(frame.data) != 9:
                raise DeviceError(
                    "JW8105 sampling-rate response must contain 8 rates and PDL time",
                    "parse_error",
                )
            return frame.data[local_channel - 1]
        if operation == "display_wavelengths":
            frame = self._query(DISPLAY_WAVELENGTHS_READ_COMMAND)
            return wavelengths(frame.data)
        if operation == "pdl":
            frame = self._query(PDL_READ_COMMAND, module_id=module_id)
            if len(frame.data) != 64:
                raise DeviceError("JW8105 PDL response must contain 8 max/min pairs", "parse_error")
            offset = (local_channel - 1) * 8
            maximum = signed_le(frame.data[offset : offset + 4], scale=1000, width=4)
            minimum = signed_le(frame.data[offset + 4 : offset + 8], scale=1000, width=4)
            return {"maximum_dbm": maximum, "minimum_dbm": minimum, "pdl_db": maximum - minimum}
        raise Unsupported(f"JW8105 operation is not implemented: {operation}")

    def read_status(self) -> DeviceStatus:
        modules = (1, 2) if len(self.channels) == 16 else (self.module_id,)
        powers = tuple(value for module in modules for value in self._powers(display=False, module_id=module))
        values: dict[str, Any] = {
            f"{channel}.power": value for channel, value in zip(self.channels, powers, strict=True)
        }
        values["module.wavelength"] = self.read("wavelength")
        values["module.connection_data"] = self.connection_data.hex(" ")
        return DeviceStatus(values)

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        return tuple(
            ParameterReading(operation, channel, self.read(operation, channel), "instrument")
            for operation in operations
        )

    def _confirmed(self, command: DeviceCommand) -> None:
        if not command.confirmed:
            raise DeviceError("JW8105 setting changes require explicit confirmation", "confirmation_required")

    def apply(self, command: DeviceCommand) -> ApplyResult:
        capability = self.capabilities().get(command.operation)
        parameters = capability.validate(command.parameters)
        try:
            self._confirmed(command)
            channel = self._channel(command.channel)
            module_id, local_channel = self._module_channel(channel)
            value = parameters.get("value")
            if command.operation == "wavelength":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValidationError("JW8105 wavelength value must be numeric")
                payload = build_wavelength(float(value))
                self._query(WAVELENGTH_SET_COMMAND, payload, WAVELENGTH_SET_COMMAND + 1)
                readback = self.read("wavelength")
            elif command.operation == "sampling_rate":
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 8:
                    raise ValidationError("JW8105 sampling_rate value must be an integer code in [0, 8]")
                self._query(
                    SAMPLING_SET_COMMAND,
                    bytes((local_channel, value)),
                    SAMPLING_SET_COMMAND + 1,
                    module_id=module_id,
                )
                readback = self.read("sampling_rate", channel)
            elif command.operation == "display_wavelengths":
                if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_DISPLAY_WAVELENGTHS:
                    raise ValidationError("JW8105 display_wavelengths must contain 1..32 values")
                encoded = []
                for item in value:
                    try:
                        numeric = float(item)
                    except (TypeError, ValueError) as exc:
                        raise ValidationError("JW8105 display wavelengths must be numeric") from exc
                    if not 850 <= numeric <= 1625 or numeric != round(numeric):
                        raise ValidationError(
                            "JW8105 display wavelengths must be integral nanometres in [850, 1625]"
                        )
                    encoded.append(int(numeric).to_bytes(2, "little"))
                payload = bytes((len(value),)) + b"".join(encoded)
                self._query(DISPLAY_WAVELENGTHS_SET_COMMAND, payload, DISPLAY_WAVELENGTHS_SET_COMMAND + 1)
                readback = self.read("display_wavelengths")
            else:
                raise Unsupported(f"JW8105 operation is not writable: {command.operation}")
            self.configuration[f"{command.channel}.{command.operation}"] = {"value": value}
            return ApplyResult("success", command.operation, {"value": value}, readback, 1)
        except DeviceError as exc:
            return ApplyResult("uncertain", command.operation, parameters, completed_steps=0, error=str(exc))

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if self.identity is None:
            raise DeviceError("Identify the instrument before acquisition", "identity_required")
        if any(channel not in self.channels for channel in request.channels):
            raise ValidationError(f"JW8105 channels must be one of {self.channels}")
        addresses = {self._module_channel(channel)[0] for channel in request.channels}
        # Follow the instrument's displayed power without reading or changing
        # wavelength settings (V23.02.02, section II.4, command 0x0162).
        # Read each required module once; publish only after every reply validates.
        values = {address: self._powers(display=True, module_id=address) for address in sorted(addresses)}
        measurements = tuple(
            Measurement(
                "power",
                "dBm",
                values[self._module_channel(channel)[0]][self._module_channel(channel)[1] - 1],
                channel=channel,
            )
            for channel in request.channels
        )
        return MeasurementBatch(
            self.identity,
            measurements,
            dict(self.configuration),
            validation="model_protocol",
        )
