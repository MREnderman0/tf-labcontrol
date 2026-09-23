from __future__ import annotations

from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import Capability, DeviceCommand, DeviceError, Parameter, ValidationError
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
KEITHLEY_SOURCE = "https://download.tek.com/manual/077100401_Referencemanual.pdf"
SPD_SOURCE = "https://www.siglent.eu/_downloads/140e4cc86e0498634759dcffa81aab7e"
GPD_SOURCE = "https://www.gwinstek.com/en-US/products/downloadSeriesDownNew/7840/1477"
ITECH_SOURCE = "https://usa.itechate.com/uploadfiles/%E7%94%A8%E6%88%B7%E6%89%8B%E5%86%8C/user%20manual/it6300/IT6300%20Programming%20Guide-EN.pdf"


def keithley() -> Profile:
    select = ("INST:NSEL {ch}",)
    commands = {}
    for name, path, param, confirm in (
        ("voltage", "VOLT", parameter("V", 0, 30), False),
        ("current", "CURR", parameter("A", 0, 3), False),
        ("output", "CHAN:OUTP", BOOL, True),
        ("voltage_limit", "VOLT:LIM", parameter("V", 0, 30), False),
        ("voltage_limit_enabled", "VOLT:LIM:STAT", BOOL, True),
        ("timer", "OUTP:TIM", BOOL, True),
        ("timer_delay", "OUTP:TIM:DEL", parameter("s", 0.1, 99999.9), False),
        ("voltage_step", "VOLT:STEP", parameter("V", 0, 30), False),
        ("current_step", "CURR:STEP", parameter("A", 0, 3), False),
    ):
        commands[name] = command(name, path, param, select=select, confirm=confirm)
    commands["all_outputs"] = command("all_outputs", "OUTP", BOOL, scoped=False, confirm=True)
    commands["coupling"] = CommandSpec(
        Capability(
            "coupling",
            {"value": enum("SER", "PAR", "TRAC", "OFF")},
            writable=True,
            requires_confirmation=True,
            section="INSTrument:COMbine",
        ),
        ("INST:COM:{value}",),
        compare=False,
    )
    for name, path in (
        ("measured_voltage", "MEAS:VOLT?"),
        ("measured_current", "MEAS:CURR?"),
        ("measured_power", "MEAS:POW?"),
    ):
        commands[name] = replace(read_command(name, path), select=select)
    commands["protection"] = read_command("protection", "STAT:QUES:COND?", scoped=False)
    return Profile(
        "keithley.2231a",
        ("KEITHLEY", "TEKTRONIX"),
        ("2231A-30-3",),
        "power_supply",
        (1, 2, 3),
        KEITHLEY_SOURCE,
        "077100401 September 2014, section 5",
        commands,
        ("voltage", "current", "output", "protection"),
        acquisition="power",
        channel_limits={3: {"voltage": 5, "voltage_limit": 5}},
    )


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


def gpd() -> Profile:
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
        )
    for name, path in (("measured_voltage", "VOUT"), ("measured_current", "IOUT")):
        commands[name] = read_command(name, path + "{ch}?")
    commands["all_outputs"] = CommandSpec(
        Capability(
            "all_outputs", {"value": BOOL}, writable=True, requires_confirmation=True, section="OUT<Boolean>"
        ),
        ("OUT{value}",),
        compare=False,
    )
    commands["tracking"] = CommandSpec(
        Capability(
            "tracking",
            {"value": enum("0", "1", "2")},
            writable=True,
            requires_confirmation=True,
            section="TRACK<X>",
        ),
        ("TRACK{value}",),
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
        GPD_SOURCE,
        "GPD-X303S user manual, Remote Control",
        commands,
        ("voltage", "current", "status_register"),
        error_query="",
        acquisition="power",
        channel_limits={3: {"voltage": 5}, 4: {"voltage": 5, "current": 1}},
    )


class PowerAdapter(ScpiAdapter):
    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        super().preflight(cmd, parameters)
        if cmd.operation == "list_upload" and self.read("list_run", cmd.channel) not in (0, "0", "OFF"):
            raise ValidationError("Stop the channel list explicitly before replacing its steps")

    def read(self, operation: str, channel: int = 1) -> Any:
        if self.adapter_id == "gwinstek.gpd4303s" and operation == "status_register":
            fixed = getattr(self.transport, "query_fixed", None)
            raw = fixed("STATUS?", 1) if fixed else self.transport.query("STATUS?")
            if len(raw) != 1:
                raise DeviceError("GPD STATUS? must return one status byte", "parse_error")
            value = raw[0]
            return {
                "raw": value,
                "ch1_mode": "CV" if value & 1 else "CC",
                "ch2_mode": "CV" if value & 2 else "CC",
                "tracking_bits": (value >> 2) & 3,
                "output_enabled": bool(value & 32),
                "baud_bits": (value >> 6) & 3,
            }
        return super().read(operation, channel)

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
        if self.adapter_id == "gwinstek.gpd4303s" and operation == "all_outputs":
            return (f"OUT{int(parameters['value'])}",)
        return super().encode(operation, spec, context, parameters)


def it6322() -> Profile:
    source = "https://jdingtech.com.tw/Official/sharefile/ITECH_Protocol_IT6322_V1.0.0.1.pdf"
    select = ("INST:NSEL {ch}",)
    commands = {}
    for name, path, param, confirm in (
        ("voltage", "VOLT", parameter("V", 0, 30), False),
        ("current", "CURR", parameter("A", 0, 3), False),
        ("voltage_limit", "VOLT:PROT", parameter("V", 0, 30), False),
        ("output", "OUTP", BOOL, True),
        ("timer_delay", "OUTP:TIM:DATA", parameter("s", 0, 999999), False),
    ):
        commands[name] = command(name, path, param, select=select, confirm=confirm)
    for name, path in (
        ("measured_voltage", "MEAS:VOLT?"),
        ("measured_current", "MEAS:CURR?"),
        ("measured_power", "MEAS:POW?"),
    ):
        commands[name] = replace(read_command(name, path), select=select)
    return Profile(
        "itech.it6322",
        ("ITECH",),
        ("IT6322",),
        "power_supply",
        (1, 2, 3),
        source,
        "Manufacturer manual IT6322-402205 V2.2 (2018-03-30), distributor-hosted copy, chapters 3-8",
        commands,
        ("voltage", "current", "output"),
        acquisition="power",
        channel_limits={3: {"voltage": 5, "voltage_limit": 5}},
    )


PROFILES = (keithley(), spd(), gpd(), it6322())
