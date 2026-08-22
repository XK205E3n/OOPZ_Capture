# Part 1：录音与转写 Worker

> 历史接口设计记录：当前连续录音与 QQ 生产流程以根目录 README 和 `docs/CURRENT_ARCHITECTURE.md` 为准。

## 客观完成状态

Part 1 的核心技术链路已经完成并经过真实多人语音验收：OOPZ 登录、加入频道、按 Agora UID 分轨录音、OOPZ/Agora/昵称映射、Silero VAD、SenseVoice 转写、时间戳 JSONL 和 UTF-8 Markdown。

`oopz-worker` 补齐了原先缺少的操作闭环：一次请求自动完成录音、离线转写、输出校验、分析器交接和录音删除，并在每次运行前清理到期的受管 Session。

“录音结束后 15 分钟内完成分析”是整个系统的端到端指标。当前 Worker 会把准确的 `analysis_deadline_at` 交给分析器，并在剩余时间内强制结束转写；只有 Part 2 的 DeepSeek 分析器也完成后，才能验收整个 15 分钟指标。

## 本地直接运行

无需激活 PowerShell 虚拟环境，直接调用可执行文件：

```powershell
Set-Location D:\Codex\OOPZ_Capture
.\.venv\Scripts\oopz-worker.exe run `
  --area "01HZVPY9B86ZM5RE6905N7E3WB" `
  --channel "01HZVPY9BM8D7MQB5X55T3132Y" `
  --duration 90 `
  --consent-confirmed
```

默认参数：中文识别、CPU、录音结束后 900 秒的分析截止时间、文本保留 168 小时。只有新 `oopz-worker` 创建且含 `managed_by=oopz-worker-v1` 的 Session 才会自动清理；历史验收样本不会被碰触。

成功后的关键输出：

- `transcript.jsonl`：机器可读、UTF-8、逐段时间戳和说话人 ID。
- `transcript.md`：人类可读、UTF-8、明确标注 Session/OOPZ/Agora ID。
- `handoff/analyzer_request.json`：Part 2 分析器的唯一入口。
- `lifecycle.json`：截止时间、删除时间、状态和失败原因。
- 标准输出 JSONL：QQ 控制器可实时读取的状态事件。

确认转写产物合法后，`audio/` 会被删除。若录音、转写、校验或删除任一步失败，任务状态为 `failed`，录音保留供诊断，且最迟随 Session 的一周保留策略删除。

## 程序 A：QQ 控制器到 Worker

QQ 控制器生成一个 UTF-8 JSON 文件，然后执行：

```powershell
.\.venv\Scripts\oopz-worker.exe run-request "D:\path\request.json"
```

请求示例：

```json
{
  "schema_version": "oopz.worker.request.v1",
  "command": "record_and_transcribe",
  "request_id": "6b467ff3-9efc-4867-a54e-220bb758659e",
  "area_id": "01HZVPY9B86ZM5RE6905N7E3WB",
  "channel_id": "01HZVPY9BM8D7MQB5X55T3132Y",
  "duration_seconds": 1800,
  "consent_confirmed": true,
  "language": "auto",
  "processing_deadline_seconds": 900,
  "retention_hours": 168,
  "requested_by": {
    "source": "qq",
    "chat_type": "group",
    "chat_id": "QQ群号",
    "sender_id": "QQ用户号"
  }
}
```

控制器应保存 `request_id`，再逐行解析 Worker 标准输出中的 `oopz.worker.event.v1`。最终成功事件为 `session.ready_for_analysis`；失败事件为 `session.failed`。所有 Session 均另有唯一 `session_id`，对人输出时必须同时标明名称与 ID，不能只显示昵称。

可在不登录 OOPZ 的情况下先验证请求：

```powershell
.\.venv\Scripts\oopz-worker.exe validate-request "D:\path\request.json"
```

完整规范：

- `schemas/worker_request.schema.json`
- `schemas/worker_event.schema.json`

## 程序 B：Worker 到 DeepSeek 分析器

分析器只消费成功 Session 中的 `handoff/analyzer_request.json`。其中所有输入文件均为相对 Session 根目录的路径，不依赖当前盘符；`encoding` 固定为 `UTF-8`。

分析器必须在 `analysis_deadline_at` 前生成：

- `analysis/result.json`，符合 `oopz.analyzer.result.v1`；
- `analysis/summary.md`，供人阅读；
- `handoff/qq_messages.jsonl`，每行一个符合 `oopz.qq.message.v1` 的待发送消息。

完整规范：

- `schemas/analyzer_request.schema.json`
- `schemas/analyzer_result.schema.json`
- `schemas/qq_message.schema.json`

短期总结按300秒语音时间轴窗口生成；长期摘要按3600秒窗口聚合。具体 API 模型名、限流、失败重试和 QQ 目标属于 Part 2/3，不应硬编码进录音 Worker。

## 一周保留与人工检查

Worker 每次启动都会清理到期的受管 Session。也可以由 Windows 任务计划或未来云端定时器调用：

```powershell
.\.venv\Scripts\oopz-worker.exe cleanup --output-root "D:\Codex\OOPZ_Capture\output" --dry-run
.\.venv\Scripts\oopz-worker.exe cleanup --output-root "D:\Codex\OOPZ_Capture\output"
```

`--dry-run` 只列出 Session ID 和路径。清理器拒绝删除输出根目录、非直接子目录、符号链接或 Windows reparse point，也不会删除没有合法 `lifecycle.json` 管理标记的旧资料。
