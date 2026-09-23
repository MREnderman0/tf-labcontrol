param(
    [ValidateSet('Visa','Pico','Thorlabs')][string[]]$Vendor = @('Visa','Pico','Thorlabs'),
    [switch]$UseProxy
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$folder = Join-Path $root '.tools\installers'
New-Item -ItemType Directory -Force -Path $folder | Out-Null
$packages = @{
    Visa = @{ version='7.2.6'; filename='RS_VISA_Setup_Win_7_2_6.exe'; url='https://cdn.rohde-schwarz.com.cn/pws/dl_downloads/dl_application/application_notes/1dc02___rs_v/RS_VISA_Setup_Win_7_2_6.exe'; publisher='Rohde'; page='https://www.rohde-schwarz.com.cn/applications/rs-visa_56280-148812.html' }
    Pico = @{ version='11.1.0.481'; filename='PicoSDK_x64_11.1.0.481.exe'; url='https://www.picotech.com/download/software/sr/PicoSDK_x64_11.1.0.481.exe'; publisher='Pico'; page='https://www.picotech.com/downloads/_lightbox/pico-software-development-kit-64bit' }
    Thorlabs = @{ version='7.0.5320.947'; filename='Thorlabs.OPM.7.0.5320.947.zip'; url='https://media.thorlabscms.cn/contentassets/012d23129bc54ff2bfdbf4c6d4a306fa/thorlabs.opticalparametermonitor.7.0.5320.947.zip'; publisher='Thorlabs'; page='https://www.thorlabschina.cn/software-pages/OPM' }
}
$results = @()
foreach ($name in $Vendor) {
    $package = $packages[$name]
    $path = Join-Path $folder $package.filename
    $record = @{ vendor=$name; version=$package.version; source=$package.url; source_page=$package.page; path=$path; proxy=[bool]$UseProxy; state='pending'; hardware_accepted=$false }
    try {
        if (-not (Test-Path -LiteralPath $path)) {
            $partial = $path + '.partial'
            $curlArgs = @('--fail','--location','--silent','--show-error','--connect-timeout','15','--max-time','600','--continue-at','-','--output',$partial)
            if (-not $UseProxy) { $curlArgs += @('--noproxy','*') }
            & curl.exe @curlArgs $package.url
            if ($LASTEXITCODE -ne 0) { throw 'Direct download incomplete. Partial file retained; rerun to resume. No proxy/foreign fallback.' }
            Move-Item -LiteralPath $partial -Destination $path
        }
        $record.sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        $installer = $path
        if ($name -eq 'Thorlabs') {
            $expanded = Join-Path $folder 'Thorlabs.OPM.7.0.5320.947'
            Expand-Archive -LiteralPath $path -DestinationPath $expanded -Force
            $installer = Join-Path $expanded 'ThorlabsOPMSetup.exe'
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        $record.signature = [string]$signature.Status
        $record.signer = $signature.SignerCertificate.Subject
        if ($signature.Status -ne 'Valid' -or $record.signer -notmatch $package.publisher) { throw 'Installer signature/publisher validation failed; do not run.' }
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
Write-Host "Log: $log. Install with the verified vendor wizard; do not switch device drivers or restart automatically."
if ($results.state -contains 'blocked') { exit 1 }
