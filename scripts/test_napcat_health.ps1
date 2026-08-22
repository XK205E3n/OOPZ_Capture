param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$gatewayExe = Join-Path $projectRoot '.venv\Scripts\oopz-onebot.exe'

if (-not (Test-Path -LiteralPath $gatewayExe -PathType Leaf)) {
    throw "OneBot gateway executable not found: $gatewayExe"
}

# A listening WebSocket port is not sufficient: NapCat can leave OneBot
# listening while the QQ account itself is offline.  `diagnose` performs
# acknowledged OneBot calls including get_login_info, so success proves both
# the service and the account are usable at the instant of the restart.
$listener = Get-NetTCPConnection -State Listen -LocalPort 3001 -ErrorAction SilentlyContinue
if (-not $listener) {
    Write-Output '{"healthy":false,"reason":"onebot_port_not_listening"}'
    exit 1
}

Push-Location $projectRoot
try {
    $diagnostic = & $gatewayExe diagnose 2>&1
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    $detail = ($diagnostic | Out-String).Trim()
    if ($detail.Length -gt 1000) {
        $detail = $detail.Substring(0, 1000)
    }
    [pscustomobject]@{
        healthy = $false
        reason = 'onebot_login_diagnose_failed'
        detail = $detail
    } | ConvertTo-Json -Compress
    exit 1
}

[pscustomobject]@{
    healthy = $true
    reason = 'onebot_login_confirmed'
} | ConvertTo-Json -Compress
exit 0
