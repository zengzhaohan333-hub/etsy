@echo off
title OKX Demo Monitor - Read Only
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "OKX_MONITOR_LAUNCHER=%~f0"
set "OKX_MONITOR_PAYLOAD_FILE=%~dp0monitor-windows.ps1"
powershell.exe -STA -NoProfile -Command "try { & ([ScriptBlock]::Create([IO.File]::ReadAllText($env:OKX_MONITOR_PAYLOAD_FILE))) } catch { Write-Host 'Monitor could not start. Extract the entire updated project. The trading robot is unaffected.'; exit 1 }"
set "OKX_MONITOR_EXIT=%ERRORLEVEL%"
echo.
echo Monitor exited with code %OKX_MONITOR_EXIT%. The trading robot is unaffected.
pause
exit /b %OKX_MONITOR_EXIT%
