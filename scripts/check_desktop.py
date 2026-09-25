"""Copy a built app outside the checkout and run real frozen workers and GUI selftest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/TF-LabControl")
    parser.add_argument("--report", type=Path, default=ROOT / ".tools/deployment/frozen-smoke.json")
    args = parser.parse_args()
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="labcontrol-frozen-中文 ") as temporary:
        outside = Path(temporary).resolve()
        assert outside.parent == Path(tempfile.gettempdir()).resolve()
        assert not outside.is_relative_to(ROOT)
        target = outside / "应用 程序"
        shutil.copytree(args.bundle.resolve(), target)
        executable = target / "TF-LabControl.exe"
        environment = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            environment.pop(name, None)
        windows = Path(environment.get("WINDIR", r"C:\Windows"))
        environment.update(
            PATH=os.pathsep.join(
                map(str, (windows / "System32", windows, windows / "System32/WindowsPowerShell/v1.0"))
            ),
            QT_QPA_PLATFORM="offscreen",
            LAB_CONTROL_HOME=str(outside / "data"),
            LOCALAPPDATA=str(outside / "local"),
        )
        result = subprocess.run(
            [str(executable), "--deployment-check", str(args.report)],
            cwd=outside,
            env=environment,
            capture_output=True,
            timeout=90,
        )
        if result.returncode:
            raise RuntimeError(
                args.report.read_text(encoding="utf-8") if args.report.exists() else repr(result.stderr)
            )
        folder = outside / "损坏的记录 中文"
        folder.mkdir()
        data = folder / "segment-00001.sqlite3"
        data.write_bytes(b"invalid sqlite fixture")
        worker = subprocess.run(
            [str(executable), "--csv-worker", str(folder)],
            cwd=outside,
            env=environment,
            capture_output=True,
            timeout=20,
        )
        assert worker.returncode == 1
        assert data.read_bytes() == b"invalid sqlite fixture"
        assert json.loads((folder / "csv-status.json").read_text())["error"]
        assert not list(outside.rglob("hardware.lock"))
        report = json.loads(args.report.read_text(encoding="utf-8"))
        report.update(
            outside_source=True,
            path_sanitized=True,
            corrupt_worker_preserved_input=True,
            exe_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        )
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
