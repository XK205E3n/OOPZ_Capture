param([string]$RepositoryRoot, [string]$TestRoot)
$ErrorActionPreference = 'Stop'
. (Join-Path $RepositoryRoot 'scripts\install_prerequisites.ps1')
function Assert-True($Value, $Message) { if (-not $Value) { throw $Message } }
$script:calls = [Collections.Generic.List[string]]::new()
function Update-OopzProcessPath { }
function Assert-OopzInstallerHost { }
function Complete-OopzPrerequisites { }
function Get-OopzTools { param($PythonTarget); return $script:state.Clone() }
function Install-OopzTool {
    param($Tool, $Detected, $Downloads, $PythonTarget)
    $script:calls.Add($Tool)
    if ($script:failTool -eq $Tool) { throw 'simulated installer failure' }
    $script:state[$Tool] = "C:\fake\$Tool.exe"
    if ($Tool -eq 'Node') { $script:state.Npm = 'C:\fake\npm.cmd'; $script:state.Npx = 'C:\fake\npx.cmd' }
}
$script:failTool = ''
$script:state = @{VCRuntime='vc'; Python='python'; Node='node'; Npm='npm'; Npx='npx'; Browser='edge'}
Invoke-OopzPrerequisites $TestRoot $TestRoot
Assert-True ($script:calls.Count -eq 0) 'Already installed tools must not invoke installers.'
$script:state = @{VCRuntime=$null; Python=$null; Node=$null; Npm=$null; Npx=$null; Browser=$null}
Invoke-OopzPrerequisites $TestRoot $TestRoot -InspectOnly
Assert-True ($script:calls.Count -eq 0) 'CheckOnly must not install anything.'
Invoke-OopzPrerequisites $TestRoot $TestRoot
Assert-True (($script:calls -join ',') -eq 'VCRuntime,Python,Node,Browser') 'Only runtime prerequisites should install, without Git or gh.'
$script:calls.Clear()
Invoke-OopzPrerequisites $TestRoot $TestRoot
Assert-True ($script:calls.Count -eq 0) 'Second run must skip all installed tools.'
$script:state.Npm = $null
Invoke-OopzPrerequisites $TestRoot $TestRoot
Assert-True (($script:calls -join ',') -eq 'Node') 'Missing npm must use Node installation, not skip it.'
$script:calls.Clear(); $script:state.Npx = $null
Invoke-OopzPrerequisites $TestRoot $TestRoot
Assert-True (($script:calls -join ',') -eq 'Node') 'Missing npx must also repair Node installation.'
$script:calls.Clear()
$script:state.Python = $null; $script:state.Node = $null; $script:state.Npm = $null
$script:failTool = 'Python'; $failed = $false
try { Invoke-OopzPrerequisites $TestRoot $TestRoot } catch { $failed = $_.Exception.Message -eq 'simulated installer failure' }
Assert-True ($failed -and ($script:calls -join ',') -eq 'Python') 'Installer failure must stop subsequent installs.'

# Reload real helper functions; all network and installer invocations remain mocked.
. (Join-Path $RepositoryRoot 'scripts\install_prerequisites.ps1')
function Update-OopzProcessPath { }
function Get-AuthenticodeSignature { return @{Status='NotSigned'} }
function Invoke-WebRequest { throw 'unexpected download' }
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
Set-Content -LiteralPath (Join-Path $TestRoot 'fixture.msi') -Value 'test-only installer fixture'
$failed = $false
try { Get-OopzInstaller 'https://nodejs.org/fixture.msi' $TestRoot } catch { $failed = $_.Exception.Message -match 'signature is not valid' }
Assert-True $failed 'Unsigned cached download must be rejected.'
$failed = $false
try { Get-OopzInstaller 'https://invalid.example/fixture.msi' $TestRoot } catch { $failed = $_.Exception.Message -match 'approved official source' }
Assert-True $failed 'Unapproved download source must be rejected.'
function Start-Process { param($FilePath,$ArgumentList,[switch]$Wait,[switch]$PassThru,$WindowStyle); $script:installerArgs=$ArgumentList; return @{ExitCode=$script:installerCode} }
$script:installerCode=3010
Invoke-OopzInstaller 'C:\test space\fixture.msi'
Assert-True (('/qn' -in $script:installerArgs) -and ('/norestart' -in $script:installerArgs)) 'MSI must be unattended without reboot.'
$script:installerCode=1603; $failed=$false
try { Invoke-OopzInstaller 'C:\test space\fixture.msi' } catch { $failed=$_.Exception.Message -match '1603' }
Assert-True $failed 'Unexpected installer exit code must fail.'
Write-Output 'prerequisite branch checks passed'
