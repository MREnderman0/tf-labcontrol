"""AFG31252 protocol differences and reversible edit-memory access.

Reference: Tektronix 077148803, FUNC, PM:DEV and TRACe|DATA chapters.
No helper resets the instrument, starts output, or writes nonvolatile memory.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import numpy as np

from lab_control.domain.instruments import (
    ApplyResult,
    Capability,
    DeviceCommand,
    DeviceError,
    Parameter,
    ValidationError,
)
from lab_control.instruments.base import CommandSpec, Profile, ScpiAdapter, command, enum, number, parameter
from lab_control.transport.base import MAX_MESSAGE, block_payload, ieee_block

from .limits import Limits

SOURCE = "https://www.tek.com/-/media/documents/077148803_afg31000_series_programmersmanual_nov2020.pdf"


def waveform_value(raw: bytes) -> str:
    value = raw.decode("ascii").strip().upper()
    return "NOIS" if value in {"PRN", "PRNOISE"} else value


def pm_degrees(raw: bytes) -> float:
    return math.degrees(number(raw))


BOOL = Parameter("boolean")


def base_profile() -> Profile:
    adapter_id = "tek.afg31252"
    maximum = 250000000.0
    p = "SOUR{ch}:"
    commands = {
        "waveform": command("waveform", p + "FUNC", enum("SIN", "SQU", "RAMP", "PULS", "NOIS", "DC")),
        "frequency": command("frequency", p + "FREQ", parameter("Hz", 1e-06, maximum)),
        "amplitude": command("amplitude", p + "VOLT", parameter("Vpp", 0.001, 10)),
        "offset": command("offset", p + "VOLT:OFFS", parameter("V", -5, 5)),
        "amplitude_unit": command("amplitude_unit", p + "VOLT:UNIT", enum("VPP", "VRMS", "DBM")),
        "phase": command("phase", p + "PHAS", parameter("rad", -math.pi, math.pi)),
        "pulse_duty": command("pulse_duty", p + "PULS:DCYC", parameter("%", 0.01, 99.99)),
        "pulse_width": command("pulse_width", p + "PULS:WIDT", parameter("s", 4e-09, 999.99)),
        "ramp_symmetry": command("ramp_symmetry", p + "FUNC:RAMP:SYMM", parameter("%", 0, 100)),
        "output": command("output", "OUTP{ch}", BOOL, confirm=True),
        "load": command("load", f"OUTP{{ch}}:{'IMP'}", parameter("ohm", 1, 10000)),
        "burst": command("burst", p + "BURS:STAT", BOOL, confirm=True),
        "burst_mode": command("burst_mode", p + "BURS:MODE", enum("TRIG", "GAT")),
        "burst_cycles": command(
            "burst_cycles", p + "BURS:NCYC", Parameter("integer", minimum=1, maximum=1000000)
        ),
        "sweep_time": command("sweep_time", p + "SWE:TIME", parameter("s", 0.001, 300)),
        "sweep_spacing": command("sweep_spacing", p + "SWE:SPAC", enum("LIN", "LOG")),
        "sweep_start": command("sweep_start", p + "FREQ:STAR", parameter("Hz", 1e-06, maximum)),
        "sweep_stop": command("sweep_stop", p + "FREQ:STOP", parameter("Hz", 1e-06, maximum)),
        "trigger": command("trigger", "*TRG", read=False, scoped=False, confirm=True),
        "reference_clock": command("reference_clock", "SOUR:ROSC:SOUR", enum("INT", "EXT"), scoped=False),
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
    commands["frequency_mode"] = command("frequency_mode", p + "FREQ:MODE", enum("CW", "SWE"), confirm=True)
    commands["trigger_source"] = command("trigger_source", "TRIG:SOUR", enum("TIM", "EXT"), scoped=False)
    commands["phase_sync"] = command("phase_sync", p + "PHAS:INIT", read=False, confirm=True)
    commands["upload_waveform"] = CommandSpec(
        Capability(
            "upload_waveform",
            {"samples": Parameter("array", minimum=-1, maximum=1), "name": Parameter("text", required=False)},
            writable=True,
            channel_scoped=True,
            section="TRACe|DATA[:DATA]",
            requires_confirmation=True,
        )
    )
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
        ("TEKTRONIX",),
        ("AFG31252",),
        "signal_generator",
        (1, 2),
        SOURCE,
        "077-1488-00",
        commands,
        ("frequency", "amplitude", "offset", "output"),
    )


def afg31252() -> Profile:
    profile = base_profile()
    commands = dict(profile.commands)
    commands["waveform"] = replace(commands["waveform"], decode=waveform_value)
    commands["pm_deviation"] = replace(
        commands["pm_deviation"], writes=("SOUR{ch}:PM:DEV {value}DEG",), decode=pm_degrees
    )
    commands["select_uploaded_waveform"] = replace(
        commands["select_uploaded_waveform"],
        capability=replace(commands["select_uploaded_waveform"].capability, readable=True),
        query="SOUR{ch}:FUNC?",
    )
    return replace(profile, commands=commands, source=SOURCE, revision="077148803")


def validate_edit_payload(payload: bytes) -> None:
    if not 2 <= len(payload) <= MAX_MESSAGE or len(payload) % 2:
        raise ValidationError("Edit memory must contain a complete sequence of 16-bit samples")
    if np.any(np.frombuffer(payload, dtype=">u2") > 16382):
        raise ValidationError("Edit memory contains codes outside 0..16382")


class Afg31252Adapter(ScpiAdapter):
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
        Limits(self.transport.query, cmd, parameters, self.config.limits).validate()

    def apply(self, command: DeviceCommand) -> ApplyResult:
        # Tek 077148901: phase resolution is 0.01 deg for sine, 0.1 deg
        # otherwise. 75W-61444-3 specifies 0.1 deg for PM deviation.
        # Retain the actual reading; only accept the documented half-step
        # plus rounding of the instrument's five-decimal radian response.
        tolerance = 0.0
        if command.operation in {"phase", "pm_deviation"}:
            self.validate_command(command)
            if command.operation == "phase":
                waveform = self.read("waveform", command.channel)
                tolerance = math.radians(0.005 if waveform == "SIN" else 0.05) + 0.000005
            else:
                tolerance = 0.05 + math.degrees(0.000005)
        result = super().apply(command)
        if (
            tolerance
            and result.outcome == "rejected"
            and result.error == "Readback differs from request; device may quantize or reject this value"
            and isinstance(result.readback, (int, float))
            and math.isclose(
                float(command.parameters["value"]), result.readback, rel_tol=0, abs_tol=tolerance
            )
        ):
            self.configuration[f"{command.channel}.{command.operation}"] = result.requested
            return replace(result, outcome="success", error="")
        return result

    def encode(
        self, operation: str, spec: CommandSpec, context: dict[str, Any], parameters: dict[str, Any]
    ) -> tuple[bytes | str, ...]:
        if operation == "upload_waveform":
            samples = np.asarray(parameters["samples"])
            data = np.rint((samples + 1) * 8191).astype(">u2").tobytes()
            return (f"DATA:DATA EMEM{context['ch']},".encode() + ieee_block(data) + b"\n",)
        if operation == "waveform" and parameters["value"] == "NOIS":
            return (f"SOUR{context['ch']}:FUNC PRNOISE",)
        return super().encode(operation, spec, context, parameters)

    def read_edit_memory(self, channel: int) -> bytes:
        self._context(channel, {})
        payload = block_payload(self.transport.query(f"DATA:DATA? EMEM{channel}"))
        validate_edit_payload(payload)
        return payload

    def restore_edit_memory(self, channel: int, payload: bytes) -> None:
        """Restore previously backed-up raw codes, including memories larger than upload limits."""
        self._context(channel, {})
        validate_edit_payload(payload)
        for ch in self.profile.channels:
            if number(self.transport.query(f"OUTP{ch}?")) != 0:
                raise ValidationError("Both outputs must be OFF before restoring edit memory")
        self.transport.write(f"DATA:DATA EMEM{channel},".encode("ascii") + ieee_block(payload) + b"\n")
        error = self.transport.query("SYST:ERR?").decode("ascii").strip()
        if error.split(",", 1)[0].strip() not in {"0", "+0"}:
            raise DeviceError(f"Edit memory restore rejected: {error}", "device_error")
        if self.read_edit_memory(channel) != payload:
            raise DeviceError("Edit memory restore readback differs from backup", "parse_error")
