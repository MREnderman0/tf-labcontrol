from __future__ import annotations

import re
from dataclasses import replace

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, parameter, read_command

BOOL = Parameter("boolean")
RTM_SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1335_9090_01/RTM3000_UserManual_en_12.pdf"
RTO_SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1801_6687_01/RTO6_UserManual_en_14.pdf"
DS_SOURCE = "https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DS1000ZE_ProgrammingGuide_EN.pdf"
LECROY_SOURCE = "https://cdn.teledynelecroy.com/files/manuals/maui-remote-control-and-automation-manual.pdf"


def rs_scope(rto: bool = False) -> Profile:
    commands = {}
    for name, path, param in (
        ("channel_enabled", "CHAN{ch}:STAT", BOOL),
        ("scale", "CHAN{ch}:SCAL", parameter("V/div", 0.001, 10)),
        ("offset", "CHAN{ch}:OFFS", parameter("V", -10, 10)),
        ("coupling", "CHAN{ch}:COUP", enum("DCL", "AC", "DC") if rto else enum("DCL", "ACL", "GND", "DC")),
        (
            "probe_attenuation",
            "PROB{ch}:SET:ATT:MAN",
            parameter("ratio", 0.1, 10000),
        ),
        ("trigger_level", "TRIG1:LEV{ch}" if rto else "TRIG:A:LEV{ch}", parameter("V", -10, 10)),
    ):
        commands[name] = command(name, path, param)
    for name, path, param in (
        ("timebase", "TIM:SCAL", parameter("s/div", 1e-10, 50)),
        ("time_position", "TIM:HOR:POS" if rto else "TIM:POS", parameter("s", -100, 100)),
        ("trigger_source", "TRIG1:SOUR" if rto else "TRIG:A:SOUR", enum("CH1", "CH2", "CH3", "CH4", "EXT")),
        ("trigger_mode", "TRIG1:MODE" if rto else "TRIG:A:MODE", enum("AUTO", "NORM")),
        ("record_length", "ACQ:POIN", Parameter("integer", minimum=1000, maximum=10000000)),
        (
            "acquisition_count",
            "ACQ:COUN" if rto else "ACQ:NSIN:COUN",
            Parameter("integer", minimum=1, maximum=1000000),
        ),
    ):
        commands[name] = command(name, path, param, scoped=False)
    for name, path in (("run", "RUN"), ("stop", "STOP"), ("single", "SING")):
        commands[name] = command(name, path, read=False, scoped=False, confirm=True)
    commands["waveform_header"] = read_command("waveform_header", "CHAN{ch}:DATA:HEAD?")
    if not rto:
        commands["acquisition_mode"] = command(
            "acquisition_mode", "ACQ:TYPE", enum("REFR", "AVER", "ENV"), scoped=False
        )
        commands["averages"] = command(
            "averages", "ACQ:AVER:COUN", Parameter("integer", minimum=2, maximum=1024), scoped=False
        )
        commands["decimation"] = command(
            "decimation", "CHAN:TYPE", enum("SAMP", "PDET", "HRES"), scoped=False
        )
        commands["bandwidth_limit"] = command("bandwidth_limit", "CHAN{ch}:BAND", enum("FULL", "B20"))
        commands["segmented"] = command(
            "segmented", "ACQ:SEGM:STAT", BOOL, scoped=False, option="segmented_memory"
        )
    return Profile(
        "rs.rto64" if rto else "rs.rtm3004",
        ("Rohde&Schwarz", "Rohde-Schwarz"),
        ("RTO64",) if rto else ("RTM3004",),
        "oscilloscope",
        (1, 2, 3, 4),
        RTO_SOURCE if rto else RTM_SOURCE,
        "1801.6687 revision 14" if rto else "1335.9090 revision 12",
        commands,
        ("channel_enabled", "scale", "timebase", "trigger_mode"),
        acquisition="rto" if rto else "rtm",
    )


def rigol() -> Profile:
    commands = {}
    for name, path, param in (
        ("channel_enabled", "CHAN{ch}:DISP", BOOL),
        ("scale", "CHAN{ch}:SCAL", parameter("V/div", 0.001, 10)),
        ("offset", "CHAN{ch}:OFFS", parameter("V", -100, 100)),
        ("coupling", "CHAN{ch}:COUP", enum("AC", "DC", "GND")),
        ("probe_attenuation", "CHAN{ch}:PROB", enum("0.01", "0.1", "1", "10", "100", "1000")),
        ("bandwidth_limit", "CHAN{ch}:BWL", enum("OFF", "20M")),
        ("invert", "CHAN{ch}:INV", BOOL),
    ):
        commands[name] = command(name, path, param)
    for name, path, param in (
        ("timebase", "TIM:SCAL", parameter("s/div", 5e-9, 50)),
        ("time_position", "TIM:OFFS", parameter("s", -100, 100)),
        ("trigger_source", "TRIG:EDGE:SOUR", enum("CHAN1", "CHAN2", "EXT", "AC")),
        ("trigger_slope", "TRIG:EDGE:SLOP", enum("POS", "NEG", "RFAL")),
        ("trigger_level", "TRIG:EDGE:LEV", parameter("V", -100, 100)),
        ("trigger_mode", "TRIG:SWE", enum("AUTO", "NORM", "SING")),
        ("acquisition_mode", "ACQ:TYPE", enum("NORM", "AVER", "PEAK", "HRES")),
        ("averages", "ACQ:AVER", enum("2", "4", "8", "16", "32", "64", "128", "256", "512", "1024")),
        ("record_length", "ACQ:MDEP", enum("AUTO", "12000", "120000", "1200000", "12000000", "24000000")),
    ):
        commands[name] = command(name, path, param, scoped=False)
    for name, path in (("run", "RUN"), ("stop", "STOP"), ("single", "SING"), ("force_trigger", "TFOR")):
        commands[name] = command(name, path, read=False, scoped=False, confirm=True)
    commands["trigger_status"] = read_command("trigger_status", "TRIG:STAT?", scoped=False)
    return Profile(
        "rigol.ds1202ze",
        ("RIGOL",),
        ("DS1202Z-E",),
        "oscilloscope",
        (1, 2),
        DS_SOURCE,
        "DS1000Z-E Programming Guide",
        commands,
        ("channel_enabled", "scale", "timebase", "trigger_status"),
        acquisition="rigol_scope",
    )


def lecroy_value(raw: bytes):
    """Accept both CHDR ON and OFF without changing the instrument's header setting."""
    value = raw.decode("ascii").strip()
    if " " in value and re.match(r"^(?:C\d:)?[A-Z_]+\s", value):
        value = value.split(" ", 1)[1].strip()
    value = value.replace(" ", "")
    numeric = re.fullmatch(r"([+-]?[\d.]+(?:[eE][+-]?\d+)?)([a-zA-Z]*)", value)
    if numeric:
        return float(numeric[1])
    return value


def lecroy(model: str) -> Profile:
    commands = {}
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
    commands["single"] = command("single", "TRMD SINGLE", read=False, scoped=False, confirm=True)
    commands["run"] = command("run", "TRMD AUTO", read=False, scoped=False, confirm=True)
    commands["stop"] = command("stop", "TRMD STOP", read=False, scoped=False, confirm=True)
    commands = {name: replace(spec, decode=lecroy_value) for name, spec in commands.items()}
    return Profile(
        "lecroy." + model.lower().replace("waverunner", ""),
        ("LECROY", "TELEDYNE"),
        (model, model.replace("WaveRunner", "")),
        "oscilloscope",
        (1, 2, 3, 4),
        LECROY_SOURCE,
        "MAUI Remote Control & Automation, IEEE 488.2 chapters 6-7",
        commands,
        ("channel_enabled", "scale", "timebase", "trigger_mode"),
        error_query="",
        acquisition="lecroy",
    )


PROFILES = (rs_scope(), rs_scope(True), rigol(), lecroy("WaveRunner9254"))
