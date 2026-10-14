"""Explicit single-resource, read-only CoreMorrow XMT hardware test."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_records import atomic_json, digest
from lab_control.storage.repository import application_home

from ..profile import OFFICIAL_SITE, VENDOR_HEADER_EVIDENCE
from .records import publish_report

PORT_PATTERN = re.compile(r"(?:COM|ASRL)([1-9][0-9]*)(?:::\s*INSTR)?$", re.IGNORECASE)


class TraceTransport:
    def __init__(self, inner: Any, transcript: Path, counters: dict[str, int]) -> None:
        self.inner = inner
        self.resource = inner.resource
        self.transcript = transcript
        self.counters = counters
        self._lock = threading.Lock()

    def _write(self, item: dict[str, Any]) -> None:
        with self._lock:
            with self.transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": utc_now(), **item}, ensure_ascii=False) + "\n")

    def query_frame(self, payload: bytes) -> bytes:
        self.counters["commands_sent"] += 1
        self._write({"direction": "tx", "frame": payload.hex(" ")})
        try:
            response = self.inner.query_frame(payload)
        except Exception as exc:
            self._write({"direction": "error", "error": f"{type(exc).__name__}: {exc}"})
            raise
        self._write({"direction": "rx", "frame": response.hex(" ")})
        return response

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


def _enumerate(resource: str) -> dict[str, Any]:
    result: dict[str, Any] = {"requested": resource, "serial_ports": [], "visa_alias": f"ASRL{resource[3:]}::INSTR"}
    try:
        from serial.tools import list_ports

        for item in list_ports.comports():
            if item.device.upper() == resource.upper():
                result["serial_ports"].append(
                    {"device": item.device, "description": item.description, "hwid": item.hwid}
                )
    except Exception as exc:
        result["error"] = str(exc)
    return result


def run(resource: str, asset_name: str, baud_rate: int) -> Path:
    match = PORT_PATTERN.fullmatch(resource)
    if not match:
        raise ValueError("Specify one explicit COM or ASRL resource")
    if resource.upper().startswith("ASRL"):
        resource = f"COM{match.group(1)}"
    if not re.fullmatch(r"COM[1-9][0-9]*", resource, re.IGNORECASE):
        raise ValueError("Only a single COM resource is allowed for this entry")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", asset_name):
        raise ValueError("asset name must contain only letters, digits, dot, underscore or hyphen")

    root = application_home()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    folder = root / "hardware-tests" / asset_name / f"{stamp}-readonly-{uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    transcript = folder / "communication.jsonl"
    transcript.touch(exist_ok=False)
    counters = {"ports_opened": 0, "ports_closed": 0, "commands_sent": 0}
    config = _config(resource.upper(), baud_rate)
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "adapter_id": "coremorrow.xmt",
        "model": "XMT-compatible controller (exact model pending)",
        "config": jsonable(config),
        "resource": resource.upper(),
        "expected_serial": "",
        "identity": {"manufacturer": "", "model": "", "serial": "", "firmware": "", "confidence": "unknown"},
        "scope": "Explicit COM6-compatible read-only protocol test through the project adapter; no settings or output commands.",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "runtime_home": str(root),
            "process_id": os.getpid(),
            "executable": sys.executable,
        },
        "source_sha256": {path.name: digest(path) for path in (Path(__file__), Path(__file__).parents[1] / "adapter.py", Path(__file__).parents[1] / "protocol.py", Path(__file__).parents[1] / "transport.py")},
        "stages": {key: {"status": "not_tested", "reason": "Not reached"} for key in ("enumeration", "identity", "compatibility_connection", "parameter_read", "parameter_write", "restoration", "reconnection", "physical_output")},
        "notes": [OFFICIAL_SITE, VENDOR_HEADER_EVIDENCE, "Exact model, instrument serial and raw-data units remain pending vendor confirmation."],
        "final_state": "No instrument I/O attempted yet",
        "final_outputs": {"settings_modified": False, **counters},
    }

    def save() -> None:
        report["final_outputs"] = {"settings_modified": False, **counters}
        atomic_json(folder / "results.json", report)

    save()
    current_stage = "enumeration"
    runtime: ApplicationRuntime | None = None
    device_id = f"readonly:{resource.upper()}"
    try:
        evidence = _enumerate(resource.upper())
        atomic_json(folder / "enumeration.json", evidence)
        report["enumeration"] = evidence
        report["stages"]["enumeration"] = {"status": "passed" if evidence["serial_ports"] else "blocked", "reason": "Selected COM port enumerated without opening it"}
        if not evidence["serial_ports"]:
            raise DeviceError("Selected COM port is absent", "driver_missing")

        current_stage = "compatibility_connection"
        runtime = ApplicationRuntime((config,), hardware=True)

        def factory(selected: ConnectionConfig):
            if selected.resource != config.resource or selected.adapter != config.adapter:
                raise DeviceError("Only the explicitly selected CoreMorrow resource is allowed", "identity_conflict")
            adapter = create_adapter(selected, runtime.visa)
            counters["ports_opened"] += 1
            adapter.transport = TraceTransport(adapter.transport, transcript, counters)  # type: ignore[attr-defined]
            return adapter

        runtime.sessions.factory = factory
        identity = runtime.sessions.connect(device_id, config, configure_acquisition=False).result(timeout=90)
        report["identity"] = jsonable(identity)
        report["stages"]["identity"] = {"status": "passed", "reason": "XMT configuration frame parsed; exact model and serial remain unresolved"}
        report["stages"]["compatibility_connection"] = {"status": "passed", "reason": "Project session, resource lease and CoreMorrow adapter connected"}

        def call(operation: str, argument: Any = None) -> Any:
            return runtime.sessions.submit(device_id, operation, argument).result(timeout=90)

        current_stage = "parameter_read"
        readings = {"status": jsonable(call("status")), "raw_channel_data": {str(channel): jsonable(call("read_settings", (("raw_channel_data",), channel))) for channel in range(1, 4)}}
        report["initial_snapshot"] = readings
        report["stages"]["parameter_read"] = {"status": "passed", "reason": "Configuration and three raw channel frames read and validated"}
        report["stages"]["parameter_write"] = {"status": "not_tested", "reason": "Read-only scope; adapter rejects all writes"}
        report["stages"]["restoration"] = {"status": "not_tested", "reason": "No settings were changed"}

        current_stage = "reconnection"
        runtime.sessions.disconnect(device_id, drain_reads=True)
        reconnected = runtime.sessions.connect(device_id, config, configure_acquisition=False).result(timeout=90)
        reconnect_status = call("status")
        report["reconnections"] = [{"identity": jsonable(reconnected), "status": jsonable(reconnect_status), "identity_matches": reconnected == identity}]
        report["stages"]["reconnection"] = {"status": "passed" if reconnected == identity else "failed", "reason": "Session released and reopened once"}
        report["stages"]["physical_output"] = {"status": "not_tested", "reason": "No calibrated external measurement was connected"}
        report["final_state"] = "Read-only compatibility test passed; no settings or outputs were changed"
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        report["error"] = str(exc)
        report["error_code"] = code
        report["stages"][current_stage] = {"status": "blocked" if code in {"resource_busy", "timeout", "driver_missing", "connection_failed", "disconnected"} else "failed", "reason": str(exc)}
        report["final_state"] = "No settings were sent; final instrument state is unknown after the reported error"
    finally:
        if runtime is not None:
            try:
                if device_id in runtime.sessions.sessions:
                    runtime.sessions.disconnect(device_id, drain_reads=True)
            finally:
                runtime.close()
        report["finished_at"] = utc_now()
        report["evidence_sha256"] = {path.name: digest(path) for path in folder.glob("*.json") if path.name != "results.json"}
        save()
        rows = "\n".join(f"| {key} | {value.get('status')} | {value.get('reason', '')} |" for key, value in report["stages"].items())
        (folder / "record.md").write_text(
            f"# 芯明天 XMT 只读真机测试 / {resource}\n\n时间：{report['started_at']} 至 {report['finished_at']}。\n\n"
            f"## 结论\n\n{report['final_state']}\n\n| 项目 | 结果 | 说明 |\n| --- | --- | --- |\n{rows}\n\n"
            "完整型号、仪器序列号、原始数据单位和物理输出仍待核对；原始帧见 communication.jsonl，结构化结果见 results.json。\n",
            encoding="utf-8",
        )
        publish_report(folder)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--asset-name", default="COREMORROW-XMT-UNKNOWN-COM6")
    parser.add_argument("--baud-rate", type=int, default=9600, choices=(9600, 115200))
    args = parser.parse_args()
    folder = run(args.resource, args.asset_name, args.baud_rate)
    report = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    print(json.dumps({"folder": str(folder), "stages": report["stages"], "final_state": report["final_state"]}, ensure_ascii=False))
    return int(bool(report.get("error")))


if __name__ == "__main__":
    raise SystemExit(main())

