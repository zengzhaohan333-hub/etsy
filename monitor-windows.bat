@echo off
title OKX Demo Monitor - Read Only
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "OKX_MONITOR_LAUNCHER=%~f0"
powershell.exe -STA -NoProfile -Command "try { $text = [IO.File]::ReadAllText($env:OKX_MONITOR_LAUNCHER); $marker = '# OKX_MONITOR_PAYLOAD'; $offset = $text.LastIndexOf($marker, [StringComparison]::Ordinal); if ($offset -lt 0) { throw 'Monitor payload is missing.' }; & ([ScriptBlock]::Create($text.Substring($offset + $marker.Length))) } catch { Write-Host 'Monitor could not start. The trading robot is unaffected.'; exit 1 }"
set "OKX_MONITOR_EXIT=%ERRORLEVEL%"
echo.
echo Monitor exited with code %OKX_MONITOR_EXIT%. The trading robot is unaffected.
pause
exit /b %OKX_MONITOR_EXIT%

# OKX_MONITOR_PAYLOAD
try {
    $projectPath = [IO.Path]::GetDirectoryName($env:OKX_MONITOR_LAUNCHER)
    Set-Location -LiteralPath $projectPath -ErrorAction Stop
    if (-not (Test-Path -LiteralPath (Join-Path $projectPath 'okx_bot\monitor.py') -PathType Leaf)) {
        throw 'Monitor module is missing.'
    }
    $wslCommand = Get-Command 'wsl.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $wslCommand) { throw 'WSL is missing.' }
    $wslExe = $wslCommand.Source
    Write-Host '量化实验室：只读连接正在运行的欧易模拟盘。' -ForegroundColor Cyan
    Write-Host '不需要 API 密钥，不会启动新机器人或提交订单。'
    Write-Host '请选择当前机器人目录中的 .runtime-demo\status.json；可以选择旧版本文件夹。'
    $selectedPath = $env:OKX_MONITOR_STATUS_FILE
    if ([string]::IsNullOrWhiteSpace($selectedPath)) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        try {
            $dialog.Title = '选择正在运行机器人的 .runtime-demo 中的 status.json'
            $dialog.Filter = '模拟盘状态文件 (status.json)|status.json'
            $dialog.CheckFileExists = $true
            $ownDirectory = Join-Path $projectPath '.runtime-demo'
            if (Test-Path -LiteralPath $ownDirectory -PathType Container) {
                $dialog.InitialDirectory = $ownDirectory
            } else {
                $dialog.InitialDirectory = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Downloads'
            }
            if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
                Write-Host '已取消网页监控，现有机器人不受影响。'
                exit 0
            }
            $selectedPath = $dialog.FileName
        } finally { $dialog.Dispose() }
    }
    $selectedPath = [IO.Path]::GetFullPath($selectedPath)
    if ([IO.Path]::GetFileName($selectedPath) -cne 'status.json' -or
        -not (Test-Path -LiteralPath $selectedPath -PathType Leaf)) {
        throw 'Select an existing status.json file.'
    }
    $converted = @(& $wslExe --exec wslpath -u $selectedPath)
    if ($LASTEXITCODE -ne 0 -or $converted.Count -ne 1 -or -not $converted[0].StartsWith('/')) {
        throw 'Cannot convert selected file path for WSL.'
    }
    Write-Host ''
    Write-Host '网页服务启动后，请打开 http://127.0.0.1:8766' -ForegroundColor Green
    Write-Host '保持原机器人窗口及代理运行。此监控窗口不需要修改代理。'
    $arguments = @('--cd', $projectPath, '--exec', 'python3', '-m', 'okx_bot.monitor',
        '--status-file', $converted[0], '--port', '8766')
    & $wslExe @arguments
    $result = $LASTEXITCODE
    if ($null -eq $result) { $result = 1 }
    exit $result
} catch {
    Write-Host '监控网页未启动。请检查 WSL、文件选择和项目是否完整解压；现有机器人不受影响。' -ForegroundColor Yellow
    exit 1
}
