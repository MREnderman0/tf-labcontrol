"""Explicit empty-input verification of one serial-checked RTM3004.

Readonly is the default. Other phases require the caller's hardware authorization.
Every attempt retains its own report, communication evidence and restoration result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np

from lab_control.application.config import load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.recording import Recording
from lab_control.storage.runtime import hardware_lock_home, runtime_home

from ..adapter import Rtm3004Adapter
from .records import publish_report


def save(path, value):
    path.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AuditTransport:
    def __init__(self, inner, folder):
        self.inner, self.folder, self.resource = inner, folder, inner.resource
        self.allow_writes = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def log(self, **row):
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": utc_now(), **row}, ensure_ascii=False) + "\n")

    def write(self, command):
        if not self.allow_writes or any(c in command for c in ";\r\n"):
            raise DeviceError("Writes require a verified backup", "unsupported")
        self.log(write=command)
        self.inner.write(command)

    def _query(self, command, call):
        self.log(query=command)
        try:
            raw = call()
            if raw.startswith(b"#"):
                self.log(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            else:
                self.log(raw=raw.decode("ascii", errors="backslashreplace"))
            return raw
        except Exception as exc:
            self.log(error=str(exc))
            raise

    def query(self, command):
        return self._query(command, lambda: self.inner.query(command))

    def query_binary(self, command, max_bytes):
        return self._query(command, lambda: self.inner.query_binary(command, max_bytes))

    def query_timeout(self, command, seconds):
        self.log(timeout_seconds=seconds)
        return self._query(command, lambda: self.inner.query_timeout(command, seconds))


def snapshot(adapter):
    values, stamps = adapter.snapshot()
    return {
        "values": values,
        "timestamps": stamps,
        "format": adapter.read("transfer_format"),
        "byte_order": adapter.read("byte_order"),
        "state": adapter.read("acquisition_state"),
    }


def equivalent(left, right):
    if isinstance(left, (float, int)) and isinstance(right, (float, int)):
        return bool(np.isclose(left, right, rtol=1e-7, atol=1e-12))
    return left == right


def differences(before, after):
    """Sample rate and auto-selected memory are observations, not saved settings."""
    ignored = {"1.sample_rate"}
    if before["values"]["1.record_length_auto"]:
        ignored.add("1.record_length")
    delta = {
        key: {"before": value, "after": after["values"].get(key)}
        for key, value in before["values"].items()
        if key not in ignored and not equivalent(value, after["values"].get(key))
    }
    for key in ("format", "byte_order"):
        if before[key] != after[key]:
            delta[key] = {"before": before[key], "after": after[key]}
    if (before["state"] == "RUN") != (after["state"] == "RUN"):
        delta["state"] = {"before": before["state"], "after": after["state"]}
    return delta


def frame_metadata(batch):
    return {
        "performance": jsonable(batch.performance),
        "validation": batch.validation,
        "channels": [
            {
                "channel": m.channel,
                "points": len(m.values),
                "sha256": hashlib.sha256(m.values.tobytes()).hexdigest(),
                "axis": [float(m.axis[0]), float(m.axis[-1])],
                "min": float(np.min(m.values)),
                "max": float(np.max(m.values)),
                "metadata": jsonable(m.metadata),
            }
            for m in batch.measurements
        ],
    }


class MetadataRecording(Recording):
    def _csv_loop(self):
        self.thread.join()
        self.finished.set_result([])
        self.wake()


def install_metadata_sinks(app, report):
    def forbidden_export(*args, **kwargs):
        raise AssertionError("Curve export forbidden in a performance batch")

    def factory(mode, **options):
        recorder = MetadataRecording(app.repository.root, mode, prepare_storage=False, **options)

        def append(sequence, batch):
            report.setdefault("saved_frames", []).append({"sequence": sequence, **frame_metadata(batch)})
            return "metadata-only"

        setattr(recorder.segments, "append", append)
        return recorder

    app.repository.export = forbidden_export
    app.repository.create_recording = factory


class Exercises:
    def __init__(self, app, device, folder, report, initial):
        self.app, self.device, self.folder, self.report, self.initial = app, device, folder, report, initial
        self.session = app.sessions.sessions[device]
        self.adapter = self.session.adapter
        self.changed = []

    def work(self, function, *args):
        return self.session.worker.submit(function, *args).result(60)

    def apply(self, operation, value=None, channel=1):
        command = DeviceCommand(operation, {} if value is None else {"value": value}, channel, confirmed=True)
        self.changed.append((channel, operation))
        result = self.app.sessions.submit(self.device, "apply", command).result(60)
        self.report.setdefault("controls", []).append(
            {"command": jsonable(command), "result": jsonable(result)}
        )
        save(self.folder / "results.json", self.report)
        if result.outcome != "success":
            raise DeviceError(f"{operation}: {result.error}", "control_failed")
        return result

    def roundtrip(self, operation, value, channel=1):
        before = self.work(snapshot, self.adapter)
        original = before["values"][f"{channel}.{operation}"]
        self.apply(operation, value, channel)
        self.apply(operation, original, channel)
        after = self.work(snapshot, self.adapter)
        delta = differences(before, after)
        self.report.setdefault("roundtrips", []).append(
            {"operation": operation, "channel": channel, "differences": delta}
        )
        if delta:
            raise DeviceError("Roundtrip changed correlated settings", "readback_mismatch")

    def controls(self):
        self.report["active_stage"] = "parameter_write"
        values = self.initial["values"]
        for ch in range(1, 5):
            self.roundtrip("channel_enabled", not values[f"{ch}.channel_enabled"], ch)
            self.roundtrip("scale", values[f"{ch}.scale"] * 2, ch)
            self.roundtrip("offset", values[f"{ch}.offset"] + values[f"{ch}.scale"] * 0.1, ch)
            self.roundtrip("coupling", "ACL" if values[f"{ch}.coupling"] != "ACL" else "DCL", ch)
            self.roundtrip("probe_attenuation", values[f"{ch}.probe_attenuation"] * 2, ch)
            self.roundtrip("bandwidth_limit", "B20", ch)
            self.roundtrip("trigger_level", values[f"{ch}.trigger_level"] + values[f"{ch}.scale"] * 0.1, ch)
        for operation, value in (
            ("timebase", values["1.timebase"] / 2),
            ("time_position", values["1.time_position"] + values["1.timebase"] * 0.1),
            ("trigger_source", "CH2"),
            ("trigger_slope", "NEG"),
            ("averages", 4),
            ("acquisition_mode", "AVER"),
            ("decimation", "HRES"),
        ):
            self.roundtrip(operation, value)
        self.report["stages"]["parameter_write"] = "passed"

    def acquisition(self, *, metadata_only, duration):
        self.report["active_stage"] = "waveform"
        self.work(self.adapter.prepare_acquisition)
        self.report["preparation"] = self.adapter.acquisition_preparation
        for ch in range(1, 5):
            self.apply("channel_enabled", True, ch)
        self.apply("timebase", 0.001)
        self.apply("trigger_mode", "AUTO")
        self.apply("stop")
        request = AcquisitionRequest("waveform", (1, 2, 3, 4), confirmed=True)
        batch = self.app.sessions.submit(self.device, "acquire", request).result(60)
        self.report["waveform"] = frame_metadata(batch)
        self.report["stages"]["waveform"] = "passed"
        self.apply("trigger_mode", "AUTO")
        service = self.app.monitoring
        self.report["active_stage"] = "monitoring"
        service.start(self.device, (request,), interval=0.5, record_interval=None, cadence="interval")
        task = service.tasks[self.device]
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if not task.running:
                raise DeviceError("Monitoring paused: " + task.error, "monitoring_failed")
            if task.sequence >= 3:
                break
            time.sleep(0.05)
        if task.sequence < 3:
            raise DeviceError("Insufficient valid frames", "monitoring_failed")
        self.report["stages"]["monitoring"] = "passed"
        self.report["active_stage"] = "recording"
        service.set_recording(self.device, True)
        deadline = time.monotonic() + duration
        while task.recorder is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if task.recorder is None:
            raise DeviceError("Recorder not ready", "recording_failed")
        try:
            self.app.sessions.submit(self.device, "apply", DeviceCommand("offset", {"value": 0.0}))
        except DeviceError as exc:
            if exc.code != "task_busy":
                raise
            self.report["recording_parameter_lock"] = "passed"
        else:
            raise DeviceError("Missing recording parameter lock", "lock_failed")
        while time.monotonic() < deadline:
            if not task.running:
                raise DeviceError("Recording paused: " + task.error, "recording_failed")
            if task.recorder.snapshot()["committed"] >= 5:
                break
            time.sleep(0.05)
        service.stop(self.device).result(60)
        task.recorder.finished.result(60)
        status = task.recorder.snapshot()
        self.report["recording"] = {
            **status,
            "folder": str(task.recorder.folder),
            "metadata_only": metadata_only,
        }
        self.report["monitoring"] = {"frames": task.sequence, "timings": list(task.timings)}
        if (
            not status["binary_done"]
            or status.get("error")
            or status.get("csv_error")
            or status["committed"] < 5
            or status["received"] != status["committed"]
        ):
            raise DeviceError("Recording did not drain", "recording_failed")
        if not metadata_only and status["converted"] != status["committed"]:
            raise DeviceError("CSV conversion incomplete", "recording_failed")
        self.report["stages"]["recording"] = "passed"

    def coordinates(self):
        self.report["active_stage"] = "display_coordinates"
        if self.initial["values"]["1.time_reference"] != 50:
            raise DeviceError("This qualification requires the centered reference", "unsupported")
        self.work(self.adapter.prepare_acquisition)
        self.apply("channel_enabled", True, 1)
        self.apply("timebase", 0.001)
        self.apply("trigger_mode", "AUTO")
        self.report["coordinate_checks"] = []
        for position in (0.001, -0.001):
            self.apply("time_position", position)
            batch = self.app.sessions.submit(
                self.device, "acquire", AcquisitionRequest("waveform", (1,))
            ).result(60)
            measured = batch.measurements[0]
            center = (float(measured.axis[0]) + float(measured.axis[-1])) / 2
            step = float(measured.axis[1] - measured.axis[0])
            self.report["coordinate_checks"].append(
                {
                    "requested_position": position,
                    "axis_center": center,
                    "increment": step,
                    "display": measured.metadata["scope_display"],
                }
            )
            if abs(center - position) > 2 * step:
                raise DeviceError("Display center differs from exported coordinates", "coordinate_mismatch")
        self.report["stages"]["display_coordinates"] = "passed"

    def restore(self):
        self.app.monitoring.stop(self.device).result(60)
        current = self.work(
            snapshot, self.adapter
        )  # Unreadable/closed transport stops all restoration writes.
        if not self.changed and not self.adapter.acquisition_preparation:
            return current
        initial = self.initial
        if current["state"] == "RUN":
            self.apply("stop")
        # Probe ratio precedes scales/offsets/levels; timebase precedes time position.
        operations = (
            "probe_attenuation",
            "coupling",
            "scale",
            "offset",
            "trigger_level",
            "bandwidth_limit",
            "channel_enabled",
            "timebase",
            "time_position",
            "trigger_source",
            "trigger_slope",
            "averages",
            "acquisition_mode",
            "decimation",
            "acquisition_count",
        )
        for operation in operations:
            cap = self.adapter.profile.commands[operation].capability
            for ch in range(1, 5) if cap.channel_scoped else (1,):
                original = initial["values"][f"{ch}.{operation}"]
                if not equivalent(self.work(self.adapter.read, operation, ch), original):
                    self.apply(operation, original, ch)
        for query, command, original in (
            ("ACQ:POIN:AUT?", "ACQ:POIN:AUT", "1" if initial["values"]["1.record_length_auto"] else "0"),
            ("TRIG:A:MODE?", "TRIG:A:MODE", initial["values"]["1.trigger_sweep"]),
            ("FORM?", "FORM", initial["format"]),
            ("FORM:BORD?", "FORM:BORD", initial["byte_order"]),
        ):
            actual = self.work(self.adapter.transport.query, query).decode().strip()
            if actual != original:
                self.work(self.adapter.transport.write, command + " " + original)
                if self.work(self.adapter.transport.query, query).decode().strip() != original:
                    raise DeviceError("Restoration readback mismatch: " + query, "readback_mismatch")
        if not initial["values"]["1.record_length_auto"]:
            original = initial["values"]["1.record_length"]
            if self.work(self.adapter.read, "record_length") != original:
                self.apply("record_length", original)
        if initial["state"] == "RUN":
            self.apply("run")
        return self.work(snapshot, self.adapter)


def verify(config, folder, *, phase="readonly", duration=30, restore_from=None):
    report = {
        "adapter_id": "rs.rtm3004",
        "model": "RTM3004",
        "started_at": utc_now(),
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "scope": f"Empty-input {phase}; exact selected asset only; physical precision not tested",
        "environment": {"python": platform.python_version()},
        "source_sha256": {
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in p.parts
        },
        "stages": {
            name: "not_tested"
            for name in (
                "enumeration",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "parameter_write",
                "waveform",
                "monitoring",
                "recording",
                "restoration",
                "reconnection",
                "physical_output",
            )
        },
        "hardware_accepted": False,
    }
    app = exercise = initial = None
    restoration_attempted = False
    report["active_stage"] = "compatibility_connection"
    save(folder / "results.json", report)
    try:
        report["paths"] = verify_runtime_paths(runtime_home(), hardware_lock_home())
        app = ApplicationRuntime((config,), root=runtime_home())
        if phase == "acquisition":
            install_metadata_sinks(app, report)  # Before connect and first frame.
        files_before = set(app.repository.root.rglob("*"))

        def factory(selected):
            adapter = create_adapter(selected, app.visa)
            if not isinstance(adapter, Rtm3004Adapter):
                adapter.close()
                raise DeviceError("Factory mismatch", "identity_conflict")
            adapter.transport = AuditTransport(adapter.transport, folder)
            return adapter

        app.sessions.factory = factory
        device = app.add(config)
        report["device_id"] = device
        report["identity"] = jsonable(app.connect(device, configure_acquisition=False).result(60))
        report["stages"].update(identity="passed", compatibility_connection="passed")
        session = app.sessions.sessions[device]
        adapter = session.adapter
        assert isinstance(adapter, Rtm3004Adapter)
        report["active_stage"] = "parameter_read"
        initial = session.worker.submit(snapshot, adapter).result(60)
        report["initial_snapshot"] = initial
        save(folder / "initial.json", initial)
        report["stages"]["parameter_read"] = "passed"
        if restore_from is not None:
            if phase != "restore":
                raise ValueError("A recovery source is only valid for the explicit restore phase")
            source = json.loads((restore_from / "results.json").read_text(encoding="utf-8"))
            if source["identity"] != report["identity"]:
                raise DeviceError("Restoration source identity differs", "identity_conflict")
            initial = source["initial_snapshot"]
            report["recovery_source"] = str(restore_from)
            report["recovery_target_snapshot"] = initial
        exercise = Exercises(app, device, folder, report, initial)
        assert isinstance(adapter.transport, AuditTransport)
        adapter.transport.allow_writes = phase != "readonly"
        if phase != "readonly":
            report["preexisting_errors"] = []
            for _ in range(20):
                error = exercise.work(adapter.transport.query, "SYST:ERR?").decode().strip()
                report["preexisting_errors"].append(error)
                if error.startswith(("0,", "+0,")):
                    break
            else:
                raise DeviceError("Error queue did not drain before tests", "preflight_failed")
        if phase == "controls":
            exercise.controls()
        elif phase == "coordinates":
            exercise.coordinates()
        elif phase in {"acquisition", "recording"}:
            exercise.acquisition(metadata_only=phase == "acquisition", duration=duration)
        elif phase == "restore" and restore_from is not None:
            exercise.changed.append((1, "explicit_recovery"))
        elif phase != "readonly":
            raise ValueError("Unknown phase")
        report["active_stage"] = "restoration"
        restoration_attempted = True
        report["final_state"] = "Restoration in progress; final state not yet verified"
        final = exercise.restore()
        report["final_snapshot"] = final
        report["restoration_differences"] = differences(initial, final)
        if report["restoration_differences"]:
            raise DeviceError("Restoration mismatch", "restoration_failed")
        report["stages"]["restoration"] = "passed" if phase != "readonly" else "not_tested"
        exercise = None  # Restoration completed, no implicit retry in finally.
        report["active_stage"] = "reconnection"
        app.sessions.disconnect(device)
        report["reconnected_identity"] = jsonable(app.connect(device, configure_acquisition=False).result(60))
        session = app.sessions.sessions[device]
        observed = session.worker.submit(snapshot, session.adapter).result(60)
        report["reconnected_snapshot"] = observed
        if differences(initial, observed):
            raise DeviceError("Reconnected state mismatch", "readback_mismatch")
        report["stages"]["reconnection"] = "passed"
        report["final_state"] = (
            "Verified control settings and transfer format restored; acquisition "
            + observed["state"]
            + "; no signal-generator commands. Captured waveform history is not restored."
        )
        if phase == "acquisition":
            forbidden = [
                str(p)
                for p in set(app.repository.root.rglob("*")) - files_before
                if p.suffix.lower() in {".csv", ".npy", ".npz", ".png", ".bin"}
                or p.name.startswith("segment-")
            ]
            report["unexpected_curve_files"] = forbidden
            report["stages"]["metadata_only"] = "failed" if forbidden else "passed"
    except Exception as exc:
        report["error"] = repr(exc)
        report["traceback"] = traceback.format_exc()
        report["stages"][report["active_stage"]] = "failed"
        if restoration_attempted and exercise is not None:
            report["restoration_error"] = repr(exc)
            report["final_state"] = (
                "Restoration incomplete; no automatic replay; inspect final observations and backup"
            )
            try:
                report["final_observed_snapshot"] = exercise.work(snapshot, exercise.adapter)
            except Exception as read_error:
                report["final_read_error"] = repr(read_error)
    finally:
        if exercise is not None and not restoration_attempted:
            try:
                final = exercise.restore()
                report["final_snapshot"] = final
                report["restoration_differences"] = differences(initial, final)
                report["stages"]["restoration"] = "failed" if report["restoration_differences"] else "passed"
                report["final_state"] = (
                    "Observed acquisition "
                    + final["state"]
                    + "; restoration differences recorded; waveform history not restored"
                )
            except Exception as exc:
                report["restoration_error"] = repr(exc)
                report["stages"]["restoration"] = "failed"
                report["final_state"] = (
                    "Restoration incomplete; see last communication and backup; final state not fully verified"
                )
        if app:
            app.close()
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        (folder / "record.md").write_text(
            "# RTM3004 verification\n\n"
            + report["scope"]
            + "\n\n"
            + report.get("final_state", "No setting writes before backup")
            + "\n\nSee results.json and communication.jsonl. Physical precision is not tested.\n",
            encoding="utf-8",
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--phase",
        choices=("readonly", "controls", "coordinates", "acquisition", "recording"),
        default="readonly",
    )
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1 or configs[0].adapter != "rs.rtm3004" or not configs[0].expected_serial:
        parser.error("Select one RTM3004 with an expected serial")
    config = configs[0]
    serial = config.expected_serial.replace("/", "_").replace("\\", "_")
    folder = (
        runtime_home()
        / "hardware-tests"
        / ("RTM3004-" + serial)
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + args.phase)
    )
    folder.mkdir(parents=True)
    result = verify(config, folder, phase=args.phase)
    print(
        json.dumps(
            {
                "folder": str(folder),
                "stages": result["stages"],
                "error": result.get("error"),
                "restoration_error": result.get("restoration_error"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
