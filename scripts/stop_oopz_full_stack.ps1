param(
    [ValidateSet('shutdown', 'restarting')]
    [string]$Notice = 'shutdown',
    [switch]$PreserveNapCat
)

$ErrorActionPreference = 'Stop'
$projectRootFull = Split-Path -Parent $PSScriptRoot
$napCatRoot = Join-Path $projectRootFull 'NapCatQQ\NapCat.50969.Shell'
$controllerPath = Join-Path $projectRootFull '.venv\Scripts\oopz-qq-controller.exe'
$gatewayPath = Join-Path $projectRootFull '.venv\Scripts\oopz-onebot.exe'
$watchdogModule = 'oopz_capture.qq_watchdog'

$noticeSent = $false
$noticeError = $null
if (Test-Path -LiteralPath $gatewayPath -PathType Leaf) {
    Push-Location $projectRootFull
    try {
        & $gatewayPath notify-admin --lifecycle $Notice 2>$null | Out-Null
        $noticeSent = $LASTEXITCODE -eq 0
        if (-not $noticeSent) {
            $noticeError = "notify-admin exit code: $LASTEXITCODE"
        }
    }
    catch {
        $noticeError = $_.Exception.Message
    }
    finally {
        Pop-Location
    }
}

# NapCat acknowledges the OneBot action before the QQ client has necessarily
# flushed the private message to Tencent. Keep the client alive briefly.
if ($noticeSent) {
    Start-Sleep -Seconds 3
}

function Get-OopzProcessIds {
    @(Get-CimInstance Win32_Process | Where-Object {
        $commandLine = $_.CommandLine
        if ($_.ProcessId -eq $PID -or -not $commandLine) {
            return $false
        }
        $isNapCat = $commandLine.Contains($napCatRoot)
        $isProjectService = (
            $commandLine.Contains($controllerPath) -or
            $commandLine.Contains($gatewayPath) -or
            $commandLine.Contains($watchdogModule)
        )
        $isProjectService -or ($isNapCat -and -not $PreserveNapCat)
    } | Select-Object -ExpandProperty ProcessId)
}

$stopped = [System.Collections.Generic.List[int]]::new()
for ($pass = 1; $pass -le 5; $pass++) {
    $ids = @(Get-OopzProcessIds | Select-Object -Unique)
    if ($ids.Count -eq 0) {
        break
    }
    foreach ($processId in ($ids | Sort-Object -Descending)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        $stopped.Add([int]$processId)
    }
    Start-Sleep -Seconds 1
}

$remaining = Get-OopzProcessIds
[pscustomobject]@{
    stopped_processes = @($stopped | Select-Object -Unique)
    remaining_processes = @($remaining)
    lifecycle_notice = $Notice
    lifecycle_notice_sent = $noticeSent
    lifecycle_notice_error = $noticeError
    napcat_preserved = [bool]$PreserveNapCat
} | ConvertTo-Json -Compress

if ($remaining.Count -gt 0) {
    exit 1
}
