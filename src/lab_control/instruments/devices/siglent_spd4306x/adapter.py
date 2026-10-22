from __future__ import annotations

from typing import Any

from lab_control.domain.instruments import Capability, DeviceCommand, Parameter, ValidationError
from lab_control.instruments.base import (
    CommandSpec,
    Profile,
    ScpiAdapter,
    command,
    enum,
    parameter,
    read_command,
)

BOOL = Parameter("boolean")
SPD_SOURCE = "https://www.siglent.eu/_downloads/140e4cc86e0498634759dcffa81aab7e"


def spd() -> Profile:
    commands = {}
    for name, path, param, confirm in (
        ("voltage", "VOLT", parameter("V", 0, 30), False),
        ("current", "CURR", parameter("A", 0, 6), False),
        ("ovp", "OVP", parameter("V", 0, 33), False),
        ("ocp", "OCP", parameter("A", 0, 6.6), False),
        ("ovp_enabled", "OVP:STAT", BOOL, True),
        ("ocp_enabled", "OCP:STAT", BOOL, True),
        ("ocp_delay", "OCP:DEL", parameter("s", 0.001, 10), False),
        ("output", "OUTP", BOOL, True),
        ("output_on_delay", "OUTP:ON:DEL", parameter("s", 0, 3600), False),
        ("output_off_delay", "OUTP:OFF:DEL", parameter("s", 0, 3600), False),
        ("list_cycles", "LIST:CYCL", Parameter("integer", minimum=1, maximum=99999), False),
        ("list_continuous", "LIST:CONT", BOOL, True),
        ("list_run", "LIST:RUN", BOOL, True),
    ):
        query_path = path + ":SET" if name in {"voltage", "current"} else path
        commands[name] = CommandSpec(
            Capability(
                name,
                {"value": param},
                readable=True,
                writable=True,
                requires_confirmation=confirm,
                channel_scoped=True,
                section="10.4 SOURce " + path,
            ),
            (f"{path} CH{{ch}},{{value}}",),
            f"{query_path}? CH{{ch}}",
        )
    commands["all_outputs"] = command("all_outputs", "OUTP:ALL", BOOL, scoped=False, confirm=True)
    commands["tracking"] = command(
        "tracking", "OUTP:TRACK", Parameter("integer", minimum=0, maximum=2), scoped=False, confirm=True
    )
    commands["sense_mode"] = CommandSpec(
        Capability(
            "sense_mode",
            {"value": enum("2W", "4W")},
            readable=True,
            writable=True,
            channel_scoped=True,
            requires_confirmation=True,
            section="10.4 MODE (CH2/CH3)",
        ),
        ("MODE CH{ch},{value}",),
        "MODE? CH{ch}",
        decode=lambda raw: {"0": "2W", "1": "4W"}.get(raw.decode().strip(), raw.decode().strip()),
    )
    for name, query in (
        ("measured_voltage", "MEAS:VOLT?"),
        ("measured_current", "MEAS:CURR?"),
        ("measured_power", "MEAS:POW?"),
        ("ovp_tripped", "OVP:PROT:STAT?"),
        ("ocp_tripped", "OCP:PROT:STAT?"),
    ):
        commands[name] = read_command(name, query + " CH{ch}")
    commands["list_upload"] = CommandSpec(
        Capability(
            "list_upload",
            {
                "voltage": Parameter("array", minimum=0, maximum=30),
                "current": Parameter("array", minimum=0, maximum=6),
                "duration": Parameter("array", minimum=0.001, maximum=99999),
            },
            writable=True,
            requires_confirmation=True,
            channel_scoped=True,
            section="10.4 LIST subsystem",
        )
    )
    return Profile(
        "siglent.spd4306x",
        ("SIGLENT",),
        ("SPD4306X",),
        "power_supply",
        (1, 2, 3, 4),
        SPD_SOURCE,
        "SPD4000X user/programming manual EN01B, chapter 10",
        commands,
        ("voltage", "current", "output", "ovp_tripped", "ocp_tripped"),
        error_query="",
        acquisition="power",
        channel_limits={
            1: {"voltage": 6, "current": 3, "ovp": 6.6, "ocp": 3.3},
            4: {"voltage": 6, "current": 3, "ovp": 6.6, "ocp": 3.3},
        },
    )


class Spd4306xAdapter(ScpiAdapter):
    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        super().preflight(cmd, parameters)
        if cmd.operation == "list_upload" and self.read("list_run", cmd.channel) not in (0, "0", "OFF"):
            raise ValidationError("Stop the channel list explicitly before replacing its steps")

    def validate_command(self, cmd: DeviceCommand) -> tuple[CommandSpec, dict[str, Any]]:
        spec, values = super().validate_command(cmd)
        if cmd.operation == "sense_mode" and cmd.channel not in (2, 3):
            raise ValidationError("SPD4306X remote sense is available on CH2/CH3 only")
        if cmd.operation == "list_upload":
            if not 1 <= len(values["voltage"]) <= 100 or len({len(v) for v in values.values()}) != 1:
                raise ValidationError("Lists must have equal length, 1..100 steps")
            for name in ("voltage", "current"):
                maximum = self.profile.channel_limits.get(cmd.channel, {}).get(name, float("inf"))
                maximum = min(maximum, self.config.limits.get(name, float("inf")))
                if max(values[name]) > maximum:
                    raise ValidationError(f"{name} list exceeds channel/safety limit")
        return spec, values

    def encode(
        self, operation: str, spec: CommandSpec, context: dict[str, Any], parameters: dict[str, Any]
    ) -> tuple[bytes | str, ...]:
        if operation == "list_upload":
            return (f"LIST:CLE CH{context['ch']}",) + tuple(
                f"LIST:{path} CH{context['ch']}," + ",".join(f"{v:.12g}" for v in parameters[key])
                for key, path in (("voltage", "VOLT"), ("current", "CURR"), ("duration", "TIME"))
            )
        return super().encode(operation, spec, context, parameters)

    def acquire_measurements(self, request):
        from lab_control.instruments.common.scalar import acquire_power

        return acquire_power(self, request)
