"""Offline Markdown links, headings, navigation and maintenance-source checks."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
COMMAND_TOKEN = re.compile(r""""[^"\n]*"|'[^'\n]*'|<[^>\n]*>|\S+""")


def command_lines(text: str):
    """Yield shell examples, including PowerShell continuations, without executing them."""
    fence = ""
    shell = False
    pending = ""
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            run, language = match.groups()
            if not fence:
                fence = run
                shell = language.strip().lower() in {
                    "",
                    "powershell",
                    "pwsh",
                    "ps1",
                    "shell",
                    "bash",
                    "bat",
                    "cmd",
                    "console",
                }
                continue
            if run[0] == fence[0] and len(run) >= len(fence) and not language.strip():
                if pending:
                    yield start, pending
                fence, pending = "", ""
                continue
        if not ((fence and shell) or (not fence and line.startswith("    "))):
            continue
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not pending:
            start = number
        continuation = line.endswith("`") or line.endswith(" \\")
        pending += " " + (line[:-1] if continuation else line)
        if not continuation:
            yield start, pending.strip()
            pending = ""


def declared_options(path: Path) -> tuple[dict[str, tuple | None], bool]:
    """Read literal argparse option names/choices. Dynamic parsers stay incomplete."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    options: dict[str, tuple | None] = {"-h": None, "--help": None}
    found, constructor, complete = False, False, True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "ArgumentParser") or (
            isinstance(node.func, ast.Name) and node.func.id == "ArgumentParser"
        ):
            constructor = True
            if any(keyword.arg == "parents" for keyword in node.keywords):
                complete = False
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "add_argument":
            found = True
            names = [
                arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if len(names) != len(node.args):
                complete = False
            choices = None
            for keyword in node.keywords:
                if keyword.arg == "choices":
                    try:
                        value = ast.literal_eval(keyword.value)
                        if isinstance(value, (list, tuple, set)):
                            choices = tuple(str(item) for item in value)
                    except (ValueError, TypeError):
                        pass
            for name in names:
                if name.startswith("-"):
                    options[name] = choices
        elif node.func.attr in {"parse_known_args", "parse_known_intermixed_args"}:
            complete = False
    # A factory imported from another module can add options not visible here.
    # Validate its visible literal choices, but never call unknown options invalid.
    return options, found and constructor and complete


def command_errors(root: Path, text: str) -> list[tuple[int, str]]:
    """Check clear project script/launcher examples by reading source AST only.

    This intentionally does not import scripts, invoke --help, infer subparser
    scope, evaluate shell variables or validate hardware/configuration values.
    """
    errors = []
    for number, line in command_lines(text):
        raw = COMMAND_TOKEN.findall(line)
        if raw and raw[0] == "&":
            raw.pop(0)
        tokens = [token.strip("\"'") for token in raw]
        if not tokens:
            continue
        executable = tokens.pop(0).replace("\\", "/").rsplit("/", 1)[-1].lower()
        source = None
        launcher = executable in {"run.bat", "lab-control", "lab-control.exe", "tf-labcontrol.exe"}
        if executable in {"lab-control-cli", "lab-control-cli.exe"}:
            source = root / "src/lab_control/cli/main.py"
        if (executable.startswith("run_") and executable.endswith(".bat")) or executable in {
            "lab-control-demo",
            "lab-control-demo.exe",
        }:
            errors.append((number, "obsolete launch entry; use run.bat or the formal launcher"))
            continue
        if executable in {"python", "python.exe", "python3", "python3.12"} and tokens:
            if tokens[:2] == ["-m", "lab_control"]:
                launcher, tokens = True, tokens[2:]
            elif tokens[:2] == ["-m", "lab_control.cli.main"]:
                source, tokens = root / "src/lab_control/cli/main.py", tokens[2:]
            elif tokens[:2] in (["-m", "lab_control.app"], ["-m", "lab_control.launcher"]):
                errors.append((number, "bypassed formal module entry; use python -m lab_control"))
                continue
            elif tokens[0].replace("\\", "/").lstrip("./") in {
                "src/lab_control/app.py",
                "src/lab_control/launcher.py",
            }:
                errors.append((number, "bypassed formal module entry; use python -m lab_control"))
                continue
            elif tokens[0].replace("\\", "/").lstrip("./").startswith(("scripts/", "examples/")):
                target = tokens.pop(0).replace("\\", "/")
                if any(char in target for char in "$<>%*"):
                    continue
                source = (root / target).resolve()
                if not source.is_relative_to(root) or not source.is_file():
                    errors.append((number, f"missing project command script {target}"))
                    continue
        if launcher:
            if executable == "run.bat" and not (root / "run.bat").is_file():
                errors.append((number, "missing formal launch script run.bat"))
                continue
            source = root / "src/lab_control/app.py"
            if tokens and tokens[0] == "--cli":
                source, tokens = root / "src/lab_control/cli/main.py", tokens[1:]
            elif tokens and tokens[0] in {"--csv-worker", "--deployment-check"}:
                # These non-argparse worker modes are dispatched by the launcher.
                continue
        if source is None:
            continue
        if not source.is_file():
            errors.append((number, f"missing command parser source {source.relative_to(root)}"))
            continue
        try:
            options, complete = declared_options(source)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            errors.append((number, f"cannot inspect command parser {source.relative_to(root)}: {exc}"))
            continue
        for index, token in enumerate(tokens):
            if token == "--" or token.startswith("#"):
                break
            option, equal, value = token.partition("=")
            if not re.match(r"^--?[a-zA-Z]", option):
                continue
            if option not in options:
                if complete:
                    errors.append((number, f"unknown command option {option} in {source.relative_to(root)}"))
                continue
            if not equal and index + 1 < len(tokens):
                value = tokens[index + 1]
            choices = options[option]
            if (
                choices
                and value
                and not value.startswith(("-", "$", "@", "<", "%", "["))
                and value not in choices
            ):
                errors.append((number, f"invalid literal choice {value} for {option}; expected {choices}"))
    return errors


def prose(text: str) -> str:
    # Fences can use backticks or tildes and can be longer than three characters.
    lines = []
    fence = ""
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            run = match[1]
            if not fence:
                fence = run
                continue
            if run[0] == fence[0] and len(run) >= len(fence) and not match[2].strip():
                fence = ""
                continue
        if not fence:
            lines.append(line)
    return "\n".join(lines)


def markdown_files(root: Path) -> list[Path]:
    """Include tracked and new non-ignored documents, never local caches/payloads."""
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=True,
        )
        if Path(top.stdout.decode("utf-8").strip()).resolve() == root:
            listed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    "*.md",
                ],
                capture_output=True,
                check=True,
            )
            return sorted(
                {
                    (root / name).resolve()
                    for name in listed.stdout.decode("utf-8").split("\0")
                    if name and (root / name).is_file()
                }
            )
    except (OSError, subprocess.CalledProcessError):
        pass
    # Source distributions and small test trees do not need Git installed.
    files = {root / name for name in ("README.md", "AGENTS.md", "direction-approved.md")}
    for folder in (
        "docs",
        "examples",
        "config/examples",
        "src/lab_control/instruments/devices",
        "deployment",
        "design",
    ):
        files.update((root / folder).rglob("*.md"))
    return sorted(path.resolve() for path in files if path.is_file())


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
    root = root.resolve()
    files = markdown_files(root)
    errors = []
    texts = {}
    for path in files:
        try:
            texts[path.resolve()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"{path.relative_to(root)}: invalid UTF-8 text")
    graph: dict[Path, set[Path]] = {path: set() for path in texts}
    for path, text in texts.items():
        label = path.relative_to(root)
        historical = "<!-- docs: historical -->" in text
        redirect = "<!-- docs: redirect -->" in text
        template = "<!-- docs: template -->" in text
        if path.name != "README.md" and {"reviews", "archive"}.intersection(label.parts) and not historical:
            errors.append(f"{label}: missing historical marker")
        if label.parts[:3] == ("docs", "testing", "templates") and path.name != "README.md" and not template:
            errors.append(f"{label}: missing template marker")
        if sum((historical, redirect, template)) > 1:
            errors.append(f"{label}: conflicting document markers")
        if not historical and not redirect:
            for number, line in enumerate(text.splitlines(), 1):
                if re.match(r"^\s*uv\s+(?:run|build|sync|pip|lock|export)\b", line):
                    errors.append(f"{label}:{number}: bare uv command; use the project environment")
            errors.extend(f"{label}:{number}: {error}" for number, error in command_errors(root, text))
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
    redirects = {path for path, text in texts.items() if "<!-- docs: redirect -->" in text}
    for path in redirects:
        targets = graph[path]
        label = path.relative_to(root)
        if len(targets) != 1:
            errors.append(f"{label}: redirect must link to one local Markdown target")
            continue
        target = next(iter(targets))
        if target == path or target in redirects:
            errors.append(f"{label}: redirect must target a canonical document, not a redirect")
            continue
        if not anchors(texts[path]).issubset(anchors(texts[target]) | {"文档已迁移"}):
            errors.append(f"{label}: preserved redirect anchors missing from target")
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
        "asset-status.md": ("当前结论", "接口验证矩阵", "功能覆盖表", "已知问题", "批次索引"),
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
        f"Checked {count} Markdown documents: links, anchors, navigation, sources, markers, commands "
        f"and templates; {len(errors)} errors"
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
