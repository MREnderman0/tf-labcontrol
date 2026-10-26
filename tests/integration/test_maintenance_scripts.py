"""Exercise Windows argument handling and lock interop without drivers or elevation."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lab_control.application.runtime import ApplicationRuntime

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows maintenance entry points")
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def powershell(command, environment):
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_start_process_roundtrips_space_unicode_quotes_empty_and_trailing_slash(tmp_path):
    output = tmp_path / "参数 output.json"
    arguments = ["Program Files\\设备 驱动", 'say "hello"', "", "C:\\space dir\\", 'slash\\"quote']
    values = ["-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *arguments]
    environment = dict(
        os.environ,
        LAB_TEST_HELPER=str(SCRIPTS / "maintenance-common.ps1"),
        LAB_TEST_PYTHON=sys.executable,
        LAB_TEST_VALUES=json.dumps(values),
        LAB_TEST_OUTPUT=str(output),
    )
    powershell(
        """
$ErrorActionPreference = 'Stop'
. $env:LAB_TEST_HELPER
[string[]]$values = ConvertFrom-Json $env:LAB_TEST_VALUES
$process = Start-Process -FilePath $env:LAB_TEST_PYTHON -WindowStyle Hidden -Wait -PassThru `
    -ArgumentList (ConvertTo-LabProcessArguments $values) -RedirectStandardOutput $env:LAB_TEST_OUTPUT
if ($process.ExitCode -ne 0) { throw 'argv child failed' }
""",
        environment,
    )
    assert json.loads(output.read_text(encoding="utf-8-sig")) == arguments


def test_sufficient_vc_runtime_skips_without_installer_or_elevation(tmp_path):
    environment = dict(
        os.environ,
        LAB_TEST_SCRIPT=str(SCRIPTS / "ensure_vc_runtime.ps1"),
        LAB_CONTROL_HOME=str(tmp_path / "用户 data"),
        LOCALAPPDATA=str(tmp_path / "appdata"),
        ProgramData=str(tmp_path / "programdata"),
    )
    powershell(
        """
$ErrorActionPreference = 'Stop'
function Get-ItemProperty { return [PSCustomObject]@{ Installed = 1; Version = 'v99.0.0.0' } }
function Start-Process { throw 'This test must not start an installer or elevate' }
& $env:LAB_TEST_SCRIPT -InstallerPath 'Z:\nonexistent installer.exe'
""",
        environment,
    )
    record = json.loads(
        (tmp_path / "用户 data/maintenance/vc-runtime/results.json").read_text(encoding="utf-8-sig")
    )
    assert record["action"] == "skipped" and record["downgrade_prevented"]
    assert record["installer"] is None and record["installer_exit_code"] is None


def test_fixed_hardware_gate_excludes_other_data_home(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    monkeypatch.setenv("LAB_CONTROL_HOME", str(tmp_path / "first"))
    code = """
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError
try:
    with ApplicationRuntime(hardware=True):
        print('acquired')
except DeviceError as exc:
    print(exc.code)
"""
    environment = dict(os.environ, LAB_CONTROL_HOME=str(tmp_path / "second"))

    def child():
        return subprocess.run(
            [sys.executable, "-c", code],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout.strip()

    with ApplicationRuntime(hardware=True):
        assert child() == "process_busy"
    assert child() == "acquired"


def test_maintenance_holds_gate_until_exit_and_releases_it(tmp_path):
    environment = dict(
        os.environ,
        LOCALAPPDATA=str(tmp_path / "appdata"),
        LAB_CONTROL_HOME=str(tmp_path / "legacy"),
        LAB_TEST_HELPER=str(SCRIPTS / "maintenance-common.ps1"),
        LAB_TEST_PYTHON=sys.executable,
    )
    result = powershell(
        """
$ErrorActionPreference = 'Stop'
. $env:LAB_TEST_HELPER
$code = @'
from lab_control.storage.runtime import hardware_lock_home
from lab_control.application.leases import ProcessLease
from lab_control.domain.instruments import DeviceError
try:
    lease = ProcessLease(hardware_lock_home())
    lease.close()
    print('acquired')
except DeviceError as exc:
    print(exc.code)
'@
$lease = Enter-LabMaintenanceLease
try { & $env:LAB_TEST_PYTHON -c $code }
finally { Exit-LabMaintenanceLease $lease }
& $env:LAB_TEST_PYTHON -c $code
""",
        environment,
    )
    assert result.stdout.split() == ["process_busy", "acquired"]
