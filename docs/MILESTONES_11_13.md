# Part 2 — Milestones 11–13

> **历史归档，不是当前运行说明。** 本文记录当时仅支持 DeepSeek 的开发阶段，所列 `DEEPSEEK_*` 变量、验收状态和功能边界均已被当前通用分析 API 与飞书生产链路替代。当前默认值、模型行为和运维步骤以根目录 README、`docs/CURRENT_ARCHITECTURE.md`、`docs/OPERATIONS.md` 为准。

## 完成范围

### Milestone 11：分析任务与输入校验

`oopz-analyzer` 能安全读取 `handoff/analyzer_request.json`，并验证：

- Request ID 和 Segment ID 为合法 UUID；主 Session ID 使用录制任务开始时的北京时间命名（例如 `2026-08-13_22-15-30_BJT`），同时兼容历史 UUID；
- 交接文件所在目录与 Session ID 一致；
- 所有输入路径均为 Session 内的相对路径，拒绝 `..`、绝对路径、符号链接和 Windows reparse point；
- JSONL 为UTF-8，每条记录均含时间范围、说话人ID和非空文本；
- 声明的 segment count 与实际行数一致；
- 保留期限不超过168小时；
- 短总结固定300秒，长期摘要固定3600秒。

准备任务时会创建SHA-256输入指纹、生命周期、任务文件和检查点。相同输入重复执行会复用窗口计划，不重复创建新任务。输入改变后会重新规划。

输出：

```text
analysis/
  lifecycle.json
  job.json
  checkpoint.json
  windows.json
  windows.md
```

### Milestone 12：确定性时间窗口

窗口从整场 Session 的 `00:00` 开始固定划分，而不是从第一句话开始：

```text
短窗口：00:00–05:00、05:00–10:00、10:00–15:00……
长窗口：00:00–60:00、60:00–120:00……
```

规则：

- 一个完整5分钟录音Chunk对应一个短窗口；
- 一个完整60分钟摘要包含12个短窗口；
- 静音窗口保留在机器计划中并标注 `silent=true`；
- 最后不足300秒或60分钟的窗口标注 `partial=true`；
- 跨边界语句同时出现在相邻窗口的证据中，并用 `visible_start_ms`、`visible_end_ms` 标明每个窗口可见范围；
- Window ID由Session、窗口类型和起止时间确定，可重复生成且保持不变；
- 每个窗口保留源 Segment ID 和完整说话人身份。

### Milestone 13：DeepSeek API适配器

适配器使用官方OpenAI兼容的 `POST /chat/completions` 契约和JSON Output模式，但不依赖OpenAI SDK。配置全部来自环境变量：

```text
DEEPSEEK_API_KEY              必填
DEEPSEEK_MODEL               必填，必须是账户获准的精确模型ID
DEEPSEEK_BASE_URL            默认 https://api.deepseek.com
DEEPSEEK_TIMEOUT_SECONDS     默认60
DEEPSEEK_MAX_RETRIES         默认3
DEEPSEEK_MIN_INTERVAL_SECONDS 默认0.5
DEEPSEEK_MAX_TOKENS          默认2048
```

实现包括：

- HTTPS和超时校验；
- Bearer认证，但错误信息和输出中不打印API Key；
- `response_format={"type":"json_object"}`；
- 提示词必须明确包含 `JSON`；
- 必填字段和字段类型校验；
- HTTP 429、HTTP 5xx、网络错误、空响应、非法JSON和缺字段时指数退避重试；
- 单进程请求最小间隔限制；
- 响应ID、实际模型、finish reason、token usage和尝试次数元数据；
- 完全离线且确定性的Mock客户端。

真实模型调用尚未验收，因为精确模型ID和API Key尚未配置。代码不会把“DeepSeek V4 Flash”当作模型ID硬编码。

## 命令

验证 Part 1 交接，不写分析任务：

```powershell
.\.venv\Scripts\oopz-analyzer.exe validate `
  "D:\Codex\OOPZ_Capture\output\SESSION_ID\handoff\analyzer_request.json"
```

幂等生成窗口计划：

```powershell
.\.venv\Scripts\oopz-analyzer.exe prepare `
  "D:\Codex\OOPZ_Capture\output\SESSION_ID\handoff\analyzer_request.json"
```

离线检查API适配器，不使用网络或Key：

```powershell
.\.venv\Scripts\oopz-analyzer.exe api-check --mock
```

配置环境变量后执行真实最小调用：

```powershell
$env:DEEPSEEK_API_KEY = "仅放在环境变量中的密钥"
$env:DEEPSEEK_MODEL = "账户提供的精确模型ID"
.\.venv\Scripts\oopz-analyzer.exe api-check
```

真实 `api-check` 会产生一次API调用和可能的费用，因此只应在明确需要验收时执行。

## 当前边界

Milestones 11–13只完成输入、窗口和API基础设施。它们不会生成真正的短总结、长期摘要或最终报告；这些属于Milestones 14–16。
