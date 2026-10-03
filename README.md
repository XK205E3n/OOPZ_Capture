# OOPZ Capture

通过飞书群控制 OOPZ 语音录制，按参与者保存独立音轨，以本地 CPU 模型分片转写，再通过可配置的分析 API 生成会话报告。报告经群内审查后，可发布为飞书文档并写入 Base 索引。

当前主线应用版本 **0.11.15（Linux 主线，尚未发布）**。远程控制入口为飞书群，部署目标为 **Ubuntu 24.04 LTS x86_64**；Windows 版已冻结在分支 `windows-legacy`。QQ、NapCat、OneBot 不属于当前运行链路。各模块的实际进度见 [项目状态](docs/PROJECT_STATUS.md)；已有发布包（≤0.11.15，均为 Windows 版）见 [Releases](https://github.com/XK205E3n/OOPZ_Capture/releases)，变更见 [CHANGELOG.md](CHANGELOG.md)。

## 核心能力

- **独立音轨与分片录制**：基于 OOPZ SDK / Agora 浏览器音频后端，按 UID 采集，默认每 300 秒关闭一个分片。
- **本地语音转写**：Silero VAD 检测语音，SenseVoiceSmall 在 CPU 上识别；默认自动识别语言并保留中文、粤语和英语结果，无需 GPU。
- **可恢复的处理状态**：分片与分析结果写入会话目录；提供转写修复、分析检查点复用和失效进程锁回收。
- **分层报告**：300 秒短窗、60 分钟长窗及最终综合，输出内部 Markdown 与候选公开 PDF。
- **审查后发布**：通过飞书卡片批准、撤回和删除；公开飞书文档与 Base 索引由应用管理。

## 项目结构

| 路径 / 模块 | 职责 |
| --- | --- |
| `feishu_gateway.py`、`feishu_protocol.py`、`feishu_cli.py`、`feishu_setup.py`、`feishu_publisher.py` | 长连接网关、消息与卡片协议、命令行入口（`oopz-feishu`）、一键配置、文档发布 |
| `controller.py`、`controller_protocol.py`、`send_request.py` | 录音任务控制、群内指令、capture-only 模式、分析与发布决策 |
| `continuous.py`、`browser_probe.py`、`recorder.py`、`session.py`、`capture_session.py`、`identity.py` | 浏览器音频采集、身份映射、分片队列、断线处理与 WAV 写入 |
| `vad.py`、`asr.py`、`transcript.py`、`speech_cli.py`、`pipeline.py` | 语音检测、模型推理、转写输出（生产路径经 `speech_cli` 子进程调用） |
| `analyzer_job.py`、`analysis_windows.py`、`analysis_pipeline.py`、`deepseek_client.py` | 分析输入校验、时间窗口、API 调用与检查点 |
| `reports.py`、`pdf_reports.py`、`weasy_pdf.py`、`tools/md_to_pdf.mjs` | 报告生成与 PDF 渲染（默认 Chromium，可显式选 WeasyPrint） |
| `settings.py`、`env_loader.py` | 配置读取与 `.env` 原地写入 |
| `scripts/linux/`、`scripts/build_release.ps1` | Ubuntu 发布管理（安装、更新、回滚、任务锁与事务）与发布包构建 |
| `tests/`、`schemas/` | 行为测试与数据契约 |
| `docs/` | 项目状态、架构、运维、部署与验证记录 |
| `output/`、`feishu_state/`、`logs/`、`models/` | 本地会话、网关状态、日志和模型；均不进入 Git / 发布包 |

上表省略路径前缀的 Python 模块均位于 `src/oopz_capture/`。`main.py`、`worker_cli.py`、`continuous_cli.py`、`analyzer_cli.py`、`analysis.py` 是手动调试入口，生产路径不依赖它们。

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

## 安装与运行

**服务器**：见 [部署指南](docs/DEPLOYMENT.md)（Ubuntu 24.04，发布包分阶段安装，事务式更新与回滚）。服务器尚未完成验收，实际状态以 [部署状态](docs/DEPLOYMENT_STATE.md) 为准。

**本地开发**（任意系统；Python 3.12、Node.js LTS）：

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[speech,feishu]"                 # Linux 可选 PDF 后端：".[pdf]"
npx pnpm@10.15.0 install --frozen-lockfile        # PDF 工具依赖
python scripts/download_sensevoice_model.py --target models/SenseVoiceSmall
python -m playwright install --no-shell chromium  # 录音浏览器
cp .env.example .env                              # 填写 OOPZ 登录与全部 ANALYZER_* 项
oopz-feishu setup                                 # 飞书扫码一键配置，自动写入 App ID/Secret
oopz-feishu serve                                 # 启动网关
```

同一飞书应用只允许一个长连接网关；本地运行前请确认服务器上的网关没有在用同一应用。首次启动会在群内发送启动提示与帮助，重启只发送生命周期状态。

`OOPZ_FEISHU_ADMIN_CHAT_ID` 可以留空。首次启动后将机器人邀请进目标群，程序会自动保存首次邀请对应的群 ID，且以后不会被其他邀请覆盖。若机器人已经在群内、无法再次产生邀请事件，可手动运行：

```bash
oopz-feishu discover-ids
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

最低部署要求为 Ubuntu 24.04 LTS x86_64、4 vCPU / 8 GiB、80 GiB SSD；低于此配置不属于支持的部署配置，最低配置也不保证所有负载满足处理时限，须按实际频道验证每片耗时、内存、磁盘与队列。容量与运维见 [运维说明](docs/OPERATIONS.md#服务器容量)。

本项目只通过出站连接访问 OOPZ、飞书与分析 API，**不要求开放业务入站端口**。

发布包只由 `scripts/build_release.ps1` 从干净的已提交 `HEAD` 构建（ZIP + SHA-256），服务器用 `scripts/linux/` 安装到独立版本目录；配置、模型、输出和状态保存在 `shared/`。不要把包含 `.env`、模型或会话数据的开发目录上传，也不要直接修改服务器版本目录。现有 GitHub Release（≤0.11.15）是 Windows 版；Linux 版尚未发布，下一次发布应使用新的版本号（建议 0.12.0），因为已有多个内容不同的构建共用 0.11.15。

## 开发验证

```bash
python -m pytest
```

提交约定与变更记录规则见 [AGENTS.md](AGENTS.md)。文档索引：[项目状态](docs/PROJECT_STATUS.md)、[架构与数据生命周期](docs/CURRENT_ARCHITECTURE.md)、[运维说明](docs/OPERATIONS.md)、[部署指南](docs/DEPLOYMENT.md)、[部署状态](docs/DEPLOYMENT_STATE.md)、[Ubuntu 验证记录](docs/VALIDATION_UBUNTU.md)、[飞书应用配置](README_FEISHU_BOT_SETUP.md)。
