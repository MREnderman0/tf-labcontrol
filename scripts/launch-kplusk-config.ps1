param(
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$filename = 'KK_FirmwareUpload_Win32_Ver_5.1.0_25-11-28.exe'
$expected = '769427BDC115AD006C5116E987505B6D2334CC882546392183C9D96B1C7CB0D9'
$candidates = @(
    (Join-Path $root "deployment\packages\tools\kplusk\$filename"),
    (Join-Path $PSScriptRoot "packages\tools\kplusk\$filename"),
    (Join-Path $root "dist\TF-LabControl\maintenance\packages\tools\kplusk\$filename")
)
$path = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $path) {
    throw "K+K configuration tool is not bundled. Expected $filename under deployment/packages/tools/kplusk or the directory build maintenance cache."
}
$actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
if ($actual -ne $expected) {
    throw "K+K configuration tool hash mismatch; refusing to run. Expected $expected, got $actual."
}
if ($VerifyOnly) {
    [pscustomobject]@{ path = $path; sha256 = $actual; verified = $true } | ConvertTo-Json
    exit 0
}
Write-Host "Verified K+K configuration tool: $path"
Write-Host "Use USB for configuration. Do not change the PC NIC; after restart verify the configured IP through the switch."
Start-Process -FilePath $path -WorkingDirectory (Split-Path $path -Parent)
