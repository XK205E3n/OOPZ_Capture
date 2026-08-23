param(
    [ValidateSet('shutdown', 'restarting')]
    [string]$Notice = 'shutdown'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (Test-Path -LiteralPath $python -PathType Leaf) {
    $message = if ($Notice -eq 'restarting') {
        'OOPZ 飞书机器人正在重启；恢复连接后会发送完成通知。'
    } else {
        'OOPZ 飞书机器人正在关闭。'
    }
    Push-Location $projectRoot
    try {
        & $python -m oopz_capture.feishu_cli notify $message
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Feishu lifecycle notice failed (exit code $LASTEXITCODE); continuing with shutdown."
        }
    }
    catch {
        Write-Warning "Feishu lifecycle notice failed: $($_.Exception.Message); continuing with shutdown."
    }
    finally {
        Pop-Location
    }
}

function Test-OopzFullStackProcess($Process) {
    if ($Process.ProcessId -eq $PID -or -not $Process.CommandLine) { return $false }
    if ($Process.Name -ieq 'python.exe') {
        return $Process.CommandLine -match '(?i)(?:^|\s)-m\s+oopz_capture\.feishu_cli\s+serve(?:\s|$)'
    }
    if ($Process.Name -ieq 'powershell.exe' -or $Process.Name -ieq 'pwsh.exe') {
        return $Process.CommandLine -match '(?i)(?:^|\s)-File\s+(?:"[^"]*watch_feishu_(?:runtime_status|messages)\.ps1"|[^\s"]*watch_feishu_(?:runtime_status|messages)\.ps1)(?:\s|$)'
    }
    return $false
}

$ids = @(Get-CimInstance Win32_Process | Where-Object {
    Test-OopzFullStackProcess $_
} | Select-Object -ExpandProperty ProcessId -Unique)

foreach ($processId in $ids) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1
$remaining = @(Get-CimInstance Win32_Process | Where-Object {
    Test-OopzFullStackProcess $_
} | Select-Object -ExpandProperty ProcessId -Unique)

[pscustomobject]@{
    stopped_processes = $ids
    remaining_processes = $remaining
    lifecycle_notice = $Notice
} | ConvertTo-Json -Compress
if ($remaining.Count -gt 0) { exit 1 }
