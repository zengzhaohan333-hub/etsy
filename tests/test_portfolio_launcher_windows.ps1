# Windows PowerShell 5.1 integration test. No real WSL, network or API keys.
# Run: powershell.exe -NoProfile -File tests/test_portfolio_launcher_windows.ps1
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$proxySubkey = 'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$proxyValueNames = @('ProxyEnable', 'ProxyServer')

function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { throw $Message }
}

function Read-ProxySnapshot {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($proxySubkey)
    $snapshot = @{ KeyExists = ($null -ne $key); Values = @{} }
    try {
        foreach ($name in $proxyValueNames) {
            $exists = ($null -ne $key) -and ($key.GetValueNames() -contains $name)
            $value = $null
            $kind = $null
            if ($exists) {
                $value = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                $kind = $key.GetValueKind($name)
            }
            $snapshot['Values'][$name] = @{ Exists = $exists; Value = $value; Kind = $kind }
        }
        return $snapshot
    } finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

function Assert-ProxyUnchanged {
    param([hashtable] $Expected)
    $actual = Read-ProxySnapshot
    Assert-True ($actual['KeyExists'] -eq $Expected['KeyExists']) 'Launcher changed the proxy registry key.'
    foreach ($name in $proxyValueNames) {
        $before = $Expected['Values'][$name]
        $after = $actual['Values'][$name]
        Assert-True ($before.Exists -eq $after.Exists) "Launcher changed presence of $name."
        if ($before.Exists) {
            Assert-True ($before.Kind -eq $after.Kind) "Launcher changed registry kind of $name."
            # Compare in memory, never print the saved user proxy settings.
            $beforeJson = ConvertTo-Json -InputObject $before.Value -Compress
            $afterJson = ConvertTo-Json -InputObject $after.Value -Compress
            Assert-True ($beforeJson -ceq $afterJson) "Launcher changed value of $name."
        }
    }
}

function Set-ProxyFixture {
    param([int] $Enabled, [AllowNull()][object] $Server)
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($proxySubkey)
    try {
        $key.SetValue('ProxyEnable', $Enabled, [Microsoft.Win32.RegistryValueKind]::DWord)
        if ($null -eq $Server) {
            $key.DeleteValue('ProxyServer', $false)
        } else {
            $key.SetValue('ProxyServer', $Server, [Microsoft.Win32.RegistryValueKind]::String)
        }
    } finally { $key.Dispose() }
}

function Restore-ProxySnapshot {
    param([hashtable] $Snapshot)
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($proxySubkey)
    try {
        foreach ($name in $proxyValueNames) {
            $saved = $Snapshot['Values'][$name]
            if ($saved.Exists) {
                $key.SetValue($name, $saved.Value, $saved.Kind)
            } else {
                $key.DeleteValue($name, $false)
            }
        }
    } finally { $key.Dispose() }
    if (-not $Snapshot['KeyExists']) {
        # Only remove an originally absent key if it remains otherwise empty.
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($proxySubkey)
        try { $empty = $key.ValueCount -eq 0 -and $key.SubKeyCount -eq 0 }
        finally { $key.Dispose() }
        if ($empty) { [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKey($proxySubkey, $false) }
    }
}

function Invoke-Launcher {
    param([string] $BatchPath, [int] $PortfolioExit)
    Remove-Item -LiteralPath $env:OKX_PORTFOLIO_TEST_CAPTURE -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $env:OKX_PORTFOLIO_TEST_CALLS -Force -ErrorAction SilentlyContinue
    $env:OKX_PORTFOLIO_TEST_EXIT = [string] $PortfolioExit
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $env:ComSpec
    # The path is cmd.exe's quoted /c command argument, never PowerShell code.
    $start.Arguments = '/d /s /c ""' + $BatchPath + '""'
    $start.WorkingDirectory = [IO.Path]::GetDirectoryName($BatchPath)
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        [void] $process.Start()
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        # Blank input only lets the batch final pause exit; never feed API keys
        # or a DEMO acknowledgement into the product. Python is never executed.
        $process.StandardInput.Write("`r`n`r`n")
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(30000)) {
            # Kill only this test-owned process tree if a batch pause gets stuck.
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
            throw 'Launcher test timed out.'
        }
        $process.WaitForExit()
        return @{
            ExitCode = $process.ExitCode
            Output = $output.GetAwaiter().GetResult() + $errors.GetAwaiter().GetResult()
        }
    } finally { $process.Dispose() }
}

function Assert-LaunchArguments {
    param([string] $ProjectPath, [string] $ProxyUrl, [bool] $ExpectRoute = $true, [bool] $ExpectReconcile = $false)
    Assert-True ([IO.File]::Exists($env:OKX_PORTFOLIO_TEST_CAPTURE)) 'Fake WSL never received the portfolio wizard invocation.'
    $actual = [IO.File]::ReadAllLines($env:OKX_PORTFOLIO_TEST_CAPTURE)
    $expected = @('--cd', $ProjectPath, '--exec', 'env',
        ('https_proxy=' + $ProxyUrl), ('http_proxy=' + $ProxyUrl),
        ('HTTPS_PROXY=' + $ProxyUrl), ('HTTP_PROXY=' + $ProxyUrl),
        'python3', '-m', 'okx_bot.connect', '--portfolio')
    if ($ExpectReconcile) { $expected += '--reconcile' }
    Assert-True ($actual.Count -eq $expected.Count) 'WSL received an unexpected argument count.'
    for ($i = 0; $i -lt $expected.Count; $i++) {
        Assert-True ($actual[$i] -ceq $expected[$i]) "WSL argument $i was split, changed or injected."
    }
    $calls = [IO.File]::ReadAllLines($env:OKX_PORTFOLIO_TEST_CALLS)
    if ($ExpectRoute) {
        Assert-True ($calls.Count -eq 2) 'Expected exactly one route lookup and one portfolio wizard invocation.'
        Assert-True ($calls[0] -ceq 'route' -and $calls[1] -ceq 'portfolio') 'Unexpected WSL invocation order.'
    } else {
        Assert-True ($calls.Count -eq 1 -and $calls[0] -ceq 'portfolio') 'A nonloopback proxy must not query or rewrite the WSL gateway.'
    }
}

$temporary = Join-Path ([IO.Path]::GetTempPath()) ('okx-portfolio-launcher-test-' + [Guid]::NewGuid().ToString('N'))
$originalProxy = Read-ProxySnapshot
$savedEnvironment = @{}
foreach ($name in @('PATH', 'OKX_PORTFOLIO_TEST_CAPTURE', 'OKX_PORTFOLIO_TEST_CALLS', 'OKX_PORTFOLIO_TEST_EXIT', 'OKX_PORTFOLIO_TEST_ROUTE', 'OKX_PORTFOLIO_RECONCILE')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

try {
    $source = Join-Path $PSScriptRoot '..\connect-okx-portfolio.bat'
    Assert-True (Test-Path -LiteralPath $source -PathType Leaf) 'connect-okx-portfolio.bat is missing.'
    $project = Join-Path $temporary "project with spaces'and&symbols"
    $binaryDirectory = Join-Path $temporary 'fake-bin'
    [void] [IO.Directory]::CreateDirectory((Join-Path $project 'okx_bot'))
    [void] [IO.Directory]::CreateDirectory($binaryDirectory)
    [IO.File]::WriteAllText((Join-Path $project 'okx_bot\connect.py'), '# Deliberately never executed by this test.')
    $companionSource = Join-Path $PSScriptRoot '..\connect-okx-portfolio.ps1'
    Assert-True (Test-Path -LiteralPath $companionSource -PathType Leaf) 'connect-okx-portfolio.ps1 is missing.'
    $companionPath = Join-Path $project 'connect-okx-portfolio.ps1'
    $companionText = [IO.File]::ReadAllText($companionSource).Replace("`r`n", "`n").Replace("`n", "`r`n")
    [IO.File]::WriteAllText($companionPath, $companionText, [Text.UTF8Encoding]::new($false))
    $launcher = Join-Path $project 'connect-okx-portfolio.bat'
    $sourceText = [IO.File]::ReadAllText($source).Replace("`r`n", "`n").Replace("`n", "`r`n")
    [IO.File]::WriteAllText($launcher, $sourceText, [Text.UTF8Encoding]::new($false))
    $recoverySource = Join-Path $PSScriptRoot '..\reconcile-okx-portfolio.bat'
    Assert-True (Test-Path -LiteralPath $recoverySource -PathType Leaf) 'reconcile-okx-portfolio.bat is missing.'
    $recoveryLauncher = Join-Path $project 'reconcile-okx-portfolio.bat'
    $recoveryText = [IO.File]::ReadAllText($recoverySource).Replace("`r`n", "`n").Replace("`n", "`r`n")
    [IO.File]::WriteAllText($recoveryLauncher, $recoveryText, [Text.UTF8Encoding]::new($false))

    # This executable has no process/network APIs: every invocation terminates
    # here, so neither an installed WSL nor Python can receive the test command.
    $fakeSource = @'
using System;
using System.IO;
using System.Text;
public static class FakeWslForPortfolioLauncherTest {
    public static int Main(string[] args) {
        string calls = Environment.GetEnvironmentVariable("OKX_PORTFOLIO_TEST_CALLS");
        if (String.Join(" ", args) == "--exec ip -4 route show default") {
            File.AppendAllText(calls, "route\n", new UTF8Encoding(false));
            string route = Environment.GetEnvironmentVariable("OKX_PORTFOLIO_TEST_ROUTE");
            Console.WriteLine(String.IsNullOrEmpty(route) ? "default via 172.31.42.1 dev eth0" : route);
            return 0;
        }
        string capture = Environment.GetEnvironmentVariable("OKX_PORTFOLIO_TEST_CAPTURE");
        File.AppendAllLines(capture, args, new UTF8Encoding(false));
        bool portfolio = args.Length >= 4 && args[0] == "--cd" && args[2] == "--exec" && args[3] == "env";
        File.AppendAllText(calls, portfolio ? "portfolio\n" : "unexpected\n", new UTF8Encoding(false));
        int code;
        return portfolio && Int32.TryParse(Environment.GetEnvironmentVariable("OKX_PORTFOLIO_TEST_EXIT"), out code) ? code : 99;
    }
}
'@
    $fakeWsl = Join-Path $binaryDirectory 'wsl.exe'
    # Compile in a short-lived PowerShell process so its loaded assembly cannot
    # keep wsl.exe locked when this test removes its temporary directory.
    $sourcePath = Join-Path $temporary 'FakeWsl.cs'
    $compilePath = Join-Path $temporary 'compile-fake.ps1'
    [IO.File]::WriteAllText($sourcePath, $fakeSource)
    $compiler = @'
param([string] $SourcePath, [string] $OutputPath)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition ([IO.File]::ReadAllText($SourcePath)) -Language CSharp -OutputAssembly $OutputPath -OutputType ConsoleApplication
'@
    [IO.File]::WriteAllText($compilePath, $compiler)
    & powershell.exe -NoProfile -File $compilePath -SourcePath $sourcePath -OutputPath $fakeWsl
    Assert-True ($LASTEXITCODE -eq 0 -and [IO.File]::Exists($fakeWsl)) 'Cannot compile fake WSL executable.'
    $env:PATH = $binaryDirectory + ';' + $env:PATH
    $env:OKX_PORTFOLIO_TEST_CAPTURE = Join-Path $temporary 'arguments.txt'
    $env:OKX_PORTFOLIO_TEST_CALLS = Join-Path $temporary 'calls.txt'
    $env:OKX_PORTFOLIO_TEST_ROUTE = $null
    # Launching the wizard must not reset existing sessions or recovery journals.
    $runtimeSentinels = @{}
    foreach ($directory in @('.runtime', '.runtime-demo', '.runtime-backtest')) {
        $runtimeDirectory = Join-Path $project $directory
        [void] [IO.Directory]::CreateDirectory($runtimeDirectory)
        $sentinel = Join-Path $runtimeDirectory 'status.json'
        $contents = '{"sentinel":"' + $directory + '","equity":"5000"}'
        [IO.File]::WriteAllText($sentinel, $contents, [Text.UTF8Encoding]::new($false))
        $runtimeSentinels[$sentinel] = $contents
    }
    $journalPath = Join-Path $project '.runtime-demo\demo-journal.json'
    $journalContents = '{"orders":[{"client_id":"fixture","status":"unresolved"}]}'
    [IO.File]::WriteAllText($journalPath, $journalContents, [Text.UTF8Encoding]::new($false))
    $runtimeSentinels[$journalPath] = $journalContents
    $resolvedWsl = (Get-Command wsl.exe -CommandType Application | Select-Object -First 1).Source
    Assert-True ($resolvedWsl -ieq $fakeWsl) 'Fake WSL must be first on PATH; refusing to test against real WSL.'

    Set-ProxyFixture -Enabled 1 -Server '127.0.0.1:10090'
    $fixture = Read-ProxySnapshot
    # A parent's recovery setting must never turn the normal entry into recovery.
    $env:OKX_PORTFOLIO_RECONCILE = '1'
    $result = Invoke-Launcher -BatchPath $launcher -PortfolioExit 0
    Assert-True ($result.ExitCode -eq 0) ('Bare proxy launch failed: ' + $result.Output)
    Assert-LaunchArguments -ProjectPath $project -ProxyUrl 'http://172.31.42.1:10090'
    Assert-ProxyUnchanged $fixture
    Write-Host 'PASS: bare loopback proxy, quoted project path, one portfolio wizard invocation.'
    Assert-True ($env:OKX_PORTFOLIO_RECONCILE -ceq '1') 'Normal launcher leaked its mode change into the parent environment.'
    Write-Host 'PASS: normal entry ignores an inherited recovery mode.'

    # The explicit recovery entry must likewise override an inherited normal mode.
    $env:OKX_PORTFOLIO_RECONCILE = '0'
    $result = Invoke-Launcher -BatchPath $recoveryLauncher -PortfolioExit 0
    Assert-True ($result.ExitCode -eq 0) ('CRLF recovery launch failed: ' + $result.Output)
    Assert-LaunchArguments -ProjectPath $project -ProxyUrl 'http://172.31.42.1:10090' -ExpectReconcile $true
    Assert-ProxyUnchanged $fixture
    Assert-True ($env:OKX_PORTFOLIO_RECONCILE -ceq '0') 'Recovery launcher leaked its mode change into the parent environment.'
    Write-Host 'PASS: CRLF recovery entry adds only --reconcile and ignores an inherited normal mode.'

    # Exercise both LF copies and the attribute-defined CRLF form, so
    # alternate source exports remain usable as well.
    $launcherText = [IO.File]::ReadAllText($launcher).Replace("`r`n", "`n")
    [IO.File]::WriteAllText($launcher, $launcherText, [Text.UTF8Encoding]::new($false))
    $recoveryText = [IO.File]::ReadAllText($recoveryLauncher).Replace("`r`n", "`n")
    [IO.File]::WriteAllText($recoveryLauncher, $recoveryText, [Text.UTF8Encoding]::new($false))
    $companionText = [IO.File]::ReadAllText($companionPath).Replace("`r`n", "`n")
    [IO.File]::WriteAllText($companionPath, $companionText, [Text.UTF8Encoding]::new($false))
    Set-ProxyFixture -Enabled 1 -Server 'http=127.0.0.1:10080;https=localhost:10090'
    $fixture = Read-ProxySnapshot
    $env:OKX_PORTFOLIO_RECONCILE = '1'
    $result = Invoke-Launcher -BatchPath $launcher -PortfolioExit 7
    Assert-True ($result.ExitCode -eq 7) ('Portfolio wizard exit code was not propagated: ' + $result.Output)
    Assert-LaunchArguments -ProjectPath $project -ProxyUrl 'http://172.31.42.1:10090'
    Assert-ProxyUnchanged $fixture
    Write-Host 'PASS: LF BAT/PS1, HTTPS proxy selection and nonzero portfolio wizard exit propagation.'

    $env:OKX_PORTFOLIO_RECONCILE = '0'
    $result = Invoke-Launcher -BatchPath $recoveryLauncher -PortfolioExit 7
    Assert-True ($result.ExitCode -eq 7) ('Recovery exit code was not propagated: ' + $result.Output)
    Assert-LaunchArguments -ProjectPath $project -ProxyUrl 'http://172.31.42.1:10090' -ExpectReconcile $true
    Assert-ProxyUnchanged $fixture
    Assert-True ($env:OKX_PORTFOLIO_RECONCILE -ceq '0') 'LF recovery launcher leaked a mode change into the parent environment.'
    Write-Host 'PASS: LF recovery BAT/PS1, fixed recovery arguments and nonzero exit propagation.'


    Set-ProxyFixture -Enabled 1 -Server 'http://proxy.example.test:10091'
    $fixture = Read-ProxySnapshot
    $result = Invoke-Launcher -BatchPath $launcher -PortfolioExit 0
    Assert-True ($result.ExitCode -eq 0) ('Nonloopback proxy launch failed: ' + $result.Output)
    Assert-LaunchArguments -ProjectPath $project -ProxyUrl 'http://proxy.example.test:10091' -ExpectRoute $false
    Assert-ProxyUnchanged $fixture
    Write-Host 'PASS: configured nonloopback HTTP proxy is preserved without a route lookup.'

    Set-ProxyFixture -Enabled 1 -Server '127.0.0.1:10090'
    $fixture = Read-ProxySnapshot
    $env:OKX_PORTFOLIO_TEST_ROUTE = 'default dev eth0'
    $result = Invoke-Launcher -BatchPath $launcher -PortfolioExit 0
    Assert-True ($result.ExitCode -ne 0) 'An unusable WSL default route was accepted.'
    Assert-True (-not [IO.File]::Exists($env:OKX_PORTFOLIO_TEST_CAPTURE)) 'An unusable default route invoked the portfolio wizard.'
    $calls = [IO.File]::ReadAllLines($env:OKX_PORTFOLIO_TEST_CALLS)
    Assert-True ($calls.Count -eq 1 -and $calls[0] -ceq 'route') 'An unusable route must stop before invoking Python.'
    Assert-ProxyUnchanged $fixture
    $env:OKX_PORTFOLIO_TEST_ROUTE = $null
    Write-Host 'PASS: an unusable WSL gateway fails without a guessed address or portfolio wizard invocation.'

    foreach ($case in @(
        @{ Name = 'disabled manual proxy'; Enabled = 0; Server = '127.0.0.1:10090' },
        @{ Name = 'missing proxy server'; Enabled = 1; Server = $null },
        @{ Name = 'credentialed proxy'; Enabled = 1; Server = 'http://fake-user:fake-pass@127.0.0.1:10090' },
        @{ Name = 'SOCKS-only proxy'; Enabled = 1; Server = 'socks=127.0.0.1:10090' }
    )) {
        Set-ProxyFixture -Enabled $case.Enabled -Server $case.Server
        $fixture = Read-ProxySnapshot
        $result = Invoke-Launcher -BatchPath $launcher -PortfolioExit 0
        Assert-True ($result.ExitCode -ne 0) ('Invalid configuration was accepted: ' + $case.Name)
        Assert-True (-not [IO.File]::Exists($env:OKX_PORTFOLIO_TEST_CAPTURE)) ('Invalid configuration invoked the portfolio wizard: ' + $case.Name)
        Assert-True (-not $result.Output.Contains('fake-user') -and -not $result.Output.Contains('fake-pass')) 'Launcher echoed proxy credentials.'
        Assert-ProxyUnchanged $fixture
        Write-Host ('PASS: ' + $case.Name + ' is rejected without invoking Python.')
    }

    foreach ($sentinel in $runtimeSentinels.Keys) {
        Assert-True ([IO.File]::Exists($sentinel)) 'Portfolio launcher removed an existing runtime state or journal.'
        Assert-True ([IO.File]::ReadAllText($sentinel) -ceq $runtimeSentinels[$sentinel]) 'Portfolio launcher changed an existing runtime state or journal.'
    }
    Assert-True (-not [IO.Directory]::Exists((Join-Path $project '.runtime-portfolio'))) 'Portfolio launcher created a separate runtime directory instead of preserving the demo journal.'
    Write-Host 'PASS: portfolio launcher preserves runtime states and the existing demo recovery journal.'
} finally {
    try { Restore-ProxySnapshot $originalProxy }
    finally {
        foreach ($name in $savedEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
        }
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Recurse -Force
        }
    }
}

Assert-ProxyUnchanged $originalProxy
foreach ($name in $savedEnvironment.Keys) {
    $restored = [Environment]::GetEnvironmentVariable($name, 'Process')
    Assert-True ($restored -ceq $savedEnvironment[$name]) "Process environment variable $name was not restored."
}
Write-Host 'PASS: original proxy registry settings and process environment restored.'
