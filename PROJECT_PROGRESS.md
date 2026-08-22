# 当前项目进度

更新时间：2026-08-21。

## 已完成的生产链路

- QQ 管理员私聊控制，按可读域名和频道名选择录音目标；
- 最长 300 秒录音分片、后台 SenseVoiceSmall `auto` 转写；
- 只保留中文（含粤语）和英文识别结果；
- OOPZ/Agora 断线后在同一 Session 内重连；
- 频道连续无人 300 秒、管理员离开、指定时长或北京时间 04:00 停止；
- 失败转写分片自动修复，成功后删除音频；
- 通用 OpenAI Chat Completions 兼容分析接口，当前配置为 OpenCode Go `mimo-v2.5`；
- 300 秒总结、60 分钟摘要、最终总结和关键信息；
- QQ 摘要、公开 PDF、内部完整 Markdown 和可选转发；
- 168 小时 Session/报告保留；
- NapCat/OneBot 持久化重试与不影响录音进程的 QQ 自愈监视器；
- 多管理员分析责任转移、多步流程隔离和同 Session 分析去重。

## 当前默认值

| 项目 | 默认值 |
|---|---|
| Session 命名 | 北京时间 `YYYY-MM-DD_HH-MM-SS_BJT` |
| 分片 | 300 秒 |
| 转写 | SenseVoiceSmall，`language=auto` |
| 自动修复 | 1 轮 |
| 无人退出 | 300 秒 |
| 强制结束 | 北京时间 04:00 |
| 分析 CLI 路线 | `configured-api` |
| 当前 API | OpenCode Go / `mimo-v2.5` |
| API 并行 | 4 |
| 300 秒批处理 | 每次最多 4 个窗口 |
| 文本/报告保留 | 168 小时 |
| 音频保留 | 成功转写后不保留 |

## 仍保留的实验能力

路径 2–4 的 Qwen/Ollama 代码、benchmark 和 matrix 仍可人工调用，用于历史对比或未来实验。一键启动器和 QQ 默认流程均不启动 Ollama，也不依赖本地模型。

## 下一步验收

以一次新的真实录音执行完整链路，重点核对：自动转写修复、多管理员责任转移、通用 API 分析、报告三件套投递、转发，以及到期清理 PDF。

详细规范见：

- [README.md](README.md)
- [当前架构](docs/CURRENT_ARCHITECTURE.md)
- [运行与故障处理](docs/OPERATIONS.md)
- [2026-08-21 审查记录](docs/PROJECT_AUDIT_2026-08-21.md)
