"""Environment reports and proposed repairs; no repair executor is exposed."""

from __future__ import annotations

import copy
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import sys
import tempfile
import threading
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lab_control.application.config import ConnectionConfig
from lab_control.deployment.catalog import (
    cache_directories,
    load_manifest,
    payload_status,
    required_components,
)

STATES = {"ok", "missing", "incompatible", "unknown", "not_required", "error"}


def export_report(report: dict, path: Path) -> None:
    """Atomically export an explicitly chosen report. No repair is performed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".environment-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def plan_repair(report: dict) -> dict:
    actions = []
    for item in report.get("items", []):
        if item["state"] in {"ok", "not_required"}:
            continue
        actions.append(
            {
                "id": item["id"],
                "title": item["title"],
                "reason": item["reason"],
                "action": item["action"],
                "affected": item["affected"],
                "payload": item.get("payload", {"state": "not_applicable"}),
                "requires_admin": item.get("requires_admin", False),
                "reboot": item.get("reboot", "no"),
                "state": "needs_verification" if item["state"] in {"unknown", "error"} else "proposed",
            }
        )
    return {
        "schema_version": 1,
        "kind": "repair_plan",
        "executed": False,
        "executor_available": False,
        "inspection_complete": report.get("state") == "complete",
        "notice": "自动修复待安装维护组件接入",
        "actions": actions,
    }


def _storage(root: Path) -> tuple[str, str]:
    # A temporary write is the only mutation performed by environment inspection.
    if not root.is_dir():
        return "missing", "数据目录不存在；请检查目录设置"
    free = shutil.disk_usage(root).free
    with tempfile.TemporaryFile(dir=root) as stream:
        stream.write(b"TF-LabControl storage check")
        stream.flush()
        os.fsync(stream.fileno())
    if free < 1024**3:
        return "unknown", f"数据目录可写；剩余 {free / 1024**3:.2f} GiB，请确认采集所需容量"
    return "ok", f"数据目录可写；剩余 {free / 1024**3:.1f} GiB"


def expected_versions() -> dict[str, str]:
    bundled = Path(__file__).with_name("release-manifest.json")
    lock = Path(__file__).resolve().parents[3] / "uv.lock"
    if bundled.is_file():
        packages = json.loads(bundled.read_text(encoding="utf-8"))["packages"]
    elif lock.is_file():
        packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    else:
        return {}
    return {item["name"].lower(): item["version"] for item in packages}


def inspect_environment(
    root: Path,
    configs: tuple[ConnectionConfig, ...] = (),
    *,
    lan: bool = False,
    dhcp: bool = False,
    cancel: threading.Event | None = None,
    progress: Callable[[dict], None] | None = None,
    caches: tuple[Path, ...] | None = None,
) -> dict:
    cancel = cancel or threading.Event()
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "environment_inspection",
        "state": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "root": str(root),
        "hardware_accessed": False,
        "settings_changed": False,
        "executor_available": False,
        "items": [],
        "observations": {},
    }

    def add(key, title, state, reason, action, affected=(), **extra):
        assert state in STATES
        report["items"].append(
            {
                "id": key,
                "title": title,
                "state": state,
                "reason": reason,
                "action": action,
                "affected": list(affected),
                **extra,
            }
        )
        if progress:
            progress(copy.deepcopy(report))

    def finish():
        report["state"] = "cancelled" if cancel.is_set() else "complete"
        report["repair_plan"] = plan_repair(report)
        return report

    supported = (
        sys.platform == "win32"
        and struct.calcsize("P") == 8
        and platform.machine().lower() in {"amd64", "x86_64"}
    )
    if sys.platform == "win32":
        supported = (
            supported and sys.getwindowsversion().build >= 17763 and sys.getwindowsversion().product_type == 1
        )
    add(
        "platform",
        "操作系统与架构",
        "ok" if supported else "incompatible",
        platform.platform(),
        "使用已验证的 Windows 10/11 x64 安装包",
    )
    try:
        manifest = load_manifest()
        needed = required_components(configs, lan=lan, dhcp=dhcp)
        add("manifest", "组件台账", "ok", "组件清单已加载", "使用同版本离线安装包修复程序文件")
    except Exception as exc:
        add("manifest", "组件台账", "error", str(exc), "使用同版本离线安装包修复程序文件")
        return finish()
    try:
        expected = expected_versions()
    except (OSError, ValueError, KeyError) as exc:
        expected = {}
        add("versions", "发行版本清单", "error", str(exc), "使用同版本安装包修复版本清单")
    for name in {**manifest["python_roots"], **manifest.get("python_transitive_roots", {})}:
        if cancel.is_set():
            return finish()
        try:
            version = importlib.metadata.version(name)
            wanted = expected.get(name.lower())
            state = "ok" if version == wanted else "incompatible" if wanted else "unknown"
            add(
                "python:" + name,
                name,
                state,
                f"检测版本 {version}；发行要求 {wanted or '待核实'}",
                "使用同版本离线安装包修复应用运行库",
            )
        except importlib.metadata.PackageNotFoundError:
            add(
                "python:" + name,
                name,
                "missing",
                "运行包或版本元数据缺失",
                "使用同版本离线安装包修复应用运行库",
            )
    try:
        from lab_control.app import load_stylesheet

        if not load_stylesheet().strip():
            raise ValueError("主题文件为空")
        add("resources", "应用资源", "ok", "主题和型号注册可读取", "使用同版本离线安装包修复应用文件")
    except Exception as exc:
        add("resources", "应用资源", "error", str(exc), "使用同版本离线安装包修复应用文件")
    try:
        state, reason = _storage(root)
        add("storage", "数据目录", state, reason, "检查保存路径、可用容量和当前用户权限")
    except OSError as exc:
        add("storage", "数据目录", "error", str(exc), "检查保存路径、可用容量和当前用户权限")
    if cancel.is_set():
        return finish()
    windows = {}
    try:
        from lab_control.deployment.windows import inspect_windows

        windows = inspect_windows(cancel)
        report["observations"]["windows"] = windows
    except InterruptedError:
        return finish()
    except Exception as exc:
        add("windows", "Windows 组件检查", "unknown", str(exc), "重试检查并导出报告", requires_admin=False)
    if cancel.is_set():
        return finish()
    if lan or dhcp:
        try:
            from lab_control.application.network_setup import inspect_network

            network = inspect_network(cancel)
            report["observations"]["network"] = network
            usable = [a for a in network["adapters"] if a["up"] and a["ethernet"] and a["addresses"]]
            add(
                "network",
                "实验网络",
                "ok" if usable else "missing",
                f"发现 {len(usable)} 个具有 IPv4 地址的已连接物理有线网口；实验接口需核对",
                "在部署向导中选择实验网口并重新生成本机网络方案",
                requires_admin=True,
            )
            add(
                "dhcp-state",
                "DHCP 状态",
                "unknown",
                f"本机 UDP 67 监听者 {len(network.get('dhcp_listeners', []))} 个；不能据此判断网络中其他 DHCP",
                "核对实验网口与已有 DHCP；独立实验网按本机方案部署",
                requires_admin=True,
            )
        except Exception as exc:
            add("network", "实验网络", "unknown", str(exc), "重试检查；核对当前实验网卡", requires_admin=True)
        add(
            "firewall",
            "防火墙",
            "unknown",
            windows.get("firewall_error")
            or "已记录可读取的配置；未进行报文测试，尚未核实部署规则与实际程序路径",
            "由安装维护组件按实验网口和当前程序路径核验并恢复应用规则",
            requires_admin=True,
        )
    else:
        add(
            "network",
            "实验网络 / DHCP / 防火墙",
            "not_required",
            "当前连接未启用实验网络",
            "按实际设备路线启用",
        )
    for component in manifest["components"]:
        if cancel.is_set():
            return finish()
        key = component["id"]
        affected = sorted(
            {
                c.adapter or c.transport
                for c in configs
                if (c.transport in component["transports"] or c.adapter in component["adapters"])
            }
        ) or (["实验网络发现"] if key == "rs-visa" and lan else component["adapters"])
        if key not in needed:
            add(
                key,
                component["name"],
                "not_required",
                "当前设备与连接方式未需要",
                "需要时由安装包准备",
                affected,
            )
            continue
        state, reason = "unknown", "已记录需求；安装检测与目标版本兼容性尚待核实"
        if component["detection"].get("kind") == "files_usb":
            try:
                from lab_control.deployment.file_components import inspect_files_usb

                state, reason = inspect_files_usb(component["detection"], tuple(
                    c for c in configs if c.adapter in component["adapters"]
                ))
            except Exception as exc:
                state, reason = "error", str(exc)
        elif key == "rs-visa":
            # PyVISA ResourceManager may be shared with active sessions: do not create/close one.
            try:
                from pyvisa.ctwrapper.highlevel import IVIVisaLibrary

                paths = [str(path) for path in IVIVisaLibrary.get_library_paths()]
                state, reason = (
                    "unknown",
                    "VISA 库候选：" + ", ".join(paths) + "；未打开资源管理器，实际加载及版本待核实",
                )
            except Exception as exc:
                state, reason = "unknown", str(exc)
        elif component["sdk"]:
            from lab_control.application.environment import dependency_diagnostics

            checks = dependency_diagnostics(
                configs,
                sdk_kinds=(
                    ("pm100d" if any(c.transport == "pm100d" for c in configs) else component["sdk"]),
                ),
            )["sdk"]
            detail = next(iter(checks.values()))
            state = {"loadable": "unknown", "missing": "missing", "error": "incompatible"}[detail["state"]]
            reason = (
                "DLL 检查：" + json.dumps(detail, ensure_ascii=False) + "；驱动版本与实机兼容性须另行核实"
            )
        elif key == "msvc-runtime":
            vc = windows.get("vc_runtime") or {}
            if vc.get("Installed") == 1:
                state, reason = "unknown", f"检测到 VC++ {vc.get('Version')}；目标运行库版本约束待核实"
            else:
                state, reason = "unknown", windows.get("vc_runtime_error", "无法核实 VC++ 运行库")
        elif key in {"ftdi-vcp", "keithley-usb", "serial-interface", "gpib-interface"}:
            drivers = windows.get("drivers", [])
            reason = f"已记录 {len(drivers)} 条相关 PnP 驱动信息；需要按实际接口硬件 ID 核实版本"
        payload = payload_status(component, caches if caches is not None else cache_directories(), cancel)
        if cancel.is_set():
            return finish()
        add(
            key,
            component["name"],
            state,
            reason,
            "使用离线维护组件检测并准备匹配版本；当前不执行安装",
            affected,
            payload=payload,
            requires_admin=component["installation"]["requires_admin"],
            reboot=component["installation"]["reboot"],
        )
    return finish()
