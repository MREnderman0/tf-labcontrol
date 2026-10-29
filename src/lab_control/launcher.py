"""Shared source/frozen entry point. Dispatch workers before importing Qt or hardware."""

from __future__ import annotations

import sys
from pathlib import Path


def worker_command(folder: Path) -> list[str]:
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "lab_control"]
    return [*prefix, "--csv-worker", str(folder.resolve())]


def main() -> int:
    args = sys.argv[1:]
    connector_executable = (
        getattr(sys, "frozen", False) and Path(sys.executable).name.lower() == "tf-labcontrol-agent.exe"
    )
    if connector_executable or (args and args[0] == "--agent-connector"):
        from lab_control.agent.connector import main as connector_main

        return connector_main(args[1:] if args and args[0] == "--agent-connector" else args)
    if args and args[0] == "--csv-worker":
        if len(args) != 2:
            return 2
        from lab_control.storage.recording import csv_worker

        return csv_worker(Path(args[1]))
    if args and args[0] == "--deployment-check":
        if len(args) != 2:
            return 2
        from lab_control.deployment.selftest import run

        return run(Path(args[1]))
    if args and args[0] == "--cli":
        from lab_control.cli.main import main as cli_main

        return cli_main(args[1:])
    from lab_control.app import main as gui_main

    return gui_main()
