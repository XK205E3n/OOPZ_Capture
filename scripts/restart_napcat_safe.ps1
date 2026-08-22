param(
    [ValidateSet('saved', 'interactive')]
    [string]$LoginMode = 'saved'
)

$ErrorActionPreference = 'Stop'
$projectRootFull = (Split-Path -Parent $PSScriptRoot)
$napCatRoot = (Join-Path $projectRootFull 'NapCatQQ\NapCat.50969.Shell')
$startScript = Join-Path $napCatRoot 'start-napcat.bat'
$savedScript = Join-Path $PSScriptRoot 'start_napcat_saved_session.ps1'

if (-not (Test-Path -LiteralPath $napCatRoot -PathType Container)) {
    throw "NapCat directory is missing: $napCatRoot"
}
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "NapCat start script is missing: $startScript"
}

# Only processes whose executable path is physically inside the dedicated
# NapCat directory are eligible.  OOPZ controller/recording/analyzer processes
# cannot match this boundary.
$targets = @(Get-Process -Name 'NapCatWinBootMain','QQ' -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Path -and (
            $_.Path.Equals($napCatRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
            $_.Path.StartsWith($napCatRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
        )
    })
foreach ($target in ($targets | Sort-Object Id -Descending)) {
    # Stopping the dedicated parent QQ process can make its children disappear
    # before this loop reaches them. That is success, not a recovery failure.
    # Suppress only this expected race; the explicit remaining-process check
    # below is the authoritative safety check.
    Stop-Process -Id $target.Id -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$remaining = @(Get-Process -Name 'NapCatWinBootMain','QQ' -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Path -and $_.Path.StartsWith($napCatRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
    })
if ($remaining.Count -gt 0) {
    throw "NapCat restart aborted: $($remaining.Count) verified process(es) did not stop"
}

if ($LoginMode -eq 'saved') {
    if (-not (Test-Path -LiteralPath $savedScript -PathType Leaf)) {
        throw "Saved-session launcher is missing: $savedScript"
    }
    & $savedScript -NapCatDirectory $napCatRoot
}
else {
    # No quick-login variables are supplied.  The official QQ window performs
    # normal QR/device verification and the watchdog will not restart it again
    # while it waits for the user.
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/k', ('"' + $startScript + '"')) `
        -WorkingDirectory $napCatRoot -WindowStyle Normal
}

[pscustomobject]@{
    status = 'started'
    login_mode = $LoginMode
    stopped_napcat_processes = @($targets | Select-Object -ExpandProperty Id)
    oopz_processes_touched = 0
} | ConvertTo-Json -Compress
