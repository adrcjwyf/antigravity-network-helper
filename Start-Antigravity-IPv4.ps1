param(
    [ValidateSet('Models', 'CLI', 'VSCode', 'Desktop', 'ResumeVSCode')]
    [string]$Target = 'Models',
    [int]$ListenPort = 0
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Application-Lifetime.ps1')
$workerFile = Join-Path $PSScriptRoot 'ipv4_worker.py'
$agyFile = Join-Path $env:USERPROFILE '.gemini\bin\agy.exe'
$codeFile = (Get-Command code.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $codeFile) {
    foreach ($installRoot in @("$env:LOCALAPPDATA\Programs", $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $installRoot) { continue }
        $candidate = Join-Path $installRoot 'Microsoft VS Code\bin\code.cmd'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $codeFile = $candidate; break }
    }
}
if ($Target -eq 'VSCode' -and -not $codeFile) { throw 'Cannot find code.cmd on PATH or in the default VS Code installation folders.' }
$desktopFile = Join-Path $env:LOCALAPPDATA 'Programs\antigravity\Antigravity.exe'
if ($Target -eq 'VSCode' -and (Get-Process -Name Code -ErrorAction SilentlyContinue)) {
    throw 'Save your work and close all VS Code windows before using this entry.'
}
if ($Target -eq 'Desktop' -and (Get-Process -Name Antigravity -ErrorAction SilentlyContinue)) {
    throw 'Close Antigravity before using this entry.'
}
if ($Target -eq 'ResumeVSCode' -and -not (Get-Process -Name Code -ErrorAction SilentlyContinue)) {
    throw 'There is no VS Code session to resume.'
}
$pythonFile = (Get-Command python -ErrorAction Stop).Source
$runFolder = Join-Path $PSScriptRoot ('.runtime\launch-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runFolder -Force | Out-Null
$readyFile = Join-Path $runFolder 'ready.json'
$stopFile = Join-Path $runFolder 'stop'
$outFile = Join-Path $runFolder 'worker.log'
$errFile = Join-Path $runFolder 'worker-error.log'
$vars = @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')
$previous = @{}
foreach ($name in $vars) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$worker = $null
$resultCode = 0
try {
    $workerArgs = @(
        '-u', ('"' + $workerFile + '"'),
        '--ready-file', ('"' + $readyFile + '"'),
        '--stop-file', ('"' + $stopFile + '"'),
        '--parent-pid', $PID, '--listen-port', $ListenPort
    )
    if ($codeFile) { $workerArgs += @('--code-cmd', ('"' + $codeFile + '"')) }
    $worker = Start-Process -FilePath $pythonFile -ArgumentList $workerArgs -WindowStyle Hidden -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $ready = $false
    Write-Host 'Checking the pinned IPv4 exit and Google TLS targets...'
    for ($attempt = 0; $attempt -lt 360; $attempt++) {
        if ($worker.HasExited) {
            $detail = Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue
            throw ('IPv4 worker stopped before startup. ' + $detail)
        }
        if (Test-Path -LiteralPath $readyFile) {
            try {
                $status = Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json
                if ($status.port -gt 0 -and $status.worker_pid -eq $worker.Id) { $ready = $true; break }
            } catch { }
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'IPv4 verification timed out. Application was not started.' }
    $proxyUrl = 'http://127.0.0.1:' + $status.port
    foreach ($name in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')) {
        [Environment]::SetEnvironmentVariable($name, $proxyUrl, 'Process')
    }
    [Environment]::SetEnvironmentVariable('NO_PROXY', 'localhost,127.0.0.1,::1', 'Process')
    Write-Host ('IPv4 exit verified: ' + $status.expected_ipv4 + '. No IPv6 or direct fallback.')
    switch ($Target) {
        'Models' { & $agyFile models; $resultCode = $LASTEXITCODE }
        'CLI' { & $agyFile; $resultCode = $LASTEXITCODE }
        'VSCode' {
            # --wait follows the original window marker, which disappears on Open Folder.
            # Keep the proxy alive for the application's processes instead.
            & $codeFile --new-window
            $resultCode = $LASTEXITCODE
            if ($resultCode -eq 0) { Wait-AntigravityApplication -ProcessName Code -Worker $worker }
        }
        'ResumeVSCode' { Wait-AntigravityApplication -ProcessName Code -Worker $worker }
        'Desktop' { $app = Start-Process -FilePath $desktopFile -PassThru -Wait; $resultCode = $app.ExitCode }
    }
    if ($worker.HasExited) { throw 'IPv4 worker stopped during use. No alternate exit was used. Check ipv4-last-start.log.' }
} finally {
    foreach ($name in $vars) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    if ($worker -and -not $worker.HasExited) {
        Set-Content -LiteralPath $stopFile -Value 'stop' -Encoding ASCII
        if (-not $worker.WaitForExit(35000)) { Stop-Process -Id $worker.Id -ErrorAction SilentlyContinue }
    }
    if (Test-Path -LiteralPath $outFile) {
        Copy-Item -LiteralPath $outFile -Destination (Join-Path $PSScriptRoot 'ipv4-last-start.log') -Force
    }
    # Only these known files are removed; no recursive cleanup of user folders.
    foreach ($file in @($readyFile, $stopFile, $outFile, $errFile)) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
    }
    if (Test-Path -LiteralPath $runFolder) { Remove-Item -LiteralPath $runFolder }
}
if ($resultCode -ne 0) { throw ('Application exited with code ' + $resultCode + '. IPv4 proxy stopped; environment restored.') }
