import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_hardware_environment_cannot_enable_retired_test(monkeypatch):
    monkeypatch.setenv("LAB_CONTROL_HARDWARE_CONFIG", "must-not-be-opened.toml")
    monkeypatch.setenv("LAB_CONTROL_HARDWARE_RESOURCE", "must-not-be-contacted")
    entry = runpy.run_path(str(ROOT / "tests/hardware/test_readonly.py"))
    with pytest.raises(pytest.skip.Exception, match="retired"):
        entry["test_configured_instrument_readonly"]()


def test_local_checks_exclude_hardware_and_do_not_sync_dependencies(monkeypatch):
    entry = runpy.run_path(str(ROOT / "scripts/check_project.py"))
    python = ROOT / ".venv-dev/Scripts/python.exe"
    plan = entry["commands"](python)
    assert plan["tests"][0][-2:] == ["-m", "not hardware"]
    assert all(command[0] == str(python) for group in plan.values() for command in group)
    assert [str(python), "scripts/generate_download_docs.py", "--check"] in plan["docs"]
    monkeypatch.setenv("LAB_CONTROL_HARDWARE_CONFIG", "old-config")
    environment = entry["software_environment"]()
    assert "LAB_CONTROL_HARDWARE_CONFIG" not in environment
    assert environment["QT_QPA_PLATFORM"] == "offscreen"
    assert environment["UV_OFFLINE"] == "1"


def test_desktop_checks_use_offline_materials_and_unique_output(tmp_path):
    entry = runpy.run_path(str(ROOT / "scripts/check_project.py"))
    commands = entry["desktop_commands"](tmp_path / "python.exe", tmp_path / "new-run")
    assert "--offline" in commands[0] and "--no-build-isolation" in commands[0]
    assert commands[2][-2:] == ["--output", str(tmp_path / "new-run/bundle")]
    assert str(tmp_path / "new-run/bundle/TF-LabControl") in commands[3]
    assert all("dist/" not in value.replace("\\", "/") for command in commands for value in command)


@pytest.mark.parametrize("failure", ["command", "missing-environment", "interrupted"])
def test_check_report_survives_failures(tmp_path, monkeypatch, failure):
    entry = runpy.run_path(str(ROOT / "scripts/check_project.py"))
    entry["main"].__globals__["ROOT"] = tmp_path
    python = tmp_path / ".venv-dev" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if failure != "missing-environment":
        python.parent.mkdir(parents=True)
        python.touch()
    report = tmp_path / "checks/report.json"
    monkeypatch.setattr(sys, "argv", ["check_project.py", "--only", "docs", "--report", str(report)])

    def execute(*args, **kwargs):
        if failure == "interrupted":
            raise KeyboardInterrupt
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr("subprocess.run", execute)
    result = entry["main"]()
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert result != 0 and saved["returncode"] == result
    assert saved["status"] == ("interrupted" if failure == "interrupted" else "failed")
    assert saved["duration_seconds"] >= 0 and saved["finished_at"]
    assert not saved["hardware_tests_enabled"]
    if failure != "missing-environment":
        assert len(saved["checks"]) == 1
        assert saved["checks"][0]["returncode"] == result
