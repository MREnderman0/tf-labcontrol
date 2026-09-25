"""DG5504 Pro controls with fresh correlated limits and no write retries."""

from __future__ import annotations

from typing import Any

from lab_control.domain.instruments import DeviceCommand, DeviceError, Unsupported, ValidationError
from lab_control.instruments.base import CommandSpec, ScpiAdapter, number, text_value

from .protocol import FREQUENCIES, amplitude_envelope, catalog, state, validate_name

MODEL_QUERIES = {
    "track": "TRAC",
    "frequency_coupling": "FREQ:COUP:STAT",
    "amplitude_coupling": "VOLT:COUP:STAT",
    "phase_coupling": "PHAS:COUP:STAT",
    "advanced": "FUNC:ADV:STAT",
    "square_fast": "FUNC:SQU:TRAN:STAT",
}


class Dg5504ProAdapter(ScpiAdapter):
    def read(self, operation: str, channel: int = 1) -> Any:
        if operation in MODEL_QUERIES:
            self._context(channel, {})
            return text_value(self.transport.query(f"SOUR{channel}:{MODEL_QUERIES[operation]}?"))
        if operation == "amplitude" and str(super().read("amplitude_unit", channel)).upper() != "VPP":
            raise Unsupported("Amplitude readback requires VPP; select the unit explicitly")
        return super().read(operation, channel)

    def validate_command(self, cmd: DeviceCommand) -> tuple[CommandSpec, dict[str, Any]]:
        spec, values = super().validate_command(cmd)
        if "name" in values:
            validate_name(values["name"])
        if cmd.operation == "upload_waveform" and not 32 <= len(values["samples"]) <= 131072:
            raise ValidationError("This uploader supports 32..131072 points (implementation limit)")
        return spec, values

    def independent(self) -> None:
        # The benchmark can change a different channel: inspect ALL four paths.
        for ch in self.profile.channels:
            if str(self.read("track", ch)).upper() != "OFF":
                raise ValidationError("Disable all channel tracking before independent control")
            for name in ("frequency_coupling", "amplitude_coupling", "phase_coupling"):
                if state(self.read(name, ch)):
                    raise ValidationError("Disable all channel coupling before independent control")

    def bounds(self, operation: str, channel: int) -> tuple[float, float]:
        query = self.profile.commands[operation].query.format(ch=channel)
        try:
            low, high = (number(self.transport.query(query + " " + suffix)) for suffix in ("MIN", "MAX"))
            if low > high:
                raise DeviceError("Inverted DG dynamic limits", "parse_error")
            self.capability_details[f"{channel}.{operation}.range"] = [low, high]
            return low, high
        except Exception:
            self.capability_details.pop(f"{channel}.{operation}.range", None)
            raise

    def frequency_limit(self, waveform: str, channel: int, modulated: bool = False) -> float:
        if waveform not in FREQUENCIES:
            raise Unsupported("Frequency control is not applicable to this waveform")
        maximum = FREQUENCIES[waveform]
        if waveform == "SQU" and not state(self.read("square_fast", channel)):
            maximum = 120e6
        if modulated:
            maximum = {"SIN": 350e6, "SQU": 120e6, "RAMP": 2.5e6, "PULS": 120e6}[waveform]
        configured = self.config.limits.get("frequency", maximum)
        return min(maximum, configured)

    def voltage_check(self, ch: int, waveform: str, frequency: float, values: dict[str, Any]) -> None:
        if str(self.read("amplitude_unit", ch)).upper() != "VPP":
            raise ValidationError("Select amplitude_unit VPP before changing waveform/frequency/voltage")
        load = values.get("load", self.read("load", ch))
        minimum, maximum = amplitude_envelope(frequency, waveform, load)
        amplitude = 0.0 if waveform == "DC" else float(values.get("amplitude", self.read("amplitude", ch)))
        offset = float(values.get("offset", self.read("offset", ch)))
        if waveform != "DC" and not minimum <= amplitude <= maximum:
            raise ValidationError("DG amplitude exceeds waveform/frequency/load limits")
        # DC offset follows the DC limits, independent of the hidden frequency.
        if waveform == "DC":
            maximum = 20 if load == "INF" else 10
        if amplitude / 2 + abs(offset) > maximum / 2 + 1e-12:
            raise ValidationError("DG amplitude/2 + abs(offset) exceeds load/frequency envelope")
        if amplitude > self.config.limits.get("amplitude", float("inf")):
            raise ValidationError("Amplitude exceeds experiment limit")
        if offset > self.config.limits.get("offset", float("inf")):
            raise ValidationError("Offset exceeds experiment limit")

    def pulse_check(self, ch: int, frequency: float, operation: str, value: Any) -> None:
        period = 1 / frequency
        width = float(value) if operation == "pulse_width" else float(self.read("pulse_width", ch))
        if operation == "pulse_duty":
            width = period * float(value) / 100
        if not 4.2e-9 <= width <= period - 4.2e-9:
            raise ValidationError("Pulse width/duty exceeds the target period limits")
        if operation in {"frequency", "waveform"}:
            # The guide says changing period can silently adjust both edges and width.
            # Guard both possible width/duty holding behaviours before that command.
            duty_width = period * float(self.read("pulse_duty", ch)) / 100
            edge = sum(
                number(self.transport.query(f"SOUR{ch}:FUNC:PULS:TRAN:{path}?")) for path in ("LEAD", "TRA")
            )
            if not 4.2e-9 <= duty_width <= period - 4.2e-9 or edge > min(
                width, period - width, duty_width, period - duty_width
            ):
                raise ValidationError("Pulse period may clamp the saved width or edges; adjust them first")

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        op, ch = cmd.operation, cmd.channel
        value: Any = parameters.get("value")
        if op == "output" and value is False:
            return
        self.independent()
        if op == "upload_waveform":
            if (parameters["name"] + ".arb").casefold() in {name.casefold() for name in self.file_catalog()}:
                raise ValidationError("Refusing to overwrite an existing waveform file")
            return
        if op == "load_waveform":
            if parameters["name"] + ".arb" not in self.file_catalog():
                raise ValidationError("Waveform file does not exist")
            if state(self.read("output", ch)):
                raise ValidationError("Turn output OFF before loading a waveform")
            if not state(self.read("advanced", ch)) or not state(self.read("arb_enabled", ch)):
                raise ValidationError("Select Advanced Arb mode explicitly before loading a file")
            return
        if op in {"trigger", "reference_clock", "amplitude_unit", "arb_enabled", "arb_sample_rate"}:
            if op == "arb_sample_rate":
                # SRAT? has no MIN/MAX form in the programming guide, §3.12.9.4.
                if not state(self.read("advanced", ch)) or not state(self.read("arb_enabled", ch)):
                    raise ValidationError("Select Advanced Arb mode before setting sample rate")
            if op == "arb_enabled" and value and not state(self.read("advanced", ch)):
                raise Unsupported(
                    "Advanced mode transitions have not been verified; Arb enable is unavailable"
                )
            return
        if state(self.read("advanced", ch)):
            raise ValidationError("Exit Advanced mode explicitly before basic controls")
        waveform = str(self.read("waveform", ch)).upper()
        modulations = {
            name + "_enabled": state(self.read(name + "_enabled", ch)) for name in ("am", "fm", "pm")
        }
        active_mod = any(modulations.values())
        burst = state(self.read("burst", ch))
        mode_names = {"sweep", "burst", *modulations}
        if op in mode_names and value:
            modes = {**modulations, "burst": burst, "sweep": state(self.read("sweep", ch))}
            if any(enabled for name, enabled in modes.items() if name != op):
                raise ValidationError("Disable the current mode before selecting another mode")
        if op == "waveform":
            waveform = str(value)
        frequency = float(value) if op == "frequency" else float(self.read("frequency", ch))
        if op in {"waveform", "frequency", "output"} and waveform not in {"DC", "NOIS"}:
            if not 1e-6 <= frequency <= self.frequency_limit(waveform, ch, active_mod or burst):
                raise ValidationError("Frequency outside target waveform/mode limits; no automatic clamping")
        if op == "frequency" and waveform in {"DC", "NOIS"}:
            raise Unsupported("DC/noise has no periodic frequency control")
        if op == "amplitude" and waveform == "DC":
            raise Unsupported("DC has no Vpp amplitude")
        if op in {"waveform", "frequency", "amplitude", "offset", "load", "output"}:
            self.voltage_check(ch, waveform, frequency, {op: value})
        if op in {"pulse_duty", "pulse_width"} and waveform != "PULS":
            raise ValidationError("Pulse waveform required")
        if waveform == "PULS" and op in {"frequency", "waveform", "pulse_width", "pulse_duty", "output"}:
            self.pulse_check(ch, frequency, op, value)
        if op == "ramp_symmetry" and waveform != "RAMP":
            raise ValidationError("Ramp waveform required")
        if op in {"sweep_start", "sweep_stop"}:
            if not 1e-6 <= value <= self.frequency_limit(waveform, ch):
                raise ValidationError("Sweep endpoint outside waveform/experiment limits")
            self.voltage_check(ch, waveform, float(value), {})
        if op == "sweep" and value or op == "output" and state(self.read("sweep", ch)):
            for name in ("sweep_start", "sweep_stop"):
                endpoint = float(self.read(name, ch))
                if not 1e-6 <= endpoint <= self.frequency_limit(waveform, ch):
                    raise ValidationError("Saved sweep endpoint exceeds waveform/experiment limits")
                self.voltage_check(ch, waveform, endpoint, {})
        if op in {"am_enabled", "fm_enabled", "pm_enabled", "burst"} and value:
            if waveform not in {"SIN", "SQU", "RAMP"} and not (op == "burst" and waveform == "PULS"):
                raise ValidationError("Unsupported carrier waveform for this mode")
            if frequency > self.frequency_limit(waveform, ch, True):
                raise ValidationError("Carrier exceeds modulation/burst frequency limits")
            if waveform == "SQU" and state(self.read("square_fast", ch)):
                raise ValidationError("Mode change would implicitly disable fast square edges")
        if (
            op == "fm_deviation"
            or (op == "fm_enabled" and value)
            or (op in {"waveform", "frequency", "output"} and modulations["fm_enabled"])
        ):
            deviation = float(value) if op == "fm_deviation" else float(self.read("fm_deviation", ch))
            if deviation > min(frequency - 1e-6, self.frequency_limit(waveform, ch, True) - frequency):
                raise ValidationError("FM deviation exceeds carrier/bandwidth margins")
        if op.startswith(("am_", "fm_", "pm_")) and op.endswith(("frequency", "shape")):
            if str(self.read(op[:2] + "_source", ch)).upper() != "INT":
                raise ValidationError("Select internal modulation source first")
        # Every writable numeric limit below has a documented MIN/MAX query.
        # Never retain a stale range or fall back after a failed hardware query.
        numeric = {
            "frequency",
            "amplitude",
            "offset",
            "phase",
            "pulse_width",
            "pulse_duty",
            "ramp_symmetry",
            "sweep_start",
            "sweep_stop",
            "sweep_time",
            "am_depth",
            "fm_deviation",
            "pm_deviation",
            "am_frequency",
            "fm_frequency",
            "pm_frequency",
            "load",
            "burst_cycles",
        }
        if op in numeric:
            low, high = self.bounds(op, ch)
            if not low <= value <= high:
                raise ValidationError(f"{op} outside current DG hardware limits [{low:g}, {high:g}]")

    def file_catalog(self) -> dict[str, int | None]:
        return catalog(self.transport.query("MMEM:CAT? INT:\\"))

    def synchronization(self) -> dict[str, Any]:
        # Programming guide §3.11.2: BUNDle? requires a CH<n> argument.
        return {
            "benchmark": self.transport.query("SYNC:BENC?").decode().strip(),
            "bundle": {
                str(ch): state(self.transport.query(f"SYNC:BUND? CH{ch}")) for ch in self.profile.channels
            },
        }

    def delete_created_file(self, name: str, created: set[str]) -> None:
        validate_name(name)
        if name not in created:
            raise ValidationError("Only this run's newly created files may be deleted")
        self.transport.write(f"MMEM:DEL INT:\\{name}.arb")
        error = self.transport.query("SYST:ERR?").decode().strip()
        if not error.startswith(("0,", "+0,")) or name + ".arb" in self.file_catalog():
            raise DeviceError(f"Waveform cleanup failed: {error}", "restoration_failed")
        created.remove(name)

    def encode(
        self, operation: str, spec: CommandSpec, context: dict[str, Any], parameters: dict[str, Any]
    ) -> tuple[bytes | str, ...]:
        if operation == "trigger_source":
            return (
                f"TRIG{context['ch']}:SOUR "
                + {"INT": "IMM", "MAN": "BUS", "EXT": "EXT"}[parameters["value"]],
            )
        if operation == "load_waveform":
            return (f"SOUR{context['ch']}:FUNC:ADV:ARB:LOAD INT:\\{parameters['name']}.arb",)
        if operation == "upload_waveform":
            samples = parameters["samples"]
            chunks = [samples[start : start + 512] for start in range(0, len(samples), 512)]
            return tuple(
                f"MMEM:TRAC:ARB:DATA INT:\\{parameters['name']}.arb,"
                + ("END" if i == len(chunks) - 1 else "HEAD" if i == 0 else "CONT")
                + ","
                + ",".join(f"{v:.8g}" for v in chunk)
                for i, chunk in enumerate(chunks)
            )
        return super().encode(operation, spec, context, parameters)
