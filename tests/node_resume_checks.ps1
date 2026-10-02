param([string]$RepositoryRoot, [string]$TestRoot, [string]$FixtureZip)
$ErrorActionPreference='Stop'
Import-Module Microsoft.PowerShell.Utility
. (Join-Path $RepositoryRoot 'scripts\resume_node_install.ps1')
function Assert-True($Value,$Message){if(-not $Value){throw $Message}}
function Get-FileHash {
    param($LiteralPath,$Algorithm)
    if($LiteralPath -like '*.zip'){return @{Hash='31f349ebd021af2d416d99157c7cfca96f324c9cc8cd5c6f53d4f6777b27e4de'}}
    $stream=[IO.File]::OpenRead($LiteralPath);$sha=[Security.Cryptography.SHA256]::Create()
    try{return @{Hash=([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','')}}
    finally{$stream.Dispose();$sha.Dispose()}
}
function Get-CimInstance { return @() }
$script:npmFails=$false; $script:importsFail=$false; $script:calls=0
function Invoke-OopzNodePackages {
    Assert-True ($env:npm_config_registry -eq 'https://registry.npmjs.org') 'Use explicit official registry.'
    Assert-True ($env:npm_config_fetch_retries -eq '5' -and $env:npm_config_strict_ssl -eq 'true') 'Retries must retain TLS verification.'
    Assert-True (-not $env:npm_config_test_sentinel) 'Old npm override must be isolated.'
    $script:calls++
    if($script:npmFails){throw 'npm failed'}
}
function Test-OopzInstalledDependencies {
    if($script:importsFail){throw 'imports failed'}
}
function New-Fixture($Name){
    $r=Join-Path $TestRoot $Name
    $release=Join-Path $r 'releases\v0.11.9-5769293b3236'
    foreach($dir in @('artifacts','shared\models','shared\config','releases\v0.11.9-5769293b3236\.venv\Scripts','releases\v0.11.9-5769293b3236\tools\node')){New-Item -ItemType Directory -Force -Path (Join-Path $r $dir) | Out-Null}
    Copy-Item $FixtureZip (Join-Path $r 'artifacts\release.zip')
    @{Artifact=(Join-Path $r 'artifacts\release.zip');ReleaseId='v0.11.9-5769293b3236';Commit='5769293b3236460d24bc0553561fa3ac68ae79be'} | ConvertTo-Json | Set-Content (Join-Path $r 'artifacts\deployment-inputs.json')
    [IO.Compression.ZipFile]::ExtractToDirectory($FixtureZip,$release)
    foreach($file in @('.env','.venv\Scripts\python.exe','tools\node\node.exe')){Set-Content (Join-Path $release $file) 'retained fixture'}
    Set-Content (Join-Path $r 'shared\models\keep.txt') 'keep model'
    return $r
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$env:npm_config_test_sentinel='restore-me'; $env:npm_config_registry='old-registry'
$cwd=(Get-Location).Path
$r=New-Fixture 'failed';$script:npmFails=$true;$failed=$false
try{Resume-OopzNodeInstall $r}catch{if($_.Exception.Message -ne 'npm failed'){throw};$failed=$true}
Assert-True $failed 'npm error must stop.'
Assert-True ($env:npm_config_registry -eq 'old-registry' -and $env:npm_config_test_sentinel -eq 'restore-me') 'Environment must be restored after failure.'
Assert-True ((Get-Location).Path -eq $cwd) 'Working directory must be restored.'
Assert-True (-not (Test-Path (Join-Path $r 'artifacts\first-start-ready.json'))) 'Failure must not write readiness.'
Assert-True (Test-Path (Join-Path $r 'releases\v0.11.9-5769293b3236\.venv\Scripts\python.exe')) 'Do not rename or remove installed Python.'
$script:npmFails=$false;$script:importsFail=$true;$r=New-Fixture 'bad-import';$failed=$false
try{Resume-OopzNodeInstall $r}catch{$failed=$_.Exception.Message -eq 'imports failed'}
Assert-True ($failed -and -not (Test-Path (Join-Path $r 'artifacts\first-start-ready.json'))) 'Import failure must block readiness.'
$script:importsFail=$false;$r=New-Fixture 'ready'
Resume-OopzNodeInstall $r
Assert-True ((Get-Content (Join-Path $r 'artifacts\first-start-ready.json') -Raw | ConvertFrom-Json).status -eq 'ready_for_first_start') 'Write readiness only after success.'
Assert-True (-not (Test-Path (Join-Path $r 'current'))) 'Node recovery must not start the gateway.'
Assert-True (Test-Path (Join-Path $r 'shared\models\keep.txt')) 'Retain shared models.'
Assert-True ($env:npm_config_registry -eq 'old-registry') 'Restore environment after success.'
$r=New-Fixture 'active';New-Item -ItemType Directory (Join-Path $r 'current') | Out-Null;$failed=$false
try{Resume-OopzNodeInstall $r}catch{$failed=$_.Exception.Message -match 'Existing current'}
Assert-True $failed 'An active installation must block this first-install helper.'
$r=New-Fixture 'modified';Add-Content (Join-Path $r 'releases\v0.11.9-5769293b3236\package.json') 'changed';$failed=$false
try{Resume-OopzNodeInstall $r}catch{$failed=$_.Exception.Message -match 'Release file changed'}
Assert-True $failed 'Changed release sources must be rejected.'
Write-Output 'node resume checks passed'
