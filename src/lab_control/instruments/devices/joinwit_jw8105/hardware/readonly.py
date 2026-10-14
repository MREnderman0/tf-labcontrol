"""Selected JW8105 read-only verification with a complete hardware record."""

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
from lab_control.instruments.common.hardware_paths import physical_path, verify_runtime_paths
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_records import atomic_json, digest
from lab_control.storage.repository import application_home

from ..profile import OFFICIAL_DOWNLOAD_URL, OFFICIAL_PRODUCT_URL, PROTOCOL_EVIDENCE
from .preflight import enumerate_selected
from .records import publish_report

ASSET_PATTERN = re.compile(r"JW8105-UNKNOWN-[A-Za-z0-9_-]+")
PORT_PATTERN = re.compile(r"COM[1-9][0-9]*", re.IGNORECASE)
READ_OPERATIONS = ("display_power", "wavelength", "display_wavelengths")


class TraceTransport:
    """Log model-owned binary exchanges without permitting any extra I/O."""

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


def _safe_asset_name(value: str) -> str:
    if not ASSET_PATTERN.fullmatch(value):
        raise ValueError("asset name must be JW8105-UNKNOWN-<manual-id>")
    return value


def _config(resource: str) -> ConnectionConfig:
    return ConnectionConfig(
        resource=resource,
        adapter="joinwit.jw8105",
        transport="serial",
        baud_rate=115200,
        timeout=3.0,
        terminator="\n",
    )


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
    folder = root / "hardware-tests" / asset_name / f"{stamp}-readonly-{uuid4().hex[:8]}"
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
        "identity": {
            "manufacturer": "",
            "model": "",
            "serial": "",
            "firmware": "",
            "confidence": "unknown",
        },
        "scope": (
            "COM9 selected explicitly through the native PowerShell task; protocol identity and all "
            "documented read-only settings were queried. No setting, acquisition, reset, or output command was sent."
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
            "The supplied protocol has no serial-number or firmware query; USB bridge serial is not instrument identity.",
            "Stop bit is not stated by the supplied protocol; implementation used 115200 8N1 as an explicit provisional setting.",
        ],
        "final_state": "No instrument I/O attempted yet",
        "final_outputs": {"settings_modified": False, **counters},
    }

    def save() -> None:
        report["final_outputs"] = {"settings_modified": False, **counters}
        atomic_json(folder / "results.json", report)

    save()
    report["runtime_paths"] = _path_evidence(root, lock_root, folder)
    current_stage = "enumeration"
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
        runtime: ApplicationRuntime | None = None
        device_id = f"readonly:{resource}"
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

        try:
            runtime = ApplicationRuntime((config,), hardware=True)
            runtime.sessions.factory = factory
            future = runtime.sessions.connect(device_id, config, configure_acquisition=False)
            identity = future.result(timeout=90)
            report["identity"] = jsonable(identity)
            report["stages"]["identity"] = {
                "status": "passed",
                "reason": "0x0140/0x0141 response identified Joinwit JW8105 at protocol level; serial and firmware unavailable",
            }
            report["stages"]["compatibility_connection"] = {
                "status": "passed",
                "reason": "Model adapter opened COM9 under the formal process and resource leases",
            }

            def call(operation: str, argument: Any = None) -> Any:
                return runtime.sessions.submit(device_id, operation, argument).result(timeout=90)

            current_stage = "parameter_read"
            status = call("status")
            readings: dict[str, Any] = {"status": jsonable(status)}
            for operation in READ_OPERATIONS:
                readings[operation] = jsonable(call("read_settings", ((operation,), 1)))
            readings["sampling_rate"] = {
                str(channel): jsonable(call("read_settings", (("sampling_rate",), channel)))
                for channel in range(1, 9)
            }
            readings["pdl"] = {
                str(channel): jsonable(call("read_settings", (("pdl",), channel))) for channel in range(1, 9)
            }
            report["initial_snapshot"] = readings
            report["stages"]["parameter_read"] = {
                "status": "passed",
                "reason": "Power, display power, wavelength, display wavelengths, sampling and PDL readbacks completed",
            }
            report["stages"]["parameter_write"] = {
                "status": "not_tested",
                "reason": "Read-only scope; no setting commands were sent",
            }
            report["stages"]["restoration"] = {
                "status": "not_tested",
                "reason": "No settings were changed, so no restoration write was allowed",
            }
            current_stage = "reconnection"
            runtime.sessions.disconnect(device_id, drain_reads=True)
            reconnected = runtime.sessions.connect(device_id, config, configure_acquisition=False).result(
                timeout=90
            )
            reconnect_status = call("status")

            def stable_status(value: Any) -> Any:
                data = jsonable(value)
                if isinstance(data, dict):
                    data.pop("timestamp", None)
                return data

            report["reconnections"] = [
                {
                    "identity": jsonable(reconnected),
                    "status": jsonable(reconnect_status),
                    "identity_matches": reconnected == identity,
                    "status_matches": stable_status(reconnect_status) == stable_status(status),
                }
            ]
            if reconnected != identity or stable_status(reconnect_status) != stable_status(status):
                raise DeviceError("Read-only reconnect identity or status changed", "readback_mismatch")
            report["stages"]["reconnection"] = {
                "status": "passed",
                "reason": "Session released and reopened once; identity and status matched",
            }
            report["stages"]["physical_output"] = {
                "status": "not_tested",
                "reason": "No calibrated external reference or optical output measurement was performed",
            }
            report["final_state"] = (
                "Read-only protocol test passed; settings and output commands were not sent; COM9 session was released"
            )
        finally:
            if runtime is not None:
                try:
                    if device_id in runtime.sessions.sessions:
                        runtime.sessions.disconnect(device_id, drain_reads=True)
                finally:
                    runtime.close()
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
            "No settings were sent; final instrument state is unknown after the reported error"
        )
    finally:
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
            f"# JW8105 只读真机测试 / {resource}\n\n"
            f"时间：{report['started_at']} 至 {report['finished_at']}。\n\n"
            "## 结论\n\n"
            f"{report['final_state']}\n\n"
            "| 项目 | 结果 | 说明 |\n| --- | --- | --- |\n" + rows + "\n\n"
            "参数写入、恢复写入和物理输出测量按只读授权范围保留为未测试；串口桥序列号不作为仪器身份。"
            "原始帧见 communication.jsonl，结构化结果见 results.json。\n",
            encoding="utf-8",
        )
        publish_report(folder)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--asset-name", default="JW8105-UNKNOWN-COM9")
    args = parser.parse_args()
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
