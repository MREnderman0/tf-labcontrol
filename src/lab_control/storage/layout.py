"""Versioned recording locations, shared by writers and offline readers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def raw_folder(folder: Path) -> Path:
    return folder / "_raw" if (folder / "_raw" / "recording.json").is_file() else folder


def manifest(folder: Path) -> dict[str, Any]:
    path = raw_folder(folder) / "recording.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("layout_version", 1) not in (1, 2):
        raise ValueError("Unsupported recording layout version")
    return value


def atomic_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding=encoding, newline="") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
