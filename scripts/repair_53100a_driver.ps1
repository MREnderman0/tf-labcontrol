[CmdletBinding()]
param(
    [string]$InstanceId = '',
    [string]$DriverRoot = '',
    [string]$OutputRoot = 'D:\TF-LabControl-runtime\TF-LabControl\hardware-tests\53100A-selected-usb',
    [string]$Batch = '',
    [switch]$CycleDevice,
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$expected = @{
    'MD53100A.inf' = '2b3b4df27b8e76e7ca83d09b0902c2e927f22a352bf5e3492754c17a0daf125c'
    'MD53100A.cat' = 'bc242e13d77416260dc02530103882e5f0942ddc4dfa555dc3e87496092891d8'
    'MD53100A.sys' = '97c93789273ab3d2da624a0917be5a1244fe1cfd678fd5cd8fa3b267732db912'
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$batch = if ($Batch) { $Batch } else { Join-Path $OutputRoot ($stamp + '-driver-repair') }
New-Item -ItemType Directory -Force -Path $batch | Out-Null
$result = [ordered]@{
    schema_version = 1
    started_at = (Get-Date).ToUniversalTime().ToString('o')
    scope = 'Selected 53100A USB driver repair; exact device only; no acquisition or parameter writes'
    instance_id = $InstanceId
    cycle_requested = [bool]$CycleDevice
    stages = [ordered]@{}
    final_state = [ordered]@{ driver_ready = $false; security_policy_changed = $false; reboot_performed = $false }
}

function Write-Result {
    $result.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $batch 'results.json') -Encoding UTF8
    $lines = @('# 53100A 外部 PowerShell 驱动修复', '', ('设备：`' + $InstanceId + '`'), '', '| 阶段 | 状态 | 说明 |', '| --- | --- | --- |')
    foreach ($entry in $result.stages.GetEnumerator()) {
        $reason = [string]$entry.Value.reason -replace '\|', '/'
        $lines += "| $($entry.Key) | $($entry.Value.status) | $reason |"
    }
    $lines += '', ('最终状态：`' + ($result.final_state | ConvertTo-Json -Compress) + '`')
    if ($result.error) { $lines += '', ('错误：' + $result.error) }
    $lines -join "`n" | Set-Content -LiteralPath (Join-Path $batch 'record.md') -Encoding UTF8
}

function Snapshot {
    $device = @(Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
        Where-Object { $_.PNPDeviceID -ieq $InstanceId } |
        Select-Object Name, PNPDeviceID, Present, Service, ConfigManagerErrorCode, Status)
    $signed = @(Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
        Where-Object { $_.DeviceID -ieq $InstanceId } |
        Select-Object DeviceID, DeviceName, DriverVersion, DriverProviderName, InfName, IsSigned)
    [ordered]@{ timestamp = (Get-Date).ToUniversalTime().ToString('o'); device = $device; signed_driver = $signed }
}

function Invoke-Native {
    param([string[]]$Arguments, [string]$LogName)
    $log = Join-Path $batch $LogName
    & "$env:SystemRoot\System32\pnputil.exe" @Arguments *> $log
    $code = $LASTEXITCODE
    $result.stages[$LogName] = @{ status = if ($code -in @(0, 3010)) { 'passed' } else { 'failed' }; reason = "pnputil exit $code; see $LogName" }
    if ($code -eq 3010) { $result.final_state.reboot_required = $true }
    if ($code -notin @(0, 3010)) { throw "pnputil returned $code" }
}

try {
    if (-not $InstanceId -or $InstanceId -notmatch '^USB\\VID_16D0&PID_05B8\\') {
        throw '必须传入本次明确选定的完整 USB InstanceId（USB\\VID_16D0&PID_05B8\\...）；脚本不从源码猜测序列号'
    }
    if (-not $Elevated -and -not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-InstanceId', $InstanceId, '-OutputRoot', $OutputRoot, '-Batch', $batch, '-Elevated')
        if ($DriverRoot) { $args += @('-DriverRoot', $DriverRoot) }
        if ($CycleDevice) { $args += '-CycleDevice' }
        $result.stages['elevation'] = @{ status = 'waiting'; reason = 'external Windows PowerShell UAC prompt' }
        Write-Result
        try {
            $child = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $args -WorkingDirectory (Split-Path -Parent $PSCommandPath) -Wait -PassThru -ErrorAction Stop
            exit $child.ExitCode
        } catch {
            $result.error = 'UAC 提权被取消或启动失败：' + $_.Exception.Message
            $result.stages['elevation'] = @{ status = 'blocked'; reason = $result.error }
            Write-Result
            exit 1
        }
    }

    $busy = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('TF-LabControl.exe', 'tserve64.exe', 'tserve.exe', 'TimeLab64.exe', 'TimeLab.exe') })
    if ($busy) { throw '检测到 TF-LabControl/TimeLab/TSERVE 正在运行，拒绝在占用中重绑驱动' }
    $before = Snapshot
    $before | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $batch 'before.json') -Encoding UTF8
    $target = @($before.device | Where-Object { $_.Present })
    if ($target.Count -ne 1) { throw "未找到唯一的已连接目标设备；当前匹配条目数 $($target.Count)，请连接指定 53100A 后重试" }
    if ($target[0].PNPDeviceID -ine $InstanceId) { throw '目标设备 ID 不匹配，拒绝绑定其他 USB 设备' }
    $result.stages['preflight'] = @{ status = 'passed'; reason = 'exact selected instance present; no instrument process running' }

    if (-not $DriverRoot) { $DriverRoot = Join-Path ${env:ProgramFiles} 'Microchip\TimeLab\USB Driver\53100A\wlh\x64' }
    $files = @()
    foreach ($name in $expected.Keys) {
        $path = Join-Path $DriverRoot $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "缺少驱动文件：$path" }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        $sig = Get-AuthenticodeSignature -LiteralPath $path
        $files += [ordered]@{ name = $name; path = $path; sha256 = $hash; expected_sha256 = $expected[$name]; signature = $sig.Status.ToString(); signer = $sig.SignerCertificate.Subject }
        if ($hash -ne $expected[$name]) { throw "驱动文件哈希不匹配：$name" }
        if ($name -ne 'MD53100A.inf' -and $sig.Status -ne 'Valid') { throw "驱动签名无效：$name / $($sig.Status)" }
    }
    $files | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $batch 'driver-files.json') -Encoding UTF8
    $result.stages['payload'] = @{ status = 'passed'; reason = 'locked INF/CAT/SYS hashes verified; no file replacement' }

    Invoke-Native @('/add-driver', (Join-Path $DriverRoot 'MD53100A.inf'), '/install') 'pnputil-install.log'
    if ($CycleDevice) {
        Disable-PnpDevice -InstanceId $InstanceId -Confirm:$false
        Start-Sleep -Seconds 2
        Enable-PnpDevice -InstanceId $InstanceId -Confirm:$false
        Start-Sleep -Seconds 3
        $result.stages['device_cycle'] = @{ status = 'passed'; reason = 'only selected PNP instance disabled and enabled once' }
    }
    $after = Snapshot
    $after | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $batch 'after.json') -Encoding UTF8
    $final = @($after.device | Where-Object { $_.Present -and $_.PNPDeviceID -ieq $InstanceId })
    if ($final.Count -ne 1) { throw '驱动绑定后目标设备不再枚举' }
    $result.final_state.device = $final[0]
    if ([int]$final[0].ConfigManagerErrorCode -ne 0) {
        $result.final_state.code_integrity_events = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; StartTime = (Get-Date).AddHours(-2) } -ErrorAction SilentlyContinue |
            Where-Object { $_.Message -match 'MD53100A|16D0|05B8' } |
            Select-Object TimeCreated, Id, Message)
        throw "驱动仍未加载：ConfigManagerErrorCode=$($final[0].ConfigManagerErrorCode)；未修改 Secure Boot/Code Integrity"
    }
    $result.final_state.driver_ready = $true
    $result.stages['validation'] = @{ status = 'passed'; reason = 'selected device reports ConfigManagerErrorCode=0' }
} catch {
    $result.error = $_.Exception.Message
    $result.stages['repair'] = @{ status = 'blocked'; reason = $_.Exception.Message }
    $result.final_state.recovery = 'No security-policy change, reboot, driver deletion or downgrade was performed automatically'
    try { (Snapshot | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath (Join-Path $batch 'after-error.json') -Encoding UTF8 } catch {}
    Write-Result
    exit 1
}
Write-Result
exit 0
