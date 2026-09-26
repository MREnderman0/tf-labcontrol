"""Explicit per-asset hardware verification. No discovery or other instruments."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.repository import application_home
from lab_control.transport.diagnostics import measured

from ..adapter import DHO924Adapter
from ..profile import TRANSFER_SETTINGS
from ..protocol import equivalent
from .exercises import Exercises
from .records import publish_report


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_paths(home: Path) -> dict:
    if platform.system() != "Windows":
        return {"home": str(home.resolve())}
    import msvcrt
    from ctypes import wintypes

    f = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    f.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    f.restype = wintypes.DWORD
    result = {}
    for name in ("hardware.lock", "inventory.sqlite3"):
        with (home / name).open("rb") as stream:
            buffer = ctypes.create_unicode_buffer(32768)
            if not f(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0):
                raise ctypes.WinError()
            actual = buffer.value.removeprefix("\\\\?\\")
            if actual.casefold() != str(home / name).casefold():
                raise DeviceError("Formal runtime path is redirected: " + actual, "path_redirected")
            result[name] = actual
    return result


class AuditTransport:
    def __init__(self, inner, folder: Path):
        self.inner, self.folder, self.resource = inner, folder, inner.resource
        self.allow_writes = False
        self.sequence = 0
        self.last = ""
        self.samples: set[tuple[str, int]] = set()

    def log(self, **row):
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": utc_now(), **row}, ensure_ascii=False) + "\n")

    def close(self):
        self.inner.close()

    def open(self):
        raise RuntimeError("Session owns opening")

    def write(self, command):
        if not isinstance(command, str) or any(c in command for c in ";\r\n"):
            raise DeviceError("Unreviewed compound/raw command", "unsupported")
        if ("?" not in command or command == ":SYST:ERR?") and not self.allow_writes:
            raise DeviceError("No writes/error-queue reads before verified backup", "unsupported")
        self.last = command
        self.log(direction="tx", command=command)
        self.inner.write(command)

    def read(self):
        try:
            raw = self.inner.read()
            self.record(raw)
            return raw
        except Exception as exc:
            self.log(direction="error", error=str(exc))
            raise

    def record(self, raw):
        self.sequence += 1
        if self.last == ":WAV:DATA?" or raw.startswith(b"#"):
            evidence = ""
            signature = (self.last, len(raw))
            if signature not in self.samples:
                self.samples.add(signature)
                evidence = f"response-{self.sequence:05d}.bin"
                (self.folder / evidence).write_bytes(raw)
            self.log(
                direction="rx", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), evidence=evidence
            )
        else:
            self.log(direction="rx", text=raw.decode("ascii", errors="backslashreplace"))

    @measured("query")
    def query(self, command):
        self.write(command)
        return self.read()

    def query_binary(self, command, max_bytes):
        self.last = command
        self.log(direction="tx", command=command, limit=max_bytes)
        try:
            raw = self.inner.query_binary(command, max_bytes)
            self.record(raw)
            return raw
        except Exception as exc:
            self.log(direction="error", error=str(exc))
            raise

    def query_timeout(self, command, seconds):
        self.log(direction="tx", command=command, timeout=seconds)
        raw = self.inner.query_timeout(command, seconds)
        self.last = command
        self.record(raw)
        return raw


def snapshot(adapter: DHO924Adapter) -> dict:
    values, stamps = adapter.snapshot()
    return {
        "values": values,
        "timestamps": stamps,
        "transfer": {op: adapter.read(op) for op in TRANSFER_SETTINGS},
        "trigger_mode": adapter.read("trigger_mode"),
    }


def differences(before: dict, after: dict) -> dict:
    delta = {}
    for section in ("values", "transfer"):
        for key, value in before[section].items():
            if not equivalent(value, after[section].get(key)):
                delta[f"{section}.{key}"] = {"before": value, "after": after[section].get(key)}
    if before["trigger_mode"] != after["trigger_mode"]:
        delta["trigger_mode"] = {"before": before["trigger_mode"], "after": after["trigger_mode"]}
    return delta


def verify(config: ConnectionConfig, folder: Path, *, phase: str = "readonly") -> dict:
    import pyvisa

    report: dict[str, Any] = {
        "started_at": utc_now(),
        "adapter_id": "rigol.dho924",
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "scope": f"DHO924 {phase}; four analog inputs disconnected",
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "project_head": os.environ.get("LAB_TEST_PROJECT_HEAD", "unavailable"),
        "source_sha256": {
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in p.parts
        },
        "stages": {
            key: "not_tested"
            for key in (
                "identity",
                "connection",
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
    }
    app = None
    adapter = None
    session = None
    initial = None
    exercise = None
    stage = "connection"
    try:
        if report["project_head"] == "unavailable":
            try:
                report["project_head"] = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True
                ).strip()
            except OSError:
                pass
        report["paths"] = verify_paths(application_home())
        app = ApplicationRuntime((config,))

        def factory(selected):
            obj = create_adapter(selected, app.visa)
            if not isinstance(obj, DHO924Adapter):
                obj.close()
                raise DeviceError("Factory mismatch", "identity_conflict")
            obj.transport = AuditTransport(obj.transport, folder)
            return obj

        app.sessions.factory = factory
        device_id = app.add(config)
        report["identity"] = jsonable(app.connect(device_id, configure_acquisition=False).result(60))
        report["stages"].update(identity="passed", connection="passed")
        report["environment"]["visa"] = str(app.visa.manager.visalib)
        session = app.sessions.sessions[device_id]
        adapter = session.adapter
        assert isinstance(adapter, DHO924Adapter)
        stage = "parameter_read"
        initial = session.worker.submit(snapshot, adapter).result(60)
        report["initial_snapshot"] = initial
        save(folder / "initial.json", initial)
        report["stages"][stage] = "passed"
        if phase != "readonly":
            assert isinstance(adapter.transport, AuditTransport)
            adapter.transport.allow_writes = True
            exercise = Exercises(app, device_id, folder, report)
            if phase == "protocol":
                stage = "waveform"
                exercise.qualify_word()
                report["stages"][stage] = "passed"
            elif phase == "functional":
                exercise.controls()
                exercise.waveforms()
                exercise.monitoring()
            elif phase == "completion":
                exercise.completion_probe()
            elif phase == "acquisition":
                exercise.waveforms()
                exercise.monitoring(duration=60)
            else:
                raise ValueError("Unknown test phase")
            return report
        stage = "reconnection"
        report["reconnections"] = []
        for attempt in range(3):
            app.sessions.disconnect(device_id)
            identity = app.connect(device_id, configure_acquisition=False).result(60)
            current = app.sessions.sessions[device_id]
            assert isinstance(current.adapter, DHO924Adapter)
            final = current.worker.submit(snapshot, current.adapter).result(60)
            delta = differences(initial, final)
            report["reconnections"].append(
                {"attempt": attempt + 1, "identity": jsonable(identity), "differences": delta}
            )
            report["final_snapshot"] = final
            if delta:
                raise DeviceError("Read-only state differs", "readback_mismatch")
        report["stages"][stage] = "passed"
        report["final_state"] = "Read-only verification; no setting commands sent"
    except Exception as exc:
        stage = report.get("active_stage", stage)
        report["error"] = str(exc)
        report["error_code"] = getattr(exc, "code", type(exc).__name__)
        report["stages"][stage] = (
            "blocked"
            if report["error_code"]
            in {"process_busy", "resource_busy", "path_redirected", "connection_failed", "timeout"}
            else "failed"
        )
    finally:
        if exercise is not None and initial is not None and session is not None:
            try:
                assert isinstance(adapter, DHO924Adapter)
                assert app is not None
                app.monitoring.stop(device_id).result(60)
                # Read the current state before a single, backed-up restoration.
                # A closed/uncertain transport aborts here instead of reconnecting.
                session.worker.submit(snapshot, adapter).result(60)
                exercise.restore_initial(initial)
                final = session.worker.submit(snapshot, adapter).result(60)
                report["final_snapshot"] = final
                report["restoration_differences"] = differences(initial, final)
                save(folder / "final.json", final)
                report["stages"]["restoration"] = "failed" if report["restoration_differences"] else "passed"
            except Exception as exc:
                report["restoration_error"] = str(exc)
                report["stages"]["restoration"] = "failed"
                try:
                    assert isinstance(adapter, DHO924Adapter)
                    observed = session.worker.submit(snapshot, adapter).result(60)
                    report["final_observed_snapshot"] = observed
                    report["restoration_differences"] = differences(initial, observed)
                    save(folder / "final-observed.json", observed)
                except Exception as read_error:
                    report["final_state"] = "Unknown: " + str(read_error)
        if app:
            app.close()
            report["session_released"] = True
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        (folder / "record.md").write_text(
            "# DHO924 verification\n\n"
            + report["scope"]
            + "\n\n"
            + json.dumps(report["stages"], indent=2)
            + "\n\n"
            + report.get("error", "")
            + "\n",
            encoding="utf-8",
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--phase",
        choices=("readonly", "protocol", "functional", "completion", "acquisition"),
        default="readonly",
    )
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1 or configs[0].adapter != "rigol.dho924" or not configs[0].expected_serial:
        parser.error("Select one DHO924 with an expected serial")
    config = configs[0]
    folder = (
        application_home()
        / "hardware-tests"
        / ("DHO924-" + config.expected_serial)
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + args.phase)
    )
    folder.mkdir(parents=True)
    report = verify(config, folder, phase=args.phase)
    print(
        json.dumps(
            {"folder": str(folder), "stages": report["stages"], "error": report.get("error")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
