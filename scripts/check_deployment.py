"""Check component coverage; --release additionally requires verified offline deployment."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

from lab_control.deployment.catalog import cache_directories, load_manifest, release_issues
from lab_control.instruments.devices.catalog import load_definitions
from lab_control.instruments.registry import PROFILES, SDK_ADAPTERS

ROOT = Path(__file__).resolve().parents[1]


def check(root: Path = ROOT, *, release: bool = False, caches=None) -> dict:
    manifest = load_manifest(root / "deployment/components.json")
    components = {item["id"]: item for item in manifest["components"]}
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    names = {re.split(r"[<>=!~\[; ]", item)[0].lower() for item in project["project"]["dependencies"]}
    registered = {name.lower() for name in manifest["python_roots"]}
    errors = (
        ["Runtime dependencies differ from inventory: " + str(sorted(names ^ registered))]
        if names != registered
        else []
    )
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked = {p["name"] for p in lock["package"]}
    transitives = manifest.get("python_transitive_roots", {})
    for name in transitives:
        if name.lower() not in locked:
            errors.append("Unresolved transitive dependency: " + name)
    modules = (
        set(manifest["python_roots"].values())
        | set(transitives.values())
        | sys.stdlib_module_names
        | {"lab_control", "__future__"}
    )
    for path in (root / "src/lab_control").rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                imported = [node.module.split(".")[0]]
            for module in imported:
                if module not in modules:
                    errors.append(f"{path.relative_to(root)}: unregistered runtime import {module}")
    for definition in load_definitions().values():
        if not definition.deployment_components:
            errors.append(definition.adapter_id + ": no deployment component references")
        for key in definition.deployment_components:
            if key not in components:
                errors.append(definition.adapter_id + ": unregistered component " + key)
    for component in components.values():
        for adapter in component["adapters"]:
            if adapter not in PROFILES and adapter not in SDK_ADAPTERS:
                errors.append(component["id"] + ": unknown adapter " + adapter)
    pending = {}
    for component in components.values():
        if release:
            problems = release_issues(component, caches if caches is not None else cache_directories())
        else:
            problems = [
                field + " pending"
                for field in ("redistribution", "installation", "validation")
                if component[field].get("state") != "verified"
            ]
        if problems:
            pending[component["id"]] = problems
    return {
        "schema_version": 1,
        "mode": "release" if release else "development",
        "errors": sorted(set(errors)),
        "pending": pending,
        "passed": not errors and (not release or not pending),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--cache", type=Path, action="append")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = check(release=args.release, caches=tuple(args.cache) if args.cache else None)
    except (ValueError, OSError) as exc:
        result = {"passed": False, "errors": [str(exc)]}
    if args.report:
        from lab_control.deployment.environment import export_report

        export_report(result, args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
