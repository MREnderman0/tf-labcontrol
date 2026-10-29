"""Explicit, separately elevated maintenance for one scoped MCP firewall rule."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


def firewall_plan(settings: dict, program: Path, *, remove=False) -> dict:
    import ipaddress

    address = ipaddress.ip_address(settings["lan_address"])
    if address.version != 4 or address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ValueError("防火墙仅适用于已选择的具体实验网 IPv4 地址")
    port = settings["lan_port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("端口无效")
    program = program.resolve(strict=True)
    identity = f"{program}|{address}|{port}"
    return {
        "schema_version": 1,
        "operation": "remove" if remove else "add",
        "program": str(program),
        "address": str(address),
        "port": port,
        "rule": "TF-LabControl-Agent-" + hashlib.sha256(identity.encode()).hexdigest()[:16],
        "remote_address": "LocalSubnet",
        "profile": "Private,Domain",
    }


def launch_firewall(runtime, settings, *, remove=False):
    if sys.platform != "win32":
        raise RuntimeError("防火墙维护仅支持 Windows")
    if not settings.get("lan_enabled"):
        raise ValueError("请先保存并启用实验网接入设置")
    # Existing maintenance admission prevents sessions from starting while this
    # operator-requested rule change is in progress.
    runtime.begin_maintenance()
    try:
        plan = firewall_plan(settings, Path(sys.executable), remove=remove)
        folder = runtime.repository.root / "agent/firewall"
        folder.mkdir(parents=True, exist_ok=True)
        name = uuid4().hex
        path, report = folder / f"{name}.json", folder / f"{name}.report.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        runtime.repository.audit("agent_firewall_requested", plan=plan, report=str(report))
        arguments = subprocess.list2cmdline(
            [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(Path(__file__).with_name("firewall.ps1")),
                "-Plan",
                str(path),
                "-Report",
                str(report),
            ]
        )
        literal = "'" + arguments.replace("'", "''") + "'"
        command = (
            "$ErrorActionPreference='Stop'; try { $child = Start-Process -FilePath powershell.exe "
            "-ArgumentList " + literal + " -Verb RunAs -WindowStyle Hidden -Wait -PassThru; "
            "exit $child.ExitCode } catch { Write-Error $_; exit 2 }"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True,
        )
        if completed.returncode and not report.is_file():
            raise RuntimeError("提升权限的维护未完成；未收到防火墙变更结果")
        if not report.is_file():
            raise RuntimeError("维护程序未产生结果；请核对 UAC 和防火墙状态")
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        runtime.repository.audit("agent_firewall_completed", result=result)
        if not result.get("passed"):
            raise RuntimeError(result.get("error", "防火墙变更未核实，请查看维护报告"))
        return result
    finally:
        runtime.end_maintenance()
