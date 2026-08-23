param(
    [ValidateSet('started', 'restarted')]
    [string]$Lifecycle = 'started'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logRoot = Join-Path $projectRoot 'logs'
$runtimeLog = Join-Path $logRoot 'feishu_runtime.log'
$errorLog = Join-Path $logRoot 'feishu_runtime.err.log'
$messageWatcher = Join-Path $PSScriptRoot 'watch_feishu_messages.ps1'
$statusWatcher = Join-Path $PSScriptRoot 'watch_feishu_runtime_status.ps1'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python runtime is missing: $python"
}
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $runtimeLog -PathType Leaf)) {
    [System.IO.File]::WriteAllText($runtimeLog, '', [System.Text.UTF8Encoding]::new($true))
}
if (-not (Test-Path -LiteralPath $errorLog -PathType Leaf)) {
    [System.IO.File]::WriteAllText($errorLog, '', [System.Text.UTF8Encoding]::new($true))
}

# The gateway opens these files itself in append mode. Start-Process output
# redirection must not be used here because it truncates an existing file.
$env:PYTHONUTF8 = '1'
$gateway = Start-Process -FilePath $python -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -ArgumentList @(
        '-m', 'oopz_capture.feishu_cli', 'serve', '--lifecycle', $Lifecycle,
        '--runtime-log', $runtimeLog, '--error-log', $errorLog
    )

Start-Process -FilePath 'powershell.exe' -WorkingDirectory $projectRoot -WindowStyle Normal -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $messageWatcher, '-LogPath', $runtimeLog, '-ErrorLogPath', $errorLog
)
Start-Process -FilePath 'powershell.exe' -WorkingDirectory $projectRoot -WindowStyle Normal -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $statusWatcher, '-LogPath', $runtimeLog
)

Write-Output "Started gateway PID $($gateway.Id) and two visible UTF-8 monitor windows."
