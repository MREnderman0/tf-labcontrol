"""Explicit, locked LAN acceptance. Every attempt preserves original evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyvisa

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.recording import read_frames
from lab_control.storage.repository import application_home
from lab_control.storage.runtime import hardware_lock_home, physical_path

from ..adapter import Keysight53220aAdapter
from ..protocol import token
from .records import publish_report


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2), encoding="utf-8")


class Transcript:
    def __init__(self, inner, folder: Path):
        self.inner, self.folder, self.resource = inner, folder, inner.resource

    def log(self, direction, value):
        raw = value if isinstance(value, bytes) else str(value).encode("ascii", "backslashreplace")
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": utc_now(),
                        "direction": direction,
                        "ascii": raw.decode("ascii", "backslashreplace"),
                        "hex": raw.hex(),
                    }
                )
                + "\n"
            )

    def write(self, command):
        self.log("tx", command)
        try:
            return self.inner.write(command)
        except Exception as exc:
            self.log("error", str(exc))
            raise

    def query(self, command):
        self.log("tx", command)
        try:
            raw = self.inner.query(command)
            self.log("rx", raw)
            return raw
        except Exception as exc:
            self.log("error", str(exc))
            raise

    def query_binary_timeout(self, command, max_bytes, seconds):
        self.log("tx", command)
        try:
            raw = self.inner.query_binary_timeout(command, max_bytes, seconds)
            self.log("rx", raw)
            return raw
        except Exception as exc:
            self.log("error", str(exc))
            raise

    def close(self):
        self.inner.close()

    def open(self):
        self.inner.open()

    def read(self):
        raw = self.inner.read()
        self.log("rx", raw)
        return raw


def verify_readonly(config, folder, *, duration=0, root=None, input_condition="unspecified"):
    return verify(config, Path(folder), root=root, input_condition=input_condition)


def verify(
    config: ConnectionConfig,
    folder: Path,
    *,
    root=None,
    control=False,
    duration=0.0,
    input_condition="CH1 signal unspecified; CH2 open",
    test_empty=False,
    restore_from: Path | None = None,
):
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "results.json").exists():
        raise ValueError("Do not overwrite an existing batch")
    report: dict[str, Any] = {
        "adapter_id": "keysight.53220a",
        "model": "53220A",
        "started_at": utc_now(),
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "scope": {
            "input_condition": input_condition,
            "control": control,
            "duration": duration,
            "test_empty_ch2": test_empty,
        },
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "stages": {
            name: {"state": "not_tested"}
            for name in (
                "enumeration",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "parameter_write",
                "restoration",
                "reconnection",
                "frequency_read",
                "monitoring",
                "recording",
                "frame_export",
                "no_signal",
                "physical_accuracy",
                "physical_output",
            )
        },
        "operations": [],
        "hardware_accepted": False,
        "final_state": "unknown; no setting writes attempted",
    }
    try:
        report["source_version"] = {
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "dirty": subprocess.check_output(["git", "status", "--porcelain"], text=True),
        }
    except (OSError, subprocess.CalledProcessError):
        report["source_version"] = "unavailable"
    report["model_source_hashes"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parents[1].glob("*.py")
    }
    app = None
    device = ""
    initial = None
    stage = "enumeration"
    modified = False
    try:
        app = ApplicationRuntime((config,), root=root)
        report["runtime"] = {
            "root": str(app.repository.root),
            "inventory": str(physical_path(app.repository.root / "inventory.sqlite3")),
            "hardware_lock": str(physical_path(hardware_lock_home() / "hardware.lock")),
        }
        try:
            report["advertisements"] = app.visa.discover_lan(timeout=2)
            report["stages"][stage] = {
                "state": "passed" if config.resource in report["advertisements"] else "not_tested",
                "reason": "Advertisement result recorded; explicit selected address is separately verified",
            }
        except Exception as exc:
            report["stages"][stage] = {"state": "blocked", "error": str(exc)}

        def factory(selected):
            if (selected.resource, selected.expected_serial, selected.adapter) != (
                config.resource,
                config.expected_serial,
                config.adapter,
            ):
                raise DeviceError("Only the selected asset is allowed", "identity_conflict")
            adapter = create_adapter(selected, app.visa)
            assert isinstance(adapter, Keysight53220aAdapter)
            adapter.transport = Transcript(adapter.transport, folder)
            return adapter

        app.sessions.factory = factory
        device = app.add(config)
        stage = "identity"
        identity = app.connect(device, configure_acquisition=False).result(30)
        report["identity"] = jsonable(identity)
        report["protocol"] = "VISA SOCKET" if config.resource.endswith("::SOCKET") else "VXI-11"
        report["environment"]["visa"] = str(app.visa.manager.visalib)
        report["stages"].update(identity={"state": "passed"}, compatibility_connection={"state": "passed"})

        def on_worker(fn):
            session = app.sessions.sessions[device]
            return session.worker.submit(lambda: fn(session.adapter)).result(90)

        stage = "parameter_read"
        initial = on_worker(lambda a: a.snapshot())
        report["initial_snapshot"] = initial
        save(folder / "initial_snapshot.json", initial)
        report["stages"][stage] = {"state": "passed"}
        report["options"] = on_worker(lambda a: a.detected_options)
        report["channel3_installed"] = any(v in {"106", "115"} for v in report["options"])
        if control or duration > 0 or test_empty or restore_from:

            def drain_prior_errors(a):
                errors = []
                for _ in range(32):
                    error = token(a.transport.query("SYST:ERR?"))
                    errors.append(error)
                    if error.split(",", 1)[0] in {"0", "+0"}:
                        return errors
                raise DeviceError("Error queue did not drain within 32 reads", "instrument_error")

            report["prior_error_queue"] = on_worker(drain_prior_errors)
            save(folder / "progress.json", report)
        if restore_from:
            previous = json.loads(restore_from.read_text(encoding="utf-8"))
            if (previous["expected_serial"], previous["resource"]) != (
                config.expected_serial,
                config.resource,
            ):
                raise DeviceError("Recovery asset differs from selected target", "identity_conflict")
            report["recovery_source"] = str(restore_from)
            report["recovery_source_sha256"] = hashlib.sha256(restore_from.read_bytes()).hexdigest()
            report["pre_recovery_snapshot"] = initial
            initial = previous["initial_snapshot"]
            modified = True
        if control:
            stage = "parameter_write"
            # CH1 has an externally supplied signal of unknown amplitude: exercise CH2 inputs only.
            targets: list[tuple[str, int, Any]] = [("gate_time", 1, value) for value in (0.1, 1.0)]
            targets += [
                (name, 2, value)
                for name, values in {
                    "coupling": ("DC", "AC"),
                    "input_impedance": (50, 1000000),
                    "input_range": (50, 5),
                    "auto_level": (False, True),
                    "input_slope": ("NEG", "POS"),
                    "input_filter": (True, False),
                }.items()
                for value in values
            ]
            targets.append(("trigger_level", 2, 0.1))
            for name, channel, value in targets:
                modified = True  # Mark before a possibly uncertain write.
                if name == "trigger_level":
                    prerequisite = app.sessions.submit(
                        device, "apply", DeviceCommand("auto_level", {"value": False}, 2)
                    ).result(30)
                    report["operations"].append(
                        {"operation": "manual_level_prerequisite", "result": jsonable(prerequisite)}
                    )
                    if prerequisite.outcome != "success":
                        raise DeviceError("Cannot disable CH2 auto level", "readback_mismatch")
                result = app.sessions.submit(
                    device, "apply", DeviceCommand(name, {"value": value}, channel)
                ).result(30)
                entry = {"operation": name, "channel": channel, "request": value, "result": jsonable(result)}
                report["operations"].append(entry)
                if result.outcome != "success":
                    raise DeviceError(f"Control failed: {entry}", "readback_mismatch")
                # The opposite channel's input settings must not change.
                opposite = on_worker(lambda a: a.snapshot())
                entry["opposite_channel"] = opposite
                isolated = all(
                    value == opposite[key]
                    for key, value in initial.items()
                    if key.startswith("INP1:") and key != "INP1:LEV"
                )
                entry["channel_isolation_passed"] = isolated
                if not isolated:
                    raise DeviceError("CH1 settings changed during CH2 test", "configuration_changed")
                restored = on_worker(lambda a: a.restore(initial))
                entry["restoration"] = restored
                save(folder / "progress.json", report)
                if not restored["settings_match"]:
                    raise DeviceError("Per-step restoration mismatch", "restoration_failed")
            report["stages"][stage] = {
                "state": "passed",
                "reason": "Gate and CH2 input controls; CH1 impedance not changed",
            }
        if duration > 0:
            stage = "frequency_read"
            modified = True
            request = AcquisitionRequest(
                kind="scalar", quantity="frequency", channels=(1,), points=1, initiate=True, confirmed=True
            )
            first = app.sessions.submit(device, "acquire", request).result(45)
            report["first_frame"] = jsonable(first)
            measured = float(first.measurements[0].values[0])
            report["stages"][stage] = {
                "state": "passed",
                "frequency_hz": measured,
                "approximately_100MHz": 90e6 <= measured <= 110e6,
                "reason": "User supplied approximate reference; not an accuracy calibration",
            }
            stage = "monitoring"
            app.monitoring.start(device, (request,), interval=0.5, recording=True, record_interval=None)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                state = app.monitoring.snapshots()[device]
                if state.get("error"):
                    raise DeviceError(str(state["error"]), "monitoring_failed")
                time.sleep(0.1)
            app.monitoring.stop(device).result(90)
            report["monitoring"] = app.monitoring.snapshots()[device]
            task = app.monitoring.tasks[device]
            if task.recorder is None:
                raise DeviceError("Recording did not start", "recording_failed")
            paths = task.recorder.finish().result(90)
            report["recording"] = {"paths": paths, "state": task.recorder.snapshot()}
            saved_frames = [frame for path in paths for frame in read_frames(Path(path))]
            report["recording"]["verified_frame_count"] = len(saved_frames)
            report["recording"]["frequency_range_hz"] = (
                [
                    min(float(f["measurements"][0]["values"][0]) for f in saved_frames),
                    max(float(f["measurements"][0]["values"][0]) for f in saved_frames),
                ]
                if saved_frames
                else []
            )
            report["stages"][stage] = {"state": "passed"}
            report["stages"]["recording"] = {
                "state": "passed"
                if len(saved_frames) > 0
                and len(saved_frames)
                == report["recording"]["state"]["committed"]
                == report["recording"]["state"]["received"]
                else "failed"
            }
            stage = "frame_export"
            latest = app.monitoring.latest(device, "scalar", "frequency")
            if latest is None:
                raise DeviceError("No valid frame for export", "recording_failed")
            report["frame_export"] = str(
                app.monitoring.export_displayed_frame(device, latest, "general").result(90)
            )
            report["stages"][stage] = {"state": "passed"}
        if test_empty:
            stage = "no_signal"
            modified = True
            try:
                frame = app.sessions.submit(
                    device,
                    "acquire",
                    AcquisitionRequest(
                        kind="scalar",
                        quantity="frequency",
                        channels=(2,),
                        points=1,
                        initiate=True,
                        confirmed=True,
                    ),
                ).result(45)
                report["stages"][stage] = {
                    "state": "passed"
                    if any(m.quality.value != "good" for m in frame.measurements)
                    else "not_tested",
                    "reason": "Invalid measurement rejected"
                    if any(m.quality.value != "good" for m in frame.measurements)
                    else "Open input returned a number; no signal-validity conclusion",
                    "frame": jsonable(frame),
                    "valid_frame_saved": False,
                }
            except DeviceError as exc:
                report["stages"][stage] = {
                    "state": "passed" if exc.code in {"timeout", "overload"} else "failed",
                    "error": str(exc),
                    "code": exc.code,
                    "valid_frame_saved": False,
                }
    except Exception as exc:
        report["error"] = str(exc)
        report["stages"][stage] = {
            "state": "blocked"
            if getattr(exc, "code", "") in {"process_busy", "resource_busy", "driver_missing"}
            else "failed",
            "error": str(exc),
        }
    finally:
        if app and device and initial is not None:
            try:
                if device in app.monitoring.tasks:
                    app.monitoring.stop(device).result(90)
                if modified:
                    # Reopen once under the same process/asset gate if framing became uncertain.
                    session = app.sessions.sessions[device]
                    if session.state in {"error", "degraded"}:
                        app.sessions.disconnect(device)
                        app.connect(device, configure_acquisition=False).result(30)
                    restored = on_worker(lambda a: a.restore(initial))
                    report["restoration"] = restored
                    report["stages"]["restoration"] = {
                        "state": "passed" if restored["settings_match"] else "failed"
                    }
                    report["final_state"] = restored
                else:
                    report["final_state"] = {
                        "snapshot": on_worker(lambda a: a.snapshot()),
                        "setting_writes": False,
                    }
                app.sessions.disconnect(device)
                identity = app.connect(device, configure_acquisition=False).result(30)
                report["reconnected_identity"] = jsonable(identity)
                report["reconnected_snapshot"] = on_worker(lambda a: a.snapshot())
                differences = Keysight53220aAdapter.snapshot_differences(
                    initial, report["reconnected_snapshot"]
                )
                report["stages"]["reconnection"] = {
                    "state": "failed" if differences else "passed",
                    "differences": differences,
                }
            except Exception as exc:
                report["recovery_error"] = str(exc)
                report["stages"]["restoration"] = {"state": "failed", "error": str(exc)}
                report["final_state"] = "unknown; restoration not confirmed"
        if app:
            try:
                app.close()
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        rows = "\n".join(
            f"| {k} | {v['state']} | {v.get('reason', v.get('error', ''))} |"
            for k, v in report["stages"].items()
        )
        (folder / "record.md").write_text(
            f"# 53220A LAN 实机验证\n\n时间：{report['started_at']} 至 {report['finished_at']}。\n\n"
            f"目标：{config.expected_serial}；资源：`{config.resource}`。\n\n接线：{input_condition}。\n\n"
            f"| 阶段 | 结果 | 说明 |\n| --- | --- | --- |\n{rows}\n\n"
            "恢复以 results.json 的逐项比较为准；不恢复先前在途测量。约 100 MHz 是合理性参考，未做精度校准或整机验收。\n\n"
            f"错误：{report.get('error', '无')}；恢复错误：{report.get('recovery_error', '无')}。\n\n"
            "[原始结果](results.json) · [通信记录](communication.jsonl)\n",
            encoding="utf-8",
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--control", action="store_true")
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--test-empty", action="store_true")
    parser.add_argument("--restore-from", type=Path)
    parser.add_argument("--input-condition", default="CH1 ~100 MHz; amplitude unknown; CH2 open")
    args = parser.parse_args()
    config = ConnectionConfig(
        args.resource, "keysight.53220a", expected_serial=args.expected_serial, timeout=5
    )
    from lab_control.instruments.common.readonly_hardware import asset_folder_name

    folder = (
        application_home()
        / "hardware-tests"
        / asset_folder_name("53220A", args.expected_serial)
        / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
            + (
                "-recovery"
                if args.restore_from
                else "-control"
                if args.control
                else "-acquisition"
                if args.duration or args.test_empty
                else "-readonly"
            )
        )
    )
    print(folder, flush=True)
    report = verify(
        config,
        folder,
        control=args.control,
        duration=args.duration,
        test_empty=args.test_empty,
        input_condition=args.input_condition,
        restore_from=args.restore_from,
    )
    print(json.dumps(report["stages"], ensure_ascii=False), flush=True)
    return int(bool(report.get("error") or report.get("recovery_error")))


if __name__ == "__main__":
    raise SystemExit(main())
