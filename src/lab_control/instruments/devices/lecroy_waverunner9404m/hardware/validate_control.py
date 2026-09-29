"""Opt-in asset verification, with backup, bounded controls and verified restoration."""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import platform
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home
from lab_control.transport.diagnostics import measured

from ..adapter import CHANNEL_SETTINGS, WaveRunner9404MAdapter, equivalent
from ..protocol import numeric
from ..ranges import MEMORY_QUERIES, PATHS, cvar_query
from .records import publish_report


def verify_saved_samples(folder: Path) -> dict[str, Any]:
    """Compare every converted coordinate/sample against committed binary frames."""
    from lab_control.storage.analysis import verify_analysis
    from lab_control.storage.layout import manifest
    from lab_control.storage.recording import read_frames

    if manifest(folder).get("layout_version", 1) == 2:
        return verify_analysis(folder)
    frames = samples = cached = 0
    contexts: set[str] = set()
    timestamps: set[str] = set()
    for path in sorted(folder.glob("segment-*.sqlite3")):
        with path.with_suffix(".csv").open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream)
            for frame in read_frames(path):
                frames += 1
                cached += frame["validation"] == "cached"
                contexts.add(frame["performance"].get("acquisition_context_id", ""))
                timestamps.add(json.dumps(frame["settings_timestamps"], sort_keys=True))
                for measurement in frame["measurements"]:
                    for x, value in zip(measurement["axis"], measurement["values"], strict=True):
                        row = next(rows)
                        if (
                            int(row["sequence"]) != frame["sequence"]
                            or row["config_id"] != frame["config_id"]
                            or int(row["channel"]) != measurement["channel"]
                            or row["unit"] != measurement["unit"]
                            or row["axis_unit"] != measurement["axis_unit"]
                            or float(row["axis"]) != x
                            or float(row["real"]) != value.real
                            or float(row["imaginary"]) != value.imag
                        ):
                            raise DeviceError("Binary/CSV sample mismatch", "recording_failed")
                        samples += 1
            if next(rows, None) is not None:
                raise DeviceError("Unexpected CSV rows", "recording_failed")
    if not frames or not samples:
        raise DeviceError("No committed samples", "recording_failed")
    return {
        "frames": frames,
        "sample_rows": samples,
        "cached_frames": cached,
        "contexts": sorted(contexts),
        "distinct_settings_snapshots": len(timestamps),
        "equal": True,
    }


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def native_paths(home: Path) -> dict[str, str]:
    import msvcrt
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    result = {}
    for name in ("hardware.lock", "inventory.sqlite3"):
        with (home / name).open("rb") as stream:
            buffer = ctypes.create_unicode_buffer(32768)
            if not function(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0):
                raise ctypes.WinError()
            result[name] = buffer.value
            expected = str(home / name)
            if buffer.value.removeprefix("\\\\?\\").casefold() != expected.casefold():
                raise DeviceError(
                    "Refuse redirected inventory/hardware lock: " + buffer.value, "path_redirected"
                )
    return result


class AuditTransport:
    def __init__(self, inner, folder: Path, allowed: set[str]):
        self.inner, self.folder, self.allowed = inner, folder, allowed
        self.resource = inner.resource
        self.writes_allowed = False
        self.last = ""
        self.binary_samples: set[str] = set()
        self.setting_writes = 0

    def log(self, **data):
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": utc_now(), **data}, ensure_ascii=False) + "\n")

    def open(self):
        raise RuntimeError("Session owns opening")

    def close(self):
        self.inner.close()

    def write(self, payload):
        if not isinstance(payload, str) or any(c in payload for c in "\r\n"):
            raise DeviceError("Hardware test rejects compound/raw writes", "unsupported")
        if ";" in payload:
            if not all(q in self.allowed and q.endswith("?") for q in payload.split(";")):
                raise DeviceError("Hardware test rejects unreviewed compound commands", "unsupported")
            self.last = payload
            self.log(direction="tx", command=payload)
            self.inner.write(payload)
            return
        if "?" not in payload:
            if not self.writes_allowed:
                raise DeviceError("No setting writes before verified backup", "unsupported")
            self.setting_writes += 1
        elif payload not in self.allowed and payload not in {f"C{ch}:WF? ALL" for ch in (1, 2, 3, 4)}:
            raise DeviceError("Query not in reviewed hardware plan", "unsupported")
        self.last = payload
        self.log(direction="tx", command=payload)
        self.inner.write(payload)

    def read(self):
        try:
            raw = self.inner.read()
            if ":WF?" in self.last and b"#" in raw:
                data = {"direction": "rx", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                if self.last not in self.binary_samples:
                    name = self.last.split(":")[0] + "-first-waveform.bin"
                    (self.folder / name).write_bytes(raw)
                    data["evidence"] = name
                    self.binary_samples.add(self.last)
                self.log(**data)
            else:
                self.log(direction="rx", text=raw.decode("ascii", errors="backslashreplace"), hex=raw.hex())
            return raw
        except Exception as exc:
            self.log(direction="error", error=str(exc))
            raise

    @measured("query")
    def query(self, payload):
        self.write(payload)
        return self.read()

    def query_binary(self, payload, max_bytes):
        bounded = getattr(self.inner, "query_binary", None)
        if not callable(bounded):
            return self.query(payload)
        if payload not in {f"C{ch}:WF? ALL" for ch in (1, 2, 3, 4)}:
            raise DeviceError("Unreviewed binary query", "unsupported")
        self.log(direction="tx", command=payload, receive_limit=max_bytes)
        try:
            raw = bounded(payload, max_bytes)
            marker = raw.index(b"#")
            digits = int(raw[marker + 1 : marker + 2])
            self.log(
                direction="rx",
                bytes=len(raw),
                declared=int(raw[marker + 2 : marker + 2 + digits]),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
            if payload not in self.binary_samples:
                (self.folder / (payload[:2] + "-first-waveform.bin")).write_bytes(raw)
                self.binary_samples.add(payload)
            return raw
        except Exception as exc:
            self.log(direction="error", error=str(exc))
            raise

    def query_timeout(self, payload, seconds):
        bounded = getattr(self.inner, "query_timeout", None)
        if not callable(bounded):
            return self.query(payload)
        if payload not in self.allowed:
            raise DeviceError("Unreviewed timed query", "unsupported")
        self.log(direction="tx", command=payload, timeout=seconds)
        try:
            raw = bounded(payload, seconds)
            self.log(direction="rx", text=raw.decode("ascii"))
            return raw
        except Exception as exc:
            self.log(direction="error", error=str(exc))
            raise


class Verification:
    def __init__(self, app, device_id, folder, report):
        self.app, self.device_id, self.folder, self.report = app, device_id, folder, report

    @property
    def adapter(self) -> WaveRunner9404MAdapter:
        adapter = self.app.sessions.sessions[self.device_id].adapter
        assert isinstance(adapter, WaveRunner9404MAdapter)
        return adapter

    def call(self, action):
        return self.app.sessions.sessions[self.device_id].worker.submit(action).result(timeout=120)

    def snapshot(self):
        return self.call(lambda: self.adapter.snapshot()[0])

    def stage(self, name):
        self.report["active_stage"] = name
        save(self.folder / "results.json", self.report)

    def apply(self, name, value=None, channel=1):
        command = DeviceCommand(name, {} if value is None else {"value": value}, channel, confirmed=True)
        entry = {"time": utc_now(), "command": jsonable(command)}
        self.report["operations"].append(entry)
        result = self.app.sessions.submit(self.device_id, "apply", command).result(timeout=120)
        entry["result"] = jsonable(result)
        save(self.folder / "results.json", self.report)
        if result.outcome != "success":
            raise DeviceError(result.error or result.outcome, "control_failed")
        return result

    def restore_parameters(self, desired):
        current = self.snapshot()
        if all(equivalent(value, current[key]) for key, value in desired.items()):
            return
        # A successful full snapshot is required before any restoration write.
        if current["1.trigger_mode"] != "STOP":
            self.apply("stop")
        for channel in (1, 2, 3, 4):

            def value(name):
                return desired[f"{channel}.{name}"]

            changed = {op for op in CHANNEL_SETTINGS if not equivalent(value(op), current[f"{channel}.{op}"])}
            if not changed:
                continue
            # Zero the offset before changing sensitivity. A scale valid on both
            # impedances prevents restoring a large 1 MOhm scale while still at 50 Ohm.
            analog = bool(changed & {"scale", "coupling", "probe_attenuation"})
            if analog:
                if not equivalent(self.call(lambda: self.adapter.read("offset", channel)), 0.0):
                    self.apply("offset", 0.0, channel)
                attenuation = self.call(lambda: self.adapter.read("probe_attenuation", channel))
                safe_scale = min(attenuation * 0.01, 10.0)
                if not equivalent(self.call(lambda: self.adapter.read("scale", channel)), safe_scale):
                    self.apply("scale", safe_scale, channel)
            operations = (("coupling", "probe_attenuation", "scale") if analog else ()) + (
                "trigger_level",
                "trigger_slope",
                "channel_enabled",
            )
            for operation in operations:
                target = value(operation)
                actual = self.call(lambda op=operation: self.adapter.read(op, channel))
                if not equivalent(actual, target):
                    self.apply(operation, target, channel)
            if not equivalent(self.call(lambda: self.adapter.read("offset", channel)), value("offset")):
                self.apply("offset", value("offset"), channel)
        for operation in ("timebase", "record_length"):
            target = desired[f"1.{operation}"]
            actual = self.call(lambda op=operation: self.adapter.read(op))
            if not equivalent(actual, target):
                spec = self.adapter.capabilities_for_channel(1).get(operation).parameters["value"]
                assert spec.minimum is not None and spec.maximum is not None
                if not spec.minimum <= target <= spec.maximum:
                    # Restore only this exact backed-up original, after independent
                    # live hardware-range confirmation. Never widen the UI budget.
                    def restore_original():
                        wire = self.adapter.transport
                        low = numeric(wire.query(cvar_query(operation, 1, "GetMinValue")))
                        high = numeric(wire.query(cvar_query(operation, 1, "GetMaxValue")))
                        if not low <= target <= high:
                            raise DeviceError(
                                "Saved value is outside current hardware range", "restoration_failed"
                            )
                        mnemonic = {"timebase": "TDIV", "record_length": "MSIZ"}[operation]
                        wire.write(f"{mnemonic} {target:.12g}")
                        self.adapter.complete_setting(DeviceCommand(operation, {"value": target}))
                        if not equivalent(self.adapter.read(operation), target):
                            raise DeviceError("Saved value restoration differs", "restoration_failed")

                    self.call(restore_original)
                else:
                    self.apply(operation, target)
        # Restore the exact saved transfer setup, not a default or a guessed inverse.
        for operation, mnemonic in (
            ("transfer_format", "CFMT"),
            ("byte_order", "CORD"),
            ("waveform_setup", "WFSU"),
            ("response_header", "CHDR"),
        ):
            target = desired[f"1.{operation}"]
            actual = self.call(lambda op=operation: self.adapter.read(op))
            if actual != target:
                encoded = (
                    ",".join(f"{k},{target[k]}" for k in ("SP", "NP", "FP", "SN"))
                    if operation == "waveform_setup"
                    else str(target)
                )
                self.call(lambda m=mnemonic, v=encoded: self.adapter.transport.write(m + " " + v))
                if self.call(lambda op=operation: self.adapter.read(op)) != target:
                    raise DeviceError("Transfer restoration differs: " + operation, "restoration_failed")
        mode = desired["1.trigger_mode"]
        if self.call(lambda: self.adapter.read("trigger_mode")) != mode:
            self.apply("trigger_mode", mode)
        # Memory and re-arming can adapt delay; restore it after those dependencies.
        if not equivalent(self.call(lambda: self.adapter.read("time_position")), desired["1.time_position"]):
            self.apply("time_position", desired["1.time_position"])
        after = self.snapshot()
        differences = {
            k: {"before": v, "after": after[k]} for k, v in desired.items() if not equivalent(v, after[k])
        }
        if differences:
            raise DeviceError("Restoration differs: " + json.dumps(differences), "restoration_failed")

    def controls(self, original):
        self.stage("parameter_write")
        for channel in (1, 2, 3, 4):
            targets: dict[str, Any] = {
                "channel_enabled": "OFF" if original[f"{channel}.channel_enabled"] == "ON" else "ON",
                "scale": 0.02,
                "offset": 0.01,
                "coupling": "D1M",
                "probe_attenuation": 10.0,
                "trigger_level": 0.01,
                "trigger_slope": "NEG",
            }
            for operation, target in targets.items():
                before = self.snapshot()
                old = before[f"{channel}.{operation}"]
                if equivalent(old, target):
                    target = {
                        "scale": 0.05,
                        "offset": 0.0,
                        "coupling": "A1M",
                        "probe_attenuation": 1.0,
                        "trigger_level": 0.0,
                        "trigger_slope": "POS",
                    }.get(operation, target)
                cap = self.adapter.capabilities().get(operation)
                try:
                    cap.validate({"value": old})
                except DeviceError as exc:
                    self.report["skipped"].append(
                        {"operation": operation, "channel": channel, "reason": str(exc)}
                    )
                    continue
                self.apply(operation, target, channel)
                actual = self.snapshot()
                other_changes = [
                    key
                    for key in before
                    if key.split(".")[0] != str(channel)
                    and key.split(".")[1] in CHANNEL_SETTINGS
                    and not equivalent(before[key], actual[key])
                ]
                if other_changes:
                    raise DeviceError(
                        "Unexpected other-channel changes: " + str(other_changes), "isolation_failed"
                    )
                self.restore_parameters(before)
        for operation, target in (
            ("timebase", 0.001),
            ("time_position", 0.0),
            ("record_length", 10000),
            ("trigger_mode", "AUTO"),
        ):
            before = self.snapshot()
            if equivalent(before[f"1.{operation}"], target):
                target = {
                    "timebase": 0.002,
                    "time_position": 0.0001,
                    "record_length": 20000,
                    "trigger_mode": "STOP",
                }[operation]
            try:
                self.adapter.capabilities().get(operation).validate({"value": before[f"1.{operation}"]})
            except DeviceError as exc:
                self.report["skipped"].append({"operation": operation, "reason": str(exc)})
                continue
            self.apply(operation, target)
            self.restore_parameters(before)
        self.report["stages"]["parameter_write"] = "passed" if not self.report["skipped"] else "blocked"
        self.stage("acquisition_state")
        for operation in ("run", "stop", "single", "stop"):
            result = self.apply(operation)
            self.report.setdefault("trigger_results", []).append(
                {"operation": operation, "readback": result.readback}
            )
        self.restore_parameters(original)
        self.report["stages"]["acquisition_state"] = "passed"

    def capture(self, duration):
        self.stage("waveform")
        self.call(self.adapter.prepare_acquisition)
        self.report["preparation"] = self.adapter.acquisition_preparation
        # Limit the transfer, not the instrument memory. Explicitly restored later.
        self.call(lambda: self.adapter.transport.write("WFSU SP,1,NP,10000,FP,0,SN,0"))
        if self.call(lambda: self.adapter.read("waveform_setup")) != {"SP": 1, "NP": 10000, "FP": 0, "SN": 0}:
            raise DeviceError("Waveform transfer limit readback differs", "readback_mismatch")
        for channel in (1, 2, 3, 4):
            self.apply("channel_enabled", "ON", channel)
        self.apply("run")
        time.sleep(1)
        self.apply("stop")
        request = AcquisitionRequest("waveform", (1, 2, 3, 4), points=10000)
        batch = self.app.sessions.submit(self.device_id, "acquire", request).result(timeout=120)
        self.report["waveforms"] = [
            {
                "channel": m.channel,
                "points": len(m.values),
                "unit": m.unit,
                "axis_unit": m.axis_unit,
                "metadata": m.metadata,
            }
            for m in batch.measurements
        ]
        self.report["stages"]["waveform"] = "passed"
        self.stage("single_save")
        saved = self.app.monitoring.capture_and_export(self.device_id, request, "electronic").result(
            timeout=120
        )
        self.report["single_save"] = str(saved)
        self.report["stages"]["single_save"] = "passed"
        self.stage("monitoring_recording")
        self.apply("run")
        self.app.monitoring.start(
            self.device_id, (request,), interval=0.5, record_interval=1.0, cadence="interval"
        )
        self.app.monitoring.set_recording(self.device_id, True)
        task = self.app.monitoring.tasks[self.device_id]
        rejected = False
        try:
            self.app.sessions.submit(self.device_id, "apply", DeviceCommand("timebase", {"value": 0.002}))
        except DeviceError:
            rejected = True
        if not rejected:
            raise DeviceError("Recording parameter lock not enforced", "lock_failed")
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if not task.running:
                raise DeviceError(
                    "Monitoring stopped: " + str(self.app.monitoring.snapshots()), "monitoring_failed"
                )
            time.sleep(0.1)
        self.report["manual_save_during_recording"] = str(
            self.app.monitoring.capture_and_export(self.device_id, request, "electronic").result(timeout=120)
        )
        self.app.monitoring.stop(self.device_id).result(timeout=120)
        recorder = task.recorder
        if recorder is None:
            raise DeviceError("Missing recorder", "recording_failed")
        recorder.finished.result(timeout=120)
        status = recorder.snapshot()
        self.report["recording"] = {**status, "folder": str(recorder.folder), "lock_rejected": rejected}
        if (
            not status["binary_done"]
            or status.get("error")
            or status.get("csv_error")
            or status.get("committed", 0) < 1
            or status["converted"] != status["committed"]
            or status["received"] != status["committed"]
            or status["pending"]
        ):
            raise DeviceError("Recording/CSV completion failed", "recording_failed")
        if not tuple(recorder.folder.rglob("*.csv")):
            raise DeviceError("No converted CSV", "recording_failed")
        self.report["binary_csv_comparison"] = verify_saved_samples(recorder.folder)
        self.apply("stop")  # This also verifies release of the recording parameter lock.
        self.report["stages"]["monitoring_recording"] = "passed"

    def continuity(self):
        """Short, empty-input control batch; normal monitor intent survives each group."""
        self.stage("parameter_write")
        self.app.sessions.submit(self.device_id, "refresh_capabilities").result(120)
        self.apply("record_length", 1000)
        self.apply("timebase", 1e-6)
        self.apply("channel_enabled", "ON", 1)
        self.apply("scale", 0.002, 1)
        self.apply("offset", 0, 1)
        self.call(self.adapter.prepare_acquisition)
        self.apply("run")
        time.sleep(0.3)
        service = self.app.monitoring
        service.start(
            self.device_id, (AcquisitionRequest("waveform", (1,)),), interval=0.5, cadence="interval"
        )

        def await_frame(previous=None):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = service.snapshots()[self.device_id]
                frame = service.latest(self.device_id, "waveform", channel=1)
                if not state["running"]:
                    raise DeviceError(str(state), "monitoring_failed")
                if frame is not None and frame is not previous:
                    return frame
                time.sleep(0.05)
            raise DeviceError("No new valid frame after setting", "timeout")

        try:
            previous = await_frame()
            self.report["continuity_frames"] = []
            timebase_range = self.adapter.capabilities_for_channel(1).get("timebase").parameters["value"]
            long_timebase = min(0.02, timebase_range.maximum or 0.02)
            for operation, value in (
                ("offset", 0.00006),
                ("time_position", 1.73e-6),
                ("record_length", 2500),
                ("timebase", long_timebase),
                ("record_length", 10000),
                ("timebase", 1e-6),
            ):
                self.app.sessions.set_control_group(self.device_id, True)
                try:
                    result = self.apply(operation, value)
                finally:
                    self.app.sessions.set_control_group(self.device_id, False)
                current = await_frame(previous)
                previous = current
                self.report["continuity_frames"].append(
                    {
                        "operation": operation,
                        "requested": value,
                        "actual": result.readback,
                        "points": len(current.batch.measurements[0].values),
                        "monitoring_running": service.snapshots()[self.device_id]["running"],
                    }
                )
            self.report["stages"].update(parameter_write="passed", waveform="passed", monitoring="passed")
        finally:
            service.stop(self.device_id).result(120)


def verify(
    config: ConnectionConfig,
    folder: Path,
    *,
    write: bool = False,
    duration: float = 30,
    capture_only: bool = False,
    continuity_only: bool = False,
    restore_from: Path | None = None,
) -> dict[str, Any]:
    import pyvisa

    source = Path(__file__).parents[1]
    (folder / "communication.jsonl").touch(exist_ok=True)
    report: dict[str, Any] = {
        "adapter_id": "lecroy.9404m",
        "started_at": utc_now(),
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "config": jsonable(config),
        "operations": [],
        "skipped": [],
        "scope": (
            "Empty-input acquisition, save and restore"
            if capture_only
            else "Empty-input basic control, acquisition, save, restore"
        )
        if write
        else "Read-only full settings and reconnect",
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "source_sha256": {
            str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob("*.py")
            if "tests" not in p.parts
        },
        "stages": dict.fromkeys(
            (
                "enumeration",
                "identity",
                "connection",
                "parameter_read",
                "reconnection",
                "parameter_write",
                "acquisition_state",
                "waveform",
                "single_save",
                "monitoring_recording",
                "restoration",
                "physical_output",
            ),
            "not_tested",
        ),
        "active_stage": "connection",
        "final_state": "Not yet queried; no settings written",
    }
    app = test = original = None
    mutated = False
    transcripts = []
    try:
        if (
            config.adapter != "lecroy.9404m"
            or config.transport not in {"visa", "vicp"}
            or not config.expected_serial
        ):
            raise DeviceError(
                "Require exact model, expected serial and VISA/VICP configuration", "identity_required"
            )
        home = application_home()
        report["physical_paths"] = native_paths(home)
        # Batch repository isolates exports; compatibility and asset locks remain at the fixed root.
        app = ApplicationRuntime((config,), root=folder / "runtime", hardware=True)
        if config.transport == "visa":
            report["enumeration"] = app.visa.discover_lan(3)
            report["environment"]["visa"] = str(app.visa.manager.visalib)
            report["stages"]["enumeration"] = (
                "passed" if config.resource in report["enumeration"] else "not_tested"
            )
        runtime = app

        def factory(selected):
            if selected != config:
                raise DeviceError("Only the configured asset is allowed", "identity_conflict")
            adapter = create_adapter(selected, runtime.visa)
            assert isinstance(adapter, WaveRunner9404MAdapter)
            allowed = {"*IDN?", "*OPC?", *MEMORY_QUERIES.values()} | {
                s.query.format(ch=ch)
                for s in adapter.profile.commands.values()
                if s.query
                for ch in (1, 2, 3, 4)
            }
            allowed |= {
                cvar_query(op, ch, member)
                for op in PATHS
                for ch in (1, 2, 3, 4)
                for member in (
                    "GetMinValue",
                    "GetMaxValue",
                    "GetGrainValue",
                    "GetRequestedValue",
                    "GetAdaptedValue",
                )
            }
            transport = AuditTransport(adapter.transport, folder, allowed)
            transcripts.append(transport)
            adapter.transport = transport
            return adapter

        app.sessions.factory = factory
        key = app.add(config)
        identity = app.connect(key, configure_acquisition=False).result(timeout=120)
        report["identity"] = jsonable(identity)
        report["stages"].update(identity="passed", connection="passed")
        test = Verification(app, key, folder, report)
        test.stage("parameter_read")
        original = test.snapshot()
        report["initial_capability_details"] = jsonable(test.adapter.capability_details)
        report["initial_capabilities"] = jsonable(test.adapter.capabilities_for_channel(1))
        report["initial_snapshot"] = original
        save(folder / "initial_snapshot.json", original)
        report["stages"]["parameter_read"] = "passed"
        test.stage("reconnection")
        report["reconnections"] = []
        for attempt in range(3):
            app.sessions.disconnect(key)
            actual = app.connect(key, configure_acquisition=False).result(timeout=120)
            same = actual.key == identity.key and test.snapshot() == original
            report["reconnections"].append(
                {"attempt": attempt + 1, "identity": jsonable(actual), "unchanged": same}
            )
            if not same:
                raise DeviceError("Identity/state changed on reconnect", "readback_mismatch")
        report["stages"]["reconnection"] = "passed"
        if write:
            # A full successful snapshot precedes enabling any setting command.
            cast(AuditTransport, test.adapter.transport).writes_allowed = True
            mutated = True
            if restore_from is not None:
                saved = json.loads(restore_from.read_text(encoding="utf-8"))
                if (
                    saved.get("identity", {}).get("serial") != identity.serial
                    or saved.get("identity", {}).get("model") != identity.model
                ):
                    raise DeviceError("Recovery backup belongs to another asset", "identity_conflict")
                original = saved["initial_snapshot"]
                report["recovery_source"] = str(restore_from)
                report["recovery_target"] = original
                test.stage("restoration")
                # The single restoration attempt runs in finally, including
                # its final verification. Never replay a failed recovery here.
            elif continuity_only:
                test.continuity()
            else:
                if not capture_only:
                    test.controls(original)
                test.capture(duration)
        report["final_snapshot"] = test.snapshot()
    except Exception as exc:
        report.update(error=str(exc), error_code=getattr(exc, "code", type(exc).__name__))
        report["error_traceback"] = traceback.format_exc()
        report["stages"][report["active_stage"]] = (
            "blocked"
            if report["error_code"]
            in {
                "remote_interface_mismatch",
                "path_redirected",
                "process_busy",
                "resource_busy",
                "connection_failed",
                "driver_missing",
                "unsupported",
            }
            else "failed"
        )
    finally:
        if test and original is not None:
            assert app is not None
            try:
                if mutated:
                    app.monitoring.stop(test.device_id).result(timeout=120)
                    if app.sessions.sessions[test.device_id].state in {"error", "degraded"}:
                        app.sessions.disconnect(test.device_id)
                        app.connect(test.device_id, configure_acquisition=False).result(timeout=120)
                        cast(AuditTransport, test.adapter.transport).writes_allowed = True
                    test.restore_parameters(original)
                final = test.snapshot()
                report["final_snapshot"] = final
                differences = {
                    k: {"before": v, "after": final[k]}
                    for k, v in original.items()
                    if not equivalent(v, final[k])
                }
                save(folder / "restoration-differences.json", differences)
                report["restoration_differences"] = differences
                report["stages"]["restoration"] = (
                    ("failed" if differences else "passed") if mutated else "not_tested"
                )
                report["final_state"] = (
                    "Settings restored and verified"
                    if mutated and not differences
                    else "No setting writes; final snapshot verified"
                    if not mutated
                    else "Restoration incomplete; see differences"
                )
            except Exception as exc:
                report["restoration_error"] = str(exc)
                report["stages"]["restoration"] = "failed" if mutated else "not_tested"
                report["final_state"] = "Final state could not be fully verified: " + str(exc)
        elif not mutated:
            report["final_state"] = "No settings written; current acquisition/settings could not be verified"
        if app:
            try:
                app.close()
                report["session_released"] = True
            except Exception as exc:
                report["close_error"] = str(exc)
        report["setting_writes"] = sum(t.setting_writes for t in transcripts)
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        if "final_snapshot" in report:
            save(folder / "final_snapshot.json", report["final_snapshot"])
        publish_report(folder)
        rows = "\n".join(f"| {name} | {status} |" for name, status in report["stages"].items())
        (folder / "record.md").write_text(
            f"# WaveRunner 9404M verification\n\n{report['scope']}\n\n"
            f"{report['started_at']} — {report['finished_at']}\n\n"
            f"| Stage | Result |\n| --- | --- |\n{rows}\n\n"
            f"Final state: {report['final_state']}\n\nError: {report.get('error', 'none')}\n\n"
            "[Raw results](results.json) · [Summary](summary.json) · [Communication](communication.jsonl)\n\n"
            "Empty-input communication/function tests do not qualify physical accuracy.\n",
            encoding="utf-8",
        )
        from lab_control.storage.hardware_documents import refresh_after_batch

        refresh_after_batch(folder)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--empty-inputs", action="store_true")
    parser.add_argument("--continuity-only", action="store_true")
    parser.add_argument(
        "--capture-only",
        action="store_true",
        help="Repeat only acquisition/save after successful control evidence",
    )
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--asset-name")
    args = parser.parse_args(argv)
    configs = load_config(args.config)
    if (
        len(configs) != 1
        or (args.write and not args.empty_inputs)
        or (args.capture_only and not args.write)
        or (args.continuity_only and (not args.write or args.capture_only))
        or not 1 <= args.duration <= 60
    ):
        parser.error("Require one exact asset; writes require --empty-inputs; duration 1..60 s")
    config = configs[0]
    name = args.asset_name or "WaveRunner9404M-" + config.expected_serial
    if any(char in name for char in '\\/:*?"<>|') or name in {"", ".", ".."}:
        parser.error("Asset folder must be a single safe component")
    folder = (
        application_home()
        / "hardware-tests"
        / name
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ("-control" if args.write else "-readonly"))
    )
    folder.mkdir(parents=True, exist_ok=False)
    report = verify(
        config,
        folder,
        write=args.write,
        duration=args.duration,
        capture_only=args.capture_only,
        continuity_only=args.continuity_only,
    )
    print(folder, flush=True)
    print(json.dumps(report["stages"], ensure_ascii=False), flush=True)
    return int(bool(report.get("error") or report.get("restoration_error") or report.get("close_error")))


if __name__ == "__main__":
    raise SystemExit(main())
