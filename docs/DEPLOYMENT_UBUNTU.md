# Ubuntu 24.04 LTS 部署指南

> **状态声明：本指南描述的目标平台尚未完成实际验收。** 截至 2026-09-30，Linux 适配（跨平台代码、安装/升级/回滚脚本、systemd 服务、文档）已并入仓库并通过全部单元测试与 Windows 回归（281 passed、1 skipped），但尚未在真实 Ubuntu Server 24.04 机器上执行安装、录音、转写、PDF、飞书与持续负载验收。在完成验收前，Ubuntu 不是已支持的生产运行目标；下文所有标注“未实测”的步骤都按文档推演编写，首次执行时可能需要现场修正。Windows Server 路径仍是唯一已完成验收的部署目标，本文不替代它。

## 1. 系统要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Ubuntu Server 24.04 LTS x86_64，无桌面环境 |
| 资源 | 初始要求与 Windows 相同：4 vCPU / 8 GiB，80 GiB SSD；Linux 实际最低资源必须经真实负载测试重新确定，未实测前不得宣称更低配置可用 |
| 网络 | 仅出站访问 OOPZ、飞书长连接、分析 API、PyPI/npm/GitHub/魔搭；无业务入站端口。SSH/RDP 替代管理端口只允许可信来源 |
| 用户 | 应用以专用普通用户 `oopz` 运行（安装脚本创建），不使用 root 运行网关 |

## 2. 与 Windows 部署的关系

- 两种平台共用同一 Git 仓库、同一正式 Release ZIP 和同一套核心 Python/Node 代码；平台差异只存在于启动器（`.bat`/PowerShell ↔ systemd/bash）、路径与浏览器查找。
- 正式发布包仍由 Windows 端 `scripts/build_release.ps1` 从干净 HEAD 构建，ZIP 内同时包含 Windows 与 Linux 部署文件（`.gitattributes` 保证 bash 脚本为 LF、git 索引保证脚本可执行位）。
- 服务器目录布局对齐 Windows 生产约定：`/opt/oopz/{releases,current,shared,artifacts}`，`shared` 保存 `.env`、模型、会话、飞书状态和日志；`current` 为指向 `releases/<release-id>` 的符号链接（Windows 上是 junction）。
- 发布目录内的 `.env`、`models`、`output`、`feishu_state`、`logs`、`tools/node` 均为指向 `shared` 的符号链接；回滚不回滚数据。

## 3. 从零安装（未实测，按 Windows 主流程等价推演）

### 3.1 系统前置

```bash
# 将发布包上传/下载后，解出 scripts/linux/ 并以 root 执行：
sudo bash scripts/linux/install_prerequisites.sh --install-root /opt/oopz
```

脚本内容：apt 安装 Python 3.12、`python3.12-venv`、Node.js + npm、unzip、curl、`fonts-noto-cjk` 中文字体（PDF 渲染必需）；创建系统用户 `oopz`（家目录 `/opt/oopz`）；创建目录布局并把共享 `tools/node/node` 链接到系统 Node。Ubuntu 24.04 的 Node.js 18 满足 PDF 工具要求。

### 3.2 生产配置

```bash
# .env.example 位于解压后的发布目录（releases/<release-id>/.env.example）：
sudo cp /opt/oopz/releases/<release-id>/.env.example /opt/oopz/shared/config/.env
sudo nano /opt/oopz/shared/config/.env   # 填写全部 OOPZ_* 与 ANALYZER_* 项
sudo chmod 600 /opt/oopz/shared/config/.env
sudo chown oopz:oopz /opt/oopz/shared/config/.env
```

配置契约与 Windows 完全一致：全部 `ANALYZER_*` 必填项、飞书凭据、`OOPZ_LOGIN_PHONE/PASSWORD`。新增可选 `OOPZ_NODE_PATH`（PDF Node 运行时绝对路径；留空时按平台自动查找，Linux 上依次尝试 `tools/node/node` 与系统 PATH 的 `node`）。飞书 App ID/Secret 可在安装完成后用一键配置写入（见 3.4）。

### 3.3 安装 Release（事务式）

```bash
sudo bash scripts/linux/install_release.sh \
  -f /opt/oopz/artifacts/oopz-capture-v<版本>-<提交>.zip \
  --install-root /opt/oopz --health-timeout 120
```

脚本按序执行：SHA-256 校验 → 解压并读取 `RELEASE_MANIFEST.json` → 已装同版本拒绝 → 建独立 `.venv` → 安装 `.[speech,feishu]` 与 `pip check` → 以 `oopz` 用户安装并启动验证 Playwright Chromium（录音浏览器；`playwright install-deps chromium` 以 root 补系统库）→ 下载/校验固定修订版 SenseVoiceSmall → pnpm 锁定安装 Node 依赖 → Node 依赖导入检查 → `pip freeze` 存档 → import 冒烟 → 写入 systemd 单元与 logrotate 配置 → 拒绝在有活动录音/分析任务时切换（可用 `--force` 覆盖）→ 停旧进程 → 原子切换 `current` → 启动服务 → 在 `shared/logs/feishu_runtime.log` 中等待“飞书长连接已就绪”。

任何一步失败：`current` 自动回退到旧版本并重启服务；失败版本目录保留供诊断。模型下载或校验失败不切换版本。

### 3.4 飞书一键配置（交互）

```bash
sudo -u oopz -H env HOME=/opt/oopz \
  /opt/oopz/current/.venv/bin/oopz-feishu setup
```

流程与 Windows 相同：扫码创建/更新应用、申请 11 项权限、配置长连接与卡片回调、自动写入 `.env`。SSH 终端无法显示二维码时加 `--url-only`，用手机打开确认链接。完成后需发布应用版本并邀请机器人进目标群（首次入群自动绑定控制群 ID）。

### 3.5 服务管理（systemd）

```bash
systemctl status oopz-capture      # 状态
sudo systemctl start oopz-capture  # 启动（开机自启已在安装时启用）
sudo systemctl stop oopz-capture   # 停止
sudo systemctl restart oopz-capture
journalctl -u oopz-capture -f      # 内核级输出
tail -f /opt/oopz/shared/logs/feishu_runtime.log   # 应用主日志（logrotate 周轮转 + 50M 截断）
tail -f /opt/oopz/shared/logs/feishu_error.log
```

单元要点：`User=oopz`、`WorkingDirectory=/opt/oopz/current`、`Restart=on-failure`（10 秒后重启）、`KillMode=control-group`（停止时连同 Chromium 与转写子进程一并结束）、`TimeoutStopSec=90`。

## 4. 停止语义与未完成录音

- systemd 停止服务发送 SIGTERM；网关在完成当前维护步骤后断开飞书长连接、取消其余任务（含进行中的录音）。
- 未完成的录音由控制器持久化为可恢复/可删除状态，与 Windows 上的停止语义一致；重启后通过“状态”“待分析”“删除会话”处理，不会自动重发消息或重复分析。
- 转写子进程、分析锁均带 PID 记录；被 SIGKILL 留下的失效锁会在下次启动时由“原进程已不存在”检查回收。

## 5. 更新与回滚（未实测）

```bash
# 一键更新到最新正式 Release（GitHub 匿名下载、校验、事务安装）：
sudo bash /opt/oopz/current/scripts/linux/update_release.sh

# 指定回滚目标：
sudo bash /opt/oopz/current/scripts/linux/rollback_release.sh                 # 回到最近一个其他版本
sudo bash /opt/oopz/current/scripts/linux/rollback_release.sh -t v0.11.15-<提交>
```

- 更新前脚本读取 `current/RELEASE_MANIFEST.json`，已是最新版则不重装不重启；存在活动录音/分析任务时拒绝切换（`install_release.sh --force` 可覆盖，但需自行承担中断）。
- 回滚只切换代码与依赖，`shared` 不被覆盖；这与 Windows 侧 `rollback_release.ps1` 语义一致。
- 成功升级与失败回滚均须各实测一次后才可宣称可用。

## 6. 从 Windows 迁移数据（未实测）

1. 停止两侧网关；打包 Windows 服务器的 `shared\`（`.env`、`models\SenseVoiceSmall`、`output`、`feishu_state`、`logs`）。
2. 解到 Linux `/opt/oopz/shared/` 对应位置；`.env` 内无 Windows 绝对路径（配置全部为键值或相对根），无需改写。
3. 机器相关的 PID 状态（`feishu_state` 中运行期锁、`controller.json` 的 `active` 字段）在停机状态下属过期数据：控制器启动时的失效锁回收与 `controller_restarted` 归位逻辑会处理，不要手工清空业务状态。
4. 首次安装后做一次“状态”与一次短录音验证共享数据可读写；保留迁移前打包文件作为可恢复备份。

## 7. 已知风险：Ubuntu 24.04 的 Chromium 沙箱（未实测）

Ubuntu 24.04 默认开启 `kernel.apparmor_restrict_unprivileged_userns=1`，可能阻止 Playwright Chromium 的命名空间沙箱启动，使 3.3 的“Voice Chromium launch check”失败。补救顺序：

1. 优先安装发行版/官方 `.deb` Chrome 或 Edge（其软件包自带 AppArmor 允许配置），再把 `MD_TO_PDF_CHROME_PATH` 指向它——注意该变量只影响 PDF 渲染浏览器，录音仍用 Playwright Chromium；
2. 或为本机显式放宽限制：`echo 'kernel.apparmor_restrict_unprivileged_userns = 0' | sudo tee /etc/sysctl.d/99-oopz-chromium.conf && sudo sysctl --system`，并评估安全权衡；
3. 具体哪个方案有效须在真实机器上验证后回填本节。

## 8. 平台差异清单

| 差异点 | Windows | Linux（本指南） |
| --- | --- | --- |
| 启动/守护 | `启动OOPZ全流程.bat` + 两个 PowerShell 观察窗 | `oopz-capture.service`（journald + logrotate） |
| 安装/更新/回滚 | `install_release.ps1` / `update_latest_release.ps1` / `rollback_release.ps1` | `scripts/linux/*.sh`（同构事务） |
| PDF Node | `tools/node/node.exe`（shared junction） | `shared/tools/node/node` → 系统 Node，或 `OOPZ_NODE_PATH` |
| PDF 浏览器 | Chrome/Edge 注册表路径查找 | `/usr/bin/google-chrome*`、`/usr/bin/microsoft-edge*`、`/usr/bin/chromium*`、snap |
| 中文字体 | 系统自带 | `fonts-noto-cjk`（前置脚本安装） |
| 共享链接 | junction + 硬链接 `.env` | 符号链接（程序原地写 `.env` 的语义两种平台均保留） |
| 录音浏览器 | Playwright Chromium（同一 SDK 代码路径） | 同左，另需 `playwright install-deps` 系统库 |

## 9. 验收要求（完成前不得宣称 Linux 已支持）

按需求表执行并记录证据：依赖安装与真实音频转写（L01）、无桌面录音与断线重连（L02）、中文 PDF（L04）、配置写回与升级保留（L05）、`current` 切换后共享数据一致（L06/L07）、开机自启与停止无残留进程（L08/L09）、干净机安装与失败不切换（L10）、升级与回滚各一次（L12）、数小时连续录音资源指标（L16）、Windows 回归（L17）。缺少环境时交付未验证清单，不得标记为生产就绪。
