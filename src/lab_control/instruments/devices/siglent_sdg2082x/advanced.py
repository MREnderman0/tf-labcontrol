"""Model-local advanced capability declarations; no hardware access at import."""

from dataclasses import replace
from functools import partial

from lab_control.domain.instruments import Capability, Parameter
from lab_control.instruments.base import CommandSpec, command, enum, parameter

from .protocol import field

BASIC_FIELDS = {"phase": "PHSE", "pulse_duty": "DUTY", "pulse_width": "WIDTH", "ramp_symmetry": "SYM"}
GROUPS = {"modulation": "MDWV", "sweep": "SWWV", "burst": "BTWV"}
ADVANCED_FIELDS = {
    "sweep_start": ("SWWV", "START"),
    "sweep_stop": ("SWWV", "STOP"),
    "sweep_time": ("SWWV", "TIME"),
    "sweep_spacing": ("SWWV", "SWMD"),
    "sweep_direction": ("SWWV", "DIR"),
    "sweep_trigger": ("SWWV", "TRSR"),
    "burst_mode": ("BTWV", "GATE_NCYC"),
    "burst_cycles": ("BTWV", "TIME"),
    "burst_trigger": ("BTWV", "TRSR"),
    "burst_period": ("BTWV", "PRD"),
    "burst_delay": ("BTWV", "DLAY"),
    "burst_phase": ("BTWV", "STPS"),
}


def advanced_commands() -> dict[str, CommandSpec]:
    result = {}

    def add(name, group, key, spec, prefix="", mode=""):
        result[name] = replace(
            command(name, f"C{{ch}}:{group}", spec, query=f"C{{ch}}:{group}?", confirm=True),
            writes=(f"C{{ch}}:{group} {prefix}{key},{{value}}",),
            decode=partial(field, name=key, mode=mode),
        )

    for name, spec in (
        ("phase", parameter("deg", 0, 360)),
        ("pulse_duty", parameter("%", 0.001, 99.999)),
        ("pulse_width", parameter("s", 16.3e-9, 1e6)),
        ("ramp_symmetry", parameter("%", 0, 100)),
    ):
        add(name, "BSWV", BASIC_FIELDS[name], spec)
    for name, group in GROUPS.items():
        add(name, group, "STATE", Parameter("boolean"))
    specs = {
        "sweep_start": parameter("Hz", 1e-6, 80e6),
        "sweep_stop": parameter("Hz", 1e-6, 80e6),
        "sweep_time": parameter("s", 0.001, 500),
        "sweep_spacing": enum("LINE", "LOG"),
        "sweep_direction": enum("UP", "DOWN"),
        "sweep_trigger": enum("INT", "EXT", "MAN"),
        "burst_mode": enum("NCYC", "GATE"),
        "burst_cycles": Parameter("integer", minimum=1, maximum=1000000),
        "burst_trigger": enum("INT", "EXT", "MAN"),
        "burst_period": parameter("s", 1e-6, 1000),
        "burst_delay": parameter("s", 0, 100),
        "burst_phase": parameter("deg", 0, 360),
    }
    for name, (group, key) in ADVANCED_FIELDS.items():
        add(name, group, key, specs[name])
    result["modulation_type"] = replace(
        command("modulation_type", "C{ch}:MDWV", enum("AM", "FM", "PM"), query="C{ch}:MDWV?", confirm=True),
        decode=partial(field, name="MODE"),
    )
    for mode in ("AM", "FM", "PM"):
        for suffix, key, spec in (
            ("source", "SRC", enum("INT", "EXT")),
            ("shape", "MDSP", enum("SINE", "SQUARE", "TRIANGLE", "UPRAMP", "DNRAMP", "NOISE")),
            ("frequency", "FRQ", parameter("Hz", 0.001, 1e6)),
            (
                "depth" if mode == "AM" else "deviation",
                "DEPTH" if mode == "AM" else "DEVI",
                parameter(
                    "%" if mode == "AM" else "Hz" if mode == "FM" else "deg",
                    0,
                    120 if mode == "AM" else 40e6 if mode == "FM" else 360,
                ),
            ),
        ):
            add(f"{mode.lower()}_{suffix}", "MDWV", key, spec, mode + ",", mode)
    for name, parameters in (
        (
            "upload_waveform",
            {"name": Parameter("text"), "samples": Parameter("array", minimum=-1, maximum=1)},
        ),
        ("load_waveform", {"name": Parameter("text")}),
    ):
        result[name] = CommandSpec(
            Capability(
                name,
                parameters,
                writable=True,
                channel_scoped=True,
                requires_confirmation=True,
                section="PG02-E05A 3.9/3.31",
            )
        )
    return result
