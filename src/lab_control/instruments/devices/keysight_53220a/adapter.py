"""Single-input, bounded binary frequency acquisition and verified parameter control."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    Measurement,
    MeasurementBatch,
    Parameter,
    ParameterReading,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import ScpiAdapter

from .profile import INPUT_SETTINGS
from .protocol import frequency_block, frequency_channel, numeric, token

# Every setting changed by preparation or SENS:FUNC (Programmer's Reference p322).
# Input level2 is deliberately absent: it is unavailable for frequency mode.
GLOBAL_STATE = (
    "SENS:FREQ:MODE",
    "SENS:FREQ:GATE:SOUR",
    "SENS:FREQ:GATE:TIME",
    "TRIG:SOUR",
    "TRIG:SLOP",
    "TRIG:DEL",
    "TRIG:COUN",
    "SAMP:COUN",
    "SENS:GATE:STAR:SOUR",
    "SENS:GATE:STAR:SLOP",
    "SENS:GATE:STAR:DEL:SOUR",
    "SENS:GATE:STAR:DEL:TIME",
    "SENS:GATE:STOP:SOUR",
    "SENS:GATE:STOP:SLOP",
    "SENS:GATE:STOP:HOLD:SOUR",
    "SENS:GATE:STOP:HOLD:TIME",
    "CALC:SCAL:STAT",
    "CALC:SMO:STAT",
    "CALC:LIM:STAT",
    "CALC:AVER:STAT",
    "CALC:STAT",
    "CALC2:TRAN:HIST:STAT",
    "SYST:TIM",
    "FORM:DATA",
    "FORM:BORD",
)
INPUT_STATE = ("PROB", "RANG", "IMP", "COUP", "FILT", "NREJ", "SLOP", "LEV:REL", "LEV", "LEV:AUTO")
OBSERVATIONS = ("SENS:ROSC:SOUR", "SENS:ROSC:SOUR:AUTO", "SENS:ROSC:EXT:FREQ", "OUTP:STAT")


class Keysight53220aAdapter(ScpiAdapter):
    acquisition_revision = "53220a-frequency-v1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.acquisition_preparation: dict[str, Any] = {}
        self._prepared_channel: int | None = None
        self._input_ranges: dict[int, tuple[float, float]] = {}

    def identify(self):
        identity = super().identify()
        if self.config.expected_serial and identity.serial != self.config.expected_serial:
            raise DeviceError("Serial number does not match selected asset", "identity_conflict")
        if not identity.key:
            raise DeviceError("53220A did not return a trustworthy serial", "identity_conflict")
        self.detected_options = tuple(token(self.transport.query("*OPT?")).split(","))
        self.capability_details.update(
            acquisition_channel_selection="single",
            supported_inputs=[1, 2],
            optional_input_supported=False,
        )
        return identity

    def refresh_capabilities(self):
        for channel in (1, 2):
            self.read("input_range", channel)
            self.read("probe_factor", channel)
        self.capability_details["active_channel"] = frequency_channel(self.read("configuration"))

    def capabilities_for_channel(self, channel: int) -> CapabilitySet:
        caps = super().capabilities_for_channel(channel)
        input_range, probe = self._input_ranges.get(channel, (5.0, 1.0))
        result = []
        for cap in caps.operations:
            if cap.name == "trigger_level":
                cap = replace(
                    cap,
                    parameters={
                        "value": Parameter(
                            unit="V",
                            minimum=-input_range * 41 / 40,
                            maximum=input_range * 41 / 40,
                            resolution=input_range / 2000,
                        )
                    },
                )
            elif cap.name == "input_range":
                cap = replace(
                    cap,
                    parameters={
                        "value": Parameter(
                            unit="V",
                            minimum=5 * probe,
                            maximum=50 * probe,
                            choices=tuple(str(v * probe) for v in (5.0, 50.0)),
                        )
                    },
                )
            result.append(cap)
        return replace(caps, operations=tuple(result))

    def read(self, operation: str, channel: int = 1) -> Any:
        value = super().read(operation, channel)
        old_range, old_probe = self._input_ranges.get(channel, (5.0, 1.0))
        if operation == "input_range":
            self._input_ranges[channel] = (value, old_probe)
        elif operation == "probe_factor":
            self._input_ranges[channel] = (old_range, value)
        return value

    def read_settings(self, operations: tuple[str, ...], channel: int = 1):
        readings = super().read_settings(operations, channel)
        return tuple(replace(r, source="unavailable") if r.value is None else r for r in readings)

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        value, ch = parameters["value"], command.channel
        if command.operation == "input_range":
            probe = self.read("probe_factor", ch)
            if value not in (5 * probe, 50 * probe):
                raise ValidationError("Range does not match the current probe factor")
            current_level = self.read("trigger_level", ch)
            if current_level is not None and abs(current_level) > value * 41 / 40:
                raise ValidationError("Lower the manual trigger level before reducing the range")
        if command.operation == "trigger_level":
            input_range = self.read("input_range", ch)
            if self.read("auto_level", ch):
                raise ValidationError("Disable automatic level before setting a manual level")
            step = input_range / 2000
            if abs(value) > input_range * 41 / 40 or not math.isclose(
                value / step, round(value / step), abs_tol=1e-7
            ):
                raise ValidationError(f"Trigger level must fit ±{input_range * 1.025:g} V, step {step:g} V")
        if command.operation == "gate_time" and not math.isclose(
            value / 1e-5, round(value / 1e-5), abs_tol=1e-5
        ):
            raise ValidationError("53220A gate time requires a 10 us step")

    def complete_setting(self, command: DeviceCommand) -> None:
        if numeric(self.transport.query("*OPC?")) != 1:
            raise DeviceError("Setting completion was not confirmed", "readback_mismatch")
        self._prepared_channel = None
        if command.operation == "input_range":
            self.read("input_range", command.channel)

    def check_error(self) -> None:
        error = token(self.transport.query("SYST:ERR?"))
        if error.split(",", 1)[0] not in {"0", "+0"}:
            raise DeviceError(f"53220A: {error}", "instrument_error")

    def snapshot(self) -> dict[str, str]:
        result = {"SENS:FUNC": token(self.transport.query("SENS:FUNC?"))}
        for path in (
            *GLOBAL_STATE,
            *(f"INP{ch}:{suffix}" for ch in (1, 2) for suffix in INPUT_STATE),
            *OBSERVATIONS,
        ):
            result[path] = token(self.transport.query(path + "?"))
        return result

    def write_setting(self, path: str, value: str) -> None:
        # Only locally declared setting paths; no arbitrary learn-string replay.
        if any(c in value for c in '\r\n;"'):
            raise ValidationError("Invalid setting reply for restoration")
        self.transport.write(f"{path} {value}")
        actual = token(self.transport.query(path + "?"))
        try:
            matches = math.isclose(float(actual), float(value), rel_tol=1e-10, abs_tol=1e-12)
        except ValueError:
            matches = actual == value or (
                value in {"ON", "OFF"} and actual == ("1" if value == "ON" else "0")
            )
        if not matches:
            raise DeviceError(f"{path}: requested {value}, returned {actual}", "readback_mismatch")
        self.check_error()

    def restore(self, original: dict[str, str]) -> dict[str, Any]:
        """Restore only declared settings; never restart an old acquisition or enable an output."""
        self.transport.write("ABOR")
        current = self.snapshot()
        if current["SENS:FUNC"] != original["SENS:FUNC"]:
            if frequency_channel(original["SENS:FUNC"]) is None:
                raise Unsupported("Restoring another measurement function requires a separate plan")
            self.set_function(frequency_channel(original["SENS:FUNC"]))
        for path in GLOBAL_STATE:
            if token(self.transport.query(path + "?")) != original[path]:
                self.write_setting(path, original[path])
        for ch in (1, 2):
            for suffix in INPUT_STATE:
                path = f"INP{ch}:{suffix}"
                # Auto-level has no reproducible absolute value. Restore its mode/relative level.
                if suffix == "LEV" and original[f"INP{ch}:LEV:AUTO"] in {"1", "+1", "ON"}:
                    continue
                if token(self.transport.query(path + "?")) != original[path]:
                    self.write_setting(path, original[path])
        final = self.snapshot()
        differences = self.snapshot_differences(original, final)
        self._prepared_channel = None
        return {
            "settings_match": not differences,
            "differences": differences,
            "snapshot": final,
            "acquisition": "stopped; pre-existing in-flight measurement is not restarted",
        }

    @staticmethod
    def snapshot_differences(original: dict[str, str], final: dict[str, str]) -> dict[str, Any]:
        return {
            key: {"before": value, "after": final.get(key)}
            for key, value in original.items()
            if final.get(key) != value
            and not (
                key == "SENS:FUNC"
                and frequency_channel(value) is not None
                and frequency_channel(value) == frequency_channel(final[key])
            )
            and not (key in {"INP1:LEV", "INP2:LEV"} and original[key + ":AUTO"] in {"1", "+1", "ON"})
        }

    def prepare_acquisition(self) -> dict[str, Any]:
        # The explicit connection hook enables future starts, but does not configure the counter.
        return self.acquisition_preparation

    def set_function(self, channel: int | None) -> None:
        if channel not in (1, 2):
            raise ValidationError("Frequency function requires CH1 or CH2")
        self.transport.write(f'SENS:FUNC "FREQ {channel}"')
        if frequency_channel(self.read("configuration")) != channel:
            raise DeviceError("Function readback differs from request", "readback_mismatch")
        self.check_error()

    def prepare_frequency(self, channel: int) -> None:
        before = self.snapshot()
        self.acquisition_preparation = {"before": before, "commands": [], "complete": False}
        self.capability_details["acquisition_preparation"] = self.acquisition_preparation
        if frequency_channel(before["SENS:FUNC"]) is None:
            raise Unsupported(
                "Select frequency measurement on the instrument before starting this frequency-only adapter"
            )
        if before["OUTP:STAT"] not in {"0", "+0", "OFF"}:
            raise Unsupported("Gate output is enabled; frequency preparation would change it")
        if token(self.transport.query("SYST:LANG?")) != "53220A":
            raise Unsupported("53220A native SCPI mode is required; language was not changed")
        timeout = numeric(self.transport.query("SYST:TIM?"))
        if not 0.01 <= timeout <= 2000:
            raise ValidationError("Use a finite instrument measurement timeout")
        self.transport.write("ABOR")
        if frequency_channel(before["SENS:FUNC"]) != channel:
            command = f'SENS:FUNC "FREQ {channel}"'
            self.acquisition_preparation["commands"].append(command)
            self.set_function(channel)
        settings = {
            "SENS:FREQ:GATE:TIME": before["SENS:FREQ:GATE:TIME"],
            "SENS:FREQ:GATE:SOUR": "TIME",
            "TRIG:SOUR": "IMM",
            "TRIG:DEL": "0",
            "TRIG:COUN": "1",
            "SAMP:COUN": "1",
            "CALC:STAT": "0",
            "CALC2:TRAN:HIST:STAT": "0",
            "FORM:DATA": "REAL,64",
            "FORM:BORD": "NORM",
        }
        for path, value in settings.items():
            self.acquisition_preparation["commands"].append(f"{path} {value}")
            self.write_setting(path, value)
        self._prepared_channel = channel
        self.acquisition_preparation.update(complete=True, after=self.snapshot())

    def acquisition_state(self, channel: int) -> dict[str, Any]:
        state = {
            name: self.read(name, channel if name in INPUT_SETTINGS else 1)
            for name in ("configuration", "gate_time", "transfer_format", "byte_order", *INPUT_SETTINGS)
        }
        # Absolute automatic level follows the signal and is not a setting change.
        if state["auto_level"]:
            state.pop("trigger_level")
        for path in (
            "TRIG:SOUR",
            "TRIG:COUN",
            "TRIG:DEL",
            "SAMP:COUN",
            "CALC:STAT",
            "SENS:FREQ:GATE:SOUR",
            "SENS:FREQ:MODE",
            *OBSERVATIONS,
            f"INP{channel}:PROB",
            f"INP{channel}:NREJ",
            f"INP{channel}:PROT",
        ):
            state[path] = token(self.transport.query(path + "?"))
        return state

    def acquisition_readings(self, batch: MeasurementBatch) -> tuple[ParameterReading, ...]:
        if batch.validation != "full" or not batch.measurements:
            return ()
        channel = batch.measurements[0].channel
        return tuple(
            ParameterReading(
                name,
                1 if name == "gate_time" else channel,
                batch.configuration.get(name),
                "instrument" if name in batch.configuration else "unavailable",
                timestamp=batch.timestamp,
            )
            for name in ("gate_time", *INPUT_SETTINGS)
        )

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind != "scalar" or request.quantity != "frequency" or request.points != 1:
            raise Unsupported("53220A supports single scalar frequency readings only")
        if len(request.channels) != 1 or request.channels[0] not in (1, 2):
            raise ValidationError("53220A requires exactly one of CH1/CH2")
        if not request.initiate or not request.confirmed:
            raise DeviceError(
                "Starting a fresh counter measurement requires explicit initiation", "confirmation_required"
            )
        if self.identity is None:
            raise DeviceError("Identify before acquisition", "identity_required")
        channel = request.channels[0]
        if self._prepared_channel != channel:
            self.prepare_frequency(channel)
        before = self.acquisition_state(channel)
        if (
            frequency_channel(before["configuration"]) != channel
            or before["transfer_format"] != "REAL,64"
            or before["TRIG:SOUR"] != "IMM"
            or float(before["TRIG:COUN"]) != 1
            or float(before["SAMP:COUN"]) != 1
            or float(before["CALC:STAT"]) != 0
            or float(before["TRIG:DEL"]) != 0
            or not 0.0001 <= before["gate_time"] <= 1000
            or before["SENS:FREQ:GATE:SOUR"] != "TIME"
        ):
            self._prepared_channel = None
            raise DeviceError(
                "Counter measurement settings changed; stop and prepare again", "not_configured"
            )
        timeout = numeric(self.transport.query("SYST:TIM?"))
        if not 0.01 <= timeout <= 2000:
            raise ValidationError("Infinite measurement timeout is not supported")
        self.transport.write("INIT:IMM")
        bounded = getattr(self.transport, "query_binary_timeout", None)
        binary = getattr(self.transport, "query_binary", None)
        raw = (
            bounded("DATA:REM? 1,WAIT", 8, before["gate_time"] + timeout + 5)
            if callable(bounded)
            else binary("DATA:REM? 1,WAIT", 8)
            if callable(binary)
            else self.transport.query("DATA:REM? 1,WAIT")
        )
        try:
            value = frequency_block(raw, before["byte_order"])
        except DeviceError as exc:
            if exc.code == "overload":
                error = token(self.transport.query("SYST:ERR?"))
                if error.split(",", 1)[0] not in {"0", "+0", "321", "+321"}:
                    raise DeviceError(f"53220A: {error}", "instrument_error") from exc
            raise
        after = self.acquisition_state(channel)
        if before != after:
            self._prepared_channel = None
            changed = {
                k: {"before": v, "after": after.get(k)} for k, v in before.items() if after.get(k) != v
            }
            raise DeviceError(f"Frequency configuration changed: {changed}", "configuration_changed")
        self.check_error()
        return MeasurementBatch(
            self.identity,
            (
                Measurement(
                    "frequency",
                    "Hz",
                    (value,),
                    channel=channel,
                    metadata={"gate_time_s": before["gate_time"], "channel": channel, "format": "REAL,64"},
                ),
            ),
            {**before, "acquisition_preparation": self.acquisition_preparation},
            validation="full",
            time_basis="host_utc_at_read; no instrument timestamp",
        )
