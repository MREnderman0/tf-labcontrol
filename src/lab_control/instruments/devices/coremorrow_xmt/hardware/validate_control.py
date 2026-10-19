"""Explicit reversible CoreMorrow XMT voltage control validation."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_records import atomic_json, digest
from lab_control.storage.repository import application_home

from ..profile import OFFICIAL_E00_PAGE, OFFICIAL_E00_SOFTWARE_MANUAL, VENDOR_HEADER_EVIDENCE
from .readonly import _enumerate
from .records import publish_report

PORT_PATTERN = re.compile(r"COM[1-9][0-9]*", re.IGNORECASE)
CHANNELS = (1, 2, 3)
TEST_VOLTAGE = 0.25
SAFE_GATE = 0.1


class TraceTransport:
    """Record binary reads and writes without changing transport semantics."""

    def __init__(self, inner: Any, transcript: Path, counters: dict[str, int]) -> None:
        self.inner = inner
        self.resource = inner.resource
        self.transcript = transcript
        self.counters = counters

    def _write(self, direction: str, frame: bytes) -> None:
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"at": utc_now(), "direction": direction, "frame": frame.hex(" ")}, ensure_ascii=False
                )
                + "\n"
            )

    def query_frame(self, payload: bytes) -> bytes:
        self.counters["commands_sent"] += 1
        self._write("tx", payload)
        try:
            response = self.inner.query_frame(payload)
        except Exception as exc:
            self._write("error", str(exc).encode("utf-8", errors="replace"))
            raise
        self._write("rx", response)
        return response

    def write_frame(self, payload: bytes) -> None:
        self.counters["commands_sent"] += 1
        self._write("tx", payload)
        writer = getattr(self.inner, "write_frame", None)
        if callable(writer):
            writer(payload)
        else:
            self.inner.write(payload)

    def close(self) -> None:
        self.inner.close()
        self.counters["ports_closed"] += 1


def _config(resource: str, baud_rate: int) -> ConnectionConfig:
    return ConnectionConfig(
        resource=resource,
        adapter="coremorrow.xmt",
        transport="serial",
        baud_rate=baud_rate,
        timeout=3.0,
        terminator="\n",
    )


def _safe_asset_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("asset name must contain only letters, digits, dot, underscore or hyphen")
    return value


def _read_value(runtime: ApplicationRuntime, device_id: str, channel: int) -> float:
    readings = runtime.sessions.submit(device_id, "read_settings", (("voltage",), channel)).result(timeout=90)
    values = jsonable(readings)
    if (
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0].get("value"), (int, float))
    ):
        raise DeviceError(f"CH {channel} voltage readback is invalid", "parse_error")
    return float(values[0]["value"])


def run(resource: str, asset_name: str, baud_rate: int, recovery_zero: bool = False) -> Path:
    resource = resource.upper()
    if not PORT_PATTERN.fullmatch(resource):
        raise ValueError("Specify one explicit COM port")
    asset_name = _safe_asset_name(asset_name)
    root = application_home()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    folder = root / "hardware-tests" / asset_name / f"{stamp}-control-{uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    transcript = folder / "communication.jsonl"
    transcript.touch(exist_ok=False)
    counters = {"ports_opened": 0, "ports_closed": 0, "commands_sent": 0}
    config = _config(resource, baud_rate)
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "adapter_id": "coremorrow.xmt",
        "model": "E00.C3-13 (operator nameplate; protocol identity pending)",
        "config": jsonable(config),
        "resource": resource,
        "expected_serial": "",
        "identity": {"manufacturer": "", "model": "", "serial": "", "firmware": "", "confidence": "unknown"},
        "scope": (
            "One selected COM6 asset; backup all three voltage setpoints, test one 0.25 V write per channel, "
            "read back, restore each original value, and release the session."
            if not recovery_zero
            else "Recovery of one selected COM6 asset after an interrupted control test; backup all three voltage setpoints, set each to 0 V once, read back, and release the session."
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
            OFFICIAL_E00_PAGE,
            OFFICIAL_E00_SOFTWARE_MANUAL,
            VENDOR_HEADER_EVIDENCE,
            "The 0..120 V package limit is the positive control envelope selected from the E00.C/E03.00.13 output data; negative output and physical measurement remain outside this test.",
        ],
        "final_state": "No instrument I/O attempted yet",
        "final_outputs": {"settings_modified": False, "physical_output_verified": False, **counters},
        "operations": [],
    }

    def save() -> None:
        report["final_outputs"] = {
            "settings_modified": bool(report.get("writes_completed")),
            "physical_output_verified": False,
            **counters,
        }
        atomic_json(folder / "results.json", report)

    save()
    runtime: ApplicationRuntime | None = None
    device_id = f"control:{resource}"
    current_stage = "enumeration"
    try:
        evidence = _enumerate(resource)
        atomic_json(folder / "enumeration.json", evidence)
        report["enumeration"] = evidence
        report["stages"]["enumeration"] = {
            "status": "passed" if evidence["serial_ports"] else "blocked",
            "reason": "Selected COM port enumerated without opening it",
        }
        if not evidence["serial_ports"]:
            raise DeviceError("Selected COM port is absent", "driver_missing")

        current_stage = "compatibility_connection"
        runtime = ApplicationRuntime((config,), hardware=True)

        def factory(selected: ConnectionConfig):
            if selected.resource != resource or selected.adapter != config.adapter:
                raise DeviceError(
                    "Only the explicitly selected CoreMorrow resource is allowed", "identity_conflict"
                )
            adapter = create_adapter(selected, runtime.visa)
            counters["ports_opened"] += 1
            adapter.transport = TraceTransport(adapter.transport, transcript, counters)  # type: ignore[attr-defined]
            return adapter

        runtime.sessions.factory = factory
        identity = runtime.sessions.connect(device_id, config, configure_acquisition=False).result(timeout=90)
        report["identity"] = jsonable(identity)
        report["stages"]["identity"] = {
            "status": "passed",
            "reason": "Power configuration frame parsed; exact instrument serial and firmware remain unresolved",
        }
        report["stages"]["compatibility_connection"] = {
            "status": "passed",
            "reason": "Project adapter opened COM6 under the formal process and resource leases",
        }

        current_stage = "parameter_read"
        initial = {str(channel): _read_value(runtime, device_id, channel) for channel in CHANNELS}
        report["initial_snapshot"] = {"voltage": initial}
        atomic_json(folder / "initial-state.json", report["initial_snapshot"])
        report["stages"]["parameter_read"] = {
            "status": "passed",
            "reason": "All three writable voltage setpoints were read before any write",
        }
        if recovery_zero:
            current_stage = "parameter_write"
            for channel in CHANNELS:
                original = initial[str(channel)]
                entry: dict[str, Any] = {
                    "channel": channel,
                    "operation": "voltage",
                    "original": original,
                    "target": 0.0,
                    "started_at": utc_now(),
                }
                report["operations"].append(entry)
                result = runtime.sessions.submit(
                    device_id, "apply", DeviceCommand("voltage", {"value": 0.0}, channel, True)
                ).result(timeout=90)
                entry["write"] = jsonable(result)
                if result.outcome != "success":
                    raise DeviceError(
                        f"CH {channel} recovery write was not confirmed: {result.error}", "write_uncertain"
                    )
                report["writes_completed"] = int(report.get("writes_completed", 0)) + 1
                entry["readback"] = _read_value(runtime, device_id, channel)
                if abs(entry["readback"]) > 0.01:
                    raise DeviceError(f"CH {channel} recovery readback was not zero", "readback_mismatch")
                entry["state"] = "passed"
                entry["finished_at"] = utc_now()
            report["stages"]["parameter_write"] = {
                "status": "passed",
                "reason": "Recovery sent one explicit 0 V command to each channel and confirmed each readback",
            }
            report["stages"]["restoration"] = {
                "status": "not_tested",
                "reason": "This batch is a safety recovery; it does not claim restoration of the interrupted test value",
            }
            report["final_snapshot"] = {
                "voltage": {str(channel): _read_value(runtime, device_id, channel) for channel in CHANNELS}
            }
            atomic_json(folder / "final-state.json", report["final_snapshot"])
            report["stages"]["reconnection"] = {
                "status": "not_tested",
                "reason": "Recovery batch did not reopen the session",
            }
            report["stages"]["physical_output"] = {
                "status": "not_tested",
                "reason": "No calibrated external voltage measurement was connected",
            }
            report["final_state"] = (
                "Safety recovery completed; all three software readbacks are within 0.01 V of 0 V; interrupted original state is not claimed restored"
            )
        elif any(abs(value) > SAFE_GATE for value in initial.values()):
            report["stages"]["parameter_write"] = {
                "status": "blocked",
                "reason": f"At least one original setpoint exceeded the {SAFE_GATE} V safety gate; no write was sent",
            }
            report["stages"]["restoration"] = {"status": "not_tested", "reason": "No setting was changed"}
        else:
            current_stage = "parameter_write"
            for channel in CHANNELS:
                original = initial[str(channel)]
                restore_target = max(0.0, original)
                entry: dict[str, Any] = {
                    "channel": channel,
                    "operation": "voltage",
                    "original": original,
                    "target": TEST_VOLTAGE,
                    "restore_target": restore_target,
                    "started_at": utc_now(),
                }
                report["operations"].append(entry)
                result = runtime.sessions.submit(
                    device_id, "apply", DeviceCommand("voltage", {"value": TEST_VOLTAGE}, channel, True)
                ).result(timeout=90)
                entry["write"] = jsonable(result)
                if result.outcome != "success":
                    raise DeviceError(
                        f"CH {channel} write was not confirmed: {result.error}", "write_uncertain"
                    )
                report["writes_completed"] = int(report.get("writes_completed", 0)) + 1
                entry["readback"] = _read_value(runtime, device_id, channel)
                if abs(entry["readback"] - TEST_VOLTAGE) > 0.01:
                    raise DeviceError(f"CH {channel} readback differed from 0.25 V", "readback_mismatch")
                restore = runtime.sessions.submit(
                    device_id, "apply", DeviceCommand("voltage", {"value": restore_target}, channel, True)
                ).result(timeout=90)
                entry["restore"] = jsonable(restore)
                if restore.outcome != "success":
                    raise DeviceError(
                        f"CH {channel} restoration was not confirmed: {restore.error}", "restoration_failed"
                    )
                entry["restore_readback"] = _read_value(runtime, device_id, channel)
                if abs(entry["restore_readback"] - restore_target) > 0.01:
                    raise DeviceError(f"CH {channel} restoration differed from backup", "restoration_failed")
                entry["state"] = "passed"
                entry["finished_at"] = utc_now()
            report["stages"]["parameter_write"] = {
                "status": "passed",
                "reason": "Each of three channels received one 0.25 V write with explicit readback; no write was retried",
            }
            current_stage = "restoration"
            final = {str(channel): _read_value(runtime, device_id, channel) for channel in CHANNELS}
            report["final_snapshot"] = {"voltage": final}
            atomic_json(folder / "final-state.json", report["final_snapshot"])
            if any(abs(final[key] - max(0.0, initial[key])) > 0.01 for key in initial):
                raise DeviceError("Final voltage snapshot differs from backup", "restoration_failed")
            report["stages"]["restoration"] = {
                "status": "passed",
                "reason": "Every tested setpoint was read back within 0.01 V of its saved value; negative near-zero readbacks were restored to the supported 0 V setpoint",
            }
        report["stages"]["reconnection"] = {
            "status": "not_tested",
            "reason": "Reconnection was verified in the preceding read-only batch",
        }
        report["stages"]["physical_output"] = {
            "status": "not_tested",
            "reason": "No calibrated external voltage measurement was connected",
        }
        if not recovery_zero:
            report["final_state"] = (
                "Reversible project-adapter voltage test completed; final setpoints were checked in software; physical output remains unmeasured"
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
            "Control sequence stopped; inspect final readback evidence before any further command; restoration is not claimed unless its stage passed"
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
            f"# 芯明天 E00.C3-13 / {resource} / 兼容层可逆电压控制测试\n\n## 结论\n\n{report['final_state']}\n\n| 项目 | 结果 | 说明 |\n| --- | --- | --- |\n{rows}\n\n原始帧见 communication.jsonl；summary.json 为派生台账，未把软件读回当作物理输出验收。\n",
            encoding="utf-8",
        )
        publish_report(folder)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--asset-name", default="COREMORROW-E00-C3-13-COM6")
    parser.add_argument("--baud-rate", type=int, default=9600, choices=(9600, 115200))
    parser.add_argument(
        "--recovery-zero",
        action="store_true",
        help="After an interrupted test, set all selected channels to 0 V and verify the readback",
    )
    args = parser.parse_args()
    folder = run(args.resource, args.asset_name, args.baud_rate, args.recovery_zero)
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
