param(
    [string]$TaskName = '80_HomeMCPGateway - Claude Code'
)
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Test-Path -LiteralPath (Join-Path $Root '.git') -PathType Container)) {
    throw 'Install from the primary Home MCP Gateway checkout.'
}
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Entry = Join-Path $PSScriptRoot 'gateway_muse_http.py'
$Init = Join-Path $PSScriptRoot 'init_claude_mcp_secrets.py'
$Launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'start_claude_gateway_http.ps1'))
$StateRoot = Join-Path $Root '.local\claude-code-mcp'
$PowerShell = [IO.Path]::GetFullPath((Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'))
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name

foreach ($p in @($Python,$Entry,$Init,$Launcher,$PowerShell)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        throw 'Claude MCP task prerequisite missing.'
    }
}
# Generates only on first install; validates ACLs and preserves existing credentials.
& $Python $Init --state-root $StateRoot
if ($LASTEXITCODE -ne 0) { throw 'Claude MCP credential initialization failed.' }

function Get-ClaudeMcpListenerPid {
    $listener = Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort 8766 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if (-not $process -or $process.Name -ne 'python.exe' -or
        ([string]$process.CommandLine).IndexOf($Entry, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or
        ([string]$process.CommandLine).IndexOf('--prefix /claude-mcp', [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Port 8766 is owned by an unexpected process; no task changed.'
    }
    return [int]$listener.OwningProcess
}
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    if (@($Existing.Actions).Count -ne 1 -or
        ([string]$Existing.Actions[0].Arguments).IndexOf($Launcher, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Existing task name is not owned by Claude Gateway; refusing overwrite.'
    }
}
$PidBefore = Get-ClaudeMcpListenerPid
if ($PidBefore -and -not $Existing) { throw 'Claude MCP port already in use outside the expected scheduled task.' }
if ($Existing -and $Existing.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName
}
if ($PidBefore) {
    # The task may stop its PowerShell parent but leave Python listening.
    # Only this PID (already checked by command line) is eligible for cleanup.
    Start-Sleep -Milliseconds 250
    $listenerNow = Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort 8766 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listenerNow -and $listenerNow.OwningProcess -ne $PidBefore) {
        throw 'Claude MCP port owner changed during replacement.'
    }
    if ($listenerNow) {
        Stop-Process -Id $PidBefore -Force -ErrorAction Stop
        Start-Sleep -Milliseconds 500
    }
    if (Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort 8766 -ErrorAction SilentlyContinue) {
        throw 'Old Claude MCP process still owns port 8766.'
    }
}

$argsLine = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -StateRoot "{1}"' -f $Launcher, $StateRoot
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $argsLine -WorkingDirectory $Root
# Startup trigger makes the task boot-aware. Interactive user context is
# essential for actual Windows desktop control; logon is the guaranteed fallback.
$Triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn -User $User)
)
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$Description = 'Authenticated Claude remote MCP; reuses Home MCP Gateway with unrestricted tools. Boot+logon triggers; active desktop access requires this user to sign in.'
$BootTriggerInstalled = $false
try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers -Principal $Principal -Settings $Settings -Description $Description -Force -ErrorAction Stop | Out-Null
    $BootTriggerInstalled = $true
} catch {
    # Non-elevated Windows accounts typically cannot create startup triggers.
    # Install the user-logon trigger rather than leaving the service disabled.
    if ([string]$_.Exception.Message -notmatch 'Access is denied|アクセスが拒否|0x80070005') { throw }
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($Triggers[1]) -Principal $Principal -Settings $Settings -Description $Description -Force -ErrorAction Stop | Out-Null
    Write-Warning 'Boot trigger requires an elevated PowerShell. User-logon auto-start installed; rerun this installer elevated to add AtStartup.'
}
Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
Write-Output ("Startup trigger installed: " + $BootTriggerInstalled)
for ($i = 0; $i -lt 40; $i++) {
    if (Get-ClaudeMcpListenerPid) {
        Write-Output 'Claude MCP server is listening on 127.0.0.1:8766.'
        Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
        return
    }
    Start-Sleep -Milliseconds 500
}
throw 'Claude MCP task did not start. Inspect .local\claude-code-mcp\claude-gateway-server.log.'
