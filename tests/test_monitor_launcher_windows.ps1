# Windows PowerShell integration: fake WSL only, no network, keys or proxy writes.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { throw $Message }
}
function Invoke-Fixture {
    param([string] $Batch)
    Remove-Item -LiteralPath $env:OKX_MONITOR_TEST_CAPTURE -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $env:OKX_MONITOR_TEST_TRANSLATE -Force -ErrorAction SilentlyContinue
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $env:ComSpec
    $start.Arguments = '/d /s /c ""' + $Batch + '""'
    $start.WorkingDirectory = [IO.Path]::GetDirectoryName($Batch)
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
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
            throw 'Monitor fixture timed out.'
        }
        $process.WaitForExit()
        [void] $output.GetAwaiter().GetResult()
        [void] $errors.GetAwaiter().GetResult()
        return $process.ExitCode
    } finally { $process.Dispose() }
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('okx-monitor-test-' + [Guid]::NewGuid().ToString('N'))
$names = @('PATH', 'OKX_MONITOR_STATUS_FILE', 'OKX_MONITOR_TEST_CAPTURE', 'OKX_MONITOR_TEST_TRANSLATE', 'OKX_MONITOR_TEST_FAIL', 'OKX_MONITOR_TEST_EXIT')
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $project = Join-Path $temporary "monitor with spaces'and&symbols"
    $binary = Join-Path $temporary 'fake-bin'
    $oldState = Join-Path $temporary "old robot's & folder\.runtime-demo\status.json"
    [void] [IO.Directory]::CreateDirectory((Join-Path $project 'okx_bot'))
    [void] [IO.Directory]::CreateDirectory($binary)
    [void] [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($oldState))
    [IO.File]::WriteAllText($oldState, '{"mode":"demo","fixture":true}')
    [IO.File]::WriteAllText((Join-Path $project 'okx_bot\monitor.py'), '# fixture')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '..\monitor-windows.ps1') -Destination (Join-Path $project 'monitor-windows.ps1')
    $env:OKX_MONITOR_STATUS_FILE = $oldState
    $env:OKX_MONITOR_TEST_CAPTURE = Join-Path $temporary 'capture.txt'
    $env:OKX_MONITOR_TEST_TRANSLATE = Join-Path $temporary 'translate.txt'
    $env:OKX_MONITOR_TEST_FAIL = '0'
    $env:OKX_MONITOR_TEST_EXIT = '0'
    $env:PATH = $binary + ';' + $saved['PATH']
    $code = @'
using System;
using System.IO;
public class MonitorFakeWsl {
    public static int Main(string[] args) {
        if (args.Length == 4 && args[0] == "--exec" && args[1] == "wslpath" && args[2] == "-u") {
            File.WriteAllLines(Environment.GetEnvironmentVariable("OKX_MONITOR_TEST_TRANSLATE"), args);
            if (Environment.GetEnvironmentVariable("OKX_MONITOR_TEST_FAIL") == "1") return 9;
            Console.WriteLine("/mnt/fake/old robot's & folder/.runtime-demo/status.json");
            return 0;
        }
        if (args.Length == 10 && args[0] == "--cd" && args[5] == "okx_bot.monitor") {
            File.WriteAllLines(Environment.GetEnvironmentVariable("OKX_MONITOR_TEST_CAPTURE"), args);
            return Int32.Parse(Environment.GetEnvironmentVariable("OKX_MONITOR_TEST_EXIT"));
        }
        return 11;
    }
}
'@
    Add-Type -TypeDefinition $code -OutputAssembly (Join-Path $binary 'wsl.exe') -OutputType ConsoleApplication
    $source = [IO.File]::ReadAllText((Join-Path $PSScriptRoot '..\monitor-windows.bat'))
    $normalized = $source.Replace("`r`n", "`n")
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $batch = Join-Path $project 'monitor-windows.bat'
    foreach ($ending in @('LF', 'CRLF')) {
        $text = $normalized
        if ($ending -eq 'CRLF') { $text = $normalized.Replace("`n", "`r`n") }
        [IO.File]::WriteAllText($batch, $text, $encoding)
        foreach ($expectedExit in @(0, 17)) {
            $env:OKX_MONITOR_TEST_EXIT = [string] $expectedExit
            $result = Invoke-Fixture $batch
            Assert-True ($result -eq $expectedExit) 'Monitor exit code was not propagated.'
            $translation = [IO.File]::ReadAllLines($env:OKX_MONITOR_TEST_TRANSLATE)
            Assert-True ($translation[3] -ceq $oldState) 'Selected status path was changed or injected.'
            $actual = [IO.File]::ReadAllLines($env:OKX_MONITOR_TEST_CAPTURE)
            $expected = @('--cd', $project, '--exec', 'python3', '-m', 'okx_bot.monitor', '--status-file', "/mnt/fake/old robot's & folder/.runtime-demo/status.json", '--port', '8766')
            Assert-True ($actual.Count -eq $expected.Count) 'Unexpected monitor arguments.'
            for ($i = 0; $i -lt $expected.Count; $i++) {
                Assert-True ($actual[$i] -ceq $expected[$i]) "Monitor argument $i was split or changed."
            }
            Assert-True ([IO.File]::ReadAllText($oldState) -ceq '{"mode":"demo","fixture":true}') 'Monitor changed existing robot state.'
        }
        $env:OKX_MONITOR_TEST_FAIL = '1'
        Assert-True ((Invoke-Fixture $batch) -eq 1) 'Invalid WSL path conversion was not rejected.'
        Assert-True (-not [IO.File]::Exists($env:OKX_MONITOR_TEST_CAPTURE)) 'Monitor started after failed path conversion.'
        $env:OKX_MONITOR_TEST_FAIL = '0'
        $env:OKX_MONITOR_STATUS_FILE = Join-Path $temporary 'missing-status.json'
        Assert-True ((Invoke-Fixture $batch) -eq 1) 'Invalid selected file was not rejected.'
        Assert-True (-not [IO.File]::Exists($env:OKX_MONITOR_TEST_TRANSLATE)) 'WSL was called for invalid file.'
        $env:OKX_MONITOR_STATUS_FILE = $oldState
    }
    Write-Host 'Windows monitor launcher integration passed.'
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
