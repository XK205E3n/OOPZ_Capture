# 运维说明

## 必要配置

从 `.env.example` 创建本机 `.env`。生产启动前至少需要：

```text
ANALYZER_PROVIDER=opencode-go
ANALYZER_API_KEY=...
ANALYZER_BASE_URL=https://opencode.ai/zen/go/v1
ANALYZER_MODEL=mimo-v2.5
OOPZ_FEISHU_APP_ID=...
OOPZ_FEISHU_APP_SECRET=...
OOPZ_LOGIN_PHONE=...
OOPZ_LOGIN_PASSWORD=...
```

`OOPZ_FEISHU_ADMIN_CHAT_ID` 可预先填写，也可留空后启动：将机器人首次邀请进目标群时，程序会自动写入该群 ID，之后不会被其他邀请覆盖。如果机器人已经在群内且无法重新触发邀请事件，可用 `oopz-feishu discover-ids` 手动发现 ID。

要启用对外发布，还必须同时填写 `OOPZ_FEISHU_PUBLIC_FOLDER_TOKEN`、`OOPZ_FEISHU_BASE_APP_TOKEN`、`OOPZ_FEISHU_BASE_TABLE_ID` 和 `OOPZ_FEISHU_PUBLIC_INDEX_URL`。公开文件夹和 Base 必须向飞书应用授予编辑权限。只填其中一部分会使启动时配置校验失败。

不要在飞书群内设置密码、手机号或 API Key；这些值仅允许在本机 `.env` 中配置。

## 启停

- 首次启动：[启动OOPZ全流程.bat](../启动OOPZ全流程.bat)
- 正常关闭：[一键关闭OOPZ全流程.bat](../一键关闭OOPZ全流程.bat)
- 重启：[一键重启OOPZ全流程.bat](../一键重启OOPZ全流程.bat)

启动程序运行 `oopz_capture.feishu_cli serve`，并显示“飞书消息收发记录”与“录音、转写与分析状态”两个窗口。关闭/重启脚本先尝试发送群通知，随后停止网关和两个监视窗口。

## 分析失败

检查 `output/<Session ID>/analysis_variants/configured-api/lifecycle.json`：其中记录失败阶段、实际供应商与模型。`opencode-go/mimo-v2.5` 的 HTTP 500 表示 OpenCode Go 服务端或其上游请求失败，不表示系统调用了 DeepSeek。

修正配置或重启网关后，在群内发送“待分析”并选择该 Session。流水线会复用成功的短窗口和长窗口，仅重试缺失的阶段。默认 `ANALYZER_THINKING_MODE=auto` 对 OpenCode Go 不发送 `thinking` / `reasoning_effort` 扩展字段；不要仅为“最终总结”强行开启它们，除非当前供应商已明确支持。

## 公开发布故障

批准发布前先确认候选公开 PDF 和内部 Markdown 内容。发布失败通常是飞书应用未获得公开文件夹或 Base 的编辑权限，或 Base 字段与程序所写字段不匹配。删除公开报告还要求应用开通 `space:document:delete` 和 `base:record:delete` 并发布包含这些权限的新版本；错误码 `99991672` 表示所需应用身份权限尚未开通。可在恢复权限后使用：

```powershell
.\.venv\Scripts\oopz-feishu.exe reconcile-publications
.\.venv\Scripts\oopz-feishu.exe repair-publication-index
```

`backfill-publications` 会发布所有当前可用的历史报告，属于批量外部写入操作，只应在明确需要时手动执行。

## 云服务器最低配置

当前发布脚本和运维入口是 Windows PowerShell/批处理，PDF 渲染也显式查找 Windows Chrome/Edge，因此不改代码时应使用 64 位 Windows Server 2022 或更新版本，并安装 Chrome 或 Edge。服务器只需主动访问 OOPZ、飞书和分析 API，不需要开放业务入站端口。

最低可接受规格：

```text
CPU：4 vCPU（持续型实例，不使用突发积分型）
内存：8 GiB，并启用系统管理的页面文件
系统盘：80 GiB SSD，长期保持至少 20 GiB 可用
网络：稳定的 10 Mbps 出站带宽，低丢包；无需 GPU
系统：Windows Server 2022/2025 64 位 Desktop Experience
```

依据当前部署实测：SenseVoiceSmall 模型文件约 0.87 GiB；仅加载 CPU ASR 后工作集约 3.0 GiB、Private Bytes 约 5.0 GiB；项目虚拟环境约 1.39 GiB。网关、两个日志窗口、无头浏览器和 PDF 渲染还需要额外内存，因此 4 GiB 不可接受。2 vCPU 虽可能完成短录音，但云 CPU 性能波动时无法可靠保证默认 900 秒转写期限，也不作为最低生产规格。

若常见录音超过数小时、同时说话人数较多或需要更稳的处理时限，建议使用 8 vCPU、16 GiB 内存和 120 GiB SSD。默认每 300 秒分片且转写后删除音频，磁盘压力通常较小；若把 `OOPZ_RETAIN_AUDIO` 改为 `true`，应按每名说话人约 0.35 GiB/小时的 48 kHz 单声道 PCM 预留额外空间。

当前启动方式是交互式 Windows 批处理，不是 Windows Service。迁移云服务器后，应使用任务计划程序在用户登录或系统启动时调用 `scripts/invoke_full_stack_launcher.ps1`，并用云厂商控制台或仅限管理 IP 的 RDP 维护；不要为了飞书机器人开放公网业务端口。自动启动属于部署配置，当前仓库不会替云主机创建系统级计划任务。
