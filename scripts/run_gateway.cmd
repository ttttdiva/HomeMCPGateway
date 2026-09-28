@echo off
setlocal
cd /d "%~dp0\.."
"%CD%\.venv\Scripts\python.exe" "%CD%\scripts\gateway_stdio.py"
