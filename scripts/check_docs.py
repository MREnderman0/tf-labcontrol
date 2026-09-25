"""Offline Markdown links, headings, navigation and maintenance-source checks."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")


def prose(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.S)


def anchors(text: str) -> set[str]:
    result: set[str] = set()
    seen: dict[str, int] = {}
    for title in re.findall(r"^#{1,6} +(.+?) *#*$", prose(text), re.M):
        title = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", title)
        slug = re.sub(r"[^\w\-\s]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug + (f"-{count}" if count else ""))
    result.update(re.findall(r'(?:id|name)=["\']([^"\']+)', text))
    return result


def check(root: Path) -> tuple[int, list[str]]:
    files = [
        root / name for name in ("README.md", "AGENTS.md", "direction-approved.md") if (root / name).exists()
    ]
    for folder in ("docs", "examples", "config/examples", "src/lab_control/instruments/devices"):
        files.extend(sorted((root / folder).rglob("*.md")))
    texts = {path.resolve(): path.read_text(encoding="utf-8") for path in files}
    graph: dict[Path, set[Path]] = {path: set() for path in texts}
    errors = []
    for path, text in texts.items():
        label = path.relative_to(root)
        if "\ufffd" in text:
            errors.append(f"{label}: replacement character in UTF-8 text")
        for match in LINK.finditer(prose(text)):
            target = match[1].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme:
                continue
            linked = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            if linked.is_dir() and (linked / "README.md").exists():
                linked /= "README.md"
            if not linked.exists():
                errors.append(f"{label}: missing local link {target}")
                continue
            if linked in texts:
                graph[path].add(linked)
                if parsed.fragment and unquote(parsed.fragment) not in anchors(texts[linked]):
                    errors.append(f"{label}: missing heading anchor {target}")
    visited: set[Path] = set()
    todo = [(root / "README.md").resolve()]
    while todo:
        current = todo.pop()
        if current in visited:
            continue
        visited.add(current)
        todo.extend(graph.get(current, ()))
    for path in texts.keys() - visited:
        if "<!-- docs: redirect -->" not in texts[path]:
            errors.append(f"{path.relative_to(root)}: unreachable from README navigation")
    templates = {
        "model-readme.md": (
            "能力与协议",
            "连接与驱动",
            "单位与限制",
            "文件与公共依赖",
            "测试入口与实机状态",
            "扩展与移除",
        ),
        "hardware-plan.md": ("目标与授权范围", "前置条件与备份", "测试矩阵", "停止与恢复", "交付与未测"),
        "record.md": ("资产与环境", "范围与证据", "恢复与最终状态", "未测及后续"),
    }
    for name, headings in templates.items():
        path = root / "docs/testing/templates" / name
        if not path.exists():
            errors.append(f"Missing template: {name}")
            continue
        text = texts.get(path.resolve(), "")
        for heading in headings:
            if "## " + heading not in text:
                errors.append(f"{name}: missing template section {heading}")
    return len(files), errors


def main() -> int:
    count, errors = check(ROOT)
    from lab_control.instruments.devices.catalog import load_definitions

    try:
        definitions = load_definitions()
        cards = [d.documentation.card for d in definitions.values()]
        if len(cards) != len(set(cards)):
            errors.append("Duplicate registered model card")
        for definition in definitions.values():
            for relative in ("README.md", "hardware/README.md", "hardware/records.py"):
                if not (definition.root / relative).is_file():
                    errors.append(f"Missing model maintenance source: {definition.root / relative}")
    except (ValueError, TypeError, ImportError) as exc:
        errors.append(str(exc))
    for error in errors:
        print(error)
    print(
        f"Checked {count} Markdown documents: links, anchors, navigation, sources and templates; {len(errors)} errors"
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
