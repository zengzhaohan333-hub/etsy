@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "OKX_RESEARCH_V2_LAUNCHER=%~f0"
set "OKX_RESEARCH_V2_PAYLOAD=%~dp0research-low-turnover-windows.ps1"
powershell.exe -NoProfile -STA -Command "try { & ([ScriptBlock]::Create([IO.File]::ReadAllText($env:OKX_RESEARCH_V2_PAYLOAD))) } catch { Write-Host 'Offline research launcher could not start. Extract the entire updated project.'; exit 1 }"
set "OKX_RESEARCH_V2_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Offline research ended. Exit code: %OKX_RESEARCH_V2_EXIT_CODE%.
pause
exit /b %OKX_RESEARCH_V2_EXIT_CODE%
