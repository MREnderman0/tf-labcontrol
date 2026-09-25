"""Explicit, offline-only hardware evidence import, checking and navigation."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lab_control.instruments.devices.catalog import DEVICE_MODULES
from lab_control.storage.hardware_records import atomic_json, digest, resource_protocol, validate


def converters():
    for package in DEVICE_MODULES:
        name = package + ".hardware.records"
        try:
            yield importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name not in {package + ".hardware", name}:
                raise


def manifest_names(root: Path) -> list[str]:
    manifest = json.loads((root / "overview/manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Invalid record manifest")
    names = manifest.get("summaries")
    if not isinstance(names, list) or any(
        not isinstance(name, str) or Path(name).name != name or not name.endswith(".json") for name in names
    ):
        raise ValueError("Invalid manifest summary path")
    return names


def read_batch(folder: Path, handlers) -> dict[str, Any]:
    source = folder / "results.json"
    issues = []
    source_hash = ""
    try:
        raw = source.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        report = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(report, dict):
            raise ValueError("results.json must be an object")
    except (OSError, ValueError) as exc:
        report = {}
        issues.append(str(exc))
    selected = [handler for handler in handlers if handler.recognizes(report, folder)]
    if len(selected) == 1:
        summary = selected[0].summarize(report)
    else:
        from lab_control.storage.hardware_records import normalize

        summary = normalize(report, adapter_id="", model="待核对", stage_names={})
        issues.append("缺少唯一的型号记录转换器；仅保留原始记录索引")
    summary["source_folder"] = str(folder.resolve())
    summary["batch"] = folder.name
    summary["asset_directory"] = folder.parent.name
    summary["evidence"] = {"path": str(source.resolve()), "sha256": source_hash}
    summary["issues"].extend(issues)
    if summary["issues"]:
        summary["completeness"] = "needs_review"
    return summary


def import_records(root: Path, sources: list[Path]) -> list[dict[str, Any]]:
    handlers = tuple(converters())
    summaries = []
    seen = set()
    for source_root in sources:
        for asset in sorted(source_root.iterdir()):
            if (
                not asset.is_dir()
                or asset.is_symlink()
                or asset.name.startswith(".")
                or asset.name == "overview"
            ):
                continue
            for batch in sorted(asset.iterdir()):
                if not batch.is_dir() or batch.is_symlink():
                    continue
                # Only timestamp batches, never backup/attachment subdirectories.
                if not batch.name[:4].isdigit():
                    continue
                physical = str(batch.resolve())
                if physical in seen:
                    continue
                seen.add(physical)
                summary = read_batch(batch, handlers)
                errors = validate(summary)
                summary["issues"].extend(errors)
                if errors:
                    summary["completeness"] = "needs_review"
                key = hashlib.sha256(physical.encode("utf-8")).hexdigest()
                atomic_json(root / "overview" / "summaries" / (key + ".json"), summary)
                summaries.append(summary)
    manifest = {
        "schema_version": 1,
        "sources": [str(p) for p in sources],
        "summaries": [hashlib.sha256(s["source_folder"].encode()).hexdigest() + ".json" for s in summaries],
    }
    atomic_json(root / "overview" / "manifest.json", manifest)
    return summaries


def cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def link(path: Path, parent: Path, label: str) -> str:
    try:
        target = Path(os.path.relpath(path, parent)).as_posix()
    except ValueError:
        target = path.as_uri()
    return f"[{cell(label)}](<{quote(target, safe='/.:_-')}>)"


def build_index(root: Path, summaries: list[dict[str, Any]]) -> None:
    out = root / "overview"
    out.mkdir(parents=True, exist_ok=True)
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for summary in summaries:
        identity = summary["identity"]
        key: tuple[str, ...] = (
            (identity["manufacturer"], identity["model"], identity["serial"])
            if summary["identity_verified"]
            else (summary["source_folder"],)
        )
        groups.setdefault(key, []).append(summary)
    lines = [
        "# 实机证据总览（离线生成）",
        "",
        "按资产、接口、项目和固件分别列证据。通过仅限记录中的测试条件；未测试不覆盖较早已执行项。后续恢复不修改原失败批次。本页不表示设备当前在线或输出状态。",
        "",
        "[待核对记录](needs-review.md) · " + link(root / "index.md", out, "历史原始台账"),
        "",
    ]
    review = ["# 待核对记录", "", "缺项不等于设备失败；未凭目录名、地址或文字猜测通过。", ""]
    for key, rows in sorted(groups.items()):
        rows.sort(key=lambda s: (s["started_at"] or s["batch"], s["source_folder"]))
        latest = rows[-1]
        title = (
            " / ".join(key) if latest["identity_verified"] else latest["asset_directory"] + "（身份未核验）"
        )
        lines += ["## " + cell(title), ""]
        notes = sorted({Path(s["source_folder"]).parent / "connection-status.md" for s in rows})
        lines += [
            "连接及人工说明：" + " · ".join(link(p, out, p.parent.name) for p in notes if p.exists()),
            "",
        ]
        # Keep every executed event, grouped by interface + item + firmware, not one success badge.
        events: dict[tuple[str, str, str, str], list[tuple[dict, dict]]] = {}
        for summary in rows:
            for item in summary["items"]:
                group = (
                    summary["interface"],
                    item["key"],
                    summary["identity"]["firmware"] or "未知",
                    resource_protocol(summary.get("resource", "")),
                )
                events.setdefault(group, []).append((summary, item))
        lines += [
            "| 接口 | 项目 | 固件 | 最近已执行结果 | 时间 / 条件 / 原始记录 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for (interface, item_key, firmware, protocol), history in sorted(events.items()):
            executed = [pair for pair in history if pair[1]["status"] != "not_tested"]
            summary, item = (executed or history)[-1]
            folder = Path(summary["source_folder"])
            evidence = folder / "record.md" if (folder / "record.md").exists() else folder / "results.json"
            label = summary["started_at"] or summary["batch"]
            lines.append(
                f"| {cell(interface)} | {cell(item_key)} | {cell(firmware)} | {item['status']} | {cell(protocol)}；{link(evidence, out, label)}；{cell(summary['scope']) or '条件待核对'} |"
            )
        lines += ["", "### 全部批次（保留失败、受阻及后续恢复）", ""]
        for summary in rows:
            folder = Path(summary["source_folder"])
            evidence = folder / "record.md" if (folder / "record.md").exists() else folder
            states = ", ".join(
                f"{i['key']}={i['status']}" for i in summary["items"] if i["status"] != "not_tested"
            )
            lines.append(
                f"- {link(evidence, out, summary['batch'])}：{cell(states) or '未执行/待核对'}；{summary['completeness']}"
            )
            if summary["issues"]:
                review.append(
                    f"- {link(evidence, out, summary['asset_directory'] + ' / ' + summary['batch'])}："
                    + "；".join(cell(x) for x in summary["issues"])
                )
        lines.append("")
    for name, contents in (("README.md", lines), ("needs-review.md", review)):
        target = out / name
        text = "\n".join(contents) + "\n"
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            temporary = target.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(target)


def check_records(root: Path) -> list[str]:
    errors: list[str] = []
    try:
        names = manifest_names(root)
    except (OSError, ValueError, TypeError) as exc:
        return [str(exc)]
    for name in names:
        path = root / "overview/summaries" / name
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            errors.extend(f"{name}: {message}" for message in validate(summary))
            evidence = summary["evidence"]
            source = Path(evidence["path"])
            if evidence["sha256"] and (not source.exists() or digest(source) != evidence["sha256"]):
                errors.append(f"Source changed since import: {source}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{name}: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("import", "index", "check", "structure"))
    parser.add_argument("--root", type=Path, required=True, help="Explicit hardware-tests directory")
    parser.add_argument(
        "--source-root",
        type=Path,
        action="append",
        default=[],
        help="Additional historical root; originals remain untouched",
    )
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    sources = list(dict.fromkeys([root, *(p.resolve(strict=True) for p in args.source_root)]))
    print(
        json.dumps({"output_root": str(root), "source_roots": [str(p) for p in sources]}, ensure_ascii=False)
    )
    if args.command == "structure":
        from lab_control.storage.hardware_documents import update_documents

        summaries = import_records(root, sources)
        build_index(root, summaries)
        update_documents(root)
        return int(bool(check_records(root)))
    if args.command == "import":
        summaries = import_records(root, sources)
        build_index(root, summaries)
        print(f"Imported {len(summaries)} batches; needs review: {sum(bool(s['issues']) for s in summaries)}")
    elif args.command == "index":
        errors = check_records(root)
        if errors:
            for error in errors:
                print(error)
            return 1
        summaries = [
            json.loads((root / "overview/summaries" / name).read_text(encoding="utf-8"))
            for name in manifest_names(root)
        ]
        build_index(root, summaries)
    errors = check_records(root)
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
