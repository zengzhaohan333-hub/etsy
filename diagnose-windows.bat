@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "OKX_DIAGNOSE_LAUNCHER=%~f0"
set "OKX_DIAGNOSE_PAYLOAD=%~dp0diagnose-windows.ps1"
powershell.exe -NoProfile -Command "try { & ([ScriptBlock]::Create([IO.File]::ReadAllText($env:OKX_DIAGNOSE_PAYLOAD))) } catch { Write-Host 'Network diagnostic launcher could not start. Extract the entire updated project.'; exit 1 }"
set "OKX_DIAGNOSE_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Network checks ended. Exit code: %OKX_DIAGNOSE_EXIT_CODE%.
pause
exit /b %OKX_DIAGNOSE_EXIT_CODE%
