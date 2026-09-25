"""Offline evidence summaries. No transport, inventory or instrument access."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

STATUSES = frozenset({"passed", "failed", "blocked", "not_tested"})
SCHEMA_VERSION = 1


def resource_protocol(resource: str) -> str:
    """Generic transport label; LAN protocols do not share control qualification."""
    value = resource.upper()
    if value.startswith("TCPIP"):
        if value.endswith("::SOCKET"):
            return "VISA SOCKET"
        return "HiSLIP" if "::HISLIP" in value else "VXI-11"
    if value.startswith("TCP://"):
        return "TCP Socket"
    if value.startswith("VICP://"):
        return "VICP"
    return ""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    """Only replace our derived artifact; never alter its source evidence."""
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def normalize(
    report: dict[str, Any],
    *,
    adapter_id: str,
    model: str,
    stage_names: dict[str, str],
    expected_models: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Common envelope only; model-specific mappings are supplied by each package."""
    issues: list[str] = []
    config = report.get("config", {})
    if not isinstance(config, dict):
        config = {}
    identity = report.get("identity", {})
    if isinstance(identity, str) and len(identity.split(",")) >= 4:
        parts = identity.strip().split(",")
        identity = dict(zip(("manufacturer", "model", "serial", "firmware"), parts[:4], strict=True))
    if not isinstance(identity, dict):
        identity = {}
    confidence = identity.get("confidence", "")
    identity = {
        key: str(identity.get(key) or "").strip() for key in ("manufacturer", "model", "serial", "firmware")
    }
    expected = str(report.get("expected_serial", config.get("expected_serial", "")))
    reliable = bool(identity["manufacturer"] and identity["model"] and identity["serial"])
    if identity["serial"].casefold() in {"unknown", "n/a", "none", "0", "-"} or confidence in {
        "unknown",
        "model",
    }:
        reliable = False
    if (
        expected_models
        and identity["model"]
        and identity["model"].replace(" ", "").casefold()
        not in {name.replace(" ", "").casefold() for name in expected_models}
    ):
        reliable = False
        issues.append("实际型号与所选记录转换器不一致")
    if expected and identity["serial"] and expected != identity["serial"]:
        reliable = False
        issues.append("实际身份与预期序列号不一致")
    if not reliable:
        issues.append("缺少可核验完整身份；不按目录名或地址合并资产")
    resource = str(report.get("resource", config.get("resource", "")))
    interface = (
        "USB"
        if resource.upper().startswith("USB")
        else "LAN"
        if resource.upper().startswith(("TCPIP", "TCP://", "VICP://"))
        else "GPIB"
        if resource.upper().startswith("GPIB")
        else "serial"
        if resource.upper().startswith(("ASRL", "COM"))
        else "unknown"
    )
    started = report.get("started_at", report.get("started", ""))
    finished = report.get("finished_at", report.get("finished", report.get("updated_at", "")))
    scope = report.get("scope", "")
    environment = report.get("environment", {})
    revision = report.get(
        "project_head", report.get("source_sha256", report.get("delivery_model_sha256", {}))
    )
    for name, value in (
        ("时间", started),
        ("接口", resource),
        ("测试范围", scope),
        ("环境", environment),
        ("源码版本/哈希", revision),
        ("固件", identity["firmware"]),
    ):
        if not value:
            issues.append(f"原记录缺少{name}")
    stages = report.get("stages", {})
    items = []
    if not isinstance(stages, dict):
        stages = {}
    if not stages:
        issues.append("无可解释的逐项阶段结果；保留原文待核对")
    for key, raw in stages.items():
        status = raw.get("state", raw.get("status", "")) if isinstance(raw, dict) else raw
        reason = raw.get("reason", "") if isinstance(raw, dict) else ""
        if not isinstance(status, str) or status not in STATUSES:
            issues.append(f"阶段 {key} 的结果 {status!r} 不属于统一枚举，未提升为通过")
            status = "not_tested"
        items.append(
            {
                "key": stage_names.get(key, key),
                "source_key": key,
                "status": status,
                "raw_result": raw,
                "reason": reason,
            }
        )
    # Preserve explicit observations, never infer OFF from a scope description or a missing field.
    final = {
        key: report[key]
        for key in (
            "final_outputs",
            "final_output_state",
            "final_state",
            "final_snapshot",
            "final_observed_snapshot",
            "restoration_differences",
            "restore_differences",
            "restoration_warning",
            "recovery_source",
            "restore_from",
        )
        if key in report
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter_id": adapter_id,
        "model": model,
        "identity": identity,
        "identity_verified": reliable,
        "expected_serial": expected,
        "resource": resource,
        "interface": interface,
        "protocol": resource_protocol(resource),
        "started_at": str(started),
        "finished_at": str(finished),
        "scope": scope,
        "environment": environment,
        "source_version": revision,
        "items": items,
        "final_state": final,
        "diagnostics": {
            key: report[key]
            for key in ("error", "control_error", "connection_note", "notes")
            if key in report
        },
        "completeness": "needs_review" if issues else "complete",
        "issues": issues,
    }


def validate(summary: dict[str, Any]) -> list[str]:
    errors = []
    if summary.get("schema_version") != SCHEMA_VERSION:
        errors.append("Unsupported summary schema_version")
    for name in ("adapter_id", "model", "identity", "interface", "started_at", "scope", "items", "issues"):
        if name not in summary:
            errors.append(f"Missing summary field: {name}")
    items = summary.get("items")
    if not isinstance(items, list):
        errors.append("items must be an array")
    else:
        keys = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("status"), str)
                or item.get("status") not in STATUSES
                or not item.get("key")
            ):
                errors.append("Invalid item key/status")
                continue
            if item["key"] in keys:
                errors.append(f"Duplicate item key: {item['key']}")
            keys.add(item["key"])
    return errors


def publish(folder: Path, summary: dict[str, Any]) -> None:
    errors = validate(summary)
    if errors:
        raise ValueError("; ".join(errors))
    source = folder / "results.json"
    summary = dict(summary, evidence={"path": "results.json", "sha256": digest(source)})
    atomic_json(folder / "summary.json", summary)


def publish_report(folder: Path, summarize: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """A derived-document failure must not interrupt instrument recovery."""
    try:
        report = json.loads((folder / "results.json").read_text(encoding="utf-8-sig"))
        publish(folder, summarize(report))
        (folder / "summary-error.json").unlink(missing_ok=True)
    except Exception as exc:
        logging.getLogger(__name__).warning("摘要生成失败，原始记录保留：%s: %s", folder, exc)
        try:
            atomic_json(folder / "summary-error.json", {"error": str(exc), "summary_available": False})
        except OSError:
            logging.getLogger(__name__).warning("摘要错误文件无法写入：%s", folder)


def matches(report: dict[str, Any], folder: Path, adapter_id: str, tokens: tuple[str, ...]) -> bool:
    """Choose a converter, not an asset identity. A filename is never proof of identity."""
    config = report.get("config", {})
    configured = report.get("adapter_id") or (config.get("adapter") if isinstance(config, dict) else None)
    if configured:
        return configured == adapter_id
    name = folder.parent.name.casefold().replace(" ", "")
    return any(name.startswith(token.casefold().replace(" ", "")) for token in tokens)
