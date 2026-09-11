param([string]$InstallRoot = 'C:\OOPZ')
$ErrorActionPreference = 'Stop'

function Assert-OopzNodeStage {
    param([string]$Root)
    if (Get-Item -LiteralPath (Join-Path $Root 'current') -Force -ErrorAction SilentlyContinue) { throw 'Existing current detected. This helper only completes a first installation before activation.' }
    $inputs = Get-Content -LiteralPath (Join-Path $Root 'artifacts\deployment-inputs.json') -Raw | ConvertFrom-Json
    if ($inputs.ReleaseId -ne 'v0.11.9-5769293b3236' -or $inputs.Commit -ne '5769293b3236460d24bc0553561fa3ac68ae79be') { throw 'Unexpected release. This helper targets v0.11.9 only.' }
    $release = Join-Path $Root ('releases\' + $inputs.ReleaseId)
    foreach ($dir in @($Root,(Join-Path $Root 'releases'),$release)) {
        $item = Get-Item -LiteralPath $dir -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Expected normal installation directory: $dir" }
    }
    $artifact = [IO.Path]::GetFullPath($inputs.Artifact)
    if (-not $artifact.StartsWith((Join-Path $Root 'artifacts').TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Artifact path escaped installation root.' }
    if ((Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash -ne '31f349ebd021af2d416d99157c7cfca96f324c9cc8cd5c6f53d4f6777b27e4de') { throw 'Release ZIP checksum mismatch.' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($artifact)
    try {
        foreach ($entry in $zip.Entries) {
            if (-not $entry.Name) { continue }
            $path = [IO.Path]::GetFullPath((Join-Path $release $entry.FullName))
            if (-not $path.StartsWith($release.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe ZIP path.' }
            $stream = $entry.Open(); $sha = [Security.Cryptography.SHA256]::Create()
            try { $expected = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
            finally { $stream.Dispose(); $sha.Dispose() }
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $expected) { throw "Release file changed or missing: $path" }
        }
    } finally { $zip.Dispose() }
    foreach ($file in @('.venv\Scripts\python.exe','.env','tools\node\node.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $release $file) -PathType Leaf)) { throw "Required installed file missing: $file" }
    }
    $active = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($release,[StringComparison]::OrdinalIgnoreCase) -ge 0 })
    if ($active.Count) { throw 'A process still references this release. Wait for it to finish; no process was stopped.' }
    return $release
}

function Invoke-OopzNodePackages {
    $arguments = @('--yes','pnpm@10.15.0','install','--frozen-lockfile','--ignore-scripts','--registry=https://registry.npmjs.org','--fetch-retries=5','--fetch-retry-mintimeout=10000','--fetch-retry-maxtimeout=60000','--fetch-timeout=600000','--network-concurrency=4')
    & npx.cmd @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Node download/install failed again. Python, models and partial Node downloads were retained. Keep the latest npm error output.' }
}

function Test-OopzInstalledDependencies {
    param([string]$Release, [string]$Root)
    $python = Join-Path $Release '.venv\Scripts\python.exe'
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency consistency check failed; do not activate or force version changes.' }
    & $python -c "import numpy,torch,torchaudio,funasr,oopz_capture,lark_oapi; print('Python imports OK'); print('numpy',numpy.__version__,'torch',torch.__version__,'torchaudio',torchaudio.__version__)"
    if ($LASTEXITCODE -ne 0) { throw 'Python import check failed. Preserve the traceback; successful pip installation alone is insufficient.' }
    & $python -c "import runpy,sys; from pathlib import Path; check=runpy.run_path(sys.argv[1]); check['verify_model'](Path(sys.argv[2])); print('Existing model hashes verified; no download performed')" (Join-Path $Release 'scripts\download_sensevoice_model.py') (Join-Path $Root 'shared\models\SenseVoiceSmall')
    if ($LASTEXITCODE -ne 0) { throw 'Existing model verification failed. No model file was removed.' }
    & (Join-Path $Release 'tools\node\node.exe') --input-type=module -e "await import('md-to-pdf'); console.log('Node report dependency OK')"
    if ($LASTEXITCODE -ne 0) { throw 'Node report dependency import failed.' }
    $freeze = & $python -m pip freeze
    if ($LASTEXITCODE -ne 0) { throw 'Could not record installed Python package versions.' }
    $freeze | Set-Content -LiteralPath (Join-Path $Release 'DEPLOYED_PYTHON_PACKAGES.txt') -Encoding UTF8
}

function Resume-OopzNodeInstall {
    param([string]$Root)
    $Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $release = Assert-OopzNodeStage $Root
    $readyPath = Join-Path $Root 'artifacts\first-start-ready.json'
    if (Test-Path -LiteralPath $readyPath) { @{status='checking';release_path=$release} | ConvertTo-Json | Set-Content -LiteralPath $readyPath -Encoding UTF8 }
    $saved = @{}
    [Environment]::GetEnvironmentVariables('Process').GetEnumerator() | Where-Object { $_.Key -like 'npm_config_*' } | ForEach-Object { $saved[$_.Key]=$_.Value }
    $temporaryConfig = Join-Path $Root ('installer-cache\npm-config-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $temporaryConfig | Out-Null
    [IO.File]::WriteAllText((Join-Path $temporaryConfig 'user.npmrc'),'')
    [IO.File]::WriteAllText((Join-Path $temporaryConfig 'global.npmrc'),'')
    try {
        foreach($name in @($saved.Keys)){ [Environment]::SetEnvironmentVariable($name,$null,'Process') }
        $env:npm_config_userconfig = Join-Path $temporaryConfig 'user.npmrc'
        $env:npm_config_globalconfig = Join-Path $temporaryConfig 'global.npmrc'
        $env:npm_config_registry = 'https://registry.npmjs.org'
        $env:npm_config_fetch_retries = '5'
        $env:npm_config_fetch_retry_mintimeout = '10000'
        $env:npm_config_fetch_retry_maxtimeout = '60000'
        $env:npm_config_fetch_timeout = '600000'
        $env:npm_config_strict_ssl = 'true'
        $env:npm_config_ignore_scripts = 'true'
        Push-Location $release
        try { Invoke-OopzNodePackages; Test-OopzInstalledDependencies $release $Root }
        finally { Pop-Location }
        $null = Assert-OopzNodeStage $Root
        @{status='ready_for_first_start';release_id='v0.11.9-5769293b3236';release_path=$release;checked_at_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'artifacts\first-start-ready.json') -Encoding UTF8
        Write-Host 'READY_FOR_FIRST_START: dependencies verified. No current link was created and no gateway was started. Continue with deployment guide section 9.3.'
    } finally {
        [Environment]::GetEnvironmentVariables('Process').GetEnumerator() | Where-Object { $_.Key -like 'npm_config_*' } | ForEach-Object { [Environment]::SetEnvironmentVariable($_.Key,$null,'Process') }
        foreach($name in $saved.Keys){ [Environment]::SetEnvironmentVariable($name,$saved[$name],'Process') }
    }
}

if ($MyInvocation.InvocationName -ne '.') { Resume-OopzNodeInstall $InstallRoot }
