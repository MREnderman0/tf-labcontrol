"""Exact DHO924 registration and supported analog controls."""

from dataclasses import replace

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, parameter, read_command

from .protocol import boolean, integer, numeric, token

SOURCE = (
    "https://download.rigol.com/en/Manual/Digital%20Oscilloscope/DHO800/DHO800900_ProgrammingGuide_EN.pdf"
)
DEPTHS = (1000, 10000, 100000, 1000000, 5000000, 10000000)
MAIN_TIMEBASE_MAX = 0.02  # Last 1-2-5 step below the 50 ms/div Auto ROLL threshold.
CHANNEL_SETTINGS = (
    "channel_enabled",
    "scale",
    "offset",
    "coupling",
    "probe_attenuation",
    "channel_unit",
    "invert",
    "vernier",
)
GLOBAL_SETTINGS = (
    "timebase",
    "time_position",
    "record_length",
    "sample_rate",
    "trigger_type",
    "trigger_source",
    "trigger_level",
    "trigger_slope",
    "trigger_sweep",
    "trigger_holdoff",
    "acquisition_type",
    "averages",
    "timebase_mode",
    "roll_enabled",
    "xy_enabled",
    "delay_enabled",
)
TRANSFER_SETTINGS = (
    "transfer_format",
    "transfer_mode",
    "transfer_source",
    "transfer_start",
    "transfer_stop",
    "transfer_points",
)


def profile() -> Profile:
    commands = {}
    auto = command("auto_setup", ":AUToset", read=False, scoped=False)
    commands["auto_setup"] = replace(
        auto, capability=replace(auto.capability, verification="documented_pending_hardware")
    )
    for name, path, spec, scoped in (
        ("channel_enabled", ":CHAN{ch}:DISP", Parameter("boolean"), True),
        ("scale", ":CHAN{ch}:SCAL", parameter("V/div", 0.0000002, 500000), True),
        ("offset", ":CHAN{ch}:OFFS", parameter("V", -5000000, 5000000), True),
        ("coupling", ":CHAN{ch}:COUP", enum("AC", "DC", "GND"), True),
        (
            "probe_attenuation",
            ":CHAN{ch}:PROB",
            Parameter(
                "number",
                "ratio",
                0.001,
                50000,
                choices=tuple(str(n * 10.0**e) for e in range(-3, 5) for n in (1, 2, 5)),
            ),
            True,
        ),
        (
            "timebase",
            ":TIM:MAIN:SCAL",
            replace(parameter("s/div", 2e-9, MAIN_TIMEBASE_MAX), range_source="software"),
            False,
        ),
        ("time_position", ":TIM:MAIN:OFFS", parameter("s", -5000, 5000), False),
        (
            "record_length",
            ":ACQ:MDEP",
            Parameter("integer", "point", DEPTHS[0], DEPTHS[-1], choices=tuple(map(str, DEPTHS))),
            False,
        ),
        ("trigger_source", ":TRIG:EDGE:SOUR", enum("CHAN1", "CHAN2", "CHAN3", "CHAN4"), False),
        ("trigger_level", ":TRIG:EDGE:LEV", parameter("V", -1e7, 1e7), False),
        ("trigger_slope", ":TRIG:EDGE:SLOP", enum("POS", "NEG", "RFAL"), False),
        ("trigger_mode", ":TRIG:SWE", enum("AUTO", "NORM", "SINGLE", "STOP"), False),
    ):
        decode = (
            boolean
            if spec.kind == "boolean"
            else token
            if spec.kind == "enum"
            else integer
            if spec.kind == "integer"
            else numeric
        )
        commands[name] = replace(command(name, path, spec, scoped=scoped), decode=decode, compare=False)
    for name, path, decode, scoped in (
        ("sample_rate", ":ACQ:SRAT?", numeric, False),
        ("channel_unit", ":CHAN{ch}:UNIT?", token, True),
        ("invert", ":CHAN{ch}:INV?", boolean, True),
        ("vernier", ":CHAN{ch}:VERN?", boolean, True),
        ("trigger_type", ":TRIG:MODE?", token, False),
        ("trigger_sweep", ":TRIG:SWE?", token, False),
        ("trigger_status", ":TRIG:STAT?", token, False),
        ("trigger_holdoff", ":TRIG:HOLD?", numeric, False),
        ("acquisition_type", ":ACQ:TYPE?", token, False),
        ("averages", ":ACQ:AVER?", integer, False),
        ("timebase_mode", ":TIM:MODE?", token, False),
        ("roll_enabled", ":TIM:ROLL?", boolean, False),
        ("xy_enabled", ":TIM:XY:ENAB?", boolean, False),
        ("delay_enabled", ":TIM:DEL:ENAB?", boolean, False),
        ("transfer_format", ":WAV:FORM?", token, False),
        ("transfer_mode", ":WAV:MODE?", token, False),
        ("transfer_source", ":WAV:SOUR?", token, False),
        ("transfer_start", ":WAV:STAR?", integer, False),
        ("transfer_stop", ":WAV:STOP?", integer, False),
        ("transfer_points", ":WAV:POIN?", integer, False),
    ):
        commands[name] = replace(read_command(name, path, scoped=scoped), decode=decode)
    for name, path in (("run", ":RUN"), ("stop", ":STOP"), ("single", ":SING")):
        commands[name] = command(name, path, read=False, scoped=False)
    return Profile(
        "rigol.dho924",
        ("RIGOL",),
        ("DHO924",),
        "oscilloscope",
        (1, 2, 3, 4),
        SOURCE,
        "DHO800900 Programming Guide PGA39106-1110: 3.3, 3.6, 3.26-3.28",
        commands,
        ("channel_enabled", "scale", "offset", "timebase", "record_length"),
        error_query=":SYST:ERR?",
        acquisition="dho924",
    )
