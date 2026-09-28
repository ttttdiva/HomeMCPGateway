param([string]$TaskName = 'Home MCP Gateway')

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $Root '.env'))) { throw 'Configure .env first.' }
if (-not (Test-Path -LiteralPath (Join-Path $Root '.venv\Scripts\python.exe'))) { throw 'Run setup_windows.ps1 first.' }

$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Watch' -f (Join-Path $PSScriptRoot 'connect_tunnel.ps1')
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    if ($Existing.Actions.Arguments -notlike ('*' + (Join-Path $PSScriptRoot 'connect_tunnel.ps1') + '*')) {
        throw 'An unrelated task has this name. Choose another TaskName.'
    }
    Stop-ScheduledTask -TaskName $TaskName
}
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Start Home MCP Gateway at user logon and monitor/reconnect the Secure MCP Tunnel every 30 seconds. Credentials are read from the repository .env.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
