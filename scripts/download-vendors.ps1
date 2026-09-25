param(
    [ValidateSet('Visa','Pico','Thorlabs')][string[]]$Vendor = @('Visa','Pico','Thorlabs'),
    [string[]]$Component = @(),
    [switch]$VerifyOnly,
    [switch]$UseProxy
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$folder = Join-Path $root '.tools\installers'
New-Item -ItemType Directory -Force -Path $folder | Out-Null
$manifestPath = Join-Path $root 'deployment\components.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.schema_version -ne 1) { throw 'Unsupported component inventory.' }
$packages = @{}
foreach ($item in $manifest.components) {
    $packages[$item.id] = $item
    if ($item.vendor) { $packages[$item.vendor] = $item }
}
$results = @()
$selected = if ($Component.Count -gt 0) { $Component } else { $Vendor }
foreach ($name in $selected) {
    $package = $packages[$name]
    if (-not $package -or -not $package.filename -or -not $package.sha256 -or -not $package.url) {
        throw "Component $name has incomplete offline payload metadata; update deployment/components.json."
    }
    if ([IO.Path]::GetFileName($package.filename) -ne $package.filename -or $package.filename -match '[\\/:]') {
        throw 'Invalid component cache filename.'
    }
    if (-not $package.signature.publisher -or $package.url -notmatch '^https://') {
        throw 'Verified publisher and official HTTPS source required.'
    }
    if ($package.signature.member -and ([IO.Path]::GetFileName($package.signature.member) -ne $package.signature.member)) {
        throw 'Signature member must be a filename in the extracted archive root.'
    }
    $path = Join-Path $folder $package.filename
    $record = @{ vendor=$name; version=$package.version; source=$package.url; source_page=$package.source_page; path=$path; proxy=[bool]$UseProxy; state='pending'; hardware_accepted=$false }
    try {
        if (-not (Test-Path -LiteralPath $path)) {
            if ($VerifyOnly) { throw 'Offline payload missing; download disabled by -VerifyOnly.' }
            $partial = $path + '.partial'
            $curlArgs = @('--fail','--location','--silent','--show-error','--connect-timeout','15','--max-time','600','--continue-at','-','--output',$partial)
            if (-not $UseProxy) { $curlArgs += @('--noproxy','*') }
            & curl.exe @curlArgs $package.url
            if ($LASTEXITCODE -ne 0) { throw 'Direct download incomplete. Partial file retained; rerun to resume. No proxy/foreign fallback.' }
            Move-Item -LiteralPath $partial -Destination $path
        }
        $record.sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ($record.sha256 -ne $package.sha256) { throw 'Installer hash differs from the pinned inventory; do not run.' }
        $installer = $path
        if ($package.signature.member) {
            $expanded = Join-Path $folder ([IO.Path]::GetFileNameWithoutExtension($package.filename))
            Expand-Archive -LiteralPath $path -DestinationPath $expanded -Force
            $installer = Join-Path $expanded $package.signature.member
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        $record.signature = [string]$signature.Status
        $record.signer = $signature.SignerCertificate.Subject
        if ($signature.Status -ne 'Valid' -or $record.signer -notlike ('*' + $package.signature.publisher + '*')) { throw 'Installer signature/publisher validation failed; do not run.' }
        $record.installer = $installer
        $record.state = 'verified_download'
        Write-Host "Verified $name $($package.version): $installer"
    } catch {
        $record.state = 'blocked'
        $record.error = $_.Exception.Message
        Write-Warning "$name : $($record.error)"
    }
    $record.timestamp = (Get-Date).ToUniversalTime().ToString('o')
    $results += $record
}
$log = Join-Path $folder ('downloads-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
ConvertTo-Json -InputObject @($results) -Depth 8 | Set-Content -LiteralPath $log -Encoding UTF8
Write-Host "Log: $log. Payload preparation only. Installation/repair executor is pending; no drivers or system settings changed."
if ($results.state -contains 'blocked') { exit 1 }
