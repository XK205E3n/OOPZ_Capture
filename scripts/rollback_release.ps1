param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v[0-9A-Za-z._-]+$')]
    [string]$ReleaseId,
    [string]$InstallRoot = 'C:\OOPZ'
)

$ErrorActionPreference = 'Stop'
$installRootPath = [System.IO.Path]::GetFullPath($InstallRoot)
$releasesRoot = Join-Path $installRootPath 'releases'
$currentPath = Join-Path $installRootPath 'current'
$targetPath = Join-Path $releasesRoot $ReleaseId
if (-not (Test-Path -LiteralPath $targetPath -PathType Container)) { throw "Release not found: $targetPath" }
if (-not (Test-Path -LiteralPath (Join-Path $targetPath 'RELEASE_MANIFEST.json') -PathType Leaf)) { throw 'Target is not a valid release.' }
if (-not (Test-Path -LiteralPath $currentPath)) { throw "Current release link is missing: $currentPath" }

$currentTarget = (Get-Item -LiteralPath $currentPath -Force).Target
if ([System.IO.Path]::GetFullPath($currentTarget) -eq [System.IO.Path]::GetFullPath($targetPath)) { throw "$ReleaseId is already current." }

$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -ieq 'python.exe' -and $_.CommandLine -and $_.CommandLine -match '(?:^|\s)-m\s+oopz_capture\.feishu_cli\s+serve(?:\s|$)'
})
if ($running.Count -gt 0) {
    & (Join-Path $currentPath 'scripts\stop_oopz_full_stack.ps1') -Notice restarting
    if ($LASTEXITCODE -ne 0) { throw 'Current release did not stop cleanly.' }
}
Remove-Item -LiteralPath $currentPath -Force
New-Item -ItemType Junction -Path $currentPath -Target $targetPath | Out-Null
try {
    & (Join-Path $currentPath 'scripts\start_feishu_windows.ps1') -Lifecycle restarted
    if ($LASTEXITCODE -ne 0) { throw 'Rolled-back release failed to start.' }
}
catch {
    Remove-Item -LiteralPath $currentPath -Force
    New-Item -ItemType Junction -Path $currentPath -Target $currentTarget | Out-Null
    & (Join-Path $currentPath 'scripts\start_feishu_windows.ps1') -Lifecycle restarted
    throw
}
[pscustomobject]@{ status = 'rolled_back'; release_id = $ReleaseId; previous = $currentTarget } | ConvertTo-Json
