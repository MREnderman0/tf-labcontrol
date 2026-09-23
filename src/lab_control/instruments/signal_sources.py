from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

import numpy as np

from lab_control.domain.instruments import Capability, DeviceCommand, Parameter, ValidationError
from lab_control.instruments.base import CommandSpec, Profile, ScpiAdapter, command, enum, parameter
from lab_control.transport.base import ieee_block

AFG_SOURCE = "https://download.tek.com/manual/077148800_AFG31000_Programmer_Manual_01.pdf"
DG_SOURCE = "https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/waveform-generators/DG5000Pro_ProgrammingGuide_EN.pdf"
SDG_SOURCE = (
    "https://siglentna.com/wp-content/uploads/dlm_uploads/2025/12/SDG6000X_ProgrammingGuide_EN06A.pdf"
)
SMA_SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1178_3834_01/SMA100B_UserManual_en_14.pdf"
BOOL = Parameter("boolean")


def function_generator(adapter_id: str) -> Profile:
    tek = adapter_id == "tek.afg31252"
    maximum = 250e6 if tek else 500e6
    p = "SOUR{ch}:"
    commands = {
        "waveform": command("waveform", p + "FUNC", enum("SIN", "SQU", "RAMP", "PULS", "NOIS", "DC")),
        "frequency": command("frequency", p + "FREQ", parameter("Hz", 1e-6, maximum)),
        "amplitude": command("amplitude", p + "VOLT", parameter("Vpp", 0.002, 10)),
        "offset": command("offset", p + "VOLT:OFFS", parameter("V", -5, 5)),
        "amplitude_unit": command("amplitude_unit", p + "VOLT:UNIT", enum("VPP", "VRMS", "DBM")),
        "phase": command(
            "phase",
            p + "PHAS",
            parameter("deg" if not tek else "rad", -3.141592653 if tek else 0, 3.141592653 if tek else 360),
        ),
        "pulse_duty": command("pulse_duty", p + "PULS:DCYC", parameter("%", 0.01, 99.99)),
        "pulse_width": command("pulse_width", p + "PULS:WIDT", parameter("s", 1e-9, 1000)),
        "ramp_symmetry": command("ramp_symmetry", p + "FUNC:RAMP:SYMM", parameter("%", 0, 100)),
        "output": command("output", "OUTP{ch}", BOOL, confirm=True),
        "load": command("load", f"OUTP{{ch}}:{'IMP' if tek else 'LOAD'}", parameter("ohm", 1, 10000)),
        "burst": command("burst", p + "BURS:STAT", BOOL, confirm=True),
        "burst_mode": command("burst_mode", p + "BURS:MODE", enum("TRIG", "GAT")),
        "burst_cycles": command(
            "burst_cycles", p + "BURS:NCYC", Parameter("integer", minimum=1, maximum=1000000)
        ),
        "sweep_time": command("sweep_time", p + "SWE:TIME", parameter("s", 0.001, 300)),
        "sweep_spacing": command("sweep_spacing", p + "SWE:SPAC", enum("LIN", "LOG")),
        "sweep_start": command("sweep_start", p + "FREQ:STAR", parameter("Hz", 1e-6, maximum)),
        "sweep_stop": command("sweep_stop", p + "FREQ:STOP", parameter("Hz", 1e-6, maximum)),
        "trigger": command("trigger", "*TRG", read=False, scoped=False, confirm=True),
        "reference_clock": command(
            "reference_clock", "SOUR:ROSC:SOUR" if tek else "SYST:ROSC:SOUR", enum("INT", "EXT"), scoped=False
        ),
    }
    for modulation in ("AM", "FM", "PM"):
        key = modulation.lower()
        commands[key + "_enabled"] = command(key + "_enabled", p + modulation + ":STAT", BOOL, confirm=True)
        commands[key + "_source"] = command(key + "_source", p + modulation + ":SOUR", enum("INT", "EXT"))
        commands[key + "_frequency"] = command(
            key + "_frequency", p + modulation + ":INT:FREQ", parameter("Hz", 0.001, 100000)
        )
    commands["am_depth"] = command("am_depth", p + "AM:DEPT", parameter("%", 0, 100))
    commands["fm_deviation"] = command("fm_deviation", p + "FM:DEV", parameter("Hz", 0, maximum / 2))
    commands["pm_deviation"] = command("pm_deviation", p + "PM:DEV", parameter("deg", 0, 180))
    if tek:
        commands["frequency_mode"] = command(
            "frequency_mode", p + "FREQ:MODE", enum("CW", "SWE"), confirm=True
        )
        commands["trigger_source"] = command("trigger_source", "TRIG:SOUR", enum("TIM", "EXT"), scoped=False)
        commands["phase_sync"] = command("phase_sync", p + "PHAS:INIT", read=False, confirm=True)
    else:
        commands["sweep"] = command("sweep", p + "SWE:STAT", BOOL, confirm=True)
        commands["trigger_source"] = command(
            "trigger_source", p + "BURS:TRIG:SOUR", enum("INT", "EXT", "MAN")
        )
        commands["arb_enabled"] = command("arb_enabled", p + "FUNC:ADV:ARB:STAT", BOOL, confirm=True)
        commands["arb_sample_rate"] = command(
            "arb_sample_rate", p + "FUNC:ADV:ARB:SRAT", parameter("Sa/s", 1, 2.5e9)
        )
        commands["load_waveform"] = CommandSpec(
            Capability(
                "load_waveform",
                {"name": Parameter("text")},
                writable=True,
                channel_scoped=True,
                requires_confirmation=True,
                section=":SOURce:FUNCtion:ADVance:ARB:LOAD",
            )
        )
    commands["upload_waveform"] = CommandSpec(
        Capability(
            "upload_waveform",
            {
                "samples": Parameter("array", minimum=-1, maximum=1),
                "name": Parameter("text", required=not tek),
            },
            writable=True,
            channel_scoped=True,
            section="TRACe|DATA[:DATA]" if tek else ":MMEMory[:TRACe]:ARB:DATA",
            requires_confirmation=True,
        )
    )
    if tek:
        commands["select_uploaded_waveform"] = CommandSpec(
            Capability(
                "select_uploaded_waveform",
                writable=True,
                channel_scoped=True,
                requires_confirmation=True,
                section="SOURce:FUNCtion EMEMory",
            ),
            ("SOUR{ch}:FUNC EMEM{ch}",),
        )
    return Profile(
        adapter_id,
        ("TEKTRONIX",) if tek else ("RIGOL",),
        ("AFG31252",) if tek else ("DG5504 Pro", "DG5504Pro"),
        "signal_generator",
        (1, 2) if tek else (1, 2, 3, 4),
        AFG_SOURCE if tek else DG_SOURCE,
        "077-1488-00" if tek else "DG5000 Pro Programming Guide",
        commands,
        ("frequency", "amplitude", "offset", "output"),
    )


def sdg_field(raw: bytes, field: str) -> Any:
    text = raw.decode("ascii").strip()
    parts = text.split(" ", 1)[-1].split(",")
    for index, item in enumerate(parts[:-1]):
        if item.upper() == field:
            value = parts[index + 1]
            for suffix in ("HZ", "V", "S", "%"):
                if value.upper().endswith(suffix):
                    value = value[: -len(suffix)]
                    break
            try:
                return float(value)
            except ValueError:
                return value
    raise ValidationError(f"Missing {field} in SDG response")


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


def sma100b() -> Profile:
    commands = {}
    for name, path, param in (
        ("frequency", "SOUR:FREQ", parameter("Hz", 8000, 3e9)),
        ("power", "SOUR:POW", parameter("dBm", -120, 18)),
        ("frequency_mode", "SOUR:FREQ:MODE", enum("CW", "SWE", "LIST")),
        ("sweep_start", "SOUR:FREQ:STAR", parameter("Hz", 8000, 3e9)),
        ("sweep_stop", "SOUR:FREQ:STOP", parameter("Hz", 8000, 3e9)),
        ("sweep_spacing", "SOUR:SWE:FREQ:SPAC", enum("LIN", "LOG")),
        ("sweep_dwell", "SOUR:SWE:FREQ:DWEL", parameter("s", 0.001, 100)),
        ("reference_clock", "SOUR:ROSC:SOUR", enum("INT", "EXT")),
    ):
        commands[name] = command(name, path, param, scoped=False)
    commands["output"] = command("output", "OUTP", BOOL, scoped=False, confirm=True)
    for modulation in ("AM", "FM", "PM"):
        key = modulation.lower()
        commands[key + "_enabled"] = command(
            key + "_enabled", "SOUR:" + modulation + ":STAT", BOOL, scoped=False, confirm=True
        )
        commands[key + "_source"] = command(
            key + "_source", "SOUR:" + modulation + ":SOUR", enum("INT", "EXT"), scoped=False
        )
    commands["am_depth"] = command("am_depth", "SOUR:AM:DEPT", parameter("%", 0, 100), scoped=False)
    commands["fm_deviation"] = command("fm_deviation", "SOUR:FM:DEV", parameter("Hz", 0, 1e6), scoped=False)
    commands["pm_deviation"] = command("pm_deviation", "SOUR:PM:DEV", parameter("rad", 0, 20), scoped=False)
    commands["pulse_enabled"] = command(
        "pulse_enabled", "SOUR:PULM:STAT", BOOL, scoped=False, confirm=True, option="pulse_modulation"
    )
    for name, path, param in (
        ("list_mode", "SOUR:LIST:MODE", enum("AUTO", "STEP")),
        ("list_trigger_source", "SOUR:LIST:TRIG:SOUR", enum("AUTO", "SING", "EXT")),
        ("list_dwell", "SOUR:LIST:DWEL", parameter("s", 0.001, 100)),
    ):
        commands[name] = command(name, path, param, scoped=False)
    commands["list_trigger"] = command(
        "list_trigger", "SOUR:LIST:TRIG:EXEC", read=False, scoped=False, confirm=True
    )
    commands["upload_list"] = CommandSpec(
        Capability(
            "upload_list",
            {
                "name": Parameter("text"),
                "frequencies": Parameter("array", minimum=300000, maximum=3e9),
                "powers": Parameter("array", minimum=-120, maximum=18),
            },
            writable=True,
            requires_confirmation=True,
            section="15.18.8 LIST subsystem",
        )
    )
    return Profile(
        "rs.sma100b",
        ("Rohde&Schwarz", "Rohde-Schwarz"),
        ("SMA100B",),
        "signal_generator",
        (1,),
        SMA_SOURCE,
        "1178.3834.01 revision 14",
        commands,
        ("frequency", "power", "output"),
    )


class SignalGeneratorAdapter(ScpiAdapter):
    def validate_command(self, cmd: DeviceCommand) -> tuple[CommandSpec, dict[str, Any]]:
        spec, values = super().validate_command(cmd)
        if cmd.operation == "upload_waveform":
            samples = values["samples"]
            if not 32 <= len(samples) <= 131072:
                raise ValidationError("This upload implementation accepts 32..131072 samples")
        if "name" in values:
            import re

            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,23}", values["name"]):
                raise ValidationError("Name: ASCII letters/digits/underscore, max 24")
        if (
            cmd.operation == "upload_list"
            and not 1 <= len(values["frequencies"]) == len(values["powers"]) <= 10000
        ):
            raise ValidationError("Frequency/power lists must have the same 1..10000 length")
        return spec, values

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
            maximum = self.profile.commands["frequency"].capability.parameters["value"].maximum or carrier
            if parameters["value"] > min(carrier, maximum - carrier):
                raise ValidationError("FM deviation exceeds carrier/bandwidth margins")

    def encode(
        self, operation: str, spec: CommandSpec, context: dict[str, Any], parameters: dict[str, Any]
    ) -> tuple[bytes | str, ...]:
        if operation == "upload_list":
            return (
                f"SOUR:LIST:SEL '{parameters['name']}'",
                "SOUR:LIST:FREQ " + ",".join(f"{v:.12g}" for v in parameters["frequencies"]),
                "SOUR:LIST:POW " + ",".join(f"{v:.12g}" for v in parameters["powers"]),
            )
        if operation == "load_waveform":
            name, ch = parameters["name"], context["ch"]
            return (
                (f"C{ch}:ARWV NAME,{name}",)
                if self.adapter_id == "siglent.sdg6032x"
                else (f"SOUR{ch}:FUNC:ADV:ARB:LOAD INT:\\{name}.arb",)
            )
        if operation != "upload_waveform":
            return super().encode(operation, spec, context, parameters)
        samples = np.asarray(parameters["samples"])
        if self.adapter_id == "tek.afg31252":
            data = np.rint((samples + 1) * 8191).astype(">u2").tobytes()
            return (f"DATA:DATA EMEM{context['ch']},".encode() + ieee_block(data) + b"\n",)
        if self.adapter_id == "siglent.sdg6032x":
            data = np.rint(samples * 32767).astype("<i2").tobytes()
            return (f"C{context['ch']}:WVDT WVNM,{parameters['name']},WAVEDATA,".encode() + data,)
        name = parameters["name"]
        chunks = [samples[start : start + 512] for start in range(0, len(samples), 512)]
        return tuple(
            f"MMEM:TRAC:ARB:DATA INT:\\{name}.arb,"
            + ("END" if index == len(chunks) - 1 else "HEAD" if index == 0 else "CONT")
            + ","
            + ",".join(f"{value:.8g}" for value in chunk)
            for index, chunk in enumerate(chunks)
        )


PROFILES = (function_generator("tek.afg31252"), function_generator("rigol.dg5504pro"), sdg6032x(), sma100b())
