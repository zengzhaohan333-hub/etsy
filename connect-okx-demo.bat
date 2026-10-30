@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Cannot open the project folder.
    pause
    exit /b 1
)

rem Pass the folder as data, never as interpolated PowerShell source.
set "OKX_CONNECT_LAUNCHER=%~f0"
powershell.exe -NoProfile -Command "try { $text = [IO.File]::ReadAllText($env:OKX_CONNECT_LAUNCHER); $marker = '# OKX_POWERSHELL_PAYLOAD'; $offset = $text.LastIndexOf($marker, [StringComparison]::Ordinal); if ($offset -lt 0) { throw 'Launcher payload is missing. Extract a fresh copy of the project.' }; & ([ScriptBlock]::Create($text.Substring($offset + $marker.Length))) } catch { Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 }"
set "OKX_CONNECT_EXIT_CODE=%ERRORLEVEL%"
echo.
echo OKX demo connection wizard exited with code %OKX_CONNECT_EXIT_CODE%.
pause
exit /b %OKX_CONNECT_EXIT_CODE%

# OKX_POWERSHELL_PAYLOAD
# The code below is read from this launcher, not from registry values or paths.
# Proxy settings are read-only. Credentials are prompted only inside WSL Python.

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
    $projectPath = [IO.Path]::GetDirectoryName($env:OKX_CONNECT_LAUNCHER)
    Set-Location -LiteralPath $projectPath -ErrorAction Stop
    if (-not (Test-Path -LiteralPath (Join-Path $projectPath 'okx_bot\connect.py') -PathType Leaf)) {
        throw 'The credential wizard is missing. Extract the entire updated project, then run this launcher from that folder.'
    }
    $wslCommand = Get-Command 'wsl.exe' -CommandType Application -ErrorAction SilentlyContinue
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
    Write-Host 'Using the enabled Windows manual HTTP proxy for this WSL process only.'
    Write-Host 'Starting the OKX DEMO connection wizard. Credentials are entered privately inside Python.'
    Write-Host 'This does not start real-money trading. Follow the wizard prompts below.'
    Write-Host 'If connection fails, keep the proxy app running and check its existing Allow LAN/listening setting.'
    Write-Host ''

    # Array arguments are passed as data. Do not use a shell command string,
    # Invoke-Expression, bash -c, or PowerShell interpolation of the project path.
    $arguments = @(
        '--cd', $projectPath, '--exec', 'env',
        ('https_proxy=' + $proxyUrl), ('http_proxy=' + $proxyUrl),
        ('HTTPS_PROXY=' + $proxyUrl), ('HTTP_PROXY=' + $proxyUrl),
        'python3', '-m', 'okx_bot.connect'
    )
    & $wslExe @arguments
    $wizardExitCode = $LASTEXITCODE
    if ($null -eq $wizardExitCode) { $wizardExitCode = 1 }
    exit $wizardExitCode
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
