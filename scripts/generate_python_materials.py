"""Refresh Windows x64 wheel material metadata from the lock and verified environment."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from build_desktop import verified_packages
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]


def generate() -> dict:
    path = ROOT / "deployment/packages/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = project["project"]["dependencies"] + [
        d for group in project["dependency-groups"].values() for d in group if isinstance(d, str)
    ]
    roots = [re.split(r"[<>=!~\[; ]", value)[0] for value in declared]
    installed = verified_packages(lock, roots)
    packages = {(p["name"], p["version"]): p for p in lock["package"]}
    supported = {tag: rank for rank, tag in enumerate(sys_tags())}
    rows = []
    runtime_roots = json.loads((ROOT / "deployment/components.json").read_text(encoding="utf-8"))[
        "python_roots"
    ]
    runtime = {p["name"] for p in verified_packages(lock, runtime_roots)}
    for package in installed:
        name, version = package["name"], package["version"]
        choices = []
        for wheel in packages[(name, version)].get("wheels", []):
            filename = unquote(Path(urlsplit(wheel["url"]).path).name)
            tags = parse_wheel_filename(filename)[3]
            ranks = [supported[tag] for tag in tags if tag in supported]
            if ranks:
                choices.append((min(ranks), filename, wheel))
        if not choices:
            raise ValueError(f"No locked Windows x64 wheel for {name} {version}")
        _rank, filename, wheel = min(choices, key=lambda item: (item[0], item[1]))
        if not wheel["url"].startswith("https://mirrors.tuna.tsinghua.edu.cn/"):
            raise ValueError("Wheel URL is outside the configured domestic mirror")
        metadata = importlib.metadata.metadata(name)
        license_text = metadata.get("License-Expression") or metadata.get("License") or "pending"
        rows.append(
            {
                "path": "deployment/packages/python/" + filename,
                "kind": "python-wheel",
                "name": name,
                "version": version,
                "size": wheel["size"],
                "sha256": wheel["hash"].removeprefix("sha256:"),
                "source": wheel["url"],
                "source_page": f"https://pypi.org/project/{name}/{version}/",
                "purpose": "desktop runtime and Agent integration"
                if name in runtime
                else "development/build",
                "signature_status": "not_applicable: wheel verified by lock SHA256",
                "license": license_text.splitlines()[0][:180],
                "validation": "installed version verified; wheel size/hash from uv.lock; clean-machine acceptance pending",
            }
        )
    manifest["files"] = [f for f in manifest["files"] if f["kind"] != "python-wheel"] + rows
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "deployment/packages/manifest.json"
    value = generate()
    if args.check:
        if json.loads(path.read_text(encoding="utf-8-sig")) != value:
            raise SystemExit("Python material inventory is outdated")
    else:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Python material inventory checked")


if __name__ == "__main__":
    main()
