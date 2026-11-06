"""Fixed-frame protocol for the manually selected optical-comb ground-test pump."""

from __future__ import annotations

import struct
from typing import Protocol

from lab_control.domain.connections import ConnectionConfig
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
    Unsupported,
    ValidationError,
)

ADAPTER_ID = "custom.optical_comb_pump"
MODEL = "光梳地检泵浦驱动"
SOURCE = "泵浦激光驱动设备通信说明"
FRAME = struct.Struct(">BBIBB")
CHANNEL = 1
READ_SETTINGS = 0xF1
OUTPUT = 0xF2
OUTPUT_OFF = "pump_output_off"

# Only channel 01 is documented with complete request and response examples.
SETTINGS = (
    ("current", 0x01, "A", 1_000_000),
    ("temperature", 0x12, "°C", 1_000),
    ("output", OUTPUT, "", 1),
    ("thermistor_beta", 0x0A, "", 1),
    ("pump_lowpass", 0xF3, "", 1),
    ("pump_current_limit", 0x11, "A", 1_000_000),
)
BOOLEAN_SETTINGS = {"output", "pump_lowpass"}

# Keep ambiguous temperature scaling and undocumented TEC sign in wire units.
TELEMETRY = {
    "current": (0x04, "A", 1_000_000),
    "pump_ld_voltage": (0x08, "V", 1_000),
    "pump_ld_temperature_raw": (0x14, "raw", 1),
    "pump_photodiode_current": (0x2E, "µA", 10),
    "pump_tec_current_raw": (0x06, "raw", 1),
    "pump_tec_voltage_raw": (0x07, "raw", 1),
    "pump_thermistor_resistance": (0x09, "Ω", 1),
}


class FixedTransport(Protocol):
    def query_fixed(self, payload: bytes, count: int) -> bytes: ...
    def close(self) -> None: ...


def frame(command: int, value: int = 0, channel: int = CHANNEL) -> bytes:
    return FRAME.pack(0x48, command, value, channel, 0x53)


def payload(reply: bytes, command: int) -> int:
    if len(reply) == FRAME.size:
        head, actual, value, channel, tail = FRAME.unpack(reply)
        if (head, actual, channel, tail) == (0x48, command, CHANNEL, 0x53):
            return value
    raise DeviceError(f"泵浦回包帧格式、命令或通道不匹配（预期 0x{command:02X} / CH 1）", "parse_error")


def decode_settings(reply: bytes) -> dict[str, int | float | bool]:
    if len(reply) != FRAME.size * len(SETTINGS):
        raise DeviceError("泵浦设置回包长度不正确", "parse_error")
    values: dict[str, int | float | bool] = {}
    for index, (name, command, _unit, scale) in enumerate(SETTINGS):
        start = index * FRAME.size
        value = payload(reply[start : start + FRAME.size], command)
        if name in BOOLEAN_SETTINGS:
            if value not in (0, 1):
                raise DeviceError(f"泵浦状态 0x{command:02X} 必须为 0 或 1", "parse_error")
            values[name] = bool(value)
        else:
            values[name] = value if scale == 1 else value / scale
    return values


def capability_schema(_config: ConnectionConfig) -> CapabilitySet:
    settings = tuple(
        Capability(
            name,
            {"value": Parameter("boolean" if name in BOOLEAN_SETTINGS else "number", unit)},
            readable=True,
            channel_scoped=True,
            source=SOURCE,
            section="5.1",
        )
        for name, _command, unit, _scale in SETTINGS
    )
    return CapabilitySet(
        "pump_driver",
        (CHANNEL,),
        (
            Capability(
                OUTPUT_OFF,
                writable=True,
                channel_scoped=True,
                requires_confirmation=True,
                source=SOURCE,
                section="3.5, 5.1",
            ),
            *settings,
        ),
    )


class OpticalCombPumpAdapter:
    adapter_id = ADAPTER_ID

    def __init__(self, config: ConnectionConfig, transport: FixedTransport) -> None:
        self.transport = transport
        self.schema = capability_schema(config)
        self.identity = DeviceIdentity("自制", MODEL, confidence="manual_protocol")
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.transport.close()

    def _query(self, request: bytes, count: int) -> bytes:
        if self.closed:
            raise DeviceError("泵浦会话已关闭，请断开后重新选择串口", "manual_reconnect_required")
        return self.transport.query_fixed(request, count)

    def _failed(self, error: Exception) -> DeviceError:
        message = f"泵浦通信异常：{error}。请断开后重新选择串口。"
        try:
            self.close()
        except Exception as cleanup:
            message += f" 串口关闭失败：{cleanup}"
        return DeviceError(message, "manual_reconnect_required")

    def _settings(self) -> dict[str, int | float | bool]:
        try:
            return decode_settings(self._query(frame(READ_SETTINGS), FRAME.size * len(SETTINGS)))
        except DeviceError as exc:
            raise self._failed(exc) from exc

    def identify(self) -> DeviceIdentity:
        self._settings()
        return self.identity

    def capabilities(self) -> CapabilitySet:
        return self.schema

    def read_status(self) -> DeviceStatus:
        return DeviceStatus(self._settings())

    def read_settings(
        self, operations: tuple[str, ...], channel: int = CHANNEL
    ) -> tuple[ParameterReading, ...]:
        if type(channel) is not int or channel != CHANNEL:
            raise ValidationError("泵浦操作接口仅支持 CH 1")
        if any(not self.schema.get(name).readable for name in operations):
            raise Unsupported("该泵浦操作没有参数读回")
        if not operations:
            return ()
        settings = self._settings()
        return tuple(ParameterReading(name, channel, settings[name], "instrument") for name in operations)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        if type(command.channel) is not int or command.channel != CHANNEL:
            raise ValidationError("泵浦操作接口仅支持 CH 1")
        if command.operation != OUTPUT_OFF:
            raise Unsupported("泵浦目前仅允许关闭 CH 1 输出")
        self.schema.get(OUTPUT_OFF).validate(command.parameters)
        if not command.confirmed:
            raise DeviceError("关闭泵浦输出需要确认", "confirmation_required")
        try:
            request = frame(OUTPUT)
            if self._query(request, FRAME.size) != request:
                raise DeviceError("关闭输出应答与发送帧不一致", "parse_error")
            settings = self._settings()
        except DeviceError as exc:
            error = exc if self.closed else self._failed(exc)
            return ApplyResult("uncertain", OUTPUT_OFF, {}, "CH 1 输出状态待核实", error=str(error))
        if settings["output"]:
            return ApplyResult("rejected", OUTPUT_OFF, {}, "CH 1 输出仍开启（设备读回）")
        return ApplyResult("success", OUTPUT_OFF, {}, "CH 1 输出已关闭（设备读回）", completed_steps=1)

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        name = request.quantity or "current"
        if (
            request.kind != "scalar"
            or request.channels != (CHANNEL,)
            or request.initiate
            or name not in TELEMETRY
        ):
            raise Unsupported("泵浦仅支持 CH 1 的被动标量读取")
        command, unit, scale = TELEMETRY[name]
        try:
            raw = payload(self._query(frame(command), FRAME.size), command)
        except DeviceError as exc:
            raise self._failed(exc) from exc
        value = raw if scale == 1 else raw / scale
        return MeasurementBatch(
            self.identity,
            (Measurement(name, unit, [value], CHANNEL, metadata={"wire_value": raw}),),
            {},
            time_basis="host_utc_at_read",
        )
