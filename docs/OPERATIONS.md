# 运维说明

## 必要配置

从 `.env.example` 创建本机 `.env`。分析器不提供默认配置，以下 `ANALYZER_*` 项全部必填：

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

用户必须按实际 API 账户填写精确值。模型仅推荐 MiMo V2.5，不推荐供应商。以下是字段格式示意，空白项及运行参数须按自行选择的服务填写，不是可直接启动的配置：

```text
ANALYZER_PROVIDER=
ANALYZER_BASE_URL=
ANALYZER_MODEL=mimo-v2.5
ANALYZER_TIMEOUT_SECONDS=60
ANALYZER_MAX_RETRIES=3
ANALYZER_MIN_INTERVAL_SECONDS=0.5
ANALYZER_MAX_TOKENS=2048
ANALYZER_THINKING_MAX_TOKENS=16384
ANALYZER_THINKING_MODE=auto
ANALYZER_JSON_MODE=true
```

模型标识需以所选服务支持的名称为准；上述运行数值仅示意格式，程序不会自动采用。API Key 必须使用用户自己的凭据。除此之外，生产启动还需要：

```text
OOPZ_FEISHU_APP_ID=...
OOPZ_FEISHU_APP_SECRET=...
OOPZ_LOGIN_PHONE=...
OOPZ_LOGIN_PASSWORD=...
```

`OOPZ_FEISHU_ADMIN_CHAT_ID` 可预先填写，也可留空后启动：将机器人首次邀请进目标群时，程序会自动写入该群 ID，之后不会被其他邀请覆盖。如果机器人已经在群内且无法重新触发邀请事件，可用 `oopz-feishu discover-ids` 手动发现 ID。

飞书机器人配置的主流程是一键命令 `.\.venv\Scripts\oopz-feishu.exe setup`：扫码确认后自动创建/更新应用、申请 11 项权限并配置事件与卡片回调，App ID/Secret 自动写入当前 `.env`。随后检查版本发布、邀请进群；公开报告资源协作者授权仍须单独完成。只有一键流程不可用时才使用 `README_FEISHU_BOT_SETUP.md` 的折叠手动保底章节。已有完整可用配置无需重新创建应用。

要启用对外发布，还必须同时填写 `OOPZ_FEISHU_PUBLIC_FOLDER_TOKEN`、`OOPZ_FEISHU_BASE_APP_TOKEN`、`OOPZ_FEISHU_BASE_TABLE_ID` 和 `OOPZ_FEISHU_PUBLIC_INDEX_URL`。公开文件夹和 Base 必须向飞书应用授予编辑权限。只填其中一部分会使启动时配置校验失败。

不要在飞书群内设置密码、手机号或 API Key；这些值仅允许在本机 `.env` 中配置。

## 启停

- 首次启动：[启动OOPZ全流程.bat](../启动OOPZ全流程.bat)
- 正常关闭：[一键关闭OOPZ全流程.bat](../一键关闭OOPZ全流程.bat)
- 重启：[一键重启OOPZ全流程.bat](../一键重启OOPZ全流程.bat)

启动程序运行 `oopz_capture.feishu_cli serve`，并显示“飞书消息收发记录”与“录音、转写与分析状态”两个窗口。关闭/重启脚本先尝试发送群通知，随后停止网关和两个监视窗口。

## 分析失败

检查 `output/<Session ID>/analysis_variants/configured-api/lifecycle.json`：其中记录失败阶段、用户配置的实际供应商与模型。HTTP 500 表示所选 API 服务端或其上游请求失败，不能仅根据客户端模块名称推断实际调用的服务。

修正配置或重启网关后，在群内发送“待分析”并选择该 Session。流水线会复用成功的短窗口和长窗口，仅重试缺失的阶段。若机器人在分析中异常退出，下一次启动会仅回收 PID 已不存在的 `.run.lock`，把会话标记为“中断可恢复”；该会话会出现在“待分析”和“删除会话”，而“状态”会显示恢复提示。仍有存活 PID 的锁不会被回收或并发重试。思考与 JSON 模式应按实际 API 能力配置，不应仅为“最终总结”开启服务未支持的扩展字段。

## 公开发布故障

批准发布前先确认候选公开 PDF 和内部 Markdown 内容。发布失败通常是飞书应用未获得公开文件夹或 Base 的编辑权限，或 Base 字段与程序所写字段不匹配。删除公开报告还要求应用开通 `space:document:delete` 和 `base:record:delete` 并发布包含这些权限的新版本；错误码 `99991672` 表示所需应用身份权限尚未开通。可在恢复权限后使用：

```powershell
.\.venv\Scripts\oopz-feishu.exe reconcile-publications
.\.venv\Scripts\oopz-feishu.exe repair-publication-index
```

`backfill-publications` 会发布所有当前可用的历史报告，属于批量外部写入操作，只应在明确需要时手动执行。

## 云服务器容量与试运行

当前发布脚本和运维入口是 Windows PowerShell/批处理，PDF 渲染也显式查找 Windows Chrome/Edge，因此不改代码时应使用 64 位 Windows Server 2022 或更新版本，并安装 Chrome 或 Edge。服务器只需主动访问 OOPZ、飞书和分析 API，不需要开放业务入站端口。

首次安装还需允许出站访问魔搭社区。安装脚本会从官方 `iic/SenseVoiceSmall` 下载项目固定修订版并校验必需文件 SHA-256；模型保存在 `C:\OOPZ\shared\models\SenseVoiceSmall`，不通过 GitHub、本地复制或发布包分发。

长期运行的保守起点（不是所有负载的实测硬下限）：

```text
CPU：4 vCPU（持续型实例，不使用突发积分型）
内存：8 GiB，并启用系统管理的页面文件
系统盘：80 GiB SSD，长期保持至少 20 GiB 可用
网络：按实际流量选择出站带宽，保证 OOPZ、飞书和分析 API 连通；无需 GPU
系统：Windows Server 2022/2025 64 位 Desktop Experience
```

2026-09-09 至 09-10 的本机限额测试使用 Ryzen 9 7950X、Windows、CPU 版 SenseVoiceSmall，以及 10 条各 300 秒的合成音轨。中密度约 612 秒合计 VAD 语音、300 段；高密度约 2,032 秒、1,000 段。限制为 2 个逻辑 CPU 时，中密度约 101–117 秒、高密度约 260–261 秒；4 个逻辑 CPU 时分别约 74 秒与 190–191 秒。逻辑 CPU 包含 SMT 线程，不等同于任意云实例的同数量 vCPU；样本为重复短语音，不代表全部真实对话。

进程峰值工作集约 3.3 GiB、峰值提交量约 5.2 GiB。将工作集硬限到 1.5–2 GiB、保留足够提交额度后，中密度仍完成；将提交额度硬限到 4 GiB 则在加载检查点深拷贝时分配失败。提交量不等于物理 RAM，工作集限额也不是整机低内存模拟：宿主剩余 RAM / 页面缓存会使测试偏有利，浏览器、Windows 与磁盘分页仍需在云机验证。

低密度交流可选择 2 vCPU / 4–8 GiB 试运行，不能仅凭内存标称值判定能否运行，也不能承诺生产稳定性。4 GiB 必须核对系统提交余量和页面文件，40 GiB 系统盘须先核对安装后的可用空间。共享型 CPU 争抢未在本机模拟。900 秒是失败超时，不是性能目标；按需验收每片关闭至转写验证完成的耗时（例如低于 240 秒），并连续录制检查队列不增长、无音频丢块、无持续硬分页。

若常见录音超过数小时、同时说话人数较多或需要更稳的处理时限，建议使用 8 vCPU、16 GiB 内存和 120 GiB SSD。默认每 300 秒分片且转写后删除音频，磁盘压力通常较小；若把 `OOPZ_RETAIN_AUDIO` 改为 `true`，应按每名说话人约 0.35 GiB/小时的 48 kHz 单声道 PCM 预留额外空间。

当前启动方式是交互式 Windows 批处理，不是 Windows Service。迁移云服务器后，应使用任务计划程序在用户登录或系统启动时调用 `scripts/invoke_full_stack_launcher.ps1`，并用云厂商控制台或仅限管理 IP 的 RDP 维护；不要为了飞书机器人开放公网业务端口。自动启动属于部署配置，当前仓库不会替云主机创建系统级计划任务。
