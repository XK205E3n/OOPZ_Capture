param(
    [string]$InstallRoot = 'C:\OOPZ',
    [switch]$DiagnoseOnly
)
$ErrorActionPreference = 'Stop'

function Assert-OopzPlainDirectory {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Expected a normal directory, not a link: $Path" }
}

function Test-OopzRecoveryPython {
    param([string]$Python)
    & $Python -I -c "import sys,struct,sysconfig; print(sys.executable); print(sys.version); print(sysconfig.get_platform()); assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 and sysconfig.get_platform()=='win-amd64', 'Expected CPython 3.12 Windows x64'"
    if ($LASTEXITCODE -ne 0) { throw 'Unexpected Python version/platform. No release directory was moved.' }
}

function Invoke-OopzDependencyProbe {
    param([string]$Python)
    Test-OopzRecoveryPython $Python
    & $Python -m pip --version
    if ($LASTEXITCODE -ne 0) { throw 'pip is not usable in the failed environment.' }
    $pipArgs = @('-m','pip','install','--dry-run','--ignore-installed','--no-deps','--only-binary=:all:','--index-url','https://pypi.org/simple','--timeout','120','--retries','3','numpy>=1.26,<3')
    & $Python @pipArgs
    if ($LASTEXITCODE -ne 0) { throw 'Official PyPI did not yield a compatible NumPy candidate. No directory was moved. Keep the error output for network/platform diagnosis.' }
}

function Test-OopzReleaseBusy {
    param([string]$Path)
    $active = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.CommandLine -and $_.CommandLine.IndexOf($Path, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    return $active.Count -gt 0
}

function Invoke-OopzOriginalInstaller {
    param([string]$ScriptPath, [string]$Artifact, [string]$Root, [string]$Python)
    $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$ScriptPath,'-Artifact',$Artifact,'-InstallRoot',$Root,'-PythonExe',$Python)
    & powershell.exe @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed again. The previous failed directory remains in failed-installs; retain this new error output.' }
}

function Invoke-OopzDependencyRecovery {
    param([string]$Root, [switch]$ProbeOnly)
    $Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    Assert-OopzPlainDirectory $Root
    if (Get-Item -LiteralPath (Join-Path $Root 'current') -Force -ErrorAction SilentlyContinue) { throw 'This helper is for first-install dependency failures only. An existing current path must be inspected separately.' }
    $inputs = Get-Content -LiteralPath (Join-Path $Root 'artifacts\deployment-inputs.json') -Raw | ConvertFrom-Json
    if ($inputs.ReleaseId -ne 'v0.11.9-5769293b3236' -or $inputs.Commit -ne '5769293b3236460d24bc0553561fa3ac68ae79be') { throw 'This recovery helper targets the verified v0.11.9 first-install failure only.' }
    $artifact = [IO.Path]::GetFullPath($inputs.Artifact)
    $artifactPrefix = (Join-Path $Root 'artifacts').TrimEnd('\') + '\'
    if (-not $artifact.StartsWith($artifactPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Artifact must remain inside this installation root.' }
    $expected = '31f349ebd021af2d416d99157c7cfca96f324c9cc8cd5c6f53d4f6777b27e4de'
    if ((Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw 'Official release ZIP hash mismatch.' }
    $sidecar = ((Get-Content -LiteralPath "$artifact.sha256" -Raw).Trim() -split '\s+')[0]
    if ($sidecar -ne $expected) { throw 'Release checksum file mismatch.' }
    $releases = Join-Path $Root 'releases'
    Assert-OopzPlainDirectory $releases
    $failed = [IO.Path]::GetFullPath((Join-Path $releases $inputs.ReleaseId))
    if (-not $failed.StartsWith($releases.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Release path escaped the intended directory.' }
    Assert-OopzPlainDirectory $failed
    $python = Join-Path $failed '.venv\Scripts\python.exe'
    $basePython = Join-Path $Root 'tools\Python312\python.exe'
    foreach ($file in @($python,$basePython,(Join-Path $Root 'shared\config\.env'))) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required file missing: $file" }
    }
    Test-OopzRecoveryPython $basePython
    if (Test-OopzReleaseBusy $failed) { throw 'A process still references the failed release. No process was stopped and no directory was moved.' }

    # Snapshot process-only pip overrides; never print their values (URLs may contain credentials).
    $saved = @{}
    [Environment]::GetEnvironmentVariables('Process').GetEnumerator() | Where-Object { $_.Key -like 'PIP_*' } | ForEach-Object { $saved[$_.Key] = $_.Value }
    Write-Host ('Temporary pip overrides present: ' + (($saved.Keys | Sort-Object) -join ', '))
    try {
        foreach ($name in @($saved.Keys)) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        # Lowercase 'nul' matches Python's os.devnull on Windows and disables all pip.ini files.
        $env:PIP_CONFIG_FILE = 'nul'
        $env:PIP_INDEX_URL = 'https://pypi.org/simple'
        $env:PIP_TIMEOUT = '120'
        $env:PIP_RETRIES = '5'
        $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
        $env:PIP_CACHE_DIR = Join-Path $Root ('installer-cache\pip-recovery-' + [guid]::NewGuid().ToString('N'))
        Invoke-OopzDependencyProbe $python
        if ($ProbeOnly) { Write-Host 'Diagnosis passed; no release directory was moved.'; return }
        if (Get-Item -LiteralPath (Join-Path $Root 'current') -Force -ErrorAction SilentlyContinue) { throw 'A current path appeared during diagnosis. No directory was moved.' }
        if (Test-OopzReleaseBusy $failed) { throw 'Release became active; refusing to move it.' }
        Assert-OopzPlainDirectory $releases
        Assert-OopzPlainDirectory $failed
        Assert-OopzPlainDirectory (Join-Path $Root 'admin')
        $backupRoot = Join-Path $Root 'failed-installs'
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        Assert-OopzPlainDirectory $backupRoot
        $backup = [IO.Path]::GetFullPath((Join-Path $backupRoot ($inputs.ReleaseId + '-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))))
        if (-not $backup.StartsWith($backupRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Backup path escaped the intended directory.' }
        # Same installation root / volume: move the directory entry, do not traverse or delete shared junction targets.
        Move-Item -LiteralPath $failed -Destination $backup
        Write-Host "Previous failed installation preserved at $backup"
        $scriptPath = Join-Path $Root 'admin\install_release.ps1'
        # Restore the exact installer from the hash-verified ZIP, never modify program files in releases.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($artifact)
        try {
            $entry = $zip.GetEntry('scripts/install_release.ps1')
            if (-not $entry) { throw 'Official installer missing from release ZIP.' }
            $inputStream = $entry.Open(); $outputStream = [IO.File]::Create($scriptPath)
            try { $inputStream.CopyTo($outputStream) } finally { $inputStream.Dispose(); $outputStream.Dispose() }
        } finally { $zip.Dispose() }
        Invoke-OopzOriginalInstaller $scriptPath $artifact $Root $basePython
    } finally {
        [Environment]::GetEnvironmentVariables('Process').GetEnumerator() | Where-Object { $_.Key -like 'PIP_*' } | ForEach-Object { [Environment]::SetEnvironmentVariable($_.Key, $null, 'Process') }
        foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-OopzDependencyRecovery $InstallRoot -ProbeOnly:$DiagnoseOnly }
