# 项目状态

更新：2026-10-04。本文回答"各模块现在做到哪一步"；服务器的实测细节见 [部署状态](DEPLOYMENT_STATE.md)，架构见 [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)，分析与出图的设计见 [DESIGN_DIGEST_PIPELINE.md](DESIGN_DIGEST_PIPELINE.md)。

## 一句话

目标流程"飞书控制 → 录音转写 → 自动分析 → 精华图 → 发到飞书群"已在 `main` 完成，**2026-10-04 以 0.12.0 部署到 Ubuntu 服务器并启用**（systemd，开机自启）。录音与转写、分析与出图均已在服务器实测；**真实飞书群里的第一次完整录音（发图、头像、出入记录、音频删除）还待验证**。

## 版本与三端对应

| 端 | 内容 | 状态 |
| --- | --- | --- |
| GitHub `main`、标签 `linux-v0.12.0` | Linux 主线，应用版本 0.12.0 | 已推送；服务器运行的就是它（提交 `18d560a`） |
| GitHub `windows-legacy`、标签 `v0.11.*`、Release | Windows 部署线，最后为 v0.11.15 | 已在生产验证、流程保持原样；其更新脚本依赖 GitHub 的 latest Release |
| GitHub `recovery/server-v0.11.15-snapshots` | 服务器 4 个发布包的原样快照 | 只读存档 |
| 服务器 `/opt/oopz` | `current` → `v0.12.0-18d560a7f329`（唯一发布目录） | `oopz-capture.service` 运行中、开机自启 |

注意：此前四个内容不同的构建共用了 0.11.15；0.12.0 起每个发布对应唯一内容，下一次修改须使用新版本号。Linux 发布使用 `linux-vX.Y.Z` 标签，GitHub Release（如要发）不得设为 Latest。

## 模块状态

| 模块 | 职责 | 状态 | 证据 / 待办 |
| --- | --- | --- | --- |
| 飞书网关与卡片<br>`feishu_gateway/protocol/cli/setup` | 唯一远程入口、命令解析、卡片、图片发送、一键配置 | 新流程代码完成（指令精简、发图、无审核发布）；**真实飞书未验证** | 单元测试覆盖；待部署后用真实群验证发图 |
| 控制器<br>`controller`、`controller_protocol`、`digest_job`、`sessions` | 录音任务状态机、录音后自动分析与发送、重新出图重试 | 代码完成；分析线程、发件箱、失败文字、重启中断标记有测试 | 待服务器端到端 |
| 录音<br>`continuous`、`browser_probe`、`recorder`、`session`、`identity` | OOPZ 无头浏览器音频、按 UID 分轨、300 秒分片、断线处理 | **Ubuntu 实测通过** | 服务器会话 159/159 分片成功；待长时间稳定性与断线恢复实测；出入记录、头像下载、累积身份映射已在 `continuous.py` 实现（本地测试通过，**未在真实 OOPZ 验证头像下载**）；旧会话里未映射的那条音轨由分析器按排除法归属（rola） |
| 转写<br>`vad`、`asr`、`transcript`、`speech_cli` | Silero VAD + SenseVoiceSmall（CPU） | **Ubuntu 实测通过** | 同上会话共 7834 段；15 分钟处理期限待在目标 CPU 上复核 |
| 分析器<br>`analyzer/` | 转写 → 窗口笔记 → 汇总 → 编辑改写，Qoder CLI，证据校验与重试 | **服务器整场实跑通过**（12.9 小时、7 窗口、约 20 次调用、10–25 分钟） | 仍受语音识别错字影响：不同次运行挑的点不同；见 [设计文档](DESIGN_DIGEST_PIPELINE.md) |
| 渲染<br>`digest/`、`digest/render/` | 契约校验、发言频率统计、离线渲染 PNG/MD | 完成，成品图经用户验收 | 频率统计用录音时写下的出入记录；旧会话没有，图上如实写"缺少足够记录" |
| 部署工具<br>`scripts/linux/*`、`build_release.ps1` | 发布包构建；安装、更新、回滚、任务锁与事务 | **已在服务器完成一次真实部署**（暴露并修了 systemd 单元引号 bug） | 重启恢复与版本回滚演练仍待做 |
| 手动调试入口<br>`main`、`worker_cli`、`continuous_cli`、`analysis` | 手动探测、录音 | 生产路径基本不依赖；例外：控制器从 `main` 导入 `_config` | 可在后续结构整理时评估 |

## 测试

`python -m pytest`：257 通过、12 跳过（缺 Linux 实机、符号链接权限或 PowerShell 条件）、0 失败（2026-10-04，本机 Windows）。覆盖飞书网关、控制器、录音与转写、分析器（含模型调用的假后端）、渲染与契约、Linux 部署脚本；没有覆盖真实 OOPZ/飞书/Qoder，也没有长时间负载。

## 还没做的事（按顺序）

1. **真实群里的第一次完整录音验证**（发图、头像下载、出入记录、音频删除）；部署已完成。
2. **QQ 发图（外挂模块，0.13.0）**：本项目一侧已实现并通过本地测试（含对本机假接口的真实 HTTP 往返），见 [QQ 发图](QQ_BRIDGE.md)；**尚未在服务器上真实发送**：需要用户在 MaiBot 后台启用接口、把目标群移出黑名单，并把口令写进服务器 `.env` 后再测试。
3. 重启恢复、版本回滚演练；4 vCPU 下的长时间负载与处理期限复核；清理服务器上的测试残留与多余发布目录。
