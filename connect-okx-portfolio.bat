@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "OKX_PORTFOLIO_RECONCILE=0"
set "OKX_PORTFOLIO_LAUNCHER=%~f0"
set "OKX_PORTFOLIO_PAYLOAD=%~dp0connect-okx-portfolio.ps1"
powershell.exe -NoProfile -Command "try { & ([ScriptBlock]::Create([IO.File]::ReadAllText($env:OKX_PORTFOLIO_PAYLOAD))) } catch { Write-Host 'Portfolio demo launcher could not start. Extract the entire updated project.'; exit 1 }"
set "OKX_PORTFOLIO_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Portfolio demo wizard ended. Exit code: %OKX_PORTFOLIO_EXIT_CODE%.
pause
exit /b %OKX_PORTFOLIO_EXIT_CODE%
