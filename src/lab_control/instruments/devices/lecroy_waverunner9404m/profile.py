"""Documented commands; public operation names preserve the migration baseline."""

from dataclasses import replace
from functools import partial
from typing import Any, Callable

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, parameter, read_command

from .protocol import choice, numeric, transfer_setup, trigger_source, value_text

SOURCE = "https://cdn.teledynelecroy.com/files/manuals/maui-remote-control-and-automation-manual.pdf"
SPECS = "https://www.teledynelecroy.com/options/productdetails.aspx?categoryid=1&groupid=5&modelid=11110"


def profile() -> Profile:
    commands = {}
    commands["auto_setup"] = replace(
        command("auto_setup", "C1:ASET", read=False, scoped=False),
        capability=replace(
            command("auto_setup", "C1:ASET", read=False, scoped=False).capability,
            verification="documented_pending_hardware",
        ),
    )
    for name, path, param in (
        ("channel_enabled", "C{ch}:TRA", enum("ON", "OFF")),
        ("scale", "C{ch}:VDIV", parameter("V/div", 0.001, 10)),
        ("offset", "C{ch}:OFST", parameter("V", -10, 10)),
        ("coupling", "C{ch}:CPL", enum("A1M", "D1M", "D50", "GND")),
        ("probe_attenuation", "C{ch}:ATTN", parameter("ratio", 1, 10000)),
        ("trigger_level", "C{ch}:TRLV", parameter("V", -10, 10)),
        ("trigger_slope", "C{ch}:TRSL", enum("POS", "NEG")),
    ):
        commands[name] = command(name, path, param)
    commands["timebase"] = command("timebase", "TDIV", parameter("s/div", 1e-10, 1000), scoped=False)
    commands["time_position"] = command("time_position", "TRDL", parameter("s", -100, 100), scoped=False)
    commands["trigger_mode"] = command(
        "trigger_mode", "TRMD", enum("AUTO", "NORM", "SINGLE", "STOP"), scoped=False, confirm=True
    )
    commands["record_length"] = command(
        "record_length", "MSIZ", Parameter("integer", minimum=1000, maximum=10000000), scoped=False
    )
    for name, mode in (("single", "SINGLE"), ("run", "AUTO"), ("stop", "STOP")):
        commands[name] = command(name, "TRMD " + mode, read=False, scoped=False, confirm=True)
    for name, spec in tuple(commands.items()):
        value_parameter = spec.capability.parameters.get("value")
        decode: Callable[[bytes], Any] = (
            partial(choice, choices=value_parameter.choices)
            if value_parameter and value_parameter.kind == "enum"
            else numeric
        )
        commands[name] = replace(spec, decode=decode)
    for name, query, decode in (
        ("transfer_format", "CFMT?", partial(choice, choices=("DEF9,WORD,BIN", "DEF9,BYTE,BIN"))),
        ("response_header", "CHDR?", partial(choice, choices=("OFF", "SHORT", "LONG"))),
        ("byte_order", "CORD?", partial(choice, choices=("HI", "LO"))),
        ("waveform_setup", "WFSU?", transfer_setup),
        ("trigger_setup", "TRSE?", value_text),
        ("sequence_setup", "SEQ?", value_text),
        ("interleaved", "ILVD?", partial(choice, choices=("ON", "OFF"))),
    ):
        commands[name] = replace(read_command(name, query, scoped=False), decode=decode)
    commands["trigger_source"] = replace(
        command("trigger_source", "TRSE EDGE,SR,{value}", scoped=False, query="TRSE?"),
        capability=replace(
            command("trigger_source", "TRSE", enum("C1", "C2", "C3", "C4"), scoped=False).capability,
            verification="documented_pending_hardware",
        ),
        decode=trigger_source,
    )
    return Profile(
        "lecroy.9404m",
        ("LECROY", "TELEDYNE"),
        ("WaveRunner9404M", "9404M"),
        "oscilloscope",
        (1, 2, 3, 4),
        SOURCE,
        "MAUI February 2026, parts 6-7; WR9404M specifications",
        commands,
        ("channel_enabled", "scale", "timebase", "trigger_mode"),
        error_query="",
        acquisition="lecroy",
    )
