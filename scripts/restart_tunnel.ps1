# Restart only the home-mcp Secure Tunnel runtime managed by this repository.
# Does not touch the dedicated AoiTalk tunnel or unrelated Gateway jobs.
param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$TaskName = '80_HomeMCPGateway - ChatGPT'
$Launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'connect_tunnel.ps1'))

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Task) {
    throw "Scheduled task '$TaskName' not found. Run scripts/install_autostart.ps1 first."
}

# Never restart an unrelated task that happens to have the same name.
$ExpectedAction = @($Task.Actions | Where-Object {
    $_.Arguments -and
    ([string]$_.Arguments).IndexOf($Launcher, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
    ([string]$_.Arguments -notmatch '(?i)(?:^|\s)-Target\s+aoitalk(?:\s|$)')
})
if ($ExpectedAction.Count -ne 1) {
    throw "Task action does not match this repository's home-mcp launcher. No action was taken."
}

$Alias = 'home-mcp'
$TunnelClient = $null
$EnvPath = Join-Path $Root '.env'
if (Test-Path -LiteralPath $EnvPath -PathType Leaf) {
    foreach ($Line in [IO.File]::ReadAllLines($EnvPath)) {
        $Entry = $Line.Trim()
        if ($Entry.StartsWith('#') -or -not $Entry) { continue }
        if ($Entry -match '^(TUNNEL_CLIENT_PATH|TUNNEL_ALIAS)=(.*)$') {
            $Name = $Matches[1]
            $Value = $Matches[2].Trim().Trim('"', "'")
            if ($Name -eq 'TUNNEL_CLIENT_PATH' -and $Value) { $TunnelClient = $Value }
            if ($Name -eq 'TUNNEL_ALIAS' -and $Value) { $Alias = $Value }
        }
    }
}
if (-not $TunnelClient) {
    $Bundled = @(Get-ChildItem -Path (Join-Path $Root 'tunnel-client-*\tunnel-client.exe') -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending)
    if ($Bundled.Count -gt 0) {
        $TunnelClient = $Bundled[0].FullName
    } else {
        $Command = Get-Command tunnel-client -ErrorAction SilentlyContinue
        if ($Command) { $TunnelClient = $Command.Source }
    }
}
if (-not $TunnelClient -or -not (Test-Path -LiteralPath $TunnelClient -PathType Leaf)) {
    throw 'tunnel-client.exe not found. Set TUNNEL_CLIENT_PATH in .env.'
}

if ($CheckOnly) {
    Write-Host ('Preflight OK: scheduled task and tunnel executable match this Gateway (alias={0}).' -f $Alias)
    return
}

$TaskStopped = $false
try {
    Write-Host "Stopping scheduled tunnel monitor: $TaskName"
    Stop-ScheduledTask -TaskName $TaskName
    $TaskStopped = $true

    for ($n = 0; $n -lt 20; $n++) {
        if ((Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') { break }
        Start-Sleep -Milliseconds 500
    }
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') {
        throw 'Scheduled tunnel monitor did not stop.'
    }

    Write-Host "Stopping tunnel runtime: $Alias"
    & $TunnelClient runtimes stop $Alias | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "tunnel-client runtimes stop failed (exit $LASTEXITCODE)." }

    Write-Host "Starting scheduled tunnel monitor: $TaskName"
    Start-ScheduledTask -TaskName $TaskName
    $TaskStopped = $false

    for ($n = 0; $n -lt 45; $n++) {
        Start-Sleep -Seconds 1
        $Json = & $TunnelClient runtimes status $Alias --json 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
        try { $Status = ($Json | Out-String | ConvertFrom-Json) } catch { continue }
        if ($Status.process_running -and $Status.healthy -and $Status.ready) {
            Write-Host 'Gateway tunnel restarted successfully: process_running=true, healthy=true, ready=true'
            if ($Status.ui_url) { Write-Host "Local status UI: $($Status.ui_url)" }
            exit 0
        }
    }
    throw "Gateway was restarted but did not reach ready state. Inspect '$Alias' runtime status."
} finally {
    if ($TaskStopped) {
        Write-Warning 'Re-enabling tunnel monitor after an incomplete restart.'
        try { Start-ScheduledTask -TaskName $TaskName } catch {
            Write-Warning 'Could not start the tunnel monitor; inspect Task Scheduler.'
        }
    }
}
