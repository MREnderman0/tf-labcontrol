"""Explicit, reversible JW8105 parameter write/readback/restore validation."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import physical_path, verify_runtime_paths
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_records import atomic_json, digest
from lab_control.storage.repository import application_home

from ..profile import OFFICIAL_DOWNLOAD_URL, OFFICIAL_PRODUCT_URL, PROTOCOL_EVIDENCE
from .preflight import enumerate_selected
from .readonly import TraceTransport, _config, _safe_asset_name
from .records import publish_report

PORT_PATTERN = re.compile(r"COM[1-9][0-9]*", re.IGNORECASE)
CHANNELS = tuple(range(1, 9))


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        try:
            return abs(float(left) - float(right)) <= 1e-6
        except (TypeError, ValueError):
            return False
    return left == right


def _value(readings: Any) -> Any:
    if not isinstance(readings, list) or len(readings) != 1:
        raise DeviceError("JW8105 readback did not contain one value", "parse_error")
    return readings[0]["value"]


def _path_evidence(root: Path, lock_root: Path, folder: Path) -> dict[str, Any]:
    paths = {
        "runtime": root,
        "inventory": root / "inventory.sqlite3",
        "global_lock": lock_root / "hardware.lock",
        "device_locks": lock_root / "devices",
        "batch": folder,
    }
    evidence: dict[str, Any] = {}
    for name, path in paths.items():
        try:
            evidence[name] = {"requested": str(path), "physical": str(physical_path(path))}
        except OSError as exc:
            evidence[name] = {"requested": str(path), "error": str(exc)}
    return evidence


def run(resource: str, asset_name: str) -> Path:
    resource = resource.upper()
    if not PORT_PATTERN.fullmatch(resource):
        raise ValueError("Specify one explicit COM port")
    asset_name = _safe_asset_name(asset_name)
    root = application_home()
    lock_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl"
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    folder = root / "hardware-tests" / asset_name / f"{stamp}-control-{uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    transcript = folder / "communication.jsonl"
    transcript.touch(exist_ok=False)
    counters = {"ports_opened": 0, "ports_closed": 0, "commands_sent": 0}
    config = _config(resource)
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "adapter_id": "joinwit.jw8105",
        "model": "JW8105",
        "config": jsonable(config),
        "resource": resource,
        "expected_serial": "",
        "identity": {"manufacturer": "", "model": "", "serial": "", "firmware": "", "confidence": "unknown"},
        "scope": (
            "Explicit reversible control validation on one selected JW8105: backup, one-setting-at-a-time write, "
            "readback, restore, and final snapshot. No reset, acquisition start, or physical output command."
        ),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "runtime_home": str(root),
            "process_id": os.getpid(),
            "executable": sys.executable,
        },
        "source_sha256": {
            path.name: digest(path)
            for path in (
                Path(__file__),
                Path(__file__).parents[1] / "adapter.py",
                Path(__file__).parents[1] / "protocol.py",
                Path(__file__).parents[1] / "transport.py",
            )
        },
        "stages": {
            key: {"status": "not_tested", "reason": "Not reached"}
            for key in (
                "enumeration",
                "runtime_paths",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "parameter_write",
                "restoration",
                "reconnection",
                "physical_output",
            )
        },
        "notes": [
            PROTOCOL_EVIDENCE,
            "Serial number and firmware remain unavailable from the supplied protocol; USB bridge identity is not instrument identity.",
            "Stop bit is absent from the supplied protocol; the existing provisional 115200 8N1 transport was used.",
            "Each write was followed by a readback and one restoration write; no write was retried blindly.",
        ],
        "final_state": "No instrument I/O attempted yet",
        "final_outputs": {"settings_modified": False, **counters},
        "operations": [],
    }

    def save() -> None:
        report["final_outputs"] = {"settings_modified": bool(report.get("writes_completed")), **counters}
        atomic_json(folder / "results.json", report)

    save()
    report["runtime_paths"] = _path_evidence(root, lock_root, folder)
    current_stage = "enumeration"
    runtime: ApplicationRuntime | None = None
    device_id = f"control:{resource}"

    try:
        try:
            verify_runtime_paths(root, lock_root)
            report["stages"]["runtime_paths"] = {
                "status": "passed",
                "reason": "Native formal runtime and lock paths resolved without redirection",
            }
        except Exception as exc:
            report["stages"]["runtime_paths"] = {"status": "blocked", "reason": str(exc)}
            report["final_state"] = "Blocked before serial open; formal runtime path was redirected"
            return folder

        evidence = enumerate_selected(resource)
        atomic_json(folder / "enumeration.json", evidence)
        report["enumeration"] = evidence
        report["stages"]["enumeration"] = {
            "status": "passed" if evidence["serial_ports"] else "blocked",
            "reason": "Selected COM port and VISA ASRL alias enumerated; enumeration opened no port",
        }
        if not evidence["serial_ports"]:
            raise DeviceError("Selected COM port is absent", "driver_missing")

        atomic_json(
            folder / "official-sources.json",
            {
                "product": OFFICIAL_PRODUCT_URL,
                "downloads": OFFICIAL_DOWNLOAD_URL,
                "protocol_status": "user-supplied-v23.02.02",
                "missing": [
                    "instrument serial/firmware query",
                    "stop-bit confirmation",
                    "full model applicability",
                ],
            },
        )
        current_stage = "compatibility_connection"

        def factory(selected: ConnectionConfig):
            if selected.resource != resource or selected.adapter != "joinwit.jw8105":
                raise DeviceError(
                    "Only the explicitly selected JW8105 resource is allowed", "identity_conflict"
                )
            adapter = create_adapter(selected, runtime.visa)  # type: ignore[union-attr]
            counters["ports_opened"] += 1
            adapter.transport = TraceTransport(adapter.transport, transcript, counters)  # type: ignore[attr-defined]
            return adapter

        runtime = ApplicationRuntime((config,), hardware=True)
        runtime.sessions.factory = factory
        identity = runtime.sessions.connect(device_id, config, configure_acquisition=False).result(timeout=90)
        report["identity"] = jsonable(identity)
        report["stages"]["identity"] = {
            "status": "passed",
            "reason": "0x0140/0x0141 response identified Joinwit JW8105 at protocol level; serial and firmware unavailable",
        }
        report["stages"]["compatibility_connection"] = {
            "status": "passed",
            "reason": "Model adapter opened the selected COM port under the formal process and resource leases",
        }

        def call(operation: str, argument: Any = None) -> Any:
            return runtime.sessions.submit(device_id, operation, argument).result(timeout=90)

        def read_one(operation: str, channel: int = 1) -> Any:
            return _value(jsonable(call("read_settings", ((operation,), channel))))

        def snapshot() -> dict[str, Any]:
            return {
                "status": jsonable(call("status")),
                "wavelength": read_one("wavelength"),
                "display_wavelengths": read_one("display_wavelengths"),
                "sampling_rate": {str(channel): read_one("sampling_rate", channel) for channel in CHANNELS},
            }

        current_stage = "parameter_read"
        initial = snapshot()
        report["initial_snapshot"] = initial
        atomic_json(folder / "initial-state.json", initial)
        report["stages"]["parameter_read"] = {
            "status": "passed",
            "reason": "Complete writable-parameter backup read before any write",
        }

        def write_restore(
            operation: str,
            channel: int,
            original: Any,
            target: Any,
            make_parameters: Callable[[Any], dict[str, Any]],
        ) -> None:
            entry: dict[str, Any] = {
                "operation": operation,
                "channel": channel,
                "original": original,
                "target": target,
                "started_at": utc_now(),
            }
            report["operations"].append(entry)
            before = read_one(operation, channel)
            entry["before_write"] = before
            if not _same(before, original):
                raise DeviceError(
                    f"{operation} changed before write; refusing to overwrite", "readback_mismatch"
                )
            result = call(
                "apply",
                DeviceCommand(operation, make_parameters(target), channel, confirmed=True),
            )
            entry["write"] = jsonable(result)
            if result.outcome != "success":
                raise DeviceError(f"{operation} write was not confirmed: {result.error}", "write_uncertain")
            entry["write_succeeded"] = True
            report["writes_completed"] = int(report.get("writes_completed", 0)) + 1
            actual = read_one(operation, channel)
            entry["readback"] = actual
            if not _same(actual, target):
                raise DeviceError(f"{operation} readback differed from requested value", "readback_mismatch")
            restore = call(
                "apply",
                DeviceCommand(operation, make_parameters(original), channel, confirmed=True),
            )
            entry["restore"] = jsonable(restore)
            if restore.outcome != "success":
                raise DeviceError(
                    f"{operation} restoration was not confirmed: {restore.error}", "restoration_failed"
                )
            restored = read_one(operation, channel)
            entry["restore_readback"] = restored
            if not _same(restored, original):
                raise DeviceError(
                    f"{operation} restoration readback differed from backup", "restoration_failed"
                )
            entry["state"] = "passed"
            entry["finished_at"] = utc_now()
            save()

        current_stage = "parameter_write"
        wavelength = initial["wavelength"]
        if not isinstance(wavelength, (int, float)) or not 850 <= wavelength <= 1624:
            raise DeviceError("Wavelength backup is outside the reversible test range", "invalid_parameter")
        write_restore(
            "wavelength", 1, wavelength, round(float(wavelength) + 1.0, 2), lambda value: {"value": value}
        )

        for channel in CHANNELS:
            original_rate = initial["sampling_rate"][str(channel)]
            if not isinstance(original_rate, int) or not 0 <= original_rate <= 8:
                raise DeviceError(
                    f"Sampling-rate backup for channel {channel} is invalid", "invalid_parameter"
                )
            target_rate = original_rate + 1 if original_rate < 8 else original_rate - 1
            write_restore(
                "sampling_rate", channel, original_rate, target_rate, lambda value: {"value": value}
            )

        display = initial["display_wavelengths"]
        if not isinstance(display, list) or not display or not isinstance(display[0], (int, float)):
            raise DeviceError("Display-wavelength backup is invalid", "invalid_parameter")
        target_display = list(display)
        target_display[0] = (
            int(target_display[0]) + 1 if int(target_display[0]) < 1625 else int(target_display[0]) - 1
        )
        write_restore(
            "display_wavelengths",
            1,
            display,
            target_display,
            lambda value: {"value": value},
        )
        report["stages"]["parameter_write"] = {
            "status": "passed",
            "reason": "Wavelength, all eight sampling-rate channels and display wavelengths changed once with explicit confirmation",
        }

        current_stage = "restoration"
        final = snapshot()
        report["final_snapshot"] = final
        atomic_json(folder / "final-state.json", final)
        if final["wavelength"] != initial["wavelength"]:
            raise DeviceError("Final wavelength differs from backup", "restoration_failed")
        if final["display_wavelengths"] != initial["display_wavelengths"]:
            raise DeviceError("Final display wavelengths differ from backup", "restoration_failed")
        if final["sampling_rate"] != initial["sampling_rate"]:
            raise DeviceError("Final sampling rates differ from backup", "restoration_failed")
        report["stages"]["restoration"] = {
            "status": "passed",
            "reason": "Every changed value was read back at its original value and the complete final snapshot matched the backup",
        }
        report["stages"]["reconnection"] = {
            "status": "not_tested",
            "reason": "Reconnection was covered by the preceding read-only batch; this batch focused on reversible writes",
        }
        report["stages"]["physical_output"] = {
            "status": "not_tested",
            "reason": "No calibrated external reference or optical output measurement was performed",
        }
        report["final_state"] = (
            "All requested parameter writes read back successfully and were restored; final snapshot matches the backup; COM port released"
        )
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        report["error"] = str(exc)
        report["error_code"] = code
        report["stages"][current_stage] = {
            "status": "blocked"
            if code in {"resource_busy", "timeout", "driver_missing", "connection_failed", "disconnected"}
            else "failed",
            "reason": str(exc),
        }
        report["final_state"] = (
            "Write sequence stopped; inspect final-state/readback evidence before any further command. Final restoration is not claimed."
        )
    finally:
        if runtime is not None:
            try:
                if device_id in runtime.sessions.sessions:
                    runtime.sessions.disconnect(device_id, drain_reads=True)
            finally:
                runtime.close()
        report["finished_at"] = utc_now()
        report["evidence_sha256"] = {
            path.name: digest(path) for path in folder.glob("*.json") if path.name != "results.json"
        }
        save()
        rows = "\n".join(
            f"| {key} | {value.get('status')} | {value.get('reason', '')} |"
            for key, value in report["stages"].items()
        )
        (folder / "record.md").write_text(
            f"# JW8105 参数写入 / 读回 / 恢复测试 / {resource}\n\n"
            f"时间：{report['started_at']} 至 {report['finished_at']}。\n\n"
            "## 结论\n\n"
            f"{report['final_state']}\n\n"
            "| 项目 | 结果 | 说明 |\n| --- | --- | --- |\n" + rows + "\n\n"
            "每个操作先保存原值，再执行一次确认写入、读回、恢复写入和恢复读回；未执行盲目重发。"
            "物理输出测量仍未完成，串口桥序列号不作为仪器身份。原始帧见 communication.jsonl，"
            "完整备份见 initial-state.json，最终状态见 final-state.json。\n",
            encoding="utf-8",
        )
        publish_report(folder)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--asset-name", default="JW8105-UNKNOWN-COM9")
    parser.add_argument(
        "--confirm", action="store_true", help="Explicitly authorize reversible hardware writes"
    )
    args = parser.parse_args()
    if not args.confirm:
        parser.error("Parameter writes require --confirm")
    folder = run(args.resource, args.asset_name)
    report = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {"folder": str(folder), "stages": report["stages"], "final_state": report["final_state"]},
            ensure_ascii=False,
        )
    )
    return int(bool(report.get("error")))


if __name__ == "__main__":
    raise SystemExit(main())
