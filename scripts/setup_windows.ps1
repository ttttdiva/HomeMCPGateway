$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m venv .venv
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m venv .venv
} else {
    throw "Python 3.10+ was not found. Install Python and run this script again."
}
if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed (exit $LASTEXITCODE)." }

$Python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (exit $LASTEXITCODE)." }
& $Python -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed (exit $LASTEXITCODE)." }
& $Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Gateway tests failed (exit $LASTEXITCODE)." }

Write-Host ""
Write-Host "Setup complete."
Write-Host "MCP command: `"$Python`" `"$PSScriptRoot\gateway_stdio.py`""
