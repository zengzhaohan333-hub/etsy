# Windows PowerShell 5.1 integration test. No real WSL, network or API keys.
# Run: powershell.exe -NoProfile -File tests/test_backtest_launcher_windows.ps1
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
    param([string] $BatchPath, [int] $BacktestExit)
    Remove-Item -LiteralPath $env:OKX_BACKTEST_TEST_CAPTURE -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $env:OKX_BACKTEST_TEST_CALLS -Force -ErrorAction SilentlyContinue
    $env:OKX_BACKTEST_TEST_EXIT = [string] $BacktestExit
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
    param([string] $ProjectPath, [string] $ProxyUrl, [bool] $ExpectRoute = $true)
    Assert-True ([IO.File]::Exists($env:OKX_BACKTEST_TEST_CAPTURE)) 'Fake WSL never received the backtest invocation.'
    $actual = [IO.File]::ReadAllLines($env:OKX_BACKTEST_TEST_CAPTURE)
    $expected = @('--cd', $ProjectPath, '--exec', 'env',
        ('https_proxy=' + $ProxyUrl), ('http_proxy=' + $ProxyUrl),
        ('HTTPS_PROXY=' + $ProxyUrl), ('HTTP_PROXY=' + $ProxyUrl),
        'python3', '-m', 'okx_bot.backtest', '--days', '30', '--capital', '5000', '--state-dir', '.runtime-backtest')
    Assert-True ($actual.Count -eq $expected.Count) 'WSL received an unexpected argument count.'
    for ($i = 0; $i -lt $expected.Count; $i++) {
        Assert-True ($actual[$i] -ceq $expected[$i]) "WSL argument $i was split, changed or injected."
    }
    $calls = [IO.File]::ReadAllLines($env:OKX_BACKTEST_TEST_CALLS)
    if ($ExpectRoute) {
        Assert-True ($calls.Count -eq 2) 'Expected exactly one route lookup and one backtest invocation.'
        Assert-True ($calls[0] -ceq 'route' -and $calls[1] -ceq 'backtest') 'Unexpected WSL invocation order.'
    } else {
        Assert-True ($calls.Count -eq 1 -and $calls[0] -ceq 'backtest') 'A nonloopback proxy must not query or rewrite the WSL gateway.'
    }
}

$temporary = Join-Path ([IO.Path]::GetTempPath()) ('okx-backtest-launcher-test-' + [Guid]::NewGuid().ToString('N'))
$originalProxy = Read-ProxySnapshot
$savedEnvironment = @{}
foreach ($name in @('PATH', 'OKX_BACKTEST_TEST_CAPTURE', 'OKX_BACKTEST_TEST_CALLS', 'OKX_BACKTEST_TEST_EXIT', 'OKX_BACKTEST_TEST_ROUTE')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

try {
    $source = Join-Path $PSScriptRoot '..\backtest-windows.bat'
    Assert-True (Test-Path -LiteralPath $source -PathType Leaf) 'backtest-windows.bat is missing.'
    $project = Join-Path $temporary "project with spaces'and&symbols"
    $binaryDirectory = Join-Path $temporary 'fake-bin'
    [void] [IO.Directory]::CreateDirectory((Join-Path $project 'okx_bot'))
    [void] [IO.Directory]::CreateDirectory($binaryDirectory)
    [IO.File]::WriteAllText((Join-Path $project 'okx_bot\backtest.py'), '# Deliberately never executed by this test.')
    $launcher = Join-Path $project 'backtest-windows.bat'
    $sourceText = [IO.File]::ReadAllText($source).Replace("`r`n", "`n").Replace("`n", "`r`n")
    [IO.File]::WriteAllText($launcher, $sourceText, [Text.UTF8Encoding]::new($false))

    # This executable has no process/network APIs: every invocation terminates
    # here, so neither an installed WSL nor Python can receive the test command.
    $fakeSource = @'
using System;
using System.IO;
using System.Text;
public static class FakeWslForBacktestLauncherTest {
    public static int Main(string[] args) {
        string calls = Environment.GetEnvironmentVariable("OKX_BACKTEST_TEST_CALLS");
        if (String.Join(" ", args) == "--exec ip -4 route show default") {
            File.AppendAllText(calls, "route\n", new UTF8Encoding(false));
            string route = Environment.GetEnvironmentVariable("OKX_BACKTEST_TEST_ROUTE");
            Console.WriteLine(String.IsNullOrEmpty(route) ? "default via 172.31.42.1 dev eth0" : route);
            return 0;
        }
        string capture = Environment.GetEnvironmentVariable("OKX_BACKTEST_TEST_CAPTURE");
        File.AppendAllLines(capture, args, new UTF8Encoding(false));
        bool backtest = args.Length >= 4 && args[0] == "--cd" && args[2] == "--exec" && args[3] == "env";
        File.AppendAllText(calls, backtest ? "backtest\n" : "unexpected\n", new UTF8Encoding(false));
        int code;
        return backtest && Int32.TryParse(Environment.GetEnvironmentVariable("OKX_BACKTEST_TEST_EXIT"), out code) ? code : 99;
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
    $env:OKX_BACKTEST_TEST_CAPTURE = Join-Path $temporary 'arguments.txt'
    $env:OKX_BACKTEST_TEST_CALLS = Join-Path $temporary 'calls.txt'
    $env:OKX_BACKTEST_TEST_ROUTE = $null
    # The fake process never creates a report, so successful fixtures cannot
    # satisfy the launcher's browser-open condition.
    Assert-True (-not [IO.File]::Exists((Join-Path $project '.runtime-backtest\report.html'))) 'Backtest report fixture must be absent.'
    $resolvedWsl = (Get-Command wsl.exe -CommandType Application | Select-Object -First 1).Source
    Assert-True ($resolvedWsl -ieq $fakeWsl) 'Fake WSL must be first on PATH; refusing to test against real WSL.'

    Set-ProxyFixture -Enabled 1 -Server '127.0.0.1:10090'
    $fixture = Read-ProxySnapshot
    $result = Invoke-Launcher -BatchPath $launcher -BacktestExit 0
    Assert-True ($result.ExitCode -eq 0) ('Bare proxy launch failed: ' + $result.Output)
    Assert-LaunchArguments -ProjectPath $project -ProxyUrl 'http://172.31.42.1:10090'
    Assert-ProxyUnchanged $fixture
    Write-Host 'PASS: bare loopback proxy, quoted project path, one backtest invocation.'

    # GitHub ZIP archives use the repository's LF form. Exercise that form as
    # well as the CRLF checkout so the actual download remains usable.
    $launcherText = [IO.File]::ReadAllText($launcher).Replace("`r`n", "`n")
    [IO.File]::WriteAllText($launcher, $launcherText, [Text.UTF8Encoding]::new($false))
    Set-ProxyFixture -Enabled 1 -Server 'http=127.0.0.1:10080;https=localhost:10090'
    $fixture = Read-ProxySnapshot
    $result = Invoke-Launcher -BatchPath $launcher -BacktestExit 7
    Assert-True ($result.ExitCode -eq 7) ('Backtest exit code was not propagated: ' + $result.Output)
    Assert-LaunchArguments -ProjectPath $project -ProxyUrl 'http://172.31.42.1:10090'
    Assert-ProxyUnchanged $fixture
    Write-Host 'PASS: LF archive, HTTPS proxy selection and nonzero backtest exit propagation.'


    Set-ProxyFixture -Enabled 1 -Server 'http://proxy.example.test:10091'
    $fixture = Read-ProxySnapshot
    $result = Invoke-Launcher -BatchPath $launcher -BacktestExit 0
    Assert-True ($result.ExitCode -eq 0) ('Nonloopback proxy launch failed: ' + $result.Output)
    Assert-LaunchArguments -ProjectPath $project -ProxyUrl 'http://proxy.example.test:10091' -ExpectRoute $false
    Assert-ProxyUnchanged $fixture
    Write-Host 'PASS: configured nonloopback HTTP proxy is preserved without a route lookup.'

    Set-ProxyFixture -Enabled 1 -Server '127.0.0.1:10090'
    $fixture = Read-ProxySnapshot
    $env:OKX_BACKTEST_TEST_ROUTE = 'default dev eth0'
    $result = Invoke-Launcher -BatchPath $launcher -BacktestExit 0
    Assert-True ($result.ExitCode -ne 0) 'An unusable WSL default route was accepted.'
    Assert-True (-not [IO.File]::Exists($env:OKX_BACKTEST_TEST_CAPTURE)) 'An unusable default route invoked the backtest.'
    $calls = [IO.File]::ReadAllLines($env:OKX_BACKTEST_TEST_CALLS)
    Assert-True ($calls.Count -eq 1 -and $calls[0] -ceq 'route') 'An unusable route must stop before invoking Python.'
    Assert-ProxyUnchanged $fixture
    $env:OKX_BACKTEST_TEST_ROUTE = $null
    Write-Host 'PASS: an unusable WSL gateway fails without a guessed address or backtest invocation.'

    foreach ($case in @(
        @{ Name = 'disabled manual proxy'; Enabled = 0; Server = '127.0.0.1:10090' },
        @{ Name = 'missing proxy server'; Enabled = 1; Server = $null },
        @{ Name = 'credentialed proxy'; Enabled = 1; Server = 'http://fake-user:fake-pass@127.0.0.1:10090' },
        @{ Name = 'SOCKS-only proxy'; Enabled = 1; Server = 'socks=127.0.0.1:10090' }
    )) {
        Set-ProxyFixture -Enabled $case.Enabled -Server $case.Server
        $fixture = Read-ProxySnapshot
        $result = Invoke-Launcher -BatchPath $launcher -BacktestExit 0
        Assert-True ($result.ExitCode -ne 0) ('Invalid configuration was accepted: ' + $case.Name)
        Assert-True (-not [IO.File]::Exists($env:OKX_BACKTEST_TEST_CAPTURE)) ('Invalid configuration invoked the backtest: ' + $case.Name)
        Assert-True (-not $result.Output.Contains('fake-user') -and -not $result.Output.Contains('fake-pass')) 'Launcher echoed proxy credentials.'
        Assert-ProxyUnchanged $fixture
        Write-Host ('PASS: ' + $case.Name + ' is rejected without invoking Python.')
    }
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
