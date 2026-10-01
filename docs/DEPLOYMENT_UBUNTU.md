# Ubuntu 24.04 LTS 部署指南

> **状态声明：本指南描述的目标平台尚未完成实际验收。** 截至 2026-10-01，Linux 适配与 2026-10-01 审查修复（R1–R6：Node 版本契约、更新器先校验后执行、分阶段安装事务、活动任务保护、PDF 浏览器闭环、首次配置流程）已并入仓库，并通过全部单元/行为测试与 Windows 回归；但尚未在真实 Ubuntu Server 24.04 机器上执行安装、录音、转写、PDF、飞书与持续负载验收。在完成验收前，Ubuntu 不是已支持的生产运行目标；下文标注“未实测”的步骤按文档推演编写，首次执行可能需要现场修正。Windows Server 路径仍是唯一已完成验收的部署目标。

## 1. 系统要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Ubuntu Server 24.04 LTS x86_64，无桌面环境 |
| 资源 | 初始要求与 Windows 相同：4 vCPU / 8 GiB，80 GiB SSD；Linux 实际最低资源必须经真实负载测试重新确定 |
| 网络 | 仅出站访问 OOPZ、飞书长连接、分析 API、PyPI/npm/GitHub/魔搭/nodejs.org；无业务入站端口；SSH 管理端口只允许可信来源 |
| 用户 | 应用以专用普通用户 `oopz` 运行（前置脚本创建），不以 root 运行网关 |
| Node | **22 LTS ≥22.12.0**（pnpm-lock 中 puppeteer 25.7.0 的 engines 契约；发行版 Node 18 不满足，前置脚本会检测并安装固定版本运行时） |

## 2. 与 Windows 部署的关系

- 共用同一 Git 仓库、同一正式 Release ZIP 和同一套核心 Python/Node 代码；平台差异只在启动器（`.bat`/PowerShell ↔ systemd/bash）、路径与浏览器查找。
- 正式发布包仍由 Windows 端 `scripts/build_release.ps1` 从干净 HEAD 构建，ZIP 同时包含两平台部署文件（`.gitattributes` 保证 bash 脚本 LF；`git archive` ZIP 不携带 Unix 权限位，统一以 `bash <script>` 调用，安装器解压后补 `chmod +x`）。`.sha256` 校验文件为 LF 行尾，Linux `sha256sum -c` 可直接校验；安装器同时兼容 CRLF 旧包。
- 目录布局对齐 Windows：`/opt/oopz/{releases,current,shared,artifacts}`；发布目录内 `.env`、`models`、`output`、`feishu_state`、`logs`、`tools/node` 均为指向 `shared` 的符号链接；回滚不回滚数据。

## 3. 从零安装（分阶段 bootstrap，未实测）

首次安装分三个阶段，解决“安装需要配置、配置需要程序”的循环：**准备**（不需要 `.env`）→ **配置/飞书绑定** → **激活**。

### 3.1 系统前置

```bash
sudo bash scripts/linux/install_prerequisites.sh --install-root /opt/oopz
```

内容：apt 安装 Python 3.12、`python3.12-venv`、unzip、curl、tar/xz、`fonts-noto-cjk`。**Node 策略（R1）**：若系统已有 Node ≥22.12.0 则直接链接进 `shared/tools/node`；否则从 nodejs.org 下载**固定版本 Node 22 LTS tarball**（当前 `v22.14.0`，脚本内 `NODE_PIN`），用官方 `SHASUMS256.txt` 校验后装入 `shared/tools/node-runtime/`，`shared/tools/node` 指向它。**不安装发行版 nodejs**（18.x 违反锁文件契约）。升级 Node 版本 = 修改 `NODE_PIN`（保持 ≥22.12.0，或随 pnpm-lock 变更同步）并重跑本脚本。脚本同时创建系统用户 `oopz` 与目录布局。

### 3.2 发布包准备（不需要 .env；失败可同包重试）

```bash
# 上传 oopz-capture-v<版本>-<提交>.zip 与同名 .sha256 到 /opt/oopz/artifacts/ 后：
sudo bash scripts/linux/install_release.sh \
  -f /opt/oopz/artifacts/oopz-capture-v<版本>-<提交>.zip --prepare-only
```

准备阶段（**不触碰服务、current、systemd 单元**）：SHA-256 校验 → Node 版本门槛（`check_node.py` 对照锁文件契约，不满足即拒绝）→ 解压到 `releases/<id>` → 独立 venv 与 Python 依赖 → 以 `oopz` 用户安装并启动验证 Playwright Chromium（含 `install-deps` 系统库）→ 固定修订版 SenseVoiceSmall → pnpm 锁定 Node 依赖（在**新版本目录**内校验导入）→ **以服务用户真实渲染一份中文 PDF**（`MD_TO_PDF_CHROME_PATH` 透传，HOME 与服务单元一致）→ import 冒烟 → 写 `.prepare-complete` 标记。任何一步失败：目录保留供诊断，**同包重跑会清理未完成目录并重来**；已是当前目标的目录永不被重试清除；已完成的准备重跑直接跳过。

### 3.3 生产配置与飞书绑定（激活前完成）

```bash
sudo cp /opt/oopz/releases/<release-id>/.env.example /opt/oopz/shared/config/.env
sudo nano /opt/oopz/shared/config/.env       # 填 OOPZ_* 与全部 ANALYZER_* 项
sudo chmod 600 /opt/oopz/shared/config/.env && sudo chown oopz:oopz /opt/oopz/shared/config/.env

# 飞书一键配置直接使用已准备好的版本环境（此时尚无 current）：
sudo -u oopz -H env HOME=/opt/oopz \
  /opt/oopz/releases/<release-id>/.venv/bin/oopz-feishu setup
```

扫码创建/更新应用、写回凭据后，把机器人邀请进目标群完成 `OOPZ_FEISHU_ADMIN_CHAT_ID` 绑定（可先用 `discover-ids` 获取群 ID 手工写入）。SSH 终端无法显示二维码时加 `--url-only`；应用版本发布与公开授权仍按飞书指南执行。

### 3.4 激活（事务切换 + 健康检查）

```bash
sudo bash scripts/linux/install_release.sh -f <artifact.zip> --activate
```

激活阶段：活动任务保护（见 §4；进入时与停服前各检查一次，防止准备期间新任务进入）→ 链接 `.env` → 停旧服务（仅当在运行）→ 写 systemd 单元与 logrotate → `daemon-reload` → 原子切换 `current` → 启动 → 健康等待。**健康判定（R6）**：`.env` 已绑定控制群 → 等待“飞书长连接已就绪”；尚未绑定 → 同时接受“尚未绑定控制群”首启提示，因此**新绑定等待不会被健康超时误判回滚**。任何失败：恢复上一 `current` 链接、由旧版本模板重写单元、`daemon-reload`、仅当旧服务原本在运行时重启它；首次安装失败则移除 `current` 与单元并 `disable`，不留自动重启循环。

日常更新/回滚（已有配置的常规场景）可直接运行安装器（准备+激活一体），或使用：

```bash
sudo bash /opt/oopz/current/scripts/linux/update_release.sh     # GitHub 匿名更新
sudo bash /opt/oopz/current/scripts/linux/rollback_release.sh   # 回滚（--to 可指定版本）
```

更新器信任模型（R2）：下载 ZIP 与**按文件名精确配对**的同名 `.zip.sha256` 后，由**当前已安装的受信任代码**先校验，校验通过才从新包提取并执行其安装器；错误哈希、缺配对附件、截断包都在执行新包代码前失败。回滚同样带活动任务保护与“启动/健康失败恢复原状”。

## 4. 活动任务保护（R4）

安装与回滚在停服/切换前调用 `scripts/linux/check_active_tasks.py`：

- `controller.json` 存在但**损坏/不可读 → 拒绝切换**（不猜测安全），只有显式 `--force` 才能继续。
- 服务运行中：`active`（录音/转写中）或 `last_job.status=analyzing`（后台分析中）→ 拒绝。服务停止时这两项是持久化的可中断状态，不阻塞运维。
- 全量扫描会话分析锁（`analysis/.prepare.lock`、`analysis/.run.lock`、`analysis_variants/*/.run.lock`）：持有进程存活（Linux 上还要求 `/proc` 命令行属于 python/oopz，排除跨机迁移的 PID 巧合）→ 拒绝；死 PID 或无关进程的同名 PID → 视为陈旧，不阻塞。不可判读/非常规文件锁按阻塞处理。
- `--force` 只能显式传参，任何脚本不会隐式启用。

## 5. 服务管理与 PDF 浏览器闭环（R5）

```bash
systemctl status oopz-capture
sudo systemctl start|stop|restart oopz-capture
journalctl -u oopz-capture -f
tail -f /opt/oopz/shared/logs/feishu_runtime.log     # logrotate 周轮转 + 50M 截断
```

单元要点：`User=oopz`、`WorkingDirectory=/opt/oopz/current`、`HOME=/opt/oopz`（Playwright 浏览器与 `.cache` 位置）、`Restart=on-failure`、`KillMode=control-group`（连同 Chromium 与转写子进程结束）、`TimeoutStopSec=90`、`NoNewPrivileges`。

PDF 浏览器来源闭环：`findChrome` 依次查找 `MD_TO_PDF_CHROME_PATH` → **`$HOME/.cache/ms-playwright/chromium-*/chrome-linux/chrome`（录音后端已安装的同一 Chromium，新版本优先）** → 发行版 Chrome/Edge/Chromium 路径 → snap。安装器在准备阶段以服务用户真实渲染中文 PDF 验证该链路（字体由 `fonts-noto-cjk` 提供）。因此“录音 Chromium 启动检查通过”与“PDF 可用”指向同一二进制——但二者仍分别验证，录音验收不可被 PDF 成功替代。systemd 环境下的端到端渲染仍属未实测项。

## 6. 从 Windows 迁移数据（未实测）

1. 两侧网关停止后打包 Windows `shared\`（`.env`、`models\SenseVoiceSmall`、`output`、`feishu_state`、`logs`），解到 `/opt/oopz/shared/` 对应位置，属主改为 `oopz`。
2. **绝对路径清点（审查项）**：`.env` 为键值与相对根，无需改写；但 `output/<session>/report_delivery.json` 的 `pdf_path` 与飞书 outbox（`feishu_state/send_requests`、`replies`）中的 `file_path` 保存**绝对路径**，迁移后失效。处理原则：历史报告如需可获取，按相对布局核对/修正对应字段；**未发送附件不得因路径失效而重复投递**——迁移完成后先在测试群核对投递行为，再开放生产群。
3. **机器相关锁（审查项）**：分析/准备锁中的 PID 属于原机器，“存在同名 PID”不能证明持有者有效（本仓库 guard 已按命令行甄别）。两端已停止是迁移前提；应用侧“死 PID 回收”与“中断可恢复”逻辑会处理陈旧锁，**不要一键清空整个 `feishu_state`**。
4. 首次激活后执行“状态”与一次短录音验证共享数据读写；保留迁移前打包作为可恢复备份。

## 7. 已知风险：Ubuntu 24.04 的 Chromium 沙箱（未实测）

Ubuntu 24.04 默认 `kernel.apparmor_restrict_unprivileged_userns=1`，可能阻止 Playwright Chromium 的命名空间沙箱启动（录音后端与 PDF 共用该二进制，影响一致）。补救顺序：优先为 Chromium 安装配套 AppArmor 配置（如安装官方 `.deb` Chrome/Edge，并评估以 `MD_TO_PDF_CHROME_PATH` 指向它）；或经安全评估后对本机放宽 userns 限制（`sysctl kernel.apparmor_restrict_unprivileged_userns=0` 并持久化）。服务单元的 `NoNewPrivileges` 与该限制的相互作用需实测确认；**不得以默认关闭系统安全限制来掩盖问题**。哪个方案有效须在真实机器验证后回填本节。

## 8. 停止语义与平台差异

systemd 停止发送 SIGTERM：网关完成当前维护步骤后断开长连接、取消任务（含进行中录音），由控制器持久化为可恢复/可删除状态；`TimeoutStopSec=90` 后仍未退出则以 cgroup 整组终止。真实 SDK、录音子进程、`asyncio.to_thread` 分析线程在 SIGTERM 下的行为**尚未实机验证**：须分别在录音、转写、等待 API 时停止并检查生命周期、锁与进程，再重启确认不卡住、不重复投递（FakeChannel 单元测试不构成该验收）。重启后通过“状态”“待分析”“删除会话”恢复，失效锁按 §4 规则回收。

| 差异点 | Windows | Linux（本指南） |
| --- | --- | --- |
| 启动/守护 | `.bat` 启动器 + PowerShell 观察窗 | `oopz-capture.service`（journald + logrotate） |
| 安装/更新/回滚 | `install_release.ps1` 等 | `scripts/linux/*.sh`（分阶段事务、guard、先校验后执行） |
| Node 运行时 | `tools/node/node.exe`（shared junction） | 系统 Node ≥22.12 或固定 Node 22 LTS tarball（`shared/tools/node`） |
| PDF 浏览器 | Chrome/Edge 固定路径 | Playwright 缓存 Chromium → 发行版浏览器 → snap |
| 中文字体 | 系统自带 | `fonts-noto-cjk` |
| 共享链接 | junction + `.env` 硬链接 | 符号链接（原地写 `.env` 语义两平台一致） |
| 录音浏览器 | Playwright Chromium | 同左 + `playwright install-deps`；AppArmor userns 风险见 §7 |

## 9. 验收要求（完成前不得宣称 Linux 已支持）

依赖安装与真实音频转写（L01）、无桌面录音与断线重连（L02）、中文 PDF（L04/L16）、配置写回与升级保留（L05）、`current` 切换共享数据一致（L06/L07）、开机自启与停止无残留进程（L08/L09，含 §8 的 SIGTERM 场景矩阵）、干净机安装与失败不切换（L10/R3/R6）、升级与回滚各一次含失败注入（L12/R2/R3）、真实会话活动任务保护（R4）、数小时连续录音资源指标（L16）、Windows 回归（L17）。本仓库的 bash 隔离行为测试（`tests/test_linux_deployment_flow.py`：注入 pip/模型/Node/单元写入/启动/健康失败并断言副作用）不等于 Ubuntu/systemd 实机验收。缺少环境时交付未验证清单，不得标记为生产就绪。
