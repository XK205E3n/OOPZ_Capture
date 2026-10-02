import os
from pathlib import Path
import subprocess

import pytest


def test_update_document_matches_script():
    root = Path(__file__).resolve().parents[1]
    text = (root / 'README_CLOUD_SERVER_DEPLOYMENT.md').read_text(encoding='utf-8')
    block = text.split('<!-- update-latest-copy:start -->')[1].split('<!-- update-latest-copy:end -->')[0]
    source = (root / 'scripts/update_latest_release.ps1').read_text(encoding='utf-8').strip()
    assert '& {\n' + source + '\n}' in block


@pytest.mark.skipif(os.name != 'nt', reason='Windows updater')
def test_latest_updater_guards_and_install_verification(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / 'test.ps1'
    script.write_text(r'''param([string]$Repository,[string]$Root)
$ErrorActionPreference='Stop'
. (Join-Path $Repository 'scripts\update_latest_release.ps1')
$script:commit='a'*40
$script:apiMode='normal'
$script:busy=$false
$script:installed=0
$script:prepared=0
function Invoke-RestMethod {
    param($Uri,$Headers)
    if ($Uri -like '*/releases/latest') {
        $digest=if($script:apiMode -eq 'badDigest'){'bad'}else{'sha256:'+('b'*64)}
        return @{tag_name='v0.11.13';draft=$false;prerelease=$false;assets=@(
            @{name='oopz-capture-v0.11.13-aaaaaaaaaaaa.zip';digest=$digest},
            @{name='oopz-capture-v0.11.13-aaaaaaaaaaaa.zip.sha256'}
        )}
    }
    return @{object=@{type='commit';sha=$script:commit}}
}
function Assert($condition,$message){if(-not $condition){throw $message}}
function MustFail($action){$failed=$false;try{& $action}catch{$failed=$true};Assert $failed 'Expected failure'}
$latest=Get-OopzLatestRelease
Assert ($latest.Commit -eq $script:commit) 'Tag commit'
$script:apiMode='badDigest';MustFail {Get-OopzLatestRelease};$script:apiMode='normal'
function Get-CimInstance {if($script:busy){return @{Name='python.exe';CommandLine='python -m oopz_capture.continuous_cli'}}}
function Initialize-OopzRelease {
    param($Root)
    $script:prepared++
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'source'),(Join-Path $Root 'artifacts') | Out-Null
    @{application_version='0.11.13'} | ConvertTo-Json | Set-Content (Join-Path $Root 'source\RELEASE_MANIFEST.json')
    @{Source=(Join-Path $Root 'source');Artifact='fixture.zip'} | ConvertTo-Json | Set-Content (Join-Path $Root 'artifacts\deployment-inputs.json')
}
function powershell.exe {
    param([switch]$NoProfile,$ExecutionPolicy,$File,$Artifact,$InstallRoot,$PythonExe)
    $script:installed++
    @{application_version='0.11.13';git_commit=$script:commit;release_id='v0.11.13-aaaaaaaaaaaa'} | ConvertTo-Json | Set-Content (Join-Path $Root 'current\RELEASE_MANIFEST.json')
    $global:LASTEXITCODE=0
}
foreach($dir in @('current','shared\config','tools\Python312')) {New-Item -ItemType Directory -Force -Path (Join-Path $Root $dir) | Out-Null}
Set-Content (Join-Path $Root 'shared\config\.env') 'preserve-test-config'
Set-Content (Join-Path $Root 'tools\Python312\python.exe') 'fixture'
function SetOld($version){@{application_version=$version;git_commit='c'*40;release_id='old'} | ConvertTo-Json | Set-Content (Join-Path $Root 'current\RELEASE_MANIFEST.json')}
SetOld '0.11.12'
$script:busy=$true;MustFail {Update-OopzLatestRelease $Root};Assert ($script:prepared -eq 0) 'Busy must not prepare';$script:busy=$false
SetOld '9.0.0';MustFail {Update-OopzLatestRelease $Root};SetOld '0.11.12'
Update-OopzLatestRelease $Root -PrepareOnly
Assert ($script:installed -eq 0) 'PrepareOnly must not install'
Update-OopzLatestRelease $Root
Assert ($script:installed -eq 1) 'Expected installation'
Update-OopzLatestRelease $Root
Assert ($script:installed -eq 1) 'Already latest must not reinstall'
Assert ((Get-Content (Join-Path $Root 'shared\config\.env') -Raw).Trim() -eq 'preserve-test-config') 'Config changed'
SetOld '0.11.12'
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'releases\v0.11.13-aaaaaaaaaaaa') | Out-Null
MustFail {Update-OopzLatestRelease $Root}
Write-Output 'UPDATER_CHECKS_OK'
''',encoding='ascii')
    result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),'-Repository',str(root),'-Root',str(tmp_path / 'install')],capture_output=True,text=True,errors='replace',timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'UPDATER_CHECKS_OK' in result.stdout
