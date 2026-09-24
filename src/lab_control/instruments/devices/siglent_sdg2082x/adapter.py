"""SDG2082X basic controls; SDG programming guide and EN02H data sheet.

The model has 80 MHz sine, 25 MHz square/pulse and 1 MHz ramp limits.
Amplitude is Vpp regardless of the front-panel amplitude display unit.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

from lab_control.domain.instruments import (
    DeviceCommand,
    DeviceError,
    DeviceStatus,
    Parameter,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import (
    Profile,
    ScpiAdapter,
    command,
    enum,
    number,
    parameter,
    read_command,
)
from lab_control.instruments.common.siglent import sdg_field

SOURCE = "https://siglentna.com/wp-content/uploads/dlm_uploads/2023/01/SDG_Programming-Guide_PG02-E05A-12.pdf"
DATASHEET = "https://siglentna.com/wp-content/uploads/dlm_uploads/2025/11/SDG2000X_DataSheet_EN02H.pdf"
FREQUENCY_LIMITS = {"SINE": 80e6, "SQUARE": 25e6, "PULSE": 25e6, "RAMP": 1e6}
FIELDS = {"waveform": "WVTP", "frequency": "FRQ", "amplitude": "AMP", "offset": "OFST"}


def output_state(raw: bytes) -> str:
    # CHDR OFF omits the response header. Both forms are documented.
    body = raw.decode("ascii").strip().split(" ", 1)[-1]
    value = body.split(",", 1)[0].upper()
    if value not in {"ON", "OFF"}:
        raise DeviceError(f"Invalid SDG output response: {body!r}", "parse_error")
    return value


def load_value(raw: bytes) -> float | str:
    parts = raw.decode("ascii").strip().split(" ", 1)[-1].split(",")
    fields = [part.strip().upper() for part in parts]
    try:
        value = fields[fields.index("LOAD") + 1]
    except (ValueError, IndexError) as exc:
        raise DeviceError("Missing LOAD in SDG output response", "parse_error") from exc
    return "HZ" if value in {"HZ", "HIZ"} else number(value)


def sdg2082x() -> Profile:
    commands = {}
    for name, spec in (
        ("waveform", enum("SINE", "SQUARE", "RAMP", "PULSE", "DC")),
        ("frequency", parameter("Hz", 1e-6, 80e6)),
        ("amplitude", parameter("Vpp", 0.001, 20)),
        ("offset", parameter("V", -10, 10)),
    ):
        field = FIELDS[name]
        commands[name] = replace(
            command(name, "C{ch}:BSWV", spec, query="C{ch}:BSWV?"),
            writes=(f"C{{ch}}:BSWV {field},{{value}}",),
            decode=partial(sdg_field, field=field),
        )
    commands["output"] = replace(
        command("output", "C{ch}:OUTP", Parameter("boolean"), confirm=True), decode=output_state
    )
    commands["load"] = replace(read_command("load", "C{ch}:OUTP?"), decode=load_value)
    for name, group in (("sweep", "SWWV"), ("burst", "BTWV"), ("modulation", "MDWV")):
        commands[name] = replace(
            read_command(name, f"C{{ch}}:{group}?"), decode=partial(sdg_field, field="STATE")
        )
    commands["coupling"] = read_command("coupling", "COUP?", scoped=False)
    return Profile(
        "siglent.sdg2082x",
        ("SIGLENT",),
        ("SDG2082X",),
        "signal_generator",
        (1, 2),
        SOURCE,
        "PG02-E05A / SDG2000X EN02H pp. 7-9",
        commands,
        ("waveform", "frequency", "amplitude", "offset", "output", "load"),
        error_query="",
    )


class Sdg2082xAdapter(ScpiAdapter):
    def read_status(self) -> DeviceStatus:
        values: dict[str, Any] = {}
        for ch in self.profile.channels:
            raw = self.transport.query(f"C{ch}:BSWV?")
            waveform = sdg_field(raw, "WVTP")
            values[f"{ch}.waveform"] = waveform
            for name, field in FIELDS.items():
                if name == "waveform" or (waveform in {"DC", "NOISE"} and name != "offset"):
                    continue
                if waveform == "NOISE":
                    continue
                values[f"{ch}.{name}"] = sdg_field(raw, field)
            output = self.transport.query(f"C{ch}:OUTP?")
            values[f"{ch}.output"] = output_state(output)
            values[f"{ch}.load"] = load_value(output)
        return DeviceStatus(values)

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        # OFF is always possible, including when another mode or coupling is active.
        if cmd.operation == "output" and not parameters["value"]:
            return
        coupling = self.transport.query("COUP?")
        if any(
            str(sdg_field(coupling, name)).upper() != "OFF" for name in ("TRACE", "FCOUP", "PCOUP", "ACOUP")
        ):
            raise ValidationError("Disable channel tracking/coupling before independent SDG2082X control")
        if cmd.operation == "output":
            return
        raw = self.transport.query(f"C{cmd.channel}:BSWV?")
        if cmd.operation == "waveform" and sdg_field(raw, "WVTP") in {"DC", "NOISE"}:
            # These modes omit periodic settings. Change mode only with output OFF,
            # then use the instrument's newly visible settings for subsequent checks.
            if output_state(self.transport.query(f"C{cmd.channel}:OUTP?")) != "OFF":
                raise ValidationError("Turn output OFF before leaving DC or NOISE mode")
            return
        waveform = parameters["value"] if cmd.operation == "waveform" else sdg_field(raw, "WVTP")
        if waveform not in (*FREQUENCY_LIMITS, "DC"):
            raise Unsupported("Select SINE, SQUARE, RAMP, PULSE or DC before changing basic settings")
        if waveform == "DC" and cmd.operation in {"frequency", "amplitude"}:
            raise Unsupported("DC has no frequency or Vpp amplitude")
        frequency = 0.0
        if waveform != "DC":
            frequency = parameters["value"] if cmd.operation == "frequency" else sdg_field(raw, "FRQ")
            if not 1e-6 <= frequency <= FREQUENCY_LIMITS[waveform]:
                raise ValidationError(f"{waveform} frequency must be <= {FREQUENCY_LIMITS[waveform]:g} Hz")
        load = load_value(self.transport.query(f"C{cmd.channel}:OUTP?"))
        if load not in ("HZ", 50.0):
            raise Unsupported("Basic write limits are verified for HiZ and 50 ohm loads only")
        factor = 1.0 if load == "HZ" else 0.5
        amplitude = (
            0.0
            if waveform == "DC"
            else (parameters["value"] if cmd.operation == "amplitude" else sdg_field(raw, "AMP"))
        )
        offset = parameters["value"] if cmd.operation == "offset" else sdg_field(raw, "OFST")
        maximum = (20 if frequency <= 20e6 else 10) * factor
        if waveform != "DC" and not 0.002 * factor <= amplitude <= maximum:
            raise ValidationError(
                f"Amplitude must be {0.002 * factor:g}..{maximum:g} Vpp at this frequency/load"
            )
        if amplitude / 2 + abs(offset) > 10 * factor:
            raise ValidationError("Amplitude/2 + abs(offset) exceeds the SDG2082X load envelope")
