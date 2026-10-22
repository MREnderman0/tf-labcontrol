from __future__ import annotations

from dataclasses import replace

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, ScpiAdapter, command, enum, parameter, read_command

DMM_SOURCE = "https://www.keysight.com/us/en/assets/9018-01343/user-manuals/9018-01343.pdf"
BOOL = Parameter("boolean")


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


class Keysight3458aAdapter(ScpiAdapter):
    def acquire_measurements(self, request):
        from .protocol import acquire

        return acquire(self, request)

    def decode_identity(self, raw):
        import re

        from lab_control.domain.instruments import DeviceIdentity

        model = raw.decode("ascii").strip().strip('"')
        if re.sub(r"\s", "", model).upper() in {"HP3458A", "KEYSIGHT3458A", "AGILENT3458A"}:
            model = "3458A"
        return DeviceIdentity("Keysight", model, confidence="model_only", raw=model)
