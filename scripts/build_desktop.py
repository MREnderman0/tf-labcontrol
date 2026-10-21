"""Build online/offline desktop bundles with unchanged release gates; never install drivers."""

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


def stage_offline_packages(target: Path, components, caches=None) -> dict:
    """Stage only the selected runtime materials, never development wheels."""
    from lab_control.deployment.materials import prepare_components

    report = prepare_components(
        components, cache=target / "maintenance/packages", caches=caches, download=False
    )
    if not report["passed"]:
        raise ValueError("Offline materials incomplete: " + json.dumps(report, ensure_ascii=False))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--distribution", choices=("online", "offline"), default="online")
    parser.add_argument("--cache", type=Path, action="append")
    parser.add_argument("--component", action="append", default=[])
    parser.add_argument("--output", type=Path, help="New output directory; existing builds are preserved")
    args = parser.parse_args()
    output = (args.output or ROOT / "dist" / args.distribution).resolve()
    if (output / "TF-LabControl").exists():
        parser.error("Output already exists; use --output with a new directory to preserve this build")
    command = [sys.executable, str(ROOT / "scripts/check_deployment.py"), "--distribution", args.distribution]
    for cache in args.cache or []:
        command += ["--cache", str(cache.resolve())]
    if args.release:
        command.append("--release")
    subprocess.run(command, cwd=ROOT, check=True)
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    from lab_control.deployment.catalog import load_manifest

    manifest = load_manifest()
    components = args.component or [c["id"] for c in manifest["components"]]
    if args.distribution == "offline":
        from lab_control.deployment.materials import component_closure, verify_component

        checks = [
            verify_component(c, tuple(args.cache) if args.cache else None)
            for c in component_closure(components)
        ]
        missing = [c for c in checks if c["state"] != "verified"]
        if missing:
            raise ValueError("Offline materials incomplete: " + json.dumps(missing, ensure_ascii=False))
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
    destination = ROOT / "build" / args.distribution / "deployment"
    destination.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "kind": "release_manifest",
        "release_ready": args.release,
        "distribution": args.distribution,
        "selected_components": components if args.distribution == "offline" else args.component,
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
            "--distpath",
            str(output),
            "--workpath",
            str(ROOT / "build" / args.distribution / "pyinstaller"),
            str(ROOT / "deployment/labcontrol.spec"),
        ],
        cwd=ROOT,
        env={**build_environment(), "LABCONTROL_BUILD_MANIFEST": str(destination / "release-manifest.json")},
        check=True,
    )
    target = output / "TF-LabControl"
    if args.distribution == "offline":
        stage_offline_packages(target, components, tuple(args.cache) if args.cache else None)
    maintenance = target / "maintenance"
    maintenance.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "deployment/components.json", maintenance / "components.json")
    for name in ("repair_53100a_driver.ps1", "ensure_vc_runtime.ps1", "launch-kplusk-config.ps1"):
        shutil.copy2(ROOT / "scripts" / name, maintenance / name)
    print(
        json.dumps(
            {
                "bundle": str(target),
                "distribution": args.distribution,
                "bytes": sum(p.stat().st_size for p in target.rglob("*") if p.is_file()),
                "release_ready": args.release,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
