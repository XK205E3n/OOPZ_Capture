# OOPZ Capture

OOPZ Capture 是一个本地优先的语音记录、转写、总结和 QQ 投递程序。当前生产链路为：

1. 管理员私聊 QQ 机器人发送 `/oopz 开始`；
2. 按可读名称选择 OOPZ 域和语音频道；
3. 程序按最长 300 秒分片录音，并在后台用 SenseVoiceSmall `language=auto` 转写；
4. 收到 `/oopz 离开`、频道连续 5 分钟无人、指定时长到达，或北京时间 04:00 时停止；
5. 失败转写分片默认自动重试一轮，成功转写后的音频立即删除；
6. 管理员确认后，使用配置的 OpenAI Chat Completions 兼容 API 完成 300 秒总结、60 分钟摘要和最终总结；
7. 向确认分析的管理员发送摘要文本、公开 PDF 和内部完整 Markdown，再询问是否转发。

当前实际配置使用 OpenCode Go 与 `mimo-v2.5`，但生产入口不再把供应商或模型写死。可通过 QQ 设置 `ANALYZER_PROVIDER`、`ANALYZER_BASE_URL`、`ANALYZER_MODEL` 和 API Key。Qwen/Ollama 路线仅作为历史实验和人工对比工具保留，不参与一键启动或默认分析。

## 一键启动与关闭

在 `D:\Codex\OOPZ_Capture` 中运行：

- `启动OOPZ全流程.bat`：启动控制器、NapCat、OneBot 网关和 QQ 自愈监视器；
- `一键关闭OOPZ全流程.bat`：发送关闭通知后停止上述组件；
- `一键重启OOPZ全流程.bat`：先实际查询 OneBot/QQ 登录状态；NapCat 健康时保留其进程和登录会话，只重启项目控制器、网关与看门狗，异常时才恢复 NapCat。
- `配置NapCat自动登录.bat`：一次性保存由 Windows 当前用户加密的 NapCat 密码摘要，供历史会话失效后的无人值守回退登录；不保存明文密码。

控制器窗口显示录音、分片转写、自动修复和分析进度；NapCat 窗口显示 QQ 登录和投递状态。QQ 自愈监视器在后台运行，只能重启 NapCat/OneBot，不会停止 OOPZ 录音、转写或分析进程。

## QQ 管理员指令

- `/oopz 开始 [秒数]`：选择域和频道后开始；支持 `300`、`5m`、`1h`；
- `/oopz 离开`：安全停止；分析确认权交给实际下达离开指令的管理员；
- `/oopz 状态`：显示录音或最近分析状态；
- `/oopz 报告`：选择最近 7 份报告，发送摘要、PDF 和管理员 Markdown；
- `/oopz 详细报告`：发送摘要和内部完整 Markdown；
- `/oopz 待分析`：选择尚未完成分析的 Session，可分析或确认删除；
- `/oopz 设置 变量=值`：修改白名单内的运行变量；
- `/oopz 设置状态`：显示全部可修改变量，秘密值会打码；

报告选择和转发对话不会调用转写或分析模型，也不会独占工作进程。默认等待3分钟；未收到有效回复时自动取消或跳过并通知管理员。可通过 `/oopz 设置 报告回复超时=180` 调整为30–1800秒。
- `/oopz 增加管理员 QQ号`：增加管理员并向其发送欢迎信息和帮助；
- `/oopz 帮助`：显示指令说明。

每位管理员的报告选择和待分析流程使用独立状态，互不抢占。显式进入“报告”或“待分析”会取消该管理员自己遗留的多步流程。任何时候都可回复“取消”或“跳过”。

## 录音与转写规则

- 顶层 Session ID 使用录音开始时的北京时间，例如 `2026-08-21_19-39-31_BJT`；
- 单个音频分片允许 30–300 秒，默认 300 秒；
- 默认转写模型为 SenseVoiceSmall，语言模式为 `auto`，结果只保留中文（含粤语）和英文；
- 空音频分片生成明确的“该时段无文本”记录，不当作失败；
- 转写失败默认自动重试 1 轮，可用 `OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS=0..3` 调整；
- 仍然失败的分片会在结束通知中注明并保留对应音频；成功分片音频会删除；
- 语音连接异常会在同一 Session 中按退避策略重连，不创建新的顶层目录；
- 自动截止时间固定按北京时间计算，默认 04:00。

低层诊断命令：

```powershell
.\.venv\Scripts\oopz-capture.exe discover
.\.venv\Scripts\oopz-continuous.exe status --session "SESSION_ID"
.\.venv\Scripts\oopz-continuous.exe repair --session "SESSION_ID"
```

## 分析 API

默认命令和 QQ 控制器都读取同一组通用配置：

```text
ANALYZER_PROVIDER=opencode-go
ANALYZER_API_KEY=...
ANALYZER_BASE_URL=https://opencode.ai/zen/go/v1
ANALYZER_MODEL=mimo-v2.5
```

`ANALYZER_PROVIDER` 可为 `deepseek`、`opencode-go` 或 `openai-compatible`。供应商无法从 Base URL 可靠推断，因为代理域名、计价和私有参数并无统一规则，所以必须显式填写；模型列表也不在每个兼容接口上统一开放，因此模型 ID 同样显式配置。

```powershell
$handoff = "D:\Codex\OOPZ_Capture\output\SESSION_ID\handoff\analyzer_request.json"
.\.venv\Scripts\oopz-analyzer.exe validate $handoff
.\.venv\Scripts\oopz-analyzer.exe run $handoff
```

`run` 默认使用 `configured-api`，与 QQ 生产路线一致。兼容性的固定 MiMo 路线仍可显式调用：`--route mimo-go`。路径 2–4 的 Qwen 实验命令仍保留，但不会被一键启动器自动使用。

生产 API 的 300 秒窗口按每 4 个窗口合并成一项请求，独立批次默认最多 4 路并行；结果按原始时间顺序还原。并行数可通过 `OOPZ_ANALYSIS_MAX_PARALLELISM=1..8` 调整。若供应商返回的批量 JSON 缺项、错项或无法解析，程序会自动降级为该批逐窗口重试，避免整份报告因单次批量格式错误而失败。

## 报告与保留

- `summary.text.md`：QQ 摘要文本；
- `summary.public.md`：最终总结、顺序进展、关键信息和 60 分钟摘要，是 PDF 来源；
- `summary.md`：内部完整报告，额外包含 300 秒总结、Token 和费用；
- PDF：`output\Report\YYYY-MM-DD\`。

报告选择始终使用同一 Session 中最近生成的完整分析版本，避免旧的 `qq_messages.jsonl` 覆盖新报告。Session 与其清单登记的归档 PDF 都遵循 `delete_after`，默认保留 168 小时。清理预览：

```powershell
.\.venv\Scripts\oopz-worker.exe cleanup --output-root "D:\Codex\OOPZ_Capture\output" --dry-run
```

## QQ 故障恢复边界

OneBot 网关保存待投递文本和附件，临时超时不会丢弃。自愈监视器依次处理：

1. WebSocket 失联：仅重启 OneBot 网关；
2. QQ 状态离线但端口仍在：重启 NapCat 并复用登录；
3. 已配置加密回退凭据时，NapCat 自动尝试密码登录；
4. 腾讯仍要求新设备或安全验证时，打开交互式登录，等待扫码或验证。

普通 QQ 协议实现无法保证腾讯侧会话永不失效。程序可以检测和恢复服务，但无法绕过账号风控或免除必要的人机验证。建议继续使用专用 QQ 账号。

## 安装与测试

```powershell
Set-Location D:\Codex\OOPZ_Capture
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[speech,qq]"
.\.venv\Scripts\python.exe -m playwright install chromium
pnpm install --frozen-lockfile
.\.venv\Scripts\python.exe -m pytest -q
```

不需要激活 PowerShell 虚拟环境。凭据只存于已被 Git 忽略的 `.env`，不要写入文档、日志或版本库。

当前规范文档见 [CURRENT_ARCHITECTURE.md](docs/CURRENT_ARCHITECTURE.md) 和 [OPERATIONS.md](docs/OPERATIONS.md)。`MILESTONES_*` 与 `PROJECT_PROGRESS.md` 是历史开发记录，不作为当前默认值的依据。
