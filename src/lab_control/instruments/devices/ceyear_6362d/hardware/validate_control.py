"""Opt-in 6362D verification; native runtime, exact asset, no discovery or network changes."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.instruments.common.readonly_hardware import asset_folder_name
from lab_control.instruments.registry import create_adapter
from lab_control.storage.recording import Recording
from lab_control.storage.repository import application_home

from ..adapter import Ceyear6362DAdapter
from ..authentication import authenticate
from ..protocol import BINARY_QUALIFICATIONS, integer
from .records import publish_report
from .state import (
    MATRIX,
    TOLERANCES,
    differences,
    prepare_control_traces,
    qualify_binary,
    restore,
    set_endpoints,
    snapshot,
    stop_scan,
    trace_attributes,
    validate_backup,
)


def save(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(jsonable(data), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


class AuditTransport:
    def __init__(self, inner, folder, profile):
        self.inner, self.resource, self.folder = inner, inner.resource, folder
        self.allowed = {"*IDN?", ":TRIGGER:STATE?", *(s.query for s in profile.commands.values() if s.query)}
        for letter in "ABCDEFG":
            self.allowed.update(
                {
                    f":TRACE:ATTRIBUTE:TR{letter}?",
                    f":TRACE:DATA:SNUMBER? TR{letter}",
                    f":TRACE:DATA:X? TR{letter}",
                    f":TRACE:DATA:Y? TR{letter}",
                }
            )
        self.patterns = [
            re.escape(t).replace(re.escape("{value}"), r"[A-Za-z0-9+.,e-]+")
            for s in profile.commands.values()
            for t in s.writes
        ]
        self.patterns.append(r":FORMAT:DATA (?:ASCII|REAL,32|REAL,64)")
        self.writes_allowed = False
        self.capture_curves = True
        self.sequence = 0
        self.faulted = False
        self.command_counts: dict[str, int] = {}

    def open(self):
        raise RuntimeError("Session owns transport opening")

    def authenticate(self, credentials, terminator):
        self.log(authentication="started", credentials="redacted")
        try:
            authenticate(self.inner, credentials, terminator)
            self.log(authentication="ready")
        except Exception:
            self.faulted = True
            self.log(authentication="failed")
            raise

    def read(self):
        raise RuntimeError("Use logged query transactions")

    def log(self, **entry):
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": utc_now(), **entry}) + "\n")

    def write(self, command):
        if not self.writes_allowed or not any(re.fullmatch(p, command) for p in self.patterns):
            raise DeviceError("Command outside backed-up 6362D plan", "unsupported")
        self.log(tx=command)
        self.command_counts[command] = self.command_counts.get(command, 0) + 1
        try:
            self.inner.write(command)
        except Exception as exc:
            self.faulted = True
            self.log(error=str(exc))
            raise

    def _query(self, command, function):
        if command not in self.allowed:
            raise DeviceError("Query outside 6362D plan", "unsupported")
        self.log(tx=command)
        start = time.monotonic()
        try:
            raw = function()
            entry = {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "seconds": time.monotonic() - start,
            }
            if command.startswith((":TRACE:DATA:X?", ":TRACE:DATA:Y?")):
                if self.capture_curves:
                    self.sequence += 1
                    name = f"backup-wire-{self.sequence:04}.bin"
                    (self.folder / name).write_bytes(raw)
                    entry["file"] = name
            else:
                entry["rx"] = raw.decode("ascii", "backslashreplace")
            self.log(**entry)
            return raw
        except Exception as exc:
            self.faulted = True
            self.log(error=str(exc))
            raise

    def query(self, command):
        return self._query(command, lambda: self.inner.query(command))

    def query_timeout(self, command, seconds):
        return self._query(command, lambda: self.inner.query_timeout(command, seconds))

    def query_binary(self, command, maximum):
        return self._query(command, lambda: self.inner.query_binary(command, maximum))

    def query_fixed(self, command, count):
        return self._query(command, lambda: self.inner.query_fixed(command, count))

    def query_binary_flexible(self, command, count):
        return self._query(command, lambda: self.inner.query_binary_flexible(command, count))

    def close(self):
        self.inner.close()


def frame_metadata(batch) -> dict:
    trace = batch.measurements[0]
    if trace.axis is None or trace.unit != "dBm" or trace.axis_unit != "nm" or not len(trace.values):
        raise DeviceError("Invalid optical trace", "parse_error")
    return {
        "timestamp": batch.timestamp,
        "validation": batch.validation,
        "points": len(trace.values),
        "unit": trace.unit,
        "axis_unit": trace.axis_unit,
        "axis_start": float(trace.axis[0]),
        "axis_stop": float(trace.axis[-1]),
        "configuration_version": batch.configuration.get("_session_configuration_version"),
        "metadata": trace.metadata,
    }


def read_trace(wire: AuditTransport, trace: str, axis: str, fmt: str, points: int | None = None) -> bytes:
    """Preserve the original framing, including binary backups before initialization."""
    command = f":TRACE:DATA:{axis}? {trace}"
    if fmt == "ASCII":
        return wire.query(command)
    if fmt not in {"REAL,32", "REAL,64"}:
        raise DeviceError("Unknown backup transfer format", "backup_unreliable")
    width = 4 if fmt == "REAL,32" else 8
    flexible_query = getattr(wire, "query_binary_flexible", None)
    if points is not None and flexible_query:
        return flexible_query(command, points * width)
    fixed_query = getattr(wire, "query_fixed", None)
    if points is not None and fixed_query:
        return fixed_query(command, points * width)
    return wire.query_binary(command, 50001 * width)


class MetadataRecording(Recording):
    def _csv_loop(self):
        self.thread.join()
        self.finished.set_result([])
        self.wake()


def install_metadata_sinks(app, report):
    def forbidden_export(*args, **kwargs):
        raise AssertionError("Curve export forbidden in performance validation")

    def factory(mode, **options):
        recording = MetadataRecording(app.repository.root, mode, prepare_storage=False, **options)

        def append(sequence, batch):
            report.setdefault("recorded_metadata", []).append({"sequence": sequence, **frame_metadata(batch)})
            return "metadata-only"

        setattr(recording.segments, "append", append)
        return recording

    app.repository.export = forbidden_export
    app.repository.create_recording = factory


def verify(
    config: ConnectionConfig,
    folder: Path,
    *,
    control=False,
    transfer_only=False,
    original_scan="unknown",
    trigger="unknown",
    leave_stopped=False,
    x_atol_nm: float | None = None,
    y_atol_db: float | None = None,
    auto_single_frames: int = 0,
    auto_record_frames: int = 10,
    auto_interval: float = 10.0,
) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "adapter_id": "ceyear.6362d",
        "resource": config.resource,
        "connection": {
            "transport": config.transport,
            "source_address": config.source_address or None,
        },
        "expected_serial": config.expected_serial,
        "started_at": utc_now(),
        "scope": "6362D B.11; explicit selected LAN asset; no network/source/calibration changes",
        "execution_mode": "transfer_only" if transfer_only else "control" if control else "readonly",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pyvisa": importlib.metadata.version("PyVISA"),
        },
        "source_sha256": {
            str(path.relative_to(Path(__file__).parents[1])): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parents[1].rglob("*.py"))
            if "tests" not in path.parts
        },
        "stages": dict.fromkeys(
            (
                "identity",
                "connection",
                "parameter_read",
                "existing_trace",
                "parameter_control",
                "single_scan",
                "binary_encoding",
                "frames",
                "monitoring",
                "restoration",
                "reconnection",
                "metadata_only",
                "physical_output",
            ),
            "not_tested",
        ),
        "panel_evidence": {"original_scan": original_scan, "trigger": trigger},
        "scan_restore_policy": "user_authorized_leave_stopped" if leave_stopped else "restore_original",
        "automatic_single_scan": {
            "requested_frames": auto_single_frames,
            "requested_recorded_frames": auto_record_frames,
            "interval_seconds": auto_interval,
        },
        "trigger_policy": "unchanged; unknown option commands are not sent",
        "operations": [],
        "matrix": MATRIX,
        "tolerances": TOLERANCES,
        "final_state": "No setting writes; state not yet verified",
    }
    save(folder / "results.json", report)
    (folder / "communication.jsonl").touch()
    stage = "connection"
    app = original = None
    adapter: Any = None
    changed = False
    attributes = None
    before_files: set[str] = set()
    key = ""

    def mark(name):
        nonlocal stage
        stage = name
        save(folder / "results.json", report)

    def run(function):
        assert app is not None
        return app.sessions.sessions[key].worker.submit(function).result(timeout=180)

    def apply(op, value=None):
        assert app is not None
        command = DeviceCommand(op, {} if value is None else {"value": value}, confirmed=True)
        result = app.sessions.submit(key, "apply", command).result(90)
        report["operations"].append({"command": jsonable(command), "result": jsonable(result)})
        save(folder / "results.json", report)
        if result.outcome != "success":
            raise DeviceError(f"{op}: {result.error}", "control_failed")
        return result

    def transfer_tests(start_scan):
        assert app is not None and x_atol_nm is not None and y_atol_db is not None
        mark("binary_encoding")
        count = run(lambda: adapter.read("trace_points"))
        assert x_atol_nm is not None and y_atol_db is not None

        def xy(fmt):
            adapter.set_transfer_format(fmt)
            return tuple(read_trace(wire, "TRA", axis, fmt, count) for axis in ("X", "Y"))

        ascii_xy = run(lambda: xy("ASCII"))
        binary_xy = run(lambda: xy("REAL,64"))
        if run(lambda: xy("ASCII")) != ascii_xy:
            raise DeviceError("Frozen curve changed during qualification", "configuration_changed")
        evidence = qualify_binary(ascii_xy, binary_xy, count, x_atol_nm=x_atol_nm, y_atol_db=y_atol_db)
        report["binary_qualification"] = {"firmware": identity.firmware, **evidence}
        save(folder / "binary-qualification.json", report["binary_qualification"])
        # Local acceptance session only; permanent qualification requires reviewed source evidence.
        BINARY_QUALIFICATIONS[identity.firmware] = evidence["byte_order"]
        run(lambda: adapter.set_transfer_format("REAL,64"))
        report["stages"][stage] = "passed"
        wire.capture_curves = False
        mark("frames")
        report["frames"] = []
        for _ in range(20):
            batch = app.sessions.submit(key, "acquire", AcquisitionRequest("spectrum")).result(180)
            report["frames"].append(frame_metadata(batch))
        report["stages"][stage] = "passed"
        mark("monitoring")
        if auto_single_frames:
            if start_scan:
                raise DeviceError(
                    "Automatic single-scan validation requires original scan stopped", "backup_unreliable"
                )
            if auto_single_frames < 5 or auto_record_frames < 10 or not 0.5 <= auto_interval <= 60:
                raise DeviceError(
                    "Automatic single-scan validation needs at least 5 frames, 10 recorded frames and a 0.5..60 s interval",
                    "invalid_parameter",
                )
        if start_scan and not auto_single_frames:
            apply("repeat")
        with app.monitoring.lock:
            single_commands_before = wire.command_counts.get(":INITIATE", 0)
            app.monitoring.start(
                key,
                (
                    AcquisitionRequest(
                        "spectrum",
                        points=2001,
                        initiate=bool(auto_single_frames),
                        confirmed=bool(auto_single_frames),
                    ),
                ),
                cadence="interval" if auto_single_frames else "fastest",
                interval=auto_interval if auto_single_frames else 0.5,
                mode="optical",
                record_interval=None,
            )
        start = time.monotonic()
        try:
            task = app.monitoring.tasks[key]
            if auto_single_frames:
                app.monitoring.set_recording(key, True)
                while task.sequence < auto_single_frames:
                    if task.error or not task.running:
                        raise DeviceError(task.error or "Monitoring stopped", "monitor_failed")
                    time.sleep(0.2)
                report["automatic_single_scan"] = {
                    **report["automatic_single_scan"],
                    "completed_frames": task.sequence,
                    "single_commands": wire.command_counts.get(":INITIATE", 0) - single_commands_before,
                }
                app.monitoring.set_recording(key, False)
                if task.recorder:
                    deadline = time.monotonic() + 30
                    while not task.recorder.binary_done:
                        if time.monotonic() > deadline:
                            raise DeviceError(
                                "Recording drain did not finish before parameter change", "monitor_failed"
                            )
                        time.sleep(0.2)
                # Change each exposed optical setting while the scanner is live;
                # session serialization drops any frame carrying the old snapshot.
                changes = (
                    ("start_wavelength", 1542),
                    ("stop_wavelength", 1558),
                    ("rbw", 0.2),
                    ("sensitivity", "MID"),
                )
                report["monitoring_parameter_changes"] = []
                before_recorded = len(report.get("recorded_metadata", []))
                for operation, value in changes:
                    result = apply(operation, value)
                    actual = run(lambda op=operation: adapter.read(op))
                    report["monitoring_parameter_changes"].append(
                        {
                            "operation": operation,
                            "requested": value,
                            "readback": actual,
                            "result": jsonable(result),
                        }
                    )
                app.monitoring.set_recording(key, True)
                deadline = time.monotonic() + max(180, auto_record_frames * auto_interval + 60)
                while len(report.get("recorded_metadata", [])) - before_recorded < auto_record_frames:
                    if task.error or not task.running:
                        raise DeviceError(task.error or "Monitoring stopped", "monitor_failed")
                    if time.monotonic() > deadline:
                        raise DeviceError(
                            "Automatic single-scan recording deadline exceeded", "monitor_failed"
                        )
                    time.sleep(0.2)
                after_change = report.get("recorded_metadata", [])[before_recorded:]
                if any(
                    abs(float(frame["axis_start"]) - 1542) > 1e-6
                    or abs(float(frame["axis_stop"]) - 1558) > 1e-6
                    for frame in after_change
                ):
                    raise DeviceError(
                        "A pre-change optical frame was recorded after configuration change",
                        "configuration_changed",
                    )
                report["monitoring_parameter_changes_summary"] = {
                    "recorded_before_change": before_recorded,
                    "recorded_after_change": len(after_change),
                    "post_change_axes": sorted(
                        {(frame["axis_start"], frame["axis_stop"]) for frame in after_change}
                    ),
                    "recording_disabled_during_change": True,
                }
            else:
                app.monitoring.set_recording(key, True)
                while time.monotonic() - start < 60:
                    task = app.monitoring.tasks[key]
                    if task.error or not task.running:
                        raise DeviceError(task.error or "Monitoring stopped", "monitor_failed")
                    time.sleep(0.2)
        finally:
            app.monitoring.stop(key).result(180)
        task = app.monitoring.tasks[key]
        recorded = len(report.get("recorded_metadata", []))
        report["monitoring"] = {
            "seconds": time.monotonic() - start,
            "frames": task.sequence,
            "recorded_frames": recorded,
            "single_commands": wire.command_counts.get(":INITIATE", 0) - single_commands_before,
            "acquisition_mode": "single_per_frame" if auto_single_frames else "passive",
        }
        if not task.sequence:
            raise DeviceError("No valid monitoring frames", "monitor_failed")
        if auto_single_frames and recorded < auto_record_frames:
            raise DeviceError(
                "Automatic single-scan recording did not retain enough frames", "monitor_failed"
            )
        if not auto_single_frames and recorded != task.sequence:
            raise DeviceError("Metadata recording did not retain every monitoring frame", "monitor_failed")
        if start_scan and not auto_single_frames:
            apply("stop")
        report["stages"][stage] = "passed"

    try:
        if leave_stopped and not control:
            raise DeviceError("Leave-stopped policy requires explicit control mode", "backup_unreliable")
        lan_resource = re.fullmatch(r"TCPIP\d*::[^:]+::10001::SOCKET", config.resource, re.I)
        if (
            config.adapter != "ceyear.6362d"
            or config.transport not in {"visa", "tcp"}
            or (config.transport == "visa" and config.source_address)
            or not config.expected_serial
            or not lan_resource
        ):
            raise DeviceError("One exact Ceyear 6362D LAN asset and serial required", "identity_required")
        home = application_home()
        report["physical_paths"] = verify_runtime_paths(home, home)
        if not folder.absolute().is_relative_to((home / "hardware-tests").absolute()):
            raise DeviceError("Hardware batches must use the formal runtime", "path_redirected")
        app = ApplicationRuntime((config,))
        install_metadata_sinks(app, report)  # Before every possible acquisition, including an existing trace.
        before_files = {str(p) for p in (home / "data").rglob("*") if p.is_file()}

        def factory(selected):
            if selected != config:
                raise DeviceError("Unexpected instrument", "identity_conflict")
            result = create_adapter(selected, app.visa)
            assert isinstance(result, Ceyear6362DAdapter)
            result.transport = AuditTransport(result.transport, folder, result.profile)
            return result

        app.sessions.factory = factory
        key = app.add(config)
        identity = app.connect(key, configure_acquisition=False).result(90)
        report.update(identity=jsonable(identity), inventory_id=key)
        report["environment"]["visa_library"] = str(app.visa.manager.visalib)
        report["stages"].update(identity="passed", connection="passed")
        adapter = app.sessions.sessions[key].adapter
        assert isinstance(adapter, Ceyear6362DAdapter)
        wire = adapter.transport
        assert isinstance(wire, AuditTransport)
        mark("parameter_read")
        original = run(lambda: snapshot(adapter))
        report["initial_snapshot"] = original
        save(folder / "initial_snapshot.json", original)
        report["stages"][stage] = "passed"
        if control:
            validate_backup(original, original_scan, trigger, leave_stopped=leave_stopped)
            if x_atol_nm is None or y_atol_db is None:
                raise DeviceError("Fix ASCII comparison precision before writing", "backup_unreliable")
            if trigger == "off" and run(lambda: integer(wire.query(":TRIGGER:STATE?"))) != 0:
                raise DeviceError("H04 trigger is not off", "backup_unreliable")
            if differences(run(lambda: snapshot(adapter)), original):
                raise DeviceError("Backup changed before testing", "backup_unreliable")
        mark("existing_trace")
        # Save original raw replies without changing their format or guessing byte order.
        report["trace_backups"] = {}
        for letter in "ABCDEFG":
            trace = "TR" + letter
            attribute = run(lambda: integer(wire.query(f":TRACE:ATTRIBUTE:{trace}?")))
            count = run(lambda: integer(wire.query(f":TRACE:DATA:SNUMBER? {trace}")))
            if not 0 <= count <= 50001:
                raise DeviceError("Invalid backup point count", "backup_unreliable")
            report["trace_backups"][trace] = {"attribute": attribute, "points": count}
            if control and (
                attribute not in {0, 1} or (not leave_stopped and letter != "A" and attribute != 1)
            ):
                raise DeviceError("Other active traces have no qualified recovery", "backup_unreliable")
            if count:
                for axis in ("X", "Y"):
                    raw = run(lambda: read_trace(wire, trace, axis, original["transfer_format"], count))
                    report["trace_backups"][trace][axis] = {
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
        if transfer_only:
            if control or original["trace_mode"] != "FIX" or x_atol_nm is None or y_atol_db is None:
                raise DeviceError(
                    "Transfer-only qualification requires fixed TRA and preset precision", "backup_unreliable"
                )
            if differences(run(lambda: snapshot(adapter)), original):
                raise DeviceError("Settings changed before format qualification", "backup_unreliable")
            report["stages"].update(parameter_control="blocked", single_scan="blocked")
            report["blocked_reason"] = (
                "TRA held, TRB selected; original running/trigger state not confirmed; no scan or measurement parameter writes"
            )
        if not control:
            batch = app.sessions.submit(key, "acquire", AcquisitionRequest("spectrum")).result(180)
            report["existing_trace"] = frame_metadata(batch)
            report["stages"][stage] = "passed"
            if transfer_only:
                wire.writes_allowed = True
                wire.patterns = [r":FORMAT:DATA (?:ASCII|REAL,32|REAL,64)"]
                changed = True
                report["final_state"] = (
                    "Transfer format validation in progress; original format restoration pending"
                )
                transfer_tests(False)
        else:
            attributes = {trace: info["attribute"] for trace, info in report["trace_backups"].items()}
            if run(lambda: trace_attributes(adapter)) != attributes:
                raise DeviceError("Trace attributes changed after backup", "backup_unreliable")
            wire.writes_allowed = True
            if leave_stopped:
                wire.patterns += [r":TRACE:ATTRIBUTE:TR[A-G] (?:WRITE|FIX)", r":TRACE:ACTIVE TR[A-G]"]
            changed = True
            report["final_state"] = "Control validation in progress; backed-up settings restoration pending"
            report["initial_stop"] = run(lambda: stop_scan(adapter))
            run(lambda: adapter.set_transfer_format("ASCII"))
            existing = app.sessions.submit(key, "acquire", AcquisitionRequest("spectrum")).result(180)
            report["existing_trace"] = frame_metadata(existing)
            report["stages"][stage] = "passed"
            if leave_stopped:
                run(lambda: prepare_control_traces(adapter, attributes))
            mark("parameter_control")
            set_endpoints(apply, lambda op: run(lambda: adapter.read(op)), 1540, 1560)
            for op, value in (
                ("center_wavelength", 1550),
                ("wavelength_span", 20),
                *MATRIX,
                ("points_auto", True),
                ("points_auto", False),
                ("sweep_points", 2001),
            ):
                apply(op, value)
                actual = run(lambda: snapshot(adapter))
                if differences(
                    actual,
                    {
                        "start_wavelength": 1540.0,
                        "stop_wavelength": 1560.0,
                        "center_wavelength": 1550.0,
                        "wavelength_span": 20.0,
                    },
                ):
                    raise DeviceError("Related wavelengths differ", "readback_mismatch")
                report["operations"][-1]["snapshot"] = actual
            report["stages"][stage] = "passed"
            mark("single_scan")
            apply("single")
            fresh = app.sessions.submit(key, "acquire", AcquisitionRequest("spectrum")).result(180)
            frame = frame_metadata(fresh)
            report["single_scan_curve"] = frame
            if (
                frame["points"] != 2001
                or abs(frame["axis_start"] - 1540) > 1e-6
                or abs(frame["axis_stop"] - 1560) > 1e-6
            ):
                raise DeviceError("New sweep data does not match tested range/points", "readback_mismatch")
            report["stages"][stage] = "passed"
            transfer_tests(not auto_single_frames)
    except Exception as exc:
        report.update(
            error=str(exc),
            error_code=getattr(exc, "code", type(exc).__name__),
            traceback=traceback.format_exc(),
        )
        report["stages"][stage] = (
            "blocked"
            if report["error_code"]
            in {
                "resource_busy",
                "process_busy",
                "unsupported",
                "identity_required",
                "backup_unreliable",
                "path_redirected",
            }
            else "failed"
        )
    finally:
        if changed and app and adapter and original:
            mark("restoration")
            try:
                wire = adapter.transport
                if wire.faulted or app.sessions.sessions[key].state == "degraded":
                    app.sessions.disconnect(key)
                    recovered = app.connect(key, configure_acquisition=False).result(90)
                    if jsonable(recovered) != report["identity"]:
                        raise DeviceError("Recovery identity mismatch", "identity_conflict")
                    adapter = app.sessions.sessions[key].adapter
                    adapter.transport.writes_allowed = True
                    adapter.transport.capture_curves = False
                    if leave_stopped:
                        adapter.transport.patterns += [
                            r":TRACE:ATTRIBUTE:TR[A-G] (?:WRITE|FIX)",
                            r":TRACE:ACTIVE TR[A-G]",
                        ]
                if transfer_only:
                    run(lambda: adapter.set_transfer_format(original["transfer_format"]))
                    actual = run(lambda: snapshot(adapter))
                    delta = differences(actual, original)
                    save(folder / "restoration-differences.json", delta)
                    if delta:
                        raise DeviceError(
                            "Settings changed during format-only verification", "restoration_failed"
                        )
                    report["restoration"] = {
                        "snapshot": actual,
                        "differences": delta,
                        "scope": "transfer format only; no acquisition parameter or scan writes",
                    }
                else:
                    report["restoration"] = run(
                        lambda: restore(
                            adapter,
                            original,
                            original_scan,
                            trigger,
                            leave_stopped=leave_stopped,
                            attributes=attributes if leave_stopped else None,
                        )
                    )
                save(folder / "restoration-differences.json", report["restoration"]["differences"])
                report["stages"][stage] = "passed"
                report["final_state"] = (
                    "Settings verified; scan restoration command sent; panel confirmation pending; historical curves not restored"
                )
                if transfer_only:
                    report["final_state"] = (
                        "Original ASCII format restored; every backed-up setting unchanged; no scan/measurement-parameter writes"
                    )
                elif leave_stopped:
                    report["final_state"] = (
                        "Backed-up settings and trace attributes restored; ABORT completed and no scan restarted, "
                        "as authorized. Trigger settings unchanged; historical curves not restored."
                    )
                mark("reconnection")
                report["reconnections"] = []
                for _ in range(3):
                    app.sessions.disconnect(key)
                    reconnected = app.connect(key, configure_acquisition=False).result(90)
                    adapter = app.sessions.sessions[key].adapter
                    actual = run(lambda: snapshot(adapter))
                    delta = differences(actual, original)
                    report["reconnections"].append({"identity": jsonable(reconnected), "differences": delta})
                    if jsonable(reconnected) != report["identity"] or delta:
                        raise DeviceError("Reconnection identity/settings mismatch", "readback_mismatch")
                report["stages"][stage] = "passed"
            except Exception as exc:
                report["restore_error"] = str(exc)
                report["stages"][stage] = "failed"
                report["final_state"] = "Recovery stopped at first failure; final scan/settings state unknown"
        if app:
            try:
                app.close()
                after = {str(p) for p in (app.repository.root / "data").rglob("*") if p.is_file()}
                report["new_curve_files"] = sorted(after - before_files)
                report["stages"]["metadata_only"] = "failed" if after - before_files else "passed"
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        for name in ("initial_snapshot.json", "restoration-differences.json"):
            if not (folder / name).exists():
                save(folder / name, {"status": "unavailable", "reason": report.get("error", "Not reached")})
        (folder / "record.md").write_text(
            "# 6362D 实机核验\n\n"
            + report["scope"]
            + "\n\n|项目|结果|\n|---|---|\n"
            + "\n".join(f"|{k}|{v}|" for k, v in report["stages"].items())
            + "\n\n最终状态："
            + report["final_state"]
            + "\n\n自动单次扫描："
            + json.dumps(report.get("automatic_single_scan", {}), ensure_ascii=False)
            + "\n\n监测："
            + json.dumps(report.get("monitoring", {}), ensure_ascii=False)
            + "\n\n监测中参数变更："
            + json.dumps(report.get("monitoring_parameter_changes", []), ensure_ascii=False)
            + "\n\n配置切换记录核对："
            + json.dumps(report.get("monitoring_parameter_changes_summary", {}), ensure_ascii=False)
            + "\n\n物理输出：未测试（没有校准参考光源）"
            + "\n\n异常："
            + report.get("error", "无")
            + "\n\n恢复异常："
            + report.get("restore_error", "无")
            + "\n\n[结果](results.json) · [摘要](summary.json) · [通信](communication.jsonl)\n",
            encoding="utf-8",
        )
        publish_report(folder)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--control", action="store_true")
    modes.add_argument("--qualify-transfer", action="store_true")
    parser.add_argument("--original-scan", choices=("stopped", "repeat"), default="unknown")
    parser.add_argument("--trigger", choices=("absent", "off"), default="unknown")
    parser.add_argument(
        "--leave-stopped",
        action="store_true",
        help="User-authorized waiver of original running state; preserve trigger settings and finish stopped",
    )
    parser.add_argument("--x-atol-nm", type=float)
    parser.add_argument("--y-atol-db", type=float)
    parser.add_argument(
        "--auto-single-frames", type=int, default=0, help="Run one confirmed single scan per monitoring frame"
    )
    parser.add_argument(
        "--auto-record-frames",
        type=int,
        default=10,
        help="Valid metadata-only frames required after the live setting change",
    )
    parser.add_argument(
        "--auto-interval",
        type=float,
        default=10.0,
        help="Target interval for automatic single scans (0.5..60 s)",
    )
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1:
        parser.error("Exactly one selected asset required")
    config = configs[0]
    folder = (
        application_home()
        / "hardware-tests"
        / asset_folder_name("6362D", config.expected_serial)
        / (datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + ("-control" if args.control else "-readonly"))
    )
    report = verify(
        config,
        folder,
        control=args.control,
        transfer_only=args.qualify_transfer,
        original_scan=args.original_scan,
        trigger=args.trigger,
        leave_stopped=args.leave_stopped,
        x_atol_nm=args.x_atol_nm,
        y_atol_db=args.y_atol_db,
        auto_single_frames=args.auto_single_frames,
        auto_record_frames=args.auto_record_frames,
        auto_interval=args.auto_interval,
    )
    print(
        json.dumps(
            {
                "folder": str(folder),
                "stages": report["stages"],
                "error": report.get("error"),
                "restore_error": report.get("restore_error"),
            },
            ensure_ascii=False,
        )
    )
    return int(bool(report.get("error") or report.get("restore_error") or report.get("close_error")))


if __name__ == "__main__":
    raise SystemExit(main())
