$ErrorActionPreference = 'Stop'
$TaskName = 'Home MCP Edge CDP Broker'
$ModuleArgumentSets = @(
    '-m home_mcp_gateway.edge_cdp_broker --serve',
    '-m home_mcp_gateway.edge_cdp_broker --serve --dedicated-profile'
)

$task = Get-ScheduledTask -TaskName 'Home MCP Edge CDP Broker' -ErrorAction SilentlyContinue
if ($task) {
    $actions = @($task.Actions)
    if ($actions.Count -ne 1) {
        throw 'The named task does not match the old Edge broker; no changes were made.'
    }

    $action = $actions[0]
    $sameExecutable = [IO.Path]::GetFileName([string]$action.Execute) -ieq 'pythonw.exe'
    $arguments = ([string]$action.Arguments).Trim()
    if (-not ($sameExecutable -and ($arguments -in $ModuleArgumentSets))) {
        throw 'The named task does not match the old Edge broker; no changes were made.'
    }

    if ([string]$task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
    }
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
}

$brokerProcesses = Get-CimInstance -ClassName Win32_Process | Where-Object {
    [string]$_.Name -ieq 'pythonw.exe' -and
    [string]$_.CommandLine -and
    ([string]$_.CommandLine).IndexOf('home_mcp_gateway.edge_cdp_broker', [StringComparison]::OrdinalIgnoreCase) -ge 0
}
foreach ($process in $brokerProcesses) {
    Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
}

[pscustomobject]@{
    TaskName = $TaskName
    TaskFound = [bool]$task
    TaskDisabled = [bool]$task
    BrokerProcessesStopped = @($brokerProcesses).Count
}
