"""Generate user-facing download links from pinned inventories without networking."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lab_control.deployment.catalog import load_manifest
from lab_control.deployment.materials import sources

ROOT = Path(__file__).resolve().parents[1]


def render() -> str:
    lines = [
        "# 依赖下载资源（自动生成）",
        "",
        "由 `scripts/generate_download_docs.py` 根据[组件清单](../../deployment/components.json)和"
        "[开发材料清单](../../deployment/packages/manifest.json)生成；勿手工修改。",
        "",
        "通常在首次运行或设置 → 环境与修复 → 准备设备组件中选择所需组件。程序按下列顺序直连下载，"
        "验证固定大小、SHA256 和适用的签名后才启用；不自动运行安装程序。没有已核实国内来源的厂商组件使用官方直连。"
        "材料已准备、组件已检测和设备验收分别显示；下载记录不能替代安装或设备验收。",
        "",
        "固定链接可能失效或被厂商更新；大小/哈希不符时拒绝使用，不自动改清单或升级版本。"
        "待补的接口驱动继续待核对，不提供猜测下载地址。",
        "",
    ]
    for c in load_manifest()["components"]:
        lines += [
            f"## {c['name']}",
            "",
            f"组件 ID：`{c['id']}`；版本：{c['version'] or '待核实'}。",
            "",
            c["purpose"],
            "",
        ]
        for i, s in enumerate(sources(c), 1):
            label = {"official-domestic": "官方国内站", "official-direct": "官方直连"}.get(
                s["kind"], s["kind"]
            )
            lines.append(f"{i}. [{label} — {c['filename']}]({s['url']})")
        if not sources(c):
            lines.append("下载材料：待登记。")
        lines += [
            "",
            f"[来源说明]({c['source_page']})" if c["source_page"] else "来源说明：待核实。",
            "",
            f"大小：{c.get('size', '待核实')} 字节；SHA256：`{c['sha256'] or '待核实'}`。",
            "",
            f"验证策略：`{c['signature'].get('policy', 'pending')}`；"
            f"分发依据：`{c['redistribution']['state']}`；"
            f"安装验证：`{c['installation']['state']}`；目标环境验证：`{c['validation']['state']}`。",
            "",
        ]
        if c["signature"].get("policy") == "official-sha256":
            lines += ["该官方归档未签名，仅按固定哈希及指定解压成员验证，不标记为签名通过。", ""]
    inventory = json.loads((ROOT / "deployment/packages/manifest.json").read_text(encoding="utf-8-sig"))
    python = next(f for f in inventory["files"] if f["path"].endswith("python-3.12.10-amd64.exe"))
    lines += [
        "## 开发环境材料",
        "",
        "冻结程序包含 Python/Qt；以下资源只供源码开发准备。",
        "",
        "- [清华 PyPI 镜像（默认）](https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/)：版本及哈希由 `uv.lock` 锁定。",
        "- [阿里云 PyPI 镜像（显式备用）](https://mirrors.aliyun.com/pypi/simple/)：使用 `setup-dev.ps1 -Mirror Aliyun`，无国外回退。",
        f"- [Python 3.12.10 x64 国内镜像]({python['source']})、[官方版本说明]({python['source_page']})："
        f"{python['size']} 字节，SHA256 `{python['sha256']}`，另核验 Python Software Foundation 签名。",
        "",
        "恢复开发 wheelhouse 使用 `scripts/prepare_dev_cache.py --output <目录>`；"
        "安装和离线构建见[部署指南](../development/desktop-deployment.md)。",
        "",
    ]
    for entry in inventory["files"]:
        if entry["kind"] == "developer-tool-installer":
            lines += [
                f"### {entry['name']} {entry['version']}",
                "",
                entry["purpose"],
                "",
                f"[国内镜像]({entry['source']})、[官方直连]({entry['official_source']})、"
                f"[版本说明]({entry['source_page']})。",
                "",
                f"大小：{entry['size']} 字节；SHA256：`{entry['sha256']}`。",
                "",
                f"签名：{entry['signature_status']}；发布者：{entry['publisher']}；"
                f"许可：{entry['license']}。",
                "",
                f"验证状态：{entry['validation']}。",
                "",
            ]
    lines += [
        "## 锁定的 Python 材料",
        "",
        "由 `scripts/generate_python_materials.py` 核对锁文件、Windows x64 wheel 与已安装版本后登记。"
        "哈希用于下载验证；目标电脑、局域网与实机验收仍分别留档。wheel 不适用 Authenticode 签名。",
        "",
        "| 包 / 版本 | 用途 | 国内来源 | 大小（字节） | SHA256 | 许可 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in inventory["files"]:
        if entry["kind"] == "python-wheel":
            license_name = entry.get("license", "待核实").replace("|", "/")
            lines.append(
                f"| {entry.get('name', Path(entry['path']).name)} {entry.get('version', '')} | "
                f"{entry.get('purpose', '待核实')} | [下载]({entry['source']}) | {entry['size']} | "
                f"`{entry['sha256']}` | {license_name} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "docs/generated/download-resources.md"
    content = render()
    if args.check:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            print("Download documentation is outdated")
            return 1
    else:
        path.write_text(content, encoding="utf-8", newline="\n")
    print("Download documentation checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
