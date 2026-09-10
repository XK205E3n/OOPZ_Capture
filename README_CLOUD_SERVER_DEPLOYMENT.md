# OOPZ Capture — Windows 云服务器部署指南

本文说明如何从私有 GitHub 仓库 `XK205E3n/OOPZ_Capture` 将经过测试的 OOPZ Capture 发布包部署到 Windows 云服务器，并在后续版本中安全更新或回滚。

## 1. 部署模型

```text
本地开发与测试
  → Git 提交并推送私有 GitHub 仓库
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
| Git for Windows | 克隆运维副本、按提交检出脚本 | https://git-scm.com/download/win |
| Visual C++ v14 x64 运行库 | 支持 PyTorch 等 Windows 原生依赖，缺少时自动安装 | https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist |
| GitHub CLI (gh) | 登录私有仓库、下载指定 Release 与校验文件 | https://cli.github.com/ |
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

## 3. 登录私有 GitHub 仓库

在服务器执行：

```powershell
gh auth login --hostname github.com --git-protocol https --web
gh auth status
```

在浏览器中登录有权读取 `XK205E3n/OOPZ_Capture` 的账号并完成设备授权。不要把 GitHub Token 写进项目、`.env`、计划任务参数或脚本。长期生产服务器宜使用权限尽可能小的只读凭据，并由 Windows 凭据存储保护。

## 4. 建立持久目录

以管理员 PowerShell 执行：

```powershell
$oopzDirectories = @(
    'C:\OOPZ\admin',
    'C:\OOPZ\artifacts',
    'C:\OOPZ\releases',
    'C:\OOPZ\shared\config',
    'C:\OOPZ\shared\models',
    'C:\OOPZ\shared\output',
    'C:\OOPZ\shared\feishu_state',
    'C:\OOPZ\shared\logs',
    'C:\OOPZ\shared\tools\node'
)
$oopzDirectories | ForEach-Object {
    New-Item -ItemType Directory -Path $_ -Force | Out-Null
}
```

最终结构：

```text
C:\OOPZ\
  current -> releases\<release-id>
  releases\<release-id>\
  admin\
  artifacts\
  shared\config\.env
  shared\models\SenseVoiceSmall\
  shared\output\
  shared\feishu_state\
  shared\logs\
  shared\tools\node\
```

`shared` 是持久区；更新和代码回滚都不得删除或覆盖其中的数据。

PDF 渲染使用项目内固定的 Node 运行时：把已安装 Node.js LTS 目录中的 `node.exe` 复制到 `C:\OOPZ\shared\tools\node\node.exe`。发布包不含该文件，安装脚本会把它联接到每个版本目录的 `tools\node`，缺失时安装中止并给出明确提示。

## 5. 获取指定版本的管理脚本

首次部署时克隆运维副本；后续更新时只获取新提交：

```powershell
if (Test-Path C:\OOPZ\source\.git) {
    git -C C:\OOPZ\source fetch --tags --prune
} else {
    gh repo clone XK205E3n/OOPZ_Capture C:\OOPZ\source
}
```

在 GitHub Release 页面取得目标 Release 对应的完整 Git 提交，然后显式检出；不要依赖随时间变化的 `main`：

```powershell
git -C C:\OOPZ\source fetch --tags
git -C C:\OOPZ\source checkout <full-git-commit>

Copy-Item C:\OOPZ\source\scripts\install_release.ps1 C:\OOPZ\admin\ -Force
Copy-Item C:\OOPZ\source\scripts\rollback_release.ps1 C:\OOPZ\admin\ -Force
```

## 6. 下载 GitHub Release

将 `<release-id>` 替换为 GitHub Releases 页面显示的标签，当前版本为 `v0.11.9`；ZIP 文件名和包内 `release_id` 另含构建提交后缀，安装时以包内清单为准：

```powershell
gh release download <release-id> `
    --repo XK205E3n/OOPZ_Capture `
    --dir C:\OOPZ\artifacts `
    --pattern '*.zip' `
    --pattern '*.sha256'
```

确认 ZIP 和同名 `.zip.sha256` 均存在：

```powershell
Get-ChildItem C:\OOPZ\artifacts
```

安装脚本会再次核对 SHA-256；校验文件缺失或不匹配时拒绝安装。

## 7. 创建生产配置

首次部署时从目标提交的模板创建配置：

```powershell
Copy-Item C:\OOPZ\source\.env.example C:\OOPZ\shared\config\.env
notepad C:\OOPZ\shared\config\.env
```

至少填写：

- `OOPZ_FEISHU_APP_ID`、`OOPZ_FEISHU_APP_SECRET`，优先由下方一键流程获取，无需先手动创建应用；
- `OOPZ_LOGIN_PHONE`、`OOPZ_LOGIN_PASSWORD`；
- 全部 `ANALYZER_*` 项：Provider、API Key、Base URL、模型、超时、重试、请求间隔、普通/思考 Token 上限、思考模式和 JSON 模式；程序不提供默认值；
- 控制群 ID，或首次启动时保持为空并执行自动绑定；
- 启用公开发布时所需的文件夹和 Base 四项配置。

### 主流程：一键创建或更新飞书机器人

1. 首次部署前，在已经装好项目依赖的本地电脑运行 `.\.venv\Scripts\oopz-feishu.exe setup`，通过飞书扫码确认创建/更新。完成后安全地把本机 `.env` 中的 App ID/Secret 两行写入服务器 `shared\config\.env`。
2. 依照一键命令的后续提示检查应用版本是否需要发布；准备控制群。如需公开报告，另行完成文件夹/Base 的配置与协作者授权。
3. 已安装好的服务器需要更新应用配置时，在 `C:\OOPZ\current` 目录运行 `.\.venv\Scripts\oopz-feishu.exe setup`。二维码无法显示时加 `--url-only`；配置写入经 `.env` 硬链接落入 `shared\config\.env`。

首次安装（第 9 节）之前服务器尚无 `current` 虚拟环境，不能先运行第 3 步；按第 1 步获取凭据后再继续安装。已有可用机器人时可直接安全填写其现有凭据，不必重复创建。不要用 Git 在本地和服务器之间同步 `.env`。

### 保底：一键流程不可用时手动配置

仅当一键配置失败、租户不支持或管理员策略要求手动操作时，使用 [飞书手册](README_FEISHU_BOT_SETUP.md) 中折叠的第 2–4 节，再按第 5 节发布应用并安全写入凭据。手动配置不是默认安装步骤。

生产 `.env` 只保存在 `C:\OOPZ\shared\config\.env`。不要用 Git 在本地和服务器之间同步它。飞书应用的完整配置见 [README_FEISHU_BOT_SETUP.md](README_FEISHU_BOT_SETUP.md)。

分析 API 必须由服务器运维人员按实际账户填写。模型仅推荐 MiMo V2.5，不推荐供应商。供应商标识、API 地址、实际模型名称和运行参数由运维人员按所选服务配置；发布包不会自动选择或填入。

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

将 `<artifact.zip>` 替换成实际文件名：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\OOPZ\admin\install_release.ps1 `
    -Artifact C:\OOPZ\artifacts\<artifact.zip>
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

在 Windows 任务计划程序中创建任务：

```text
程序：powershell.exe
参数：-NoProfile -ExecutionPolicy Bypass -File C:\OOPZ\current\scripts\invoke_full_stack_launcher.ps1
起始目录：C:\OOPZ\current
触发器：系统启动或指定运行账户登录
选项：使用最高权限运行
```

当前项目会启动两个监视窗口。需要看到窗口时选择“仅当用户登录时运行”；后台运行时这些窗口不会出现在普通桌面会话。RDP 维护结束后使用“断开连接”，不要注销运行账户，除非计划任务已验证能在无登录会话下恢复。

创建任务后必须进行一次服务器重启演练，确认网关无需人工打开项目目录即可恢复。

## 12. 后续更新

本地完成 Bug 修复后执行（顺序遵循 AGENTS.md 发布规则）：

```text
审阅修改 → 同步 CHANGELOG / DEPLOYMENT 文档 → 工作树干净
→ 跑测试（开发沙箱中 2 个 Windows rmdir/symlink 环境测试会失败，用 scripts/build_release.ps1 -SkipTests）
→ 用 scripts/build_release.ps1 从已提交 HEAD 生成 ZIP/SHA-256（禁止复制工作目录部署）
→ 用 release-audit 技能 + .codex/release-audit-baseline.json 审计，脱敏结果写入 logs/release_audit/latest.json
→ Git 提交并推送 main → 打 tag（v<版本>-<提交>） → 创建 GitHub Release 并附 ZIP 与 .sha256
```

服务器更新只需要重复第 5、6、9、10 节。生产 `.env`、已校验模型和业务数据保持不变。模型修订版只有在项目代码、校验值和部署变更记录同时更新时才会变化。

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
    -ReleaseId <previous-release-id>
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
