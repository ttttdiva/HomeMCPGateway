param(
    [ValidateSet('home-mcp', 'aoitalk')]
    [string]$Target = 'home-mcp',
    [string]$TaskName
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $Root '.env'))) { throw 'Configure .env first.' }
if (-not (Test-Path -LiteralPath (Join-Path $Root '.venv\Scripts\python.exe'))) { throw 'Run setup_windows.ps1 first.' }

$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'connect_tunnel.ps1'))
$PowerShell = [IO.Path]::GetFullPath($PowerShell)
if (-not $TaskName) {
    $TaskName = if ($Target -eq 'aoitalk') { 'Home MCP Gateway - AoiTalk' } else { 'Home MCP Gateway' }
}
$Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Watch -Target {1}' -f $Launcher, $Target
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    $SameTarget = $false
    $ExistingActions = @($Existing.Actions)
    if ($ExistingActions.Count -ne 1) { throw 'An existing task with this name does not match this repository and target; it was left untouched.' }
    foreach ($ExistingAction in $ExistingActions) {
        $ExistingArguments = [string]$ExistingAction.Arguments
        $SameRepo = $ExistingArguments.IndexOf($Launcher, [StringComparison]::OrdinalIgnoreCase) -ge 0
        $SameExecutable = $false
        try { $SameExecutable = [IO.Path]::GetFullPath([string]$ExistingAction.Execute).Equals($PowerShell, [StringComparison]::OrdinalIgnoreCase) }
        catch { $SameExecutable = $false }
        $SameWorkingDirectory = $false
        try { $SameWorkingDirectory = [IO.Path]::GetFullPath([string]$ExistingAction.WorkingDirectory).Equals($Root, [StringComparison]::OrdinalIgnoreCase) }
        catch { $SameWorkingDirectory = $false }
        $TargetMatch = $false
        if ($Target -eq 'home-mcp') {
            # A launcher created before target selection is the legacy home target.
            $TargetMatch = ($ExistingArguments -notmatch '(?i)(?:^|\s)-Target\s+aoitalk(?:\s|$)') -and
                ($ExistingArguments -notmatch '(?i)(?:^|\s)-(?:TunnelId|Alias|RuntimeKeyEnv)\s')
        } else {
            $TargetMatch = ($ExistingArguments -match '(?i)(?:^|\s)-Target\s+aoitalk(?:\s|$)') -and
                ($ExistingArguments -notmatch '(?i)(?:^|\s)-(?:TunnelId|Alias|RuntimeKeyEnv)\s')
        }
        if ($SameRepo -and $SameExecutable -and $SameWorkingDirectory -and $TargetMatch) { $SameTarget = $true; break }
    }
    if (-not $SameTarget) { throw 'An existing task with this name does not match this repository and target; it was left untouched.' }
    Stop-ScheduledTask -TaskName $TaskName
}
$Description = if ($Target -eq 'aoitalk') {
    'Start the dedicated AoiTalk MCP plugin at user logon and monitor/reconnect its Secure MCP Tunnel. Credentials are read from the repository .env.'
} else {
    'Start Home MCP Gateway at user logon and monitor/reconnect the Secure MCP Tunnel every 30 seconds. Credentials are read from the repository .env.'
}
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description $Description -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
