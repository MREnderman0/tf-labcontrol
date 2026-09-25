"""Structured, offline current views over versioned summaries; raw evidence is immutable."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lab_control.storage.hardware_records import resource_protocol

MARKER = "<!-- hardware-documents: structured-v1 -->"
COMMON = frozenset(
    {
        "enumeration",
        "scan",
        "identity",
        "connection",
        "compatibility_connection",
        "parameter_read",
        "restoration",
        "reconnection",
        "stable_reads",
        "default_connection",
    }
)


def cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def link(path: Path, parent: Path, label: str) -> str:
    try:
        target = Path(os.path.relpath(path, parent)).as_posix()
    except ValueError:
        target = path.as_uri()
    return f"[{cell(label)}](<{quote(target, safe='/.:_-')}>)"


def write_text(path: Path, text: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8-sig") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def preserve_manual(path: Path) -> list[Path]:
    """Retain bytes and relative links in the same directory before replacing a current view."""
    if path.exists() and not path.read_text(encoding="utf-8-sig").startswith(MARKER):
        data = path.read_bytes()
        backup = path.with_name(
            path.stem + "-before-structured-" + hashlib.sha256(data).hexdigest()[:12] + ".md"
        )
        if not backup.exists():
            backup.write_bytes(data)
    return sorted(path.parent.glob(path.stem + "-before-structured-*.md"))


def evidence(summary: dict[str, Any]) -> Path:
    folder = Path(summary["source_folder"])
    for name in ("record.md", "results.json"):
        if (folder / name).exists():
            return folder / name
    return folder


def collect(root: Path) -> list[dict[str, Any]]:
    """Read summaries only. Model conversion remains with each model's offline converter."""
    rows: dict[str, dict[str, Any]] = {}
    manifest = root / "overview/manifest.json"
    if manifest.exists():
        names = json.loads(manifest.read_text(encoding="utf-8"))["summaries"]
        for name in names:
            if Path(name).name != name or not name.endswith(".json"):
                raise ValueError("Invalid summary manifest path")
            item = json.loads((root / "overview/summaries" / name).read_text(encoding="utf-8"))
            rows[item["source_folder"]] = item
    for path in root.glob("*/*/summary.json"):
        if not path.parent.name[:4].isdigit():
            continue
        item = json.loads(path.read_text(encoding="utf-8"))
        item.update(
            source_folder=str(path.parent.resolve()),
            batch=path.parent.name,
            asset_directory=path.parent.parent.name,
        )
        # Imported summaries may reflect more recent converter fixes. Prefer them
        # until the raw batch has actually changed.
        old = rows.get(item["source_folder"])
        if not old or old.get("evidence", {}).get("sha256") != item.get("evidence", {}).get("sha256"):
            rows[item["source_folder"]] = item
    return sorted(rows.values(), key=lambda s: (s.get("started_at", ""), s["source_folder"]))


def render_asset(rows: list[dict[str, Any]], destination: Path, preference: dict[str, Any]) -> str:
    latest = rows[-1]
    identity = latest["identity"]
    lines = [
        MARKER,
        f"# {cell(identity['model'])} / {cell(identity['serial'])} 测试状态",
        "",
        "## 当前结论",
        "",
        "历史证据不代表设备当前在线或输出状态；控制读回不等于物理输出验收。",
        "",
        "| 项目 | 当前记录 |",
        "| --- | --- |",
        f"| 默认连接 | {cell(preference.get('resource', '未核对；参见保留的人工说明'))} |",
        f"| 最近批次 | {link(evidence(latest), destination, latest.get('started_at') or latest['batch'])} |",
        f"| 本批范围 | {cell(latest.get('scope', '待核对'))} |",
        "",
    ]
    manual = preserve_manual(destination / "connection-status.md")
    original_notes = {Path(s["source_folder"]).parent / "connection-status.md" for s in rows}
    manual += sorted(p for p in original_notes if p.parent != destination and p.exists())
    if manual:
        lines += ["保留说明：" + " · ".join(link(p, destination, p.name) for p in manual), ""]
    events: dict[tuple[str, str, str, str], list[tuple[dict, dict]]] = {}
    for summary in rows:
        for item in summary["items"]:
            event_key = (
                summary["interface"],
                item["key"],
                summary["identity"].get("firmware") or "未知",
                resource_protocol(summary.get("resource", "")),
            )
            events.setdefault(event_key, []).append((summary, item))
    selected = []
    for event_key, history in sorted(events.items()):
        executed = [pair for pair in history if pair[1]["status"] != "not_tested"]
        selected.append((event_key, *(executed or history)[-1]))
    for title, common in (("接口验证矩阵", True), ("功能覆盖表", False)):
        lines += [
            "## " + title,
            "",
            "| 接口 | 项目 | 固件 | 最近已执行结果 | 条件与证据 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for (interface, key, firmware, protocol), summary, item in selected:
            if (key in COMMON) != common:
                continue
            lines.append(
                f"| {cell(interface)} | {cell(key)} | {cell(firmware)} | {item['status']} | "
                f"{cell(protocol)}；{cell(summary.get('scope', ''))}；{link(evidence(summary), destination, summary['batch'])} |"
            )
        lines.append("")
    lines += ["## 已知问题", "", "| 项目 | 状态／说明 | 证据 |", "| --- | --- | --- |"]
    for (interface, key, firmware, protocol), summary, item in selected:
        if item["status"] != "passed":
            lines.append(
                f"| {cell(interface + ' / ' + protocol + ' / ' + key + ' / ' + firmware)} | "
                f"{item['status']}：{cell(item.get('reason') or item.get('raw_result'))} | "
                f"{link(evidence(summary), destination, summary['batch'])} |"
            )
    for summary in rows:
        notes = [
            *summary.get("issues", []),
            *[f"{k}: {v}" for k, v in summary.get("diagnostics", {}).items() if v],
        ]
        if notes:
            lines.append(
                f"| 历史诊断（不代表当前故障） | {cell('；'.join(map(str, notes)))} | {link(evidence(summary), destination, summary['batch'])} |"
            )
    lines += ["", "## 批次索引", "", "| 时间 | 接口 | 分项结果 | 记录 |", "| --- | --- | --- | --- |"]
    for summary in rows:
        states = "; ".join(f"{i['key']}={i['status']}" for i in summary["items"])
        lines.append(
            f"| {cell(summary.get('started_at', ''))} | {cell(summary['interface'])} | {cell(states)} | {link(evidence(summary), destination, summary['batch'])} |"
        )
    return "\n".join(lines) + "\n"


def update_documents(root: Path) -> None:
    """Serialize current-view updates across processes, then reread all available summaries."""
    from lab_control.application.leases import FileLease
    from lab_control.domain.instruments import DeviceError

    root = root.resolve()
    lease = None
    for attempt in range(50):
        try:
            lease = FileLease(root / "overview/documents.lock")
            break
        except DeviceError:
            if attempt == 49:
                raise
            time.sleep(0.1)
    try:
        rows = collect(root)
        preferences_path = root / "connection-preferences.json"
        preferences = (
            json.loads(preferences_path.read_text(encoding="utf-8")) if preferences_path.exists() else {}
        )
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for item in rows:
            if item.get("identity_verified"):
                identity = item["identity"]
                groups.setdefault(tuple(identity[k] for k in ("manufacturer", "model", "serial")), []).append(
                    item
                )
        index = [MARKER, "# 实机测试台账", "", "[按设备、接口及功能查看](overview/README.md)", ""]
        old_indexes = preserve_manual(root / "index.md")
        if old_indexes:
            index += ["保留原台账：" + " · ".join(link(p, root, p.name) for p in old_indexes), ""]
        index += ["| 时间 | 资产 | 接口 | 结果 | 批次 |", "| --- | --- | --- | --- | --- |"]
        for item in rows:
            states = "; ".join(
                f"{i['key']}={i['status']}" for i in item["items"] if i["status"] != "not_tested"
            )
            index.append(
                f"| {cell(item.get('started_at', ''))} | {cell(item['asset_directory'])} | {cell(item['interface'])} | {cell(states) or '待核对'} | {link(evidence(item), root, item['batch'])} |"
            )
        write_text(root / "index.md", "\n".join(index) + "\n")
        for group in groups.values():
            local = [s for s in group if Path(s["source_folder"]).parent.parent == root]
            chosen = (local or group)[-1]
            name = chosen["asset_directory"]
            if Path(name).name != name or name in {".", ".."}:
                raise ValueError("Invalid asset directory")
            destination = root / name
            destination.mkdir(exist_ok=True)
            for alias in {Path(s["source_folder"]).parent for s in local} - {destination}:
                path = alias / "connection-status.md"
                previous_notes = preserve_manual(path)
                redirect = [
                    MARKER,
                    "# 资产现行说明",
                    "",
                    link(destination / "connection-status.md", alias, "查看统一结构化测试状态"),
                    "",
                ]
                if previous_notes:
                    redirect += [
                        "保留人工说明：" + " · ".join(link(p, alias, p.name) for p in previous_notes),
                        "",
                    ]
                write_text(path, "\n".join(redirect))
            key = "|".join(group[-1]["identity"][k] for k in ("manufacturer", "model", "serial"))
            write_text(
                destination / "connection-status.md",
                render_asset(group, destination, preferences.get(key, {})),
            )
    finally:
        if lease:
            lease.close()


def refresh_after_batch(folder: Path) -> None:
    """Documentation errors must not interrupt hardware cleanup or replace test results."""
    from lab_control.storage.hardware_records import atomic_json

    try:
        update_documents(folder.parent.parent)
        (folder / "documents-error.json").unlink(missing_ok=True)
    except Exception as exc:
        try:
            atomic_json(folder / "documents-error.json", {"error": str(exc)})
        except Exception:
            logging.getLogger(__name__).exception("Cannot persist hardware documentation error: %s", exc)
