"""Archive selected-port enumeration; never open a port or send instrument commands."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from lab_control.domain.instruments import utc_now
from lab_control.instruments.common.hardware_paths import physical_path, verify_runtime_paths
from lab_control.storage.hardware_records import atomic_json, digest
from lab_control.storage.repository import application_home

from ..profile import OFFICIAL_DOWNLOAD_URL, OFFICIAL_PRODUCT_URL, PROTOCOL_EVIDENCE
from .records import publish_report

REASON = (
    "JW8105 V23.02.02 帧协议已归档并实现；连接回包不提供仪器序列号/固件，"
    "停止位和实机型号仍需确认"
        )


def enumerate_selected(resource: str) -> dict[str, Any]:
    from serial.tools.list_ports import comports

    from lab_control.transport.visa import VisaRuntime

    ports = [
        {key: getattr(port, key, None) for key in (
            "device", "description", "hwid", "vid", "pid", "serial_number", "manufacturer", "location"
        )}
        for port in comports()
        if port.device.upper() == resource
    ]
    visa = VisaRuntime()
    aliases: list[str] = []
    error = ""
    try:
        expected = f"ASRL{int(resource[3:])}::INSTR"
        aliases = [value for value in visa.manager.list_resources("ASRL?*::INSTR") if value == expected]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        visa.close()
    return {"serial_ports": ports, "visa_aliases": aliases, "visa_error": error, "ports_opened": 0}


def run(resource: str, asset_name: str) -> Path:
    resource = resource.upper()
    if not re.fullmatch(r"COM[1-9][0-9]*", resource):
        raise ValueError("Specify a single COM port; no wildcard or address guessing")
    if not re.fullmatch(r"JW8105-UNKNOWN-[A-Za-z0-9_-]+", asset_name):
        raise ValueError("Use a provisional JW8105-UNKNOWN-<manual-id> asset name until identity is verified")
    root = application_home()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    folder = root / "hardware-tests" / asset_name / f"{stamp}-preflight-{uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "operator": "Explicit JW8105 metadata preflight",
        "adapter_id": "joinwit.jw8105",
        "adapter_registered": True,
        "target_model": "JW8105",
        "identity": {"manufacturer": "", "model": "", "serial": "", "firmware": "", "confidence": "unknown"},
        "resource": resource,
        "transport": "serial",
        "scope": "Selected COM port OS/VISA enumeration and runtime path checks; no port opened or commands sent",
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "pyvisa": importlib.metadata.version("PyVISA"),
            "pyserial": importlib.metadata.version("pyserial"),
            "runtime_home": str(root),
        },
        "source_sha256": {
            path.name: digest(path)
            for path in (
                Path(__file__),
                Path(__file__).parents[1] / "adapter.py",
                Path(__file__).parents[1] / "protocol.py",
            )
        },
        "initial_state": "Unknown; no instrument queries performed; optical input and settings not verified",
        "stages": {
            key: {"status": "not_tested", "reason": "Preflight only; no instrument I/O"}
            for key in (
                "enumeration", "runtime_paths", "identity", "compatibility_connection", "parameter_read",
                "parameter_write", "restoration", "reconnection", "physical_output"
            )
        },
        "notes": [REASON, PROTOCOL_EVIDENCE, "USB bridge serial is not the instrument serial"],
        "final_state": {"ports_opened": 0, "commands_sent": 0, "settings_modified": False, "instrument_state": "unknown"},
    }
    atomic_json(folder / "results.json", report)
    try:
        lock_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl"
        paths = {
            "runtime": root, "inventory": root / "inventory.sqlite3", "global_lock": lock_root / "hardware.lock",
            "device_locks": lock_root / "devices", "batch": folder,
        }
        path_evidence = {}
        for key, path in paths.items():
            try:
                path_evidence[key] = {"requested": str(path), "physical": str(physical_path(path))}
            except OSError as exc:
                path_evidence[key] = {"requested": str(path), "error": str(exc)}
        atomic_json(folder / "runtime-paths.json", path_evidence)
        try:
            verify_runtime_paths(root, lock_root)
            report["stages"]["runtime_paths"] = {"status": "passed", "reason": "OS paths match formal paths"}
        except Exception as exc:
            report["stages"]["runtime_paths"] = {"status": "blocked", "reason": str(exc)}
        evidence = enumerate_selected(resource)
        atomic_json(folder / "enumeration.json", evidence)
        report["stages"]["enumeration"] = {
            "status": "passed" if evidence["serial_ports"] else "blocked",
            "reason": "Selected serial bridge enumerated; protocol available but instrument identity unknown" if evidence["serial_ports"] else "Selected COM port absent",
        }
        for key in ("identity", "compatibility_connection"):
            report["stages"][key] = {"status": "blocked", "reason": REASON}
        atomic_json(folder / "official-sources.json", {
            "product": OFFICIAL_PRODUCT_URL, "downloads": OFFICIAL_DOWNLOAD_URL,
            "reviewed_on": "2026-10-10", "protocol_status": "user-supplied-v23.02.02",
            "missing": ["instrument serial/firmware query", "stop-bit confirmation", "full model/firmware applicability"],
        })
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["finished_at"] = utc_now()
        report["evidence_sha256"] = {p.name: digest(p) for p in folder.glob("*.json") if p.name != "results.json"}
        atomic_json(folder / "results.json", report)
        rows = [f"| {key} | {item['status']} | {item['reason']} |" for key, item in report["stages"].items()]
        (folder / "record.md").write_text(
            f"# JW8105 协议预检 / {resource}\n\n"
            f"时间：{report['started_at']} 至 {report['finished_at']}。\n\n"
            "## 资产与环境\n\n型号为用户指定目标，仪器序列号、固件和光输入状态未知；桥接器序列号不作为仪器身份。环境及源码哈希见 [原始结果](results.json)。\n\n"
            "## 范围与证据\n\n仅枚举所选端口并检查运行路径。\n\n| 项目 | 结果 | 原因 |\n| --- | --- | --- |\n"
            + "\n".join(row.replace("\n", " ") for row in rows)
            + "\n\n## 恢复与最终状态\n\n未打开串口、未发送命令、未修改参数；实际仪器状态未知，恢复与重连未测试。\n\n"
            "## 未测及后续\n\n先核对协议适用型号、停止位、铭牌完整型号及序列号，再在正式物理路径和占用锁有效时按型号硬件计划执行身份和参数测试。枚举不代表通信或整机验收通过。\n",
            encoding="utf-8",
        )
        publish_report(folder)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--asset-name", required=True)
    args = parser.parse_args()
    folder = run(args.resource, args.asset_name)
    print(json.dumps({"folder": str(folder), "protocol_status": "available_pending_identity", "ports_opened": 0}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
