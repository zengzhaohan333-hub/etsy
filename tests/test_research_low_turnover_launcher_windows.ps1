# Windows PowerShell 5.1 integration. Test-only picker and fake WSL; no network.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { throw $Message }
}

function Write-Selection {
    param([AllowNull()][object] $Selection, [bool] $FailPicker = $false)
    $json = ConvertTo-Json -InputObject @{ selection = $Selection; failPicker = $FailPicker } -Compress
    [IO.File]::WriteAllText($env:OKX_RESEARCH_V2_TEST_SELECTION_FILE, $json, [Text.UTF8Encoding]::new($false))
}

function Invoke-Launcher {
    param([string] $BatchPath, [int] $ResearchExit = 0, [string] $ConversionMode = 'normal')
    foreach ($path in @($env:OKX_RESEARCH_V2_TEST_CAPTURE, $env:OKX_RESEARCH_V2_TEST_CALLS, $env:OKX_RESEARCH_V2_TEST_CONVERT_ARGS)) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
    $env:OKX_RESEARCH_V2_TEST_EXIT = [string] $ResearchExit
    $env:OKX_RESEARCH_V2_TEST_CONVERSION_MODE = $ConversionMode
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $env:ComSpec
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
        # Blank input releases only the batch's final pause. No Python is run.
        $process.StandardInput.Write("`r`n`r`n")
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(30000)) {
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
            throw 'Offline launcher test timed out.'
        }
        $process.WaitForExit()
        return @{ ExitCode = $process.ExitCode; Output = $output.GetAwaiter().GetResult() + $errors.GetAwaiter().GetResult() }
    } finally { $process.Dispose() }
}

function Assert-NoWsl {
    Assert-True (-not [IO.File]::Exists($env:OKX_RESEARCH_V2_TEST_CALLS)) 'Invalid selection invoked WSL.'
    Assert-True (-not [IO.File]::Exists($env:OKX_RESEARCH_V2_TEST_CAPTURE)) 'Invalid selection invoked research.'
}

function Assert-Arguments {
    param([string] $ProjectPath, [string] $DatasetPath)
    $conversion = [IO.File]::ReadAllLines($env:OKX_RESEARCH_V2_TEST_CONVERT_ARGS)
    $expectedConversion = @('--exec', 'wslpath', '-u', '--', $DatasetPath)
    Assert-True ($conversion.Count -eq $expectedConversion.Count) 'Unexpected conversion argument count.'
    for ($i = 0; $i -lt $expectedConversion.Count; $i++) {
        Assert-True ($conversion[$i] -ceq $expectedConversion[$i]) "Conversion argument $i was split, injected or changed."
    }
    $actual = [IO.File]::ReadAllLines($env:OKX_RESEARCH_V2_TEST_CAPTURE)
    $expected = @('--cd', $ProjectPath, '--exec', 'python3', '-m', 'okx_bot.research_low_turnover',
        '--dataset-dir', $env:OKX_RESEARCH_V2_TEST_WSL_PATH, '--capital', '5000', '--daily-target', '200',
        '--state-dir', '.runtime-research-v2')
    Assert-True ($actual.Count -eq $expected.Count) 'Unexpected research argument count or proxy/credential injection.'
    for ($i = 0; $i -lt $expected.Count; $i++) {
        Assert-True ($actual[$i] -ceq $expected[$i]) "Research argument $i was split, injected or changed."
    }
    $calls = [IO.File]::ReadAllLines($env:OKX_RESEARCH_V2_TEST_CALLS)
    Assert-True ($calls.Count -eq 2 -and $calls[0] -ceq 'convert' -and $calls[1] -ceq 'research') 'Expected one path conversion and one offline research process.'
}

$environmentNames = @('PATH', 'OKX_RESEARCH_V2_TEST_SELECTION_FILE', 'OKX_RESEARCH_V2_TEST_CAPTURE',
    'OKX_RESEARCH_V2_TEST_CALLS', 'OKX_RESEARCH_V2_TEST_CONVERT_ARGS', 'OKX_RESEARCH_V2_TEST_EXIT',
    'OKX_RESEARCH_V2_TEST_CONVERSION_MODE', 'OKX_RESEARCH_V2_TEST_WSL_PATH', 'OKX_RESEARCH_V2_TEST_REPORT_REQUEST')
$savedEnvironment = @{}
foreach ($name in $environmentNames) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('okx-offline-launcher-test-' + [Guid]::NewGuid().ToString('N'))

try {
    $unicodeName = ([string][char]0x5386) + [char]0x53F2
    $project = Join-Path $temporary ("project with spaces'and&" + $unicodeName)
    $dataset = Join-Path $temporary ("previous datasets'and&" + $unicodeName)
    $binaryDirectory = Join-Path $temporary 'fake-bin'
    foreach ($directory in @((Join-Path $project 'okx_bot'), $dataset, $binaryDirectory)) {
        [void] [IO.Directory]::CreateDirectory($directory)
    }
    [IO.File]::WriteAllText((Join-Path $project 'okx_bot\research_low_turnover.py'), '# This fixture is never executed.')
    $sentinels = @{}
    foreach ($symbol in @('BTC', 'ETH', 'SOL', 'XRP')) {
        $path = Join-Path $dataset ($symbol + '-USDT-SWAP.json')
        $content = '{"cached":"' + $symbol + '"}'
        [IO.File]::WriteAllText($path, $content, [Text.UTF8Encoding]::new($false))
        $sentinels[$path] = $content
    }
    foreach ($relative in @('.runtime-demo\status.json', '.runtime-demo\demo-journal.json',
        '.runtime-demo\session.json', '.runtime\status.json', '.runtime-research\report.html')) {
        $path = Join-Path $project $relative
        [void] [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
        $content = 'unchanged fixture ' + $relative
        [IO.File]::WriteAllText($path, $content, [Text.UTF8Encoding]::new($false))
        $sentinels[$path] = $content
    }

    $batchSource = Join-Path $PSScriptRoot '..\research-low-turnover-windows.bat'
    $payloadSource = Join-Path $PSScriptRoot '..\research-low-turnover-windows.ps1'
    $launcher = Join-Path $project 'research-low-turnover-windows.bat'
    $payload = Join-Path $project 'research-low-turnover-windows.ps1'
    $batchText = [IO.File]::ReadAllText($batchSource).Replace("`r`n", "`n").Replace("`n", "`r`n")
    [IO.File]::WriteAllText($launcher, $batchText, [Text.UTF8Encoding]::new($false))
    $production = [IO.File]::ReadAllText($payloadSource)
    $picker = [regex]::Matches($production, '(?s)# BEGIN HISTORY PICKER.*?# END HISTORY PICKER')
    Assert-True ($picker.Count -eq 1) 'Expected one isolated production history-picker function.'
    # Only a copied test payload substitutes the GUI. Data comes from JSON;
    # no selected path is ever interpolated into executable PowerShell source.
    $testPicker = @'
# BEGIN HISTORY PICKER
function Select-HistoryFile {
    param([string] $InitialDirectory)
    $fixture = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($env:OKX_RESEARCH_V2_TEST_SELECTION_FILE))
    if ($fixture.failPicker) { throw 'private-picker-error-not-for-console' }
    return $fixture.selection
}
# END HISTORY PICKER
'@
    $noBrowser = @'
function Invoke-Item {
    [CmdletBinding()]
    param([string] $LiteralPath)
    [IO.File]::WriteAllText($env:OKX_RESEARCH_V2_TEST_REPORT_REQUEST, $LiteralPath, [Text.UTF8Encoding]::new($false))
}
'@
    $payloadText = $noBrowser + "`r`n" + $production.Substring(0, $picker[0].Index) + $testPicker + $production.Substring($picker[0].Index + $picker[0].Length)
    $payloadText = $payloadText.Replace("`r`n", "`n").Replace("`n", "`r`n")
    [IO.File]::WriteAllText($payload, $payloadText, [Text.UTF8Encoding]::new($false))

    $fakeSource = @'
using System;
using System.IO;
using System.Text;
public static class OfflineResearchFakeWsl {
    public static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        string calls = Environment.GetEnvironmentVariable("OKX_RESEARCH_V2_TEST_CALLS");
        if (args.Length == 5 && args[0] == "--exec" && args[1] == "wslpath" && args[2] == "-u" && args[3] == "--") {
            File.AppendAllText(calls, "convert\n", new UTF8Encoding(false));
            File.WriteAllLines(Environment.GetEnvironmentVariable("OKX_RESEARCH_V2_TEST_CONVERT_ARGS"), args, new UTF8Encoding(false));
            string mode = Environment.GetEnvironmentVariable("OKX_RESEARCH_V2_TEST_CONVERSION_MODE");
            if (mode == "fail") return 9;
            if (mode == "empty") return 0;
            if (mode == "multiline") { Console.WriteLine("/mnt/c/one"); Console.WriteLine("/mnt/c/two"); return 0; }
            if (mode == "relative") { Console.WriteLine("not-an-absolute-path"); return 0; }
            Console.WriteLine(Environment.GetEnvironmentVariable("OKX_RESEARCH_V2_TEST_WSL_PATH"));
            return 0;
        }
        File.AppendAllText(calls, "research\n", new UTF8Encoding(false));
        File.WriteAllLines(Environment.GetEnvironmentVariable("OKX_RESEARCH_V2_TEST_CAPTURE"), args, new UTF8Encoding(false));
        int code;
        return Int32.TryParse(Environment.GetEnvironmentVariable("OKX_RESEARCH_V2_TEST_EXIT"), out code) ? code : 99;
    }
}
'@
    $fakeWsl = Join-Path $binaryDirectory 'wsl.exe'
    $sourceFile = Join-Path $temporary 'FakeWsl.cs'
    $compilerFile = Join-Path $temporary 'compile.ps1'
    [IO.File]::WriteAllText($sourceFile, $fakeSource)
    $compiler = @'
param([string] $SourceFile, [string] $OutputFile)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition ([IO.File]::ReadAllText($SourceFile)) -Language CSharp -OutputAssembly $OutputFile -OutputType ConsoleApplication
'@
    [IO.File]::WriteAllText($compilerFile, $compiler)
    & powershell.exe -NoProfile -File $compilerFile -SourceFile $sourceFile -OutputFile $fakeWsl
    Assert-True ($LASTEXITCODE -eq 0 -and [IO.File]::Exists($fakeWsl)) 'Cannot compile fake WSL.'
    $env:PATH = $binaryDirectory + ';' + $env:PATH
    $env:OKX_RESEARCH_V2_TEST_SELECTION_FILE = Join-Path $temporary 'selection.json'
    $env:OKX_RESEARCH_V2_TEST_CAPTURE = Join-Path $temporary 'arguments.txt'
    $env:OKX_RESEARCH_V2_TEST_CALLS = Join-Path $temporary 'calls.txt'
    $env:OKX_RESEARCH_V2_TEST_CONVERT_ARGS = Join-Path $temporary 'conversion-args.txt'
    $env:OKX_RESEARCH_V2_TEST_REPORT_REQUEST = Join-Path $temporary 'requested-report.txt'
    $env:OKX_RESEARCH_V2_TEST_WSL_PATH = "/mnt/c/offline cache'and&" + $unicodeName
    $resolvedWsl = (Get-Command wsl.exe -CommandType Application | Select-Object -First 1).Source
    Assert-True ($resolvedWsl -ieq $fakeWsl) 'Fake WSL must be first on PATH; refusing a real WSL invocation.'
    Assert-True (-not [IO.File]::Exists((Join-Path $project '.runtime-research-v2\report.html'))) 'Fake report must not exist; tests cannot open a browser.'
    $btc = Join-Path $dataset 'BTC-USDT-SWAP.json'

    Write-Selection $btc
    $result = Invoke-Launcher $launcher
    Assert-True ($result.ExitCode -eq 0) ('CRLF launch failed: ' + $result.Output)
    Assert-Arguments $project $dataset
    Write-Host 'PASS: CRLF, Unicode/metacharacter paths, one conversion and one offline research command.'

    [IO.File]::WriteAllText($launcher, $batchText.Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($payload, $payloadText.Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
    $result = Invoke-Launcher $launcher -ResearchExit 7
    Assert-True ($result.ExitCode -eq 7) ('Research exit code was not preserved: ' + $result.Output)
    Assert-Arguments $project $dataset
    Write-Host 'PASS: LF BAT/PS1 and nonzero offline-module exit propagation.'

    Write-Selection $null
    $result = Invoke-Launcher $launcher
    Assert-True ($result.ExitCode -eq 0) 'Picker cancellation must exit cleanly.'
    Assert-NoWsl

    Write-Selection (Join-Path $dataset 'ETH-USDT-SWAP.json')
    $result = Invoke-Launcher $launcher
    Assert-True ($result.ExitCode -ne 0) 'Wrong selected filename was accepted.'
    Assert-NoWsl

    Write-Selection $btc
    $missing = Join-Path $dataset 'XRP-USDT-SWAP.json'
    [IO.File]::Delete($missing)
    $result = Invoke-Launcher $launcher
    Assert-True ($result.ExitCode -ne 0) 'Incomplete four-market cache was accepted.'
    Assert-NoWsl
    [IO.File]::WriteAllText($missing, $sentinels[$missing], [Text.UTF8Encoding]::new($false))

    $locked = [IO.File]::Open((Join-Path $dataset 'ETH-USDT-SWAP.json'), [IO.FileMode]::Open,
                              [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try {
        $result = Invoke-Launcher $launcher
        Assert-True ($result.ExitCode -ne 0) 'Unreadable cache was accepted.'
        Assert-NoWsl
    } finally { $locked.Dispose() }

    Write-Selection $btc -FailPicker $true
    $result = Invoke-Launcher $launcher
    Assert-True ($result.ExitCode -ne 0) 'Picker exception was ignored.'
    Assert-True (-not $result.Output.Contains('private-picker-error-not-for-console')) 'Raw picker exception was printed.'
    Assert-NoWsl
    Write-Selection $btc
    Write-Host 'PASS: cancellation, wrong file, incomplete/unreadable data and picker failure never invoke WSL.'

    foreach ($mode in @('fail', 'empty', 'multiline', 'relative')) {
        $result = Invoke-Launcher $launcher -ConversionMode $mode
        Assert-True ($result.ExitCode -ne 0) ('Invalid conversion was accepted: ' + $mode)
        Assert-True (-not [IO.File]::Exists($env:OKX_RESEARCH_V2_TEST_CAPTURE)) 'Failed conversion started research.'
        $calls = [IO.File]::ReadAllLines($env:OKX_RESEARCH_V2_TEST_CALLS)
        Assert-True ($calls.Count -eq 1 -and $calls[0] -ceq 'convert') 'Failed conversion must not start another process.'
    }
    foreach ($path in $sentinels.Keys) {
        Assert-True ([IO.File]::Exists($path)) 'An original dataset or running-bot file was removed.'
        Assert-True ([IO.File]::ReadAllText($path) -ceq $sentinels[$path]) 'An original dataset or running-bot file was modified.'
    }
    Assert-True (-not [IO.Directory]::Exists((Join-Path $project '.runtime-research-v2'))) 'Launcher wrote runtime data outside the fake offline module.'
    Assert-True (-not [IO.File]::Exists($env:OKX_RESEARCH_V2_TEST_REPORT_REQUEST)) 'Launcher opened an old or missing report.'
    # A test-local command mock records browser intent, never opens a real GUI.
    $report = Join-Path $project '.runtime-research-v2\report.html'
    [void] [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($report))
    [IO.File]::WriteAllText($report, '<html>fixture</html>')
    $result = Invoke-Launcher $launcher
    Assert-True ($result.ExitCode -eq 0) 'A successful offline run did not complete.'
    Assert-True ([IO.File]::ReadAllText($env:OKX_RESEARCH_V2_TEST_REPORT_REQUEST) -ceq $report) 'The launcher did not choose the fixed new report.'
    [IO.File]::Delete($env:OKX_RESEARCH_V2_TEST_REPORT_REQUEST)
    $result = Invoke-Launcher $launcher -ResearchExit 7
    Assert-True ($result.ExitCode -eq 7) 'A failed offline run lost its exit status.'
    Assert-True (-not [IO.File]::Exists($env:OKX_RESEARCH_V2_TEST_REPORT_REQUEST)) 'A failed run opened a stale report.'
    Write-Host 'PASS: invalid path conversions fail closed; old sessions, caches and recovery journals are unchanged.'
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
foreach ($name in $savedEnvironment.Keys) {
    Assert-True ([Environment]::GetEnvironmentVariable($name, 'Process') -ceq $savedEnvironment[$name]) "Environment variable $name was not restored."
}
Write-Host 'PASS: process environment restored; no real WSL, accounts, network or GUI were used.'
