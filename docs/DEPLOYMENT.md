# 云服务器部署与更新

## 结论

本项目采用“Git 管代码、发布包管上线、共享目录管生产状态”的方式。当前仓库公开可读，服务器可匿名下载固定 Release；生产凭据与运行数据不进入 Git。日常修复在本地完成，服务器只接收由已提交版本生成的不可变发布包，更新可验证、失败可回滚，共享数据保持独立。

不建议把本地项目目录通过网盘、RDP 或 `scp -r` 整体覆盖到服务器。这会混入 `.venv`、缓存和未提交文件，也容易反向覆盖生产数据。Docker 暂不作为首选：当前启动、浏览器/PDF 和监视窗口明显依赖 Windows，容器化需要额外改造和验证。

## 一次性准备

新服务器按[从零部署第 2.1 节](../README_CLOUD_SERVER_DEPLOYMENT.md#21-自动安装全部基础环境首次部署主流程)复制执行 PowerShell，或运行 `scripts/install_prerequisites.ps1`，自动安装缺少的 Visual C++ 运行库、Python 3.12 x64、Node/npm/npx 及浏览器。服务器不需要安装 Git 或 GitHub CLI。已有可用组件跳过；该步骤同时准备 PDF 的共享 Node 运行时，但不创建应用配置、不部署程序。已有 v0.11.9 发布包不含此后续新增脚本，可直接使用在线文档中的代码。

1. 开发端使用 Git 远端管理版本，主分支只接收通过测试的提交；不要提交 `.env`、模型和运行数据。服务器下载当前公开 Release 不需要 GitHub 账户或 Token。
2. 准备 Windows Server 2022/2025 x64 Desktop Experience（最低部署要求为 4 vCPU/8 GiB，需要更多余量时选 8 vCPU/16 GiB），安装 Python 3.12 x64、Node.js LTS、Chrome 或 Edge，并启用系统管理页面文件。低于 4 vCPU 或 8 GiB 的服务器不属于支持的部署配置；达到最低要求后仍需云端整机验收。测试条件与限制见 [运维说明](OPERATIONS.md#云服务器容量与试运行)。
3. 按[部署指南第 3 节](../README_CLOUD_SERVER_DEPLOYMENT.md#3-匿名下载正式发布包无需登录-github)匿名下载并校验 ZIP，建立目录、提取管理脚本；第 7 节通过 PowerShell 输入缺少的配置，并在服务器发起一键飞书配置。无需克隆仓库、记事本或本地电脑上的配置环境；飞书扫码授权仍由本人完成，手动配置仅作保底。首次正式安装时，服务器从魔搭社区下载固定修订版模型并校验 SHA-256。
4. 云防火墙只开放管理所需的 RDP，并限制来源 IP。应用本身只需出站访问 OOPZ、飞书和分析 API，不开放业务入站端口。

## 静音记录兼容

v0.11.12 为静音分片生成稳定 UUID，并在读取时兼容旧 no-speech 标记。无数据迁移或配置变化；更新后从“待分析”重试旧会话，原转写文件保留。详情见部署指南第 9.6 节。

## 内容审核失败处理

v0.11.13 对短窗口内容审核失败实行一次时间对半拆分，再次被拒绝的半段跳过并在所有分析报告中标明缺失。原始转写保留，普通错误及长摘要/最终综合不被静默跳过；无配置或数据迁移。详见部署指南第 9.7 节。

## 分析配置验收

v0.11.10 修复百炼 qwen3.8-flash 思考开关；云端建议单次超时 180 秒。配置保留原值，不会因升级自动更改，API 密钥与入口必须匹配。完整参数、401/超时区别和账户使用范围见[部署指南](../README_CLOUD_SERVER_DEPLOYMENT.md#部署前必读v01113)。

## 录音浏览器前置

录音浏览器是独立前置：Python 依赖安装完成后，使用运行账户和目标虚拟环境安装 Playwright Chromium，并实际启动验证。最新安装/续装脚本将其作为激活或就绪标记的门槛；旧 v0.11.9 发布包须手动执行[第 9.4 节](../README_CLOUD_SERVER_DEPLOYMENT.md#94-录音浏览器缺失或connecting持续失败)，不能仅检查系统 Edge/Chrome。

## 本地开发与发布

每个 Bug 使用独立分支（建议 `codex/fix-...`），本地修改、测试、代码审查后合并。若变更影响部署，按根目录 `AGENTS.md` 同步三份部署文档。

正式发布前：

```powershell
git status --short
pytest
git commit
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_release.ps1
```

脚本会拒绝脏工作区，重新运行测试，从 `HEAD` 生成 `artifacts\oopz-capture-v<version>-<commit>.zip` 和同名 `.sha256`。只上传这两个文件；模型不进入发布包，由服务器从指定开源仓库获取。

ZIP 和 SHA-256 作为同一标签的 GitHub Release 附件。服务器主流程为 PowerShell 匿名下载和固定哈希校验，完整可复制代码见从零部署第 3 节；只有从 GitHub main 取得最新版准备脚本时才可以运行（不要使用旧 ZIP 内的 prepare_release.ps1，包内固定值是构建时快照）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prepare_release.ps1
```

已经下载的正式 ZIP 和校验文件可放入服务器 `C:\OOPZ\artifacts`，脚本会跳过下载但仍进行校验。若未来仓库改回私有，匿名访问失败时需要取得合法授权或安全传入已下载的包，不能绕过认证。完整流程见根目录部署指南。

## v0.11.10 首次启动误判

已确认 Windows PowerShell 5.1 对无 BOM UTF-8 安装器的中文就绪标记解码错误，可能在网关已连通后误回滚。v0.11.11 已改为 ASCII 源码构造 Unicode 标记；旧 v0.11.10 ZIP 不变。符合前提的首次安装按[指南第 9.5 节](../README_CLOUD_SERVER_DEPLOYMENT.md#95-v01110-已连通却回滚重试提示版本已存在)恢复 current 并启动，已正常运行无需重装。

## 服务器更新

以管理员 PowerShell 执行发布包内或运维目录中的脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\OOPZ\admin\install_release.ps1 `
  -Artifact C:\OOPZ\artifacts\<artifact.zip>
```

安装脚本执行以下事务：校验发布包 SHA-256、解压到新版本目录、创建独立虚拟环境、安装声明范围内的 Python 依赖、从魔搭社区下载或校验固定修订版 SenseVoiceSmall、安装锁定的 Node 依赖、连接共享配置/模型/数据、运行导入检查、停止旧进程、切换 `current`、启动新版本并等待飞书长连接就绪。模型下载或校验失败不会切换版本；切换后的健康检查失败时自动把 `current` 切回旧版本并重启。

当前 Python 依赖是版本范围而不是完整 lock，因此不同日期部署可能解析出不同的间接依赖。正式长期运行前应增加受审查的 Python 锁文件；在此之前，发布记录必须保留实际 `pip freeze`（安装脚本写入每个发布目录的 `DEPLOYED_PYTHON_PACKAGES.txt`）。Node 依赖由 `pnpm-lock.yaml` 锁定，服务器通过固定版本的 pnpm 和 `--frozen-lockfile` 安装。

## 回滚

自动回滚发生在新版本启动或健康检查失败时。人工回滚：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\OOPZ\admin\rollback_release.ps1 `
  -ReleaseId <previous-release-id>
```

回滚只切换代码与依赖，不回滚共享 `.env` 和业务数据。如果新版本做了不可逆的数据迁移，必须在对应 `DEPLOYMENT_CHANGELOG.md` 条目中写出备份与恢复步骤；没有可行回滚方案时不得发布。

## 健康验证清单

- `current\RELEASE_MANIFEST.json` 的提交、版本与本次发布一致。
- Python 网关进程保持运行，`shared\logs\feishu_runtime.log` 出现本次启动后的“飞书长连接已就绪”。
- 飞书群内收到重启完成消息并能执行“状态”。
- 做一次短录音，确认输出、转写和报告写入共享目录。
- 触发一次分析和（在允许时）候选报告投递；首次部署需额外验证批准发布/Base 索引。
- 检查磁盘、内存、错误日志和任务计划程序重启行为。

## 更新频率与保留

- 紧急 Bug 也走“本地修复 → 测试 → 提交 → 发布包 → 部署”，不在服务器热改。
- 正常保留当前版和至少两个已验证旧版本；确认数据兼容且备份有效后再人工清理更旧发布目录。
- 每次部署完成后立即更新 `DEPLOYMENT_STATE.md` 与 `DEPLOYMENT_CHANGELOG.md` 并提交，确保文档与线上状态闭环。
