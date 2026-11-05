@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart_tunnel.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
    echo Restart failed. See the error above.
) else (
    echo Gateway restart completed.
)
pause
exit /b %RESULT%
