# 运维说明

## 必要配置

从 `.env.example` 创建本机 `.env`。生产启动需要：

```text
OOPZ_FEISHU_APP_ID=...
OOPZ_FEISHU_APP_SECRET=...
OOPZ_LOGIN_PHONE=...
OOPZ_LOGIN_PASSWORD=...
OOPZ_ANALYZER_CLI=/opt/oopz/shared/tools/qodercn-.../node_modules/.bin/qoderclicn
OOPZ_ANALYZER_HOME=/opt/oopz/shared/tools/qodercn-home
```

`OOPZ_ANALYZER_CLI` 或 `OOPZ_ANALYZER_HOME` 缺失时，生产网关拒绝启动（不要等到第一次录音结束才发现）。`OOPZ_ANALYZER_MODEL`（默认 `Qwen3.8-Flash`）、`OOPZ_ANALYZER_TIMEOUT_SECONDS`、`OOPZ_NODE_PATH`、`OOPZ_FONT_DIR` 为可选。字体用 `python scripts/download_fonts.py` 下载（校验 SHA-256），目录必须在服务可读的共享路径。

`OOPZ_FEISHU_ADMIN_CHAT_ID` 可预先填写，也可留空后启动：将机器人首次邀请进目标群时，程序会自动写入该群 ID，之后不会被其他邀请覆盖。如果机器人已经在群内，可用 `oopz-feishu discover-ids` 手动发现 ID。

飞书机器人配置的主流程是一键命令 `oopz-feishu setup`：扫码确认后自动创建/更新应用并配置事件与卡片回调，App ID/Secret 自动写入当前 `.env`。随后检查版本发布、邀请进群。只有一键流程不可用时才使用 `README_FEISHU_BOT_SETUP.md` 的手动保底章节。机器人需要"发送图片消息"的权限。

不要在飞书群内设置密码、手机号等凭据；这些值仅允许在本机 `.env` 中配置。

## 启停

当前服务器以手动方式运行测试进程，尚未建立 systemd 服务（见 [部署状态](DEPLOYMENT_STATE.md)）。服务化后使用 `systemctl start|stop|restart oopz-capture`；停止走 SIGTERM 优雅关闭：当前录音分片收尾、不中断正在运行的分析线程、发件箱里未发的消息留到下次启动。本地开发运行 `oopz-feishu serve`（同一飞书应用只允许一个长连接网关，不要与服务器同时运行）。

启停前先在群内发送"状态"，确认没有进行中的录音或分析；部署脚本也会在切换版本前自动检查并拒绝。

## 分析失败

分析失败时群里会收到一条文字说明，里面有 Session 和原因。排查文件在 `output/<Session ID>/analysis/`：`failure.json`（最终失败原因）、`calls.jsonl`（每次模型调用的耗时、错误、被拒绝的原文）、`windows.json`（各窗口结果）。常见原因：Qoder CLI 未登录或额度用尽（单次调用报错）、模型多次输出被校验拒绝、转写没有任何发言（无法出图）。

修复后在群内发送"待分析"并选择该 Session 重试；机器人重启时正在分析的会话会显示"分析被中断"，同样可重试。重试会重新分析整场（不复用窗口结果；手动调试可用 `python -m oopz_capture.analyzer analyze … --reuse <旧输出目录>` 只重跑汇总和改写）。

## 服务器容量

- 最低部署要求：4 vCPU / 8 GiB 内存、80 GiB SSD（长期保持至少 20 GiB 可用）、出站访问 OOPZ、飞书与魔搭社区；无需 GPU，无需入站业务端口。当前 Ubuntu 服务器为 4 vCPU / 7.5 GiB / 59 GB 根分区，磁盘低于建议值，需持续监控。
- 录音前须单独验证 Playwright Chromium（`python -m playwright install --no-shell chromium`），系统浏览器不能替代。
- 模型使用项目固定修订版的 SenseVoiceSmall（CPU），由 `scripts/download_sensevoice_model.py` 下载并校验 SHA-256，保存在共享目录，发布间不重复下载。
- 分析器是外部 CLI 调用，不占用本机推理资源；渲染一张长图约几秒、内存几百 MiB。一场 12 小时的录音分析约 10–25 分钟（每次调用 25–280 秒，最多 3 路并行）。
- 历史测量（2026-09，Windows、Ryzen 9 7950X）：10 条各 300 秒合成音轨，进程峰值工作集约 3.3 GiB；该数据仅作记录。长录音、多说话人或需要更稳处理时限时，建议 8 vCPU / 16 GiB / 120 GiB。默认每 300 秒分片并在转写成功后删除音频。
- 15 分钟转写处理期限须在目标服务器的实际 CPU 上复核，合成或模拟计时不算容量证明。
