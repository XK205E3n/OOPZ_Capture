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

飞书机器人配置的主流程是一键命令 `oopz-feishu setup`：扫码确认后自动创建/更新应用、申请 11 项权限并配置事件与卡片回调，App ID/Secret 自动写入当前 `.env`。随后检查版本发布、邀请进群；公开报告资源协作者授权仍须单独完成。只有一键流程不可用时才使用 `README_FEISHU_BOT_SETUP.md` 的折叠手动保底章节。已有完整可用配置无需重新创建应用。

要启用对外发布，还必须同时填写 `OOPZ_FEISHU_PUBLIC_FOLDER_TOKEN`、`OOPZ_FEISHU_BASE_APP_TOKEN`、`OOPZ_FEISHU_BASE_TABLE_ID` 和 `OOPZ_FEISHU_PUBLIC_INDEX_URL`。公开文件夹和 Base 必须向飞书应用授予编辑权限。只填其中一部分会使启动时配置校验失败。

不要在飞书群内设置密码、手机号或 API Key；这些值仅允许在本机 `.env` 中配置。

## 启停

当前服务器以手动 `nohup` 方式运行测试网关，尚未建立 systemd 服务（见 [部署状态](DEPLOYMENT_STATE.md)）。服务化后使用 `systemctl start|stop|restart oopz-capture`；停止走 SIGTERM 优雅关闭：当前录音分片收尾、不中断正在运行的分析、已完成的报告留在发件箱待下次启动。本地开发运行 `oopz-feishu serve`（同一飞书应用只允许一个长连接网关，不要与服务器同时运行）。

启停前先在群内发送“状态”，确认没有进行中的录音或分析；部署脚本也会在切换版本前自动检查并拒绝。

## 分析失败

检查 `output/<Session ID>/analysis_variants/configured-api/lifecycle.json`：其中记录失败阶段、用户配置的实际供应商与模型。HTTP 500 表示所选 API 服务端或其上游请求失败，不能仅根据客户端模块名称推断实际调用的服务。

修正配置或重启网关后，在群内发送“待分析”并选择该 Session。流水线会复用成功的短窗口和长窗口，仅重试缺失的阶段。若机器人在分析中异常退出，下一次启动会仅回收 PID 已不存在的 `.run.lock`，把会话标记为“中断可恢复”；该会话会出现在“待分析”和“删除会话”，而“状态”会显示恢复提示。仍有存活 PID 的锁不会被回收或并发重试。思考与 JSON 模式应按实际 API 能力配置，不应仅为“最终总结”开启服务未支持的扩展字段。

## 公开发布故障

批准发布前先确认候选公开 PDF 和内部 Markdown 内容。发布失败通常是飞书应用未获得公开文件夹或 Base 的编辑权限，或 Base 字段与程序所写字段不匹配。删除公开报告还要求应用开通 `space:document:delete` 和 `base:record:delete` 并发布包含这些权限的新版本；错误码 `99991672` 表示所需应用身份权限尚未开通。可在恢复权限后使用：

```bash
oopz-feishu reconcile-publications
oopz-feishu repair-publication-index
```

`backfill-publications` 会发布所有当前可用的历史报告，属于批量外部写入操作，只应在明确需要时手动执行。

## 服务器容量

- 最低部署要求：4 vCPU / 8 GiB 内存、80 GiB SSD（长期保持至少 20 GiB 可用）、出站访问 OOPZ、飞书、分析 API 与魔搭社区；无需 GPU，无需入站业务端口。当前 Ubuntu 服务器为 4 vCPU / 7.5 GiB / 59 GB 根分区，磁盘低于建议值，需持续监控。
- 录音前须单独验证 Playwright Chromium（`python -m playwright install --no-shell chromium`），系统浏览器不能替代；PDF 使用 Chromium 或显式选择的 WeasyPrint 后端，详见 [部署指南](DEPLOYMENT.md)。
- 模型使用项目固定修订版的 SenseVoiceSmall（CPU），由 `scripts/download_sensevoice_model.py` 下载并校验 SHA-256，保存在共享目录，发布间不重复下载。
- 历史测量（2026-09，Windows、Ryzen 9 7950X）：10 条各 300 秒合成音轨，进程峰值工作集约 3.3 GiB；该数据仅作记录，不构成降低最低要求的依据。长录音、多说话人或需要更稳处理时限时，建议 8 vCPU / 16 GiB / 120 GiB。默认每 300 秒分片并在转写成功后删除音频。
- 15 分钟转写处理期限须在目标服务器的实际 CPU 上复核，合成或模拟计时不算容量证明。

## DeepSeek 官方 Flash 费用参考（2026-09-21 核验）

当前部署仅使用 `deepseek-flash`，对应官方 DeepSeek-V4.1-Flash；不使用 Pro，也没有失败后切换 Pro 的逻辑。Base URL 为 `https://api.deepseek.com`，凭据由独立生产配置保存。

| 人民币／百万 Token | 空闲时段 | 高峰时段 |
| --- | ---: | ---: |
| 缓存命中输入 | 0.02 | 0.04 |
| 缓存未命中输入 | 1 | 2 |
| 输出（含思考） | 4 | 8 |

北京时间周一至周五、非中国法定节假日的 09:00–12:00 和 14:00–18:00 为高峰，其余时段含周末全天为空闲。时段按 API 请求时间判断，不按录音时间；周末调休上班仍按官方“周末全天空闲”口径。内置节假日表按国务院 2026 年放假安排，其他年份报告会提示日历未核验。

费用是当前价格快照下的参考估算，历史请求重算不代表历史实际扣费，最终以平台账单为准。旧服务器程序的费率不会因修改 `.env` 自动更新，需要包含本次价格修正的新发布包。

来源：[DeepSeek 官方模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[2026 年节假日安排（政府转载）](https://www.beijing.gov.cn/fuwu/bmfw/sy/jrts/202511/t20251104_4258838.html)。
