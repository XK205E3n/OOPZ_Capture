# 连续录音模式

连续模式优先保持一次 OOPZ 连接，并将本地录音轮转为最长300秒的 Chunk。上一段在后台转写和删除音频时，下一段继续录制；正常轮转不会让机器人退出或重新加入频道。若 Agora 或浏览器语音连接真正断开，程序会在同一个 Session 中自动重连。

## 固定策略

- 录音最长分段：300秒（5分钟），程序拒绝更大的值。
- 短期总结窗口：300秒（5分钟）。
- 长期摘要窗口：3600秒（60分钟）。
- QQ发送模式：只发送离开频道后的最终报告。
- 自动退出：北京时间04:00；不受服务器所在时区影响。
- 转写：每个 Chunk 使用 SenseVoice 自动语言识别，仅保留中文（含粤语）和英文结果，合并为标准转写。
- 音频删除：成功转写后删除；失败分片由 QQ 控制器默认自动修复一轮，仍失败时保留音频并在通知中标明。
- 文本、摘要和最终报告：最长保留168小时。
- 成员刷新：后台每30秒执行；HTTP 522、超时及其他临时失败只告警并退避，不会停止录音。
- 空频道退出：成功刷新确认“除录音机器人外没有其他成员”连续300秒后，保存并转写当前Chunk，退出频道并结束本轮任务；有人重新加入会重置计时。成员刷新失败不会触发该条件。
- 断线宽限：Agora 的 `RECONNECTING`/`DISCONNECTED` 状态持续15秒后才主动重建连接。
- 自动重连：默认在5分钟窗口内尝试，1秒起步指数退避，单次间隔最多30秒。
- 目录策略：短暂断线重连继续使用原 `Session ID` 和原顶层目录，只增加带连续时间偏移的 Chunk。
- 主 Session 命名：使用录制任务开始时的北京时间，例如 `2026-08-13_22-15-30_BJT`；同一秒重复启动时依次增加 `-02`、`-03`。历史 UUID Session 仍可查询、停止、修复和分析。

300秒和60分钟仅为分析窗口。默认情况下每个5分钟Chunk对应一个短总结，每个60分钟摘要对应12个短总结。最终分析器读取整场 `transcript.jsonl`，根据时间戳生成所有300秒短总结和60分钟摘要，再组合为一次最终报告。

## 开始连续录音

无需激活虚拟环境。所有参与者知情后执行：

```powershell
Set-Location D:\Codex\OOPZ_Capture
.\.venv\Scripts\oopz-continuous.exe start `
  --area "01HZVPY9B86ZM5RE6905N7E3WB" `
  --channel "01HZVPY9BM8D7MQB5X55T3132Y" `
  --language auto `
  --consent-confirmed
```

标准运行不要使用 `--retain-audio`。默认会在转写完成后删除音频；只有故障时才保留音频，供 `repair` 修复使用。

程序会输出唯一且人类可读的 `Session ID`、北京时间04:00截止时间以及停止命令。名称中的日期和时间是任务开始时的北京时间。`--chunk-seconds` 可以用于短时间诊断，但必须在30至300秒之间；正式运行默认且推荐300秒。

默认重连参数适合数小时录制，无需额外填写。需要调整时可使用：

```powershell
  --membership-refresh-seconds 30 `
  --empty-channel-timeout-seconds 300 `
  --disconnect-grace-seconds 15 `
  --reconnect-window-seconds 300 `
  --reconnect-initial-delay-seconds 1 `
  --reconnect-max-delay-seconds 30
```

`--empty-channel-timeout-seconds` 默认是300秒；正式运行不建议设置过短，否则朋友暂时离开频道时会提前结束。`--reconnect-window-seconds 300` 表示一次连续断线在5分钟内恢复时仍属于同一场 Session。窗口耗尽后程序才以 `reconnect_window_expired` 结束并交接已经完成的转写。操作系统杀进程、断电或整机重启无法由进程内部重连，云服务器部署时仍需要 systemd/Windows 服务等外部进程守护；这是后续部署层职责。

## QQ离开指令对应接口

当前 QQ Bot 尚未实现，因此可以在第二个 PowerShell 窗口模拟 QQ 的“离开”指令：

```powershell
Set-Location D:\Codex\OOPZ_Capture
.\.venv\Scripts\oopz-continuous.exe stop --session "SESSION_ID"
```

停止命令只写入目标 Session 的原子控制文件。录音进程检测到后会：

1. 保存不足5分钟的最后一个Chunk；
2. 离开OOPZ频道；
3. 等待所有Chunk完成转写；
4. 合并整场带时间戳转写；
5. 生成最终分析器交接文件。

未来 QQ Bot 应调用同一停止函数或生成符合 `oopz.continuous.stop.v1` 的请求，不应直接杀死录音进程。

## 状态检查

```powershell
.\.venv\Scripts\oopz-continuous.exe status --session "SESSION_ID"
```

活跃状态为 `recording`，断线恢复期间为 `reconnecting`，此时仍可提交停止命令。正常退出后的状态为 `ready_for_analysis`；若部分Chunk失败或重连窗口耗尽，则为 `ready_for_analysis_with_errors`，失败Chunk的音频会保留供诊断。

## 输出结构

```text
output/<SESSION_ID>/
  lifecycle.json
  session.json
  users.json
  transcript.jsonl
  transcript.md
  transcript_summary.json
  handoff/analyzer_request.json
  debug/connectivity_events.jsonl
  control/stop.json
  chunks/<INDEX>-<CHUNK_ID>/
    chunk.json
    lifecycle.json
    users.json
    transcript.jsonl
    transcript.md
```

成功Chunk的 `audio/` 会被删除。总转写中的每条记录同时包含 `Session ID`、`Chunk ID`、说话人的 OOPZ UID、Agora UID、全场时间偏移和绝对时间。

`lifecycle.json` 会记录 `connection_attempts`、`successful_connections`、`reconnect_count`、`reconnect_attempts`、`total_disconnected_seconds`、成员刷新成功/失败次数、`empty_channel_timeout_seconds` 和最后一次确认的其他成员数。空频道退出的停止原因为 `empty_channel_timeout`；`debug/connectivity_events.jsonl` 以 UTF-8 记录连接尝试、失败、恢复、退避和空频道计时事件，不记录音频或聊天正文。

分析器交接文件包含：

```json
{
  "delivery_mode": "final_only",
  "summary_windows": {
    "short_summary_seconds": 300,
    "long_summary_seconds": 3600
  }
}
```

接口规范：

- `schemas/continuous_request.schema.json`
- `schemas/continuous_stop.schema.json`
- `schemas/analyzer_request.schema.json`
