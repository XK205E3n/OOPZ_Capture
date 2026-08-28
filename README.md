# OOPZ Capture — 飞书控制版

OOPZ Capture 用于录制 OOPZ 语音频道、分片转写并生成会话报告。飞书群是唯一的远程控制和报告投递入口；不再使用 QQ、NapCat 或 OneBot。

## 生产流程

```text
飞书受控群 @OOPZ
  → 选择 OOPZ 域与语音频道 → 录音 → 分片转写 → 选择是否分析
  → OpenCode Go / MiMo-V2.5 生成报告 → 群内审查
  → 批准后创建公开飞书文档，并写入 Base 公开索引
```

- 仅 `OOPZ_FEISHU_ADMIN_CHAT_ID` 指定的群可控制机器人；私聊被禁用，且消息必须 @ 机器人。
- 该群所有成员权限相同。不存在单独的管理员白名单。
- 录音知情同意由实际发起录音的群成员在操作前确认；飞书入口不提供绕过该责任的自动判断。
- 结束录音后先完成转写，再由群内卡片决定“开始分析”或“暂不分析”。
- 分析完成后，内部 Markdown 与候选公开 PDF 会发到群内。只有点击“批准发布”才会创建对外可读的飞书文档和 Base 索引记录。

## 安装与启动

1. 复制 `.env.example` 为 `.env`，至少填写飞书应用、OOPZ 手机号/密码和分析 API Key；不要提交 `.env`。
2. 安装依赖：`pip install -e ".[speech,feishu]"`。
3. 确认 `models/SenseVoiceSmall/model.pt` 存在，并安装 64 位 Chrome 或 Edge 供 OOPZ 浏览器音频和 PDF 渲染使用。
4. 运行 [启动OOPZ全流程.bat](启动OOPZ全流程.bat)。

启动后会打开两个可见窗口：飞书收发记录，以及录音/转写/分析进度。首次启动会在群内发送启动提示与帮助；重启只发送生命周期状态，不重复帮助。关闭和重启分别使用 [一键关闭OOPZ全流程.bat](一键关闭OOPZ全流程.bat)、[一键重启OOPZ全流程.bat](一键重启OOPZ全流程.bat)。

`OOPZ_FEISHU_ADMIN_CHAT_ID` 可以留空。首次启动后将机器人邀请进目标群，程序会自动保存首次邀请对应的群 ID，且以后不会被其他邀请覆盖。若机器人已经在群内、无法再次产生邀请事件，可手动运行：

```powershell
.\.venv\Scripts\oopz-feishu.exe discover-ids
```

然后在目标群 @ 机器人发送“帮助”；终端会打印 `OOPZ_FEISHU_ADMIN_CHAT_ID`。该兼容模式只用于发现 ID，不能执行录音或发送消息。

## 群内指令

所有指令都需要在受控群中 @ 机器人。支持中文自然表达和 `/oopz` 兼容命令。

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

当前项目推荐 OpenCode Go 的 `mimo-v2.5`：现有实测中成本较低、中文语音总结效果较好。推荐值为 `ANALYZER_PROVIDER=opencode-go`、`ANALYZER_BASE_URL=https://opencode.ai/zen/go/v1`、`ANALYZER_MODEL=mimo-v2.5`；这只是建议，程序不会自动填入，价格与模型可用性以供应商最新信息为准。

短窗口与长窗口使用普通 JSON Chat Completions。最终报告在流水线中是“最终综合”阶段。若用户选择 OpenCode Go 的 MiMo 端点，建议显式设置 `ANALYZER_THINKING_MODE=auto`，以标准 OpenAI-compatible 方式请求；只有供应商明确支持扩展字段时才设为 `enabled`。

每个非静音的 300 秒短窗口固定对应一次独立 API 请求，不合并多个窗口的文本。生产 OpenCode Go 路线默认最多并行 4 个独立窗口请求，由 `OOPZ_ANALYSIS_MAX_PARALLELISM=1..8` 调整；输出仍按原始时间顺序汇总。

失败报告会写入对应 Session 的 `analysis_variants/configured-api/lifecycle.json`。再次选择“待分析”会复用已完成的窗口结果，只重试未完成阶段。

如果机器人在分析期间异常退出，下一次启动会检查分析锁的 PID。仅当原进程已不存在时，系统才释放旧锁、将会话标为“中断可恢复”，并让它重新出现在“待分析”和“删除会话”中；“状态”会提示恢复入口。仍在运行的分析任务不会被抢占。尚未完成分析的会话没有最终 PDF 或完整报告，因此需先从“待分析”恢复。

## 云服务器下限

保持当前 Windows 脚本、本地 CPU 转写和 PDF 渲染逻辑不变时，最低可接受配置为：Windows Server 2022/2025 64 位、4 vCPU、8 GiB 内存、80 GiB SSD、稳定 10 Mbps 出站网络、系统管理页面文件；无需 GPU。4 GiB 内存或 2 vCPU 突发型实例不满足当前本地 ASR 和 900 秒处理期限的生产余量。详细依据见运维文档。

更多部署和故障处理见 [docs/OPERATIONS.md](docs/OPERATIONS.md)；架构与数据生命周期见 [docs/CURRENT_ARCHITECTURE.md](docs/CURRENT_ARCHITECTURE.md)。

Windows 云服务器从零部署见 [README_CLOUD_SERVER_DEPLOYMENT.md](README_CLOUD_SERVER_DEPLOYMENT.md)；飞书应用从零配置见 [README_FEISHU_BOT_SETUP.md](README_FEISHU_BOT_SETUP.md)。版本化更新和回滚原理见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)；本地与服务器的当前差异以 [docs/DEPLOYMENT_STATE.md](docs/DEPLOYMENT_STATE.md) 为准，部署相关修改必须登记到 [docs/DEPLOYMENT_CHANGELOG.md](docs/DEPLOYMENT_CHANGELOG.md)。
