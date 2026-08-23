# 云服务器部署与更新

## 结论

本项目采用“私有 Git 管代码、发布包管上线、共享目录管生产状态”的方式。日常修复仍在本地完成；服务器只接收由已提交版本生成的不可变发布包。这样可以准确知道线上运行哪个提交，更新可验证，失败可自动切回上一版，同时不会覆盖 `.env`、模型、报告或飞书状态。

不建议把本地项目目录通过网盘、RDP 或 `scp -r` 整体覆盖到服务器。这会混入 `.venv`、缓存和未提交文件，也容易反向覆盖生产数据。Docker 暂不作为首选：当前启动、浏览器/PDF 和监视窗口明显依赖 Windows，容器化需要额外改造和验证。

## 一次性准备

1. 使用私有 GitHub、GitLab 或自建 Git 建立 `origin`。主分支只接收通过测试的提交；不要提交 `.env`、模型和运行数据。
2. 准备 Windows Server 2022/2025 x64 Desktop Experience（建议 8 vCPU/16 GiB；最低 4 vCPU/8 GiB），安装 Python 3.12 x64、Node.js LTS、Chrome 或 Edge，并启用系统管理页面文件。
3. 在服务器创建 `C:\OOPZ\shared\config`、`models`、`output`、`feishu_state`、`logs` 和 `C:\OOPZ\artifacts`。从 `.env.example` 创建 `shared\config\.env`；通过 RDP、SFTP 或云盘加密通道单独上传 `SenseVoiceSmall` 到 `shared\models`。
4. 云防火墙只开放管理所需的 RDP，并限制来源 IP。应用本身只需出站访问 OOPZ、飞书和分析 API，不开放业务入站端口。

## 本地开发与发布

每个 Bug 使用独立分支（建议 `codex/fix-...`），本地修改、测试、代码审查后合并。若变更影响部署，按根目录 `AGENTS.md` 同步三份部署文档。

正式发布前：

```powershell
git status --short
pytest
git commit
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_release.ps1
```

脚本会拒绝脏工作区，重新运行测试，从 `HEAD` 生成 `artifacts\oopz-capture-v<version>-<commit>.zip` 和同名 `.sha256`。只上传这两个文件；模型仅首次部署或模型版本变化时另行传输。

推荐把 ZIP 和 SHA-256 作为同一标签的 GitHub Release 附件。服务器可在完成私有仓库认证后下载指定版本：

```powershell
gh release download <release-id> --repo XK205E3n/OOPZ_Capture `
  --dir C:\OOPZ\artifacts --pattern '*.zip' --pattern '*.sha256'
```

不能或不希望在服务器保存 GitHub 读取凭据时，也可以从本地通过 RDP 磁盘映射、SFTP 或 OpenSSH `scp` 安全传输这两个文件。服务器地址应放在个人 SSH 配置或密码管理器中，不写进仓库。可进一步用 CI 自动执行测试并产出同样的发布包，但生产切换仍建议保留人工批准。完整命令见根目录 `README_CLOUD_SERVER_DEPLOYMENT.md`。

## 服务器更新

以管理员 PowerShell 执行发布包内或运维目录中的脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\OOPZ\admin\install_release.ps1 `
  -Artifact C:\OOPZ\artifacts\<artifact.zip>
```

安装脚本执行以下事务：校验 SHA-256、解压到新版本目录、创建独立虚拟环境、安装锁定声明范围内的 Python/Node 依赖、连接共享配置/模型/数据、运行导入检查、停止旧进程、切换 `current`、启动新版本并等待飞书长连接就绪。健康检查失败时自动把 `current` 切回旧版本并重启。

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
