param(
    [int]$Port = 8765,
    [string]$StateRoot
)

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Entry = Join-Path $PSScriptRoot 'gateway_muse_http.py'
if (-not $StateRoot) {
    $StateRoot = Join-Path $Root '.local\muse-grok-native\mcp'
}
if (-not [IO.Path]::IsPathRooted($StateRoot)) {
    throw 'Muse Gateway state directory must be an absolute path.'
}
$StateRoot = [IO.Path]::GetFullPath($StateRoot)
$SecretsRoot = Join-Path $StateRoot 'secrets'
$AuditPath = Join-Path $StateRoot 'muse-full-gateway-audit.jsonl'
$ServerLog = Join-Path $StateRoot 'muse-full-gateway-server.log'

foreach ($RequiredPath in @($Python, $Entry, (Join-Path $SecretsRoot 'path.secret'), (Join-Path $SecretsRoot 'token.secret'))) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw 'Muse Gateway startup prerequisite is missing.'
    }
}
if (-not (Test-Path -LiteralPath $StateRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
}
if ($Port -lt 1 -or $Port -gt 65535) {
    throw 'Invalid Muse Gateway port.'
}

Set-Location -LiteralPath $Root
. (Join-Path $PSScriptRoot 'http_gateway_console.ps1')
$ConsoleArgs = @{
    Label = 'Muse MCP'
    Port = $Port
    Python = $Python
    Entry = $Entry
    ServerArguments = @('--secret-dir', $SecretsRoot, '--audit', $AuditPath, '--host', '127.0.0.1', '--port', "$Port")
    Log = $ServerLog
}
Invoke-HomeMcpHttpConsole @ConsoleArgs
