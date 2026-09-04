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
- 最低 4 vCPU、8 GiB 内存、80 GiB SSD，建议 8 vCPU、16 GiB 内存、120 GiB SSD；
- 系统管理页面文件，系统盘长期至少保留 20 GiB；
- 稳定出站网络，可访问 GitHub、PyPI、npm、魔搭社区、OOPZ、飞书和分析 API；
- 不需要 GPU，也不需要开放应用业务入站端口；
- RDP 仅允许可信管理 IP。

安装以下 x64 软件（均从官方渠道获取，避免使用第三方镜像或非 LTS 版本）：

| 软件 | 在部署中的用途 | 官方下载地址 |
| --- | --- | --- |
| Git for Windows | 克隆运维副本、按提交检出脚本 | https://git-scm.com/download/win |
| GitHub CLI (gh) | 登录私有仓库、下载指定 Release 与校验文件 | https://cli.github.com/ |
| Python 3.12 | 运行环境与发布专用虚拟环境；须为 3.12.x（`install_release.ps1` 默认 `PythonExe=python.exe`，`[speech,feishu]` 额外依赖按 3.12 校验，勿用 3.13/3.14） | https://www.python.org/downloads/windows/ |
| Node.js 当前 LTS | 仅提供 `npx`/`npm`；安装脚本通过 `npx pnpm@10.15.0 install --frozen-lockfile` 固定 pnpm 版本，**无需预装 pnpm** | https://nodejs.org/ （取 LTS 版） |
| Chrome 或 Edge | `md-to-pdf`/报表渲染所需的无头浏览器内核 | https://www.google.com/chrome/ 或 https://www.microsoft.com/edge |

重新打开管理员 PowerShell，确认：

```powershell
git --version
gh --version
python --version
node --version
npm --version
```

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
    'C:\OOPZ\shared\logs'
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
```

`shared` 是持久区；更新和代码回滚都不得删除或覆盖其中的数据。

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

将 `<release-id>` 替换为 GitHub Releases 页面显示的标签，例如 `v0.11.4-79ac108081c4`（当前发布版本）：

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

- `OOPZ_FEISHU_APP_ID`、`OOPZ_FEISHU_APP_SECRET`；
- `OOPZ_LOGIN_PHONE`、`OOPZ_LOGIN_PASSWORD`；
- 全部 `ANALYZER_*` 项：Provider、API Key、Base URL、模型、超时、重试、请求间隔、普通/思考 Token 上限、思考模式和 JSON 模式；程序不提供默认值；
- 控制群 ID，或首次启动时保持为空并执行自动绑定；
- 启用公开发布时所需的文件夹和 Base 四项配置。

`OOPZ_FEISHU_APP_ID`/`OOPZ_FEISHU_APP_SECRET` 可通过以下任一方式获得：

1. 在本地开发机运行一键配置 `.\.venv\Scripts\oopz-feishu.exe setup`（见主 README 安装步骤），完成后把本机 `.env` 中这两行人工抄入服务器 `.env`；
2. 服务器完成安装后，在服务器终端运行 `C:\OOPZ\current\.venv\Scripts\oopz-feishu.exe setup`（RDP 终端会渲染二维码；无法显示时加 `--url-only` 只打印确认链接）。该写入经 `.env` 硬链接直接落入 `shared\config\.env`；
3. 按 [README_FEISHU_BOT_SETUP.md](README_FEISHU_BOT_SETUP.md) 第 2–5 节手动创建应用并抄写凭据。

首次安装（第 9 节）之前服务器尚无虚拟环境，因此首次部署只能用第 1 或第 3 种方式；无论哪种方式，都不要用 Git 在本地和服务器之间同步 `.env` 文件。

生产 `.env` 只保存在 `C:\OOPZ\shared\config\.env`。不要用 Git 在本地和服务器之间同步它。飞书应用的完整配置见 [README_FEISHU_BOT_SETUP.md](README_FEISHU_BOT_SETUP.md)。

分析 API 必须由服务器运维人员按实际账户填写。当前项目推荐 OpenCode Go + `mimo-v2.5`，现有实测中成本较低、总结效果较好，但发布包不会自动选择该供应商或模型；供应商价格和模型可用性应在部署时重新确认。

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
5. 安装 Node 依赖（安装脚本通过 `npx pnpm@10.15.0 install --frozen-lockfile` 完成，`pnpm-lock.yaml` 已随发布包提供，需服务器可访问 npm），并将 `.env`、模型、输出、状态和日志连接到 `shared`；
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
