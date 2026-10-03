# 项目状态

更新：2026-10-03。本文回答“各模块现在做到哪一步”；服务器的实测细节见 [部署状态](DEPLOYMENT_STATE.md)，架构见 [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)。

## 一句话

录音、转写已在 Ubuntu 服务器上实测通过；分析、报告、飞书发布这几段在 Windows 上生产验证过，但在 Ubuntu 上还没跑过（服务器未配置分析 API）；服务器尚未服务化，只有一个手动启动的 capture-only 测试进程。

## 版本与三端对应

| 端 | 内容 | 状态 |
| --- | --- | --- |
| GitHub `main` | Linux 主线，应用版本 0.11.15 | 与服务器运行代码同源（`4291aa8` 去掉生成文件后），未发布 |
| GitHub `windows-legacy`、标签 `v0.11.*`、Release | Windows 版，最后为 v0.11.15（`3be0c95`） | 冻结；Release 包均为 Windows 版 |
| GitHub `recovery/server-v0.11.15-snapshots` | 服务器 4 个发布包的原样快照 | 只读存档，非原始历史 |
| 服务器 `/opt/oopz` | 4 个 `v0.11.15-*` 发布目录，运行的是 `4291aa894f7a` | 手动 nohup 的 capture-only 测试进程；无 `current` 链接、无 systemd |

注意：四个内容不同的构建共用了 0.11.15，下一次发布必须使用新版本号（建议 0.12.0）。

## 模块状态

| 模块 | 职责 | 状态 | 证据 / 待办 |
| --- | --- | --- | --- |
| 飞书网关与卡片<br>`feishu_gateway/protocol/cli/setup` | 唯一远程入口、命令解析、卡片、一键配置 | Windows 生产验证；Ubuntu 仅在 capture-only 下运行 | 单元测试覆盖；待在 Ubuntu 完整模式下验证 |
| 控制器<br>`controller`、`controller_protocol` | 录音任务状态机、群内指令、分析/发布决策 | 已稳定；capture-only、发起人确认卡、优雅关闭为 10-02/03 新增 | `test_controller`、`test_capture_only`；capture-only 已在服务器运行 |
| 录音<br>`continuous`、`browser_probe`、`recorder`、`session`、`identity` | OOPZ 无头浏览器音频、按 UID 分轨、300 秒分片、断线处理 | **Ubuntu 实测通过** | 服务器会话 159/159 分片成功、失败 0；待长时间稳定性与断线恢复实测 |
| 转写<br>`vad`、`asr`、`transcript`、`speech_cli` | Silero VAD + SenseVoiceSmall（CPU） | **Ubuntu 实测通过** | 同上会话共 7834 段；15 分钟处理期限待在目标 CPU 上复核 |
| 分析<br>`analysis_pipeline`、`analysis_windows`、`analyzer_job`、`deepseek_client` | 短/长窗口、最终综合、检查点复用、内容审核拆分 | Windows 生产验证（DeepSeek flash）；**Ubuntu 未验证** | 服务器 `.env` 无分析 API 配置；待配置后端到端验证 |
| 报告与 PDF<br>`reports`、`pdf_reports`、`weasy_pdf`、`tools/md_to_pdf.*` | 内部 Markdown、候选公开 PDF | Chromium 路径 Windows 验证；Linux WeasyPrint 后端为新增 | 离线与真实中文 PDF 测试通过；待用真实报告在服务器验证 |
| 发布/撤回/删除<br>`feishu_publisher` | 公开飞书文档、Base 索引、远程优先删除 | Windows 生产验证；**Ubuntu 未验证** | 依赖分析链路先打通 |
| 部署工具<br>`scripts/linux/*`、`build_release.ps1` | 发布包构建；安装、更新、回滚、任务锁、事务恢复 | 隔离测试通过；**未在服务器激活** | 待建立 `current`、systemd 单元，做重启与回滚演练 |
| 手动调试入口<br>`main`、`worker_cli`、`continuous_cli`、`analyzer_cli`、`analysis`、`pipeline` | 手动探测、录音、分析 | 生产路径不依赖（`speech_cli` 除外，它由工作流子进程调用） | 可在后续结构整理时评估是否保留 |

## 测试

`python -m pytest`：340 通过、20 跳过（缺 Linux 实机、符号链接权限或 PowerShell 条件）、0 失败（2026-10-03，本机 Windows）。测试覆盖飞书网关、控制器、录音与转写、分析流水线、报告/PDF、Linux 部署脚本；没有覆盖真实 OOPZ/飞书/分析 API，也没有长时间负载。

## 还没做的事（按顺序）

1. 在服务器上服务化：建立 `current` 与 systemd 单元，停掉测试残留进程。
2. 配置分析 API，打通分析 → 报告 → 飞书发布的 Ubuntu 端到端。
3. 重启恢复、版本回滚演练；4 vCPU 下的长时间负载与处理期限复核。
4. 以新版本号构建并发布 Linux 版本；清理服务器上的测试产物与多余发布目录（磁盘已用约 79%）。
