param(
    [string]$OutputDirectory = "artifacts",
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $dirty = git status --porcelain
    if ($LASTEXITCODE -ne 0) { throw 'This directory is not a Git repository.' }
    if ($dirty) { throw 'Refusing to build: commit or stash every working-tree change first.' }

    if (-not $SkipTests) {
        $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            throw "Local virtual environment is missing: $python"
        }
        & $python -m pytest
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed; no release was built.' }
    }

    $commit = (git rev-parse --short=12 HEAD).Trim()
    $versionMatch = Select-String -LiteralPath 'pyproject.toml' -Pattern '^version\s*=\s*"([^"]+)"$'
    if (-not $versionMatch) { throw 'Could not read project version from pyproject.toml.' }
    $version = $versionMatch.Matches[0].Groups[1].Value
    $releaseId = "v$version-$commit"
    $outputRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
    New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
    $artifact = Join-Path $outputRoot "oopz-capture-$releaseId.zip"
    $checksumFile = "$artifact.sha256"
    if ((Test-Path -LiteralPath $artifact) -or (Test-Path -LiteralPath $checksumFile)) {
        throw "Release output already exists: $artifact"
    }

    $temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("oopz-release-" + [guid]::NewGuid().ToString('N'))
    $archiveFromGit = Join-Path $temporaryRoot 'source.zip'
    $staging = Join-Path $temporaryRoot 'source'
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    try {
        git archive --format=zip --output=$archiveFromGit HEAD
        if ($LASTEXITCODE -ne 0) { throw 'git archive failed.' }
        Expand-Archive -LiteralPath $archiveFromGit -DestinationPath $staging
        $manifest = [ordered]@{
            release_id = $releaseId
            application_version = $version
            git_commit = (git rev-parse HEAD).Trim()
            git_commit_short = $commit
            created_at_utc = [DateTime]::UtcNow.ToString('o')
            source = 'clean committed HEAD via git archive'
        }
        $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $staging 'RELEASE_MANIFEST.json') -Encoding utf8
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::Open(
            $artifact,
            [System.IO.Compression.ZipArchiveMode]::Create
        )
        try {
            $stagingPrefix = $staging.TrimEnd('\') + '\'
            Get-ChildItem -LiteralPath $staging -Recurse -File | ForEach-Object {
                if (-not $_.FullName.StartsWith($stagingPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "Release source escaped the staging directory: $($_.FullName)"
                }
                $entryName = $_.FullName.Substring($stagingPrefix.Length).Replace('\', '/')
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $archive,
                    $_.FullName,
                    $entryName,
                    [System.IO.Compression.CompressionLevel]::Optimal
                ) | Out-Null
            }
        }
        finally {
            $archive.Dispose()
        }

        $verification = [System.IO.Compression.ZipFile]::OpenRead($artifact)
        try {
            foreach ($requiredEntry in @(
                'RELEASE_MANIFEST.json',
                'scripts/install_release.ps1',
                'scripts/download_sensevoice_model.py'
            )) {
                if ($null -eq $verification.GetEntry($requiredEntry)) {
                    throw "Release entry is missing: $requiredEntry"
                }
            }
            $blockedEntries = @($verification.Entries | Where-Object {
                $_.FullName -match '(^|[\\/])(\.env$|\.venv[\\/]|models[\\/]|output[\\/]|feishu_state[\\/]|logs[\\/]|node_modules[\\/]|artifacts[\\/])'
            })
            if ($blockedEntries.Count -gt 0) {
                throw "Release contains protected runtime content: $($blockedEntries[0].FullName)"
            }
        }
        finally {
            $verification.Dispose()
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryRoot) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        }
    }

    $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([System.IO.Path]::GetFileName($artifact))" | Set-Content -LiteralPath $checksumFile -Encoding ascii
    [pscustomobject]@{ release_id = $releaseId; artifact = $artifact; sha256 = $hash; checksum_file = $checksumFile } | ConvertTo-Json
}
finally {
    Pop-Location
}
