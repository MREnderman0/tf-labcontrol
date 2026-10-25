"""Generate capability inventory and model implementation sections without opening hardware.

Run with the project Python: scripts/generate_device_docs.py [--check]
All model notes and protocol metadata live in their packages.
Commands and capabilities always come from the registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from lab_control.application.config import ConnectionConfig
from lab_control.application.controls import CATEGORY_LABELS, MEASUREMENT_CATEGORIES, OPERATION_LABELS
from lab_control.domain.instruments import jsonable
from lab_control.instruments.registry import DEVICE_DEFINITIONS, PENDING, describe_definition

ROOT = Path(__file__).resolve().parents[1]
MODEL_CARD_MARKER = "<!-- Generated from an independent device package; edit its README.md. -->"
LEGACY_CARD_MARKER = "<!-- Generated from scripts/generate_device_docs.py legacy metadata. -->"

CATEGORY_FOLDERS = {
    "signal-sources": "信号源",
    "power-supplies": "电源",
    "oscilloscopes": "示波器",
    "spectrum-analyzers": "频谱仪",
    "measurement-analyzers": "测量与分析仪",
    "temperature-load-drivers": "温控、负载与驱动",
}


def render(definitions=None) -> dict[Path, str]:
    definitions = DEVICE_DEFINITIONS if definitions is None else definitions
    models = [
        (
            d.adapter_id,
            d.documentation.card,
            d.documentation.title,
            d.documentation.connection,
            d.documentation.driver,
            "",
        )
        for d in definitions.values()
    ]
    for field, label in ((0, "adapter ID"), (1, "card path")):
        values = [row[field] for row in models]
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate documentation {label}")
    for definition in definitions.values():
        for relative in ("README.md", "hardware/README.md"):
            if not (definition.root / relative).is_file():
                raise ValueError(f"Missing model documentation source: {definition.root / relative}")
    rows = []
    files: dict[Path, str] = {}
    for adapter, card, title, connection, driver, note in models:
        config = ConnectionConfig("metadata://only", adapter)
        definition = definitions[adapter]
        profile = definition.profile
        caps = describe_definition(definition, config)
        monitoring_page = caps.category in MEASUREMENT_CATEGORIES or (
            caps.category == "power_supply" and not definition.management_surface
        )
        page = ("实时监测" if monitoring_page else "设备管理") + " / " + CATEGORY_LABELS[caps.category]
        fixture = definition.root / "tests/fixtures/control.json"
        fixture_op = json.loads(fixture.read_text(encoding="utf-8"))["operation"] if fixture.exists() else ""
        record = {
            "adapter_id": adapter,
            "model": title,
            "category": caps.category,
            "control_page": page,
            "channels": caps.channels,
            "source": profile.source if profile else definition.model.source,
            "revision": profile.revision if profile else definition.model.revision,
            "acquisition": profile.acquisition if profile else definition.model.acquisition,
            "channel_limits": profile.channel_limits if profile else {},
            "hardware_passed": False,
            "operations": [],
        }
        operations = []
        for cap in caps.operations:
            entry = jsonable(cap)
            entry.update(
                {
                    "implementation": "implemented",
                    "schema_tested": bool(profile),
                    "protocol_fixture_tested": cap.name == fixture_op,
                    "hardware_passed": False,
                    "ui_label": OPERATION_LABELS[cap.name],
                    # Protocol capabilities do not describe actual GUI controls.
                    # Retain the legacy string field without inventing an entry.
                    "ui_location": "",
                }
            )
            if profile:
                spec = profile.commands[cap.name]
                entry["wire_templates"] = {
                    "select": spec.select,
                    "writes": spec.writes,
                    "query": spec.query,
                    "custom_encoder": cap.writable and not bool(spec.writes),
                }
            record["operations"].append(entry)
            parameters = (
                "; ".join(
                    f"{name}: {p.kind} {p.unit} "
                    + ("/".join(p.choices) if p.choices else f"[{p.minimum}, {p.maximum}]")
                    for name, p in cap.parameters.items()
                )
                or "无"
            )
            state = "缺选件/配置" if not cap.available else "软件已实现；实测按资产查证"
            operations.append(
                f"| `{cap.name}` | {parameters} | {'通道' if cap.channel_scoped else '整机'} | {'读' if cap.readable else ''}{'/写' if cap.writable else ''} | {'需要' if cap.requires_confirmation else '无需'} | {state} |"
            )
        rows.append(record)
        card_path = ROOT / f"docs/devices/{card}.md"
        readme = definition.root / "README.md"
        body = readme.read_text(encoding="utf-8")

        def rebase_link(match):
            label, target = match.groups()
            if "://" in target or target.startswith("#"):
                return match.group(0)
            resolved = readme.parent / target
            relative = Path(os.path.relpath(resolved, card_path.parent)).as_posix()
            return f"[{label}]({relative})"

        body = re.sub(r"\[([^\]]+)\]\(([^\s)]+)\)", rebase_link, body)
        files[card_path] = (
            MODEL_CARD_MARKER
            + "\n\n维护源：[包内 README]("
            + Path(os.path.relpath(readme, card_path.parent)).as_posix()
            + ")。修改维护源后重新生成。\n\n"
            + body.rstrip()
            + "\n\n## 注册能力（自动生成）\n\n"
            + "范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。\n\n"
            + "| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |\n"
            + "| --- | --- | --- | --- | --- | --- |\n"
            + "\n".join(operations)
            + "\n"
        )
    matrix = {
        "schema_version": 1,
        "hardware_tested": False,
        "hardware_evidence_scope": "asset/interface/firmware/test-case records in runtime directory",
        "legacy_hardware_boolean_note": "Compatibility fields only; false is not an asset verdict.",
        "ui_location_note": (
            "control_page identifies the model page only. ui_location is a legacy empty field: "
            "operation-level GUI entries are not inferred from protocol capabilities. See the current user guide."
        ),
        "models": rows,
        "pending": PENDING,
    }
    files[ROOT / "docs/generated/capability-matrix.json"] = (
        json.dumps(jsonable(matrix), ensure_ascii=False, indent=2) + "\n"
    )
    lines = [
        "# 实际能力矩阵（自动生成）",
        "",
        "由 `scripts/generate_device_docs.py` 从实际注册表生成。下面的操作均有实现；单项参数、命令定位与软件测试标记见 [JSON](capability-matrix.json)。未列出的功能返回 `unsupported`；选件或 TSERVE 配置缺失会禁用相应能力。具体资产的实机证据保存在运行目录，不将个别资产的验证结果推广到整个型号。",
        "",
        "软件测试分为通用参数约束、特定协议夹具、SDK 生命周期和应用集成；协议夹具通过不代表目标固件通过，也不代表所有高级组合已经验证。",
        "",
        "JSON 保留 hardware_tested/hardware_passed 兼容字段；固定 false 不代表现场设备失败或全部未测。资产证据见[离线台账说明](../testing/records.md)。",
        "",
        "JSON 的 control_page 表示型号所在页面，不保证每项底层能力都有 GUI 控件。ui_location 作为兼容字段保留空值；实际操作入口以[使用指南](../guides/control-and-monitoring.md)为准，不从协议能力推断常用、高级或诊断按钮。",
        "",
        "| 型号 | 注册 ID | 操作数 | 采集 | 详情 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record, meta in zip(rows, models, strict=True):
        lines.append(
            f"| {record['model']} | `{record['adapter_id']}` | {len(record['operations'])} | {record['acquisition'] or '外部仪器测量输出'} | [型号卡片](../devices/{meta[1]}.md) |"
        )
    lines += [
        "",
        "## 未实现 / 待补项",
        "",
        "通用协议错误处理与已注册功能可进入实测。下列功能尚未作为真实能力开放，调用返回 `unsupported`，不能仅靠配置开启：",
        "",
        "- 函数发生器复杂序列编辑；SMA100B 未注册的选件专用功能（频率、扫频边界和功率范围已按实机查询适配）。",
        "- 电源斜率、触发链以及未核实型号的远端采样/列表（SPD4306X 已实现列表与 CH2/3 远端采样）。",
        "- 示波器分段逐段时间戳、多帧/数学波形导出；Pico 无间隙无限 streaming。",
        "- 频谱仪前置放大与带宽/信道功率应用；网络分析仪分段表写入及完整校准向导。",
        "- PM100D 校准证书导出；未知厂家设备真实协议。",
        "",
        "这些是后续能力扩展任务，不标记为硬件不具备。",
        "",
        "| 原始条目 | 状态 |",
        "| --- | --- |",
    ]
    lines += [f"| {name} | 待补信息：{reason} |" for name, reason in PENDING.items()]
    files[ROOT / "docs/generated/capability-matrix.md"] = "\n".join(lines) + "\n"
    cards = {
        f"docs/devices/{d.documentation.card}.md": hashlib.sha256(
            files[ROOT / f"docs/devices/{d.documentation.card}.md"].encode("utf-8")
        ).hexdigest()
        for d in definitions.values()
    }
    files[ROOT / "docs/generated/device-module-docs.json"] = json.dumps(cards, indent=2) + "\n"
    folders = dict(CATEGORY_FOLDERS)
    for _adapter, card, *_rest in models:
        folders.setdefault(str(Path(card).parent), str(Path(card).parent))
    device_navigation = [
        "# 设备型号导航（自动生成）",
        "",
        "全部已注册型号的维护源在各自包内；未注册条目仅作为资料需求。实测结果按具体资产、接口和固件查看。",
        "",
        "[原始设备条目对应](../device-research-index.md) · [新设备接入流程](../testing/onboarding.md) · [能力矩阵](../generated/capability-matrix.md)",
        "",
    ]
    for folder, title in folders.items():
        navigation = [
            f"# {title}（自动生成）",
            "",
            "软件实现与资产实测分别判定；维护型号包后重新生成本导航。",
            "",
            "| 型号 | 接入路线 | 文档 |",
            "| --- | --- | --- |",
        ]
        owned = set()
        for _adapter, card, model_title, connection, _driver, _note in models:
            if str(Path(card).parent) == folder:
                owned.add(Path(card).name + ".md")
                navigation.append(f"| {model_title} | {connection} | [型号说明]({Path(card).name}.md) |")
        # Unregistered requirement cards are human-maintained; never delete or manufacture an adapter.
        for pending in sorted((ROOT / "docs/devices" / folder).glob("*.md")):
            text = pending.read_text(encoding="utf-8")
            if (
                pending.name == "README.md"
                or pending.name in owned
                or text.startswith((MODEL_CARD_MARKER, LEGACY_CARD_MARKER))
            ):
                continue
            name = next(
                (line.lstrip("# ") for line in text.splitlines() if line.startswith("# ")), pending.stem
            )
            navigation.append(f"| {name} | 未注册，待补资料 | [待补卡片]({pending.name}) |")
        files[ROOT / "docs/devices" / folder / "README.md"] = "\n".join(navigation) + "\n"
        device_navigation.append(f"- [{title}]({folder}/README.md)")
    files[ROOT / "docs/devices/README.md"] = "\n".join(device_navigation) + "\n"
    index_path = ROOT / "docs/device-research-index.md"
    index = index_path.read_text(encoding="utf-8")
    index_rows = [
        "| 型号 | 注册 ID | 连接路线 | 归档 |",
        "| --- | --- | --- | --- |",
        *(
            f"| {d.documentation.title} | `{d.adapter_id}` | {d.documentation.connection} | "
            f"[卡片](devices/{d.documentation.card}.md) |"
            for d in definitions.values()
        ),
    ]
    index, count = re.subn(
        r"<!-- device-modules:start -->.*?<!-- device-modules:end -->",
        "<!-- device-modules:start -->\n" + "\n".join(index_rows) + "\n<!-- device-modules:end -->",
        index,
        flags=re.DOTALL,
    )
    if count != 1:
        raise ValueError("Device research index requires one generated module section")
    files[index_path] = index
    return files


def obsolete_cards(files: dict[Path, str]) -> list[Path]:
    """Only remove our recorded, unmodified generated cards under docs/devices."""
    manifest = ROOT / "docs/generated/device-module-docs.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    current = json.loads(files[manifest])
    stale = []
    for name in previous.keys() - current.keys():
        path = (ROOT / name).resolve()
        if not path.is_relative_to((ROOT / "docs/devices").resolve()) or path.suffix != ".md":
            raise ValueError(f"Unsafe generated document path: {name}")
        if path.exists():
            contents = path.read_text(encoding="utf-8")
            if (
                not contents.startswith(MODEL_CARD_MARKER)
                or hashlib.sha256(contents.encode("utf-8")).hexdigest() != previous[name]
            ):
                raise ValueError(f"Removed model card was manually modified; preserve it: {name}")
            stale.append(path)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = render()
    stale = obsolete_cards(files)
    changed = []
    for path in stale:
        changed.append(str(path.relative_to(ROOT)))
        if not args.check:
            path.unlink()
    for path, content in files.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
    if args.check and changed:
        print("Outdated generated documents: " + ", ".join(changed))
        return 1
    print(f"Generated documentation checked ({len(changed)} changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
