param([string]$RepositoryRoot, [string]$TestRoot, [string]$FixtureDirectory)
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility
function Assert-True($Value, $Message) { if (-not $Value) { throw $Message } }
. (Join-Path $RepositoryRoot 'scripts\prepare_release.ps1')
$script:definition = Get-Content (Join-Path $FixtureDirectory 'definition.json') -Raw | ConvertFrom-Json
function Get-OopzPinnedRelease { return $script:definition }
$script:downloads = 0
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile, $Headers, [switch]$UseDefaultCredentials)
    Assert-True (-not $Headers -and -not $UseDefaultCredentials) 'Download must remain anonymous.'
    $script:downloads++
    Copy-Item -LiteralPath (Join-Path $FixtureDirectory ([IO.Path]::GetFileName(([Uri]$Uri).AbsolutePath))) -Destination $OutFile
}
$installRoot = Join-Path $TestRoot 'install'
Initialize-OopzRelease $installRoot
Assert-True ($script:downloads -eq 2) 'First run must fetch ZIP and checksum.'
Initialize-OopzRelease $installRoot
Assert-True ($script:downloads -eq 2) 'Second run must skip downloads.'
$inputs = Get-Content (Join-Path $installRoot 'artifacts\deployment-inputs.json') -Raw | ConvertFrom-Json
Assert-True (Test-Path (Join-Path $installRoot 'admin\install_release.ps1')) 'Admin script must be extracted.'
Add-Content -LiteralPath (Join-Path $inputs.Source 'scripts\install_release.ps1') -Value '# changed'
$failed = $false
try { Initialize-OopzRelease $installRoot } catch { $failed = $_.Exception.Message -match 'Extracted file was changed' }
Assert-True $failed 'Changed extracted scripts must not be reused or silently overwritten.'
Set-Content -LiteralPath $inputs.Artifact -Value 'corrupt'
$failed = $false
try { Initialize-OopzRelease $installRoot } catch { $failed = $_.Exception.Message -match 'checksum mismatch' }
Assert-True $failed 'Corrupt cached ZIP must be rejected.'

. (Join-Path $RepositoryRoot 'scripts\configure_server_env.ps1')
$script:asked = [Collections.Generic.List[string]]::new()
function Read-OopzConfigValue { param($Key); $script:asked.Add($Key); return 'temporary test value' }
$envFile = Join-Path $TestRoot 'config\.env'
$template = Join-Path $TestRoot 'template'
$apiKey = 'ANALYZER_' + 'API_KEY'
Set-Content -LiteralPath $template -Value ($apiKey + '="existing value"') -Encoding UTF8
Initialize-OopzServerEnv $envFile $template
Assert-True (-not $script:asked.Contains($apiKey)) 'Existing config values must not be prompted or replaced.'
Assert-True ($script:asked.Count -eq 12) 'Only missing required fields should be prompted.'
$script:asked.Clear()
$before = (Get-FileHash $envFile).Hash
Initialize-OopzServerEnv $envFile $template
Assert-True ($script:asked.Count -eq 0 -and (Get-FileHash $envFile).Hash -eq $before) 'Configured rerun must leave file unchanged.'
$lines = [IO.File]::ReadAllLines($envFile)
$lines = @($lines | Where-Object { -not $_.StartsWith('ANALYZER_MODEL=') })
[IO.File]::WriteAllLines($envFile, $lines)
$alias = Join-Path $TestRoot 'linked-env'
New-Item -ItemType HardLink -Path $alias -Target $envFile | Out-Null
Initialize-OopzServerEnv $envFile $template
Assert-True ((Get-FileHash $alias).Hash -eq (Get-FileHash $envFile).Hash) 'Config writes must preserve hard links.'
$script:asked.Clear()
Add-Content -LiteralPath $envFile -Value ($apiKey + '="duplicate"')
$failed = $false
try { Initialize-OopzServerEnv $envFile $template } catch { $failed = $_.Exception.Message -match 'Duplicate configuration key' }
Assert-True $failed 'Duplicate keys must stop rather than silently choose a value.'
Write-Output 'PowerShell deployment checks passed'
