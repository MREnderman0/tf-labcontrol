"""Check local Markdown links and UTF-8 text without opening external URLs."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    files = [ROOT / "README.md"]
    for folder in ("docs", "examples", "config/examples"):
        files.extend(sorted((ROOT / folder).rglob("*.md")))
    errors = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if "\ufffd" in text:
            errors.append(f"{path.relative_to(ROOT)}: replacement character in UTF-8 text")
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for match in re.finditer(r"\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)", text):
            target = match[1].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("#"):
                continue
            linked = path.parent / unquote(parsed.path)
            if not linked.exists():
                errors.append(f"{path.relative_to(ROOT)}: missing local link {target}")
    for error in errors:
        print(error)
    print(f"Checked {len(files)} UTF-8 Markdown files; {len(errors)} errors")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
