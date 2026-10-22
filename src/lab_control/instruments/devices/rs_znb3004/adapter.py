from __future__ import annotations

from dataclasses import replace

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, ScpiAdapter, command, enum, parameter, read_command

ZNB_SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1179_7491_01/ZNB3000_UserManual_en_09.pdf"
BOOL = Parameter("boolean")


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


class Znb3004Adapter(ScpiAdapter):
    def acquire_measurements(self, request):
        from .protocol import acquire

        return acquire(self, request)
