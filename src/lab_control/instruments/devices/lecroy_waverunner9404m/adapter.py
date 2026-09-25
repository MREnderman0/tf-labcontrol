"""Independent WaveRunner 9404M controls and complete-frame acquisition."""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    DeviceCommand,
    DeviceError,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
    utc_now,
)
from lab_control.instruments.base import ScpiAdapter
from lab_control.transport.diagnostics import parsing

from .context import AcquisitionContext, fingerprint
from .protocol import grouped_scalars, waveform

CHANNEL_SETTINGS = (
    "channel_enabled",
    "scale",
    "offset",
    "coupling",
    "probe_attenuation",
    "trigger_level",
    "trigger_slope",
)
GLOBAL_SETTINGS = (
    "timebase",
    "time_position",
    "trigger_mode",
    "record_length",
    "trigger_setup",
    "sequence_setup",
    "interleaved",
    "transfer_format",
    "response_header",
    "byte_order",
    "waveform_setup",
)
TRANSFER_SETTINGS = ("response_header", "transfer_format", "byte_order", "waveform_setup")


def equivalent(expected: Any, actual: Any) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        # Readback serialization precision, not a physical measurement tolerance.
        return isinstance(actual, (int, float)) and math.isclose(
            expected, actual, rel_tol=1e-6, abs_tol=1e-15
        )
    return expected == actual


class WaveRunner9404MAdapter(ScpiAdapter):
    acquisition_revision = "waverunner9404m-context-v2"
    # Qualified with full WORD frames on firmware 9.2.0; see communication-research.md.
    supports_live_acquisition = True
    supports_stream_acquisition = True
    _acquisition_context: AcquisitionContext | None = None

    def invalidate_acquisition_context(self, reason: str) -> None:
        self._acquisition_context = None

    def snapshot(self, channels: tuple[int, ...] = (1, 2, 3, 4)) -> tuple[dict[str, Any], dict[str, str]]:
        values: dict[str, Any] = {}
        stamps: dict[str, str] = {}
        for channel, operations in ((1, GLOBAL_SETTINGS), *((ch, CHANNEL_SETTINGS) for ch in channels)):
            if self.grouped_snapshot_queries():
                queries = tuple(self.profile.commands[op].query.format(ch=channel) for op in operations)
                raw = grouped_scalars(self.transport.query(";".join(queries)), queries)
                stamp = utc_now()
                for operation, field in zip(operations, raw, strict=True):
                    key = f"{channel}.{operation}"
                    values[key] = self.profile.commands[operation].decode(field)
                    stamps[key] = stamp
                continue
            for operation in operations:
                key = f"{channel}.{operation}"
                values[key] = self.read(operation, channel)
                stamps[key] = utc_now()
        return values, stamps

    def grouped_snapshot_queries(self) -> bool:
        # Select before I/O, never retry or fall back after a compound message fails.
        prepared = getattr(self, "acquisition_preparation", {})
        return bool(
            self.identity
            and self.identity.firmware == "9.2.0"
            and self.config.transport == "visa"
            and prepared.get("status") == "ready"
            and prepared.get("before", {}).get("response_header") == "SHORT"
        )

    def prepare_acquisition(self) -> None:
        self.invalidate_acquisition_context("connection_setup")
        if self.identity is None:
            raise DeviceError("Identify before acquisition setup", "identity_required")
        info: dict[str, Any] = {"started_at": utc_now(), "status": "reading", "format_write_attempted": False}
        self.acquisition_preparation = info
        try:
            before = {key: self.read(key) for key in TRANSFER_SETTINGS}
            info["before"] = before
            if before["transfer_format"] != "DEF9,WORD,BIN":
                info["format_write_attempted"] = True
                self.transport.write("CFMT DEF9,WORD,BIN")
            after = {key: self.read(key) for key in TRANSFER_SETTINGS}
            info["after"] = after
            if after != {**before, "transfer_format": "DEF9,WORD,BIN"}:
                raise DeviceError("Transfer initialization readback differs", "readback_mismatch")
            info.update(status="ready", finished_at=utc_now())
        except Exception as exc:
            info.update(status="failed", error=str(exc), finished_at=utc_now())
            raise

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        # No model protocol is placed in the common adapter/session layer.
        op, channel = command.operation, command.channel
        if op in {"scale", "offset", "coupling", "probe_attenuation"}:
            coupling = self.read("coupling", channel)
            attenuation = self.read("probe_attenuation", channel)
            scale = self.read("scale", channel)
            offset = self.read("offset", channel)
            if attenuation <= 0:
                raise DeviceError("Invalid probe attenuation", "parse_error")
            target_coupling = parameters["value"] if op == "coupling" else coupling
            # Datasheet input sensitivity; ATTN changes displayed probe-tip units.
            input_scale = (parameters["value"] if op == "scale" else scale) / attenuation
            maximum = 1.0 if target_coupling == "D50" else 10.0
            if not 0.001 <= input_scale <= maximum:
                raise ValidationError("Vertical scale exceeds the selected input coupling/probe range")
            input_offset = (parameters["value"] if op == "offset" else offset) / attenuation
            # Conservative shared subset of the documented 50-ohm offset ranges.
            # Do not infer BWL-dependent limits from a scalar VDIV value.
            if abs(input_offset) > 1.4:
                raise ValidationError(
                    "Offset exceeds the qualified ±1.4 V input subset; preserve current state"
                )

    def apply(self, command: DeviceCommand) -> ApplyResult:
        self.invalidate_acquisition_context("apply")
        result = super().apply(command)
        if result.outcome == "success" and "value" in result.requested and result.readback is not None:
            if not equivalent(result.requested["value"], result.readback):
                result = replace(
                    result,
                    outcome="rejected",
                    error="Readback differs from request at scalar serialization precision",
                )
        if result.outcome == "success" and command.operation in {"run", "stop", "single"}:
            try:
                actual = self.read("trigger_mode")
                expected = {"run": "AUTO", "stop": "STOP", "single": "SINGLE"}[command.operation]
                # SINGLE may already complete; STOP then proves no continued acquisition,
                # not that a valid new waveform has been captured.
                okay = actual == expected or (command.operation == "single" and actual == "STOP")
                result = replace(
                    result,
                    readback=actual,
                    outcome="success" if okay else "rejected",
                    error="" if okay else "Trigger state differs from request",
                )
            except DeviceError as exc:
                result = replace(result, outcome="partial", error=str(exc))
        return result

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        try:
            return self._acquire(request)
        except Exception:
            self.invalidate_acquisition_context("error")
            raise

    def _acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind != "waveform" or any(ch not in self.profile.channels for ch in request.channels):
            raise Unsupported("Use analog C1-C4 waveform acquisition")
        if self.identity is None:
            raise DeviceError("Identify before acquisition", "identity_required")
        cached_mode = request.consistency in {"live", "stream"} and not request.initiate
        if cached_mode and not request.context_id:
            raise ValidationError("Cached acquisition requires a nonempty run context")
        key = (request.consistency, request.context_id, request.channels)
        context = self._acquisition_context
        if context is not None and context.key != key:
            self.invalidate_acquisition_context("new_run_or_channels")
            context = None
        if request.initiate:
            # An empty input can wait indefinitely. Never claim OPC alone proves
            # that a newly triggered sweep exists or replay SINGLE after timeout.
            result = self.apply(DeviceCommand("single", confirmed=True))
            if result.outcome != "success":
                raise DeviceError(result.error, "acquisition_failed")
            deadline = time.monotonic() + min(self.config.timeout, 5.0)
            while self.read("trigger_mode") != "STOP":
                if time.monotonic() >= deadline:
                    raise DeviceError("Single acquisition is still waiting for a trigger", "timeout")
                time.sleep(0.05)
        reused = cached_mode and context is not None
        before = deepcopy(context.configuration) if reused and context else self.snapshot(request.channels)[0]
        if before["1.transfer_format"] != "DEF9,WORD,BIN":
            raise DeviceError(
                "Reconnect explicitly to prepare WORD binary acquisition", "acquisition_not_prepared"
            )
        if before["1.interleaved"] != "OFF" or not str(before["1.sequence_setup"]).startswith("OFF"):
            raise Unsupported("Sequence and RIS acquisition have not been qualified")
        if any(before[f"{ch}.channel_enabled"] != "ON" for ch in request.channels):
            raise Unsupported("Selected waveform channel is disabled; enable it explicitly")
        setup = before["1.waveform_setup"]
        if setup["SP"] not in (0, 1) or setup["FP"] != 0:
            raise Unsupported("Only unsparsed, zero-offset waveform transfers are qualified")
        values = []
        for channel in request.channels:
            raw = self.transport.query(f"C{channel}:WF? ALL")
            with parsing():
                measured = waveform(raw, channel)
            values.append(replace(measured, metadata={**measured.metadata, "received_at": utc_now()}))
        # Parse/length failures above are never disguised as configuration changes.
        if reused and context:
            after, stamps = deepcopy(context.configuration), dict(context.timestamps)
        else:
            after, stamps = self.snapshot(request.channels)
        changes = {
            key: {"before": value, "after": after[key]}
            for key, value in before.items()
            if not equivalent(value, after[key])
        }
        descriptors = {m.channel: fingerprint(m) for m in values}
        for measurement in values:
            channel = measurement.channel
            # Unambiguous descriptor fields checked against the actual snapshot.
            expected = {
                "sample_width": 2,
                "byte_order": after["1.byte_order"],
                "probe_attenuation": after[f"{channel}.probe_attenuation"],
            }
            if reused and context:
                expected.update(context.descriptors[channel])
            for name, value in expected.items():
                actual = descriptors[channel][name]
                if not equivalent(value, actual):
                    changes[f"C{channel}.descriptor.{name}"] = {"before": value, "after": actual}
        if changes:
            raise DeviceError(
                "Configuration changed: " + json.dumps(changes, ensure_ascii=False), "configuration_changed"
            )
        tokens = {m.channel: m.metadata["acquisition_key"] for m in values}
        observations = {
            str(ch): (
                "unknown"
                if token is None
                else "first"
                if not context or context.tokens[ch] is None
                else "repeat"
                if token == context.tokens[ch]
                else "changed"
            )
            for ch, token in tokens.items()
        }
        if cached_mode:
            self._acquisition_context = AcquisitionContext(
                key,
                deepcopy(after),
                dict(stamps),
                context.descriptors if reused and context else descriptors,
                tokens,
            )
        return MeasurementBatch(
            self.identity,
            tuple(values),
            after,
            validation="cached" if reused else "full",
            settings_timestamps=stamps,
            adapter_version=self.acquisition_revision,
            performance={
                "configuration_source": (
                    "recording_start" if request.consistency == "stream" else "monitoring_snapshot"
                )
                if reused
                else "instrument_before_and_after",
                "acquisition_context_id": request.context_id if request.consistency == "stream" else "",
                "logical_queries": len(values)
                if reused
                else 2 * (1 + len(values) if self.grouped_snapshot_queries() else len(before)) + len(values),
                "acquisition_observations": observations,
                "sequential_channels": True,
                "requested_points_is_not_a_hardware_limit": True,
            },
        )

    def acquisition_readings(self, batch: MeasurementBatch) -> tuple[ParameterReading, ...]:
        if batch.validation == "cached":
            return ()
        readings = []
        for key, value in batch.configuration.items():
            if "." not in key or key.startswith("_"):
                continue
            channel, operation = key.split(".", 1)
            readings.append(
                ParameterReading(
                    operation, int(channel), value, "instrument", timestamp=batch.settings_timestamps[key]
                )
            )
        return tuple(readings)
