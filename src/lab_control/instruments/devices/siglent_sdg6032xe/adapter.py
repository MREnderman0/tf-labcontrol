"""Independent SDG6032X-E basic controls, EN06A and CN06A."""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from lab_control.domain.instruments import (
    DeviceCommand,
    DeviceError,
    DeviceStatus,
    Parameter,
    ParameterReading,
    Quality,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import Profile, ScpiAdapter, command, enum, parameter, read_command

from .protocol import FIELDS, basic, basic_value, body, decode_basic, fields, numeric, output, required

SOURCE = "https://siglentna.com/wp-content/uploads/dlm_uploads/2025/12/SDG6000X_ProgrammingGuide_EN06A.pdf"
DATASHEET = "https://www.siglent.com/wp-content/uploads/2025/03/SDG6000X_E_DataSheet_CN06A.pdf"
FREQUENCIES = {"SINE": 350e6, "SQUARE": 120e6, "RAMP": 5e6}


def sdg6032xe() -> Profile:
    commands = {}
    for name, param in (
        ("waveform", enum("SINE", "SQUARE", "RAMP", "DC")),
        ("frequency", parameter("Hz", 1e-6, 350e6)),
        ("amplitude", parameter("Vpp", 0.001, 20)),
        ("offset", parameter("V", -10, 10)),
        ("phase", parameter("deg", 0, 360)),
    ):
        commands[name] = replace(
            command(name, f"C{{ch}}:BSWV {FIELDS[name]},{{value}}", param, query="C{ch}:BSWV?"),
            writes=(f"C{{ch}}:BSWV {FIELDS[name]},{{value}}",),
            decode=partial(decode_basic, operation=name),
        )
    commands["output"] = command("output", "C{ch}:OUTP", Parameter("boolean"), confirm=True)
    commands["load"] = read_command("load", "C{ch}:OUTP?")
    for name, group in (("modulation", "MDWV"), ("sweep", "SWWV"), ("burst", "BTWV")):
        commands[name] = read_command(name, f"C{{ch}}:{group}?")
    for name, query in (("coupling", "COUP?"), ("phase_mode", "MODE?"), ("reference_clock", "ROSC?")):
        commands[name] = read_command(name, query, scoped=False)
    return Profile(
        "siglent.sdg6032xe",
        ("SIGLENT",),
        ("SDG6032X-E",),
        "signal_generator",
        (1, 2),
        SOURCE,
        "EN06A §§3.1–3.4, 3.15, 3.22–3.23; CN06A pp.8–11",
        commands,
        ("waveform", "frequency", "amplitude", "offset", "output", "load"),
        error_query="",
    )


class Sdg6032xeAdapter(ScpiAdapter):
    def read(self, operation: str, channel: int = 1):
        self._context(channel, {})
        self.capabilities().get(operation)
        if operation in FIELDS:
            return basic_value(basic(self.transport.query(f"C{channel}:BSWV?"), channel), operation)
        if operation in {"output", "load"}:
            return output(self.transport.query(f"C{channel}:OUTP?"), channel)[operation == "load"]
        if operation in {"modulation", "sweep", "burst"}:
            group = {"modulation": "MDWV", "sweep": "SWWV", "burst": "BTWV"}[operation]
            # Advanced payloads include nested carrier fields; only the leading STATE is read.
            text = body(self.transport.query(f"C{channel}:{group}?"), f"C{channel}:{group}")
            parts = text.split(",")
            if len(parts) < 2 or parts[0] != "STATE" or parts[1] not in {"ON", "OFF"}:
                raise DeviceError("Invalid SDG advanced-mode state", "parse_error")
            return parts[1]
        if operation == "reference_clock":
            value = body(self.transport.query("ROSC?"), "ROSC").split(",")[0]
            if value not in {"INT", "EXT"}:
                raise DeviceError("Invalid SDG clock source", "parse_error")
            return value
        return super().read(operation, channel)

    def read_status(self) -> DeviceStatus:
        result = {}
        for ch in self.profile.channels:
            values = basic(self.transport.query(f"C{ch}:BSWV?"), ch)
            for operation, field in FIELDS.items():
                if field in values:
                    result[f"{ch}.{operation}"] = basic_value(values, operation)
            result[f"{ch}.output"], result[f"{ch}.load"] = output(self.transport.query(f"C{ch}:OUTP?"), ch)
        return DeviceStatus(result)

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        readings: list[ParameterReading] = []
        for operation in operations:
            try:
                readings.extend(super().read_settings((operation,), channel))
            except Unsupported as exc:
                readings.append(
                    ParameterReading(
                        operation, channel, None, "unavailable", quality=Quality.STALE, error=str(exc)
                    )
                )
        return tuple(readings)

    def validate_command(self, cmd: DeviceCommand):
        spec, parameters = super().validate_command(cmd)

        # Bind readback to the selected channel, including CHDR OFF replies.
        def decode(raw: bytes):
            if cmd.operation in FIELDS:
                return basic_value(basic(raw, cmd.channel), cmd.operation)
            return output(raw, cmd.channel)[0]

        return replace(spec, decode=decode), parameters

    def preflight(self, cmd: DeviceCommand, parameters: dict) -> None:
        if cmd.operation == "output" and parameters["value"] is False:
            return
        coupling = fields(self.transport.query("COUP?"), "COUP")
        if any(required(coupling, key) != "OFF" for key in ("TRACE", "FCOUP", "PCOUP", "ACOUP")):
            raise ValidationError("Disable tracking/coupling before independent channel control")
        for name in ("modulation", "sweep", "burst"):
            if self.read(name, cmd.channel) != "OFF":
                raise ValidationError("Disable modulation/sweep/burst before basic control")
        state, load = output(self.transport.query(f"C{cmd.channel}:OUTP?"), cmd.channel)
        if load not in {"HZ", 50.0}:
            raise Unsupported("Write limits currently require HiZ or 50 ohm load")
        current = basic(self.transport.query(f"C{cmd.channel}:BSWV?"), cmd.channel)
        old_wave = current["WVTP"]
        waveform = parameters["value"] if cmd.operation == "waveform" else old_wave
        if old_wave not in {*FREQUENCIES, "DC"}:
            raise Unsupported(
                "Basic writes require SINE, SQUARE, RAMP or DC; advanced waveforms are read-only"
            )
        if cmd.operation == "waveform" and state != "OFF":
            raise ValidationError("Turn output OFF before waveform changes")
        if waveform == "DC" and cmd.operation in {"frequency", "amplitude", "phase"}:
            raise Unsupported("DC has no periodic frequency, amplitude or phase")
        # Leaving DC exposes hidden mode settings. Output stays OFF; subsequent
        # parameter writes/output ON validate the newly read state.
        if cmd.operation == "waveform" and old_wave == "DC":
            return
        frequency = (
            0.0
            if waveform == "DC"
            else (
                parameters["value"]
                if cmd.operation == "frequency"
                else numeric(required(current, "FRQ"), "HZ")
            )
        )
        if waveform != "DC" and not 1e-6 <= frequency <= FREQUENCIES[waveform]:
            raise ValidationError(f"{waveform} frequency exceeds its model limit")
        factor = 1.0 if load == "HZ" else 0.5
        ceiling = amplitude_ceiling(waveform, frequency) * factor
        amplitude = (
            0.0
            if waveform == "DC"
            else (
                parameters["value"]
                if cmd.operation == "amplitude"
                else numeric(required(current, "AMP"), "V")
            )
        )
        offset = parameters["value"] if cmd.operation == "offset" else numeric(required(current, "OFST"), "V")
        if waveform != "DC" and not 0.002 * factor <= amplitude <= ceiling:
            raise ValidationError("Amplitude exceeds waveform/frequency/load limits")
        # Conservative envelope at the current bandwidth; never rely on device clamping.
        if amplitude / 2 + abs(offset) > ceiling / 2:
            raise ValidationError("Amplitude/2 + abs(offset) exceeds the supported output envelope")
        if "MAX_OUTPUT_AMP" in current and amplitude > numeric(current["MAX_OUTPUT_AMP"], "V"):
            raise ValidationError("Amplitude exceeds the instrument's configured maximum")


def amplitude_ceiling(waveform: str, frequency: float) -> float:
    if waveform == "SINE":
        return 20 if frequency <= 40e6 else 10 if frequency <= 120e6 else 5 if frequency <= 160e6 else 3
    if waveform == "SQUARE":
        return 20 if frequency <= 20e6 else 10
    return 20
