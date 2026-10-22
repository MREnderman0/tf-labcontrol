from __future__ import annotations

import re
from dataclasses import replace

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, ScpiAdapter, command, enum, parameter

BOOL = Parameter("boolean")
LECROY_SOURCE = "https://cdn.teledynelecroy.com/files/manuals/maui-remote-control-and-automation-manual.pdf"


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


def lecroy(model: str = "WaveRunner9254") -> Profile:
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


class WaveRunner9254Adapter(ScpiAdapter):
    def acquire_measurements(self, request):
        from .protocol import acquire

        return acquire(self, request)
