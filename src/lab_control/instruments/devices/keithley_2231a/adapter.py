from __future__ import annotations

from dataclasses import replace

from lab_control.domain.instruments import Capability, Parameter
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


class Keithley2231aAdapter(ScpiAdapter):
    """keithley.2231a control and scalar measurements."""

    def acquire_measurements(self, request):
        from lab_control.instruments.common.scalar import acquire_power

        return acquire_power(self, request)
