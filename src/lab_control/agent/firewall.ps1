param([Parameter(Mandatory=$true)][string]$Plan, [Parameter(Mandatory=$true)][string]$Report)
$ErrorActionPreference = 'Stop'
$result = @{passed=$false; before=$null; after=$null; rollback=$null; error=''; timestamp=[DateTime]::UtcNow.ToString('o')}
$created = $false
$removed = $false
function Read-AgentRule {
    param([string]$Name)
    $rule = Get-NetFirewallRule -Name $Name -ErrorAction SilentlyContinue
    if (-not $rule) { return $null }
    $app = $rule | Get-NetFirewallApplicationFilter
    $port = $rule | Get-NetFirewallPortFilter
    $address = $rule | Get-NetFirewallAddressFilter
    $interface = $rule | Get-NetFirewallInterfaceFilter
    return @{name=$rule.Name; group=$rule.Group; enabled=$rule.Enabled.ToString(); action=$rule.Action.ToString();
        direction=$rule.Direction.ToString(); profile=$rule.Profile.ToString(); program=$app.Program;
        protocol=$port.Protocol; port=@($port.LocalPort); local=@($address.LocalAddress);
        remote=@($address.RemoteAddress); interfaces=@($interface.InterfaceAlias)}
}
function Add-AgentRule {
    New-NetFirewallRule -Name $p.rule -DisplayName 'TF LabControl Agent HTTPS' -Group 'TF-LabControl-Agent' `
        -Direction Inbound -Action Allow -Enabled True -Protocol TCP -LocalPort $p.port `
        -Program $p.program -LocalAddress $p.address -RemoteAddress LocalSubnet `
        -InterfaceAlias $interfaceAlias -Profile Private,Domain | Out-Null
}
function Assert-AgentRule {
    param($Rule)
    if (-not $Rule -or $Rule.group -ne 'TF-LabControl-Agent' -or $Rule.program -ne $p.program -or
        ($Rule.local -join ',') -ne $p.address -or ($Rule.port -join ',') -ne [string]$p.port -or
        ($Rule.remote -join ',') -ne 'LocalSubnet' -or ($Rule.interfaces -join ',') -ne $interfaceAlias -or
        $Rule.direction -ne 'Inbound' -or $Rule.action -ne 'Allow' -or $Rule.enabled -ne 'True' -or
        $Rule.protocol -notin @('TCP','6') -or $Rule.profile -notmatch 'Domain' -or
        $Rule.profile -notmatch 'Private' -or $Rule.profile -match 'Public') {
        throw 'Existing/read-back rule does not match the scoped plan; no unrelated rule may be changed.'
    }
}
try {
    $p = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($p.schema_version -ne 1 -or $p.operation -notin @('add','remove') -or
        $p.rule -notmatch '^TF-LabControl-Agent-[a-f0-9]{16}$' -or
        [int]$p.port -lt 1024 -or [int]$p.port -gt 65535 -or
        -not (Test-Path -LiteralPath $p.program -PathType Leaf)) { throw 'Invalid maintenance plan' }
    $ip = [Net.IPAddress]::Parse($p.address)
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        [Net.IPAddress]::IsLoopback($ip) -or $p.address -eq '0.0.0.0') { throw 'Invalid local address' }
    $rows = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $p.address)
    if ($rows.Count -ne 1) { throw 'Address no longer belongs to one local interface' }
    $interfaceAlias = $rows[0].InterfaceAlias
    $result.plan = $p
    $result.before = Read-AgentRule $p.rule
    if ($result.before) { Assert-AgentRule $result.before }
    if ($p.operation -eq 'add' -and -not $result.before) {
        Add-AgentRule
        $created = $true
    } elseif ($p.operation -eq 'remove' -and $result.before) {
        Remove-NetFirewallRule -Name $p.rule
        $removed = $true
    }
    $result.after = Read-AgentRule $p.rule
    if ($p.operation -eq 'add') { Assert-AgentRule $result.after }
    elseif ($result.after) { throw 'Rule removal was not confirmed' }
    $result.passed = $true
} catch {
    $result.error = $_.Exception.Message
    try {
        if ($created) { Remove-NetFirewallRule -Name $p.rule -ErrorAction Stop }
        if ($removed) { Add-AgentRule }
        $result.rollback = Read-AgentRule $p.rule
        if ($removed) { Assert-AgentRule $result.rollback }
        if ($created -and $result.rollback) { throw 'New rule still exists after rollback' }
        $result.rollback_confirmed = $true
    } catch {
        $result.rollback_confirmed = $false
        $result.rollback_error = $_.Exception.Message
    }
} finally {
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Report -Encoding UTF8
}
if (-not $result.passed) { exit 2 }
