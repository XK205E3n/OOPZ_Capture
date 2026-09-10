param([string]$RepositoryRoot, [string]$TestRoot, [string]$FixtureZip)
$ErrorActionPreference='Stop'
Import-Module Microsoft.PowerShell.Utility
. (Join-Path $RepositoryRoot 'scripts\retry_dependency_install.ps1')
function Assert-True($Value,$Message){if(-not $Value){throw $Message}}
$script:expected='31f349ebd021af2d416d99157c7cfca96f324c9cc8cd5c6f53d4f6777b27e4de'
function Get-FileHash {
    param($LiteralPath,$Algorithm)
    if ($LiteralPath -like '*.zip') { return @{Hash=$script:expected} }
    return Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256
}
function Test-OopzRecoveryPython { param($Python); Assert-True (Test-Path $Python) 'Python must exist.' }
$script:busy=$false
function Test-OopzReleaseBusy { return $script:busy }
$script:probeFails=$false
function Invoke-OopzDependencyProbe {
    Assert-True ($env:PIP_CONFIG_FILE -eq 'nul' -and $env:PIP_INDEX_URL -eq 'https://pypi.org/simple') 'Probe must isolate pip config.'
    Assert-True (-not $env:PIP_TEST_SENTINEL) 'Old pip overrides must be absent during probe.'
    if ($script:probeFails){throw 'probe failed'}
}
$script:installerCalls=0
$script:installerFails=$false
function Invoke-OopzOriginalInstaller {
    param($ScriptPath,$Artifact,$Root,$Python)
    Assert-True (Test-Path $ScriptPath) 'Original installer must be restored.'
    Assert-True (-not (Test-Path (Join-Path $Root 'releases\v0.11.9-5769293b3236'))) 'Failed directory must be moved before retry.'
    Assert-True ($env:PIP_CONFIG_FILE -eq 'nul') 'Retry must inherit isolated pip config.'
    $script:installerCalls++
    if ($script:installerFails) { throw 'retry failed' }
}
function New-Fixture($Name){
    $r=Join-Path $TestRoot $Name
    foreach($d in @('admin','artifacts','releases\v0.11.9-5769293b3236\.venv\Scripts','tools\Python312','shared\config','shared\models')) {New-Item -ItemType Directory -Force -Path (Join-Path $r $d) | Out-Null}
    Copy-Item $FixtureZip (Join-Path $r 'artifacts\release.zip')
    Set-Content (Join-Path $r 'artifacts\release.zip.sha256') $script:expected
    @{Artifact=(Join-Path $r 'artifacts\release.zip');ReleaseId='v0.11.9-5769293b3236';Commit='5769293b3236460d24bc0553561fa3ac68ae79be'} | ConvertTo-Json | Set-Content (Join-Path $r 'artifacts\deployment-inputs.json')
    foreach($f in @('releases\v0.11.9-5769293b3236\.venv\Scripts\python.exe','tools\Python312\python.exe','shared\config\.env','shared\models\keep.txt')) {Set-Content (Join-Path $r $f) 'test fixture'}
    New-Item -ItemType HardLink -Path (Join-Path $r 'releases\v0.11.9-5769293b3236\.env') -Target (Join-Path $r 'shared\config\.env') | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $r 'releases\v0.11.9-5769293b3236\models') -Target (Join-Path $r 'shared\models') | Out-Null
    return $r
}
$env:PIP_TEST_SENTINEL='restore-me'
$env:PIP_CONFIG_FILE='old-config'
$r=New-Fixture 'probe-only'
Invoke-OopzDependencyRecovery $r -ProbeOnly
Assert-True (Test-Path (Join-Path $r 'releases\v0.11.9-5769293b3236')) 'Probe-only must preserve directory.'
Assert-True ($env:PIP_TEST_SENTINEL -eq 'restore-me' -and $env:PIP_CONFIG_FILE -eq 'old-config') 'Probe-only must restore environment.'
$r=New-Fixture 'probe-failure';$script:probeFails=$true;$failed=$false
try{Invoke-OopzDependencyRecovery $r}catch{$failed=$_.Exception.Message -eq 'probe failed'}
Assert-True ($failed -and (Test-Path (Join-Path $r 'releases\v0.11.9-5769293b3236'))) 'Probe failure must not move release.'
Assert-True ($env:PIP_TEST_SENTINEL -eq 'restore-me') 'Failure must restore environment.'
$script:probeFails=$false
$r=New-Fixture 'active';New-Item -ItemType Directory (Join-Path $r 'current') | Out-Null;$failed=$false
try{Invoke-OopzDependencyRecovery $r}catch{$failed=$_.Exception.Message -match 'existing current'}
Assert-True $failed 'An existing current must block recovery.'
$r=New-Fixture 'busy';$script:busy=$true;$failed=$false
try{Invoke-OopzDependencyRecovery $r}catch{$failed=$_.Exception.Message -match 'process still references'}
Assert-True $failed 'Active release processes must block recovery.'
$script:busy=$false
$r=New-Fixture 'recovery'
Invoke-OopzDependencyRecovery $r
Assert-True ($script:installerCalls -eq 1) 'Exactly one retry should occur.'
Assert-True (Test-Path (Join-Path $r 'shared\models\keep.txt')) 'Shared junction target must survive the move.'
Assert-True (Test-Path (Join-Path $r 'shared\config\.env')) 'Shared config must survive the move.'
$backup=@(Get-ChildItem (Join-Path $r 'failed-installs') -Directory)
Assert-True ($backup.Count -eq 1 -and (Test-Path (Join-Path $backup[0].FullName '.venv\Scripts\python.exe'))) 'Backup must retain the failed environment.'
Assert-True ($env:PIP_TEST_SENTINEL -eq 'restore-me' -and $env:PIP_CONFIG_FILE -eq 'old-config') 'Success must restore environment.'
$r=New-Fixture 'retry-failure'; $script:installerFails=$true; $failed=$false
try { Invoke-OopzDependencyRecovery $r } catch { $failed=$_.Exception.Message -eq 'retry failed' }
Assert-True ($failed -and (Test-Path (Join-Path $r 'failed-installs'))) 'Retry failure must retain backup.'
Assert-True ($env:PIP_TEST_SENTINEL -eq 'restore-me' -and $env:PIP_CONFIG_FILE -eq 'old-config') 'Retry failure must restore environment.'
Write-Output 'dependency recovery checks passed'
