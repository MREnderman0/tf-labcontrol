"""Explicit FPH02 LAN preflight/control/historical restoration; metadata-only traces."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.service import DiscoveryCandidate
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Quality,
    jsonable,
    utc_now,
)
from lab_control.instruments.registry import create_adapter
from lab_control.storage.recording import Recording
from lab_control.storage.repository import application_home

from ..adapter import FphAdapter
from .records import publish_report
from .state import (
    OTHER_MARKERS,
    SETTINGS,
    VerificationAdapter,
    differences,
    restore,
    validate_target,
    verification_profile,
)


def save_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(jsonable(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def physical_path(path: Path) -> str:
    if os.name != "nt":
        return str(path.resolve())
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    open_file.restype = wintypes.HANDLE
    handle = open_file(str(path), 0, 7, None, 3, 0x02000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        get_path = kernel.GetFinalPathNameByHandleW
        get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        if not get_path(handle, buffer, len(buffer), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value.removeprefix("\\\\?\\")
    finally:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)


def frame_record(batch) -> dict:
    if not batch.measurements:
        raise DeviceError("FPH returned no measurement", "parse_error")
    m = batch.measurements[0]
    if m.quality not in {Quality.GOOD, Quality.SIMULATED}:
        raise DeviceError(m.metadata.get("error", "Invalid FPH measurement"), m.quality.value)
    if m.axis is None or len(m.axis) != 711 or len(m.values) != 711:
        raise DeviceError("FPH returned incomplete measurement/axis", "parse_error")
    return {
        "timestamp": batch.timestamp,
        "validation": batch.validation,
        "performance": batch.performance,
        "settings_timestamps": batch.settings_timestamps,
        "unit": m.unit,
        "axis_unit": m.axis_unit,
        "axis_start": float(m.axis[0]),
        "axis_stop": float(m.axis[-1]),
        **m.metadata,
    }


class MetadataRecording(Recording):
    def _csv_loop(self):
        self.thread.join()
        self.finished.set_result([])
        self.wake()


def install_metadata_sinks(app, report):
    def forbidden_export(*args, **kwargs):
        raise AssertionError("Full trace export forbidden in hardware acceptance")

    def factory(mode, **options):
        recording = MetadataRecording(app.repository.root, mode, prepare_storage=False, **options)

        def append(sequence, batch):
            report.setdefault("recorded_metadata", []).append({"sequence": sequence, **frame_record(batch)})
            return "metadata-only"

        setattr(recording.segments, "append", append)
        return recording

    app.repository.export = forbidden_export
    app.repository.create_recording = factory


class AuditTransport:
    def __init__(self, inner, folder: Path):
        self.inner, self.resource, self.folder = inner, inner.resource, folder
        self.writes_allowed = False
        self.profile = verification_profile()

    def open(self):
        raise RuntimeError("Session owns opening")

    def close(self):
        self.inner.close()

    def log(self, **entry):
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": utc_now(), **entry}) + "\n")

    def write(self, payload):
        if not self.writes_allowed or not isinstance(payload, str):
            raise DeviceError("Setting writes are not authorized in preflight", "unsupported")
        patterns = [
            re.escape(t).replace(re.escape("{value}"), r"[A-Za-z0-9+.,e-]+")
            for s in self.profile.commands.values()
            for t in s.writes
        ]
        if not any(re.fullmatch(pattern, payload) for pattern in patterns):
            raise DeviceError("Command outside FPH control plan", "unsupported")
        self.log(tx=payload)
        self.inner.write(payload)

    def read(self):
        raise RuntimeError("Use transactional query")

    def query(self, payload):
        queries = {s.query for s in self.profile.commands.values() if s.query}
        if payload not in queries | {"*IDN?", "*OPC?", "TRAC:DATA? TRACE1"}:
            raise DeviceError("Query outside FPH preflight/control plan", "unsupported")
        start = time.monotonic()
        self.log(tx=payload)
        try:
            raw = self.inner.query(payload)
            if payload == "TRAC:DATA? TRACE1":
                self.log(
                    bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), seconds=time.monotonic() - start
                )
                if raw.startswith(b"#"):
                    from lab_control.transport.base import block_payload

                    body = block_payload(raw)
                    if len(body) == 711 * 4:
                        statistics = {}
                        for order in (">f4", "<f4"):
                            values = np.frombuffer(body, dtype=order)
                            valid = values[np.isfinite(values) & (np.abs(values) < 9e37)]
                            statistics[order] = {
                                "finite_available": len(valid),
                                "minimum": float(valid.min()) if len(valid) else None,
                                "maximum": float(valid.max()) if len(valid) else None,
                            }
                        self.log(binary_order_diagnostic=statistics)
            else:
                self.log(rx=raw.decode("ascii", "backslashreplace"), seconds=time.monotonic() - start)
            return raw
        except Exception as exc:
            self.log(error=str(exc), seconds=time.monotonic() - start)
            raise


class TestSession:
    __test__ = False

    def __init__(self, app, key, folder, report):
        self.app, self.key, self.folder, self.report = app, key, folder, report

    def read(self, op):
        reading = self.app.sessions.submit(self.key, "read_settings", ((op,), 1)).result(65)[0]
        if reading.source != "instrument":
            raise DeviceError(f"No reliable readback for {op}: {reading.error}", "backup_unreliable")
        return reading.value

    def snapshot(self):
        state: dict[str, Any] = {}
        for op in SETTINGS:
            if op == "marker_frequency" and (
                not state["marker_enabled"] or any(state[k] for k in OTHER_MARKERS)
            ):
                state[op] = None
            else:
                state[op] = self.read(op)
        return state

    def apply(self, op, value=None):
        cmd = DeviceCommand(op, {} if value is None else {"value": value}, confirmed=True)
        result = self.app.sessions.submit(self.key, "apply", cmd).result(65)
        self.report["operations"].append({"command": jsonable(cmd), "result": jsonable(result)})
        save_json(self.folder / "results.json", self.report)
        if result.outcome != "success":
            raise DeviceError(result.error, "control_failed")
        return result

    def stage(self, name):
        self.report["active_stage"] = name
        self.report["stages"].setdefault(name, "not_tested")
        save_json(self.folder / "results.json", self.report)

    def passed(self):
        self.report["stages"][self.report["active_stage"]] = "passed"

    def frames(self, name, consistency="live", count=20):
        self.stage(name)
        seconds = []
        for _ in range(count):
            start = time.monotonic()
            batch = self.app.sessions.submit(
                self.key, "acquire", AcquisitionRequest("spectrum", consistency=consistency, context_id=name)
            ).result(65)
            duration = time.monotonic() - start
            self.report["traces"].append({"group": name, "seconds": duration, **frame_record(batch)})
            seconds.append(duration)
        self.report.setdefault("performance_groups", {})[name] = {
            "frames": count,
            "median": float(np.median(seconds)),
            "p95": float(np.percentile(seconds, 95)),
            "maximum": max(seconds),
            "first_frame": seconds[0],
            "invalid_frames": 0,
        }
        self.passed()
        save_json(self.folder / "results.json", self.report)

    def control(self):
        self.stage("binary_encoding")
        self.apply("continuous", False)
        self.apply("sweep_count", 1)
        self.apply("single")
        frozen = []
        for fmt, order in (("ASC", "NORM"), ("REAL", "NORM"), ("REAL", "SWAP")):
            self.apply("transfer_format", fmt)
            self.apply("byte_order", order)
            batch = self.app.sessions.submit(self.key, "acquire", AcquisitionRequest("spectrum")).result(65)
            self.report["traces"].append({"group": "binary_encoding", **frame_record(batch)})
            frozen.append(batch.measurements[0].values)
        if any(not np.allclose(frozen[0], values, rtol=0, atol=0.0001) for values in frozen[1:]):
            raise DeviceError("FPH V1.40 binary order qualification failed", "readback_mismatch")
        self.passed()
        self.stage("frequency")
        self.apply("continuous", False)
        self.apply("sweep_count", 1)
        for center, span in ((100e6, 1e6), (110e6, 2e6)):
            self.apply("span", 0)
            self.apply("center_frequency", center)
            self.apply("span", span)
            if (
                self.read("start_frequency") != center - span / 2
                or self.read("stop_frequency") != center + span / 2
            ):
                raise DeviceError("Frequency endpoint readback differs", "readback_mismatch")
        self.apply("start_frequency", 99e6)
        self.apply("stop_frequency", 101e6)
        if self.read("center_frequency") != 100e6 or self.read("span") != 2e6:
            raise DeviceError("Center/span readback differs", "readback_mismatch")
        self.passed()
        self.stage("bandwidth")
        for op, values in (("rbw", (10000, 30000)), ("vbw", (3000, 10000))):
            self.apply(op + "_auto", False)
            for value in values:
                self.apply(op, value)
            self.apply(op + "_auto", True)
            self.read(op)
            self.apply(op + "_auto", False)
        self.passed()
        self.stage("level")
        self.apply("amplitude_unit", "DBM")
        self.apply("attenuation_auto", False)
        for att, level in ((20, -20), (30, -10)):
            self.apply("attenuation", att)
            self.apply("reference_level", level)
        self.apply("attenuation_auto", True)
        self.read("attenuation")
        self.apply("attenuation_auto", False)
        self.passed()
        self.stage("detector_marker")
        self.apply("detector_auto", False)
        for detector in ("POS", "SAMP"):
            self.apply("detector", detector)
        self.apply("detector_auto", True)
        self.apply("detector_auto", False)
        self.apply("marker_enabled", False)
        self.apply("marker_enabled", True)
        self.apply("marker_frequency", 100e6)
        self.apply("single")
        self.apply("marker_peak")
        if not self.read("start_frequency") <= self.read("marker_frequency") <= self.read("stop_frequency"):
            raise DeviceError("Peak marker outside axis", "readback_mismatch")
        self.passed()
        self.stage("sweep")
        self.apply("continuous", True)
        self.apply("continuous", False)
        self.apply("single")
        self.passed()
        for axis in ("frequency", "zero_span"):
            if axis == "zero_span":
                self.stage("zero_span_control")
                self.apply("span", 0)
                self.apply("sweep_time_auto", False)
                for duration in (0.1, 0.2):
                    self.apply("sweep_time", duration)
                    self.apply("single")
                # V1.40 rejects AutoSweep in zero span (SCPI -221).
                # Verify the supported manual state without sending that write.
                if self.read("sweep_time_auto"):
                    raise DeviceError(
                        "Zero span unexpectedly enabled automatic sweep time", "readback_mismatch"
                    )
                self.apply("sweep_time", 0.1)
                self.apply("single")
                self.passed()
            # Frozen single sweep: formats compare the exact same measurement.
            frozen = []
            for fmt in ("ASC", "REAL"):
                self.apply("transfer_format", fmt)
                batch = self.app.sessions.submit(self.key, "acquire", AcquisitionRequest("spectrum")).result(
                    65
                )
                frame_record(batch)
                frozen.append(batch.measurements[0].values.copy())
                self.frames(axis + "_" + fmt, count=20)
            self.stage(axis + "_format_equivalence")
            if not np.allclose(frozen[0], frozen[1], rtol=0, atol=0.0001):
                raise DeviceError(
                    "ASCII/REAL32 frozen trace differs beyond decimal/float32 precision", "readback_mismatch"
                )
            self.passed()
        self.stage("monitoring")
        self.apply("span", 2e6)
        self.apply("sweep_time_auto", True)
        self.apply("continuous", True)
        self.app.monitoring.start(self.key, (AcquisitionRequest("spectrum"),), cadence="fastest")
        start = time.monotonic()
        try:
            while time.monotonic() - start < 60:
                task = self.app.monitoring.tasks[self.key]
                if task.error or not task.running or task.recording:
                    raise DeviceError(task.error or "Unexpected monitoring state", "monitor_failed")
                time.sleep(0.2)
        finally:
            self.app.monitoring.stop(self.key).result(65)
        task = self.app.monitoring.tasks[self.key]
        self.report["monitoring"] = {
            "seconds": time.monotonic() - start,
            "frames": task.sequence,
            "recording": task.recording,
        }
        if task.sequence < 10:
            raise DeviceError("Insufficient monitoring frames", "monitor_failed")
        self.passed()


def verify(
    config: ConnectionConfig, folder: Path, *, control=False, restore_from: Path | None = None
) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "adapter_id": "rs.fph",
        "config": jsonable(config),
        "expected_serial": config.expected_serial,
        "resource": config.resource,
        "started_at": utc_now(),
        "operations": [],
        "traces": [],
        "scope": "FPH02 V1.40 common controls and metadata-only traces; RF input empty/50 ohm; no firmware/network changes",
        "active_stage": "connection",
        "final_state": "No setting writes; state not yet confirmed",
        "environment": {"python": platform.python_version()},
        "stages": dict.fromkeys(
            (
                "identity",
                "connection",
                "parameter_read",
                "existing_trace",
                "frequency",
                "bandwidth",
                "level",
                "sweep",
                "detector_marker",
                "monitoring",
                "restoration",
                "reconnection",
                "physical_output",
            ),
            "not_tested",
        ),
        "source_sha256": {
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in p.parts
        },
    }
    app = test = original = None
    mutated = False
    data_before: set[str] = set()
    try:
        if (
            config.adapter != "rs.fph"
            or config.transport != "visa"
            or not config.expected_serial
            or not config.resource.startswith("TCPIP")
        ):
            raise DeviceError("One explicit LAN VISA FPH02 asset/serial is required", "identity_required")
        home = application_home()
        paths = {
            "home": home,
            "inventory": home / "inventory.sqlite3",
            "lock": Path(os.environ["LOCALAPPDATA"]) / "TF-LabControl/hardware.lock",
        }
        report["physical_paths"] = {k: physical_path(p) for k, p in paths.items()}
        if any(Path(report["physical_paths"][k]) != p for k, p in paths.items()) or not folder.is_relative_to(
            home / "hardware-tests"
        ):
            raise DeviceError("Formal runtime paths are redirected", "path_redirected")
        app = ApplicationRuntime((config,))
        install_metadata_sinks(app, report)
        data_before = {str(p) for p in (home / "data").rglob("*") if p.is_file()}
        report["enumerated_resources"] = app.visa.discover_lan()
        report["environment"]["visa"] = str(app.visa.manager.visalib)

        def factory(selected):
            if selected != config:
                raise DeviceError("Unexpected asset requested", "identity_conflict")
            adapter = create_adapter(selected, app.visa)
            assert isinstance(adapter, FphAdapter)
            adapter = VerificationAdapter(adapter.transport, verification_profile(), selected)
            adapter.profile = verification_profile()
            adapter.transport = AuditTransport(adapter.transport, folder)
            return adapter

        app.sessions.factory = factory
        # Resolve DHCP address changes before opening a long-lived worker. A manual
        # candidate followed by "connected" would create a second conflicting asset.
        owner = "fph-acceptance-identity"
        app.leases.acquire(owner, (config.resource,), identified=False)
        probe = None
        try:
            probe = factory(config)
            identity = probe.identify()
            if identity.serial != config.expected_serial:
                raise DeviceError("Selected FPH serial does not match", "identity_conflict")
            record = app.inventory.merge(
                DiscoveryCandidate(config, "lan", safe_scpi=True),
                folder.name,
                identity,
                config.adapter,
                reason="identified",
            )
            key = record["id"]
        finally:
            if probe:
                probe.close()
            app.leases.release(owner)
        identity = app.sessions.connect(key, config, configure_acquisition=False).result(65)
        report.update(identity=jsonable(identity), inventory_id=key)
        if identity.firmware != "V1.40":
            raise DeviceError("This hardware plan requires the selected V1.40 firmware", "identity_conflict")
        report["stages"].update(identity="passed", connection="passed")
        test = TestSession(app, key, folder, report)
        test.stage("parameter_read")
        report["preexisting_errors"] = []
        for _ in range(16):
            error = test.read("errors")
            report["preexisting_errors"].append(error)
            if re.match(r"^[+]?0(?:\s|,|$)", error):
                break
        else:
            raise DeviceError("Error queue not clean", "backup_unreliable")
        original = test.snapshot()
        report["initial_snapshot"] = original
        save_json(folder / "initial_snapshot.json", original)
        report["initial_snapshot_sha256"] = hashlib.sha256(
            (folder / "initial_snapshot.json").read_bytes()
        ).hexdigest()
        test.passed()
        test.frames("existing_trace", "full", 1)
        if restore_from:
            history = json.loads((restore_from / "results.json").read_text(encoding="utf-8"))
            contents = (restore_from / "initial_snapshot.json").read_bytes()
            if (
                history["identity"] != report["identity"]
                or hashlib.sha256(contents).hexdigest() != history["initial_snapshot_sha256"]
            ):
                raise DeviceError("Historical snapshot identity/hash mismatch", "backup_unreliable")
            original = json.loads(contents)
            report["restore_from"] = str(restore_from)
        if control or restore_from:
            adapter = app.sessions.sessions[key].adapter
            assert isinstance(adapter, FphAdapter)
            validate_target(original, adapter)
            if not restore_from and differences(test.snapshot(), original):
                raise DeviceError("Backup did not remain stable", "backup_unreliable")
            assert isinstance(adapter.transport, AuditTransport)
            adapter.transport.writes_allowed = True
            mutated = True
            report["final_state"] = "Control in progress; restoration pending"
            if control:
                test.control()
        else:
            report["final_snapshot"] = test.snapshot()
            diff = differences(report["final_snapshot"], original)
            save_json(folder / "restoration-differences.json", diff)
            if diff:
                raise DeviceError("Read-only snapshot changed", "configuration_changed")
            report["final_state"] = "Read-only parameters/trace verified; no setting or scan writes"
    except Exception as exc:
        report["exception_traceback"] = traceback.format_exc()
        report.update(error=str(exc), error_code=getattr(exc, "code", type(exc).__name__))
        report["stages"][report["active_stage"]] = (
            "blocked"
            if report["error_code"]
            in {"process_busy", "resource_busy", "path_redirected", "backup_unreliable", "unsupported"}
            else "failed"
        )
    finally:
        if mutated and test and original and app:
            try:
                test.stage("restoration")
                if app.sessions.sessions[test.key].state == "degraded":
                    # One new, identity-checked recovery connection. The failed
                    # command is never replayed, and recovery is not retried.
                    app.sessions.disconnect(test.key)
                    recovery_identity = app.sessions.connect(
                        test.key, config, configure_acquisition=False
                    ).result(65)
                    if jsonable(recovery_identity) != report["identity"]:
                        raise DeviceError("Recovery identity changed", "identity_conflict")
                    report["recovery_connection"] = jsonable(recovery_identity)
                    recovery_adapter = app.sessions.sessions[test.key].adapter
                    assert isinstance(recovery_adapter, FphAdapter) and isinstance(
                        recovery_adapter.transport, AuditTransport
                    )
                    recovery_adapter.transport.writes_allowed = True
                report["before_restore"] = test.snapshot()
                restore(test, original)
                report["final_snapshot"] = test.snapshot()
                diff = differences(report["final_snapshot"], original)
                save_json(folder / "restoration-differences.json", diff)
                if diff:
                    raise DeviceError("FPH restoration differs from backup", "restoration_failed")
                test.passed()
                test.stage("reconnection")
                for _ in range(3):
                    app.sessions.disconnect(test.key)
                    identity = app.sessions.connect(test.key, config, configure_acquisition=False).result(65)
                    state = test.snapshot()
                    if differences(state, original):
                        raise DeviceError("Reconnected FPH state differs", "readback_mismatch")
                    report.setdefault("reconnections", []).append(
                        {"identity": jsonable(identity), "snapshot": state}
                    )
                test.passed()
                report["final_state"] = (
                    "Settings restored and checked across three reconnects; firmware remains V1.40; historical curves not restored"
                )
            except Exception as exc:
                report["restore_error"] = str(exc)
                report["stages"][report["active_stage"]] = "failed"
                report["final_state"] = (
                    "Recovery stopped at first failure; inspect last known snapshot and communication timestamps"
                )
        if app:
            try:
                app.close()
                after = {str(p) for p in (app.repository.root / "data").rglob("*") if p.is_file()}
                report["new_curve_files"] = sorted(after - data_before)
                report["stages"]["metadata_only"] = "failed" if after - data_before else "passed"
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        save_json(folder / "results.json", report)
        for name in ("initial_snapshot.json", "restoration-differences.json"):
            if not (folder / name).exists():
                save_json(
                    folder / name, {"status": "unavailable", "reason": report.get("error", "Not reached")}
                )
        publish_report(folder)
        (folder / "record.md").write_text(
            "# FPH02 V1.40 LAN 验收\n\n"
            + report["scope"]
            + "\n\n| 分组 | 结果 |\n| --- | --- |\n"
            + "\n".join(f"| {k} | {v} |" for k, v in report["stages"].items())
            + "\n\n最终状态："
            + report["final_state"]
            + "\n\n异常："
            + report.get("error", "无")
            + "\n\n恢复异常："
            + report.get("restore_error", "无")
            + "\n\n[结果](results.json) · [摘要](summary.json) · [备份](initial_snapshot.json) · [恢复差异](restoration-differences.json)\n",
            encoding="utf-8",
        )
        from lab_control.storage.hardware_documents import refresh_after_batch

        refresh_after_batch(folder)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--control", action="store_true")
    group.add_argument("--restore-from", type=Path)
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1:
        parser.error("Exactly one explicit asset required")
    config = configs[0]
    asset = "FPH-" + re.sub(r"[^A-Za-z0-9_.-]", "_", config.expected_serial)
    folder = (
        application_home()
        / "hardware-tests"
        / asset
        / (
            datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-v140-"
            + ("control" if args.control else "restore" if args.restore_from else "preflight")
        )
    )
    report = verify(config, folder, control=args.control, restore_from=args.restore_from)
    print(
        json.dumps(
            {
                "folder": str(folder),
                "stages": report["stages"],
                "error": report.get("error", ""),
                "restore_error": report.get("restore_error", ""),
            },
            ensure_ascii=False,
        )
    )
    return int(bool(report.get("error") or report.get("restore_error")))


if __name__ == "__main__":
    raise SystemExit(main())
