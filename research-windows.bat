@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "OKX_RESEARCH_LAUNCHER=%~f0"
set "OKX_RESEARCH_PAYLOAD=%~dp0research-windows.ps1"
powershell.exe -NoProfile -Command "try { & ([ScriptBlock]::Create([IO.File]::ReadAllText($env:OKX_RESEARCH_PAYLOAD))) } catch { Write-Host 'Research launcher could not start. Extract the entire updated project.'; exit 1 }"
set "OKX_RESEARCH_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Historical research ended. Exit code: %OKX_RESEARCH_EXIT_CODE%.
pause
exit /b %OKX_RESEARCH_EXIT_CODE%
