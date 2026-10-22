from __future__ import annotations

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, ScpiAdapter, command, enum, parameter, read_command

BOOL = Parameter("boolean")
DS_SOURCE = "https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DS1000ZE_ProgrammingGuide_EN.pdf"


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


class Ds1202zeAdapter(ScpiAdapter):
    def acquire_measurements(self, request):
        from .protocol import acquire

        return acquire(self, request)
