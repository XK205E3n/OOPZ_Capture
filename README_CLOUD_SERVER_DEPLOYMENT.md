# OOPZ Capture — Windows 云服务器部署指南

本文说明如何通过 PowerShell 从公开 GitHub Release 匿名获取经过测试的 OOPZ Capture 发布包，准备配置并部署到 Windows 云服务器，再安全更新或回滚。下载不需要 GitHub 登录，业务账户授权仍须由使用者完成。

## 1. 部署模型

```text
本地开发与测试
  → Git 提交并推送 GitHub 仓库
  → 生成与提交绑定的 ZIP 和 SHA-256
  → 上传为 GitHub Release 附件
  → Windows 服务器下载指定 Release
  → 安装到独立版本目录并切换 current
  → 健康检查；失败则回滚
```

GitHub 仓库只保存代码、脚本和文档；GitHub Release 保存可部署 ZIP 和校验文件。生产 `.env`、SenseVoice 模型、会话数据、飞书状态和日志永远不进入 GitHub。SenseVoice 模型由服务器在首次安装时从魔搭社区官方仓库下载并校验。

不要在服务器直接修改 `current`、`releases` 或执行无版本约束的 `git pull`。生产运行的代码必须能对应到一个 Release ID 和完整 Git 提交。

## 2. 服务器要求

- Windows Server 2022/2025 x64 Desktop Experience；
- 长期运行保守起点为 4 vCPU、8 GiB 内存、80 GiB SSD；需要更多余量时选 8 vCPU、16 GiB 内存、120 GiB SSD。低密度交流可用 2 vCPU/4–8 GiB 试运行，但必须验证整机内存、页面文件、磁盘与分片耗时，见 [容量与试运行](docs/OPERATIONS.md#云服务器容量与试运行)；
- 系统管理页面文件，系统盘长期至少保留 20 GiB；
- 稳定出站网络，可访问 GitHub、PyPI、npm、魔搭社区、OOPZ、飞书和分析 API；
- 不需要 GPU，也不需要开放应用业务入站端口；
- RDP 仅允许可信管理 IP。

### 2.1 自动安装全部基础环境（首次部署主流程）

默认从一台尚未安装开发工具的 Windows Server 开始。**在服务器打开 64 位管理员 PowerShell，复制执行下面整段代码**，无需先装 Git、gh、Python、Node、npm 或 winget。已有组件会跳过下载和安装；失败会停止，不会继续执行应用部署。

- 安装器统一保存在 `C:\OOPZ\installers`，MSI 日志保存在安装器旁边；先检查数字签名，再静默安装，不自动重启。
- Visual C++ x64 运行库：检测 v14 x64 注册信息与运行库 DLL，缺少时安装微软官方运行库，以满足 PyTorch 等原生组件的加载需求。
- Git / gh：从各自官方 GitHub Release 获取 x64 安装器；已有安装不升级、不重装。
- Python：检测已安装的 **3.12 x64**；只有其他版本时保留原版本，通过官方 Python 安装管理器 MSI 安装官方渠道可用的 3.12 x64 到 `C:\OOPZ\tools\Python312`，不固定到旧 EXE 的补丁版本。实际补丁号以安装输出为准；新安装目录加入系统 PATH。
- Node.js / npm / npx：缺少 Node 时安装 Node 24 LTS x64，npm 与 npx 随附；三者都有则跳过。已有 Node 但缺 npm 或 npx 时使用同版本官方 MSI 修复，不静默降级。非标准或损坏的既有安装若修复失败会停止，保留现场供排查。现有 Node 若不是 LTS，脚本保留原版本，需在部署前确认兼容性。
- Edge / Chrome：检测到任一常见安装位置即跳过，否则安装官方 Chrome x64；自动准备 PDF 所需的 `C:\OOPZ\shared\tools\node\node.exe`，已有共享运行时不覆盖。
- 安装完成后输出各工具版本与精确 `PythonExe` 路径。出现重启提示时先重启，再执行一次检查。新开 PowerShell 会读取更新的系统 PATH；特殊路径的现有 Python 请在后续安装命令中显式传入输出的 `-PythonExe`，不依赖默认解释器顺序。

已有仓库脚本时，也可以运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_prerequisites.ps1`；加 `-CheckOnly` 仅检查，不下载或安装。已下载的 v0.11.9 ZIP 不包含这个后续新增脚本，直接复制本节代码即可，无需先更新服务器程序包。

<!-- prerequisites-copy:start -->
```powershell
& {
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
}
```
<!-- prerequisites-copy:end -->

代码与 [scripts/install_prerequisites.ps1](scripts/install_prerequisites.ps1) 保持一致。下载失败或签名检查失败时，保留输出并排查网络，不从未知镜像替换安装器。

### 2.2 官方来源与安装后验证

以下链接用于核对来源或处理自动安装失败；正常部署优先执行上面的自动流程：

| 软件 | 在部署中的用途 | 官方下载地址 |
| --- | --- | --- |
| Git for Windows | 开发与维护工具；服务器默认安装流程直接使用 Release，不需要克隆仓库 | https://git-scm.com/download/win |
| Visual C++ v14 x64 运行库 | 支持 PyTorch 等 Windows 原生依赖，缺少时自动安装 | https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist |
| GitHub CLI (gh) | 可选维护工具；默认下载流程使用 PowerShell，无需 gh 登录 | https://cli.github.com/ |
| Python 3.12 x64 | 通过官方安装管理器安装；发布虚拟环境必须使用 3.12，后续 `install_release.ps1` 可用 `-PythonExe` 显式指定其路径 | https://docs.python.org/3/using/windows.html#advanced-installation |
| Node.js 当前 LTS | 提供 `npx`/`npm`（安装脚本通过 `npx pnpm@10.15.0 install --frozen-lockfile` 固定 pnpm 版本，**无需预装 pnpm**）；另需把其中的 `node.exe` 复制到 `C:\OOPZ\shared\tools\node\` 供 PDF 渲染使用（见第 4 节） | https://nodejs.org/ （取 LTS 版） |
| Chrome 或 Edge | `md-to-pdf`/报表渲染所需的无头浏览器内核 | https://www.google.com/chrome/ 或 https://www.microsoft.com/edge |

重新打开管理员 PowerShell，确认：

```powershell
git --version
gh --version
python --version
node --version
npm.cmd --version
```

这里使用 `npm.cmd` 与 `npm --version` 检查同一个 npm，避免 PowerShell 优先匹配 `npm.ps1` 而受到执行策略限制。无需为此修改机器的全局执行策略。脚本没有登录 GitHub、创建飞书应用或部署 OOPZ；基础环境就绪后继续第 3 节。

## 3. 匿名下载正式发布包（无需登录 GitHub）

截至 2026-09-10，仓库为 Public，已实际验证匿名访问正式附件成功。服务器不需要执行 `gh auth login`，不需要 Token，也不需要克隆仓库。若未来访问返回 401/403/404，请核对网络和仓库可见性；私有资源不能靠换命令绕过授权。

在管理员 PowerShell 中复制执行以下代码。它匿名下载当前 **v0.11.9** 正式 ZIP 和校验文件到 `C:\OOPZ\artifacts`，同时与这里固定的 SHA-256 比对，再验证清单和提取内容。已下载的同名文件放入该目录后会自动跳过下载；文件不符则停止，不覆盖。

此步骤还会建立持久目录、从已校验的包提取管理脚本，并生成后续命令使用的 `deployment-inputs.json`。不执行软件安装或启动网关。将来升级版本时应同步修改审核过的标签、文件名、提交和 SHA-256，不能只换标签或使用滚动的 latest 文件冒充固定版本。

<!-- prepare-release-copy:start -->
```powershell
& {
param([string]$InstallRoot = 'C:\OOPZ')
$ErrorActionPreference = 'Stop'

function Get-OopzPinnedRelease {
    return @{
        Tag = 'v0.11.9'
        File = 'oopz-capture-v0.11.9-5769293b3236.zip'
        Sha256 = '31f349ebd021af2d416d99157c7cfca96f324c9cc8cd5c6f53d4f6777b27e4de'
        Commit = '5769293b3236460d24bc0553561fa3ac68ae79be'
        ReleaseId = 'v0.11.9-5769293b3236'
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
}
```
<!-- prepare-release-copy:end -->

以上代码与 [scripts/prepare_release.ps1](scripts/prepare_release.ps1) 一致。官方下载为：
- [正式 ZIP](https://github.com/XK205E3n/OOPZ_Capture/releases/download/v0.11.9/oopz-capture-v0.11.9-5769293b3236.zip)
- [SHA-256](https://github.com/XK205E3n/OOPZ_Capture/releases/download/v0.11.9/oopz-capture-v0.11.9-5769293b3236.zip.sha256)

## 4. 持久目录已自动建立

第 3 节已经建立以下结构，不需要手动建目录或克隆 Git：

```text
C:\OOPZ\
  artifacts\                        # ZIP、校验文件、deployment-inputs.json
  source\<release-id>\              # 从已校验 ZIP 提取的安装准备副本
  admin\                            # 安装与回滚脚本
  releases\                         # 正式安装时创建独立运行版本
  shared\config\.env                # 第 7 节创建
  shared\models\
  shared\output\
  shared\feishu_state\
  shared\logs\
  shared\tools\node\node.exe         # 第 2.1 节基础环境脚本准备
```

`shared` 是持久区，更新和回滚不得覆盖它。无需在服务器配置 GitHub 账户或同步整个开发目录。

## 5. 使用发布包内的管理脚本

管理脚本已经从同一个经过校验的发布包复制到 `C:\OOPZ\admin`。可在 PowerShell 检查：

```powershell
Get-Item C:\OOPZ\admin\install_release.ps1
Get-Item C:\OOPZ\admin\rollback_release.ps1
Get-Content C:\OOPZ\artifacts\deployment-inputs.json
```

准备副本在 `source\<release-id>`，不直接作为生产运行目录；不要提前手动解压进 `releases`，否则正式安装会报告版本已存在。第 3 节再次执行时会验证提取文件与 ZIP 字节一致，发现修改则停止。

## 6. 确认待安装版本

```powershell
$oopzInputs = Get-Content C:\OOPZ\artifacts\deployment-inputs.json -Raw | ConvertFrom-Json
Get-FileHash -LiteralPath $oopzInputs.Artifact -Algorithm SHA256
Get-Content -LiteralPath ($oopzInputs.Artifact + '.sha256')
Get-Content -LiteralPath (Join-Path $oopzInputs.Source 'RELEASE_MANIFEST.json')
```

当前版本的校验值应为 `31f349ebd021af2d416d99157c7cfca96f324c9cc8cd5c6f53d4f6777b27e4de`。安装脚本还会再次校验，不能用 GitHub 自动生成的 Source code ZIP 代替正式附件。

## 7. 在 PowerShell 创建配置并一键配置飞书

所有文件准备、凭据输入和安装命令都可在服务器 PowerShell 完成；不需要记事本、本地电脑上的 GitHub 登录或仓库克隆。首次安装仍需要你已有的 OOPZ 登录信息与分析 API 账户；飞书扫码确认、版本审批和邀请进群是账户授权操作，不能通过匿名下载替代。

### 7.1 填写缺少的必填项

复制执行下面代码：不存在的生产配置从已校验模板创建，已有文件不覆盖，已有非空配置不重复询问。密码、手机号和 API Key 隐藏输入；只显示字段名，不显示配置值。无需复制凭据到命令行字符串或聊天中。

模型仅推荐 **MiMo V2.5**；供应商、API 地址、模型标识和运行参数按你自行选择的服务填写。控制群 ID 可以保持为空，由首次入群自动绑定；公开报告文件夹和 Base 的四项配置可在首次公开发布前补齐。

<!-- server-env-copy:start -->
```powershell
& {
param(
    [string]$EnvPath = 'C:\OOPZ\shared\config\.env',
    [string]$TemplatePath
)
$ErrorActionPreference = 'Stop'

function Read-OopzConfigValue {
    param([string]$Key)
    if ($Key -match 'PASSWORD|PHONE|API_KEY|SECRET|TOKEN') {
        $secure = Read-Host $Key -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer); $secure.Dispose() }
    }
    return Read-Host $Key
}

function Initialize-OopzServerEnv {
    param([string]$Path, [string]$Template)
    if (-not (Test-Path -LiteralPath $Path)) {
        if (-not $Template -or -not (Test-Path -LiteralPath $Template -PathType Leaf)) { throw 'A verified .env.example template is required for the first setup.' }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
        Copy-Item -LiteralPath $Template -Destination $Path
    }
    $keys = @('OOPZ_LOGIN_PHONE','OOPZ_LOGIN_PASSWORD','ANALYZER_PROVIDER','ANALYZER_API_KEY','ANALYZER_BASE_URL','ANALYZER_MODEL','ANALYZER_TIMEOUT_SECONDS','ANALYZER_MAX_RETRIES','ANALYZER_MIN_INTERVAL_SECONDS','ANALYZER_MAX_TOKENS','ANALYZER_THINKING_MAX_TOKENS','ANALYZER_THINKING_MODE','ANALYZER_JSON_MODE')
    foreach ($key in $keys) {
        $lines = [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)
        $pattern = '^\s*' + [regex]::Escape($key) + '\s*=(.*)$'
        $existing = @($lines | Where-Object { $_ -match $pattern })
        if ($existing.Count -gt 1) { throw "Duplicate configuration key: $key. Inspect the file locally before continuing." }
        if ($existing.Count -eq 1) {
            $null = $existing[0] -match $pattern
            $current = $Matches[1].Trim()
            if ($current -and $current -notin @('""', "''")) { Write-Host "KEEP $key"; continue }
        }
        $value = Read-OopzConfigValue $key
        if ([string]::IsNullOrWhiteSpace($value) -or $value -match '[\r\n]') { throw "Missing or multiline value for $key; nothing was written for this key." }
        $replacement = $key + '="' + $value + '"'
        if ($existing.Count) { $lines = @($lines | ForEach-Object { if ($_ -match $pattern) { $replacement } else { $_ } }) }
        else { $lines = @($lines) + $replacement }
        # Write in place: replacing the file would break release/shared hard links.
        [IO.File]::WriteAllLines($Path, [string[]]$lines, [Text.UTF8Encoding]::new($false))
        $value = $null; $replacement = $null
        Write-Host "SAVED $key"
    }
    Write-Host 'Required OOPZ/API fields are present. Run the one-click Feishu setup next; do not print or share the .env contents.'
}

if ($MyInvocation.InvocationName -ne '.') { Initialize-OopzServerEnv $EnvPath $TemplatePath }
} -TemplatePath ((Get-Content C:\OOPZ\artifacts\deployment-inputs.json -Raw | ConvertFrom-Json).Source + '\.env.example')
```
<!-- server-env-copy:end -->

代码与 [scripts/configure_server_env.ps1](scripts/configure_server_env.ps1) 一致。配置仍由程序在启动时校验；不要用 `Get-Content` 打印生产 `.env`。从前次配置中保留的错误值不会被此脚本自动改写，应在本机确认后单独修正。

### 7.2 服务器直接发起一键飞书配置（主流程）

如果生产 `.env` 已安全填写经过验证的飞书 App ID/Secret，可跳过本节直接安装；首次创建或需要更新应用时执行以下命令。

在同一个管理员 PowerShell 中执行：

```powershell
$ErrorActionPreference = 'Stop'
$oopzInputs = Get-Content C:\OOPZ\artifacts\deployment-inputs.json -Raw | ConvertFrom-Json
$oopzPython = if (Test-Path 'C:\OOPZ\tools\Python312\python.exe') {
    'C:\OOPZ\tools\Python312\python.exe'
} else {
    (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
}
& $oopzPython -I -c "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8"
if ($LASTEXITCODE -ne 0) { throw '请使用第 2.1 节输出的 PythonExe 路径（3.12 x64）。' }
$oopzSetupPython = 'C:\OOPZ\setup-venv\Scripts\python.exe'
if (-not (Test-Path $oopzSetupPython)) {
    & $oopzPython -m venv C:\OOPZ\setup-venv
    if ($LASTEXITCODE -ne 0) { throw '创建飞书配置环境失败。' }
}
& $oopzSetupPython -m pip install -e ($oopzInputs.Source + '[feishu]')
if ($LASTEXITCODE -ne 0) { throw '安装飞书配置依赖失败。' }
& $oopzSetupPython -c "from pathlib import Path; from oopz_capture.env_loader import load_project_env; from oopz_capture.feishu_setup import run_setup; p=Path(r'C:\OOPZ\shared\config\.env'); load_project_env(p); raise SystemExit(run_setup(env_path=p))"
if ($LASTEXITCODE -ne 0) { throw '飞书一键配置未完成，请按终端提示处理。' }
```

这调用的是 `oopz-feishu setup` 的同一个配置实现，显式把 App ID/Secret 写入 `shared\config\.env`。首次部署无需先有 `current` 虚拟环境；`setup-venv` 仅用于配置，正式版本仍由第 9 节单独安装。已有 App ID 时沿用一键流程的更新行为，避免重复手动创建应用。

使用飞书 App 扫描二维码并确认，检查平台是否需要发布应用版本或管理员审批。随后继续第 9 节安装，网关启动后邀请机器人进群并验收。公开报告资源的协作者授权仍需按[飞书手册](README_FEISHU_BOT_SETUP.md)完成。

### 保底：一键流程不可用时手动配置

仅在租户不支持、一键配置失败或管理员策略要求时，使用飞书手册中折叠的手动保底章节；它不是正常安装的前置步骤。整个流程不需要 GitHub 登录，但不会绕过 OOPZ、分析 API 或飞书自身的必要认证。

## 8. SenseVoice 模型自动下载

不要从开发机复制模型，也不要把模型上传 GitHub。首次执行第 9 节的安装脚本时，服务器会通过发布虚拟环境中的 ModelScope，从魔搭社区官方模型仓库下载：

```text
模型：iic/SenseVoiceSmall
来源：https://modelscope.cn/models/iic/SenseVoiceSmall
许可证：Apache-2.0
目标：C:\OOPZ\shared\models\SenseVoiceSmall
```

下载脚本固定使用当前项目审核过的模型修订版，并校验 `model.pt`、配置、CMVN 和分词模型等 5 个必需文件的 SHA-256。下载或校验失败会中止安装，不会切换 `current`。成功后会写入：

```text
C:\OOPZ\shared\models\SenseVoiceSmall\MODEL_SOURCE.json
```

其中记录模型 ID、固定修订版、来源、许可证和文件哈希，不包含凭据。该模型约 0.94 GB，首次安装必须保证魔搭社区网络可访问并预留足够时间；后续版本会复用已通过校验的共享模型，不重复下载。

如果模型目录已经存在但文件不完整或哈希不一致，安装会拒绝继续。不要用未知来源文件覆盖；应先保留现场并确认原因，再由运维人员移走无效目录后重新执行安装。

## 9. 安装并切换版本

在 PowerShell 中读取第 3 节保存的路径，并明确指定 3.12 解释器：

```powershell
$oopzInputs = Get-Content C:\OOPZ\artifacts\deployment-inputs.json -Raw | ConvertFrom-Json
$oopzPython = if (Test-Path 'C:\OOPZ\tools\Python312\python.exe') { 'C:\OOPZ\tools\Python312\python.exe' } else { (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source }
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\OOPZ\admin\install_release.ps1 `
    -Artifact $oopzInputs.Artifact -PythonExe $oopzPython
if ($LASTEXITCODE -ne 0) { throw '安装未完成，请保留终端错误并检查后重试。' }
```

安装脚本会：

1. 校验发布包 SHA-256；
2. 解压到新的 `releases\<release-id>`；
3. 创建该版本独立的 Python 虚拟环境；
4. 从魔搭社区下载或校验固定修订版 SenseVoiceSmall；
5. 安装 Node 依赖（安装脚本通过 `npx pnpm@10.15.0 install --frozen-lockfile` 完成，`pnpm-lock.yaml` 已随发布包提供，需服务器可访问 npm），并将 `.env`、模型、输出、状态和日志连接到 `shared`；同时校验 `shared\tools\node\node.exe` 存在并把 `tools\node` 联接到它，缺失时中止安装；
6. 运行 Python 导入检查；
7. 停止旧网关并切换 `current`；
8. 启动新网关并等待飞书长连接就绪；
9. 健康检查失败时自动恢复旧版本。

首次安装依赖和约 0.94 GB 模型耗时可能较长。不要在安装过程中关闭 PowerShell 或重启服务器。

## 10. 部署后验证

确认发布清单：

```powershell
Get-Content C:\OOPZ\current\RELEASE_MANIFEST.json
```

其中的 Release ID、应用版本和 Git 提交必须与 GitHub Release 一致。

检查日志：

```powershell
Get-Content C:\OOPZ\shared\logs\feishu_runtime.log -Tail 100
Get-Content C:\OOPZ\shared\logs\feishu_runtime.err.log -Tail 100
```

随后按顺序验收：

1. 日志出现“飞书长连接已就绪”；
2. 飞书控制群收到启动/重启消息；
3. `@机器人 状态` 能正常回复；
4. 做一次短录音，确认输出和转写进入共享目录；
5. 验证一次分析和候选报告投递；
6. 首次部署时额外验证批准发布、公开文档和 Base 索引；
7. 检查 CPU、内存、磁盘和错误日志。

## 11. 设置自动启动

在管理员 PowerShell 注册当前账户的登录启动任务，无需打开任务计划程序界面：

```powershell
$oopzTaskName = 'OOPZ Capture'
if (Get-ScheduledTask -TaskName $oopzTaskName -ErrorAction SilentlyContinue) {
    Write-Host '任务已存在，未覆盖。请检查下面显示的动作与触发器。'
    Get-ScheduledTask -TaskName $oopzTaskName | Select-Object TaskName, Actions, Triggers
} else {
    $oopzRunAs = "$env:USERDOMAIN\$env:USERNAME"
    $oopzAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\OOPZ\current\scripts\invoke_full_stack_launcher.ps1' -WorkingDirectory 'C:\OOPZ\current'
    $oopzTrigger = New-ScheduledTaskTrigger -AtLogOn -User $oopzRunAs
    $oopzPrincipal = New-ScheduledTaskPrincipal -UserId $oopzRunAs -LogonType Interactive -RunLevel Highest
    $oopzSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $oopzTaskName -Action $oopzAction -Trigger $oopzTrigger -Principal $oopzPrincipal -Settings $oopzSettings
}
```

此配置在该用户登录时启动，不声称服务器重启后无人登录也能恢复录制。当前项目有交互式监视窗口，RDP 维护结束应断开连接而非注销运行账户。注册任务不会立刻另启一个网关；正式安装已负责启动应用。

创建任务后须重启并登录该账户进行演练，确认无需手动打开项目目录即可恢复。无人登录启动需要另行验证 Windows 会话和浏览器音频，不能直接套用此交互式任务配置。

## 12. 后续更新

本地完成 Bug 修复后执行（顺序遵循 AGENTS.md 发布规则）：

```text
审阅修改 → 同步 CHANGELOG / DEPLOYMENT 文档 → 完成测试与 release-audit
→ Git 提交并推送，确保工作区干净
→ 用 scripts/build_release.ps1 从已提交 HEAD 生成 ZIP/SHA-256
→ 创建对应提交的 GitHub Release 并附 ZIP 与 .sha256
→ 更新已审核的匿名下载版本、提交与校验值
```

服务器更新重复采用新版本固定信息的第 3 节，再执行第 6、9、10 节。生产 `.env`、已校验模型和业务数据保持不变。模型修订版只有在项目代码、校验值和部署变更记录同时更新时才会变化。不得为绕过测试失败而默认跳过发布测试。

如果配置契约发生变化，先按新版本 `.env.example` 人工合并到生产 `.env`，禁止用模板直接覆盖生产文件。

## 13. 人工回滚

安装失败会自动尝试恢复旧版本。需要主动回滚时，先查看已安装版本：

```powershell
Get-ChildItem C:\OOPZ\releases -Directory
```

然后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\OOPZ\admin\rollback_release.ps1 `
    -ReleaseId (Read-Host '请输入上方列表中要回滚到的完整 Release ID')
```

回滚只切换代码和依赖，不回滚共享配置与业务数据。若某次版本包含不可逆数据迁移，必须按照该版本部署变更记录执行备份恢复，不能只切换代码。

## 14. 安全边界

- 云防火墙不开放应用业务入站端口；
- RDP 仅允许可信管理 IP，并启用强密码和多因素认证；
- GitHub、飞书、OOPZ 和分析服务凭据不写进仓库或命令历史；
- 语音模型只从项目指定的官方开源仓库获取并执行哈希校验；
- 生产服务器不作为开发机，不在服务器热修代码；
- 每个线上版本必须能追溯到 GitHub Release、SHA-256 和完整 Git 提交；
- 删除旧 Release 目录前至少保留当前版和两个经过验证的回滚版本，并确认业务数据备份有效。

更详细的架构约定见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)，本地与服务器差异以 [docs/DEPLOYMENT_STATE.md](docs/DEPLOYMENT_STATE.md) 为准。
