"""Run the project's offline software checks with its existing development environment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("lint", "types", "tests", "docs", "deployment")


def commands(python: Path) -> dict[str, list[list[str]]]:
    prefix = str(python)
    sources = ["src", "tests", "scripts", "examples", "conftest.py"]
    return {
        "lint": [
            [prefix, "-m", "ruff", "check", *sources],
            [prefix, "-m", "ruff", "format", "--check", *sources],
        ],
        "types": [[prefix, "-m", "mypy"]],
        "tests": [[prefix, "-m", "pytest", "-q", "-m", "not hardware"]],
        "docs": [
            [prefix, "scripts/generate_device_docs.py", "--check"],
            [prefix, "scripts/generate_download_docs.py", "--check"],
            [prefix, "scripts/check_docs.py"],
        ],
        "deployment": [
            [prefix, "scripts/generate_python_materials.py", "--check"],
            [prefix, "scripts/check_deployment.py", "--distribution", "online"],
        ],
    }


def software_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("LAB_CONTROL_HARDWARE_")
    }
    environment.update(
        QT_QPA_PLATFORM="offscreen",
        UV_OFFLINE="1",
        UV_NO_SYNC="1",
        UV_PYTHON_DOWNLOADS="never",
        PYTHONIOENCODING="utf-8",
    )
    return environment


def desktop_commands(python: Path, output: Path) -> list[list[str]]:
    uv = python.with_name("uv.exe" if os.name == "nt" else "uv")
    return [
        [
            str(uv),
            "build",
            "--wheel",
            "--offline",
            "--no-build-isolation",
            "--python",
            str(python),
            "--no-python-downloads",
            "--out-dir",
            str(output / "wheels"),
        ],
        [str(python), "scripts/check_device_wheel.py", "<generated-wheel>"],
        [
            str(python),
            "scripts/build_desktop.py",
            "--distribution",
            "online",
            "--output",
            str(output / "bundle"),
        ],
        [
            str(python),
            "scripts/check_desktop.py",
            "--check-agent-default",
            "--bundle",
            str(output / "bundle/TF-LabControl"),
            "--report",
            str(output / "frozen-smoke.json"),
        ],
    ]


def run_check(command: list[str], group: str, report: dict, *, cwd: Path) -> int:
    entry = {"group": group, "command": command, "started_at": datetime.now(timezone.utc).isoformat()}
    report["checks"].append(entry)
    started = time.monotonic()
    print(f"[{group}] " + subprocess.list2cmdline(command), flush=True)
    try:
        entry["returncode"] = subprocess.run(
            command, cwd=cwd, env=software_environment(), check=False
        ).returncode
    except OSError as exc:
        entry.update(returncode=127, error=str(exc))
    except KeyboardInterrupt:
        entry.update(returncode=130, error="Interrupted by user")
        raise
    finally:
        entry["duration_seconds"] = round(time.monotonic() - started, 3)
    return entry["returncode"]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=GROUPS, nargs="+", help="Run selected check groups")
    parser.add_argument("--list", action="store_true", help="Print commands without executing them")
    parser.add_argument(
        "--desktop", action="store_true", help="Also build and check an offline wheel and directory EXE"
    )
    parser.add_argument("--report", type=Path, help="New JSON report path; existing reports are preserved")
    args = parser.parse_args()
    python = ROOT / ".venv-dev" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    selected = args.only or GROUPS
    plan = commands(python)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = ROOT / ".tools/checks" / run_id
    if args.desktop:
        plan["desktop"] = desktop_commands(python, output / "desktop")
        selected = (*selected, "desktop")
    if args.list:
        print(json.dumps({group: plan[group] for group in selected}, ensure_ascii=False, indent=2))
        return 0
    path = (args.report or output / "report.json").resolve()
    if path.exists():
        parser.error("Report already exists; choose a new --report path to preserve previous results")
    report = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
        "groups": list(selected),
        "hardware_tests_enabled": False,
        "status": "running",
        "returncode": 1,
    }
    started = time.monotonic()
    try:
        if not python.is_file():
            raise ValueError("Project .venv-dev is missing; prepare it explicitly with scripts/setup-dev.ps1")
        for group in selected:
            for command in plan[group]:
                if command[-1] == "<generated-wheel>":
                    wheels = sorted((output / "desktop/wheels").glob("*.whl"))
                    if len(wheels) != 1:
                        raise ValueError(f"Expected exactly one generated wheel, found {len(wheels)}")
                    command = [*command[:-1], str(wheels[0])]
                result = run_check(command, group, report, cwd=ROOT)
                if result:
                    report.update(status="failed", returncode=result)
                    return result
        report.update(status="passed", returncode=0)
        return 0
    except KeyboardInterrupt:
        report.update(status="interrupted", returncode=130)
        return 130
    except (OSError, ValueError) as exc:
        report.update(status="failed", error=str(exc))
        print(str(exc), flush=True)
        return 1
    finally:
        report.update(
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round(time.monotonic() - started, 3),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Check report: " + str(path), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
