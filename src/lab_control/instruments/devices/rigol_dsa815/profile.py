"""Documented DSA815 commands; the legacy migration contract remains immutable."""

from dataclasses import replace
from functools import partial
from typing import Any, Callable

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, number, parameter, read_command

from .protocol import RBW, VBW, boolean, choice, integer, option_table, token

SOURCE = "https://beyondmeasure.rigoltech.com/acton/attachment/1579/f-06f0/1/-/-/-/-/dsa800programpdf.pdf"
BOOL = Parameter("boolean")


def dsa815() -> Profile:
    commands = {}
    for name, path, param in (
        ("start_frequency", "SENS:FREQ:STAR", parameter("Hz", 0, 1.5e9)),
        ("stop_frequency", "SENS:FREQ:STOP", parameter("Hz", 0, 1.5e9)),
        ("center_frequency", "SENS:FREQ:CENT", parameter("Hz", 0, 1.5e9)),
        ("span", "SENS:FREQ:SPAN", parameter("Hz", 0, 1.5e9)),
        ("rbw", "SENS:BAND:RES", parameter("Hz", 10, 1e6)),
        ("vbw", "SENS:BAND:VID", parameter("Hz", 1, 3e6)),
        ("rbw_auto", "SENS:BAND:RES:AUTO", BOOL),
        ("vbw_auto", "SENS:BAND:VID:AUTO", BOOL),
        ("reference_level", "DISP:WIN:TRAC:Y:RLEV", parameter("dBm", -100, 20)),
        ("attenuation", "SENS:POW:RF:ATT", Parameter("integer", "dB", 0, 30)),
        ("attenuation_auto", "SENS:POW:RF:ATT:AUTO", BOOL),
        ("sweep_time", "SENS:SWE:TIME", parameter("s", 20e-6, 7500)),
        ("sweep_time_auto", "SENS:SWE:TIME:AUTO", BOOL),
        ("continuous", "INIT:CONT", BOOL),
        ("amplitude_unit", "UNIT:POW", enum("DBM", "DBMV", "DBUV", "V", "W")),
        ("detector", "SENS:DET:FUNC", enum("POS", "NEG", "SAMP", "NORM", "RMS", "VAV")),
        ("trace_mode", "TRAC1:MODE", enum("WRIT", "MAXH", "MINH", "VIEW", "BLAN", "VID", "POW")),
        ("trigger_source", "TRIG:SEQ:SOUR", enum("IMM", "VID", "EXT")),
        ("marker_enabled", "CALC:MARK1:STAT", BOOL),
        ("marker_frequency", "CALC:MARK1:X", parameter("Hz", 0, 1.5e9)),
        ("marker_position", "CALC:MARK1:X:POS", Parameter("integer", "point", 0, 600)),
        ("marker_mode", "CALC:MARK1:MODE", enum("POS")),
        ("marker_readout", "CALC:MARK1:X:READ", enum("FREQ", "TIME")),
    ):
        if name in {"rbw", "vbw"}:
            param = replace(param, choices=tuple(f"{v:g}" for v in (RBW if name == "rbw" else VBW)))
        spec = command(name, path, param, scoped=False, confirm=name == "continuous")
        decode: Callable[[bytes], Any] = (
            boolean if param.kind == "boolean" else integer if param.kind == "integer" else number
        )
        if param.kind == "enum":
            choices = param.choices
            if name == "detector":
                choices += ("QPEAK",)
            elif name == "marker_mode":
                choices += ("DELT", "BAND", "SPAN")
            elif name == "marker_readout":
                choices += ("ITIM", "PER")
            decode = partial(choice, choices=choices)
        commands[name] = replace(spec, decode=decode, compare=False)
    for name, path, decode in (
        ("options", "SYST:OPT?", option_table),
        ("transfer_format", "FORM?", partial(choice, choices=("ASC", "REAL"))),
        ("byte_order", "FORM:BORD?", partial(choice, choices=("NORM", "SWAP"))),
        # User Guide 2-33 excludes DSA815 from variable sweep points; display has 601 points (5-7).
        ("sweep_points", "", integer),
        ("errors", "SYST:ERR?", token),
        ("sweep_count", "SENS:SWE:COUN?", integer),
        ("sweep_current", "SENS:SWE:COUN:CURR?", integer),
        ("operation_condition", "STAT:OPER:COND?", integer),
        ("measurement_mode", "CONF?", token),
        ("frequency_offset", "SENS:FREQ:OFFS?", number),
        ("reference_offset", "DISP:WIN:TRAC:Y:RLEV:OFFS?", number),
        ("preamplifier", "SENS:POW:RF:GAIN:STAT?", boolean),
        ("mixer_level", "SENS:POW:RF:MIX:RANG:UPP?", number),
        ("marker_value", "CALC:MARK1:Y?", number),
        ("marker_trace", "CALC:MARK1:TRAC?", integer),
        ("marker_trace_auto", "CALC:MARK1:TRAC:AUTO?", boolean),
        ("marker_function", "CALC:MARK1:FUNC?", token),
        ("marker_tracking", "CALC:MARK1:CPE:STAT?", boolean),
        ("marker_search", "CALC:MARK1:PEAK:SEAR:MODE?", token),
    ):
        commands[name] = replace(read_command(name, path, scoped=False), decode=decode)
    fixed_points = commands["sweep_points"]
    commands["sweep_points"] = replace(
        fixed_points,
        capability=replace(
            fixed_points.capability,
            section="User Guide 2-33 / 5-7: fixed 601 display points",
            parameters={"value": Parameter("integer", "point", 601, 601)},
        ),
    )
    commands["single"] = command("single", "INIT:IMM", read=False, scoped=False, confirm=True)
    commands["auto_setup"] = command("auto_setup", ":SENSe:POWer:ATUNe", read=False, scoped=False)
    commands["marker_peak"] = command("marker_peak", "CALC:MARK1:MAX:MAX", read=False, scoped=False)
    return Profile(
        "rigol.dsa815",
        ("RIGOL",),
        ("DSA815",),
        "spectrum_analyzer",
        (1,),
        SOURCE,
        "DSA800 Programming Guide chapters 2-16, 2-48, 2-75, 2-126..149, 2-188..199",
        commands,
        ("start_frequency", "stop_frequency", "rbw", "reference_level"),
        acquisition="dsa",
    )
