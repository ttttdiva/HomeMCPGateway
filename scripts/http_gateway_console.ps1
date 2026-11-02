# Shared visible Windows Terminal entrypoint for Muse / Claude Code HTTP MCP.
# The Python supervisor keeps all backend stdout/stderr in separate local logs.
function Invoke-HomeMcpHttpConsole {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$Entry,
        [Parameter(Mandatory = $true)][string[]]$ServerArguments,
        [Parameter(Mandatory = $true)][string]$Log,
        [ValidateRange(5, 3600)][int]$HeartbeatSeconds = 60
    )

    $Title = "80_HomeMCPGateway - $Label (HTTP :$Port)"
    try { $Host.UI.RawUI.WindowTitle = $Title } catch {}
    $ErrorLog = [IO.Path]::ChangeExtension($Log, 'stderr.log')
    $Supervisor = Join-Path $PSScriptRoot 'http_gateway_console.py'
    if (-not (Test-Path -LiteralPath $Supervisor -PathType Leaf)) {
        throw 'Missing MCP console supervisor.'
    }
    Write-Host "=========================================================="
    Write-Host " Home MCP Gateway - $Label"
    Write-Host " Local listener: 127.0.0.1:$Port"
    Write-Host " Server log: $Log"
    Write-Host " Error log: $ErrorLog"
    Write-Host " Request paths, headers, and credentials are not displayed."
    Write-Host "=========================================================="

    & $Python $Supervisor --label $Label --port "$Port" --python $Python --entry $Entry `
        --log $Log --stderr-log $ErrorLog --heartbeat-seconds "$HeartbeatSeconds" -- @ServerArguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "$Label supervisor exited with code $ExitCode; see local error log."
    }
}
