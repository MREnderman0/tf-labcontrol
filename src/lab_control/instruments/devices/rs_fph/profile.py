"""Conservative FPH02 control contract, including firmware V1.40."""

from dataclasses import replace
from functools import partial
from typing import Any, Callable

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, parameter, read_command

from .protocol import RBW, VBW, boolean, choice, integer, scalar, token

SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1321_1011_01/FPH_User_Manual_en_21.pdf"
BOOL = Parameter("boolean")


def fph() -> Profile:
    commands = {}
    for name, path, param in (
        ("start_frequency", "SENS:FREQ:STAR", parameter("Hz", 0, 2e9)),
        ("stop_frequency", "SENS:FREQ:STOP", parameter("Hz", 0, 2e9)),
        ("center_frequency", "SENS:FREQ:CENT", parameter("Hz", 0, 2e9)),
        ("span", "SENS:FREQ:SPAN", parameter("Hz", 0, 2e9)),
        ("rbw", "SENS:BAND:RES", parameter("Hz", 100, 1e6)),
        ("vbw", "SENS:BAND:VID", parameter("Hz", 10, 1e6)),
        ("rbw_auto", "SENS:BAND:RES:AUTO", BOOL),
        ("vbw_auto", "SENS:BAND:VID:AUTO", BOOL),
        ("reference_level", "DISP:TRAC1:Y:RLEV", parameter("dBm", -100, 30)),
        ("attenuation", "INP:ATT", Parameter("integer", "dB", 0, 30)),
        ("attenuation_auto", "INP:ATT:AUTO", BOOL),
        ("sweep_time", "SENS:SWE:TIME", parameter("s", 0.001, 1000)),
        ("sweep_time_auto", "SENS:SWE:TIME:AUTO", BOOL),
        ("sweep_count", "SENS:SWE:COUN", Parameter("integer", minimum=1, maximum=999)),
        ("continuous", "INIT:CONT", BOOL),
        ("amplitude_unit", "UNIT:POW", enum("DBM", "DBMV", "DBUV", "V", "W")),
        ("detector", "SENS:DET1:FUNC", enum("POS", "NEG", "SAMP", "RMS", "AVER", "APE")),
        ("detector_auto", "SENS:DET1:FUNC:AUTO", BOOL),
        ("trace_mode", "DISP:TRAC1:MODE", enum("WRIT", "AVER", "MINH", "MAXH", "VIEW")),
        ("trigger_source", "TRIG:SOUR", enum("IMM", "EXT")),
        ("marker_enabled", "CALC:MARK1:STAT", BOOL),
        ("marker_frequency", "CALC:MARK1:X", parameter("Hz", 0, 2e9)),
    ):
        if name in {"rbw", "vbw"}:
            param = replace(param, choices=tuple(f"{v:g}" for v in (RBW if name == "rbw" else VBW)))
        spec = command(name, path, param, scoped=False, confirm=name == "continuous")
        decode: Callable[[bytes], Any] = boolean if param.kind == "boolean" else scalar
        if param.kind == "integer":
            decode = integer
        elif param.kind == "enum":
            choices = param.choices
            if name == "trigger_source":
                choices += ("VID", "IQP", "GAT")
            elif name == "trace_mode":
                choices += ("FRE", "INF")
            decode = partial(choice, choices=choices)
        writes = spec.writes
        if param.unit in {"Hz", "s", "dB", "dBm"}:
            writes = (path + " {value} " + param.unit,)
        commands[name] = replace(spec, decode=decode, compare=False, writes=writes)
    for name, path, decode in (
        ("options", "*OPT?", token),
        ("measurement_mode", "INST?", token),
        ("transfer_format", "FORM?", partial(choice, choices=("ASC", "REAL"))),
        ("byte_order", "FORM:BORD?", partial(choice, choices=("NORM", "SWAP"))),
        ("sweep_points", "", integer),
        ("frequency_offset", "SENS:FREQ:OFFS?", scalar),
        ("reference_offset", "DISP:TRAC1:Y:RLEV:OFFS?", scalar),
        ("preamplifier", "INP:GAIN:STAT?", boolean),
        ("marker_value", "CALC:MARK1:Y?", scalar),
        ("marker_frequency_mode", "CALC:MARK:FREQ:MODE?", partial(choice, choices=("FREQ", "CHAN"))),
        ("errors", "SYST:ERR?", token),
    ):
        commands[name] = replace(read_command(name, path, scoped=False), decode=decode)
    commands["single"] = command("single", "INIT:IMM", read=False, scoped=False, confirm=True)
    fixed_points = commands["sweep_points"]
    commands["sweep_points"] = replace(
        fixed_points,
        capability=replace(
            fixed_points.capability,
            parameters={"value": Parameter("integer", minimum=711, maximum=711)},
            source=SOURCE,
            section="TRACe:DATA?, p.358: fixed 711 display points",
        ),
    )
    commands["marker_peak"] = command("marker_peak", "CALC:MARK1:MAX", read=False, scoped=False)
    return Profile(
        "rs.fph",
        ("Rohde&Schwarz", "Rohde-Schwarz"),
        ("FPH",),
        "spectrum_analyzer",
        (1,),
        SOURCE,
        "1321.1011 revision 21; conservative FPH02 V1.40 subset",
        commands,
        ("start_frequency", "stop_frequency", "rbw", "reference_level"),
        acquisition="fph",
    )
