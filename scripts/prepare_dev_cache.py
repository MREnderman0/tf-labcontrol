"""Export the pinned Windows developer wheelhouse separately from runtime builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_control.deployment.materials import check_file, download_file  # noqa: E402


def export_cache(destination: Path, caches: tuple[Path, ...], *, offline=False) -> dict:
    inventory = json.loads((ROOT / "deployment/packages/manifest.json").read_text(encoding="utf-8-sig"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    wheels = {Path(w["url"]).name: w for p in lock["package"] for w in p.get("wheels", [])}
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    for entry in inventory["files"]:
        if entry["kind"] != "python-wheel":
            continue
        relative = Path(entry["path"]).relative_to("deployment/packages")
        target = destination / relative
        wheel = wheels.get(target.name)
        if not wheel or wheel["hash"] != "sha256:" + entry["sha256"]:
            raise ValueError("Wheelhouse inventory differs from uv.lock: " + target.name)
        source = None
        for cache in (destination, *caches):
            candidate = cache / relative
            try:
                check_file(candidate, entry)
                source = candidate
                break
            except (OSError, ValueError):
                continue
        if source != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=destination) as temp:
                staged = Path(temp) / target.name
                if source:
                    shutil.copyfile(source, staged)
                elif offline:
                    raise FileNotFoundError(target.name)
                else:
                    if not wheel["url"].startswith("https://mirrors.tuna.tsinghua.edu.cn/"):
                        raise ValueError("Pinned wheel is not on the configured domestic mirror")
                    download_file({**entry, "id": target.name, "url": wheel["url"]}, staged)
                check_file(staged, entry)
                os.replace(staged, target)
        rows.append(entry)
    uv_entry = next(e for e in inventory["files"] if e["path"].startswith("deployment/packages/tools/uv-"))
    uv_wheel = next((destination / "python").glob("uv-*.whl"))
    with zipfile.ZipFile(uv_wheel) as archive, tempfile.TemporaryDirectory(dir=destination) as temp:
        member = next(n for n in archive.namelist() if n.endswith("/uv.exe"))
        staged = Path(temp) / "uv.exe"
        staged.write_bytes(archive.read(member))
        check_file(staged, uv_entry)
        uv_target = destination / Path(uv_entry["path"]).relative_to("deployment/packages")
        uv_target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, uv_target)
    rows.append(uv_entry)
    shutil.copy2(
        ROOT / "deployment/packages/python/requirements.txt", destination / "python/requirements.txt"
    )
    report = {
        "schema_version": 1,
        "kind": "developer_cache",
        "files": rows,
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
    }
    (destination / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"path": str(destination), "files": len(rows), "bytes": sum(e["size"] for e in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, action="append")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            export_cache(
                args.output.resolve(),
                tuple(args.cache or [ROOT / "deployment/packages"]),
                offline=args.offline,
            )
        )
    )


if __name__ == "__main__":
    main()
