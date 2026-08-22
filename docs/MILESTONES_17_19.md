# Part 3 — Milestones 17–19

> 历史里程碑记录：本文描述当时阶段，不代表当前默认配置。当前行为以根目录 README 和 `docs/CURRENT_ARCHITECTURE.md` 为准。

## Milestone 17：QQ 指令协议与授权

程序现在接受标准化的 `oopz.qq.inbound.v1` 消息，支持以下精确指令：

```text
/oopz 开始
/oopz 离开
/oopz 状态
/oopz 帮助
```

英文 `start`、`leave`、`status`、`help` 也可使用。聊天中的其他文字不会被当作指令，更不会拼接成 Shell 命令。

安全规则：

- 默认拒绝所有发送者，必须配置 `OOPZ_QQ_ALLOWED_SENDERS`；
- 可用 `OOPZ_QQ_ALLOWED_CHATS` 进一步限定群聊或私聊；
- Area ID 和 Channel ID 只来自本机环境变量，QQ 消息不能修改；
- 远程开始录音前必须显式配置 `OOPZ_RECORDING_CONSENT_CONFIRMED=true`；
- Message ID 用于幂等处理，重复投递同一指令不会启动第二个任务；
- 回复包含明确的 Message ID、Session ID 或 Request ID，不输出凭据。

接口 schema：

```text
schemas/qq_inbound.schema.json
schemas/qq_reply.schema.json
```

## Milestone 18：单实例连续录音监督器

`oopz-qq-controller serve` 是长期运行的控制核心。当前使用目录适配器：外部 QQ 网络适配器把标准消息写入 `controller_state/inbox/`，控制核心把回复写入 `controller_state/replies/`。

控制核心会：

- 保证一个实例中最多只有一个连续录音 Session；
- 把“开始”连接到现有五分钟轮转录音；
- 把“离开”转换为安全的 `control/stop.json`；
- 当时版本在没有离开指令时按本地系统时间 03:00 自动退出；当前版本改为北京时间 04:00，且可由 QQ 设置；
- 录音结束后自动运行 Part 2 分析器；
- 状态保存在 UTF-8 JSON 文件中；
- 使用 PID 锁阻止同一状态目录启动两个控制器。

控制器意外重启时，不会把旧状态误判成仍可控制的录音任务；最近任务会标记为 `controller_restarted`。当前录音任务与控制器运行在同一进程中，因此部署时必须由系统服务管理器自动重启控制器。

## Milestone 19：最终报告 Outbox 与回执

最终分析完成后，`handoff/qq_messages.jsonl` 会幂等进入：

```text
controller_state/outbox/<Message ID>.json
```

状态包括：

- `pending`：等待 QQ 网络适配器发送；
- `blocked`：缺少合法目标；
- `sent`：适配器确认发送成功；
- `failed`：发送失败，可以重试。

只有最终报告进入 Outbox，不会发送300秒总结或60分钟摘要的过程消息。网络适配器发送后必须提交回执：

```powershell
.\.venv\Scripts\oopz-qq-controller.exe ack `
  --message-id "MESSAGE_ID" `
  --status sent
```

查看待处理消息：

```powershell
.\.venv\Scripts\oopz-qq-controller.exe outbox
```

## 本地目录适配器测试

环境变量：

```powershell
# 域与频道由管理员每次通过 /oopz开始 的可读名称列表选择；无需配置默认 ID。
$env:OOPZ_QQ_ALLOWED_SENDERS = "你的QQ号"
$env:OOPZ_QQ_ALLOWED_CHATS = "获准的QQ群号"
$env:OOPZ_RECORDING_CONSENT_CONFIRMED = "true"

$env:DEEPSEEK_API_KEY = "你的API Key"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"
```

第一个 PowerShell 窗口：

```powershell
.\.venv\Scripts\oopz-qq-controller.exe serve
```

第二个 PowerShell 窗口先复制并修改 `examples/qq_inbound.example.json`，确保每次使用新的 UUID，然后提交：

```powershell
.\.venv\Scripts\oopz-qq-controller.exe validate ".\examples\qq_inbound.example.json"
.\.venv\Scripts\oopz-qq-controller.exe submit ".\examples\qq_inbound.example.json"
```

## 当前边界

目录适配器已经把业务核心与实际 QQ 登录方式隔离，但尚未实现 QQ 网络登录、群消息监听和消息发送。选择具体实现前需要单独评估当前可用协议、个人账号风控、云服务器登录稳定性和平台条款；不应把账号密码直接写入本项目。
