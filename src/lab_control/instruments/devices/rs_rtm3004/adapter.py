"""RTM3004 live ranges, explicit format preparation and full-frame checks."""

from copy import deepcopy
from dataclasses import replace
from math import isclose

from lab_control.domain.instruments import (
    ApplyResult,
    DeviceError,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
    utc_now,
)
from lab_control.domain.oscilloscope import ScopeDisplay
from lab_control.instruments.base import ScpiAdapter, number

from .autoset import apply_auto
from .profile import BOOL as BOOL
from .profile import CHANNEL_SETTINGS, GLOBAL_SETTINGS, RANGED, SOURCE
from .profile import rtm3004 as rtm3004
from .protocol import (
    POINTS_MAX,
    Header,
    decimal_rounding,
    growing_header,
    payload_values,
    rollover_header,
    waveform,
)

RTM_SOURCE = SOURCE  # Retained for the deprecated public profile import.


class Rtm3004Adapter(ScpiAdapter):
    supports_live_acquisition = True
    supports_stream_acquisition = True
    acquisition_revision = "rtm3004-real32-v1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._settings = {}
        self._stamps = {}
        self._ranges = {}
        self._context_cache = None
        self.acquisition_preparation = {}

    def read(self, operation, channel=1):
        if operation == "trigger_mode":
            state = super().read("acquisition_state")
            value = super().read("trigger_sweep") if state == "RUN" else "STOP"
        else:
            value = super().read(operation, channel)
        self._settings[f"{channel}.{operation}"] = value
        self._stamps[f"{channel}.{operation}"] = utc_now()
        return value

    def snapshot(self):
        values, stamps = {}, {}
        for ch, names in ((1, GLOBAL_SETTINGS), *((ch, CHANNEL_SETTINGS) for ch in self.profile.channels)):
            for name in names:
                key = f"{ch}.{name}"
                values[key] = self.read(name, ch)
                stamps[key] = self._stamps[key]
        return values, stamps

    def _range(self, operation, channel=1):
        query = self.profile.commands[operation].query.format(ch=channel)
        low = number(self.transport.query(query + " MIN"))
        high = number(self.transport.query(query + " MAX"))
        if low >= high:
            raise DeviceError("Invalid instrument parameter limits", "parse_error")
        self._ranges[channel, operation] = (low, high)
        return low, high

    def refresh_capabilities(self):
        self.invalidate_acquisition_context("capabilities")
        try:
            values, _ = self.snapshot()
            for name in RANGED:
                channels = (
                    self.profile.channels if self.profile.commands[name].capability.channel_scoped else (1,)
                )
                for ch in channels:
                    self._range(name, ch)
            self.capability_details.update(
                memory={"points": values["1.record_length"], "sample_rate": values["1.sample_rate"]},
                horizontal_divisions=values["1.horizontal_divisions"],
                acquisition_state=self.read("acquisition_state"),
            )
            self.capability_error = ""
        except Exception as exc:
            self.capability_error = str(exc)
            raise

    def capabilities_for_channel(self, channel):
        schema = super().capabilities_for_channel(channel)
        operations = []
        for cap in schema.operations:
            target = channel if cap.channel_scoped else 1
            bounds = self._ranges.get((target, cap.name))
            if bounds and "value" in cap.parameters:
                cap = replace(
                    cap,
                    parameters={
                        "value": replace(
                            cap.parameters["value"],
                            minimum=bounds[0],
                            maximum=bounds[1],
                            range_source="instrument",
                        )
                    },
                )
            if (
                cap.name in {"trigger_source", "trigger_level", "trigger_slope"}
                and self._settings.get("1.trigger_type", "EDGE") != "EDGE"
            ):
                cap = replace(cap, writable=False, reason="仅验证模拟边沿触发；高级触发保持不变")
            if self.capability_error:
                cap = replace(cap, writable=False, reason=self.capability_error)
            operations.append(cap)
        return replace(schema, operations=tuple(operations))

    def preflight(self, command, parameters):
        if command.operation in RANGED:
            self._range(command.operation, command.channel)
            self.capabilities_for_channel(command.channel).get(command.operation).validate(parameters)
        if (
            command.operation in {"trigger_source", "trigger_level", "trigger_slope"}
            and self.read("trigger_type") != "EDGE"
        ):
            raise Unsupported("仅验证模拟边沿触发")

    def complete_setting(self, command):
        if command.operation not in {"single", "run", "stop"}:
            if number(self.transport.query("*OPC?")) != 1:
                raise DeviceError("Setting completion unconfirmed; not replayed", "configuration_uncertain")

    def apply(self, command):
        self.invalidate_acquisition_context("apply")
        if command.operation == "auto_setup":
            return apply_auto(self, command)
        if command.operation not in {"trigger_mode", "run", "stop", "single"}:
            return super().apply(command)
        _, parameters = self.validate_command(command)
        target = parameters.get(
            "value", {"run": "RUN", "stop": "STOP", "single": "SINGLE"}.get(command.operation)
        )
        writes = (
            ("STOP",)
            if target == "STOP"
            else ("SING",)
            if target == "SINGLE"
            else ("RUN",)
            if target == "RUN"
            else (f"TRIG:A:MODE {target}", "RUN")
        )
        completed = 0
        try:
            for payload in writes:
                self.transport.write(payload)
                completed += 1
            state = self.read("acquisition_state")
            actual = self.read("trigger_sweep") if target in {"AUTO", "NORM"} else self.read("trigger_mode")
            error = self.transport.query("SYST:ERR?").decode("ascii").strip()
            okay = error.lstrip("+").startswith("0,") and (
                target in {"AUTO", "NORM"}
                and actual == target
                and state == "RUN"
                or target == "RUN"
                and state == "RUN"
                or target == "STOP"
                and state in {"STOP", "COMP", "BRE"}
                or target == "SINGLE"
                and state in {"RUN", "COMP"}
            )
            return ApplyResult(
                "success" if okay else "rejected",
                command.operation,
                parameters,
                actual,
                completed,
                "" if okay else error or "State mismatch",
            )
        except Exception as exc:
            return ApplyResult(
                "partial" if completed else "uncertain",
                command.operation,
                parameters,
                completed_steps=completed,
                error=str(exc),
            )

    def prepare_acquisition(self):
        self.invalidate_acquisition_context("connection_setup")
        before = {"format": self.read("transfer_format"), "byte_order": self.read("byte_order")}
        self.acquisition_preparation = {"before": before, "attempted": [], "after": {}, "state": "pending"}
        try:
            for name, command, target, operation in (
                ("format", "FORM REAL,32", "REAL,32", "transfer_format"),
                ("byte_order", "FORM:BORD LSBF", "LSBF", "byte_order"),
            ):
                if before[name] != target:
                    self.acquisition_preparation["attempted"].append(command)
                    self.transport.write(command)
                actual = self.read(operation)
                self.acquisition_preparation["after"][name] = actual
                if actual != target:
                    raise DeviceError("Waveform format readback differs", "readback_mismatch")
            self.acquisition_preparation["state"] = "passed"
        except Exception as exc:
            self.acquisition_preparation.update(state="failed", error=str(exc))
            raise DeviceError(
                f"RTM format initialization uncertain; not replayed: {exc}", "configuration_uncertain"
            ) from exc

    def _waveform_header(self, channel):
        try:
            return Header.parse(self.transport.query(f"CHAN{channel}:DATA:HEAD?"))
        except DeviceError as exc:
            if exc.code == "waveform_unavailable":
                state = self.read("acquisition_state")
                condition = "仪器已停止" if state != "RUN" else "仪器尚未产生该通道数据"
                raise DeviceError(
                    f"CH{channel} 尚无波形（{condition}）· 请先运行仪器采集，取得波形后重新开始监测",
                    "waveform_unavailable",
                ) from exc
            raise

    def invalidate_acquisition_context(self, reason):
        self._context_cache = None

    def acquire(self, request):
        try:
            return self._acquire(request)
        except Exception:
            self.invalidate_acquisition_context("error")
            raise

    def _acquire(self, request):
        request.validate()
        if request.kind != "waveform" or any(ch not in self.profile.channels for ch in request.channels):
            raise Unsupported("Only RTM3004 analog waveforms on channels 1-4 are supported")
        if request.initiate or request.waveform_extent != "default":
            raise Unsupported(
                "Use explicit instrument run/single controls; reads preserve the existing export window"
            )
        if self.identity is None:
            raise DeviceError("Identity required", "identity_required")
        cached_mode = request.consistency in {"live", "stream"}
        if cached_mode and not request.context_id:
            raise ValidationError("Cached acquisition requires a run context")
        context_key = (request.consistency, request.context_id, request.channels)
        context = self._context_cache
        reused = bool(cached_mode and context and context[0] == context_key)
        if reused:
            assert context is not None
            before, stamps = deepcopy(context[1]), dict(context[2])
        else:
            before, stamps = self.snapshot()
        if before["1.decimation"] == "PDET" or before["1.acquisition_mode"] == "ENV":
            raise Unsupported("Peak/envelope paired samples are not qualified")
        if any(not before[f"{ch}.channel_enabled"] for ch in request.channels):
            raise Unsupported("Selected channel is disabled")
        running_before = self.read("acquisition_state") == "RUN"
        if self.read("transfer_format") != "REAL,32" or self.read("byte_order") != "LSBF":
            raise DeviceError("Reconnect to prepare REAL32 little endian", "acquisition_not_prepared")
        # Check every selected channel before requesting any large data block.
        # An empty channel must not publish a partial multi-channel frame.
        headers = {ch: self._waveform_header(ch) for ch in request.channels}
        measurements = []
        signatures = {}
        for ch in request.channels:
            header = headers[ch] if ch == request.channels[0] else self._waveform_header(ch)
            origin_raw = self.transport.query(f"CHAN{ch}:DATA:XOR?")
            increment_raw = self.transport.query(f"CHAN{ch}:DATA:XINC?")
            origin, increment = number(origin_raw), number(increment_raw)
            bounded = getattr(self.transport, "query_binary", None)
            if not callable(bounded):
                raise Unsupported("RTM acquisition requires a bounded binary transport")
            # Running/roll acquisition can grow between HEAD? and DATA?.
            # Read the complete block within the model ceiling before deciding
            # whether a valid configuration transition invalidated this frame.
            raw = bounded(f"CHAN{ch}:DATA?", POINTS_MAX * 4)
            after_header = self._waveform_header(ch)
            progression = None
            rollover = None
            if after_header != header:
                values = payload_values(raw)  # Damage always remains a parse error.
                if running_before and after_header.points > header.points:
                    increment_after = number(self.transport.query(f"CHAN{ch}:DATA:XINC?"))
                    progression = growing_header(
                        header,
                        after_header,
                        values,
                        origin,
                        increment,
                        increment_after,
                        decimal_rounding(origin_raw)
                        + (after_header.points - 1) * decimal_rounding(increment_raw),
                    )
                elif running_before and not reused and after_header.points < header.points:
                    origin_after = number(self.transport.query(f"CHAN{ch}:DATA:XOR?"))
                    increment_after = number(self.transport.query(f"CHAN{ch}:DATA:XINC?"))
                    rollover = rollover_header(
                        header,
                        after_header,
                        values,
                        origin,
                        increment,
                        origin_after,
                        increment_after,
                        decimal_rounding(origin_raw) + (header.points - 1) * decimal_rounding(increment_raw),
                    )
                if progression is None and rollover is None:
                    raise DeviceError(
                        f"CH{ch} waveform header changed: {header} -> {after_header}", "configuration_changed"
                    )
            measured = waveform(
                raw,
                progression or rollover or header,
                origin,
                increment,
                ch,
                coordinate_error=decimal_rounding(origin_raw)
                + (header.points - 1) * decimal_rounding(increment_raw),
            )
            signatures[ch] = (origin, increment)
            if reused and context is not None and ch in context[3]:
                previous_origin, previous_increment = context[3][ch]
                if not isclose(increment, previous_increment, rel_tol=1e-9) or abs(
                    origin - previous_origin
                ) > 2 * max(increment, previous_increment):
                    raise DeviceError(
                        f"CH{ch} time grid changed from run snapshot: {context[3][ch]} -> {signatures[ch]}",
                        "configuration_changed",
                    )
            scale = before[f"{ch}.scale"]
            position = before[f"{ch}.vertical_position"]
            divisions = int(before["1.horizontal_divisions"])
            if divisions != before["1.horizontal_divisions"]:
                raise DeviceError("Non-integer horizontal division count", "parse_error")
            center = (
                before["1.time_position"]
                + (50 - before["1.time_reference"]) / 100 * divisions * before["1.timebase"]
            )
            measurements.append(
                replace(
                    measured,
                    metadata={
                        **measured.metadata,
                        "received_at": utc_now(),
                        "trigger_time_source": "unavailable",
                        "growing_prefix": progression is not None,
                        "sweep_rollover": rollover is not None,
                        "headers_observed": [header.__dict__, after_header.__dict__],
                        "scope_display": ScopeDisplay(
                            scale,
                            before[f"{ch}.offset"] - position * scale,
                            before["1.timebase"],
                            center,
                            horizontal_divisions=divisions,
                            vertical_position_divisions=position,
                        ).metadata(),
                    },
                )
            )
        after = before
        if not reused:
            after, stamps = self.snapshot()
        running_after = self.read("acquisition_state") == "RUN"
        changes = {
            key: {"before": value, "after": after.get(key)}
            for key, value in before.items()
            if after.get(key) != value
        }
        for name, expected in (("transfer_format", "REAL,32"), ("byte_order", "LSBF")):
            actual = self.read(name)
            if actual != expected:
                changes[name] = {"before": expected, "after": actual}
        if running_before != running_after:
            changes["instrument_running"] = {"before": running_before, "after": running_after}
        if changes:
            raise DeviceError(
                f"Configuration changed during waveform read: {changes}", "configuration_changed"
            )
        # A progressively filled window is not a stable descriptor context.
        # Keep the conservative full path there; cache stable windows only.
        if cached_mode and not any(
            m.metadata["growing_prefix"] or m.metadata["sweep_rollover"] for m in measurements
        ):
            self._context_cache = (context_key, deepcopy(after), dict(stamps), signatures)
        else:
            self.invalidate_acquisition_context("full_read")
        return MeasurementBatch(
            self.identity,
            tuple(measurements),
            after,
            validation="cached" if reused else "full",
            settings_timestamps=stamps,
            performance={
                "acquisition_context_id": request.context_id,
                "sequential_channels": True,
                "new_acquisitions": None,
                "instrument_running": running_after,
                "acquisition_revision": self.acquisition_revision,
            },
        )

    def acquisition_readings(self, batch):
        if batch.validation == "cached":
            return ()
        return tuple(
            ParameterReading(
                key.split(".", 1)[1],
                int(key.split(".", 1)[0]),
                value,
                "instrument",
                batch.settings_timestamps[key],
            )
            for key, value in batch.configuration.items()
            if key in batch.settings_timestamps
        )
