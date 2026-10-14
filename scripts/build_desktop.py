"""Build a test executable or enforce the offline release gate; never install drivers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
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


def stage_offline_packages() -> None:
    """Copy verified repair materials into the directory build beside the EXE."""

    source = ROOT / "deployment/packages"
    target = ROOT / "dist/TF-LabControl/maintenance/packages"
    installers = source / "installers"
    if not installers.is_dir():
        raise FileNotFoundError("deployment/packages/installers is missing")
    target.mkdir(parents=True, exist_ok=True)
    # The runtime catalog looks for vendor installers directly in this cache.
    for item in installers.iterdir():
        if item.is_file():
            shutil.copy2(item, target / item.name)
    for name in ("python", "tools"):
        folder = source / name
        if folder.is_dir():
            shutil.copytree(folder, target / name, dirs_exist_ok=True)
    for name in ("README.md", "manifest.json"):
        item = source / name
        if item.is_file():
            shutil.copy2(item, target / name)
    # Keep the external, UAC-separated driver repair entry point with the
    # maintenance materials so a directory build can repair Problem 39/45
    # without requiring the source checkout.
    repair_script = ROOT / "scripts/repair_53100a_driver.ps1"
    if repair_script.is_file():
        shutil.copy2(repair_script, target.parent / repair_script.name)
    vc_guard = ROOT / "scripts/ensure_vc_runtime.ps1"
    if vc_guard.is_file():
        shutil.copy2(vc_guard, target.parent / vc_guard.name)
    component_manifest = ROOT / "deployment/components.json"
    if component_manifest.is_file():
        shutil.copy2(component_manifest, target.parent / component_manifest.name)


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
        "offline_packages_manifest_sha256": hashlib.sha256(
            (ROOT / "deployment/packages/manifest.json").read_bytes()
        ).hexdigest(),
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
    stage_offline_packages()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
