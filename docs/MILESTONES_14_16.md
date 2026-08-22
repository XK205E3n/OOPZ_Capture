# Part 2 — Milestones 14–16

> 历史里程碑记录：当前默认值以根目录 README 和 `docs/CURRENT_ARCHITECTURE.md` 为准。

## 完成范围

### Milestone 14：300 秒短总结

分析器按 `analysis/windows.json` 的固定窗口逐一处理：

- 每个非静音窗口调用一次 DeepSeek JSON API；
- 请求明确使用 `thinking.type=disabled`，不传 `reasoning_effort`；
- 保存摘要、话题、决定、行动项、待解决问题和不确定内容；
- 保存 Window ID、源 Segment ID、说话人昵称、OOPZ UID 和 Agora UID；
- 静音窗口不调用 API，但仍生成明确的静音总结；
- 每个窗口先独立原子落盘，再更新 checkpoint，因此中途失败后可以续跑。

输出：

```text
analysis/short/*.json
analysis/short_summaries.jsonl
```

### Milestone 15：60 分钟长期摘要

每个长期窗口由同一时段内最多12个300秒短总结组成：

- 非静音窗口调用 DeepSeek，并明确使用 `thinking.type=disabled`，以降低 token 消耗；
- 对话题、进展、决定、行动项、待解决问题和不确定内容去重整合；
- 全静音窗口不调用 API；
- 每个长期摘要保存其来源 Short Window ID；
- 与短总结一样逐窗口落盘并支持断点续跑。

输出：

```text
analysis/long/*.json
analysis/long_summaries.jsonl
```

### Milestone 16：最终报告与 QQ 交接

分析器使用全部长期摘要生成最终总览。最终总览是唯一启用思考的阶段；DeepSeek V4 当前最低实际强度为 `high`（`low/medium` 会映射为 `high`），初始输出预算限制为4096 tokens，仅在截断时自动提高。最终人类可读报告采用紧凑格式，包括：

- Report ID、Session ID、Request ID；
- 参与用户只显示昵称，不显示 OOPZ UID 或 Agora UID；
- 整体总结，以及合并为紧凑列表的主要话题、明确决定、行动项、待解决问题、重要时间点和不确定内容；
- 每个60分钟长期摘要；
- 每个300秒短总结，包括静音窗口；每段以 `用户：昵称A，昵称B` 标注参与用户。

人类报告中的摘要时段使用基于 `capture_clock_started_at` 换算的北京时间（UTC+8），不显示从录音起点计算的相对时间轴；跨越北京时间日期边界时，起止两端都会显示完整日期。

完整身份映射、Window ID 和未截短的结构化分类仍保存在机器可读 JSON 中。报告格式升级时，程序会使用已有分析结果重新排版并重建 QQ 消息，不会重新调用模型。

`analysis/result.json` 的 `model.usage_by_stage` 分别记录 `short_summaries`、`long_summaries`、`final_overview` 和 `total` 的 API 调用数、输入 tokens、缓存命中/未命中输入 tokens、输出 tokens、推理 tokens 与总 tokens。`model.cost_estimate` 保存各阶段人民币输入费、输出费、合计和峰谷时段明细。最终 Markdown 报告末尾也固定附带相同的 Token 与费用信息。

当前内置 DeepSeek V4 Flash 价格快照核验于 2026-08-13，并按用户要求提前采用官方计划于北京时间 2026-08-17 00:00 生效的新峰谷价格：高峰时段为 09:00–12:00、14:00–18:00，缓存命中输入 ¥0.10/百万 tokens、缓存未命中输入 ¥3/百万 tokens、输出 ¥9/百万 tokens；其余非高峰时段分别为 ¥0.05、¥1.5、¥4.5/百万 tokens。计价依据是每次 API 请求发生的北京时间而不是录音时间；跨时段请求分别计费。旧结果若没有保存请求时间，则使用分析完成时间回退估算并在报告中标注。未分类输入按对应时段的缓存未命中价计算，推理 tokens 已包含在输出 tokens 中，不重复计费。价格可能调整，应以报告中的官方文档链接为准。

300秒总结初始输出上限为1024 tokens，60分钟摘要为2048，最终总览为4096；只有发生截断才逐级扩容。发生可计量的截断重试时，用量会累计而不是只统计最后一次成功响应。

发送给模型的证据会移除 Session/Window/Segment ID、OOPZ UID、Agora UID、模型元数据和分析指纹。300秒阶段把同一用户的连续碎片合并为 `[nickname, 原始转写]`，不删除原始文字；60分钟和最终阶段只传递上游摘要及决定、行动项、未解决问题和不确定内容，避免重复发送话题、进展和身份元数据。完整审计字段只保留在本地机器可读文件中。

最终报告为 UTF-8 Markdown。QQ 交接文件按最多3000字符的正文分片，每片标注 Report ID、Session ID、序号和总片数。当前阶段只生成待发送消息，不登录 QQ，也不实际发送；实际 QQ 连接器属于 Part 3。

输出：

```text
analysis/result.json
analysis/summary.md
handoff/qq_messages.jsonl
```

## 运行命令

先做完全离线的端到端测试，不使用 API Key：

```powershell
.\.venv\Scripts\oopz-analyzer.exe run `
  "D:\Codex\OOPZ_Capture\output\SESSION_ID\handoff\analyzer_request.json" `
  --mock
```

正式运行：

```powershell
$env:DEEPSEEK_API_KEY = "你的 API Key"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"

.\.venv\Scripts\oopz-analyzer.exe run `
  "D:\Codex\OOPZ_Capture\output\SESSION_ID\handoff\analyzer_request.json"
```

输出 JSON 会显示 `Report ID`、人类报告路径和 QQ 消息路径。相同输入和相同模型配置再次运行会直接复用结果。输入、模型、Base URL 或流水线版本变化时，分析指纹改变，程序会重新分析；API Key 永远不进入指纹或输出。

## 失败恢复

若网络或模型调用在中间失败，修复环境后原样重试同一条 `run` 命令即可。程序会复用已经成功且分析指纹一致的窗口，不会重复计费。状态可查看：

```text
analysis/lifecycle.json
analysis/checkpoint.json
```

## 当前边界

- 已生成 QQ 消息契约，但未接入 QQ 协议、登录、命令解析或发送回执；
- 模型输出只能基于 ASR 文本，转写错误会在不确定内容中保留，不能当作事实；
- 单元测试验证模式切换、幂等、断点续跑、静音跳过、UTF-8 和 ID 标注，但不能替代真实长时聊天的人工质量验收。
