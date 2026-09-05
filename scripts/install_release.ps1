param(
    [Parameter(Mandatory = $true)]
    [string]$Artifact,
    [string]$InstallRoot = 'C:\OOPZ',
    [string]$PythonExe = 'python.exe',
    [int]$HealthTimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
$artifactPath = [System.IO.Path]::GetFullPath($Artifact)
$installRootPath = [System.IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf)) { throw "Artifact not found: $artifactPath" }
if ([System.IO.Path]::GetExtension($artifactPath) -ne '.zip') { throw 'Artifact must be a .zip file.' }

$checksumPath = "$artifactPath.sha256"
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) { throw "Checksum file not found: $checksumPath" }
$expectedHash = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
$actualHash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expectedHash -ne $actualHash) { throw 'SHA-256 mismatch; refusing to install the artifact.' }

$releasesRoot = Join-Path $installRootPath 'releases'
$sharedRoot = Join-Path $installRootPath 'shared'
$currentPath = Join-Path $installRootPath 'current'
@($installRootPath, $releasesRoot, (Join-Path $installRootPath 'artifacts'), (Join-Path $sharedRoot 'config'),
  (Join-Path $sharedRoot 'models'), (Join-Path $sharedRoot 'output'), (Join-Path $sharedRoot 'feishu_state'),
  (Join-Path $sharedRoot 'logs')) | ForEach-Object { New-Item -ItemType Directory -Path $_ -Force | Out-Null }

$envPath = Join-Path $sharedRoot 'config\.env'
$modelPath = Join-Path $sharedRoot 'models\SenseVoiceSmall'
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { throw "Production config is missing: $envPath" }

$inspectRoot = Join-Path $installRootPath ('.inspect-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $inspectRoot | Out-Null
try {
    Expand-Archive -LiteralPath $artifactPath -DestinationPath $inspectRoot
    $manifestPath = Join-Path $inspectRoot 'RELEASE_MANIFEST.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Release manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.release_id -notmatch '^v[0-9A-Za-z._-]+$') { throw 'Release ID is invalid.' }
    $releasePath = Join-Path $releasesRoot $manifest.release_id
    if (Test-Path -LiteralPath $releasePath) { throw "Release is already installed: $($manifest.release_id)" }
    Move-Item -LiteralPath $inspectRoot -Destination $releasePath
    $inspectRoot = $null
}
finally {
    if ($inspectRoot -and (Test-Path -LiteralPath $inspectRoot)) { Remove-Item -LiteralPath $inspectRoot -Recurse -Force }
}

try {
    New-Item -ItemType HardLink -Path (Join-Path $releasePath '.env') -Target $envPath | Out-Null
    foreach ($name in @('models', 'output', 'feishu_state', 'logs')) {
        New-Item -ItemType Junction -Path (Join-Path $releasePath $name) -Target (Join-Path $sharedRoot $name) | Out-Null
    }
    $toolsNodeSource = Join-Path $sharedRoot 'tools\node'
    if (-not (Test-Path -LiteralPath (Join-Path $toolsNodeSource 'node.exe') -PathType Leaf)) {
        throw "PDF rendering Node runtime is missing: $toolsNodeSource\node.exe. See README_CLOUD_SERVER_DEPLOYMENT.md section 4."
    }
    New-Item -ItemType Junction -Path (Join-Path $releasePath 'tools\node') -Target $toolsNodeSource | Out-Null

    & $PythonExe -m venv (Join-Path $releasePath '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the release virtual environment.' }
    $releasePython = Join-Path $releasePath '.venv\Scripts\python.exe'
    & $releasePython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Could not upgrade pip.' }
    Push-Location $releasePath
    try {
        & $releasePython -m pip install -e '.[speech,feishu]'
        if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
        & $releasePython (Join-Path $releasePath 'scripts\download_sensevoice_model.py') --target $modelPath
        if ($LASTEXITCODE -ne 0) { throw 'SenseVoiceSmall download or checksum verification failed.' }
        if (-not (Test-Path -LiteralPath (Join-Path $modelPath 'model.pt') -PathType Leaf)) {
            throw "SenseVoiceSmall setup did not create the expected model: $modelPath"
        }
        & npx.cmd --yes pnpm@10.15.0 install --frozen-lockfile --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw 'Node dependency installation failed.' }
        & $releasePython -m pip freeze | Set-Content -LiteralPath (Join-Path $releasePath 'DEPLOYED_PYTHON_PACKAGES.txt') -Encoding utf8
        & $releasePython -c "import oopz_capture; import lark_oapi; import funasr; print('imports ok')"
        if ($LASTEXITCODE -ne 0) { throw 'Release import smoke test failed.' }
    }
    finally { Pop-Location }

    $previousTarget = $null
    if (Test-Path -LiteralPath $currentPath) {
        $previousTarget = (Get-Item -LiteralPath $currentPath -Force).Target
        $running = @(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -ieq 'python.exe' -and $_.CommandLine -and $_.CommandLine -match '(?:^|\s)-m\s+oopz_capture\.feishu_cli\s+serve(?:\s|$)'
        })
        if ($running.Count -gt 0) {
            & (Join-Path $currentPath 'scripts\stop_oopz_full_stack.ps1') -Notice restarting
            if ($LASTEXITCODE -ne 0) { throw 'Old release did not stop cleanly.' }
        }
        Remove-Item -LiteralPath $currentPath -Force
    }
    New-Item -ItemType Junction -Path $currentPath -Target $releasePath | Out-Null

    $runtimeLog = Join-Path $sharedRoot 'logs\feishu_runtime.log'
    $oldLogLength = if (Test-Path -LiteralPath $runtimeLog) { (Get-Item -LiteralPath $runtimeLog).Length } else { 0 }
    & (Join-Path $currentPath 'scripts\start_feishu_windows.ps1') -Lifecycle restarted
    if ($LASTEXITCODE -ne 0) { throw 'New release failed to start.' }

    $deadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
    $healthy = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Seconds 2
        $process = @(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -ieq 'python.exe' -and $_.CommandLine -and $_.CommandLine -match '(?:^|\s)-m\s+oopz_capture\.feishu_cli\s+serve(?:\s|$)'
        })
        if ($process.Count -eq 0) { break }
        if (Test-Path -LiteralPath $runtimeLog) {
            $stream = [System.IO.File]::Open($runtimeLog, 'Open', 'Read', 'ReadWrite')
            try {
                if ($stream.Length -gt $oldLogLength) {
                    $stream.Seek($oldLogLength, 'Begin') | Out-Null
                    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8, $true, 4096, $true)
                    try { $newLog = $reader.ReadToEnd() } finally { $reader.Dispose() }
                    if ($newLog -match '飞书长连接已就绪') { $healthy = $true; break }
                }
            }
            finally { $stream.Dispose() }
        }
    }
    if (-not $healthy) {
        & (Join-Path $currentPath 'scripts\stop_oopz_full_stack.ps1') -Notice restarting
        Remove-Item -LiteralPath $currentPath -Force
        if ($previousTarget) {
            New-Item -ItemType Junction -Path $currentPath -Target $previousTarget | Out-Null
            & (Join-Path $currentPath 'scripts\start_feishu_windows.ps1') -Lifecycle restarted
        }
        throw "Release health check failed; current was restored to '$previousTarget'."
    }

    [pscustomobject]@{ status = 'deployed'; release_id = $manifest.release_id; path = $releasePath; previous = $previousTarget; sha256 = $actualHash } | ConvertTo-Json
}
catch {
    throw
}
