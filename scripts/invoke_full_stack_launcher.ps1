param(
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$candidates = @(Get-ChildItem -LiteralPath $projectRoot -Filter '*.bat' -File | Where-Object {
    (Get-Content -LiteralPath $_.FullName -Raw) -match 'OOPZ full-stack launcher'
})
if ($candidates.Count -ne 1) {
    throw 'Could not locate exactly one OOPZ full-stack launcher batch file.'
}

if ($Restart) {
    & $candidates[0].FullName restarted reuse-session
} else {
    & $candidates[0].FullName
}
exit $LASTEXITCODE
