from __future__ import annotations

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, ScpiAdapter, command, enum, parameter, read_command

BOOL = Parameter("boolean")
RTM_SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1335_9090_01/RTM3000_UserManual_en_12.pdf"


def rtm3004() -> Profile:
    commands = {}
    for name, path, param in (
        ("channel_enabled", "CHAN{ch}:STAT", BOOL),
        ("scale", "CHAN{ch}:SCAL", parameter("V/div", 0.001, 10)),
        ("offset", "CHAN{ch}:OFFS", parameter("V", -10, 10)),
        ("coupling", "CHAN{ch}:COUP", enum("DCL", "ACL", "GND", "DC")),
        ("probe_attenuation", "PROB{ch}:SET:ATT:MAN", parameter("ratio", 0.1, 10000)),
        ("trigger_level", "TRIG:A:LEV{ch}", parameter("V", -10, 10)),
    ):
        commands[name] = command(name, path, param)
    for name, path, param in (
        ("timebase", "TIM:SCAL", parameter("s/div", 1e-10, 50)),
        ("time_position", "TIM:POS", parameter("s", -100, 100)),
        ("trigger_source", "TRIG:A:SOUR", enum("CH1", "CH2", "CH3", "CH4", "EXT")),
        ("trigger_mode", "TRIG:A:MODE", enum("AUTO", "NORM")),
        ("record_length", "ACQ:POIN", Parameter("integer", minimum=1000, maximum=10000000)),
        ("acquisition_count", "ACQ:NSIN:COUN", Parameter("integer", minimum=1, maximum=1000000)),
    ):
        commands[name] = command(name, path, param, scoped=False)
    for name, path in (("run", "RUN"), ("stop", "STOP"), ("single", "SING")):
        commands[name] = command(name, path, read=False, scoped=False, confirm=True)
    commands["waveform_header"] = read_command("waveform_header", "CHAN{ch}:DATA:HEAD?")
    if not False:
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
        "rs.rtm3004",
        ("Rohde&Schwarz", "Rohde-Schwarz"),
        ("RTM3004",),
        "oscilloscope",
        (1, 2, 3, 4),
        RTM_SOURCE,
        "1335.9090 revision 12",
        commands,
        ("channel_enabled", "scale", "timebase", "trigger_mode"),
        acquisition="rtm",
    )


class Rtm3004Adapter(ScpiAdapter):
    def acquire_measurements(self, request):
        from .protocol import acquire

        return acquire(self, request)
