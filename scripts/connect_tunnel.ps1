param(
    [string]$TunnelId,
    [string]$Alias,
    [string]$RuntimeKeyEnv = "CONTROL_PLANE_API_KEY",
    [string]$EnvFile = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [switch]$Watch
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$SavedEnvironment = @{}

try {
    # Literal KEY=VALUE only: never execute or interpolate credential file contents.
    if (Test-Path -LiteralPath $EnvFile) {
        $LineNumber = 0
        foreach ($Line in [System.IO.File]::ReadAllLines($EnvFile)) {
            $LineNumber++
            $Entry = $Line.Trim()
            if (-not $Entry -or $Entry.StartsWith('#')) { continue }
            if ($Entry -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                throw "Invalid .env assignment at line $LineNumber."
            }
            $Name = $Matches[1]
            $Value = $Matches[2].Trim()
            if ($Value.Length -ge 2 -and (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or ($Value.StartsWith("'") -and $Value.EndsWith("'")))) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }
            if (-not $SavedEnvironment.ContainsKey($Name)) {
                $SavedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
            }
            [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
        }
    }
    if (-not $TunnelId) { $TunnelId = $env:CONTROL_PLANE_TUNNEL_ID }
    if (-not $Alias) { $Alias = $env:TUNNEL_ALIAS }
    if (-not $Alias) { $Alias = 'home-mcp' }
    if (-not $TunnelId -or $TunnelId -eq 'tunnel_replace_me') { throw "Set CONTROL_PLANE_TUNNEL_ID in .env." }
    $RuntimeKey = [Environment]::GetEnvironmentVariable($RuntimeKeyEnv, 'Process')
    if (-not $RuntimeKey -or $RuntimeKey -eq 'replace_me') { throw "Set $RuntimeKeyEnv in .env." }
    if (-not (Test-Path -LiteralPath $Python)) { throw "Run scripts\setup_windows.ps1 first." }

    $TunnelClient = $env:TUNNEL_CLIENT_PATH
    if (-not $TunnelClient) {
        $Bundled = @(Get-ChildItem -Path (Join-Path $Root 'tunnel-client-*\tunnel-client.exe') -File | Sort-Object LastWriteTime -Descending)
        if ($Bundled.Count -gt 0) { $TunnelClient = $Bundled[0].FullName }
        else {
            $Command = Get-Command tunnel-client -ErrorAction SilentlyContinue
            if ($Command) { $TunnelClient = $Command.Source }
        }
    }
    if (-not $TunnelClient -or -not (Test-Path -LiteralPath $TunnelClient)) {
        throw "Extract the tunnel-client Windows release into this repository or set TUNNEL_CLIENT_PATH in .env."
    }

    # tunnel-client parses this command with shell quoting; forward slashes avoid
    # Windows backslashes being consumed as escape characters.
    $McpCommand = '"' + $Python.Replace('\', '/') + '" "' + (Join-Path $PSScriptRoot 'gateway_stdio.py').Replace('\', '/') + '"'
    $ProfileDir = Join-Path $Root '.runtime\profiles'
    Push-Location $Root
    try {
        $Failures = 0
        $NeedsConnect = $true
        New-Item -ItemType Directory -Path (Join-Path $Root '.runtime') -Force | Out-Null
        do {
            try {
                if ($NeedsConnect) {
                    & $TunnelClient runtimes connect --alias $Alias --tunnel-id $TunnelId --profile $Alias --profile-dir $ProfileDir --runtime-api-key ("env:" + $RuntimeKeyEnv) --mcp-command $McpCommand
                    if ($LASTEXITCODE -ne 0) { throw "Tunnel connection failed (exit $LASTEXITCODE)." }
                    $NeedsConnect = $false
                }
                $StatusJson = & $TunnelClient runtimes status $Alias --json
                if ($LASTEXITCODE -ne 0) { throw "Tunnel status failed (exit $LASTEXITCODE)." }
                $Status = $StatusJson | ConvertFrom-Json
                $Status | Select-Object alias, process_running, healthy, ready, ui_url | Format-List
                $Snapshot = [ordered]@{
                    checked_at = [DateTimeOffset]::Now.ToString('o')
                    supervisor_pid = $PID
                    alias = $Alias
                    process_running = $Status.process_running
                    healthy = $Status.healthy
                    ready = $Status.ready
                    runtime_pid = $Status.process.pid
                    ui_url = $Status.ui_url
                }
                $Snapshot | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root '.runtime\supervisor-status.json') -Encoding UTF8
                if ($Status.process_running -and $Status.healthy -and $Status.ready) {
                    $Failures = 0
                } elseif (-not $Watch) {
                    throw "Tunnel is not ready. Inspect tunnel-client runtimes status $Alias and its local log."
                } else {
                    $Failures++
                    # Restart dead processes immediately; allow transient health failures.
                    if (-not $Status.process_running -or $Failures -ge 3) {
                        & $TunnelClient runtimes stop $Alias
                        if ($LASTEXITCODE -ne 0) { throw "Could not stop the unhealthy runtime." }
                        $NeedsConnect = $true
                    }
                }
            } catch {
                if (-not $Watch) { throw }
                # Do not write native output, credentials, or exception bodies to the watchdog log.
                $Note = '{0} Runtime check/connect failed; retrying in 30 seconds.' -f [DateTimeOffset]::Now.ToString('o')
                Add-Content -LiteralPath (Join-Path $Root '.runtime\supervisor.log') -Value $Note
                $NeedsConnect = $true
                Start-Sleep -Seconds 30
            }
            if ($Watch) {
                if ($NeedsConnect) { continue }
                Start-Sleep -Seconds 30
            }
        } while ($Watch)
    } finally { Pop-Location }
} finally {
    foreach ($Name in $SavedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $SavedEnvironment[$Name], 'Process')
    }
}
