param([string]$InstallRoot = 'C:\OOPZ', [switch]$PrepareOnly)
$ErrorActionPreference = 'Stop'

function Get-OopzLatestRelease {
    $api = 'https://api.github.com/repos/XK205E3n/OOPZ_Capture'
    $headers = @{'User-Agent'='oopz-release-updater'; Accept='application/vnd.github+json'}
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $release = Invoke-RestMethod -Uri "$api/releases/latest" -Headers $headers
    if ($release.draft -or $release.prerelease -or $release.tag_name -notmatch '^v\d+\.\d+\.\d+$') { throw 'Latest release is not a supported stable version.' }
    $tag = $release.tag_name
    $assets = @($release.assets | Where-Object { $_.name -match ('^oopz-capture-' + [regex]::Escape($tag) + '-[0-9a-f]{12}\.zip$') })
    if ($assets.Count -ne 1) { throw 'Expected exactly one official release ZIP.' }
    $asset = $assets[0]
    if ($asset.digest -notmatch '^sha256:([0-9a-fA-F]{64})$') { throw 'GitHub asset digest missing; use the pinned deployment procedure.' }
    $digest = $Matches[1].ToLowerInvariant()
    $sidecars = @($release.assets | Where-Object { $_.name -eq ($asset.name + '.sha256') })
    if ($sidecars.Count -ne 1) { throw 'Release checksum attachment missing.' }
    $reference = Invoke-RestMethod -Uri "$api/git/ref/tags/$tag" -Headers $headers
    $obj = $reference.object
    if ($obj.type -eq 'tag') {
        $annotated = Invoke-RestMethod -Uri "$api/git/tags/$($obj.sha)" -Headers $headers
        $obj = $annotated.object
    }
    if ($obj.type -ne 'commit' -or $obj.sha -notmatch '^[0-9a-f]{40}$') { throw 'Release tag does not resolve to a commit.' }
    $id = "$tag-$($obj.sha.Substring(0,12))"
    if ($asset.name -ne "oopz-capture-$id.zip") { throw 'Release asset and tag commit disagree.' }
    return @{Tag=$tag; File=$asset.name; Sha256=$digest; Commit=$obj.sha; ReleaseId=$id}
}

function Get-OopzPinnedRelease { return $script:oopzUpdateRelease }

function Initialize-OopzRelease {
    param([string]$Root)
    $release = Get-OopzPinnedRelease
    $Root = [IO.Path]::GetFullPath($Root)
    $artifacts = Join-Path $Root 'artifacts'
    New-Item -ItemType Directory -Force -Path $artifacts | Out-Null
    $zip = Join-Path $artifacts $release.File
    $baseUrl = "https://github.com/XK205E3n/OOPZ_Capture/releases/download/$($release.Tag)"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    foreach ($item in @(@{Path=$zip; Name=$release.File}, @{Path="$zip.sha256"; Name="$($release.File).sha256"})) {
        if (Test-Path -LiteralPath $item.Path -PathType Leaf) { Write-Host "SKIP download: $($item.Path)"; continue }
        # No token, Authorization header, gh login or Git clone is needed.
        try { Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/$($item.Name)" -OutFile "$($item.Path).partial" }
        catch { throw 'Anonymous download failed. Check network/repository visibility; do not bypass authentication if the repository becomes private.' }
        Move-Item -LiteralPath "$($item.Path).partial" -Destination $item.Path
    }
    $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    $sidecar = ((Get-Content -LiteralPath "$zip.sha256" -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
    if ($actual -ne $release.Sha256 -or $sidecar -ne $release.Sha256) { throw 'Release checksum mismatch. Existing files were preserved; do not execute them.' }

    $source = Join-Path (Join-Path $Root 'source') $release.ReleaseId
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $entry = $archive.GetEntry('RELEASE_MANIFEST.json')
        if (-not $entry) { throw 'Missing release manifest; use the official ZIP attachment, not Source code.' }
        $reader = [IO.StreamReader]::new($entry.Open())
        try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
        if ($manifest.git_commit -ne $release.Commit -or $manifest.release_id -ne $release.ReleaseId) { throw 'Manifest does not match the pinned release.' }
        $prefix = $source.TrimEnd('\') + '\'
        foreach ($entry in $archive.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $source $entry.FullName))
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe ZIP entry.' }
        }
        if (-not (Test-Path -LiteralPath $source)) { New-Item -ItemType Directory -Force -Path $source | Out-Null; [IO.Compression.ZipFileExtensions]::ExtractToDirectory($archive, $source) }
        if ((Get-Item -LiteralPath $source).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Source directory must not be a directory link.' }
        # Reuse only byte-identical package files; preserve extra local setup files.
        foreach ($entry in $archive.Entries) {
            if (-not $entry.Name) { continue }
            $target = Join-Path $source $entry.FullName
            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Incomplete extracted package: $target" }
            $stream = $entry.Open(); $sha = [Security.Cryptography.SHA256]::Create()
            try { $expected = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
            finally { $stream.Dispose(); $sha.Dispose() }
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $expected) { throw "Extracted file was changed: $target. No overwrite was performed." }
        }
    } finally { $archive.Dispose() }
    foreach ($name in @('admin','releases','shared\config','shared\models','shared\output','shared\feishu_state','shared\logs','shared\tools\node')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Root $name) | Out-Null
    }
    foreach ($name in @('install_release.ps1','rollback_release.ps1')) {
        Copy-Item -LiteralPath (Join-Path $source "scripts\$name") -Destination (Join-Path $Root "admin\$name") -Force
    }
    $inputs = @{Artifact=$zip; Source=$source; InstallRoot=$Root; ReleaseId=$release.ReleaseId; Commit=$release.Commit}
    $inputs | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $artifacts 'deployment-inputs.json') -Encoding UTF8
    Write-Host "Prepared $($release.Tag); SHA256 verified. Source=$source"
}


function Assert-OopzUpdateIdle {
    param([string]$Root)
    # Do not terminate active recording/transcription/analysis jobs.
    $busy = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match 'oopz_capture\.(continuous_cli|speech_cli|worker_cli|analyzer_cli)'
    })
    if ($busy.Count) { throw 'OOPZ jobs are active. Wait for completion before updating.' }
    $output = Join-Path $Root 'shared\output'
    if (Test-Path -LiteralPath $output) {
        foreach ($file in @(Get-ChildItem -LiteralPath $output -Filter '.run.lock' -Recurse -File -Force)) {
            $lock = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            if ($lock.pid -and (Get-Process -Id ([int]$lock.pid) -ErrorAction SilentlyContinue)) {
                throw 'An analysis run lock is active. Wait for completion before updating.'
            }
        }
    }
}

function Update-OopzLatestRelease {
    param([string]$Root, [switch]$PrepareOnly)
    $Root = [IO.Path]::GetFullPath($Root)
    $current = Join-Path $Root 'current'
    $manifestPath = Join-Path $current 'RELEASE_MANIFEST.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Existing current manifest missing. Use first-install or recovery instructions instead.' }
    if (-not (Test-Path -LiteralPath (Join-Path $Root 'shared\config\.env') -PathType Leaf)) { throw 'Shared configuration missing; no changes made.' }
    $old = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $script:oopzUpdateRelease = Get-OopzLatestRelease
    $latest = $script:oopzUpdateRelease
    $version = $latest.Tag.Substring(1)
    if ([version]$old.application_version -gt [version]$version) { throw 'Local version is newer; refusing downgrade.' }
    if ($old.git_commit -eq $latest.Commit -and $old.release_id -eq $latest.ReleaseId -and $old.application_version -eq $version) {
        Write-Host "ALREADY_LATEST: $($latest.ReleaseId). No installation or restart performed."
        return
    }
    if (Get-Item -LiteralPath (Join-Path $Root ('releases\' + $latest.ReleaseId)) -Force -ErrorAction SilentlyContinue) {
        throw 'Target release directory already exists. Preserve it and diagnose the previous installation; no overwrite or deletion performed.'
    }
    Assert-OopzUpdateIdle $Root
    Initialize-OopzRelease $Root
    $inputs = Get-Content -LiteralPath (Join-Path $Root 'artifacts\deployment-inputs.json') -Raw | ConvertFrom-Json
    $staged = Get-Content -LiteralPath (Join-Path $inputs.Source 'RELEASE_MANIFEST.json') -Raw | ConvertFrom-Json
    if ($staged.application_version -ne $version) { throw 'Manifest version mismatch.' }
    if ($PrepareOnly) { Write-Host "PREPARED_ONLY: $($latest.ReleaseId); current was not changed."; return }
    $python = Join-Path $Root 'tools\Python312\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $python = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source }
    Assert-OopzUpdateIdle $Root
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'admin\install_release.ps1') -Artifact $inputs.Artifact -InstallRoot $Root -PythonExe $python
    if ($LASTEXITCODE -ne 0) { throw 'Update failed. Preserve terminal errors and installed directories. Do not repeatedly reinstall.' }
    $actual = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($actual.git_commit -ne $latest.Commit -or $actual.release_id -ne $latest.ReleaseId -or $actual.application_version -ne $version) { throw 'Installed current manifest does not match the selected release.' }
    Write-Host "UPDATED: $($latest.ReleaseId); commit=$($latest.Commit). Verify recording, transcription and analysis in Feishu."
}

if ($MyInvocation.InvocationName -ne '.') { Update-OopzLatestRelease -Root $InstallRoot -PrepareOnly:$PrepareOnly }
