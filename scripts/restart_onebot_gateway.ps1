param(
    [ValidateSet('startup', 'restarted')]
    [string]$Lifecycle = 'startup',
    [switch]$SkipNotification
)

$ErrorActionPreference = 'Stop'
$projectRootFull = Split-Path -Parent $PSScriptRoot
$gatewayExe = Join-Path $projectRootFull '.venv\Scripts\oopz-onebot.exe'
$stateRoot = Join-Path $projectRootFull 'controller_state'
$lifecycleMarker = Join-Path $stateRoot 'gateway_startup_lifecycle.txt'

if (-not (Test-Path -LiteralPath $gatewayExe -PathType Leaf)) {
    throw "OneBot gateway executable not found: $gatewayExe"
}
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
Set-Content -LiteralPath $lifecycleMarker -Value $Lifecycle -NoNewline -Encoding ascii

for ($pass = 1; $pass -le 2; $pass++) {
    $processIds = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains($gatewayExe)
    } | Select-Object -ExpandProperty ProcessId)
    foreach ($processId in ($processIds | Sort-Object -Descending)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    if ($processIds.Count -eq 0) {
        break
    }
    Start-Sleep -Milliseconds 500
}

$process = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/k', ('"' + $gatewayExe + '" serve')) -WorkingDirectory $projectRootFull -WindowStyle Normal -PassThru
if ($Lifecycle -eq 'restarted') {
    if ($SkipNotification) {
        Start-Sleep -Seconds 1
        Remove-Item -LiteralPath $lifecycleMarker -Force -ErrorAction SilentlyContinue
        [pscustomobject]@{ gateway_process_id = $process.Id; notification_skipped = $true } | ConvertTo-Json -Compress
        exit 0
    }
    $delivered = $false
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        Start-Sleep -Seconds 1
        & $gatewayExe notify-admin --lifecycle restarted 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $delivered = $true
            break
        }
    }
    if (-not $delivered) {
        throw 'Restart-complete administrator notice could not be delivered.'
    }
    Remove-Item -LiteralPath $lifecycleMarker -Force -ErrorAction SilentlyContinue
}
[pscustomobject]@{ gateway_process_id = $process.Id } | ConvertTo-Json -Compress
