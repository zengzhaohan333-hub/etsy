@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "OKX_BACKTEST_LAUNCHER=%~f0"
set "OKX_BACKTEST_PAYLOAD=%~dp0backtest-windows.ps1"
powershell.exe -NoProfile -Command "try { & ([ScriptBlock]::Create([IO.File]::ReadAllText($env:OKX_BACKTEST_PAYLOAD))) } catch { Write-Host 'Backtest launcher could not start. Extract the entire updated project.'; exit 1 }"
set "OKX_BACKTEST_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Backtest ended. Exit code: %OKX_BACKTEST_EXIT_CODE%.
pause
exit /b %OKX_BACKTEST_EXIT_CODE%
