# 部署状态基线

> 这是本地代码与生产服务器差异的唯一事实来源。任何部署相关修改和每次生产发布都必须同步更新本文件。禁止记录密钥、密码、完整服务器地址或个人信息。

更新时间：2026-09-11

## 当前状态

| 项目 | 本地开发环境 | 生产服务器 | 是否需同步 |
| --- | --- | --- | --- |
| Git 提交 | `main` 跟踪 GitHub 仓库；精确提交以 `git rev-parse HEAD` 和发布清单为准 | 尚未部署 | 是：应用部署后登记发布 ID |
| 应用版本 | `0.11.9` | 应用待部署；部署使用 [Release v0.11.9](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.9)，精确提交见包内清单 | 是 |
| 操作系统 | Windows | 已创建 Windows Server 2022 x64 中文版试用实例，应用尚未安装验收 | 待实施 |
| 计算与磁盘 | 本机 CPU 限额实验已完成，不能代替云端验收 | 经济型 e：2 vCPU / 4 GiB、40 GiB ESSD Entry；运行中，容量待实测 | 属于低负载试运行目标，未验证生产稳定性 |
| 网络与管理 | 本地开发网络 | 普通安全组默认允许出站；现有 TCP 3389、TCP 22 和 ICMP 入站规则，本次未更改 | 无需新增业务端口；RDP 当前面向所有 IPv4，需按实际管理来源收紧 |
| Python | 3.12.14 | 用户终端确认 3.12.10 x64，路径为 `C:\OOPZ\tools\Python312\python.exe` | 基础环境已准备，应用安装待完成 |
| Node.js | 本地已安装，版本待确认 | 用户终端确认 24.21.0，npm/npx 11.19.0，Edge 已存在 | PDF 与应用运行待验收 |
| 基础环境引导 | `main` 的引导脚本仅安装运行依赖，不再检测或安装 Git / GitHub CLI；已完成检查与模拟分支测试 | 等待用户在服务器执行；不假定工具已安装 | 此后续脚本未加入现有 v0.11.9 ZIP，首次部署使用在线文档代码 |
| 应用依赖 | Python / Node 依赖已安装 | 用户日志确认续装、pip check、关键模块导入与模型校验通过 | 不代表录音端到端验收通过 |
| 录音浏览器 | 默认 Playwright Chromium 通道 | 用户日志确认缺少匹配的 Chromium，可执行文件缺失导致连接重试超时；用户正在补装 | 补装后须启动验证并发起新录音；系统 Edge 不能代替此检查 |
| ASR 模型 | 本地 `models/SenseVoiceSmall`（不进 Git） | 用户日志确认固定修订版模型已 downloaded-and-verified | 保留现有模型，不重复下载 |
| 生产配置 | 本地 `.env`（不进 Git）；全部 `ANALYZER_*` 项显式配置 | 用户已准备，安装脚本通过文件存在性检查；未读取或验证值 | 启动时仍需通过配置校验 |
| 输出/状态/日志 | `output`、`feishu_state`、`logs`；分析检查点与中断恢复状态保存在会话目录 | 尚未创建 | 否：属于各环境持久数据，禁止互相覆盖；重启后仅回收已退出进程留下的分析锁 |
| 启动方式 | 交互式批处理 | 目标为任务计划程序调用稳定 `current` 路径 | 待实施 |
| 代码远端 | `origin=https://github.com/XK205E3n/OOPZ_Capture.git`（2026-09-10 实查 Public；本次未改变可见性） | 可通过 PowerShell 匿名下载固定 Release，ZIP / SHA-256 / 清单校验已在本机实测 | 服务器无需 GitHub 登录；业务凭据仍仅本地保存 |

## 生产目录约定

默认安装根目录为 `C:\OOPZ`：

```text
C:\OOPZ\
  current -> releases\<release-id>       # 只读使用的当前版本目录联接
  releases\<release-id>\                 # 每次发布独立目录，含独立 .venv/node_modules
  shared\config\.env                     # 生产配置，发布间共享
  shared\models\SenseVoiceSmall\         # 大模型，发布间共享
  shared\output\                          # 会话与报告
  shared\feishu_state\                    # 网关状态与审计
  shared\logs\                            # 运行日志
  shared\tools\node\                      # PDF 渲染的 node.exe，发布间共享
  artifacts\                              # 已上传发布包与 SHA-256 文件
```

发布目录中的 `.env` 使用同盘硬链接指向 `shared\config\.env`；`models`、`output`、`feishu_state`、`logs` 使用目录联接指向 `shared`，`tools\node` 联接到 `shared\tools\node`。因此代码回滚不会回滚或清空业务数据和配置。程序对 `.env` 的写入（首次入群控制群绑定、一键配置、群内“设置”命令）必须保持原地写：改回“临时文件替换”式写入会切断硬链接，使这些修改在下次升级时丢失（见部署变更记录）。

## 服务器专属差异（允许存在）

- `.env` 中的密钥、账号、控制群 ID、绝对数据路径及性能参数；实际值不得进入本文档。
- Windows 任务计划程序、云防火墙/RDP 白名单、页面文件、磁盘告警和系统补丁策略。
- 模型、会话数据、飞书状态和日志。

除以上项目外，生产代码、Schema、模板、脚本及 Python/Node 依赖声明必须来自同一发布包，不允许服务器手改。

## 首次部署待办

- [x] 将已审查、测试的当前版本生成并上传为首个 GitHub Release（Release ID 以发布清单为准）。
- [x] 建立 Git 远端并推送 `main`；当前仓库公开可读，不包含生产配置或运行数据。
- [ ] 准备 Windows Server，安装 Python 3.12 x64、Node.js LTS、Git、Chrome/Edge。
- [ ] 建立 `C:\OOPZ\shared`，安全创建生产 `.env`，由安装脚本从魔搭社区下载并校验 SenseVoiceSmall。
- [ ] 本地生成首个发布包及 SHA-256，传到 `C:\OOPZ\artifacts`。
- [ ] 执行服务器安装脚本，验证飞书长连接、录音、转写、分析和发布。
- [ ] 创建开机/登录启动任务，并执行一次服务器重启演练。
- [ ] 执行一次版本更新和回滚演练。

## 最近一次生产发布

应用尚未部署。2026-09-10 已只读核对试用实例与安全组；尚未登录来宾系统确认页面文件、可用磁盘、Windows 防火墙或端到端网络。首次部署成功后填写：发布 ID、Git 提交、应用版本、部署时间（含时区）、操作者、验证结果和回滚点。
