from __future__ import annotations

from dataclasses import replace

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, parameter, read_command

DSA_SOURCE = "https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/spectrum-analyzers/DSA800_ProgrammingGuide_EN.pdf"
FPH_SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1321_1011_01/FPH_User_Manual_en_21.pdf"
ZNB_SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1179_7491_01/ZNB3000_UserManual_en_09.pdf"
DMM_SOURCE = "https://www.keysight.com/us/en/assets/9018-01343/user-manuals/9018-01343.pdf"
BOOL = Parameter("boolean")


def spectrum(fph: bool = False) -> Profile:
    commands = {}
    maximum = 2e9 if fph else 1.5e9
    for name, path, param in (
        ("start_frequency", "SENS:FREQ:STAR", parameter("Hz", 0, maximum)),
        ("stop_frequency", "SENS:FREQ:STOP", parameter("Hz", 0, maximum)),
        ("center_frequency", "SENS:FREQ:CENT", parameter("Hz", 0, maximum)),
        ("span", "SENS:FREQ:SPAN", parameter("Hz", 0, maximum)),
        ("rbw", "SENS:BAND:RES", parameter("Hz", 100, 1e6)),
        ("vbw", "SENS:BAND:VID", parameter("Hz", 10, 1e6)),
        ("rbw_auto", "SENS:BAND:RES:AUTO", BOOL),
        ("vbw_auto", "SENS:BAND:VID:AUTO", BOOL),
        ("reference_level", "DISP:TRAC:Y:RLEV", parameter("dBm", -100, 30)),
        ("attenuation", "INP:ATT", parameter("dB", 0, 30)),
        ("attenuation_auto", "INP:ATT:AUTO", BOOL),
        ("sweep_time", "SENS:SWE:TIME", parameter("s", 0.001, 1000)),
        ("continuous", "INIT:CONT", BOOL),
        ("marker_enabled", "CALC:MARK1:STAT", BOOL),
        ("marker_frequency", "CALC:MARK1:X", parameter("Hz", 0, maximum)),
    ):
        commands[name] = command(name, path, param, scoped=False, confirm=name == "continuous")
    commands["single"] = command("single", "INIT:IMM", read=False, scoped=False, confirm=True)
    commands["marker_peak"] = command("marker_peak", "CALC:MARK1:MAX", read=False, scoped=False)
    commands["marker_value"] = read_command("marker_value", "CALC:MARK1:Y?", scoped=False)
    if not fph:
        commands["trace_mode"] = command(
            "trace_mode", "TRAC1:MODE", enum("WRIT", "MAXH", "MINH", "VIEW", "BLAN"), scoped=False
        )
        commands["detector"] = command(
            "detector", "SENS:DET:FUNC", enum("POS", "NEG", "SAMP", "NORM", "RMS", "VAV"), scoped=False
        )
    return Profile(
        "rs.fph" if fph else "rigol.dsa815",
        ("Rohde&Schwarz", "Rohde-Schwarz") if fph else ("RIGOL",),
        ("FPH",) if fph else ("DSA815",),
        "spectrum_analyzer",
        (1,),
        FPH_SOURCE if fph else DSA_SOURCE,
        "1321.1011 revision 21" if fph else "DSA800 Programming Guide",
        commands,
        ("start_frequency", "stop_frequency", "rbw", "reference_level"),
        acquisition="fph" if fph else "dsa",
    )


def network() -> Profile:
    commands = {}
    for name, path, param in (
        ("start_frequency", "SENS1:FREQ:STAR", parameter("Hz", 100000, 4.5e9)),
        ("stop_frequency", "SENS1:FREQ:STOP", parameter("Hz", 100000, 4.5e9)),
        ("points", "SENS1:SWE:POIN", Parameter("integer", minimum=2, maximum=100001)),
        ("if_bandwidth", "SENS1:BAND", parameter("Hz", 1, 1e6)),
        ("averaging", "SENS1:AVER", BOOL),
        ("averages", "SENS1:AVER:COUN", Parameter("integer", minimum=1, maximum=65536)),
        ("continuous", "INIT1:CONT", BOOL),
        ("sweep_type", "SENS1:SWE:TYPE", enum("LIN", "LOG", "SEGM", "POW", "CW")),
    ):
        commands[name] = command(name, path, param, scoped=False, confirm=name == "continuous")
    commands["port_power"] = command("port_power", "SOUR1:POW{ch}", parameter("dBm", -50, 10), confirm=True)
    commands["single"] = command("single", "INIT1:IMM", read=False, scoped=False, confirm=True)
    commands["correction_status"] = read_command("correction_status", "SENS1:CORR:STAT?", scoped=False)
    commands["trace_catalog"] = read_command("trace_catalog", "CALC1:PAR:CAT?", scoped=False)
    # Trace creation changes the active measurement and is explicit, never performed by discovery.
    commands["s_parameter"] = command(
        "s_parameter",
        "CALC1:PAR:SDEF 'LabControl',",
        enum("S11", "S21", "S12", "S22"),
        read=False,
        scoped=False,
    )
    commands["s_parameter"] = replace(
        commands["s_parameter"],
        writes=("CALC1:PAR:SDEF 'LabControl','{value}'", "CALC1:PAR:SEL 'LabControl'"),
    )
    commands["selected_trace"] = read_command("selected_trace", "CALC1:PAR:SEL?", scoped=False)
    return Profile(
        "rs.znb3004",
        ("Rohde&Schwarz", "Rohde-Schwarz"),
        ("ZNB3004",),
        "network_analyzer",
        (1, 2),
        ZNB_SOURCE,
        "1179.7491 revision 09, Remote commands",
        commands,
        ("start_frequency", "stop_frequency", "points", "correction_status"),
        acquisition="vna",
    )


def multimeter() -> Profile:
    commands = {}
    for name, path, param in (
        (
            "function",
            "FUNC",
            enum("DCV", "ACV", "ACDCV", "DCI", "ACI", "ACDCI", "OHM", "OHMF", "FREQ", "PER"),
        ),
        ("range", "RANGE", parameter("function-dependent", 1e-9, 1e9)),
        ("autorange", "ARANGE", enum("ON", "OFF")),
        ("integration", "NPLC", parameter("PLC", 0.0001, 1000)),
        ("resolution", "RES", parameter("%", 0.000001, 100)),
        ("autozero", "AZERO", enum("ON", "OFF", "ONCE")),
        ("delay", "DELAY", parameter("s", 0, 6000)),
        ("trigger_source", "TRIG", enum("AUTO", "EXT", "HOLD", "SYN")),
        ("trigger_arm", "TARM", enum("AUTO", "EXT", "HOLD")),
        ("memory", "MEM", enum("OFF", "FIFO", "LIFO")),
        ("output_format", "OFORMAT", enum("ASCII")),
    ):
        commands[name] = command(
            name, path, param, scoped=False, read=False, confirm=name in {"trigger_source", "trigger_arm"}
        )
    for name, path in (
        ("function_status", "FUNC?"),
        ("integration_status", "NPLC?"),
        ("errors", "ERR?"),
        ("temperature", "TEMP?"),
    ):
        commands[name] = read_command(name, path, scoped=False)
    commands["sample_count"] = replace(
        command(
            "sample_count",
            "NRDGS",
            Parameter("integer", minimum=1, maximum=1000000),
            scoped=False,
            read=False,
        ),
        writes=("NRDGS {value},AUTO",),
    )
    return Profile(
        "keysight.3458a",
        ("KEYSIGHT", "AGILENT", "HEWLETT-PACKARD"),
        ("3458A",),
        "multimeter",
        (1,),
        DMM_SOURCE,
        "03458-90014, Command reference",
        commands,
        ("function_status", "integration_status"),
        identity_query="ID?",
        error_query="",
        acquisition="dmm",
    )


PROFILES = (spectrum(), spectrum(True), network(), multimeter())
