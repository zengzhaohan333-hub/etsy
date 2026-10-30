# This trusted local file is read by both portfolio BAT launchers as UTF-8 text.
# Registry values and paths are data, never executable code. Credentials stay in Python.

function Get-HttpProxyEndpoint {
    param([string] $ProxyServer)

    if ([string]::IsNullOrWhiteSpace($ProxyServer)) {
        throw 'The Windows manual proxy has no server. Configure its address and port in Windows Settings > Network & Internet > Proxy.'
    }
    $endpoint = $ProxyServer.Trim()
    if ($endpoint.Contains('=')) {
        $servers = @{}
        foreach ($entry in $endpoint.Split(';')) {
            $entry = $entry.Trim()
            if (-not $entry) { continue }
            if ($entry -notmatch '^(http|https|ftp|socks)=(.+)$') {
                throw 'The Windows manual proxy format is unsupported. Use host:port or http=host:port;https=host:port.'
            }
            $protocol = $Matches[1].ToLowerInvariant()
            if ($servers.ContainsKey($protocol)) {
                throw 'The Windows manual proxy contains duplicate protocol entries. Correct it in Windows Proxy settings.'
            }
            $servers[$protocol] = $Matches[2].Trim()
        }
        if ($servers.ContainsKey('https')) {
            $endpoint = $servers['https']
        } elseif ($servers.ContainsKey('http')) {
            $endpoint = $servers['http']
        } else {
            throw 'A SOCKS/FTP-only proxy is unsupported. Configure a Windows manual HTTP proxy that supports HTTPS CONNECT.'
        }
    }

    # Do not accept user info, credentials, paths, query strings, whitespace or
    # shell metacharacters. HTTPS destination entries still use an HTTP proxy.
    if ($endpoint -notmatch '^(?:http://)?(?<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?):(?<port>[0-9]{1,5})$') {
        throw 'The proxy must be a credential-free HTTP host:port. SOCKS, HTTPS proxy URLs, IPv6 literals and proxy URLs with credentials or paths are unsupported.'
    }
    $hostName = $Matches['host']
    $port = [int] $Matches['port']
    if ($port -lt 1 -or $port -gt 65535 -or $hostName.Length -gt 253) {
        throw 'The Windows manual proxy has an invalid hostname or port. Correct it in Windows Proxy settings.'
    }
    $address = $null
    if ($hostName -match '^[0-9.]+$') {
        if ($hostName -notmatch '^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$' -or
            -not [Net.IPAddress]::TryParse($hostName, [ref] $address) -or
            $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
            $address.Equals([Net.IPAddress]::Any)) {
            throw 'The Windows manual proxy has an invalid IPv4 address. Use a normal dotted IPv4 address or DNS hostname.'
        }
    } else {
        foreach ($label in $hostName.Split('.')) {
            if ($label -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$') {
                throw 'The Windows manual proxy has an invalid DNS hostname. Correct it in Windows Proxy settings.'
            }
        }
    }
    $loopback = $hostName.Equals('localhost', [StringComparison]::OrdinalIgnoreCase)
    if ($null -ne $address) {
        $loopback = [Net.IPAddress]::IsLoopback($address)
    }
    return [pscustomobject] @{ HostName = $hostName; Port = $port; Loopback = $loopback }
}

try {
    # Both launchers set this process-only selector explicitly. Never accept an
    # arbitrary argument string or let an inherited value change normal startup.
    if ($env:OKX_PORTFOLIO_RECONCILE -notin @('0', '1')) {
        throw 'Invalid portfolio launcher mode. Use connect-okx-portfolio.bat or reconcile-okx-portfolio.bat from the updated project.'
    }
    $reconcile = $env:OKX_PORTFOLIO_RECONCILE -eq '1'
    if ($reconcile) {
        Write-Host '正在准备组合订单日志核对，请稍候…' -ForegroundColor Cyan
    } else {
        Write-Host '正在准备四币组合模拟盘向导，请稍候…' -ForegroundColor Cyan
    }
    $projectPath = [IO.Path]::GetDirectoryName($env:OKX_PORTFOLIO_LAUNCHER)
    Set-Location -LiteralPath $projectPath -ErrorAction Stop
    if (-not (Test-Path -LiteralPath (Join-Path $projectPath 'okx_bot\connect.py') -PathType Leaf)) {
        throw 'The credential wizard is missing. Extract the entire updated project, then run this launcher from that folder.'
    }
    $wslCommand = Get-Command 'wsl.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $wslCommand) {
        throw 'WSL was not found. Install WSL with Ubuntu, then see DESKTOP_GUIDE.md. This launcher needs no administrator privileges.'
    }
    $wslExe = $wslCommand.Source

    try {
        $settings = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
    } catch {
        throw 'Cannot read your Windows manual proxy settings. Check Windows Settings > Network & Internet > Proxy.'
    }
    if ($settings.ProxyEnable -ne 1) {
        throw 'The Windows manual proxy is disabled. Enable Use a proxy server in Windows Proxy settings. PAC/automatic setup is not used by this launcher.'
    }
    $proxy = Get-HttpProxyEndpoint -ProxyServer ([string] $settings.ProxyServer)
    $proxyHost = $proxy.HostName

    if ($proxy.Loopback) {
        # WSL localhost is not Windows localhost in this user's tested route.
        # Read the gateway again on every launch; never save a stale WSL IP.
        $routeLines = & $wslExe --exec ip -4 route show default 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw 'Cannot start the default WSL distribution or read its route. Open Ubuntu once, then retry.'
        }
        $gateway = $null
        foreach ($line in $routeLines) {
            if ($line -match '^\s*default\s+via\s+(?<gateway>(?:[0-9]{1,3}\.){3}[0-9]{1,3})(?:\s|$)') {
                $gateway = $Matches['gateway']
                break
            }
        }
        $gatewayAddress = $null
        if (-not $gateway -or -not [Net.IPAddress]::TryParse($gateway, [ref] $gatewayAddress) -or
            $gatewayAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
            [Net.IPAddress]::IsLoopback($gatewayAddress) -or $gatewayAddress.Equals([Net.IPAddress]::Any)) {
            throw 'No usable WSL IPv4 default gateway was found. Open Ubuntu and check: ip -4 route show default. This launcher will not guess an IP.'
        }
        $proxyHost = $gateway
    }

    $proxyUrl = 'http://{0}:{1}' -f $proxyHost, $proxy.Port
    if ($reconcile) {
        Write-Host '核对 BTC / ETH / SOL / XRP 的模拟盘订单及本地恢复日志。' -ForegroundColor Cyan
        Write-Host '仅查询交易所；核对通过后更新本地日志，不提交订单、不取消挂单或平仓。'
        Write-Host 'API 密钥由 Python 隐藏输入。此入口不会启动自动交易，无需输入 DEMO。'
        Write-Host '已有仓位或订单结果仍不明确时，日志会保留保护状态。请保留窗口结果。'
    } else {
        Write-Host '欧易组合模拟盘：BTC / ETH / SOL / XRP，5 分钟 K 线。' -ForegroundColor Cyan
        Write-Host '每币名义仓位上限 200 USDT，组合名义仓位上限 800 USDT。'
        Write-Host '仅使用模拟资金。API 密钥由 Python 隐藏输入，不会写入此启动器。'
        Write-Host '向导先检查账户；只有您随后输入完整 DEMO，才会启动自动模拟交易。'
        Write-Host '请保持窗口开启。自动执行不保证盈利，停止后请核对模拟盘持仓。'
    }
    Write-Host ''

    # Array arguments are passed as data. Do not use a shell command string,
    # Invoke-Expression, bash -c, or PowerShell interpolation of the project path.
    $arguments = @(
        '--cd', $projectPath, '--exec', 'env',
        ('https_proxy=' + $proxyUrl), ('http_proxy=' + $proxyUrl),
        ('HTTPS_PROXY=' + $proxyUrl), ('HTTP_PROXY=' + $proxyUrl),
        'python3', '-m', 'okx_bot.connect', '--portfolio'
    )
    if ($reconcile) { $arguments += '--reconcile' }
    & $wslExe @arguments
    $wizardExitCode = $LASTEXITCODE
    if ($null -eq $wizardExitCode) { $wizardExitCode = 1 }
    exit $wizardExitCode
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
