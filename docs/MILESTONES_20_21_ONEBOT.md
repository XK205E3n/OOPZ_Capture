# Milestones 20–21：NapCatQQ / OneBot 11 私聊网关

> 历史里程碑与部署参考：当前默认值和状态机以根目录 README 与 `docs/CURRENT_ARCHITECTURE.md` 为准。

## 完成范围

### Milestone 20：OneBot 网络网关

- 通过 OneBot 11 正向 WebSocket 连接本机 NapCatQQ；
- 使用 Bearer Token 鉴权，Token 不写入日志或状态文件；
- 仅接收指定管理员 QQ 的私聊指令；
- 群消息在读取 `message` / `raw_message` 前丢弃，其他账号私聊同样静默丢弃；
- 把管理员私聊规范化为现有 `oopz.qq.inbound.v1`，并把控制器结果通过 `send_private_msg` 回复管理员；
- 使用持久化投递标记和进程内并发保护避免重复回复；
- 保存不含消息正文和完整 QQ 号的健康状态；
- 提供 `diagnostic-echo` 隔离模式，明确禁止调用 OOPZ、录音、转写、DeepSeek。

### Milestone 21：安全配置和部署准备

- 仅允许 `127.0.0.1`、`localhost` 或 `::1`，除非显式开启远程连接；
- Token 至少 24 个字符；
- 管理员 QQ 和报告群号必须为 5–20 位 ASCII 数字；
- 提供配置校验、NapCat 登录/群列表诊断、健康状态命令；
- 提供本机 WebSocket 模拟器测试，验证鉴权、私聊反馈、群消息静默丢弃；
- 正式 NapCat 登录及真实 QQ 收发留到联合测试，不在正在执行的四小时录音期间安装或重载运行环境。

## NapCat WebUI 设置

只从 NapCatQQ 官方 GitHub Releases 下载 Windows 一键包。启动后按控制台显示的随机 WebUI Token 登录。使用专门的普通 QQ 账号扫码登录。

在 WebUI 的“网络配置”中新建并启用“WebSocket 服务端/正向 WebSocket”：

| 设置 | 值 |
|---|---|
| Host | `127.0.0.1` |
| Port | `3001` |
| Token | 与 `OOPZ_ONEBOT_ACCESS_TOKEN` 完全一致 |
| 消息格式 | `array` |
| 上报自身消息 | 关闭 |
| Debug | 关闭 |

不要启用 HTTP 服务端、HTTP 上报、WebSocket 客户端/反向 WebSocket，也不要绑定 `0.0.0.0` 或局域网地址。WebUI 自身也只允许本机访问。将机器人普通 QQ 号加入未来接收最终报告的群；当前程序不会读取或响应群聊。

## PowerShell 环境变量

先生成一个 32 字节随机 Token：

```powershell
[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

把结果同时填入 NapCat 和当前 PowerShell。不要把真实值写进 Git 文件：

```powershell
$env:OOPZ_ONEBOT_WS_URL = "ws://127.0.0.1:3001"
$env:OOPZ_ONEBOT_ACCESS_TOKEN = "刚才生成的随机Token"
$env:OOPZ_QQ_ADMIN_ID = "管理员QQ号"
$env:OOPZ_QQ_REPORT_GROUP_ID = "最终报告输出群号（当前仅用于诊断校验）"
$env:OOPZ_QQ_REPORT_FRIEND_ID = "最终报告私聊输出好友QQ号"
$env:OOPZ_QQ_REPORT_GROUP_ENABLED = "false"
$env:OOPZ_QQ_STATE_ROOT = "D:\Codex\OOPZ_Capture\controller_state"
```

## 不干扰 OOPZ 的联合测试流程

四小时录音结束前，不执行 `pip install -e`，也不运行完整控制器。录音结束后刷新命令入口：

```powershell
Set-Location D:\Codex\OOPZ_Capture
.\.venv\Scripts\python.exe -m pip install -e ".[qq]"
```

随后依次执行：

```powershell
.\.venv\Scripts\oopz-onebot.exe validate-config
.\.venv\Scripts\oopz-onebot.exe diagnose
.\.venv\Scripts\oopz-onebot.exe serve --diagnostic-echo
```

## 通过 QQ 设置运行时变量（管理员私聊）

管理员私聊可执行：
- /oopz设置 OOPZ_LOGIN_PHONE=手机号
- /oopz设置 OOPZ_LOGIN_PASSWORD=密码（回显打码）
- 域与语音频道不保存为默认 ID；每次 `/oopz开始` 都会列出可读名称供管理员依次选择。
- /oopz设置状态 查看已配置项（打码）

值会立即写入 os.environ（下次录音/分析生效）并保存到 gitignored 的 .env。

## 最终报告投递（v1，群输出默认关闭）

最终报告目前通过 outbox 由 oopz-onebot serve 自动投递：私聊发送给管理员（OOPZ_QQ_ADMIN_ID）和输出好友（OOPZ_QQ_REPORT_FRIEND_ID）。群输出默认关闭（OOPZ_QQ_REPORT_GROUP_ENABLED=false）；如需恢复发群，将该项设为 	rue，报告会额外发到 OOPZ_QQ_REPORT_GROUP_ID。

验收项：

1. 管理员私聊 `/oopz状态`，收到“QQ 网关隔离测试正常”；
2. 非管理员私聊任意内容，无回复；
3. 任意群聊发送 `/oopz状态`，无回复；
4. `oopz-onebot state` 显示群事件和未授权私聊仅增加丢弃计数；
5. 录音 Session、OOPZ 登录、DeepSeek Token 使用均无变化。

`diagnose` 只调用 OneBot 的 `get_login_info` 和 `get_group_list`，用于确认 NapCat 已登录且机器人位于报告群；它不会调用 OOPZ 或 DeepSeek。

## 当前边界

- Milestone 22 才会把完整的“开始、离开、状态、帮助”控制器反馈链路作为正式服务联调；
- 最终报告已实现 outbox 自动投递：默认私聊发送给管理员和输出好友，群输出默认关闭（`OOPZ_QQ_REPORT_GROUP_ENABLED=false`）；
- NapCatQQ 属于普通 QQ 协议端实现，不等同于腾讯官方机器人平台。账号风控和协议变化无法由本程序消除，因此应使用专用账号，避免承载个人重要资料。

## 管理员指令集（当前）

- `/oopz 开始 [秒数]`：开始录音；可带时长（秒，或 5m/1h），到时自动停止并分析
- `/oopz 离开`：提前结束录音
- `/oopz 状态`：查看任务状态
- `/oopz 报告`：列出最近 7 份最终报告 → 回复编号选择 → 报告私聊发给管理员 → 可选继续转发到群/好友（回复 群聊/好友，再输入群号或 QQ 号；跳过 取消）
- `/oopz 设置 变量=值`：设置运行变量（OOPZ 登录、area/channel 等白名单）
- `/oopz 设置状态`：查看已配置变量（打码）
- `/oopz 增加管理员 QQ号`：添加管理员（即时生效，写入 admins.json 与 .env）
- `/oopz 帮助`：完整指令说明

## 其他行为

- 好友申请：任何人添加机器人为好友，自动立即同意（网关收到 request 事件后自动 approve）
- 管理员名单：运行时以 `controller_state/admins.json` 为准，网关每次事件读取（变更后即时生效）；启动时以 `OOPZ_QQ_ADMIN_ID` / `OOPZ_QQ_ADMIN_IDS` 和该文件为准
- 多管理员：回复始终发回给实际发送者

## 分析流程（v2）

- 当时默认分析路线：路径 5 —— 300 秒短总结、60 分钟长摘要和最终总结全部通过 OpenCode Go 调用 MiMo-V2.5（`mimo-v2.5`，非 Pro）；当前生产入口已改为供应商无关的 `configured-api`。
- 收到 `/oopz 离开` 后：向实际下达离开指令的管理员询问是否分析；确认后使用当前配置的通用分析 API。
- 程序自动结束（时长到 / 频道空置 5 分钟 / 北京时间04:00兜底）：向发出开始指令的管理员询问是否分析。
- `/oopz待分析`：列出未完成分析的录制会话 → 回复编号 → 回复 分析 / 删除；删除需再次确认。
- 默认生产路线不需要 Ollama；本地 Qwen 仅为显式实验路线。
- 报告输出形态：摘要文本 + 公开 PDF + 管理员完整 Markdown；转发对象收到摘要文本和 PDF。
- 报告分层：对外（文本+PDF）只含 标题/参与用户 + 最终总结 + 每60分钟长期摘要（`summary.public.md`）；内部完整 .md（`summary.md`，含 300 秒短总结与 Token 明细）保留不转 PDF，通过 `/oopz报告全文`（管理员）选择输出
- 分析路线：保留路径2（Qwen短/DS长/DS最终）、路径3（Qwen短长/DS最终）、路径4（全Qwen思考）作为实验路线；当时默认执行路线为路径5（全程 OpenCode Go MiMo-V2.5）。
