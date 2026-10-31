from __future__ import annotations

import ctypes as ct
import time
from typing import Any

import numpy as np

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
    Quality,
    Unsupported,
    ValidationError,
)
from lab_control.transport.read_wait import read_timeout
from lab_control.transport.sdk import VendorLibrary

SOURCE = "https://www.picotech.com/helpfiles/6000a-api/"
I16, I32, U32, U64, F64, PTR = ct.c_int16, ct.c_int32, ct.c_uint32, ct.c_uint64, ct.c_double, ct.c_void_p
RAW = 0x80000000
# 6404E input ranges, not every range in the shared Pico SDK enum.
# 6000E data sheet, Vertical (analog channels), p. 31.
RANGES = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20)


class StreamingInfo(ct.Structure):
    _pack_ = 1
    _fields_ = [
        ("channel", U32),
        ("mode", U32),
        ("type", U32),
        ("count", I32),
        ("buffer_index", U64),
        ("start", I32),
        ("overflow", I16),
    ]


class TriggerInfo(ct.Structure):
    _pack_ = 1
    _fields_ = [("trigger_at", U64), ("triggered", I16), ("auto_stop", I16)]


class PicoAdapter:
    adapter_id = "pico.6404e"

    def __init__(self, config: ConnectionConfig, library: Any = None) -> None:
        self.config = config
        self.sdk = VendorLibrary(config.sdk_path, library)
        self.handle = I16()
        self.opened = False
        self.identity: DeviceIdentity | None = None
        self.settings: dict[int, dict[str, Any]] = {}
        self.triggers: dict[int, dict[str, Any]] = {}
        self.timebase = 10
        self.sample_interval = 1e-6
        self.buffers: dict[int, Any] = {}
        serial = config.expected_serial or config.resource.removeprefix("PICO::")
        if not serial:
            raise ValidationError("Select a Pico serial explicitly")
        try:
            self.call(
                "OpenUnit",
                [PTR, ct.c_char_p, U32],
                ct.byref(self.handle),
                serial.encode("ascii"),
                0,
                session=False,
            )
            self.opened = True
        except Exception:
            self.sdk.close()
            raise

    def call(self, name: str, types: list[Any], *args: Any, session: bool = True) -> None:
        if session:
            types, args = [I16, *types], (self.handle, *args)
        result = self.sdk.function("ps6000a" + name, types, U32)(*args)
        if result:
            raise DeviceError(f"ps6000a{name} returned status {result:#x}", "sdk_error")

    def info(self, code: int) -> str:
        buffer, length = ct.create_string_buffer(256), I16()
        self.call("GetUnitInfo", [PTR, I16, PTR, U32], buffer, 256, ct.byref(length), code)
        return buffer.value.decode("ascii")

    def identify(self) -> DeviceIdentity:
        identity = DeviceIdentity("Pico Technology", self.info(3), self.info(4), self.info(9))
        if identity.model.upper() != "6404E":
            raise DeviceError("Expected PicoScope 6404E", "identity_conflict")
        self.identity = identity
        return identity

    def capabilities(self) -> CapabilitySet:
        return self.capability_schema(self.config)

    @staticmethod
    def capability_schema(config: ConnectionConfig) -> CapabilitySet:
        return CapabilitySet(
            "oscilloscope",
            (1, 2, 3, 4),
            (
                Capability(
                    "channel",
                    {
                        "enabled": Parameter("boolean"),
                        "range": Parameter("enum", choices=tuple(str(v) for v in RANGES)),
                        "coupling": Parameter("enum", choices=("AC", "DC", "DC50")),
                        "offset": Parameter(unit="V", minimum=-20, maximum=20),
                    },
                    writable=True,
                    channel_scoped=True,
                    source=SOURCE,
                    section="ps6000aSetChannelOn/Off",
                ),
                Capability(
                    "timebase",
                    {"value": Parameter("integer", minimum=0, maximum=1000000)},
                    writable=True,
                    source=SOURCE,
                    section="ps6000aGetTimebase",
                ),
                Capability(
                    "sample_interval",
                    {"value": Parameter(unit="s", minimum=1e-9, maximum=1)},
                    writable=True,
                    source=SOURCE,
                    section="ps6000aRunStreaming",
                ),
                Capability(
                    "trigger",
                    {
                        "enabled": Parameter("boolean"),
                        "threshold": Parameter(unit="V", minimum=-20, maximum=20),
                        "direction": Parameter("enum", choices=("RISING", "FALLING")),
                        "timeout_us": Parameter("integer", minimum=0, maximum=10000000),
                    },
                    writable=True,
                    channel_scoped=True,
                    source=SOURCE,
                    section="ps6000aSetSimpleTrigger",
                ),
                Capability("block", readable=True, source=SOURCE, section="ps6000aRunBlock/GetValues"),
                Capability(
                    "streaming",
                    readable=True,
                    source=SOURCE,
                    section="ps6000aRunStreaming/GetStreamingLatestValues",
                ),
            ),
        )

    def read_status(self) -> DeviceStatus:
        return DeviceStatus(
            {
                "identity": self.identity,
                "configured_channels": self.settings,
                "timebase_index": self.timebase,
                "sample_interval_s": self.sample_interval,
                "settings_origin": "SDK accepted calls in this session; no invented hardware readback",
            }
        )

    def read_settings(self, operations: tuple[str, ...], channel: int = 1):
        from lab_control.instruments.settings import cached_reading

        if channel not in (1, 2, 3, 4):
            raise ValidationError("Invalid Pico channel")
        values = {
            "channel": self.settings.get(channel),
            "timebase": self.timebase,
            "sample_interval": self.sample_interval,
            "trigger": self.triggers.get(channel),
        }
        for name in operations:
            self.capabilities().get(name)
        return tuple(cached_reading(name, channel, values) for name in operations)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        cap = self.capabilities().get(command.operation)
        values = cap.validate(command.parameters)
        if command.channel not in (1, 2, 3, 4):
            raise ValidationError("Invalid Pico channel")
        if not cap.writable:
            raise Unsupported("Use acquire for waveform capture")
        try:
            if command.operation == "channel":
                if values["enabled"]:
                    input_range = float(values["range"])
                    if values["coupling"] == "DC50" and input_range > 5:
                        raise ValidationError("6404E 50-ohm input only supports ranges up to 5 V")
                    offset_limit = 1.25 if input_range <= 1 else 20
                    if abs(values["offset"]) > offset_limit:
                        raise ValidationError(f"Offset must be within +/-{offset_limit} V for this range")
                    self.call(
                        "SetChannelOn",
                        [U32, U32, U32, F64, U32],
                        command.channel - 1,
                        {"AC": 0, "DC": 1, "DC50": 50}[values["coupling"]],
                        RANGES.index(float(values["range"])),
                        values["offset"],
                        0,
                    )
                else:
                    self.call("SetChannelOff", [U32], command.channel - 1)
                self.settings[command.channel] = values
            elif command.operation == "timebase":
                self.timebase = values["value"]
            elif command.operation == "sample_interval":
                self.sample_interval = values["value"]
            elif command.operation == "trigger":
                channel = self.settings.get(command.channel)
                if not channel or not channel["enabled"]:
                    raise ValidationError("Configure and enable the trigger channel first")
                low, high = I16(), I16()
                self.call("GetAdcLimits", [U32, PTR, PTR], 0, ct.byref(low), ct.byref(high))
                threshold = round(
                    (values["threshold"] + channel["offset"]) / float(channel["range"]) * high.value
                )
                if not low.value <= threshold <= high.value:
                    raise ValidationError("Trigger threshold exceeds channel range")
                self.call(
                    "SetSimpleTrigger",
                    [I16, U32, I16, U32, U64, U32],
                    int(values["enabled"]),
                    command.channel - 1,
                    threshold,
                    2 if values["direction"] == "RISING" else 3,
                    0,
                    values["timeout_us"],
                )
                self.triggers[command.channel] = values
            return ApplyResult("success", command.operation, values, completed_steps=1)
        except DeviceError as exc:
            if isinstance(exc, ValidationError):
                raise
            return ApplyResult("uncertain", command.operation, values, error=str(exc))

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind not in {"waveform", "streaming"}:
            raise Unsupported("Pico supports waveform (block) and streaming batches")
        if not request.initiate or not request.confirmed:
            raise DeviceError(
                "Pico capture requires explicit initiate and confirmation", "confirmation_required"
            )
        if self.identity is None:
            raise DeviceError("Identify device first", "identity_required")
        for channel in request.channels:
            if channel not in self.settings or not self.settings[channel]["enabled"]:
                raise ValidationError("Explicitly configure each requested channel before capture")
        if self.buffers:
            raise DeviceError("Previous SDK cleanup failed; reconnect before capture", "resource_busy")
        deadline = time.monotonic() + read_timeout(self.config.timeout)
        overflow = 0
        try:
            # Keep every registered ctypes buffer alive until Stop and deregistration complete.
            self.buffers = {channel: (I16 * request.points)() for channel in request.channels}
            for index, (channel, buffer) in enumerate(self.buffers.items()):
                self.call(
                    "SetDataBuffer",
                    [U32, PTR, I32, U32, U64, U32, U32],
                    channel - 1,
                    buffer,
                    request.points,
                    1,
                    0,
                    RAW,
                    3 if index == 0 else 2,
                )
            low, high = I16(), I16()
            self.call("GetAdcLimits", [U32, PTR, PTR], 0, ct.byref(low), ct.byref(high))
            if high.value <= 0:
                raise DeviceError("Invalid ADC limits", "sdk_error")
            if request.kind == "waveform":
                interval, maximum, indisposed = F64(), U64(), F64()
                self.call(
                    "GetTimebase",
                    [U32, U64, PTR, PTR, U64],
                    self.timebase,
                    request.points,
                    ct.byref(interval),
                    ct.byref(maximum),
                    0,
                )
                if request.points > maximum.value:
                    raise ValidationError("Capture exceeds available memory")
                self.call(
                    "RunBlock",
                    [U64, U64, U32, PTR, U64, PTR, PTR],
                    0,
                    request.points,
                    self.timebase,
                    ct.byref(indisposed),
                    0,
                    None,
                    None,
                )
                ready = I16()
                while not ready.value:
                    self.call("IsReady", [PTR], ct.byref(ready))
                    if time.monotonic() > deadline:
                        raise DeviceError("Pico block timed out", "timeout")
                    time.sleep(0.002)
                count, over = U64(request.points), I16()
                self.call(
                    "GetValues", [U64, PTR, U64, U32, U64, PTR], 0, ct.byref(count), 1, RAW, 0, ct.byref(over)
                )
                total, dt, overflow = count.value, interval.value * 1e-9, over.value
                if not 0 < total <= request.points or dt <= 0:
                    raise DeviceError("Invalid block sample count/interval", "parse_error")
                data = {
                    channel: np.ctypeslib.as_array(buffer)[:total].copy()
                    for channel, buffer in self.buffers.items()
                }
            else:
                interval = F64(self.sample_interval)
                self.call(
                    "RunStreaming",
                    [PTR, U32, U64, U64, I16, U64, U32],
                    ct.byref(interval),
                    5,
                    0,
                    request.points,
                    1,
                    1,
                    RAW,
                )
                data = {channel: np.empty(request.points, dtype=np.int16) for channel in request.channels}
                counts = dict.fromkeys(request.channels, 0)
                infos = (StreamingInfo * len(request.channels))()
                trigger = TriggerInfo()
                while min(counts.values()) < request.points:
                    for index, channel in enumerate(request.channels):
                        infos[index].channel, infos[index].mode, infos[index].type = channel - 1, RAW, 1
                        infos[index].count = 0
                    self.call(
                        "GetStreamingLatestValues", [PTR, U64, PTR], infos, len(infos), ct.byref(trigger)
                    )
                    for info in infos:
                        channel = info.channel + 1
                        count = info.count
                        if (
                            channel not in data
                            or count < 0
                            or info.start < 0
                            or info.start + count > request.points
                        ):
                            raise DeviceError("Invalid SDK streaming buffer range", "parse_error")
                        count = min(count, request.points - counts[channel])
                        data[channel][counts[channel] : counts[channel] + count] = np.ctypeslib.as_array(
                            self.buffers[channel]
                        )[info.start : info.start + count]
                        counts[channel] += count
                        if info.overflow:
                            overflow |= 1 << (channel - 1)
                    if time.monotonic() > deadline:
                        raise DeviceError("Pico streaming batch timed out", "timeout")
                    time.sleep(0.002)
                total, dt = request.points, interval.value
            measurements = tuple(
                Measurement(
                    "voltage",
                    "V",
                    values.astype(float) * float(self.settings[channel]["range"]) / high.value
                    - self.settings[channel]["offset"],
                    channel,
                    np.arange(total) * dt,
                    "s",
                    quality=Quality.OVERLOAD if overflow & (1 << (channel - 1)) else Quality.GOOD,
                )
                for channel, values in data.items()
            )
            return MeasurementBatch(
                self.identity,
                measurements,
                {
                    "channels": self.settings,
                    "timebase": self.timebase,
                    "sample_interval_s": dt,
                    "capture_mode": request.kind,
                },
            )
        finally:
            self.call("Stop", [])
            for channel in self.buffers:
                self.call(
                    "SetDataBuffer", [U32, PTR, I32, U32, U64, U32, U32], channel - 1, None, 0, 1, 0, RAW, 1
                )
            self.buffers.clear()

    def close(self) -> None:
        try:
            if self.opened:
                try:
                    self.call("Stop", [])
                finally:
                    self.call("CloseUnit", [])
                    self.opened = False
                    self.buffers.clear()
        finally:
            self.sdk.close()


def enumerate_pico(config: ConnectionConfig) -> tuple[str, ...]:
    sdk = VendorLibrary(config.sdk_path)
    try:
        count, length, serials = I16(), I16(4096), ct.create_string_buffer(4096)
        result = sdk.function("ps6000aEnumerateUnits", [PTR, PTR, PTR], U32)(
            ct.byref(count), serials, ct.byref(length)
        )
        if result:
            raise DeviceError(f"Pico enumeration returned {result:#x}", "sdk_error")
        return tuple(
            "PICO::" + serial.strip() for serial in serials.value.decode().split(",") if serial.strip()
        )
    finally:
        sdk.close()
