[CmdletBinding()]
param(
    [string]$InstallerPath = '',
    [string]$OutputRoot = '',
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'maintenance-common.ps1')
if (-not $OutputRoot) { $OutputRoot = Join-Path (Get-LabRuntimeRoot) 'maintenance\vc-runtime' }
$maintenanceLease = $null

function Get-VersionValue {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return $null }
    $text = ([string]$Value).Trim() -replace '^v', ''
    try { return [Version]$text } catch { return $null }
}

function Get-InstalledVcRuntime {
    $key = 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    try {
        $value = Get-ItemProperty -LiteralPath $key -ErrorAction Stop
        [ordered]@{
            Installed = [int]$value.Installed
            Version = [string]$value.Version
            ParsedVersion = (Get-VersionValue $value.Version)
            RegistryPath = $key
        }
    } catch {
        [ordered]@{ Installed = 0; Version = $null; ParsedVersion = $null; RegistryPath = $key; Error = $_.Exception.Message }
    }
}

function Save-Record {
    param([System.Collections.IDictionary]$Record)
    New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
    $path = Join-Path $OutputRoot 'results.json'
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $history = Join-Path $OutputRoot 'history'
        New-Item -ItemType Directory -Force -Path $history | Out-Null
        $historyPath = Join-Path $history ((Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + $Record.action + '.json')
        Copy-Item -LiteralPath $path -Destination $historyPath -Force
    }
    $Record.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    $Record | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding UTF8
}

$record = [ordered]@{
    schema_version = 1
    kind = 'vc_runtime_guard'
    started_at = (Get-Date).ToUniversalTime().ToString('o')
    architecture = 'x64'
    action = 'not_started'
    installer = $null
    required_version = $null
    before = $null
    after = $null
    installer_exit_code = $null
    reboot_required = $false
    downgrade_prevented = $false
    security_policy_changed = $false
}

try {
    $root = Split-Path -Parent $PSScriptRoot
    $manifestPath = @(
        (Join-Path $root 'deployment\components.json'),
        (Join-Path $PSScriptRoot 'components.json')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $manifestPath) { throw 'VC++ component inventory is missing beside this maintenance script' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $component = @($manifest.components | Where-Object { $_.id -eq 'msvc-runtime' }) | Select-Object -First 1
    if (-not $component -or -not $component.version -or -not $component.sha256 -or -not $component.filename) {
        throw 'msvc-runtime inventory is incomplete; refusing to install an unpinned package'
    }
    $required = Get-VersionValue $component.version
    if ($null -eq $required) { throw "Invalid required VC++ version: $($component.version)" }
    $record.required_version = $component.version

    $record.before = Get-InstalledVcRuntime
    if ($record.before.Installed -eq 1 -and $record.before.ParsedVersion -and $record.before.ParsedVersion -ge $required) {
        $record.action = 'skipped'
        $record.downgrade_prevented = $true
        $record.reason = "Installed VC++ $($record.before.Version) is at least $($component.version); installer was not run"
        $record.after = $record.before
        Save-Record $record
        exit 0
    }

    if (-not $InstallerPath) {
        $InstallerPath = @(
            (Join-Path $env:LOCALAPPDATA ('TF-LabControl\maintenance\packages\' + $component.filename)),
            (Join-Path $env:ProgramData ('TF-LabControl\maintenance\packages\' + $component.filename)),
            (Join-Path $root ('deployment\packages\installers\' + $component.filename)),
            (Join-Path $root ('.tools\installers\' + $component.filename)),
            (Join-Path $PSScriptRoot ('packages\' + $component.filename))
        ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    $InstallerPath = (Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop).Path
    $record.installer = $InstallerPath

    if (-not $Elevated -and -not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $record.action = 'elevation_waiting'
        Save-Record $record
        $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-InstallerPath', $InstallerPath, '-OutputRoot', $OutputRoot, '-Elevated')
        try {
            $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            $child = Start-Process -FilePath $windowsPowerShell -Verb RunAs -ArgumentList (ConvertTo-LabProcessArguments $args) -WorkingDirectory $root -Wait -PassThru -ErrorAction Stop
            exit $child.ExitCode
        } catch {
            $record.action = 'blocked'
            $record.reason = 'UAC elevation was cancelled or failed: ' + $_.Exception.Message
            Save-Record $record
            exit 1
        }
    }

    $maintenanceLease = Enter-LabMaintenanceLease
    $hash = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne ([string]$component.sha256).ToLowerInvariant()) { throw 'VC++ installer SHA256 does not match deployment/components.json' }
    $signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Microsoft Corporation*') {
        throw "VC++ installer signature validation failed: $($signature.Status) / $($signature.SignerCertificate.Subject)"
    }
    $record.action = 'installing'
    $process = Start-Process -FilePath $InstallerPath -ArgumentList @('/install', '/quiet', '/norestart') -Wait -PassThru
    $record.installer_exit_code = $process.ExitCode
    $record.reboot_required = $process.ExitCode -eq 3010
    if ($process.ExitCode -notin @(0, 1638, 3010)) { throw "VC++ installer returned exit code $($process.ExitCode)" }

    $record.after = Get-InstalledVcRuntime
    if ($record.after.Installed -ne 1 -or -not $record.after.ParsedVersion -or $record.after.ParsedVersion -lt $required) {
        throw "VC++ post-install version is insufficient: $($record.after.Version); required $($component.version)"
    }
    $record.action = 'installed'
    $record.reason = "Installed VC++ $($record.after.Version); no downgrade permitted"
    Save-Record $record
    exit 0
} catch {
    $record.action = 'blocked'
    $record.reason = $_.Exception.Message
    try { $record.after = Get-InstalledVcRuntime } catch {}
    Save-Record $record
    exit 1
} finally {
    Exit-LabMaintenanceLease $maintenanceLease
}
