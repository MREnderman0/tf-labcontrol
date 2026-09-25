"""Bounded, read-only Windows observations; never install, scan or reconfigure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def powershell(script: str, *, cancel=None, timeout: float = 20) -> dict:
    if sys.platform != "win32":
        raise OSError("Windows inspection is unavailable on this platform")
    prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ErrorActionPreference='Stop'; "
    environment = dict(os.environ)
    windows = Path(environment.get("WINDIR", r"C:\Windows"))
    # A PowerShell 7 parent can leak incompatible Core modules into Windows PowerShell 5.1.
    environment["PSModulePath"] = os.pathsep.join(
        map(
            str,
            (
                windows / "System32/WindowsPowerShell/v1.0/Modules",
                Path(environment.get("ProgramFiles", r"C:\Program Files")) / "WindowsPowerShell/Modules",
            ),
        )
    )
    process = subprocess.Popen(
        [
            str(windows / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            prefix + script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8-sig",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=environment,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError("检查已取消")
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows inspection timed out")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise OSError(stderr.strip() or f"Windows inspection exited {process.returncode}")
        return json.loads(stdout)
    finally:
        if process.poll() is None:
            process.terminate()  # Only this inspection's own child, never a vendor process.
            process.communicate()


def signature_status(path: Path) -> dict:
    # Pass path as a single-quoted PowerShell literal, with embedded quotes doubled.
    literal = "'" + str(path.resolve()).replace("'", "''") + "'"
    try:
        return powershell(
            f"$s=Get-AuthenticodeSignature -LiteralPath {literal}; "
            "@{status=$s.Status.ToString();subject=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
        )
    except (OSError, ValueError, TimeoutError) as exc:
        return {"status": "unknown", "error": str(exc)}


def inspect_windows(cancel=None) -> dict:
    return powershell(
        r"""
$result=@{}
try { $result.drivers=@(Get-CimInstance Win32_PnPSignedDriver | Where-Object {
    $_.DeviceClass -in @('PORTS','USB','GPIB','INSTRUMENT') -or
    $_.Manufacturer -match 'FTDI|Pico|Thorlabs|Keithley|National Instruments'
} | Select-Object DeviceName,DeviceID,Manufacturer,DriverVersion,InfName,IsSigned) }
catch { $result.drivers_error=$_.Exception.Message }
try { $result.services=@(Get-CimInstance Win32_Service | Where-Object {
    $_.Name -match 'tftpd|tserve|labcontrol' -or $_.DisplayName -match 'tftpd|tserve|labcontrol'
} | Select-Object Name,State,StartMode,PathName) }
catch { $result.services_error=$_.Exception.Message }
try { $result.firewall_profiles=@(Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction)
    $result.firewall_rules=@(Get-NetFirewallRule | Where-Object {$_.Group -eq 'TF-LabControl'} |
        Select-Object Name,Enabled,Direction,Action,Profile)
} catch { $result.firewall_error=$_.Exception.Message }
try { $result.vc_runtime=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' |
    Select-Object Installed,Version }
catch { $result.vc_runtime_error=$_.Exception.Message }
$result | ConvertTo-Json -Depth 6 -Compress
""",
        cancel=cancel,
    )
