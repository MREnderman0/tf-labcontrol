"""AFG31252-only coupled limits, Tek 077148901 pp.2-10 and 077148803 pp.108-151.

Queries are deliberately made on every protected write, on its session worker.
No range query, unit change, output change, clamping or retry is implicit.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from lab_control.domain.instruments import DeviceCommand, Unsupported, ValidationError
from lab_control.instruments.base import number

FREQUENCIES = {
    "SIN": (1e-6, 250e6),
    "SQU": (1e-6, 160e6),
    "RAMP": (1e-6, 2.5e6),
    "PULS": (1e-3, 160e6),
    "EMEM": (1e-3, 125e6),
}
FM_MAXIMUM = {"SIN": 125e6, "SQU": 80e6, "RAMP": 1.25e6, "EMEM": 62.5e6}
SWEEP_WAVEFORMS = frozenset(FM_MAXIMUM)


def normalized_waveform(value: str) -> str:
    return {"PRN": "NOIS", "PRNOISE": "NOIS", "EMEM1": "EMEM", "EMEM2": "EMEM"}.get(value, value)


def bounds(waveform: str, burst: bool = False) -> tuple[float, float]:
    if waveform not in FREQUENCIES:
        raise Unsupported(f"{waveform}: no supported periodic frequency")
    low, high = FREQUENCIES[waveform]
    if burst and waveform in {"SIN", "EMEM"}:
        high /= 2
    return low, high


def within(name: str, value: float, low: float, high: float) -> None:
    # Relative roundoff only; an absolute epsilon would admit invalid nanosecond pulses.
    below = value < low and not math.isclose(value, low, rel_tol=1e-12, abs_tol=0)
    above = value > high and not math.isclose(value, high, rel_tol=1e-12, abs_tol=0)
    if not math.isfinite(value) or low > high or below or above:
        raise ValidationError(f"{name}: {value:g} outside {low:g}..{high:g}")


def load_factor(load: float) -> float:
    if load >= 9e37:  # Documented INFinity response is 9.9E+37, not an ordinary resistor.
        return 1.0
    within("load / ohm", load, 1, 10000)
    return load / (load + 50.0)


def pulse_bounds(period: float, leading: float, trailing: float) -> tuple[float, float]:
    within("pulse period / s", period, 1 / 160e6, 1000)
    within("leading edge / s", leading, 2e-9, 0.625 * period)
    within("trailing edge / s", trailing, 2e-9, 0.625 * period)
    # Use the stricter 4 ns value in the spec/programmer manuals, not the later 3 ns datasheet.
    low = max(4e-9, period * 0.00001, 0.625 * (leading + trailing))
    high = min(999.99, period * 0.99999, period - 0.8 * (leading + trailing))
    if low > high:
        raise ValidationError("No pulse width fits the current period and edge times")
    return low, high


class Limits:
    def __init__(
        self,
        query: Callable[[str], bytes],
        command: DeviceCommand,
        values: dict[str, Any],
        experiment: dict[str, float],
    ):
        self.query, self.command, self.values, self.experiment = query, command, values, experiment
        self.prefix = f"SOUR{command.channel}:"
        self.cache: dict[str, str] = {}

    def text(self, path: str, *, absolute: bool = False) -> str:
        query = (path if absolute else self.prefix + path) + "?"
        if query not in self.cache:
            self.cache[query] = self.query(query).decode("ascii").strip().strip('"').upper()
        return self.cache[query]

    def num(self, path: str, *, absolute: bool = False) -> float:
        return number(self.text(path, absolute=absolute))

    def enabled(self, path: str, *, absolute: bool = False) -> bool:
        value = self.num(path, absolute=absolute)
        if value not in (0, 1):
            raise ValidationError(f"Unknown boolean state for {path}: {value}")
        return bool(value)

    def uncoupled(self, groups: tuple[str, ...]) -> None:
        for ch in (1, 2):
            for group in groups:
                if self.enabled(f"SOUR{ch}:{group}:CONC", absolute=True):
                    raise ValidationError(f"Disable {group} channel coupling before independent control")

    def pulse(self, frequency: float, burst: bool, operation: str) -> None:
        period = 1.0 / frequency
        low, high = pulse_bounds(period, self.num("PULS:TRAN:LEAD"), self.num("PULS:TRAN:TRA"))
        if operation == "pulse_width":
            width = self.values["value"]
        elif operation == "pulse_duty":
            width = period * self.values["value"] / 100
        elif self.text("PULS:HOLD") == "DUTY":
            width = period * self.num("PULS:DCYC") / 100
        elif self.text("PULS:HOLD") == "WIDT":
            width = self.num("PULS:WIDT")
        else:
            raise ValidationError("Unknown pulse HOLD mode")
        within("pulse width / s", width, low, high)
        delay_max = (
            period - width - 0.8 * (self.num("PULS:TRAN:LEAD") + self.num("PULS:TRAN:TRA"))
            if burst
            else period
        )
        within("pulse delay / s", self.num("PULS:DEL"), 0, delay_max)

    def voltage(self, waveform: str, frequency: float, operation: str) -> None:
        if self.text("VOLT:UNIT") != "VPP":
            raise ValidationError("Select amplitude_unit VPP explicitly before changing related parameters")
        if self.text("COMB:FEED"):
            raise Unsupported("Disable additional noise/external signal before voltage-related control")
        old_factor = load_factor(self.num(f"OUTP{self.command.channel}:IMP", absolute=True))
        factor = load_factor(self.values["value"]) if operation == "load" else old_factor
        amplitude = self.values["value"] if operation == "amplitude" else self.num("VOLT")
        offset = self.values["value"] if operation == "offset" else self.num("VOLT:OFFS")
        ratio = factor / old_factor
        if operation == "load":
            if self.num(f"OUTP{self.command.channel}", absolute=True):
                raise ValidationError("Turn output OFF before changing load scaling")
            amplitude, offset = amplitude * ratio, offset * ratio
        if waveform == "DC":
            if operation == "amplitude":
                raise Unsupported("DC has no Vpp amplitude")
            amplitude = 0
        else:
            within("amplitude / Vpp", amplitude, 0.002 * factor, (8 if frequency > 200e6 else 10) * factor)
        within("offset plus peak amplitude / V", abs(offset) + amplitude / 2, 0, 5 * factor)
        high, low = self.num("VOLT:LIM:HIGH") * ratio, self.num("VOLT:LIM:LOW") * ratio
        within("output high / V", offset + amplitude / 2, low, high)
        within("output low / V", offset - amplitude / 2, low, high)
        for name, value in (("amplitude", amplitude), ("offset", offset), ("frequency", frequency)):
            if name in self.experiment and value > self.experiment[name]:
                raise ValidationError(f"{name} exceeds configured limit through a related setting")

    def validate(self) -> None:
        op = self.command.operation
        value: Any = self.values.get("value")
        if op == "output" and value is False:
            return
        if op in {"burst", "am_enabled", "fm_enabled", "pm_enabled"} and value is False:
            return
        if op == "frequency_mode" and value == "CW":
            return
        relevant = {
            "waveform",
            "select_uploaded_waveform",
            "frequency",
            "amplitude",
            "offset",
            "load",
            "pulse_width",
            "pulse_duty",
            "ramp_symmetry",
            "sweep_start",
            "sweep_stop",
            "sweep_time",
            "sweep_spacing",
            "frequency_mode",
            "burst",
            "burst_mode",
            "burst_cycles",
            "output",
            "phase",
            "phase_sync",
        }
        if op not in relevant and not op.startswith(("am_", "fm_", "pm_")):
            return
        self.uncoupled(("PHAS",) if op in {"phase", "phase_sync"} else ("FREQ", "VOLT"))
        if self.enabled("FSK:STAT") or self.enabled("PWM:STAT"):
            raise Unsupported("Active FSK/PWM is outside these correlated limits; disable it first")
        waveform = normalized_waveform(self.text("FUNC"))
        if op == "waveform":
            waveform = value
        elif op == "select_uploaded_waveform":
            waveform = "EMEM"
        if waveform not in {*FREQUENCIES, "NOIS", "DC"}:
            raise Unsupported(f"Unknown or unsupported waveform: {waveform}")
        burst = bool(value) if op == "burst" else self.enabled("BURS:STAT")
        mode = value if op == "frequency_mode" else self.text("FREQ:MODE")
        if mode not in {"CW", "SWE"}:
            raise Unsupported(f"Unknown frequency mode: {mode}")
        sweep = mode == "SWE"
        if op == "frequency" and sweep:
            raise ValidationError("Leave sweep mode before setting CW frequency")
        if (burst or op.startswith("burst")) and waveform in {"NOIS", "DC"}:
            raise Unsupported("Noise/DC cannot be used in burst mode")
        if op in {"phase", "phase_sync"} and waveform in {"NOIS", "DC", "PULS"}:
            raise Unsupported("This waveform has no standard phase setting")
        if op == "ramp_symmetry" and waveform != "RAMP":
            raise Unsupported("Select RAMP before changing ramp symmetry")
        if op in {"pulse_width", "pulse_duty"} and waveform != "PULS":
            raise Unsupported("Select PULS before changing pulse timing")
        frequency = value if op == "frequency" else self.num("FREQ")
        periodic = waveform in FREQUENCIES
        if sweep and burst:
            raise ValidationError("Sweep and burst cannot be enabled together")
        if op == "frequency" or periodic:
            within(f"{waveform} frequency / Hz", frequency, *bounds(waveform, burst))
        if sweep or op.startswith("sweep_"):
            if waveform not in SWEEP_WAVEFORMS:
                raise Unsupported("Sweep supports SIN/SQU/RAMP/edit waveforms only")
            start = value if op == "sweep_start" else self.num("FREQ:STAR")
            stop = value if op == "sweep_stop" else self.num("FREQ:STOP")
            for endpoint in (start, stop):
                within("sweep endpoint / Hz", endpoint, *bounds(waveform))
            if start > stop:
                raise ValidationError("Sweep start must not exceed stop; set endpoints in order")
            duration = value if op == "sweep_time" else self.num("SWE:TIME")
            within(
                "total sweep + hold + return time / s",
                duration + self.num("SWE:HTIM") + self.num("SWE:RTIM"),
                0.001,
                500,
            )
            if sweep:
                frequency = max(start, stop)
        if waveform == "PULS":
            self.pulse(frequency, burst, op)
        if op.startswith(("am_", "fm_", "pm_", "sweep_")) or op in {
            "waveform",
            "frequency",
            "select_uploaded_waveform",
            "output",
            "frequency_mode",
            "burst",
        }:
            for mode in ("am", "fm", "pm"):
                active = bool(value) if op == mode + "_enabled" else self.enabled(mode.upper() + ":STAT")
                editing = op.startswith(mode + "_")
                if active or editing:
                    if waveform not in SWEEP_WAVEFORMS:
                        raise Unsupported("AM/FM/PM carrier excludes pulse/noise/DC")
                    if active and (sweep or burst):
                        raise ValidationError("Exit sweep/burst before enabling modulation")
                    if editing and op.endswith("_frequency") and self.text(mode.upper() + ":SOUR") != "INT":
                        raise ValidationError("Internal modulation frequency requires INT source")
                    if mode == "fm" and (active or op == "fm_deviation"):
                        deviation = value if op == "fm_deviation" else self.num("FM:DEV")
                        low, high = bounds(waveform)
                        within(
                            "FM deviation / Hz",
                            deviation,
                            0,
                            min(FM_MAXIMUM[waveform], frequency - low, high - frequency),
                        )
        if op not in {
            "phase",
            "phase_sync",
            "ramp_symmetry",
            "pulse_width",
            "pulse_duty",
        } and not op.startswith(("am_", "fm_", "pm_")):
            self.voltage(waveform, frequency if periodic else 0, op)
