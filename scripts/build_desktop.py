"""Build a test executable or enforce the offline release gate; never install drivers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_environment() -> dict[str, str]:
    environment = dict(os.environ)
    windows = Path(environment.get("WINDIR", r"C:\Windows"))
    environment["PATH"] = os.pathsep.join(
        map(
            str,
            (
                Path(sys.executable).parent,
                Path(sys.base_prefix),
                windows / "System32",
                windows,
                windows / "System32/WindowsPowerShell/v1.0",
            ),
        )
    )
    for key in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        environment.pop(key, None)
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    command = [sys.executable, str(ROOT / "scripts/check_deployment.py")]
    if args.release:
        command.append("--release")
    subprocess.run(command, cwd=ROOT, check=True)
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    from lab_control.deployment.catalog import load_manifest

    manifest = load_manifest()
    wanted = set(manifest["python_roots"])
    for name in (*wanted, "pyinstaller", "pyinstaller-hooks-contrib"):
        version = importlib.metadata.version(name)
        expected = next(p["version"] for p in lock["package"] if p["name"].lower() == name.lower())
        if version != expected:
            raise ValueError(f"{name} installed {version}, lock requires {expected}")
    by_name = {p["name"]: p for p in lock["package"]}
    included = set()

    def collect(name):
        key = name.lower()
        if key in included:
            return
        included.add(key)
        for dependency in by_name[key].get("dependencies", []):
            collect(dependency["name"])

    for name in wanted:
        collect(name)
    destination = ROOT / "build/deployment"
    destination.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "kind": "release_manifest",
        "release_ready": args.release,
        "python": sys.version,
        "target": "windows-x64",
        "components": manifest["components"],
        "packages": [{"name": name, "version": by_name[name]["version"]} for name in sorted(included)],
        "build_tools": {
            name: importlib.metadata.version(name) for name in ("pyinstaller", "pyinstaller-hooks-contrib")
        },
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "components_sha256": hashlib.sha256((ROOT / "deployment/components.json").read_bytes()).hexdigest(),
    }
    (destination / "release-manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            str(ROOT / "deployment/labcontrol.spec"),
        ],
        cwd=ROOT,
        env=build_environment(),
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
