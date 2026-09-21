param([string]$InstallRoot = 'C:\OOPZ')
$ErrorActionPreference = 'Stop'

function Get-OopzPinnedRelease {
    return @{
        Tag = 'v0.11.15'
        File = 'oopz-capture-v0.11.15-3be0c95a5a5a.zip'
        Sha256 = '68c983d4880753323aae4268b6be5d180fcf2ed55b4298d327ffd6723db701e9'
        Commit = '3be0c95a5a5a200048e25e5139236dac00d27baf'
        ReleaseId = 'v0.11.15-3be0c95a5a5a'
    }
}

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

if ($MyInvocation.InvocationName -ne '.') { Initialize-OopzRelease $InstallRoot }
