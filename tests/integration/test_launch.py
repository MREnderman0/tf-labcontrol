import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab_control import app
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("lan_enabled", [True, False])
def test_formal_main_passes_config_to_real_runtime(qapp, tmp_path, monkeypatch, explicit, lan_enabled):
    from PySide6.QtCore import QTimer

    import lab_control.application.config as config
    import lab_control.application.runtime as runtime
    import lab_control.ui.real_window as window

    monkeypatch.setenv("LAB_CONTROL_HOME", str(tmp_path))
    path = tmp_path / "config with spaces.toml" if explicit else tmp_path / "config/devices.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    configurations = (object(),)
    calls = []
    monkeypatch.setattr(sys, "argv", ["lab-control", "--config", str(path)] if explicit else ["lab-control"])
    monkeypatch.setattr(config, "load_config", lambda p: (calls.append(p), configurations)[1])
    monkeypatch.setattr(app, "QApplication", lambda _args: qapp)
    monkeypatch.setattr(qapp, "exec", lambda: 0)
    monkeypatch.setattr(
        runtime,
        "ApplicationRuntime",
        lambda cfg: (
            calls.append(cfg),
            SimpleNamespace(
                close=lambda: calls.append("closed"), lan_settings=SimpleNamespace(enabled=lan_enabled)
            ),
        )[1],
    )
    scheduled = []

    def scan():
        pass

    monkeypatch.setattr(
        QTimer, "singleShot", lambda interval, callback: scheduled.append((interval, callback))
    )
    monkeypatch.setattr(window, "RealWindow", lambda _runtime: SimpleNamespace(show=lambda: None, scan=scan))
    assert app.main() == 0
    assert calls == [path, configurations, "closed"]
    # The formal entry point always performs the initial discovery pass; LAN
    # availability only changes which providers participate in that scan.
    assert scheduled == [(0, scan)]


def test_real_inventory_stays_empty_and_preserves_saved_state(tmp_path, monkeypatch):
    # All data and process leases go to a temporary fixture. No scan or connect is performed.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = tmp_path / "data"
    runtime = ApplicationRuntime(root=root)
    try:
        assert runtime.hardware
        assert not runtime.inventory.all()
        cfg = ConnectionConfig("TCPIP0::example.invalid::inst0::INSTR", "rs.sma100b", "visa")
        key = runtime.add(cfg)
        runtime.repository.save_workspace("draft/fixture", {"value": "21"})
    finally:
        runtime.close()
    mock = ApplicationRuntime(root=root, hardware=False)
    try:
        assert not mock.inventory.all()
        mock.add(ConnectionConfig("mock://isolated", "rs.sma100b", "mock"))
    finally:
        mock.close()
    runtime = ApplicationRuntime(root=root)
    try:
        assert [row["id"] for row in runtime.inventory.all()] == [key]
        assert runtime.repository.load_workspace("draft/fixture") == {"value": "21"}
    finally:
        runtime.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch launcher")
def test_missing_environment_reports_setup_failure_without_downloading(tmp_path):
    shutil.copyfile(ROOT / "run.bat", tmp_path / "run.bat")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup-dev.ps1").write_text(
        "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'invoked') -Value yes\nexit 17\n"
    )
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "run.bat", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "domestic mirror" in result.stdout
    assert (scripts / "invoked").exists()
