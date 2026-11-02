param(
    [string]$TaskName = '80_HomeMCPGateway - Muse'
)

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Test-Path -LiteralPath (Join-Path $Root '.git') -PathType Container)) {
    throw 'Run this installer from the primary checkout after merging the change.'
}
$Launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'start_muse_gateway_http.ps1'))
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Entry = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'gateway_muse_http.py'))
$StateRoot = Join-Path $Root '.local\muse-grok-native\mcp'
$SecretsRoot = Join-Path $StateRoot 'secrets'
$PowerShell = [IO.Path]::GetFullPath((Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'))
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$LegacyLauncher = [IO.Path]::GetFullPath((Join-Path $StateRoot 'Start-MuseMcp.ps1'))
$LegacyEntry = [IO.Path]::GetFullPath((Join-Path $StateRoot 'review_mcp.py'))

foreach ($RequiredPath in @($Python, $Entry, (Join-Path $SecretsRoot 'path.secret'), (Join-Path $SecretsRoot 'token.secret'))) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw 'Muse Gateway task prerequisite is missing.'
    }
}

function Get-MuseGatewayListenerPid {
    $listener = Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if (-not $process -or $process.Name -ne 'python.exe') {
        throw 'Port 8765 is occupied by an unknown process; the task was left unchanged.'
    }
    $commandLine = [string]$process.CommandLine
    $knownGateway = $commandLine.IndexOf($Entry, [StringComparison]::OrdinalIgnoreCase) -ge 0
    $knownLegacy = $commandLine.IndexOf($LegacyEntry, [StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not ($knownGateway -or $knownLegacy)) {
        throw 'Port 8765 is occupied by an unknown process; the task was left unchanged.'
    }
    return [int]$listener.OwningProcess
}

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    $Actions = @($Existing.Actions)
    if ($Actions.Count -ne 1) {
        throw 'An existing task with this name does not match the Muse Gateway task; it was left unchanged.'
    }
    $Arguments = [string]$Actions[0].Arguments
    $KnownOldAction = $Arguments.IndexOf($LegacyLauncher, [StringComparison]::OrdinalIgnoreCase) -ge 0
    $KnownNewAction = $Arguments.IndexOf($Launcher, [StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not ($KnownOldAction -or $KnownNewAction)) {
        throw 'An existing task with this name does not match the Muse Gateway task; it was left unchanged.'
    }
}

$OldListenerPid = Get-MuseGatewayListenerPid
if ($Existing -and $Existing.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName
}
if ($OldListenerPid) {
    $StopDeadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $CurrentListenerPid = Get-MuseGatewayListenerPid
        if (-not $CurrentListenerPid) { break }
        if ($CurrentListenerPid -ne $OldListenerPid) {
            throw 'Port 8765 changed owners during task replacement; the new task was not started.'
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $StopDeadline)
    if ($CurrentListenerPid) {
        Stop-Process -Id $OldListenerPid -Force
        $StopDeadline = [DateTime]::UtcNow.AddSeconds(5)
        do {
            $CurrentListenerPid = Get-MuseGatewayListenerPid
            if (-not $CurrentListenerPid) { break }
            if ($CurrentListenerPid -ne $OldListenerPid) {
                throw 'Port 8765 changed owners during task replacement; the new task was not started.'
            }
            Start-Sleep -Milliseconds 250
        } while ([DateTime]::UtcNow -lt $StopDeadline)
        if ($CurrentListenerPid) {
            throw 'The previous Muse Gateway listener did not stop.'
        }
    }
}

$Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -StateRoot "{1}"' -f $Launcher, $StateRoot
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$Description = 'Start the authenticated full Home MCP Gateway HTTP endpoint at user logon and restart it after failures. Secrets are read from owner-only files.'

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description $Description -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$Ready = $false
for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
    if (Get-MuseGatewayListenerPid) { $Ready = $true; break }
    Start-Sleep -Milliseconds 500
}
if (-not $Ready) {
    throw 'Muse Gateway task did not start listening on 127.0.0.1:8765.'
}
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
