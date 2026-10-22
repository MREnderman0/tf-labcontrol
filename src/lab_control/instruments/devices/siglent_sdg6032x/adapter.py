from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

import numpy as np

from lab_control.domain.instruments import Capability, DeviceCommand, Parameter, ValidationError
from lab_control.instruments.base import CommandSpec, Profile, ScpiAdapter, command, enum, parameter
from lab_control.instruments.common.siglent import sdg_field

SDG_SOURCE = (
    "https://siglentna.com/wp-content/uploads/dlm_uploads/2025/12/SDG6000X_ProgrammingGuide_EN06A.pdf"
)
BOOL = Parameter("boolean")


def sdg6032x() -> Profile:
    commands = {}
    for name, field, param in (
        ("waveform", "WVTP", enum("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "DC", "ARB")),
        ("frequency", "FRQ", parameter("Hz", 1e-6, 350e6)),
        ("amplitude", "AMP", parameter("Vpp", 0.002, 10)),
        ("offset", "OFST", parameter("V", -5, 5)),
        ("phase", "PHSE", parameter("deg", 0, 360)),
        ("duty", "DUTY", parameter("%", 0.01, 99.99)),
        ("ramp_symmetry", "SYM", parameter("%", 0, 100)),
    ):
        spec = command(name, f"C{{ch}}:BSWV {field},{{value}}", param, query="C{ch}:BSWV?")
        commands[name] = replace(
            spec, writes=(f"C{{ch}}:BSWV {field},{{value}}",), decode=partial(sdg_field, field=field)
        )
    commands["output"] = CommandSpec(
        Capability(
            "output",
            {"value": BOOL},
            writable=True,
            readable=True,
            channel_scoped=True,
            requires_confirmation=True,
            section="3.3 OUTPut",
        ),
        ("C{ch}:OUTP {value}",),
        "C{ch}:OUTP?",
        decode=lambda raw: raw.decode().split(" ", 1)[1].split(",")[0],
    )
    for group, fields in {
        "SWWV": (
            ("sweep", "STATE", BOOL),
            ("sweep_start", "START", parameter("Hz", 1e-6, 350e6)),
            ("sweep_stop", "STOP", parameter("Hz", 1e-6, 350e6)),
            ("sweep_time", "TIME", parameter("s", 0.001, 300)),
            ("sweep_spacing", "SWMD", enum("LINE", "LOG", "STEP")),
            ("sweep_trigger", "TRSR", enum("INT", "EXT", "MAN")),
        ),
        "BTWV": (
            ("burst", "STATE", BOOL),
            ("burst_cycles", "TIME", Parameter("integer", minimum=1, maximum=1000000)),
            ("burst_trigger", "TRSR", enum("INT", "EXT", "MAN")),
        ),
    }.items():
        for name, field, param in fields:
            commands[name] = CommandSpec(
                Capability(
                    name,
                    {"value": param},
                    readable=True,
                    writable=True,
                    requires_confirmation=True,
                    channel_scoped=True,
                    section=group,
                ),
                (f"C{{ch}}:{group} {field},{{value}}",),
                f"C{{ch}}:{group}?",
                decode=partial(sdg_field, field=field),
            )
    commands["burst_trigger_now"] = command("burst_trigger_now", "C{ch}:BTWV MTRIG", read=False, confirm=True)
    commands["sweep_trigger_now"] = command("sweep_trigger_now", "C{ch}:SWWV MTRIG", read=False, confirm=True)
    commands["reference_clock"] = command("reference_clock", "ROSC", enum("INT", "EXT"), scoped=False)
    commands["modulation"] = CommandSpec(
        Capability(
            "modulation",
            {"value": BOOL},
            readable=True,
            writable=True,
            requires_confirmation=True,
            channel_scoped=True,
            section="3.10 Modulate Wave",
        ),
        ("C{ch}:MDWV STATE,{value}",),
        "C{ch}:MDWV?",
        decode=partial(sdg_field, field="STATE"),
    )
    commands["modulation_type"] = CommandSpec(
        Capability(
            "modulation_type",
            {"value": enum("AM", "FM", "PM")},
            writable=True,
            channel_scoped=True,
            section="3.10 Modulate Wave",
        ),
        ("C{ch}:MDWV {value}",),
    )
    for group in ("AM", "FM", "PM"):
        for suffix, field, param in (
            ("source", "SRC", enum("INT", "EXT")),
            ("frequency", "FRQ", parameter("Hz", 0.001, 100000)),
            ("shape", "MDSP", enum("SINE", "SQUARE", "TRIANGLE", "UPRAMP", "DNRAMP", "NOISE")),
            (
                "depth" if group == "AM" else "deviation",
                "DEPTH" if group == "AM" else "DEVI",
                parameter(
                    "%" if group == "AM" else "Hz" if group == "FM" else "deg",
                    0,
                    120 if group == "AM" else 175e6 if group == "FM" else 360,
                ),
            ),
        ):
            name = f"{group.lower()}_{suffix}"
            commands[name] = CommandSpec(
                Capability(
                    name,
                    {"value": param},
                    readable=True,
                    writable=True,
                    channel_scoped=True,
                    section="3.10 Modulate Wave",
                ),
                (f"C{{ch}}:MDWV {group},{field},{{value}}",),
                "C{ch}:MDWV?",
                decode=partial(sdg_field, field=field),
            )
    commands["upload_waveform"] = CommandSpec(
        Capability(
            "upload_waveform",
            {"samples": Parameter("array", minimum=-1, maximum=1), "name": Parameter("text")},
            writable=True,
            channel_scoped=True,
            requires_confirmation=True,
            section="3.9.2 WVDT and 5.1.5 Upload a waveform",
        )
    )
    commands["load_waveform"] = CommandSpec(
        Capability(
            "load_waveform",
            {"name": Parameter("text")},
            writable=True,
            channel_scoped=True,
            requires_confirmation=True,
            section="3.8 ARWV",
        )
    )
    return Profile(
        "siglent.sdg6032x",
        ("SIGLENT",),
        ("SDG6032X",),
        "signal_generator",
        (1, 2),
        SDG_SOURCE,
        "EN06A",
        commands,
        ("frequency", "amplitude", "offset", "output"),
        error_query="",
    )


class Sdg6032xAdapter(ScpiAdapter):
    def validate_command(self, cmd: DeviceCommand) -> tuple[CommandSpec, dict[str, Any]]:
        spec, values = super().validate_command(cmd)
        if cmd.operation == "upload_waveform":
            samples = values["samples"]
            if not 32 <= len(samples) <= 131072:
                raise ValidationError("This upload implementation accepts 32..131072 samples")
        if "name" in values:
            import re

            if not re.fullmatch("[A-Za-z][A-Za-z0-9_]{0,23}", values["name"]):
                raise ValidationError("Name: ASCII letters/digits/underscore, max 24")
        return (spec, values)

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        super().preflight(cmd, parameters)
        if cmd.operation in {"amplitude", "offset"}:
            if (
                "amplitude_unit" in self.profile.commands
                and str(self.read("amplitude_unit", cmd.channel)).upper() != "VPP"
            ):
                raise ValidationError(
                    "Select amplitude_unit VPP explicitly before setting Vpp amplitude/offset"
                )
            amplitude = (
                parameters["value"]
                if cmd.operation == "amplitude"
                else float(self.read("amplitude", cmd.channel))
            )
            offset = (
                parameters["value"] if cmd.operation == "offset" else float(self.read("offset", cmd.channel))
            )
            if amplitude / 2 + abs(offset) > 5:
                raise ValidationError("Conservative envelope: amplitude/2 + abs(offset) <= 5 V")
        if self.adapter_id == "siglent.sdg6032x":
            group = (
                "MDWV"
                if cmd.operation == "modulation_type" or cmd.operation.startswith(("am_", "fm_", "pm_"))
                else "SWWV"
                if cmd.operation.startswith("sweep_")
                else "BTWV"
                if cmd.operation.startswith("burst_")
                else ""
            )
            if group:
                response = self.transport.query(f"C{cmd.channel}:{group}?")
                if str(sdg_field(response, "STATE")).upper() != "ON":
                    raise ValidationError(f"Explicitly enable {group} before changing its parameters")
                if group == "MDWV" and cmd.operation != "modulation_type":
                    mode = cmd.operation.split("_", 1)[0].upper()
                    if response.decode().split(" ", 1)[-1].split(",")[0] != mode:
                        raise ValidationError(f"Select modulation_type {mode} first")
                    if (
                        not cmd.operation.endswith("_source")
                        and str(sdg_field(response, "SRC")).upper() != "INT"
                    ):
                        raise ValidationError("Internal modulation source required")
        if cmd.operation == "fm_deviation":
            carrier = float(self.read("frequency", cmd.channel))
            maximum = self.capabilities().get("frequency").parameters["value"].maximum or carrier
            if parameters["value"] > min(carrier, maximum - carrier):
                raise ValidationError("FM deviation exceeds carrier/bandwidth margins")

    def encode(
        self, operation: str, spec: CommandSpec, context: dict[str, Any], parameters: dict[str, Any]
    ) -> tuple[bytes | str, ...]:
        if operation == "load_waveform":
            return (f"C{context['ch']}:ARWV NAME,{parameters['name']}",)
        if operation != "upload_waveform":
            return super().encode(operation, spec, context, parameters)
        samples = np.asarray(parameters["samples"])
        data = np.rint(samples * 32767).astype("<i2").tobytes()
        return (f"C{context['ch']}:WVDT WVNM,{parameters['name']},WAVEDATA,".encode() + data,)
