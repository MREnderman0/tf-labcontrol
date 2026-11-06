from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    Capability,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    Measurement,
    Parameter,
    ValidationError,
)
from lab_control.instruments.base import CommandSpec, Profile, ScpiAdapter, command, number, read_command
from lab_control.instruments.common.scalar import acquire_power

ADAPTER_ID = "siglent.sps5000x"
MANUAL = "https://int.siglent.com/u_file/download/22_06_28/SPS5000X_Usermanual_E02B.pdf"
RESOLUTION = 0.001
SETPOINTS = {"voltage": ("VOLTAGE", "V"), "current": ("CURRENT", "A")}


@dataclass(frozen=True)
class Rating:
    channels: int
    voltage: float
    current: float
    power: float  # Per physical channel, in independent mode.


RATINGS = {
    "SPS5041X": Rating(1, 40, 30, 360),
    "SPS5042X": Rating(1, 40, 60, 720),
    "SPS5043X": Rating(1, 40, 90, 1080),
    "SPS5044X": Rating(2, 40, 30, 360),
    "SPS5045X": Rating(3, 40, 30, 360),
    "SPS5051X": Rating(1, 50, 10, 180),
    "SPS5081X": Rating(1, 80, 15, 360),
    "SPS5082X": Rating(1, 80, 30, 720),
    "SPS5083X": Rating(1, 80, 45, 1080),
    "SPS5084X": Rating(2, 80, 15, 360),
    "SPS5085X": Rating(3, 80, 15, 360),
    "SPS5161X": Rating(1, 160, 7.5, 360),
    "SPS5162X": Rating(1, 160, 15, 720),
    "SPS5163X": Rating(1, 160, 22.5, 1080),
    "SPS5164X": Rating(2, 160, 7.5, 360),
    "SPS5165X": Rating(3, 160, 7.5, 360),
}


def numeric_reply(raw: bytes) -> float:
    try:
        return number(raw)
    except UnicodeError as exc:
        raise DeviceError("Invalid SPS5000X numeric response encoding", "parse_error") from exc


def switch_reply(raw: bytes) -> bool:
    value = raw.strip().upper()
    if value in {b"1", b"ON"}:
        return True
    if value in {b"0", b"OFF"}:
        return False
    raise DeviceError(f"Invalid SPS5000X switch response: {raw[:80]!r}", "parse_error")


def sps5000x() -> Profile:
    commands = {}
    for name, (mnemonic, unit) in SETPOINTS.items():
        path = f":SOURCE:{mnemonic}:SET"
        commands[name] = CommandSpec(
            Capability(
                name,
                {"value": Parameter(unit=unit, minimum=0, resolution=RESOLUTION)},
                readable=True,
                writable=True,
                channel_scoped=True,
                section=path,
            ),
            (f"{path} CH{{ch}},{{value}}",),
            f"{path}? CH{{ch}}",
            decode=numeric_reply,
        )
    for name, mnemonic in (
        ("measured_voltage", "VOLTAGE"),
        ("measured_current", "CURRENT"),
        ("measured_power", "POWER"),
    ):
        spec = read_command(name, f"MEASURE:{mnemonic}? CH{{ch}}")
        commands[name] = replace(spec, decode=numeric_reply)
    output = command("all_outputs", "OUTPUT", Parameter("boolean"), scoped=False, confirm=True)
    commands["all_outputs"] = replace(output, decode=switch_reply)
    return Profile(
        ADAPTER_ID,
        ("SIGLENT",),
        tuple(RATINGS),
        "power_supply",
        (1,),  # Metadata placeholder. Exact physical channels require *IDN?.
        MANUAL,
        "SPS5000X User Manual E02B, Remote Control",
        commands,
        ("voltage", "current", "all_outputs"),
        error_query="",
        acquisition="power",
    )


def rated_profile(profile: Profile, model: str) -> Profile:
    rating = RATINGS[model]
    commands = dict(profile.commands)
    for name, maximum in (("voltage", rating.voltage), ("current", rating.current)):
        spec = commands[name]
        parameter = replace(spec.capability.parameters["value"], maximum=maximum)
        commands[name] = replace(spec, capability=replace(spec.capability, parameters={"value": parameter}))
    return replace(profile, channels=tuple(range(1, rating.channels + 1)), commands=commands)


class Sps5000xAdapter(ScpiAdapter):
    def identify(self) -> DeviceIdentity:
        previous = self.identity
        self.identity = None
        self.capability_details = {}
        try:
            identity = super().identify()
            model = identity.model.upper()
            if (
                model not in RATINGS
                or identity.key is None
                or (self.config.expected_serial and identity.serial != self.config.expected_serial)
                or (previous and identity.key != previous.key)
            ):
                raise DeviceError("SPS5000X 型号或序列号无法核验", "identity_conflict")
        except Exception:
            self.identity = None
            raise
        self.profile = rated_profile(self.profile, model)
        self.capability_details = {"rated_power_per_channel_w": RATINGS[model].power}
        return identity

    def read(self, operation: str, channel: int = 1) -> Any:
        if self.identity is None:
            raise DeviceError("请先核验 SPS5000X 完整身份", "identity_required")
        if operation == "all_outputs" and channel != 1:
            raise ValidationError("整机输出请使用 channel=1")
        return super().read(operation, channel)

    def validate_command(self, cmd: DeviceCommand) -> tuple[CommandSpec, dict[str, Any]]:
        if self.identity is None:
            raise DeviceError("请先核验 SPS5000X 完整身份", "identity_required")
        spec, values = super().validate_command(cmd)
        if cmd.operation in SETPOINTS and not math.isclose(
            values["value"], round(values["value"], 3), rel_tol=0, abs_tol=1e-9
        ):
            raise ValidationError("SPS5000X 设置分辨率为 1 mV / 1 mA")
        return spec, values

    def require_independent(self, channel: int) -> None:
        role = self.transport.query(f"SYSTEM:DEVICE:ROLE? CH{channel}").strip().upper()
        if role != b"M_INDEPEN":
            raise ValidationError(f"CH{channel} 必须处于独立模式，当前角色 {role!r}")

    def require_normal(self, channel: int) -> None:
        if switch_reply(self.transport.query(f"LIST? CH{channel}")):
            raise ValidationError(f"CH{channel} 处于 List 模式")

    def require_internal_control(self, operation: str, channel: int) -> None:
        # CURR is also the short form in the manual's CURRage spelling.
        mnemonic = "VOLT" if operation == "voltage" else "CURR"
        mode = self.transport.query(f":SOURCE:CTRL:{mnemonic}:MODE? CH{channel}").strip().upper()
        if mode != b"LOCAL":
            raise ValidationError(f"CH{channel} {operation} 必须使用内部设定，当前模式 {mode!r}")

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        if cmd.operation in SETPOINTS:
            self.require_independent(1)
            if cmd.channel != 1:
                self.require_independent(cmd.channel)
            self.require_normal(cmd.channel)
            self.require_internal_control(cmd.operation, cmd.channel)
        elif cmd.operation == "all_outputs" and parameters["value"]:
            for channel in self.profile.channels:
                self.require_independent(channel)
                self.require_normal(channel)
                if not switch_reply(self.transport.query(f"OUTPUT:LOCAL:ENABLE? CH{channel}")):
                    raise ValidationError(f"CH{channel} 输出由外部接口控制")
                for operation in SETPOINTS:
                    self.require_internal_control(operation, channel)
                    value = self.read(operation, channel)
                    # Check the actual settings, including any stricter experiment limits.
                    self.validate_command(DeviceCommand(operation, {"value": value}, channel))

    def encode(
        self, operation: str, spec: CommandSpec, context: dict[str, Any], parameters: dict[str, Any]
    ) -> tuple[bytes | str, ...]:
        if operation in SETPOINTS:
            context = {**context, "value": f"{parameters['value']:.3f}"}
        return super().encode(operation, spec, context, parameters)

    def complete_setting(self, command: DeviceCommand) -> None:
        if self.transport.query("*OPC?").strip() != b"1":
            raise DeviceError("SPS5000X 未确认命令完成", "parse_error")

    def acquire_measurements(self, request: AcquisitionRequest) -> tuple[Measurement, ...]:
        return acquire_power(self, request)
