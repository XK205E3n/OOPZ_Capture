param(
    [string]$DownloadDirectory = 'C:\OOPZ\installers',
    [string]$PythonDirectory = 'C:\OOPZ\tools\Python312',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'

function Find-OopzCommand {
    param([string]$Name, [string[]]$Candidates = @())
    $paths = @((Get-Command $Name -CommandType Application -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })) + $Candidates
    foreach ($path in $paths) {
        if ($path -and $path -notmatch '\\Microsoft\\WindowsApps\\' -and (Test-Path -LiteralPath $path -PathType Leaf)) { return $path }
    }
    return $null
}

function Update-OopzProcessPath {
    # Preserve the caller's PATH too; never use setx (which may truncate PATH).
    $combined = @([Environment]::GetEnvironmentVariable('Path', 'Machine'), [Environment]::GetEnvironmentVariable('Path', 'User'), $env:Path) -join ';'
    $env:Path = (($combined -split ';' | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() } | Select-Object -Unique) -join ';')
}

function Find-OopzPython {
    param([string]$Target)
    $candidates = @((Join-Path $Target 'python.exe'), "$env:ProgramFiles\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
    $candidates += @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        foreach ($tag in @('3.12', '3.12-64')) {
            $key = Get-Item -LiteralPath "$hive\SOFTWARE\Python\PythonCore\$tag\InstallPath" -ErrorAction SilentlyContinue
            if ($key) { $candidates += Join-Path $key.GetValue('') 'python.exe' }
        }
    }
    foreach ($path in ($candidates | Select-Object -Unique)) {
        if (-not $path -or $path -match '\\Microsoft\\WindowsApps\\' -or -not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        try {
            $probe = & $path -I -c "import sys,struct; print('%s.%s/%s' % (sys.version_info.major,sys.version_info.minor,struct.calcsize('P')*8))" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$probe".Trim() -eq '3.12/64') { return $path }
        } catch { }
    }
    return $null
}

function Get-OopzTools {
    param([string]$PythonTarget)
    $vcRuntime = $null
    foreach ($key in @('HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64')) {
        $vc = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
        if ($vc -and $vc.Installed -eq 1 -and (Test-Path -LiteralPath "$env:WINDIR\System32\vcruntime140_1.dll")) { $vcRuntime = 'Visual C++ v14 x64' }
    }
    return @{
        VCRuntime = $vcRuntime
        Git = Find-OopzCommand 'git.exe' @("$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")
        Gh = Find-OopzCommand 'gh.exe' @("$env:ProgramFiles\GitHub CLI\gh.exe", "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe")
        Python = Find-OopzPython $PythonTarget
        Node = Find-OopzCommand 'node.exe' @("$env:ProgramFiles\nodejs\node.exe")
        Npm = Find-OopzCommand 'npm.cmd' @("$env:ProgramFiles\nodejs\npm.cmd")
        Npx = Find-OopzCommand 'npx.cmd' @("$env:ProgramFiles\nodejs\npx.cmd")
        Browser = Find-OopzCommand 'msedge.exe' @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe")
    }
}

function Get-OopzInstaller {
    param([string]$Url, [string]$Directory)
    if ($Url -notmatch '^https://(github\.com|www\.python\.org|nodejs\.org|dl\.google\.com|aka\.ms)/') { throw 'Installer URL is not an approved official source.' }
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $name = [IO.Path]::GetFileName(([Uri]$Url).AbsolutePath)
    $target = Join-Path $Directory $name
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        Write-Host "Downloading $Url"
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile "$target.partial"
        Move-Item -LiteralPath "$target.partial" -Destination $target
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $target
    if ($signature.Status -ne 'Valid') { throw "Installer signature is not valid: $target ($($signature.Status)). Nothing was executed." }
    Write-Host "Verified installer: $target"
    return $target
}

function Get-OopzGitHubInstallerUrl {
    param([string]$Repository, [string]$AssetPattern)
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases/latest" -Headers @{ 'User-Agent' = 'OOPZ-prerequisites' }
    $assets = @($release.assets | Where-Object { $_.name -match $AssetPattern })
    if ($assets.Count -ne 1) { throw "Expected one x64 installer in $Repository; found $($assets.Count)." }
    return $assets[0].browser_download_url
}

function Invoke-OopzInstaller {
    param([string]$Path, [string[]]$Arguments = @())
    if ([IO.Path]::GetExtension($Path) -eq '.msi') {
        $msiArguments = @('/i', ('"' + $Path + '"'), '/qn', '/norestart', '/L*v', ('"' + $Path + '.install.log"')) + $Arguments
        $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $msiArguments -Wait -PassThru -WindowStyle Hidden
    } else {
        $process = Start-Process -FilePath $Path -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    }
    if ($process.ExitCode -notin @(0, 3010)) { throw "Installer failed with code $($process.ExitCode): $Path" }
    if ($process.ExitCode -eq 3010) { Write-Warning 'Installer requests a reboot. Reboot before deployment, then rerun this script.' }
    Update-OopzProcessPath
}

function Install-OopzTool {
    param([string]$Tool, [hashtable]$Detected, [string]$Downloads, [string]$PythonTarget)
    switch ($Tool) {
        'VCRuntime' {
            Invoke-OopzInstaller (Get-OopzInstaller 'https://aka.ms/vc14/vc_redist.x64.exe' $Downloads) @('/install', '/quiet', '/norestart')
        }
        'Git' {
            $url = Get-OopzGitHubInstallerUrl 'git-for-windows/git' '^Git-[0-9.]+(?:\.[0-9]+)?-64-bit\.exe$'
            $file = Get-OopzInstaller $url $Downloads
            Invoke-OopzInstaller $file @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-')
        }
        'Gh' {
            $url = Get-OopzGitHubInstallerUrl 'cli/cli' '^gh_[0-9.]+_windows_amd64\.msi$'
            Invoke-OopzInstaller (Get-OopzInstaller $url $Downloads)
        }
        'Python' {
            $manager = Find-OopzCommand 'pymanager.exe'
            if (-not $manager) {
                # Official MSI works without Microsoft Store or winget.
                Invoke-OopzInstaller (Get-OopzInstaller 'https://www.python.org/ftp/python/pymanager/python-manager-26.3.msi' $Downloads)
                $manager = Find-OopzCommand 'pymanager.exe'
            }
            if (-not $manager) { throw 'Python install manager is not on PATH. Reopen administrator PowerShell and retry.' }
            if (Test-Path -LiteralPath $PythonTarget) { throw "Python target already exists but is not a usable 3.12 x64 installation: $PythonTarget. Preserve it and inspect before retrying." }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PythonTarget) | Out-Null
            $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = 'false'
            & $manager install --yes "--target=$PythonTarget" '3.12-64'
            if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 installation failed; rerun only after checking the output.' }
            if (-not (Find-OopzPython $PythonTarget)) { throw 'Downloaded Python is not a usable 3.12 x64 runtime.' }
            $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
            $pythonPaths = @($PythonTarget, (Join-Path $PythonTarget 'Scripts'))
            foreach ($entry in $pythonPaths) {
                if ($entry -notin ($machinePath -split ';')) { $machinePath = $entry + ';' + $machinePath }
            }
            [Environment]::SetEnvironmentVariable('Path', $machinePath, 'Machine')
            Update-OopzProcessPath
        }
        'Node' {
            if ($Detected.Node) {
                # npm is bundled with Node. Repair/install the SAME version; never downgrade silently.
                $version = (& $Detected.Node --version).Trim()
                if ($LASTEXITCODE -ne 0 -or $version -notmatch '^v\d+\.\d+\.\d+$') { throw 'Existing Node is not usable; inspect it before retrying.' }
            } else {
                $index = Invoke-RestMethod 'https://nodejs.org/dist/index.json'
                $version = ($index | Where-Object { $_.version -match '^v24\.' -and $_.lts } | Select-Object -First 1).version
                if (-not $version) { throw 'No Node 24 LTS release found in the official index.' }
            }
            $file = Get-OopzInstaller "https://nodejs.org/dist/$version/node-$version-x64.msi" $Downloads
            if ($Detected.Node) { Invoke-OopzInstaller $file @('REINSTALL=ALL', 'REINSTALLMODE=vomus', 'ADDLOCAL=ALL') }
            else { Invoke-OopzInstaller $file }
        }
        'Browser' {
            Invoke-OopzInstaller (Get-OopzInstaller 'https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi' $Downloads)
        }
    }
}

function Assert-OopzInstallerHost {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this script in an administrator PowerShell.' }
        if (-not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Use 64-bit PowerShell on x64 Windows.' }
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
}

function Complete-OopzPrerequisites {
    param([hashtable]$Detected, [string]$Downloads)
    foreach ($tool in @('Git', 'Gh', 'Python', 'Node', 'Npm', 'Npx')) {
        $env:Path = (Split-Path -Parent $Detected[$tool]) + ';' + $env:Path
    }
    $python = $Detected.Python
    $env:Path = (Split-Path -Parent $python) + ';' + $env:Path
    & $python -m pip --version
    if ($LASTEXITCODE -ne 0) {
        & $python -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw 'Python pip setup failed.' }
    }
    $nodeTarget = 'C:\OOPZ\shared\tools\node\node.exe'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $nodeTarget) | Out-Null
    if (-not (Test-Path -LiteralPath $nodeTarget)) { Copy-Item -LiteralPath $Detected.Node -Destination $nodeTarget }
    & $nodeTarget --version
    if ($LASTEXITCODE -ne 0) { throw 'The existing shared Node runtime is not usable; inspect it before replacing it.' }
    foreach ($tool in @('Git', 'Gh', 'Python', 'Node', 'Npm', 'Npx')) {
        & $Detected[$tool] --version
        if ($LASTEXITCODE -ne 0) { throw "$tool version check failed." }
    }
    Write-Host "Ready. PythonExe=$python"
    Write-Host "Use install_release.ps1 -PythonExe `"$python`" when deploying. Installers: $Downloads"
}

function Invoke-OopzPrerequisites {
    param([string]$Downloads, [string]$PythonTarget, [switch]$InspectOnly)
    Update-OopzProcessPath
    $detected = Get-OopzTools $PythonTarget
    if (-not $InspectOnly) { Assert-OopzInstallerHost }
    foreach ($tool in @('VCRuntime', 'Git', 'Gh', 'Python', 'Node', 'Browser')) {
        $ready = [bool]$detected[$tool]
        if ($tool -eq 'Node') { $ready = $ready -and [bool]$detected.Npm -and [bool]$detected.Npx }
        if ($ready) { Write-Host "SKIP $tool : $($detected[$tool])"; continue }
        if ($InspectOnly) { Write-Host "MISSING $tool"; continue }
        Write-Host "INSTALL $tool"
        Install-OopzTool $tool $detected $Downloads $PythonTarget
        $detected = Get-OopzTools $PythonTarget
        if (-not $detected[$tool] -or ($tool -eq 'Node' -and (-not $detected.Npm -or -not $detected.Npx))) { throw "$tool was not detected after installation. Inspect installer logs, then retry." }
    }
    if ($InspectOnly) { return }
    Complete-OopzPrerequisites $detected $Downloads
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-OopzPrerequisites -Downloads $DownloadDirectory -PythonTarget $PythonDirectory -InspectOnly:$CheckOnly
}
