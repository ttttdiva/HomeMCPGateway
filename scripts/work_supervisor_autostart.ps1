param(
    [Parameter(Mandatory)][ValidateSet('plan', 'install', 'status', 'disable', 'enable', 'start', 'stop', 'uninstall', 'selftest')][string]$Action,
    [string]$TaskName = 'Home MCP Work Supervisor'
)
# Logon-time recovery of the read-only work supervisor observer ONLY (not the Gateway task, not a root model).
# Same user, Interactive token, Limited, AtLogOn, IgnoreNew.  No password / S4U / SYSTEM / Highest / elevation, no
# credentials in the task: the observer reads the existing AoiTalk settings itself at run time.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Script = Join-Path $Root 'scripts\work_supervisor.py'
$CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Resolve-Sid([string]$Name) {
    if (-not $Name) { return $null }
    try { return ([Security.Principal.NTAccount]$Name).Translate([Security.Principal.SecurityIdentifier]).Value } catch { return $null }
}

function Get-Desired {
    [ordered]@{
        Execute = $Python; Arguments = ('"{0}" run' -f $Script); WorkingDirectory = $Root; Sid = $CurrentSid
        LogonType = 'Interactive'; RunLevel = 'Limited'; MultipleInstances = 'IgnoreNew'; Trigger = 'AtLogOn'
    }
}

function Get-Actual($Task) {
    $logon = @($Task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
    [ordered]@{
        Execute = [string]$Task.Actions[0].Execute; Arguments = [string]$Task.Actions[0].Arguments
        WorkingDirectory = [string]$Task.Actions[0].WorkingDirectory; Sid = (Resolve-Sid $Task.Principal.UserId)
        LogonType = [string]$Task.Principal.LogonType; RunLevel = [string]$Task.Principal.RunLevel
        MultipleInstances = [string]$Task.Settings.MultipleInstances
        Trigger = $(if ($logon.Count -eq 1 -and $Task.Triggers.Count -eq 1 -and (Resolve-Sid $logon[0].UserId) -eq (Resolve-Sid $Task.Principal.UserId)) { 'AtLogOn' } else { 'other' })
        ActionCount = @($Task.Actions).Count
    }
}

# Returns the list of mismatching fields (empty == exactly ours).  Strict: anything else is somebody else's task.
function Compare-Definition($Actual, $Desired) {
    $bad = New-Object System.Collections.Generic.List[string]
    foreach ($key in 'Execute', 'Arguments', 'WorkingDirectory') {
        if (([string]$Actual[$key]).TrimEnd('\') -ne ([string]$Desired[$key]).TrimEnd('\')) { $bad.Add($key) }
    }
    if (-not $Actual['Sid'] -or $Actual['Sid'] -ne $Desired['Sid']) { $bad.Add('Sid') }
    foreach ($key in 'LogonType', 'RunLevel', 'MultipleInstances', 'Trigger') {
        if ([string]$Actual[$key] -ne [string]$Desired[$key]) { $bad.Add($key) }
    }
    if ($Actual.Contains('ActionCount') -and $Actual['ActionCount'] -ne 1) { $bad.Add('ActionCount') }
    , $bad.ToArray()
}

function With($Base, [string]$Key, $Value) { $copy = [ordered]@{}; foreach ($k in $Base.Keys) { $copy[$k] = $Base[$k] }; $copy[$Key] = $Value; $copy }

function Write-Json($Value) { $Value | ConvertTo-Json -Depth 5 }

function Block([string]$Operation, [string]$Reason, $State) {
    Write-Json ([ordered]@{ result = 'BLOCKED'; operation = $Operation; target = $TaskName; reason = $Reason; state = $State })
    exit 2
}

function Get-Observers {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'work_supervisor\.py"?\s+run(\s|$)' } |
        ForEach-Object { [ordered]@{ pid = $_.ProcessId; parent = $_.ParentProcessId; created = $_.CreationDate.ToUniversalTime().ToString('o'); name = $_.Name } })
}

function Get-OwnTask {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return $null }
    $mismatch = Compare-Definition (Get-Actual $task) (Get-Desired)
    if ($mismatch.Count) { Block $Action 'task with this name is not this observer task' ([ordered]@{ mismatched = $mismatch }) }
    $task
}

$Desired = Get-Desired
switch ($Action) {
    'selftest' {
        $ok = [ordered]@{}
        $good = With $Desired 'Execute' $Desired.Execute
        $ok.exact = (Compare-Definition $good $Desired).Count -eq 0
        $ok.other_exe = (Compare-Definition (With $good 'Execute' 'C:\Windows\notepad.exe') $Desired) -contains 'Execute'
        $ok.other_args = (Compare-Definition (With $good 'Arguments' '"x.py" run') $Desired) -contains 'Arguments'
        $ok.other_cwd = (Compare-Definition (With $good 'WorkingDirectory' 'C:\') $Desired) -contains 'WorkingDirectory'
        $ok.other_sid = (Compare-Definition (With $good 'Sid' 'S-1-5-18') $Desired) -contains 'Sid'
        $ok.password = (Compare-Definition (With $good 'LogonType' 'Password') $Desired) -contains 'LogonType'
        $ok.s4u = (Compare-Definition (With $good 'LogonType' 'S4U') $Desired) -contains 'LogonType'
        $ok.highest = (Compare-Definition (With $good 'RunLevel' 'Highest') $Desired) -contains 'RunLevel'
        $ok.parallel = (Compare-Definition (With $good 'MultipleInstances' 'Parallel') $Desired) -contains 'MultipleInstances'
        $ok.extra_action = (Compare-Definition (With $good 'ActionCount' 2) $Desired) -contains 'ActionCount'
        $ok.no_trigger = (Compare-Definition (With $good 'Trigger' 'other') $Desired) -contains 'Trigger'
        Write-Json $ok
    }
    'plan' { Write-Json ([ordered]@{ taskName = $TaskName; desired = $Desired; gatewayTaskUntouched = 'Home MCP Gateway' }) }
    'install' {
        foreach ($path in $Python, $Script) { if (-not (Test-Path -LiteralPath $path)) { Block 'install' "missing $path" $null } }
        $existing = Get-OwnTask
        if ($existing) { Write-Json ([ordered]@{ result = 'already_installed'; state = [string]$existing.State }); break }
        $User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $act = New-ScheduledTaskAction -Execute $Python -Argument $Desired.Arguments -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
        $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
        try {
            Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trigger -Principal $principal -Settings $settings -Description 'Logon-time recovery of the read-only work supervisor observer. No credentials; reads the existing AoiTalk settings at run time.' | Out-Null
        } catch {
            Block 'install' ('registration refused: ' + $_.Exception.GetType().Name) ([ordered]@{ existing = $false })
        }
        Write-Json ([ordered]@{ result = 'installed'; state = [string](Get-ScheduledTask -TaskName $TaskName).State })
    }
    'status' {
        $task = Get-OwnTask
        $observers = Get-Observers
        if (-not $task) { Write-Json ([ordered]@{ installed = $false; observers = $observers }); break }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Json ([ordered]@{ installed = $true; state = [string]$task.State; enabled = [bool]$task.Settings.Enabled; definitionMatches = $true
                lastRunTime = $info.LastRunTime; lastTaskResult = $info.LastTaskResult; observers = $observers; actual = (Get-Actual $task) })
    }
    'disable' {
        $task = Get-OwnTask
        if (-not $task) { Block 'disable' 'task is not installed' $null }
        Disable-ScheduledTask -TaskName $TaskName | Out-Null     # no start at the next logon
        Stop-ScheduledTask -TaskName $TaskName                    # only this task's own instance
        Write-Json ([ordered]@{ result = 'disabled_and_stopped'; state = [string](Get-ScheduledTask -TaskName $TaskName).State })
    }
    'stop' {
        if (-not (Get-OwnTask)) { Block 'stop' 'task is not installed' $null }
        Stop-ScheduledTask -TaskName $TaskName
        Write-Json ([ordered]@{ result = 'stopped'; state = [string](Get-ScheduledTask -TaskName $TaskName).State })
    }
    'enable' {
        if (-not (Get-OwnTask)) { Block 'enable' 'task is not installed' $null }
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Json ([ordered]@{ result = 'enabled'; state = [string](Get-ScheduledTask -TaskName $TaskName).State })
    }
    'start' {
        $task = Get-OwnTask
        if (-not $task) { Block 'start' 'task is not installed' $null }
        if (-not $task.Settings.Enabled) { Block 'start' 'task is disabled (enable it first)' $null }
        $observers = Get-Observers
        if ($observers.Count) { Write-Json ([ordered]@{ result = 'observer_already_running'; observers = $observers }); break }   # never a second poller
        Start-ScheduledTask -TaskName $TaskName
        Write-Json ([ordered]@{ result = 'started'; state = [string](Get-ScheduledTask -TaskName $TaskName).State })
    }
    'uninstall' {
        $task = Get-OwnTask
        if (-not $task) { Write-Json ([ordered]@{ result = 'not_installed' }); break }
        Stop-ScheduledTask -TaskName $TaskName
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Json ([ordered]@{ result = 'uninstalled'; kept = 'ledger, outbox, receipts, status.json and logs are untouched' })
    }
}
