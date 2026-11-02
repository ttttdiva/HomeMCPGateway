param(
    [int]$Port = 8766,
    [string]$StateRoot
)
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Entry = Join-Path $PSScriptRoot 'gateway_muse_http.py'
if (-not $StateRoot) { $StateRoot = Join-Path $Root '.local\claude-code-mcp' }
if (-not [IO.Path]::IsPathRooted($StateRoot)) { throw 'StateRoot must be absolute.' }
$StateRoot = [IO.Path]::GetFullPath($StateRoot)
$Secrets = Join-Path $StateRoot 'secrets'
$Audit = Join-Path $StateRoot 'claude-gateway-audit.jsonl'
$Log = Join-Path $StateRoot 'claude-gateway-server.log'
if ($Port -ne 8766) { throw 'The Claude MCP server uses reserved port 8766.' }
foreach ($file in @($Python,$Entry,(Join-Path $Secrets 'path.secret'),(Join-Path $Secrets 'token.secret'))) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'Claude MCP startup prerequisite is missing.'
    }
}
Set-Location -LiteralPath $Root
. (Join-Path $PSScriptRoot 'http_gateway_console.ps1')
$ConsoleArgs = @{
    Label = 'Claude Code MCP'
    Port = $Port
    Python = $Python
    Entry = $Entry
    ServerArguments = @('--prefix', '/claude-mcp', '--secret-dir', $Secrets, '--audit', $Audit, '--host', '127.0.0.1', '--port', "$Port")
    Log = $Log
}
Invoke-HomeMcpHttpConsole @ConsoleArgs
