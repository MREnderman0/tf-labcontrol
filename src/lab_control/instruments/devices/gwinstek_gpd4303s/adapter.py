from __future__ import annotations

import math
from dataclasses import replace
from functools import partial
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    ApplyResult,
    Capability,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    Parameter,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import (
    CommandSpec,
    Profile,
    ScpiAdapter,
    command,
    enum,
    parameter,
    read_command,
)
from lab_control.transport.base import Transport

from .protocol import MANUAL, SPECIFICATIONS, PacedTransport, decode_status, numeric_readback


def gpd4303s() -> Profile:
    commands = {}
    for name, path, param in (
        ("voltage", "VSET", parameter("V", 0, 30)),
        ("current", "ISET", parameter("A", 0, 3)),
    ):
        commands[name] = CommandSpec(
            Capability(
                name,
                {"value": param},
                readable=True,
                writable=True,
                channel_scoped=True,
                section=path + "<X>",
            ),
            (path + "{ch}:{value}",),
            path + "{ch}?",
            decode=partial(numeric_readback, unit=param.unit),
        )
    for name, query in (("measured_voltage", "VOUT{ch}?"), ("measured_current", "IOUT{ch}?")):
        commands[name] = replace(
            read_command(name, query),
            decode=partial(numeric_readback, unit="V" if "voltage" in name else "A"),
        )
    for name, param, write in (
        ("all_outputs", Parameter("boolean"), "OUT{value}"),
        ("tracking", enum("0", "1", "2"), "TRACK{value}"),
    ):
        commands[name] = CommandSpec(
            Capability(
                name,
                {"value": param},
                readable=True,
                writable=True,
                requires_confirmation=True,
                channel_scoped=False,
                section=write + " / STATUS?",
            ),
            (write,),
            "STATUS?",
            compare=False,
        )
    commands["status_register"] = read_command("status_register", "STATUS?", scoped=False)
    commands["remote"] = command("remote", "REMOTE", read=False, scoped=False)
    commands["local"] = command("local", "LOCAL", read=False, scoped=False)
    commands["errors"] = read_command("errors", "ERR?", scoped=False)
    return Profile(
        "gwinstek.gpd4303s",
        ("GW INSTEK", "GW", "GOOD WILL"),
        ("GPD-4303S",),
        "power_supply",
        (1, 2, 3, 4),
        MANUAL,
        "82PD-433S0MC1, Remote Control; model rated specifications",
        commands,
        ("voltage", "current", "status_register"),
        error_query="",
        acquisition="power",
        channel_limits={3: {"voltage": 10, "current": 3}, 4: {"voltage": 5, "current": 1}},
    )


class Gpd4303sAdapter(ScpiAdapter):
    # Explicit documented remote preparation; it changes panel control, not output.
    # PacedTransport waits for the mode transition before the identity query.
    connection_commands = ("REMOTE",)

    def __init__(self, transport: Transport, profile: Profile, config: ConnectionConfig):
        super().__init__(PacedTransport(transport), profile, config)
        self.correlated: dict[str, float] = {}
        self.status: dict[str, Any] = {}
        self.local_control = False

    def identify(self) -> DeviceIdentity:
        identity = super().identify()
        self.local_control = False
        return identity

    def release_control(self) -> None:
        """Normal-session teardown only; LOCAL has no query/ack and preserves outputs."""
        if not self.local_control:
            self.transport.write("LOCAL")
            self.local_control = True
            self.correlated.clear()
            self.status.clear()

    def read(self, operation: str, channel: int = 1) -> Any:
        if self.local_control:
            raise Unsupported("设备处于本地控制，请先进入远程控制或重新连接")
        self._context(channel, {})
        if operation in {"status_register", "all_outputs", "tracking"}:
            transport = self.transport
            assert isinstance(transport, PacedTransport)
            self.status = decode_status(
                transport.query_status(), firmware=self.identity.firmware if self.identity else ""
            )
            self.capability_details = {**self.capability_details, "status": dict(self.status)}
            if operation == "status_register":
                return dict(self.status)
            value = self.status["output_enabled" if operation == "all_outputs" else "tracking"]
            if value is None:
                raise DeviceError("GPD tracking mode is unknown", "parse_error")
            return value
        value = super().read(operation, channel)
        if channel == 3 and operation in {"voltage", "current"}:
            self.correlated[operation] = float(value)
            self.capability_details = {**self.capability_details, "ch3_settings": dict(self.correlated)}
        return value

    def refresh_capabilities(self) -> None:
        if self.local_control:
            # LOCAL deliberately suspends instrument queries. Keep only static
            # metadata available; no stale electrical setting may be submitted.
            self.capability_details = {"control_mode": "local", "output_scope": "all_channels"}
            self.capability_error = ""
            return
        try:
            self.read("status_register")
            voltage = self.read("voltage", 3)
            current = self.read("current", 3)
            if not (0 <= voltage <= 10 and 0 <= current <= 3 and (voltage <= 5 or current <= 1)):
                raise DeviceError("CH3 readback is outside the documented V/I combination", "parse_error")
            self.correlated = {"voltage": voltage, "current": current}
            self.capability_error = ""
            self.capability_details = {
                "rated_range_source": SPECIFICATIONS,
                "ch3_settings": dict(self.correlated),
                "status": dict(self.status),
                "output_scope": "all_channels",
                "control_mode": "remote",
            }
        except Exception as exc:
            self.capability_error = str(exc)
            raise

    def capabilities_for_channel(self, channel: int) -> CapabilitySet:
        self._context(channel, {})
        schema = self.capabilities()
        operations = []
        for cap in schema.operations:
            if self.local_control:
                operations.append(
                    replace(
                        cap,
                        readable=False,
                        writable=cap.name == "remote",
                        reason="" if cap.name == "remote" else "本地控制，请先进入远程控制",
                    )
                )
                continue
            if cap.name in {"voltage", "current"}:
                maximum = self.profile.channel_limits.get(channel, {}).get(
                    cap.name, cap.parameters["value"].maximum
                )
                reason = ""
                if channel == 3:
                    other = "current" if cap.name == "voltage" else "voltage"
                    threshold = 1 if other == "current" else 5
                    conservative = 5 if cap.name == "voltage" else 1
                    if other not in self.correlated or self.correlated[other] > threshold:
                        maximum = conservative
                    if self.capability_error or other not in self.correlated:
                        reason = "CH3 关联设置未确认，请刷新范围"
                if channel in {1, 2}:
                    tracking = self.status.get("tracking")
                    if tracking is None:
                        reason = "跟踪模式未确认，请刷新范围"
                    elif channel == 2 and (tracking == "2" or (tracking == "1" and cap.name == "voltage")):
                        reason = "跟踪模式下由 CH1 控制"
                cap = replace(
                    cap,
                    parameters={
                        "value": replace(
                            cap.parameters["value"], maximum=maximum, range_source="model_channel"
                        )
                    },
                    writable=cap.writable and not reason,
                    reason=reason,
                )
            operations.append(cap)
        return replace(schema, operations=tuple(operations))

    def validate_command(self, cmd: DeviceCommand) -> tuple[CommandSpec, dict[str, Any]]:
        spec, parameters = super().validate_command(cmd)
        if cmd.operation in {"voltage", "current"}:
            value = parameters["value"]
            if not math.isclose(value, round(value, 3), rel_tol=0, abs_tol=1e-9):
                raise ValidationError("GPD settings require 1 mV / 1 mA resolution")
        return spec, parameters

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        if self.local_control and cmd.operation != "remote":
            raise Unsupported("设备处于本地控制，请先进入远程控制")
        if cmd.operation in {"voltage", "current"}:
            self.refresh_capabilities()
            cap = self.capabilities_for_channel(cmd.channel).get(cmd.operation)
            if not cap.writable:
                raise Unsupported(cap.reason)
            cap.validate(parameters)
        elif cmd.operation == "tracking" and self.read("all_outputs"):
            raise ValidationError("Turn the master output off before changing tracking mode")
        elif cmd.operation == "all_outputs" and parameters["value"]:
            self.read("tracking")  # Unknown mode must never enable all outputs.

    def apply(self, cmd: DeviceCommand) -> ApplyResult:
        spec, parameters = self.validate_command(cmd)
        self.preflight(cmd, parameters)
        context = self._context(cmd.channel, parameters)
        if cmd.operation == "all_outputs":
            context["value"] = int(parameters["value"])
        elif cmd.operation in {"voltage", "current"}:
            context["value"] = f"{parameters['value']:.3f}"
        completed = 0
        try:
            for payload in spec.writes:
                self.transport.write(payload.format(**context))
                completed += 1
            if cmd.operation in {"local", "remote"}:
                self.local_control = cmd.operation == "local"
                if self.local_control:
                    self.correlated.clear()
                    self.status.clear()
            actual = self.read(cmd.operation, cmd.channel) if spec.capability.readable else None
            expected = parameters.get("value")
            if expected is not None and actual is not None:
                matches = (
                    math.isclose(float(expected), float(actual), rel_tol=0, abs_tol=0.001000001)
                    if cmd.operation in {"voltage", "current"}
                    else expected == actual
                )
                if not matches:
                    return ApplyResult(
                        "rejected",
                        cmd.operation,
                        parameters,
                        actual,
                        completed,
                        "Instrument readback differs; setting was not replayed",
                    )
            self.configuration[f"{cmd.channel}.{cmd.operation}"] = parameters
            return ApplyResult("success", cmd.operation, parameters, actual, completed)
        except DeviceError as exc:
            self.capability_error = str(exc)
            return ApplyResult(
                "partial" if completed else "uncertain",
                cmd.operation,
                parameters,
                completed_steps=completed,
                error=str(exc),
            )
