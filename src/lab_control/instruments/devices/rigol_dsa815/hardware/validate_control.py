"""Single selected LAN asset: preflight, explicit control, or historical settings restoration.

Trace samples never enter reports or communication logs. No reset, file storage,
trace upload, calibration, firmware command, or transport retry exists here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home
from lab_control.transport.base import MockTransport, Transport
from lab_control.transport.scpi import scalar_query_message

from ..adapter import FRAME_SETTINGS, LIVE_SETTINGS, Dsa815Adapter, agrees, tolerance
from .records import publish_report
from .state import OTHER_MARKERS, verification_profile

SETTINGS = tuple(
    dict.fromkeys(
        (
            *FRAME_SETTINGS,
            "options",
            "continuous",
            "sweep_count",
            "operation_condition",
            "preamplifier",
            "mixer_level",
            "marker_enabled",
            "marker_mode",
            "marker_position",
            "marker_frequency",
            "marker_readout",
            "marker_trace",
            "marker_trace_auto",
            "marker_function",
            "marker_tracking",
            "marker_search",
        )
    )
)
SETTINGS = (*SETTINGS, *OTHER_MARKERS)
VOLATILE = {"operation_condition", "marker_frequency", *(f"marker{n}_frequency" for n in (2, 3, 4))}
AUTOMATIC = {
    "rbw": "rbw_auto",
    "vbw": "vbw_auto",
    "attenuation": "attenuation_auto",
    "sweep_time": "sweep_time_auto",
}


def save_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(jsonable(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


class RecordingTransport:
    def __init__(self, inner: Transport, log: Path, control: bool):
        self.inner, self.resource, self.log, self.control = inner, inner.resource, log, control
        self.last = ""
        self.profile = verification_profile()

    def record(self, **entry) -> None:
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": utc_now(), **entry}, ensure_ascii=False) + "\n")

    def open(self) -> None:
        raise RuntimeError("Session owns transport opening")

    def close(self) -> None:
        self.inner.close()

    def write(self, payload: bytes | str) -> None:
        if not isinstance(payload, str):
            raise DeviceError("Binary writes are not authorized", "unsupported")
        queries = {s.query for s in self.profile.commands.values() if s.query}
        allowed = payload in queries | {"*IDN?", "*OPC?", "SYST:ERR?", "TRAC:DATA? TRACE1"}
        if not allowed:
            allowed = payload in {
                scalar_query_message(
                    tuple(self.profile.commands[op].query for op in fields if op != "sweep_points")
                )
                for fields in (FRAME_SETTINGS, LIVE_SETTINGS)
            }
        if self.control and not allowed:
            for spec in self.profile.commands.values():
                for template in spec.writes:
                    pattern = re.escape(template).replace(re.escape("{value}"), r"[A-Z0-9+.,e-]+")
                    if re.fullmatch(pattern, payload):
                        allowed = True
        if not allowed:
            raise DeviceError("Command outside the approved DSA815 plan", "unsupported")
        self.last = payload
        self.record(tx=payload)
        self.inner.write(payload)

    def read(self) -> bytes:
        try:
            raw = self.inner.read()
            if self.last == "TRAC:DATA? TRACE1":
                self.record(trace_reply={"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
            else:
                self.record(rx=raw.decode("ascii", "backslashreplace"), hex=raw.hex())
            return raw
        except Exception as exc:
            self.record(error=str(exc), operation=self.last)
            raise

    def query(self, payload: bytes | str) -> bytes:
        self.write(payload)
        return self.read()


def differences(actual: dict, expected: dict) -> dict:
    return {
        op: {"expected": value, "actual": actual.get(op), "tolerance": tolerance(op, value)}
        for op, value in expected.items()
        if op not in VOLATILE and not agrees(op, value, actual.get(op))
    }


def validate_target(target: dict, config: ConnectionConfig) -> None:
    if set(target) != set(SETTINGS):
        raise DeviceError("Incomplete backup", "backup_unreliable")
    adapter = Dsa815Adapter(MockTransport([]), verification_profile(), config)
    for op, value in target.items():
        if value is None and op.startswith("marker") and not op.endswith("enabled"):
            prefix = op.split("_")[0] + "_"
            if target[prefix + "enabled"] is False:
                continue
        if adapter.profile.commands[op].capability.writable:
            adapter.validate_command(DeviceCommand(op, {"value": value}, confirmed=True))
    if target["measurement_mode"] != "OFF" or target["frequency_offset"] or target["reference_offset"]:
        raise DeviceError(
            "Optional measurements or display offsets prevent qualified restoration", "backup_unreliable"
        )
    if target["amplitude_unit"] != "DBM" or target["preamplifier"]:
        raise DeviceError("This control matrix requires dBm and preamplifier OFF", "backup_unreliable")
    if target["marker_enabled"] and (
        target["marker_mode"] != "POS" or target["marker_tracking"] or target["marker_function"] != "OFF"
    ):
        raise DeviceError("Only static normal markers have a qualified recovery path", "backup_unreliable")
    if target["marker_enabled"] and target["marker_readout"] != ("FREQ" if target["span"] else "TIME"):
        raise DeviceError("Marker readout cannot be reliably restored", "backup_unreliable")
    for prefix in ("marker2_", "marker3_", "marker4_"):
        if not target[prefix + "enabled"]:
            continue
        if (
            target[prefix + "mode"] != "POS"
            or target[prefix + "tracking"]
            or target[prefix + "function"] != "OFF"
        ):
            raise DeviceError("All marker states require a reliable normal-mode backup", "backup_unreliable")
        if target[prefix + "readout"] != ("FREQ" if target["span"] else "TIME"):
            raise DeviceError("Other marker readout cannot be reliably restored", "backup_unreliable")
    if target["sweep_count"] != 1 or target["trigger_source"] != "IMM":
        raise DeviceError("Require immediate trigger and one sweep for bounded recovery", "backup_unreliable")
    if not target["continuous"] and target["operation_condition"] & 24:
        raise DeviceError("An active single sweep cannot be reconstructed", "backup_unreliable")


class TestSession:
    __test__ = False

    def __init__(self, app: ApplicationRuntime, key: str, report: dict, folder: Path):
        self.app, self.key, self.report, self.folder = app, key, report, folder

    def save(self) -> None:
        save_json(self.folder / "results.json", self.report)

    def read(self, op: str) -> Any:
        return self.app.sessions.submit(self.key, "read_settings", ((op,), 1)).result(65)[0].value

    def snapshot(self) -> dict:
        state: dict[str, Any] = {}
        for op in SETTINGS:
            prefix = op.split("_")[0] + "_"
            inactive = (
                op.startswith("marker")
                and not op.endswith("enabled")
                and state.get(prefix + "enabled") is False
            )
            state[op] = None if inactive else self.read(op)
            self.report["last_known_state"] = dict(state)
            self.report["last_known_state_at"] = utc_now()
        return state

    def apply(self, op: str, value: Any = None) -> None:
        cmd = DeviceCommand(op, {} if value is None else {"value": value}, confirmed=True)
        entry = {"time": utc_now(), "request": jsonable(cmd)}
        self.report["operations"].append(entry)
        try:
            result = self.app.sessions.submit(self.key, "apply", cmd).result(65)
            entry["result"] = jsonable(result)
            if result.outcome != "success":
                raise DeviceError(result.error, "readback_mismatch")
        finally:
            self.save()

    def acquire(self) -> dict:
        batch = self.app.sessions.submit(self.key, "acquire", AcquisitionRequest("spectrum")).result(65)
        m = batch.measurements[0]
        if not m.metadata.get("sha256"):
            raise DeviceError("Trace acquisition failed", "acquisition_failed")
        entry = {
            "time": batch.timestamp,
            "unit": m.unit,
            "axis_unit": m.axis_unit,
            "axis_start": float(m.axis[0]),
            "axis_stop": float(m.axis[-1]),
            **m.metadata,
        }
        self.report["traces"].append(entry)
        self.save()
        return entry

    def restore(self, original: dict) -> None:
        if self.app.sessions.sessions[self.key].state == "degraded":
            self.report["recovery_connection"] = "one identity-checked connection"
            self.app.sessions.disconnect(self.key)
            self.app.connect(self.key, configure_acquisition=False).result(65)
        self.report["before_restore"] = self.snapshot()
        self.save()
        self.apply("continuous", False)
        self.apply("trigger_source", "IMM")
        self.apply("amplitude_unit", original["amplitude_unit"])
        self.apply("span", 0)
        self.apply("center_frequency", original["center_frequency"])
        self.apply("span", original["span"])
        # Reduce reference level before restoring the attenuator to keep the mixer within limits.
        self.apply("reference_level", min(-20, original["reference_level"]))
        for op, auto in AUTOMATIC.items():
            if original[auto]:
                self.apply(auto, True)
            else:
                self.apply(auto, False)
                self.apply(op, original[op])
        self.apply("reference_level", original["reference_level"])
        for op in ("detector", "trace_mode"):
            self.apply(op, original[op])
        for prefix in ("marker_", "marker2_", "marker3_", "marker4_"):
            if not original[prefix + "enabled"]:
                self.apply(prefix + "enabled", False)
                continue
            self.apply(prefix + "enabled", True)
            self.apply(prefix + "mode", original[prefix + "mode"])
            self.apply(prefix + "readout", original[prefix + "readout"])
            self.apply(prefix + "position", original[prefix + "position"])
            self.apply(prefix + "enabled", original[prefix + "enabled"])
        self.apply("trigger_source", original["trigger_source"])
        self.apply("continuous", original["continuous"])
        final = self.snapshot()
        self.report["final_snapshot"] = final
        diff = differences(final, original)
        self.report["restoration_differences"] = diff
        save_json(self.folder / "restoration-differences.json", diff)
        self.save()
        if diff:
            raise DeviceError("Restoration readback differences remain", "restore_failed")

    def stage(self, name: str) -> None:
        self.report["active_stage"] = name
        self.save()

    def passed(self, name: str) -> None:
        self.report["stages"][name] = "passed"
        self.save()

    def control(self) -> None:
        self.stage("frequency")
        self.apply("continuous", False)
        self.apply("span", 0)
        self.apply("center_frequency", 100e6)
        for center, span in ((100e6, 1e6), (110e6, 2e6)):
            self.apply("center_frequency", center)
            self.apply("span", span)
            if (
                self.read("start_frequency") != center - span / 2
                or self.read("stop_frequency") != center + span / 2
            ):
                raise DeviceError("Frequency endpoint association differs", "readback_mismatch")
        self.apply("start_frequency", 99e6)
        self.apply("stop_frequency", 101e6)
        if self.read("center_frequency") != 100e6 or self.read("span") != 2e6:
            raise DeviceError("Center/span association differs", "readback_mismatch")
        self.passed("frequency")
        self.stage("bandwidth")
        for op, values in (("rbw", (10000, 30000)), ("vbw", (3000, 10000))):
            self.apply(op + "_auto", False)
            for value in values:
                self.apply(op, value)
            self.apply(op + "_auto", True)
            self.read(op)
            self.apply(op + "_auto", False)
            self.apply(op, values[-1])
        self.passed("bandwidth")
        self.stage("level")
        self.apply("reference_level", -20)
        self.apply("attenuation_auto", False)
        for level, atten in ((-20, 20), (-10, 30)):
            self.apply("attenuation", atten)
            self.apply("reference_level", level)
        self.apply("attenuation_auto", True)
        self.read("attenuation")
        self.passed("level")
        self.stage("sweep")
        self.apply("single")
        self.apply("continuous", True)
        time.sleep(0.2)
        self.apply("continuous", False)
        self.passed("sweep")
        self.stage("detector_marker")
        self.apply("detector", "NORM")
        self.apply("detector", "SAMP")
        self.apply("marker_enabled", True)
        self.apply("marker_frequency", 100e6)
        self.apply("marker_frequency", 100000100)
        self.apply("marker_peak")
        marker = self.read("marker_frequency")
        if not self.read("start_frequency") <= marker <= self.read("stop_frequency"):
            raise DeviceError("Peak marker is outside the current axis", "readback_mismatch")
        self.apply("marker_enabled", False)
        self.passed("detector_marker")
        self.stage("trace")
        for mode in ("WRIT", "MAXH", "MINH", "VIEW", "BLAN", "VID", "POW"):
            self.apply("trace_mode", mode)
        self.apply("trace_mode", "WRIT")
        self.apply("single")
        for _ in range(10):
            self.acquire()
        self.apply("span", 0)
        self.apply("sweep_time_auto", False)
        for duration in (0.1, 0.2):
            self.apply("sweep_time", duration)
        self.apply("sweep_time_auto", True)
        self.read("sweep_time")
        self.apply("sweep_time_auto", False)
        self.apply("sweep_time", 0.1)
        self.apply("single")
        for _ in range(10):
            self.acquire()
        self.passed("trace")
        self.stage("monitoring")
        data = self.app.repository.root / "data"
        before = {str(p) for p in data.rglob("*") if p.is_file()}
        self.app.monitoring.start(self.key, (AcquisitionRequest("spectrum"),), interval=1)
        start = time.monotonic()
        try:
            while time.monotonic() - start < 60:
                task = self.app.monitoring.tasks[self.key]
                if task.error or not task.running:
                    raise DeviceError(task.error or "Monitoring stopped early", "monitor_failed")
                time.sleep(0.2)
        finally:
            self.app.monitoring.stop(self.key).result(65)
        after = {str(p) for p in data.rglob("*") if p.is_file()}
        task = self.app.monitoring.tasks[self.key]
        self.report["monitoring"] = {
            "seconds": time.monotonic() - start,
            "frames": task.sequence,
            "new_data_files": sorted(after - before),
            "recording": task.recording,
        }
        if after != before or task.recording or task.sequence < 10:
            raise DeviceError("Pure monitoring did not meet recording/frame expectations", "monitor_failed")
        self.passed("monitoring")


def verify(
    config: ConnectionConfig, folder: Path, *, control: bool = False, restore_from: Path | None = None
) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "adapter_id": config.adapter,
        "config": asdict(config),
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "started_at": utc_now(),
        "operations": [],
        "traces": [],
        "scope": "LAN common controls; RF input empty/50 ohm; no physical accuracy tests; traces saved by human only",
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
                "trace",
                "monitoring",
                "restoration",
                "reconnection",
                "physical_output",
            ),
            "not_tested",
        ),
        "final_state": "No settings were written; current state not yet confirmed",
        "active_stage": "connection",
        "environment": {"python": platform.python_version()},
        "source_sha256": {
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in p.parts
        },
    }
    app = test = original = None
    mutated = False
    try:
        if (
            config.adapter != "rigol.dsa815"
            or config.transport != "visa"
            or not config.expected_serial
            or not config.resource.startswith("TCPIP")
        ):
            raise DeviceError(
                "An exact DSA815 serial and LAN VISA resource are required", "identity_required"
            )
        home = application_home()
        paths = {
            "home": home,
            "inventory": home / "inventory.sqlite3",
            "lock": Path(os.environ["LOCALAPPDATA"]) / "TF-LabControl/hardware.lock",
        }
        report["environment"].update({key: str(value.resolve()) for key, value in paths.items()})
        if any(p.resolve() != p for p in (*paths.values(), folder)) or not folder.is_relative_to(
            home / "hardware-tests"
        ):
            raise DeviceError("Native formal runtime paths required", "path_redirected")
        app = ApplicationRuntime((config,))
        # R&S VISA resolves discovered VXI-11 resources in its resource-manager context.
        # Enumeration opens no instrument session and performs no SCPI probes.
        report["active_stage"] = "enumeration"
        report["enumerated_resources"] = app.visa.discover_lan()
        report["stages"]["enumeration"] = "passed"
        report["environment"]["visa"] = str(app.visa.manager.visalib)
        report["active_stage"] = "connection"

        def factory(selected):
            if selected != config:
                raise DeviceError("Different asset requested", "identity_conflict")
            adapter = create_adapter(selected, app.visa)
            assert isinstance(adapter, Dsa815Adapter)
            adapter.profile = verification_profile()
            adapter.transport = RecordingTransport(
                adapter.transport, folder / "communication.jsonl", control or bool(restore_from)
            )
            return adapter

        app.sessions.factory = factory
        key = app.add(config)
        identity = app.connect(key, configure_acquisition=False).result(65)
        report.update(identity=jsonable(identity), inventory_id=key)
        report["stages"].update(identity="passed", connection="passed")
        test = TestSession(app, key, report, folder)
        test.stage("parameter_read")
        # Read and retain queued diagnostics, never clear errors with *CLS or hide them.
        report["preexisting_errors"] = []
        for _ in range(16):
            error = test.read("errors")
            report["preexisting_errors"].append(error)
            if re.match(r"^[+]?0(?:\s|,|$)", error):
                break
        else:
            raise DeviceError("Error queue did not reach a clean baseline", "backup_unreliable")
        original = test.snapshot()
        report["initial_snapshot"] = original
        save_json(folder / "initial_snapshot.json", original)
        report["initial_snapshot_sha256"] = hashlib.sha256(
            (folder / "initial_snapshot.json").read_bytes()
        ).hexdigest()
        test.passed("parameter_read")
        test.stage("existing_trace")
        test.acquire()
        test.passed("existing_trace")
        if restore_from:
            history = json.loads((restore_from / "results.json").read_text(encoding="utf-8"))
            target_bytes = (restore_from / "initial_snapshot.json").read_bytes()
            if (
                history["identity"] != report["identity"]
                or hashlib.sha256(target_bytes).hexdigest() != history["initial_snapshot_sha256"]
            ):
                raise DeviceError("Historical backup identity/hash mismatch", "backup_unreliable")
            original = json.loads(target_bytes)
            report["restore_from"] = str(restore_from)
        if control or restore_from:
            validate_target(original, config)
            if not restore_from and differences(test.snapshot(), original):
                raise DeviceError("Backup changed during preflight", "backup_unreliable")
            report["restoration_target"] = original
            report["final_state"] = "Authorized control in progress; restoration pending"
            mutated = True
            if control:
                test.control()
        else:
            report["final_snapshot"] = test.snapshot()
            report["final_state"] = (
                "Read-only preflight complete; no setting or format writes; no trace samples saved"
            )
    except Exception as exc:
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
                test.restore(original)
                test.passed("restoration")
                for _ in range(3):
                    app.sessions.disconnect(test.key)
                    identity = app.connect(test.key, configure_acquisition=False).result(65)
                    final = test.snapshot()
                    if differences(final, original):
                        raise DeviceError("Reconnected state differs from backup", "readback_mismatch")
                    report.setdefault("reconnections", []).append(
                        {"identity": jsonable(identity), "snapshot": final}
                    )
                test.passed("reconnection")
                report["final_state"] = (
                    "Settings restored and verified across three reconnects; historical curves were not restored"
                )
            except Exception as exc:
                report["restore_error"] = str(exc)
                stage = "restoration" if report["stages"]["restoration"] != "passed" else "reconnection"
                report["stages"][stage] = "failed"
                report["final_state"] = "Recovery stopped; consult timestamped last known state: " + str(exc)
        if app:
            try:
                app.close()
                report["handles_released"] = True
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        save_json(folder / "results.json", report)
        if not (folder / "initial_snapshot.json").exists():
            save_json(folder / "initial_snapshot.json", {"unavailable": report.get("error", "")})
        if not (folder / "restoration-differences.json").exists():
            save_json(folder / "restoration-differences.json", {"status": report["stages"]["restoration"]})
        publish_report(folder)
        rows = "\n".join(f"| {key} | {value} |" for key, value in report["stages"].items())
        (folder / "record.md").write_text(
            "# DSA815 LAN 验收\n\n"
            + report["scope"]
            + "\n\n| 分组 | 结果 |\n| --- | --- |\n"
            + rows
            + "\n\n最终状态："
            + report["final_state"]
            + "\n\n异常："
            + report.get("error", "无")
            + "\n\n轨迹仅保留长度、点数、单位、轴信息和哈希；未自动保存曲线。物理精度未验收。\n\n"
            + "[结果](results.json) · [摘要](summary.json) · [通信诊断](communication.jsonl)\n",
            encoding="utf-8",
        )
        from lab_control.storage.hardware_documents import refresh_after_batch

        refresh_after_batch(folder)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--control", action="store_true")
    mode.add_argument("--restore-from", type=Path)
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1:
        parser.error("Exactly one selected asset is required")
    config = configs[0]
    asset = "DSA815-" + re.sub(r"[^A-Za-z0-9_.-]", "-", config.expected_serial)
    folder = (
        application_home()
        / "hardware-tests"
        / asset
        / (
            datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
            + "-lan-"
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
