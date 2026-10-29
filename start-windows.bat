@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where wsl.exe >nul 2>&1
if errorlevel 1 (
    echo WSL was not found. See DESKTOP_GUIDE.md for Windows setup.
    pause
    exit /b 1
)

wsl.exe --exec python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Python 3.11+ is required inside the default WSL distribution.
    echo See DESKTOP_GUIDE.md. Ubuntu 24.04 includes a suitable Python version.
    pause
    exit /b 1
)

echo Starting LOCAL SIMULATION ONLY. No API keys are required.
echo After the server starts, open 127.0.0.1:8765 in your browser.
echo Keep this window open. Press Ctrl+C to stop the server.
wsl.exe --cd "%~dp0." --exec python3 -m okx_bot web
set "BOT_EXIT_CODE=%ERRORLEVEL%"
echo Server stopped. Exit code: %BOT_EXIT_CODE%
pause
exit /b %BOT_EXIT_CODE%
