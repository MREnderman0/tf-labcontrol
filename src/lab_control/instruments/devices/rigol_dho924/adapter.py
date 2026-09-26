"""DHO924 single-session controls, screen streaming and explicit memory reads."""

from __future__ import annotations

import json
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
from lab_control.domain.oscilloscope import ScopeDisplay
from lab_control.instruments.base import ScpiAdapter
from lab_control.transport.diagnostics import parsing

from .profile import CHANNEL_SETTINGS, GLOBAL_SETTINGS, TRANSFER_SETTINGS
from .protocol import FRAME_BYTES_MAX, POINTS_MAX, Preamble, equivalent, numeric, waveform


class DHO924Adapter(ScpiAdapter):
    supports_live_acquisition = True
    supports_stream_acquisition = True
    supports_memory_waveform = True
    waveform_extents = ("screen", "memory")
    acquisition_revision = "dho924-word-v1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._settings: dict[str, Any] = {}
        self._context_cache = None
        self._ranges_dirty = True
        self._transfer: dict[str, Any] = {}
        self.acquisition_preparation: dict[str, Any] = {}

    def read(self, operation: str, channel: int = 1):
        if operation == "trigger_mode":
            status = super().read("trigger_status")
            sweep = super().read("trigger_sweep")
            value = "STOP" if status == "STOP" else "SINGLE" if sweep == "SING" else sweep
        else:
            value = super().read(operation, channel)
        key = f"{channel}.{operation}"
        if self._settings.get(key) != value:
            self._ranges_dirty = True
        self._settings[key] = value
        return value

    def snapshot(self) -> tuple[dict[str, Any], dict[str, str]]:
        values, stamps = {}, {}
        for ch, ops in ((1, GLOBAL_SETTINGS), *((ch, CHANNEL_SETTINGS) for ch in self.profile.channels)):
            for op in ops:
                key = f"{ch}.{op}"
                values[key] = self.read(op, ch)
                stamps[key] = utc_now()
        return values, stamps

    def refresh_capabilities(self) -> None:
        if not self._ranges_dirty:
            return
        try:
            self.snapshot()
            mode = self.read("trigger_mode")
            self.capability_details.update(
                memory_waveform=True,
                waveform_default_extent="screen",
                memory={
                    "points": self._settings["1.record_length"],
                    "sample_rate": self._settings["1.sample_rate"],
                },
                acquisition_state=mode,
            )
            self.capability_error = ""
            self._ranges_dirty = False
        except Exception as exc:
            self.capability_error = str(exc)
            raise

    def capabilities_for_channel(self, channel: int):
        schema = super().capabilities_for_channel(channel)
        s = self._settings
        probe = s.get(f"{channel}.probe_attenuation", 1)
        scale = s.get(f"{channel}.scale", 0.05)
        input_scale = scale / probe
        offset_limit = (
            0.5
            if input_scale < 0.0005
            else 1
            if input_scale <= 0.065
            else 8
            if input_scale <= 0.26
            else 20
            if input_scale <= 2.65
            else 100
        ) * probe
        tdiv = s.get("1.timebase", 1e-6)
        right = 1 if tdiv <= 0.01 else 100 * tdiv if tdiv < 10 else 1000 if tdiv < 200 else 5 * tdiv
        source = str(s.get("1.trigger_source", ""))
        source_ch = int(source[-1]) if source in {"CHAN1", "CHAN2", "CHAN3", "CHAN4"} else None
        edge = s.get("1.trigger_type") == "EDGE" and source_ch is not None
        operations = []
        for cap in schema.operations:
            spec = cap.parameters.get("value")
            if spec:
                if cap.name == "scale":
                    spec = replace(spec, minimum=0.0002 * probe, maximum=10 * probe)
                elif cap.name == "offset":
                    spec = replace(spec, minimum=-offset_limit, maximum=offset_limit)
                elif cap.name == "time_position":
                    spec = replace(spec, minimum=-5 * tdiv, maximum=right)
                    # Stopped acquisitions can pan outside the RUN window. Without
                    # verified memory bounds, do not invent a writable stopped range.
                    if s.get("1.trigger_mode") == "STOP":
                        cap = replace(cap, writable=False, reason="停止状态的水平偏移范围未验证；运行后可调")
                elif cap.name == "timebase" and s.get("1.roll_enabled", False):
                    spec = replace(spec, maximum=0.02)
                elif cap.name == "trigger_level" and edge:
                    div, off = s[f"{source_ch}.scale"], s[f"{source_ch}.offset"]
                    spec = replace(spec, minimum=-4.5 * div - off, maximum=4.5 * div - off)
                cap = replace(cap, parameters={"value": spec})
            if cap.name in {"trigger_source", "trigger_level", "trigger_slope"} and not edge:
                cap = replace(cap, writable=False, reason="仅支持当前模拟通道边沿触发；高级触发保持不变")
            if cap.name in {"scale", "offset", "coupling", "probe_attenuation"} and not s.get(
                f"{channel}.channel_enabled", False
            ):
                cap = replace(cap, writable=False, reason="请先开启该通道")
            if self.capability_error:
                cap = replace(cap, writable=False, reason=self.capability_error)
            operations.append(cap)
        return replace(schema, operations=tuple(operations))

    def invalidate_acquisition_context(self, reason: str) -> None:
        self._context_cache = None
        self._transfer.clear()
        if reason in {"explicit_refresh", "connection_setup", "error"}:
            self._ranges_dirty = True

    def prepare_acquisition(self) -> dict:
        before = {op: self.read(op) for op in TRANSFER_SETTINGS}
        if before["transfer_format"] != "WORD":
            self.transport.write(":WAV:FORM WORD")
            if self.read("transfer_format") != "WORD":
                raise DeviceError("WORD transfer preparation failed", "readback_mismatch")
        self._transfer.clear()
        self.acquisition_preparation = {
            "state": "ready",
            "before": before,
            "after": {**before, "transfer_format": "WORD"},
        }
        return deepcopy(self.acquisition_preparation)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        spec, parameters = self.validate_command(command)
        self.refresh_capabilities()
        cap = self.capabilities_for_channel(command.channel).get(command.operation)
        if not cap.writable:
            raise Unsupported(cap.reason)
        cap.validate(parameters)
        self.invalidate_acquisition_context("apply")
        op = command.operation
        writes = tuple(p.format(**self._context(command.channel, parameters)) for p in spec.writes)
        requested = parameters.get("value")
        if op == "trigger_mode":
            writes = (
                (":STOP",)
                if requested == "STOP"
                else (":SING",)
                if requested == "SINGLE"
                else (f":TRIG:SWE {requested}", ":RUN")
            )
        completed = 0
        try:
            for payload in writes:
                self.transport.write(payload)
                completed += 1
            if op not in {"run", "stop", "single", "trigger_mode"}:
                # This firmware's query reads the completion marker; an idle
                # query alone can stay at 0. *OPC queues that marker after the
                # preceding setting (PG 3.12.6; separately hardware-qualified).
                self.transport.write("*OPC")
                bounded = getattr(self.transport, "query_timeout", None)
                deadline = time.monotonic() + min(30, max(self.config.timeout, 5))
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeviceError(
                            "Setting completion not confirmed within deadline", "readback_mismatch"
                        )
                    raw = (
                        bounded("*OPC?", max(0.1, remaining))
                        if callable(bounded)
                        else self.transport.query("*OPC?")
                    )
                    status = numeric(raw)
                    if status == 1:
                        break
                    if status != 0:
                        raise DeviceError("Invalid setting completion response", "parse_error")
                    # Programming guide 3.12.6 explicitly returns 0 while busy.
                    # Poll only completion; the setting itself is never resent.
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            expected = requested if requested is not None else {"stop": "STOP", "single": "SINGLE"}.get(op)
            actual = self.read("trigger_mode" if op in {"run", "stop", "single"} else op, command.channel)
            if op in {"run", "stop", "single", "trigger_mode"}:
                deadline = time.monotonic() + min(self.config.timeout, 5)
                while (
                    not (
                        (op == "run" and actual in {"AUTO", "NORM"})
                        or equivalent(expected, actual)
                        or expected == "SINGLE"
                        and actual == "STOP"
                    )
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.025)
                    actual = self.read("trigger_mode")
            error = self.transport.query(":SYST:ERR?").decode("ascii").strip()
            if not error.lstrip("+").startswith("0,"):
                return ApplyResult("rejected", op, parameters, actual, completed, error)
            okay = (
                (op == "run" and actual in {"AUTO", "NORM"})
                or equivalent(expected, actual)
                or expected == "SINGLE"
                and actual == "STOP"
            )
            self._ranges_dirty = True
            if okay:
                self.configuration[f"{command.channel}.{op}"] = {"value": actual}
            return ApplyResult(
                "success" if okay else "rejected",
                op,
                parameters,
                actual,
                completed,
                "" if okay else "Readback differs from request",
            )
        except Exception as exc:
            self._ranges_dirty = True
            return ApplyResult(
                "partial" if completed else "uncertain",
                op,
                parameters,
                completed_steps=completed,
                error=str(exc),
            )

    def _select_transfer(self, name: str, value: Any) -> None:
        if self._transfer.get(name) != value:
            self.transport.write(f":WAV:{name} {value}")
            self._transfer[name] = value

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind != "waveform" or any(ch not in self.profile.channels for ch in request.channels):
            raise Unsupported("Only DHO924 analog channels 1-4 are supported")
        if request.initiate:
            raise Unsupported("Use explicit single-arm control; waiting does not prove a new waveform")
        if self.identity is None:
            raise DeviceError("Identity required", "identity_required")
        if request.waveform_extent == "memory":
            return self._memory(request)
        try:
            return self._read_frame(request, memory=False)
        except Exception:
            self.invalidate_acquisition_context("error")
            raise

    def _memory(self, request: AcquisitionRequest) -> MeasurementBatch:
        # Reject a request before STOP or any transfer write. The acquisition's
        # own full snapshot repeats this guard after STOP, never truncating data.
        points = self.read("record_length")
        self._check_budget(points, len(request.channels))
        original = self.read("trigger_mode")
        if original == "SINGLE":
            raise Unsupported("完整读取不能打断等待中的单次触发，请先停止或切换连续运行")
        transfer = {op: self.read(op) for op in TRANSFER_SETTINGS}
        self.invalidate_acquisition_context("memory")
        try:
            if original != "STOP":
                result = self.apply(DeviceCommand("stop"))
                if result.outcome != "success":
                    raise DeviceError(result.error, "acquisition_failed")
            batch = self._read_frame(request, memory=True)
            self.restore_transfer(transfer)
            if original != "STOP":
                result = self.apply(DeviceCommand("trigger_mode", {"value": original}))
                if result.outcome != "success":
                    raise DeviceError("读取完成，但运行状态恢复失败：" + result.error, "restoration_failed")
            return batch
        except Exception as exc:
            raise DeviceError(
                f"完整读取未完成：{exc}；未自动重放，需核对仪器最终状态",
                getattr(exc, "code", "acquisition_failed"),
            ) from exc
        finally:
            self.invalidate_acquisition_context("memory_finished")

    def restore_transfer(self, values: dict) -> None:
        for op, short in (
            ("transfer_source", "SOUR"),
            ("transfer_mode", "MODE"),
            ("transfer_format", "FORM"),
            ("transfer_points", "POIN"),
            ("transfer_start", "STAR"),
            ("transfer_stop", "STOP"),
        ):
            self.transport.write(f":WAV:{short} {values[op]}")
        for op in TRANSFER_SETTINGS:
            if not equivalent(values[op], self.read(op)):
                raise DeviceError(f"Transfer restoration differs: {op}", "restoration_failed")
        self._transfer.clear()

    @staticmethod
    def _check_budget(points: int, channels: int) -> None:
        if not 1 <= points <= POINTS_MAX or points * 16 * channels > FRAME_BYTES_MAX:
            raise DeviceError("完整存储超出软件点数/帧预算，请降低采集点数", "acquisition_budget_exceeded")

    def _read_frame(self, request: AcquisitionRequest, *, memory: bool) -> MeasurementBatch:
        cached_mode = request.consistency in {"live", "stream"}
        if cached_mode and not request.context_id:
            raise ValidationError("Cached acquisition requires a run context")
        key = (request.consistency, request.context_id, request.channels)
        context = self._context_cache
        reused = bool(cached_mode and context and context[0] == key and not memory)
        if reused:
            assert context is not None
            before, stamps = deepcopy(context[1]), dict(context[2])
        else:
            before, stamps = self.snapshot()
        if (
            before["1.acquisition_type"] not in {"NORM", "AVER"}
            or before["1.timebase_mode"] != "MAIN"
            or before["1.xy_enabled"]
            or before["1.delay_enabled"]
        ):
            raise Unsupported(
                "Only normal/averaged MAIN analog acquisition is qualified; no roll, XY, peak or segmented modes"
            )
        if any(not before[f"{ch}.channel_enabled"] for ch in request.channels):
            raise Unsupported("Selected channel is disabled")
        points = int(before["1.record_length"]) if memory else 1000
        self._check_budget(points, len(request.channels))
        if not reused and self.read("transfer_format") != "WORD":
            raise DeviceError("Reconnect to prepare WORD transfer", "acquisition_not_prepared")
        mode = "RAW" if memory else "NORM"
        self._select_transfer("MODE", mode)
        self._select_transfer("POIN", points)
        self._select_transfer("STAR", 1)
        self._select_transfer("STOP", points)
        measurements, signatures = [], {}
        for ch in request.channels:
            self._select_transfer("SOUR", f"CHAN{ch}")
            pre_raw = self.transport.query(":WAV:PRE?")
            pre = Preamble.parse(pre_raw)
            if pre.type != (2 if memory else 0) or pre.points != points:
                raise DeviceError("Preamble mode/points differ from full transfer window", "parse_error")
            if not memory:
                pre.check_screen(
                    pre_raw,
                    scale=before[f"{ch}.scale"],
                    offset=before[f"{ch}.offset"],
                    timebase=before["1.timebase"],
                    position=before["1.time_position"],
                )
            bounded = getattr(self.transport, "query_binary", None)
            raw = (
                bounded(":WAV:DATA?", points * 2) if callable(bounded) else self.transport.query(":WAV:DATA?")
            )
            unit = {"VOLT": "V", "AMP": "A", "WATT": "W"}.get(before[f"{ch}.channel_unit"])
            if unit is None:
                raise Unsupported("Unqualified channel unit")
            with parsing():
                measured = waveform(raw, pre, ch, unit)
            post = Preamble.parse(self.transport.query(":WAV:PRE?"))
            if pre != post:
                raise DeviceError("Waveform preamble changed during reading", "configuration_changed")
            signatures[ch] = pre.signature()
            if (
                reused
                and context is not None
                and any(not equivalent(a, b) for a, b in zip(context[3][ch], pre.signature(), strict=True))
            ):
                raise DeviceError("Waveform descriptor changed from run snapshot", "configuration_changed")
            measurements.append(
                replace(
                    measured,
                    metadata={
                        **measured.metadata,
                        "received_at": utc_now(),
                        "waveform_extent": "memory" if memory else "screen",
                        "scope_display": ScopeDisplay(
                            before[f"{ch}.scale"],
                            -before[f"{ch}.offset"],
                            before["1.timebase"],
                            before["1.time_position"],
                            unit,
                        ).metadata(),
                    },
                )
            )
        after = before
        if not reused:
            after, stamps = self.snapshot()
            changes = {
                k: {"before": v, "after": after[k]} for k, v in before.items() if not equivalent(v, after[k])
            }
            if changes:
                raise DeviceError("Configuration changed: " + json.dumps(changes), "configuration_changed")
        if cached_mode:
            self._context_cache = (key, deepcopy(after), dict(stamps), signatures)
        assert self.identity is not None
        return MeasurementBatch(
            self.identity,
            tuple(measurements),
            after,
            validation="cached" if reused else "full",
            settings_timestamps=stamps,
            performance={
                "acquisition_context_id": request.context_id,
                "waveform_extent": "memory" if memory else "screen",
                "sequential_channels": True,
                "new_acquisitions": None,
                "sample_rate": after["1.sample_rate"],
                "acquisition_revision": self.acquisition_revision,
            },
        )

    def acquisition_readings(self, batch: MeasurementBatch) -> tuple[ParameterReading, ...]:
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
