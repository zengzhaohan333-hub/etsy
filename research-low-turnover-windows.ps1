# Trusted local UTF-8 payload. Dataset paths are data, never executable code.
# No proxy, registry, credentials, account requests or bot controls are used.

# BEGIN HISTORY PICKER
function Select-HistoryFile {
    param([string] $InitialDirectory)
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = '选择上一轮研究缓存中的 BTC-USDT-SWAP.json'
    $dialog.Filter = 'BTC 历史缓存 (BTC-USDT-SWAP.json)|BTC-USDT-SWAP.json'
    $dialog.CheckFileExists = $true
    $dialog.CheckPathExists = $true
    $dialog.Multiselect = $false
    $dialog.RestoreDirectory = $true
    if ([IO.Directory]::Exists($InitialDirectory)) { $dialog.InitialDirectory = $InitialDirectory }
    try {
        if ($dialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) {
            return $dialog.FileName
        }
        return $null
    } finally { $dialog.Dispose() }
}
# END HISTORY PICKER

try {
    $projectPath = [IO.Path]::GetDirectoryName($env:OKX_RESEARCH_V2_LAUNCHER)
    Set-Location -LiteralPath $projectPath -ErrorAction Stop
    if (-not (Test-Path -LiteralPath (Join-Path $projectPath 'okx_bot\research_low_turnover.py') -PathType Leaf)) {
        throw 'Offline research module is missing. Extract the entire updated project before running this launcher.'
    }
    Write-Host '低换手策略研究：复用已有四币历史缓存，不重新下载行情。' -ForegroundColor Cyan
    Write-Host '请在弹出的窗口中选择上一轮 datasets 文件夹里的 BTC-USDT-SWAP.json。'
    Write-Host '初始虚拟资金 5,000 USDT；每日 200 USDT 仅作比较目标，不是收益承诺。'
    Write-Host '不需要 API 密钥，不读取账户、不下单，也不停止正在运行的机器人。'
    $selectedFile = Select-HistoryFile -InitialDirectory (Join-Path $projectPath '.runtime-research\datasets')
    if ($null -eq $selectedFile -or $selectedFile -eq '') {
        Write-Host '已取消，没有启动研究，也没有改动原程序。'
        exit 0
    }
    if ($selectedFile -isnot [string] -or
        [IO.Path]::GetFileName($selectedFile) -cne 'BTC-USDT-SWAP.json') {
        throw '请选择原缓存文件 BTC-USDT-SWAP.json，不要选择报告或其他币种文件。'
    }
    $datasetDirectory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($selectedFile))
    foreach ($symbol in @('BTC', 'ETH', 'SOL', 'XRP')) {
        $path = Join-Path $datasetDirectory ($symbol + '-USDT-SWAP.json')
        try {
            $item = Get-Item -LiteralPath $path -ErrorAction Stop
            if ($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Not a regular cached file.'
            }
            $stream = [IO.File]::OpenRead($path)
            try {
                if ($stream.Length -eq 0 -or $stream.ReadByte() -lt 0) { throw 'Empty cache.' }
            } finally { $stream.Dispose() }
        } catch {
            throw '所选目录必须同时包含可读取的 BTC、ETH、SOL、XRP 四个 JSON 缓存；请重新选择上一轮完整 datasets 文件夹。'
        }
    }
    $wslCommand = Get-Command 'wsl.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $wslCommand) { throw 'WSL was not found. This launcher requires the existing Ubuntu/WSL setup.' }
    $wslExe = $wslCommand.Source
    $converted = @(& $wslExe --exec wslpath -u -- $datasetDirectory 2>$null)
    if ($LASTEXITCODE -ne 0 -or $converted.Count -ne 1) {
        throw 'Cannot convert the selected cache folder to a WSL path. Open the existing Ubuntu once, then retry.'
    }
    $datasetWslPath = [string] $converted[0]
    if ([string]::IsNullOrWhiteSpace($datasetWslPath) -or
        -not $datasetWslPath.StartsWith('/') -or $datasetWslPath -match '[\x00-\x1f\x7f]') {
        throw 'The selected folder did not produce one valid absolute WSL path; research was not started.'
    }
    Write-Host '缓存文件已找到，正在离线比较低换手方案，请保持此窗口开启。' -ForegroundColor Cyan
    $arguments = @(
        '--cd', $projectPath, '--exec', 'python3', '-m', 'okx_bot.research_low_turnover',
        '--dataset-dir', $datasetWslPath, '--capital', '5000', '--daily-target', '200',
        '--state-dir', '.runtime-research-v2'
    )
    & $wslExe @arguments
    $researchExitCode = $LASTEXITCODE
    if ($null -eq $researchExitCode) { $researchExitCode = 1 }
    if ($researchExitCode -ne 0) {
        Write-Host '研究未完成，请保留上方提示。没有重新下载数据，也不会启动交易或自动重试。' -ForegroundColor Yellow
        exit $researchExitCode
    }
    Write-Host '离线研究已完成。历史结果仅供比较，不预测未来收益。' -ForegroundColor Green
    $reportPath = Join-Path $projectPath '.runtime-research-v2\report.html'
    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        try {
            Invoke-Item -LiteralPath $reportPath -ErrorAction Stop
        } catch {
            Write-Host '报告已生成，但未能自动打开；请在 .runtime-research-v2 文件夹中打开 report.html。' -ForegroundColor Yellow
        }
    } else {
        Write-Host '请查看上方研究结果；如已生成报告，可在 .runtime-research-v2 文件夹中打开。'
    }
    exit 0
} catch {
    # All input/I/O failures get a fixed explanation; no file contents or raw
    # path-conversion output are printed by this launcher.
    $safeMessages = @(
        'Offline research module is missing. Extract the entire updated project before running this launcher.',
        '请选择原缓存文件 BTC-USDT-SWAP.json，不要选择报告或其他币种文件。',
        '所选目录必须同时包含可读取的 BTC、ETH、SOL、XRP 四个 JSON 缓存；请重新选择上一轮完整 datasets 文件夹。',
        'WSL was not found. This launcher requires the existing Ubuntu/WSL setup.',
        'Cannot convert the selected cache folder to a WSL path. Open the existing Ubuntu once, then retry.',
        'The selected folder did not produce one valid absolute WSL path; research was not started.'
    )
    if ($safeMessages -ccontains $_.Exception.Message) {
        Write-Host $_.Exception.Message -ForegroundColor Red
    } else {
        Write-Host '无法读取所选历史缓存或启动离线研究。请保留原文件，检查完整解压目录和文件读取权限后重试。' -ForegroundColor Red
    }
    exit 1
}
