param(
    [ValidateSet('Visa','Pico','Thorlabs')][string[]]$Vendor = @('Visa','Pico','Thorlabs'),
    [string[]]$Component = @(),
    [switch]$VerifyOnly,
    [switch]$UseProxy,
    [string]$Cache = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv-dev\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run scripts/setup-dev.ps1 first.' }
if ($UseProxy) { throw 'Component downloads now use the shared direct-download service; -UseProxy is unsupported.' }
$mapping = @{ Visa='rs-visa'; Pico='pico-sdk'; Thorlabs='thorlabs-opm' }
$selected = if ($Component.Count) { $Component } else { @($Vendor | ForEach-Object { $mapping[$_] }) }
$action = if ($VerifyOnly) { 'verify' } else { 'prepare' }
$arguments = @('-m','lab_control','--cli','components',$action)
foreach ($id in $selected) { $arguments += @('--component',$id) }
if ($Cache) { $arguments += @('--cache',$Cache) }
& $python @arguments
exit $LASTEXITCODE
