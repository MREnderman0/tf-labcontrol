"""Explicit, recorded four-channel test. All signal outputs remain OFF."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.service import ConfiguredDiscoveryProvider
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.devices.rigol_dg5504pro.adapter import MODEL_QUERIES, Dg5504ProAdapter
from lab_control.instruments.devices.rigol_dg5504pro.profile import dg5504pro
from lab_control.instruments.devices.rigol_dg5504pro.protocol import state
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home

COUPLING = {
    "track": "TRAC",
    "frequency_coupling": "FREQ:COUP:STAT",
    "amplitude_coupling": "VOLT:COUP:STAT",
    "phase_coupling": "PHAS:COUP:STAT",
}
MODES = ("sweep", "burst", "am_enabled", "fm_enabled", "pm_enabled", "arb_enabled")
EXTRA = {
    "pulse_period": "FUNC:PULS:PER",
    "pulse_leading": "FUNC:PULS:TRAN:LEAD",
    "pulse_trailing": "FUNC:PULS:TRAN:TRA",
    "sweep_hold_start": "SWE:HTIM:STAR",
    "sweep_hold_stop": "SWE:HTIM:STOP",
    "sweep_return": "SWE:RTIM",
    "burst_period": "BURS:INT:PER",
    "burst_phase": "BURS:PHAS",
    "am_dssc": "AM:DSSC",
}


def differences(expected: Any, actual: Any, path: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        return [
            diff
            for key in expected.keys() | actual.keys()
            for diff in differences(expected.get(key), actual.get(key), path + "." + str(key))
        ]
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return [] if math.isclose(expected, actual, rel_tol=1e-5, abs_tol=1e-9) else [path]
    return [] if expected == actual else [path]


def restore_steps(actions: list[tuple[str, Callable[[], Any]]]) -> list[dict[str, str]]:
    result = []
    for label, action in actions:
        try:
            action()
            result.append({"step": label, "status": "passed"})
        except Exception as exc:
            result.append({"step": label, "status": "failed", "error": repr(exc)})
            break
    return result


def same_saved_value(expected: Any, actual: str) -> bool:
    try:
        return math.isclose(float(expected), float(actual), rel_tol=1e-5, abs_tol=1e-9)
    except (ValueError, TypeError):
        text = str(expected).upper()
        return actual.upper() == text or (text in {"ON", "OFF"} and state(actual) == state(text))


def restore_value(adapter: Dg5504ProAdapter, ch: int, path: str, value: Any) -> tuple[str, bool]:
    """Read first: restore only a verified difference, never replay an unchanged setting."""
    actual = adapter.transport.query(f"SOUR{ch}:{path}?").decode().strip()
    if same_saved_value(value, actual):
        return actual, False
    encoded = f"{value:.12g}" if isinstance(value, (int, float)) else str(value)
    adapter.transport.write(f"SOUR{ch}:{path} {encoded}")
    return adapter.transport.query(f"SOUR{ch}:{path}?").decode().strip(), True


def validate_recovery_snapshot(snapshot: dict[str, Any]) -> None:
    """Reject incomplete evidence before any recovery connection or write."""
    required = (
        {name for name, spec in dg5504pro().commands.items() if spec.query and spec.capability.channel_scoped}
        | set(MODEL_QUERIES)
        | set(EXTRA)
        | {"sync_output", "trigger_output", "other_modes"}
    )
    if set(snapshot) != {"1", "2", "3", "4", "global"}:
        raise DeviceError("Complete four-channel recovery snapshot required", "backup_incomplete")
    for channel in ("1", "2", "3", "4"):
        values = snapshot[channel]
        if not isinstance(values, dict) or not required <= values.keys():
            raise DeviceError(f"Incomplete CH{channel} recovery snapshot", "backup_incomplete")
        if values["amplitude_unit"] != "VPP" or values["waveform"] not in {"SIN", "SQU", "RAMP", "PULS"}:
            raise DeviceError("Original unit/waveform cannot be restored by this test", "backup_incomplete")
        if any(
            state(values[name])
            for name in ("output", "advanced", "arb_enabled", "sync_output", "trigger_output")
        ):
            raise DeviceError(
                "Recovery requires the original output-OFF basic-mode snapshot", "backup_incomplete"
            )
        if set(values["other_modes"]) != {"ASK", "FSK", "PSK", "PWM", "SUM"} or any(
            state(v) for v in values["other_modes"].values()
        ):
            raise DeviceError("Original unsupported mode cannot be restored", "backup_incomplete")
    if not {"reference", "benchmark", "bundle", "files"} <= snapshot["global"].keys():
        raise DeviceError("Incomplete global recovery snapshot", "backup_incomplete")


class OffTransport:
    def open(self) -> None:
        raise DeviceError("The owning session opens the transport", "unsupported")

    def __init__(self, inner: Any, log: Path):
        self.inner, self.log, self.resource = inner, log, inner.resource
        self.writes = 0
        self.allowed = {
            spec.writes[0].split(" ")[0].format(ch=ch)
            for spec in dg5504pro().commands.values()
            if spec.writes
            for ch in range(1, 5)
        }
        self.allowed.update(
            f"SOUR{ch}:{path}"
            for ch in range(1, 5)
            for path in (*EXTRA.values(), *COUPLING.values(), "FUNC:ADV:STAT")
        )
        self.allowed.update({"MMEM:TRAC:ARB:DATA", "MMEM:DEL"})

    def record(self, direction: str, payload: str) -> None:
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": utc_now(), direction: payload}, ensure_ascii=False) + "\n")

    def write(self, payload: str | bytes) -> None:
        if not isinstance(payload, str) or any(c in payload for c in "\r\n;"):
            raise DeviceError("Test forbids binary/unframed/chained commands", "unsupported")
        header = payload.split(" ")[0].upper().lstrip(":")
        if "?" not in header:
            if header not in self.allowed or header in {"*TRG", "*RST", "*CLS"}:
                raise DeviceError("Command is outside the output-OFF test", "unsupported")
            if re.fullmatch(r"OUTP[1-4]", header) and payload.rsplit(" ", 1)[-1].upper() not in {"OFF", "0"}:
                raise DeviceError("Output ON is forbidden throughout this test", "unsupported")
            self.writes += 1
        self.record("tx", payload)
        try:
            self.inner.write(payload)
        except Exception as exc:
            self.record("write_error", repr(exc))
            raise

    def read(self) -> bytes:
        try:
            raw = self.inner.read()
            self.record("rx", raw.decode("ascii", errors="backslashreplace"))
            return raw
        except Exception as exc:
            self.record("error", repr(exc))
            raise

    def query(self, payload: str | bytes) -> bytes:
        self.write(payload)
        return self.read()

    def close(self) -> None:
        self.inner.close()


class Verification:
    def __init__(self, config: ConnectionConfig, folder: Path):
        self.config, self.folder = config, folder
        self.app: ApplicationRuntime | None = None
        self.device_id = ""
        self.initial: dict[str, Any] = {}
        self.created: set[str] = set()
        self.dirty = False
        self.group = "preparation"
        self.report: dict[str, Any] = {
            "started_at": utc_now(),
            "config": jsonable(config),
            "environment": {"python": platform.python_version()},
            "stages": {
                name: "not_tested"
                for name in (
                    "scan",
                    "identity",
                    "connection",
                    "basic",
                    "pulse_ramp",
                    "sweep",
                    "modulation",
                    "burst",
                    "arbitrary_upload",
                    "arbitrary_readback",
                    "arbitrary_selection",
                    "range_rejection",
                    "repetition",
                    "restoration",
                    "stable_reads",
                    "reconnection",
                    "physical_output",
                )
            },
            "steps": [],
            "scope": "All four outputs OFF; no physical output acceptance",
        }

    def save(self) -> None:
        self.report["updated_at"] = utc_now()
        (self.folder / "results.json").write_text(
            json.dumps(jsonable(self.report), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for name in ("initial_snapshot", "final_snapshot"):
            if name in self.report:
                (self.folder / (name + ".json")).write_text(
                    json.dumps(self.report[name], ensure_ascii=False, indent=2), encoding="utf-8"
                )
        lines = [
            "# DG5504 Pro 输出关闭控制验证",
            "",
            f"资源：{self.config.resource}",
            "",
            "| 项目 | 结果 |",
            "| --- | --- |",
        ]
        lines += [f"| {name} | {status} |" for name, status in self.report["stages"].items()]
        lines += [
            "",
            "未测：实际输出/精度、外部调制、执行触发、序列及未核实选件。",
            "",
            str(self.report.get("error", "")),
            "",
            "任意波形：文件内容下载及原加载文件名无已核实查询，未宣称逐点读回或可恢复加载通过。",
            "",
            "具体请求、读回、错误、输出及恢复差异见 results.json 和 communication.jsonl。",
        ]
        (self.folder / "record.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        from lab_control.instruments.devices.rigol_dg5504pro.hardware.records import publish_report

        publish_report(self.folder)

    def worker(self, fn: Callable[[Dg5504ProAdapter], Any]) -> Any:
        assert self.app
        session = self.app.sessions.sessions[self.device_id]

        def invoke():
            adapter = session.adapter
            if not isinstance(adapter, Dg5504ProAdapter):
                raise DeviceError("DG session is unavailable", "disconnected")
            return fn(adapter)

        return session.worker.submit(invoke).result(timeout=90)

    def errors(self) -> list[str]:
        def read(adapter: Dg5504ProAdapter):
            result = []
            for _ in range(32):
                error = adapter.transport.query("SYST:ERR?").decode().strip()
                result.append(error)
                if re.match(r"^\+?0\s*,", error):
                    return result
            raise DeviceError("Error queue did not terminate within 32 entries", "device_error")

        return self.worker(read)

    def outputs(self) -> dict[str, bool]:
        return self.worker(lambda a: {str(ch): state(a.read("output", ch)) for ch in range(1, 5)})

    def off(self) -> None:
        if any(self.outputs().values()):
            raise DeviceError("Unexpected signal output enabled; stop test writes", "output_state")

    def snapshot(self) -> dict[str, Any]:
        def read(adapter: Dg5504ProAdapter):
            snapshot = {}
            for ch in range(1, 5):
                values = {
                    name: adapter.read(name, ch)
                    for name, spec in adapter.profile.commands.items()
                    if spec.query and spec.capability.channel_scoped
                }
                values.update({name: adapter.read(name, ch) for name in MODEL_QUERIES})
                values.update(
                    {
                        name: adapter.transport.query(f"SOUR{ch}:{path}?").decode().strip()
                        for name, path in EXTRA.items()
                    }
                )
                values["sync_output"] = adapter.transport.query(f"OUTP{ch}:SYNC?").decode().strip()
                values["trigger_output"] = adapter.transport.query(f"OUTP{ch}:TRIG?").decode().strip()
                values["other_modes"] = {
                    name: adapter.transport.query(f"SOUR{ch}:{name}:STAT?").decode().strip()
                    for name in ("ASK", "FSK", "PSK", "PWM", "SUM")
                }
                snapshot[str(ch)] = values
            snapshot["global"] = {
                "reference": adapter.read("reference_clock"),
                **adapter.synchronization(),
                "files": adapter.file_catalog(),
            }
            return snapshot

        return self.worker(read)

    def fingerprint(self, exclude: int, operation: str) -> dict[str, Any]:
        names = {"waveform", "frequency", "amplitude", "offset", "phase", "output", *MODES}
        if operation in dg5504pro().commands and dg5504pro().commands[operation].query:
            names.add(operation)
        return self.worker(
            lambda a: {
                f"{ch}.{name}": a.read(name, ch)
                for ch in range(1, 5)
                if ch != exclude
                for name in sorted(names)
            }
        )

    def apply(self, channel: int, operation: str, value: Any = None, **parameters: Any) -> None:
        assert self.app
        self.off()
        before = self.fingerprint(channel, operation)
        self.dirty = True
        command = DeviceCommand(operation, parameters or {"value": value}, channel, True)
        outcome = self.app.sessions.submit(self.device_id, "apply", command).result(timeout=90)
        entry = {
            "time": utc_now(),
            "group": self.group,
            "channel": channel,
            "request": jsonable(command),
            "result": jsonable(outcome),
        }
        self.report["steps"].append(entry)
        self.save()
        if outcome.outcome != "success":
            raise DeviceError(repr(outcome), "test_failed")
        entry["outputs"] = self.outputs()
        self.off()
        entry["channel_differences"] = differences(before, self.fingerprint(channel, operation))
        if entry["channel_differences"]:
            raise DeviceError("Another channel changed", "channel_isolation")

    def saved_write(self, ch: int, path: str, value: Any) -> None:
        """Model-specific recovery helper, executed on the same session worker."""
        self.off()

        actual, written = self.worker(lambda adapter: restore_value(adapter, ch, path, value))
        errors = self.errors()
        self.report.setdefault("restore_helper", []).append(
            {
                "channel": ch,
                "path": path,
                "request": value,
                "actual": actual,
                "written": written,
                "errors": errors,
            }
        )
        if len(errors) != 1:
            raise DeviceError("Restore/setup query returned device errors", "restoration_failed")
        if not same_saved_value(value, actual):
            raise DeviceError(
                f"Restore readback differs for {path}: {value!r} -> {actual!r}", "restoration_failed"
            )

    def leave_modes(self, ch: int) -> None:
        for mode in MODES:
            if state(self.worker(lambda a: a.read(mode, ch))):
                self.apply(ch, mode, False)

    def run_matrix(self) -> None:
        for ch in range(1, 5):
            for field, path in COUPLING.items():
                if str(self.initial[str(ch)][field]).upper() not in {"OFF", "0", "0.0"}:
                    self.dirty = True
                    self.saved_write(ch, path, "OFF")
        for ch in range(1, 5):
            self.leave_modes(ch)
            self.group = "basic"
            self.apply(ch, "frequency", 1000)
            self.apply(ch, "offset", 0)
            self.apply(ch, "amplitude", 0.1)
            for waveform in ("SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"):
                if waveform not in {"NOIS", "DC"}:
                    self.apply(ch, "frequency", 1000)
                self.apply(ch, "waveform", waveform)
                if waveform == "PULS":
                    self.apply(ch, "pulse_width", 0.000025)
                if waveform not in {"NOIS", "DC"}:
                    self.apply(ch, "frequency", ch * 1000)
                if waveform != "DC":
                    for amplitude in (0.1, 0.2):
                        self.apply(ch, "amplitude", amplitude)
                for offset in (0, -0.05, 0.05):
                    self.apply(ch, "offset", offset)
            self.apply(ch, "waveform", "SIN")
            for phase in (0, 30):
                self.apply(ch, "phase", phase)
            self.group = "pulse_ramp"
            self.apply(ch, "frequency", 1000)
            self.apply(ch, "waveform", "PULS")
            for duty in (25, 75):
                self.apply(ch, "pulse_duty", duty)
            self.apply(ch, "pulse_width", 0.00025)
            self.apply(ch, "waveform", "RAMP")
            for symmetry in (25, 75):
                self.apply(ch, "ramp_symmetry", symmetry)
            self.apply(ch, "waveform", "SIN")
            self.group = "sweep"
            self.apply(ch, "sweep_start", 1000)
            self.apply(ch, "sweep_stop", 2000)
            self.apply(ch, "trigger_source", "EXT")
            for spacing, duration in (("LIN", 1), ("LOG", 2)):
                self.apply(ch, "sweep_spacing", spacing)
                self.apply(ch, "sweep_time", duration)
                self.apply(ch, "sweep", True)
                self.apply(ch, "sweep", False)
            self.group = "modulation"
            self.apply(ch, "frequency", 10000)
            self.apply(ch, "offset", 0)
            self.apply(ch, "amplitude", 0.2)
            for mode, field, values in (
                ("am", "depth", (10, 20)),
                ("fm", "deviation", (100, 200)),
                ("pm", "deviation", (10, 20)),
            ):
                self.apply(ch, mode + "_source", "INT")
                self.apply(ch, mode + "_shape", "SIN")
                self.apply(ch, mode + "_frequency", 100)
                for value in values:
                    self.apply(ch, mode + "_" + field, value)
                    self.apply(ch, mode + "_enabled", True)
                    self.apply(ch, mode + "_enabled", False)
            self.group = "burst"
            self.apply(ch, "trigger_source", "EXT")
            for mode in ("TRIG", "GAT"):
                self.apply(ch, "burst_mode", mode)
                if mode == "TRIG":
                    for cycles in (1, 5):
                        self.apply(ch, "burst_cycles", cycles)
                self.apply(ch, "burst", True)
                self.apply(ch, "burst", False)
        for group in ("basic", "pulse_ramp", "sweep", "modulation", "burst"):
            self.report["stages"][group] = "passed"
        self.group = "arbitrary_upload"
        (self.folder / "waveforms").mkdir(exist_ok=True)
        token = datetime.now().strftime("%H%M%S")
        for ch in range(1, 5):
            name = f"Lc{token}C{ch}"
            samples = [
                round(0.5 * math.sin(2 * math.pi * (ch + 1) * i / 1024), 8)
                for i in range(256 if ch <= 2 else 1024)
            ]
            assert name + ".arb" not in self.worker(lambda a: a.file_catalog())
            data = json.dumps(samples).encode()
            (self.folder / "waveforms" / (name + ".json")).write_bytes(data)
            # Track even an uncertain first chunk, so cleanup can remove a partial new file.
            self.created.add(name)
            self.apply(ch, "upload_waveform", samples=samples, name=name)
            catalog_after = self.worker(lambda a: a.file_catalog())
            if name + ".arb" not in catalog_after:
                raise DeviceError("Uploaded file absent from catalog", "test_failed")
            self.report.setdefault("uploads", []).append(
                {
                    "channel": ch,
                    "name": name,
                    "points_sent": len(samples),
                    "sent_sha256": hashlib.sha256(data).hexdigest(),
                    "file_bytes": catalog_after[name + ".arb"],
                    "full_readback": "not_tested",
                }
            )
        self.report["stages"].update(
            arbitrary_upload="passed", arbitrary_readback="blocked", arbitrary_selection="blocked"
        )
        self.group = "range_rejection"
        for channel, command in (
            (1, DeviceCommand("frequency", {"value": 500e6 + 1})),
            (5, DeviceCommand("output", {"value": False}, 5, True)),
            (2, DeviceCommand("phase", {"value": 361}, 2)),
        ):

            def reject(a: Dg5504ProAdapter, command=command):
                assert isinstance(a.transport, OffTransport)
                count = a.transport.writes
                try:
                    a.validate_command(command)
                except DeviceError:
                    assert a.transport.writes == count
                    return
                raise AssertionError("Invalid input was accepted")

            self.worker(reject)
        self.report["stages"]["range_rejection"] = "passed"
        self.group = "repetition"
        for iteration in range(10):
            for ch in range(1, 5):
                self.apply(ch, "frequency", 1000 if iteration % 2 else 2000)
                self.apply(ch, "amplitude", 0.1 if iteration % 2 else 0.2)
        self.report["stages"]["repetition"] = "passed"

    def restore(self) -> None:
        self.group = "restoration"
        self.report["before_restore"] = self.snapshot()
        self.report["errors_before_restore"] = self.errors()
        self.off()
        actions: list[tuple[str, Callable[[], Any]]] = []

        def cleanup_file(name: str) -> None:
            self.worker(lambda a: a.delete_created_file(name, self.created))

        for name in sorted(self.created):
            actions.append(
                (
                    "delete temporary " + name,
                    partial(cleanup_file, name),
                )
            )
        for ch in range(1, 5):

            def restore_channel(ch=ch):
                initial = self.initial[str(ch)]
                self.leave_modes(ch)
                # Output-OFF recovery may restore a backed-up value beyond an experiment cap.
                # Each helper uses only recorded state, then the complete snapshot is compared.
                self.saved_write(ch, "VOLT:OFFS", 0)
                self.saved_write(ch, "VOLT", 0.1)
                self.saved_write(ch, "FREQ", 1000)
                for name, path in EXTRA.items():
                    self.saved_write(ch, path, initial[name])
                for name in (
                    "pulse_width",
                    "pulse_duty",
                    "ramp_symmetry",
                    "phase",
                    "sweep_start",
                    "sweep_stop",
                    "sweep_time",
                    "sweep_spacing",
                    "burst_mode",
                    "burst_cycles",
                    "am_source",
                    "am_shape",
                    "am_frequency",
                    "am_depth",
                    "fm_source",
                    "fm_shape",
                    "fm_frequency",
                    "fm_deviation",
                    "pm_source",
                    "pm_shape",
                    "pm_frequency",
                    "pm_deviation",
                ):
                    path = dg5504pro().commands[name].writes[0].split(" ")[0].split(":", 1)[1]
                    self.saved_write(ch, path, initial[name])
                self.saved_write(ch, "FUNC", initial["waveform"])
                self.saved_write(ch, "FREQ", initial["frequency"])
                self.saved_write(ch, "VOLT", initial["amplitude"])
                self.saved_write(ch, "VOLT:OFFS", initial["offset"])
                self.saved_write(ch, "VOLT:UNIT", initial["amplitude_unit"])
                self.apply(ch, "trigger_source", initial["trigger_source"])
                for mode in MODES:
                    if state(initial[mode]):
                        self.apply(ch, mode, True)

            actions.append((f"CH{ch} parameters and mode", restore_channel))
        for ch in range(1, 5):
            for field, path in COUPLING.items():
                actions.append(
                    (
                        f"CH{ch} {field}",
                        partial(self.saved_write, ch, path, self.initial[str(ch)][field]),
                    )
                )
        self.report["restore_steps"] = restore_steps(actions)
        final = self.snapshot()
        self.report["final_snapshot"] = final
        self.report["restoration_differences"] = differences(self.initial, final)
        self.report["final_outputs"] = self.outputs()
        okay = (
            not self.report["restoration_differences"]
            and not any(self.report["final_outputs"].values())
            and all(step["status"] == "passed" for step in self.report["restore_steps"])
        )
        self.report["stages"]["restoration"] = "passed" if okay else "failed"
        self.save()
        if not okay:
            raise DeviceError("Restoration incomplete; see final differences", "restoration_failed")

    def run(
        self, preflight_only: bool = False, restore_snapshot: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            self.app = ApplicationRuntime((self.config,), root=self.folder / "audit")
            app = self.app

            def factory(config):
                if (
                    config.resource != self.config.resource
                    or config.expected_serial != self.config.expected_serial
                ):
                    raise DeviceError("Only the selected asset is allowed", "identity_conflict")
                adapter = create_adapter(config, app.visa)
                assert isinstance(adapter, Dg5504ProAdapter)
                adapter.transport = OffTransport(adapter.transport, self.folder / "communication.jsonl")
                return adapter

            app.sessions.factory = factory
            app.discovery.providers = (ConfiguredDiscoveryProvider((self.config,)),)

            def probe(candidate):
                adapter = factory(candidate.config)
                try:
                    identity = adapter.identify()
                    if identity.serial != self.config.expected_serial:
                        raise DeviceError("Serial mismatch", "identity_conflict")
                    return identity, adapter.adapter_id
                finally:
                    adapter.close()

            app.discovery.probe = probe
            self.report["scan"] = app.discovery.scan().result(timeout=90)
            if any(row["error"] for row in self.report["scan"]["items"]):
                raise DeviceError("Target scan failed", "connection_failed")
            self.report["stages"]["scan"] = "passed"
            self.device_id = app.add(self.config)
            identity = app.connect(self.device_id).result(timeout=90)
            self.report["identity"] = jsonable(identity)
            self.report["environment"]["visa"] = str(app.visa.manager.visalib)
            self.report["stages"].update(identity="passed", connection="passed")
            self.report["initial_errors"] = self.errors()
            self.report["initial_outputs"] = self.outputs()
            if not preflight_only:
                for ch in range(1, 5):
                    outcome = app.sessions.submit(
                        self.device_id, "apply", DeviceCommand("output", {"value": False}, ch, True)
                    ).result(timeout=30)
                    if outcome.outcome != "success":
                        raise DeviceError("Could not verify output OFF", "output_state")
                self.off()
            self.initial = self.snapshot()
            self.report["initial_snapshot"] = self.initial
            self.report["snapshot_errors"] = self.errors()
            self.save()
            if len(self.report["snapshot_errors"]) != 1:
                raise DeviceError("Snapshot commands produced errors; no test writes", "backup_incomplete")
            if preflight_only:
                return self.report
            for ch in range(1, 5):
                initial = self.initial[str(ch)]
                if (
                    state(initial["advanced"])
                    or initial["waveform"] not in {"SIN", "SQU", "PULS", "RAMP"}
                    or any(state(value) for value in initial["other_modes"].values())
                ):
                    raise DeviceError(
                        "Original mode cannot be completely restored by this test", "backup_incomplete"
                    )
                if state(initial["sync_output"]) or state(initial["trigger_output"]):
                    raise DeviceError("Auxiliary output active; parameter writes blocked", "output_state")
            try:
                if restore_snapshot is None:
                    self.run_matrix()
                else:
                    self.report["observed_before_recovery"] = self.initial
                    self.initial = restore_snapshot
                    self.report["initial_snapshot"] = restore_snapshot
                    self.dirty = True
            except Exception as exc:
                self.report["test_error"] = repr(exc)
                self.report["stages"][self.group] = "failed"
                self.save()
                raise
            finally:
                if self.dirty:
                    self.restore()
            self.group = "stable_reads"
            for index in range(60):
                self.off()
                self.report.setdefault("stable_reads", []).append(
                    {
                        "index": index,
                        "status": jsonable(app.sessions.submit(self.device_id, "status").result(timeout=30)),
                    }
                )
                time.sleep(1)
                if (index + 1) % 10 == 0:
                    self.save()
            self.report["stages"]["stable_reads"] = "passed"
            for index in range(3):
                app.sessions.disconnect(self.device_id)
                actual = app.connect(self.device_id).result(timeout=90)
                if actual.key != identity.key or differences(self.initial, self.snapshot()):
                    raise DeviceError("Reconnect identity/settings differ", "test_failed")
                self.report.setdefault("reconnections", []).append(
                    {"index": index, "identity": jsonable(actual), "outputs": self.outputs()}
                )
            self.report["stages"]["reconnection"] = "passed"
        except Exception as exc:
            self.report["error"] = repr(exc)
            self.report["stages"][self.group] = (
                "blocked" if getattr(exc, "code", "") in {"process_busy", "backup_incomplete"} else "failed"
            )
            try:
                self.report["final_outputs"] = self.outputs()
            except Exception as final_error:
                self.report["final_outputs"] = "unknown: " + repr(final_error)
        finally:
            if self.app:
                self.app.close()
            self.save()
        return self.report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument(
        "--restore-from", type=Path, help="Original batch containing this asset's initial snapshot"
    )
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1 or configs[0].adapter != "rigol.dg5504pro" or not configs[0].expected_serial:
        parser.error("One DG5504 Pro with expected_serial is required")
    config = configs[0]
    original = None
    evidence = None
    if args.restore_from:
        source = args.restore_from / "results.json"
        raw = source.read_bytes()
        saved = json.loads(raw)
        if any(
            saved["config"].get(key) != getattr(config, key)
            for key in ("adapter", "resource", "expected_serial")
        ):
            parser.error("Recovery evidence must match the configured model, resource and serial")
        original = saved["initial_snapshot"]
        try:
            validate_recovery_snapshot(original)
        except DeviceError as exc:
            parser.error(str(exc))
        evidence = {"batch": str(args.restore_from), "results_sha256": hashlib.sha256(raw).hexdigest()}
    root = application_home() / "hardware-tests"
    asset = "DG5504Pro-" + re.sub(r"[^A-Za-z0-9_.-]", "_", config.expected_serial)
    folder = (
        root
        / asset
        / (
            datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
            + ("-preflight" if args.preflight_only else "-recovery" if args.restore_from else "-control")
        )
    )
    folder.mkdir(parents=True, exist_ok=False)
    verification = Verification(config, folder)
    if evidence:
        verification.report["recovery_source"] = evidence
    report = verification.run(args.preflight_only, original)
    from lab_control.storage.hardware_documents import refresh_after_batch

    refresh_after_batch(folder)
    print(
        json.dumps(
            {"folder": str(folder), "stages": report["stages"], "error": report.get("error")},
            ensure_ascii=False,
        )
    )
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
