"""SDG2082X controls; SDG programming guide and EN02H data sheet.

The model has 80 MHz sine, 25 MHz square/pulse and 1 MHz ramp limits.
Amplitude is Vpp regardless of the front-panel amplitude display unit.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

from lab_control.domain.instruments import (
    ApplyResult,
    DeviceCommand,
    DeviceError,
    DeviceStatus,
    Parameter,
    ParameterReading,
    Quality,
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

from .advanced import ADVANCED_FIELDS, BASIC_FIELDS, GROUPS, advanced_commands
from .protocol import (
    field,
    fields,
    parse_waveform,
    reference_source,
    user_waveforms,
    waveform_bytes,
    waveform_name,
)

SOURCE = "https://siglentna.com/wp-content/uploads/dlm_uploads/2023/01/SDG_Programming-Guide_PG02-E05A-12.pdf"
DATASHEET = "https://siglentna.com/wp-content/uploads/dlm_uploads/2025/11/SDG2000X_DataSheet_EN02H.pdf"
FREQUENCY_LIMITS = {"SINE": 80e6, "SQUARE": 25e6, "PULSE": 25e6, "RAMP": 1e6, "ARB": 20e6}
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
    commands["reference_clock"] = replace(
        command("reference_clock", "ROSC", enum("INT", "EXT"), scoped=False), decode=reference_source
    )
    commands.update(advanced_commands())
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
        spec, values = super().validate_command(cmd)
        if cmd.operation in {"upload_waveform", "load_waveform"}:
            waveform_name(values["name"])
        if cmd.operation == "upload_waveform":
            waveform_bytes(values["samples"])
        return spec, values

    def waveform_data(self, name: str) -> tuple[dict[str, Any], bytes]:
        waveform_name(name)
        return parse_waveform(self.transport.query(f"WVDT? USER,{name}"), name)

    def apply(self, cmd: DeviceCommand) -> ApplyResult:
        if cmd.operation not in {"upload_waveform", "load_waveform"}:
            result = super().apply(cmd)
            # EN02H: frequency resolution 1 uHz; pulse duty resolution 0.001%.
            # Preserve the actual instrument reading and the original request.
            quantum = {"frequency": 1e-6, "sweep_start": 1e-6, "sweep_stop": 1e-6, "pulse_duty": 0.001}.get(
                cmd.operation
            )
            if (
                quantum is not None
                and result.outcome == "rejected"
                and result.error.startswith("Readback differs from request;")
                and isinstance(result.readback, (int, float))
                and abs(result.requested["value"] - result.readback) <= quantum / 2 + quantum * 1e-9
            ):
                self.configuration[f"{cmd.channel}.{cmd.operation}"] = result.requested
                return replace(result, outcome="success", error="")
            return result
        _, values = self.validate_command(cmd)
        self.preflight(cmd, values)
        name = values["name"]
        catalog = user_waveforms(self.transport.query("STL? USER"))
        if cmd.operation == "upload_waveform" and name in catalog:
            raise ValidationError("Refusing to overwrite an existing user waveform")
        if cmd.operation == "load_waveform" and name not in catalog:
            raise ValidationError("User waveform does not exist")
        completed = 0
        try:
            if cmd.operation == "upload_waveform":
                data = waveform_bytes(values["samples"])
                self.transport.write(f"C{cmd.channel}:WVDT WVNM,{name},WAVEDATA,".encode() + data)
                completed = 1
                _, actual = self.waveform_data(name)
                if actual != data:
                    return ApplyResult(
                        "rejected",
                        cmd.operation,
                        values,
                        completed_steps=1,
                        error="Uploaded waveform bytes differ from readback",
                    )
                readback: Any = {"name": name, "points": len(actual) // 2, "bytes_verified": True}
            else:
                self.transport.write(f"C{cmd.channel}:ARWV NAME,{name}")
                completed = 1
                readback = field(self.transport.query(f"C{cmd.channel}:ARWV?"), "NAME")
                if readback != name:
                    return ApplyResult(
                        "rejected", cmd.operation, values, readback, 1, "Waveform selection mismatch"
                    )
            self.configuration[f"{cmd.channel}.{cmd.operation}"] = values
            return ApplyResult("success", cmd.operation, values, readback, completed)
        except DeviceError as exc:
            return ApplyResult(
                "partial" if completed else "uncertain",
                cmd.operation,
                values,
                completed_steps=completed,
                error=str(exc),
            )

    def read_status(self) -> DeviceStatus:
        values: dict[str, Any] = {}
        for ch in self.profile.channels:
            raw = self.transport.query(f"C{ch}:BSWV?")
            waveform = sdg_field(raw, "WVTP")
            values[f"{ch}.waveform"] = waveform
            for name, key in FIELDS.items():
                if name == "waveform" or (waveform in {"DC", "NOISE"} and name != "offset"):
                    continue
                if waveform == "NOISE":
                    continue
                values[f"{ch}.{name}"] = sdg_field(raw, key)
            output = self.transport.query(f"C{ch}:OUTP?")
            values[f"{ch}.output"] = output_state(output)
            values[f"{ch}.load"] = load_value(output)
        return DeviceStatus(values)

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        if cmd.operation == "reference_clock":
            # Device-wide timebase selection does not change channel modes or 10MOUT.
            return
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
        if cmd.operation in {
            *GROUPS,
            *ADVANCED_FIELDS,
            *BASIC_FIELDS,
            "modulation_type",
            "upload_waveform",
            "load_waveform",
        } or cmd.operation.startswith(("am_", "fm_", "pm_")):
            self.preflight_advanced(cmd, parameters)
            return
        raw = self.transport.query(f"C{cmd.channel}:BSWV?")
        if cmd.operation == "waveform" and sdg_field(raw, "WVTP") in {"DC", "NOISE"}:
            # These modes omit periodic settings. Change mode only with output OFF,
            # then use the instrument's newly visible settings for subsequent checks.
            if output_state(self.transport.query(f"C{cmd.channel}:OUTP?")) != "OFF":
                raise ValidationError("Turn output OFF before leaving DC or NOISE mode")
            self.require_basic_mode(cmd.channel)
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
        if cmd.operation == "frequency" and waveform == "PULSE":
            self.check_pulse(fields(raw), float(frequency), "pulse_width", float(field(raw, "WIDTH")))
        self.require_basic_mode(cmd.channel)

    def require_basic_mode(self, channel: int) -> None:
        for group in GROUPS.values():
            if field(self.transport.query(f"C{channel}:{group}?"), "STATE") != "OFF":
                raise ValidationError("Disable advanced modes before changing basic waveform parameters")

    @staticmethod
    def check_pulse(basic: dict, frequency: float, name: str, value: float) -> None:
        period = 1 / frequency
        width = value if name == "pulse_width" else period * value / 100
        minimum = 16.3e-9 if basic["WVTP"] == "PULSE" else 8e-9
        edge = (float(basic.get("RISE", 0)) + float(basic.get("FALL", 0))) / 2
        if min(width, period - width) < max(minimum, edge):
            raise ValidationError("Pulse width/duty violates period or rise/fall constraints")

    def check_carrier_envelope(self, ch: int, carrier: dict, highest_frequency: float) -> None:
        load = load_value(self.transport.query(f"C{ch}:OUTP?"))
        if load not in ("HZ", 50.0):
            raise Unsupported("Advanced limits require HiZ or 50 ohm load")
        factor = 1.0 if load == "HZ" else 0.5
        maximum = (20 if highest_frequency <= 20e6 else 10) * factor
        amplitude, offset = float(carrier["AMP"]), float(carrier["OFST"])
        if not 0.002 * factor <= amplitude <= maximum or amplitude / 2 + abs(offset) > 10 * factor:
            raise ValidationError("Advanced frequency range exceeds the carrier amplitude/load envelope")

    def preflight_advanced(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        name, ch = cmd.operation, cmd.channel
        value: Any = parameters.get("value")
        basic = fields(self.transport.query(f"C{ch}:BSWV?"))
        waveform = basic["WVTP"]
        if name in BASIC_FIELDS:
            if name in {"pulse_duty", "pulse_width"} and waveform not in (
                {"PULSE"} if name == "pulse_width" else {"SQUARE", "PULSE"}
            ):
                raise ValidationError("Pulse parameter requires the matching basic waveform")
            if name == "ramp_symmetry" and waveform != "RAMP":
                raise ValidationError("Symmetry requires RAMP")
            if name == "phase" and waveform not in {"SINE", "SQUARE", "RAMP", "ARB"}:
                raise ValidationError("Phase is not available for this waveform")
            if name in {"pulse_width", "pulse_duty"}:
                self.check_pulse(basic, float(basic["FRQ"]), name, float(value))
            self.require_basic_mode(ch)
            return
        if name in {"upload_waveform", "load_waveform"}:
            if output_state(self.transport.query(f"C{ch}:OUTP?")) != "OFF":
                raise ValidationError("Turn output OFF before waveform storage/selection")
            for group in GROUPS.values():
                if field(self.transport.query(f"C{ch}:{group}?"), "STATE") != "OFF":
                    raise ValidationError("Disable advanced modes before waveform storage/selection")
            return
        group = GROUPS.get(name, ADVANCED_FIELDS.get(name, ("MDWV", ""))[0])
        current = fields(self.transport.query(f"C{ch}:{group}?"))
        if name in GROUPS:
            if not value:
                return
            allowed = {"SINE", "SQUARE", "RAMP", "ARB"} | ({"PULSE"} if name == "burst" else set())
            if waveform not in allowed:
                raise ValidationError("Carrier waveform is not supported for this mode")
            if name == "burst" and float(basic["FRQ"]) < 0.002:
                raise ValidationError("Burst carrier frequency must be at least 2 mHz")
            for other in GROUPS.values():
                if other != group and field(self.transport.query(f"C{ch}:{other}?"), "STATE") != "OFF":
                    raise ValidationError("Disable the other advanced modes explicitly first")
            return
        if current.get("STATE") != "ON":
            raise ValidationError(f"Explicitly enable {group} before setting its parameters")
        carrier = current.get("CARR", basic)
        kind = carrier["WVTP"]
        maximum = FREQUENCY_LIMITS.get(kind, 0)
        if name.startswith(("am_", "fm_", "pm_")):
            mode = name[:2].upper()
            if current.get("MODE") != mode:
                raise ValidationError(f"Select modulation_type {mode} first")
            if not name.endswith("_source") and current.get("SRC") != "INT":
                raise ValidationError("Internal modulation source required")
            if name == "fm_deviation" and float(value) > min(
                float(carrier["FRQ"]), maximum - float(carrier["FRQ"]), maximum / 2
            ):
                raise ValidationError("FM deviation exceeds carrier/bandwidth margins")
            if name == "fm_deviation":
                self.check_carrier_envelope(ch, carrier, float(carrier["FRQ"]) + float(value))
        if name in {"sweep_start", "sweep_stop"}:
            start = float(value) if name == "sweep_start" else float(current["START"])
            stop = float(value) if name == "sweep_stop" else float(current["STOP"])
            if not 1e-6 <= start <= stop <= maximum:
                raise ValidationError("Sweep endpoints violate order or waveform bandwidth")
            self.check_carrier_envelope(ch, carrier, stop)
        if name in {"burst_cycles", "burst_delay"} and current.get("GATE_NCYC") != "NCYC":
            raise ValidationError("Select counted burst mode first")
        if name == "burst_period" and current.get("TRSR") != "INT":
            raise ValidationError("Burst period readback requires internal trigger on SDG2082X")
