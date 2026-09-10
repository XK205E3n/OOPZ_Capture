# OOPZ Capture

通过飞书群控制 OOPZ 语音录制，按参与者保存独立音轨，以本地 CPU 模型分片转写，再通过可配置的分析 API 生成会话报告。报告经群内审查后，可发布为飞书文档并写入 Base 索引。

当前应用版本 **0.11.9**。远程控制入口为飞书群，部署目标为 **Windows x64**；QQ、NapCat、OneBot 不属于当前运行链路。版本与发布包见 [Releases](https://github.com/XK205E3n/OOPZ_Capture/releases)，变更见 [CHANGELOG.md](CHANGELOG.md)。

## 核心能力

- **独立音轨与分片录制**：基于 OOPZ SDK / Agora 浏览器音频后端，按 UID 采集，默认每 300 秒关闭一个分片。
- **本地语音转写**：Silero VAD 检测语音，SenseVoiceSmall 在 CPU 上识别；默认自动识别语言并保留中文、粤语和英语结果，无需 GPU。
- **可恢复的处理状态**：分片与分析结果写入会话目录；提供转写修复、分析检查点复用和失效进程锁回收。
- **分层报告**：300 秒短窗、60 分钟长窗及最终综合，输出内部 Markdown 与候选公开 PDF。
- **审查后发布**：通过飞书卡片批准、撤回和删除；公开飞书文档与 Base 索引由应用管理。

## 项目结构

| 路径 / 模块 | 职责 |
| --- | --- |
| `src/oopz_capture/feishu_*` | 飞书应用配置、长连接网关、消息与卡片协议、文档发布 |
| `controller.py`、`controller_protocol.py` | 录音任务控制、群内指令、分析与发布决策 |
| `continuous.py`、`browser_probe.py`、`recorder.py` | 浏览器音频采集、分片队列、断线处理与 WAV 写入 |
| `pipeline.py`、`vad.py`、`asr.py`、`transcript.py` | 语音检测、重采样、模型推理、转写输出 |
| `analyzer_job.py`、`analysis_windows.py`、`analysis_pipeline.py` | 分析输入校验、时间窗口、API 调用与检查点 |
| `pdf_reports.py`、`tools/md_to_pdf.mjs` | Node.js 与 Chrome/Edge PDF 渲染 |
| `scripts/` | 启停监视、固定版本模型下载、发布包构建、安装和回滚 |
| `tests/`、`schemas/` | 行为测试与数据契约 |
| `docs/` | 架构、运维、部署状态与发布迁移说明 |
| `output/`、`feishu_state/`、`logs/`、`models/` | 本地会话、网关状态、日志和模型；均不进入 Git / 发布包 |

上表省略路径前缀的 Python 模块均位于 `src/oopz_capture/`。

## 生产流程

```text
飞书受控群 @OOPZ
  → 选择 OOPZ 域与语音频道 → 录音 → 分片转写 → 选择是否分析
  → 配置的分析 API 生成报告 → 群内审查
  → 批准后创建公开飞书文档，并写入 Base 公开索引
```

- 仅 `OOPZ_FEISHU_ADMIN_CHAT_ID` 指定的群可控制机器人；私聊被禁用，且消息必须 @ 机器人。
- 该群所有成员权限相同。不存在单独的管理员白名单。
- 录音知情同意由实际发起录音的群成员在操作前确认；飞书入口不提供绕过该责任的自动判断。
- 结束录音后先完成转写，再由群内卡片决定“开始分析”或“暂不分析”。
- 分析完成后，内部 Markdown 与候选公开 PDF 会发到群内。只有点击“批准发布”才会创建对外可读的飞书文档和 Base 索引记录。

录制下一片时，后台串行处理已关闭的分片；当前每片通过独立 Python 进程运行 VAD / ASR，重新加载模型，各语音段逐个识别。`OOPZ_PROCESSING_DEADLINE_SECONDS` 是失败超时，不是完成速度保证。默认成功转写后删除分片音频；会话和报告默认保留 15 天，详见 [架构与数据生命周期](docs/CURRENT_ARCHITECTURE.md)。

## 安装与启动

推荐使用已验证的 Python 3.12 x64、Node.js LTS，以及 64 位 Chrome 或 Edge。以下是在本地检出目录中的准备步骤；服务器请走下文的 Release 安装流程。

新服务器先执行[从零部署第 2.1 节](README_CLOUD_SERVER_DEPLOYMENT.md#21-自动安装全部基础环境首次部署主流程)的完整 PowerShell：缺少才安装 Visual C++ 运行库、Python 3.12、Node/npm/npx 和浏览器，已安装则跳过。服务器不需要安装 Git 或 GitHub CLI，也不需要预装 winget 或登录 GitHub。

1. 创建虚拟环境：`py -3.12 -m venv .venv`。复制 `.env.example` 为 `.env`，填写 OOPZ 登录配置和下文全部 `ANALYZER_*` 项；飞书 App ID/Secret 由第 3 步自动写入，不需要先去开放平台手动创建应用。不要提交 `.env`。
2. 安装 Python 依赖：`.\.venv\Scripts\python.exe -m pip install -e ".[speech,feishu]"`。安装报告工具依赖：`npx pnpm@10.15.0 install --frozen-lockfile`。PDF 使用固定路径 `tools/node/node.exe`，需将已安装 Node.js 的 `node.exe` 放到该目录。
3. **一键创建/更新飞书机器人（主流程）**：运行 `.\.venv\Scripts\oopz-feishu.exe setup`，用飞书 App 扫码并确认。程序创建或更新应用、申请 11 项应用身份权限、配置长连接事件和卡片回调，自动将 App ID/Secret 写入 `.env`。无法显示二维码时加 `--url-only`；默认更新已有应用，切换应用需明确使用 `--force`。完成后检查是否需要发布应用版本、再邀请进群；公开报告资源授权仍须单独完成。完整步骤见 [一键配置主流程](README_FEISHU_BOT_SETUP.md#首选一键创建或更新机器人)。只有一键流程失败、租户不支持或受管理员策略限制时，才展开手册中的手动保底步骤。
4. 下载并校验固定修订版模型：`.\.venv\Scripts\python.exe scripts/download_sensevoice_model.py --target models/SenseVoiceSmall`。确保已安装 Chrome 或 Edge，供 OOPZ 浏览器音频和 PDF 渲染使用。
5. 运行 [启动OOPZ全流程.bat](启动OOPZ全流程.bat)。

启动后会打开两个可见窗口：飞书收发记录，以及录音/转写/分析进度。首次启动会在群内发送启动提示与帮助；重启只发送生命周期状态，不重复帮助。关闭和重启分别使用 [一键关闭OOPZ全流程.bat](一键关闭OOPZ全流程.bat)、[一键重启OOPZ全流程.bat](一键重启OOPZ全流程.bat)。

`OOPZ_FEISHU_ADMIN_CHAT_ID` 可以留空。首次启动后将机器人邀请进目标群，程序会自动保存首次邀请对应的群 ID，且以后不会被其他邀请覆盖。若机器人已经在群内、无法再次产生邀请事件，可手动运行：

```powershell
.\.venv\Scripts\oopz-feishu.exe discover-ids
```

然后在目标群 @ 机器人发送“帮助”；终端会打印 `OOPZ_FEISHU_ADMIN_CHAT_ID`。该兼容模式只用于发现 ID，不能执行录音或发送消息。

## 群内指令

所有指令都需要在受控群中 @ 机器人。使用固定词表的中文指令或 `/oopz` 兼容命令，全部说法见群内帮助。

| 目的 | 示例 |
| --- | --- |
| 开始录音 | `开始录音`、`开始录音 45分钟` |
| 查看进度 | `状态` |
| 安全结束 | `停止` |
| 恢复未分析会话 | `待分析` |
| 获取候选公开 PDF | `最近报告` |
| 获取内部完整 Markdown | `详细报告` |
| 删除会话 | `删除会话`，再通过确认卡片执行 |
| 查看/修改非敏感运行参数 | `设置状态`、`设置 分片时长=300` |

没有填写时长的录音持续进行，直到成员停止，或触发频道无人、断线保护、北京时间强制结束时间等安全条件。

## 分析 API

项目不预设分析供应商、API 地址、模型或运行参数。所有 `ANALYZER_*` 项都必须由用户根据实际 API 账户显式填写；缺少任意一项时生产网关拒绝启动。完整必填项如下：

```text
ANALYZER_PROVIDER=
ANALYZER_API_KEY=
ANALYZER_BASE_URL=
ANALYZER_MODEL=
ANALYZER_TIMEOUT_SECONDS=
ANALYZER_MAX_RETRIES=
ANALYZER_MIN_INTERVAL_SECONDS=
ANALYZER_MAX_TOKENS=
ANALYZER_THINKING_MAX_TOKENS=
ANALYZER_THINKING_MODE=
ANALYZER_JSON_MODE=
```

模型仅推荐 **MiMo V2.5**，不推荐特定供应商。`ANALYZER_PROVIDER`、`ANALYZER_BASE_URL` 和 `ANALYZER_MODEL` 请按自行选择的服务填写；模型标识以该服务实际支持的名称为准。推荐不构成配置默认值，程序不会自动填入。

短窗口与长窗口使用普通 JSON Chat Completions，最终报告为“最终综合”阶段。思考模式、JSON 模式、Token 上限与超时须按所选 API 的能力显式配置；只有服务明确支持扩展字段时才启用对应选项。

每个非静音的 300 秒短窗口固定对应一次独立 API 请求，不合并多个窗口的文本。窗口默认最多并行 4 路，由 `OOPZ_ANALYSIS_MAX_PARALLELISM=1..8` 调整；实际调用还受客户端限流约束，输出按原始时间顺序汇总。

失败报告会写入对应 Session 的 `analysis_variants/configured-api/lifecycle.json`。再次选择“待分析”会复用已完成的窗口结果，只重试未完成阶段。

如果机器人在分析期间异常退出，下一次启动会检查分析锁的 PID。仅当原进程已不存在时，系统才释放旧锁、将会话标为“中断可恢复”，并让它重新出现在“待分析”和“删除会话”中；“状态”会提示恢复入口。仍在运行的分析任务不会被抢占。尚未完成分析的会话没有最终 PDF 或完整报告，因此需先从“待分析”恢复。

## 云服务器与发布

长期运行的保守起点是 Windows Server 2022/2025 Desktop Experience、4 vCPU / 8 GiB、80 GiB SSD，并启用系统管理页面文件。低密度交流可以从 2 vCPU / 4–8 GiB **试运行**，但本机限额实验不等于云端整机验收；4 GiB 更依赖页面文件，共享型 CPU 还会受资源争抢影响。按实际频道验证每片耗时、内存、磁盘与队列，再决定是否升配。测试边界见 [运维说明](docs/OPERATIONS.md#云服务器容量与试运行)。

本项目通过出站连接访问 OOPZ、飞书与分析 API，**不要求开放业务入站端口**。RDP 管理端口应只允许可信来源。服务器尚未完成应用部署验收；实际状态以 [部署状态基线](docs/DEPLOYMENT_STATE.md) 为准。

服务器使用 [Release ZIP 和 SHA-256 文件](https://github.com/XK205E3n/OOPZ_Capture/releases)，由 `scripts/install_release.ps1` 安装到独立版本目录；配置、模型、输出和状态保存在 `shared` 中。不要把包含 `.env`、模型或会话数据的整个开发目录上传，也不要直接修改服务器版本目录。

当前公开 Release 可匿名获取。从零部署指南提供完整 PowerShell：自动下载校验、提取管理脚本、终端配置、一键飞书配置及正式安装，不需要服务器登录 GitHub 或克隆仓库。业务账号、飞书扫码和租户审批仍由使用者完成。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest
```

提交与发布前按 [AGENTS.md](AGENTS.md) 执行审计、更新变更记录；正式发布包只由 `scripts/build_release.ps1` 从干净的已提交 `HEAD` 构建。

更多部署和故障处理见 [docs/OPERATIONS.md](docs/OPERATIONS.md)；架构与数据生命周期见 [docs/CURRENT_ARCHITECTURE.md](docs/CURRENT_ARCHITECTURE.md)。

Windows 云服务器从零部署见 [README_CLOUD_SERVER_DEPLOYMENT.md](README_CLOUD_SERVER_DEPLOYMENT.md)；飞书应用从零配置见 [README_FEISHU_BOT_SETUP.md](README_FEISHU_BOT_SETUP.md)。版本化更新和回滚原理见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)；本地与服务器的当前差异以 [docs/DEPLOYMENT_STATE.md](docs/DEPLOYMENT_STATE.md) 为准，部署相关修改必须登记到 [docs/DEPLOYMENT_CHANGELOG.md](docs/DEPLOYMENT_CHANGELOG.md)。
