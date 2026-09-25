"""DG5504 Pro command contract; DG5000 Pro guide PGB18101-1110."""

from dataclasses import replace

from lab_control.domain.instruments import Capability, Parameter
from lab_control.instruments.base import CommandSpec, Profile, command, enum, parameter

from .protocol import load_value, trigger_value

DG_SOURCE = "https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/waveform-generators/DG5000Pro_ProgrammingGuide_EN.pdf"
BOOL = Parameter("boolean")


def dg5504pro() -> Profile:
    maximum = 500000000.0
    p = "SOUR{ch}:"
    commands = {
        "waveform": command("waveform", p + "FUNC", enum("SIN", "SQU", "RAMP", "PULS", "NOIS", "DC")),
        "frequency": command("frequency", p + "FREQ", parameter("Hz", 1e-06, maximum)),
        "amplitude": command("amplitude", p + "VOLT", parameter("Vpp", 0.001, 20)),
        "offset": command("offset", p + "VOLT:OFFS", parameter("V", -10, 10)),
        "amplitude_unit": command("amplitude_unit", p + "VOLT:UNIT", enum("VPP", "VRMS", "DBM")),
        "phase": command("phase", p + "PHAS", parameter("deg", -360, 360)),
        "pulse_duty": command("pulse_duty", p + "FUNC:PULS:DCYC", parameter("%", 0.01, 99.99)),
        "pulse_width": command(
            "pulse_width", p + "FUNC:PULS:WIDT", parameter("s", 4.2e-9, 999999.9999563443)
        ),
        "ramp_symmetry": command("ramp_symmetry", p + "FUNC:RAMP:SYMM", parameter("%", 0, 100)),
        "output": command("output", "OUTP{ch}", BOOL, confirm=True),
        "load": command("load", f"OUTP{{ch}}:{'LOAD'}", parameter("ohm", 1, 10000)),
        "burst": command("burst", p + "BURS:STAT", BOOL, confirm=True),
        "burst_mode": command("burst_mode", p + "BURS:MODE", enum("TRIG", "GAT")),
        "burst_cycles": command(
            "burst_cycles", p + "BURS:NCYC", Parameter("integer", minimum=1, maximum=1000000)
        ),
        "sweep_time": command("sweep_time", p + "SWE:TIME", parameter("s", 0.001, 250000)),
        "sweep_spacing": command("sweep_spacing", p + "SWE:SPAC", enum("LIN", "LOG")),
        "sweep_start": command("sweep_start", p + "FREQ:STAR", parameter("Hz", 1e-06, maximum)),
        "sweep_stop": command("sweep_stop", p + "FREQ:STOP", parameter("Hz", 1e-06, maximum)),
        "trigger": command("trigger", "*TRG", read=False, scoped=False, confirm=True),
        "reference_clock": command("reference_clock", "SYST:ROSC:SOUR", enum("INT", "EXT"), scoped=False),
    }
    for modulation in ("AM", "FM", "PM"):
        key = modulation.lower()
        commands[key + "_enabled"] = command(key + "_enabled", p + modulation + ":STAT", BOOL, confirm=True)
        commands[key + "_source"] = command(key + "_source", p + modulation + ":SOUR", enum("INT", "EXT"))
        commands[key + "_frequency"] = command(
            key + "_frequency", p + modulation + ":INT:FREQ", parameter("Hz", 0.002, 1e6)
        )
    commands["am_depth"] = command("am_depth", p + "AM:DEPT", parameter("%", 0, 120))
    commands["fm_deviation"] = command("fm_deviation", p + "FM:DEV", parameter("Hz", 0, maximum / 2))
    commands["pm_deviation"] = command("pm_deviation", p + "PM:DEV", parameter("deg", 0, 360))
    commands["sweep"] = command("sweep", p + "SWE:STAT", BOOL, confirm=True)
    commands["trigger_source"] = command("trigger_source", "TRIG{ch}:SOUR", enum("INT", "EXT", "MAN"))
    commands["arb_enabled"] = command("arb_enabled", p + "FUNC:ADV:ARB:STAT", BOOL, confirm=True)
    commands["arb_sample_rate"] = command(
        "arb_sample_rate", p + "FUNC:ADV:ARB:SRAT", parameter("Sa/s", 1e-6, 1250000000.0)
    )
    commands["load_waveform"] = CommandSpec(
        Capability(
            "load_waveform",
            {"name": Parameter("text")},
            writable=True,
            channel_scoped=True,
            requires_confirmation=True,
            section=":SOURce:FUNCtion:ADVance:ARB:LOAD",
        )
    )
    commands["upload_waveform"] = CommandSpec(
        Capability(
            "upload_waveform",
            {"samples": Parameter("array", minimum=-1, maximum=1), "name": Parameter("text", required=True)},
            writable=True,
            channel_scoped=True,
            section=":MMEMory[:TRACe]:ARB:DATA",
            requires_confirmation=True,
        )
    )
    for mode in ("AM", "FM", "PM"):
        commands[mode.lower() + "_shape"] = command(
            mode.lower() + "_shape", p + mode + ":INT:FUNC", enum("SIN", "SQU", "TRI", "RAMP", "NRAM", "NOIS")
        )
    commands["load"] = replace(commands["load"], decode=load_value)
    commands["trigger_source"] = replace(
        commands["trigger_source"], writes=("TRIG{ch}:SOUR {value}",), decode=trigger_value
    )
    return Profile(
        "rigol.dg5504pro",
        ("RIGOL",),
        ("DG5504 Pro", "DG5504Pro"),
        "signal_generator",
        (1, 2, 3, 4),
        DG_SOURCE,
        "DG5000 Pro Programming Guide",
        commands,
        ("frequency", "amplitude", "offset", "output"),
    )
