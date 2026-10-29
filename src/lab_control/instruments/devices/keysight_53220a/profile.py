"""53220-90001 edition 4 (2023), chapters 3, 4, 5 and 7."""

from dataclasses import replace
from typing import Any, Callable

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, parameter, read_command

from .protocol import boolean, level, numeric, token

SOURCE = "https://www.keysight.com/us/en/assets/9018-03351/user-manuals/9018-03351.pdf"
INPUT_SETTINGS = (
    "coupling",
    "input_impedance",
    "input_range",
    "auto_level",
    "trigger_level",
    "input_slope",
    "input_filter",
)
WRITABLE = ("gate_time", *INPUT_SETTINGS)


def profile() -> Profile:
    commands = {
        "gate_time": command("gate_time", "SENS:FREQ:GATE:TIME", parameter("s", 0.0001, 1000), scoped=False),
        "coupling": command("coupling", "INP{ch}:COUP", enum("AC", "DC")),
        "input_impedance": command(
            "input_impedance",
            "INP{ch}:IMP",
            Parameter(unit="Ω", minimum=50, maximum=1000000, choices=("50", "1000000")),
        ),
        "input_range": command(
            "input_range",
            "INP{ch}:RANG",
            Parameter(unit="V", minimum=5, maximum=500, choices=("5", "50", "500")),
        ),
        "auto_level": command("auto_level", "INP{ch}:LEV:AUTO", Parameter("boolean")),
        "trigger_level": command("trigger_level", "INP{ch}:LEV", parameter("V", -512.5, 512.5)),
        "input_slope": command("input_slope", "INP{ch}:SLOP", enum("POS", "NEG")),
        "input_filter": command("input_filter", "INP{ch}:FILT", Parameter("boolean")),
    }
    for name, query, scoped in (
        ("configuration", "SENS:FUNC?", False),
        ("options", "*OPT?", False),
        ("probe_factor", "INP{ch}:PROB?", True),
        ("input_protection", "INP{ch}:PROT?", True),
        ("reference_clock", "SENS:ROSC:SOUR?", False),
        ("transfer_format", "FORM:DATA?", False),
        ("byte_order", "FORM:BORD?", False),
    ):
        commands[name] = read_command(name, query, scoped=scoped)
    for name, spec in tuple(commands.items()):
        decode: Callable[[bytes], Any] = token
        if name in {"gate_time", "input_impedance", "input_range", "probe_factor"}:
            decode = numeric
        elif name in {"auto_level", "input_filter", "input_protection"}:
            decode = boolean
        elif name == "trigger_level":
            decode = level
        commands[name] = replace(spec, decode=decode)
    return Profile(
        "keysight.53220a",
        ("Keysight", "Agilent"),
        ("53220A",),
        "frequency_counter",
        (1, 2),
        SOURCE,
        "53220-90001 edition 4, April 2023; CH1/CH2 single frequency",
        commands,
        (
            "configuration",
            "gate_time",
            *INPUT_SETTINGS,
            "probe_factor",
            "input_protection",
            "reference_clock",
        ),
        acquisition="scalar:frequency",
    )
