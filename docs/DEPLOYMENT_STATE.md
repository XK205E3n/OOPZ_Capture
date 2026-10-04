# 部署状态基线

> 这是本地代码与生产服务器差异的唯一事实来源。任何部署相关修改和每次生产发布都必须同步更新本文件。禁止记录密钥、密码、完整服务器地址或个人信息。

更新时间：2026-10-04（晚，已部署 0.12.0）

## 当前状态

### Ubuntu 生产服务器实测基线（2026-10-04 部署 0.12.0 后）

检查对象：腾讯云 CVM，Ubuntu 24.04.4 LTS，4 vCPU / 7.5 GiB，根分区 59 GB（清理后已用约 26%，`/opt/oopz` 约 3.7 GB）。以下为实测事实，不含密钥或地址。

| 项目 | 实测状态 |
| --- | --- |
| 账号 | 全机只有一个管理账号 `ubuntu`，所有操作者和 AI 共用，不另建服务账号；OOPZ 以 `ubuntu` 身份运行，靠 systemd 隔离（见下） |
| 目录 | `/opt/oopz`（属主 `ubuntu`）：`current` → `releases/v0.12.0-18d560a7f329`（唯一的发布目录，root 属主）；`.management.lock`、`backups/`（账号迁移备份）属主 root；`shared/` 持久数据（`config`、`models`、`output`、`feishu_state`、`logs`、`home`、`browsers`、`tools`、`python`、`assets/fonts`、`capture-tests`）、`artifacts/`（当前发布包与引导脚本）属主 `ubuntu` |
| 服务形态 | 持久 systemd 单元 `oopz-capture.service`（`enabled`，开机自启，`Restart=on-failure`）：`User=ubuntu`、`NoNewPrivileges=yes`、`ProtectHome=yes`、`PrivateTmp=yes`、`UMask=0077`；日志在 `shared/logs/feishu_runtime.log` 与 `feishu_error.log`。原瞬时测试单元 `oopz-capture-test` 已停止 |
| 运行进程 | `feishu_cli serve`（完整模式，非 capture-only），版本 0.12.0（提交 `18d560a`，标签 `linux-v0.12.0`）；录音后自动分析并发图 |
| 最近一次实测 | 会话 `2026-10-03_14-32-31_BJT`（北京时间）：159 个分片全部转写成功、失败 0、共 7834 段，状态 `ready_for_analysis`；日志无错误。仅验证录音与转写；该会话音频已按用户批准删除，转写文本保留供分析测试 |
| 配置 | `shared/config/.env` 共 12 个键：飞书应用与管理群、OOPZ 登录、`OOPZ_DEVICE=cpu`、`OOPZ_RETAIN_AUDIO=false`（转写成功即删分片音频）、`OOPZ_ANALYZER_CLI/HOME/MODEL/TIMEOUT_SECONDS`、`OOPZ_FONT_DIR=/opt/oopz/shared/assets/fonts`；已去掉 `OOPZ_PDF_BACKEND` |
| 依赖 | Python 3.12 与 Node v22.23.3 位于 `shared/`；录音用的 Chromium 在 `shared/browsers`（PDF 用的 `/opt/oopz-browser-runtime` 已删除）；模型约 897 MB；字体在 `shared/assets/fonts`；分析器用的 Qoder CLI 在 `shared/tools/qodercn-*`（登录状态在 `qodercn-home`，须留在 `/opt` 下，`ProtectHome` 会挡住 `/home`） |

### 2026-10-04 部署 0.12.0（Linux 主线首个发布）

- 流程：本地 `scripts/build_release.ps1` 从提交 `18d560a` 构建 `oopz-capture-v0.12.0-18d560a7f329.zip`（SHA-256 `f93b55f6…cd9b5`），上传后在服务器核对哈希，解出引导脚本，`prepare`（独立 Python 3.12 虚拟环境、CPU 版 torch、模型哈希已验证）→ 停止 `oopz-capture-test` → `activate --enable`。启动后日志出现"飞书长连接已就绪"，服务 `active`、`enabled`。
- 部署中发现并修复：systemd 单元模板 `WorkingDirectory="@ROOT@/current"` 带引号，systemd 报"path is not absolute"（此前从未在服务器激活过）。第一次激活失败后，事务恢复也因单元坏了而失败（按设计保留日志并停服务）；按日志记录的"原先全无"状态手动删除了单元、logrotate、`current` 链接和日志，重建发布包（`a85be8d` → `18d560a`）后重新部署。受影响的 `a85be8d` 构建只准备过、从未激活。
- 实测：用服务器上 2026-10-03 那场会话的副本，在新发布的环境里完整跑了分析与渲染（4642 段、7 个窗口，约 20 分钟），得到 1080×5870 的图和 `digest.md`，无渲染警告；未映射的音轨按排除法归给 `rola`。副本已删除。**真实飞书群的发图、真实头像下载与出入记录尚未验证**，由用户在群里发起第一次录音时验证。
- 空间清理（根分区已用 44% → 26%）：删除 4 个旧的 `v0.11.15` 发布目录（约 8.5 GB）、`artifacts/` 里的旧发布包/引导/测试产物、`shared/dev`（开发用虚拟环境与分析实验）、capture-only 用的空 state/output、3 个含凭据的旧 `.env` 备份、pip/pnpm/npm 缓存、PDF 用的 `/opt/oopz-browser-runtime`（393 MB）、`tools/uv`。保留：模型、录音浏览器、Node/Python/Qoder、`capture-tests/20261003T0625`（旧会话的转写，供测试）、账号迁移备份。
- 回滚：旧版本目录已删除，回滚方式是用 Git 标签/发布包重新构建旧版本；旧版本只是 capture-only 测试代码，没有回滚价值。

### 2026-10-04 服务账号迁移：退役原专用账号，OOPZ 改以 `ubuntu` 运行

- 备份：`/opt/oopz/backups/` 下 2026-10-04 11:42:56 生成的迁移备份压缩包及其 SHA-256 文件（root 属主、权限 600，含原账号家目录、`shared/config`、`shared/feishu_state`，含凭据，仅 root 可读）。
- 属主：`/opt/oopz` 下原属专用账号的约 3.6 万个条目已改为 `ubuntu:ubuntu`；`releases/**` 全部保持 root（改前后条目数一致）。`shared/` 里另有 7,667 个 root 属主文件，是 pnpm 缓存与 `releases/*/node_modules` 共享 inode 的硬链接，改它们的属主会连带改掉发布目录文件，所以保持 root，属预期。
- 隔离：服务进程为 `ubuntu` 身份，`NoNewPrivileges` 内核标志已生效，`/home` 在服务内不可见（SSH 密钥读不到），可写 `shared/`；`ubuntu` 不在 docker 组，服务无法操作同机的 ChatBot 容器。仓库的单元模板已加 `ProtectHome=yes`，`manage_release.py` 的 `--user` 默认值改为 `ubuntu`。
- 清理：冗余的完整密钥副本 `/home/ubuntu/oopz-upload.env`（与本机 `.env` 逐字节相同）、Qoder 测试产物与 SDK probe、`/tmp`、`/var/tmp`、`/var/crash` 中属原账号的残留（含一份 Chromium 崩溃转储）、`artifacts/` 里的 `.pyc` 缓存均已删除。
- 账号：原专用账号及其同名组、家目录已于 2026-10-04 删除；`/etc/subuid`、`/etc/subgid` 中无残留；全盘没有属于该账号 uid/gid 的文件。同时删除了 `/var/lib/` 下一个只给该组开放读取的 ChatBot 上下文脱敏快照目录（临时生成、可重新生成）。全盘仍有 21 个属 uid/gid 501 的无主条目，属腾讯云监控 agent（`/usr/local/qcloud/stargate`），与本次无关，保持原样。
- 迁移后实测：OOPZ 单元 active、`User=ubuntu`、`NoNewPrivileges=yes`、`ProtectHome=yes`；`maibot.service` active，`napcat` 容器运行时长未被重置，ubuntu 的 `authorized_keys` 未变。

Git 对应关系：这四个发布提交来自此前丢失的工作环境的 Git 历史，不在本仓库历史中。四个发布包经 SHA-256 核对后逐个导入分支 `recovery/server-v0.11.15-snapshots`（仅为快照，非原始历史）。本分支以 `037b988` 为基线采用最新快照 `4291aa894f7a` 作为 Linux 部署基础，其 Linux 工具与 `codex/fix-ubuntu-analysis-guard` 同源。

较早的本地 R1–R6 Linux 实现（分支 `legacy/local-linux-r1-r6`）与上述服务器线相互独立，功能上已基本被服务器线覆盖；两者只能保留一套。该实现的未提交改动保存在仅本地的 `wip/local-main-uncommitted-20261003`，未并入 `main`。已知差异：服务器线对仍存活的锁 PID 一律保守拒绝切换，不做 `/proc` 命令行归属判断，迁移后遇 PID 巧合只会误拒绝、不会误放行。

### Windows 部署已废弃

用户于 2026-10-03 确认 Windows 生产服务器已废弃。Windows 部署线（脚本、从零部署指南、历史部署记录）已在生产验证，流程保持原样，保留在分支 `windows-legacy`（`037b988`，发布提交 `3be0c95`）、`v0.11.x` 标签和 GitHub Release 中；`main` 不再包含它，也不应据此判断 Ubuntu 服务器。Windows 更新脚本读取 GitHub 的 latest Release，Linux 发布须使用 `linux-v*` 标签且不设为 Latest。

本地检出布局（同一个 Git 仓库的两份工作副本）：`D:\AI-Cloud-Linux\OOPZ_Capture` 为 `main`（Linux/Ubuntu 主线），`D:\AI-Cloud-Linux\OOPZ_Capture_Windows` 为 `windows-legacy`（保留的 Windows 部署线）。`scripts/build_release.ps1` 仍是发布包构建入口，应用代码保持跨平台。

2026-10-03隔离录音候选：正式基线归档恢复后独立增加capture-only及发起人确认卡；仅本地验证，无真实部署变更。工作环境重置导致之前未交付的候选丢失，本候选重新验证，不沿用其测试或提交身份。

最新Ubuntu准备基线为 `v0.11.15-bf8a2d777692`（提交 `bf8a2d777692a0b4ea68c659f5593c4fb806cd02`）：目标机395 passed、9项Windows/PowerShell条件跳过；真实SDK浏览器三项、CPU公开音频及受管理Chromium的PDF检查通过，后两项还在NoNewPrivileges/PrivateTmp的临时systemd环境验证。应用未激活，current/业务服务尚未建立，用户业务配置未提供。

后续已获批的WeasyPrint开发：新增Linux显式PDF后端、严格资源访问限制及独立渲染进程；当前仍在本地验证/审计，未据此改写目标版本。生成旧报告示例仅本地转换用户批准的原始Markdown，不请求模型、不发布飞书；私有样本与产物不进入Git或发布包。

2026-10-02部署准备：远端Linux候选已推进至 `4e38d1dd50dd2fd90d10aff37341d7ee34082014`；在独立修复分支补齐Linux正式构建、Chromium准备检查及固定HOME。用户已批准本次替代独立发布审计，检查原基线、敏感信息、受保护路径、Git历史和大文件并保存脱敏报告；不宣称运行不可用的release-audit技能。代码/依赖准备与业务配置分离；在用户稍后提供配置前，不激活飞书网关，不影响其他项目。该条仅记录准备状态，不表示Ubuntu真实链路或生产服务已验收。

Ubuntu 24.04实机首次准备：`v0.11.15-f19cb4a60606`（提交 `f19cb4a6060605ef315f126577cf241c807b7b16`）正式包已通过prepare，模型哈希已校验；网关未激活、用户业务配置尚未提供。三项真实浏览器捕获/PCM/probe测试通过；全量测试暴露一项请求先后顺序假设，独立复现后仅修复测试。项目PDF遇到宿主Chromium沙箱限制，尚未通过该门槛；不将此前浏览器成功推定为PDF成功，不修改全局安全策略。另一个项目的系统Python和进程保持原状，OOPZ采用独立Python与共享目录。

2026-10-01复核修复：Linux切换guard已补 `last_job` 与真实分析阶段检查，拒绝无锁但已登记/进行中的分析。本机Windows单元回归309 passed、13 skipped；Linux真实管理入口和POSIX用例被跳过，Ubuntu实机与生产服务未操作。补丁交付分支为 `codex/fix-ubuntu-analysis-guard`，正式发布及部署由接手任务办理。

Ubuntu开发候选：在独立实现分支加入跨平台配置/Node/PDF、Linux安装事务和中断恢复，开发验证环境为Debian 13，非Ubuntu 24.04。CPU VAD及固定模型公开音频转写成功；Chromium被沙箱socket权限阻止。实际systemd、Windows回归、飞书/API及数小时负载未验收。未提交或发布正式包，生产仍保持原状。详见 [验证记录](VALIDATION_UBUNTU.md)。

最低部署要求：4 vCPU / 8 GiB；低于此配置不作为支持的部署目标。当前新服务器 CPU/内存达到最低要求，仍须完成实际负载验收。

## 生产目录约定（Ubuntu）

```text
/opt/oopz/
  current -> releases/<release-id>       # 计划中的当前版本链接（尚未建立）
  releases/<release-id>/                 # 每次发布独立目录，含独立 .venv/node_modules
  shared/config/.env                     # 生产配置，发布间共享
  shared/{models,output,feishu_state,logs,tools,python,browsers,cache,home}
  artifacts/                             # 发布包与 SHA-256 文件
```

版本目录中的 `.env`、`models`、`output`、`feishu_state`、`logs` 是指向 `shared/` 的符号链接，代码回滚不会回滚或清空业务数据和配置。程序对 `.env` 的写入必须原地进行：改成“临时文件替换”式写入会切断链接，使修改在下次升级时丢失。

## 服务器专属差异（允许存在）

- `.env` 中的密钥、账号、控制群 ID、绝对数据路径及性能参数；实际值不得进入 Git 或本文档。
- systemd 单元状态、logrotate、云防火墙/安全组、磁盘告警和系统补丁策略。
- 模型、会话数据、飞书状态和日志。

除以上项目外，生产代码、Schema、模板、脚本及依赖声明必须来自同一发布包，不允许服务器手改。

## Ubuntu 验收待办

- [x] 建立 `current` 链接与 systemd 单元，使服务随开机恢复（2026-10-04 完成）。
- [ ] 在真实群里验证"录音 → 自动分析 → 图和 digest.md 发到群"全链路，并检查真实头像下载、出入记录、音频删除（部署已完成，等第一次真实录音）。
- [ ] 在目标机验证无人频道退出、服务重启恢复与版本回滚演练。
- [ ] 清理测试残留（测试网关进程、`artifacts/` 中的探测脚本与分片，磁盘仅剩约 12 GB）并设置告警。
- [ ] 完成 4 vCPU 下的长时间负载与 15 分钟转写期限复核。
