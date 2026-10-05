# OOPZ Capture

通过飞书群控制 OOPZ 语音录制，按参与者保存独立音轨，用本地 CPU 模型分片转写；录音结束后自动用 Qoder CN CLI（免费 Qwen 模型）分析整场转写，生成一张"语音精华"长图并发到飞书群。

当前主线应用版本 **0.13.1（Linux 主线）**。远程控制入口为飞书群，部署目标为 **Ubuntu 24.04 LTS x86_64**；Windows 部署线（已在生产验证、原流程保持不变）保留在分支 `windows-legacy`。各模块的实际进度见 [项目状态](docs/PROJECT_STATUS.md)；已有发布包（≤0.11.15，均为 Windows 版）见 [Releases](https://github.com/XK205E3n/OOPZ_Capture/releases)，变更见 [CHANGELOG.md](CHANGELOG.md)。

## 核心能力

- **独立音轨与分片录制**：基于 OOPZ SDK / Agora 浏览器音频后端，按 UID 采集，默认每 300 秒关闭一个分片。
- **本地语音转写**：Silero VAD 检测语音，SenseVoiceSmall 在 CPU 上识别，无需 GPU。
- **自动出图**：录音结束后无需任何确认，自动分析、渲染并把图发到群里；失败时发文字说明，可用"重新出图"重试。
- **可恢复的处理状态**：分片与转写写入会话目录；提供转写修复和分析重试。

## 项目结构

以下 Python 模块位于 `src/oopz_capture/`。

| 路径 / 模块 | 职责 |
| --- | --- |
| `feishu_gateway.py`、`feishu_protocol.py`、`feishu_cli.py`、`feishu_setup.py` | 长连接网关、消息与卡片、命令行入口（`oopz-feishu`）、一键配置 |
| `controller.py`、`controller_protocol.py`、`send_request.py` | 录音任务控制、群内指令、录音后自动分析与发送、发件箱 |
| `continuous.py`、`browser_probe.py`、`recorder.py`、`session.py`、`capture_session.py`、`identity.py` | 浏览器音频采集、身份映射、分片队列、断线处理与 WAV 写入 |
| `vad.py`、`asr.py`、`transcript.py`、`speech_cli.py`、`pipeline.py` | 语音检测、模型推理、转写输出 |
| `analyzer/` | 分析器：转写分段 → 窗口分析 → 汇总 → 编辑改写，调用 Qoder CLI，校验证据（见 [设计](docs/DESIGN_DIGEST_PIPELINE.md)） |
| `digest/` | 回顾内容契约与校验、发言频率统计、头像、离线渲染器（`digest/render/`，Pillow） |
| `qq_bridge/` | **外挂模块**：成品图经 MaiBot 本机接口发到 QQ 群（群号留空即关闭，见 [QQ 发图](docs/QQ_BRIDGE.md)） |
| `digest_job.py`、`sessions.py` | 控制器调用的"一场录音 → 图"入口；查找没出图/已出图的录音 |
| `settings.py`、`env_loader.py` | 配置读取与 `.env` 原地写入 |
| `scripts/linux/`、`scripts/build_release.ps1`、`scripts/download_fonts.py` | Ubuntu 发布管理、发布包构建、出图字体下载 |
| `tests/`、`schemas/` | 行为测试与数据契约 |
| `docs/` | 项目状态、架构、运维、部署与验证记录 |
| `output/`、`feishu_state/`、`logs/`、`models/`、`assets/fonts/` | 本地会话、网关状态、日志、模型、字体；均不进入 Git / 发布包 |

`main.py`、`worker_cli.py`、`continuous_cli.py`、`analysis.py` 是手动调试入口，生产路径不依赖它们（控制器从 `main` 导入 `_config`）。

## 生产流程

```text
飞书受控群 @OOPZ 开始录音
  → 选择 OOPZ 域与语音频道（点选后立即开始录音，没有任何确认步骤）→ 录音 → 分片转写
  → 结束录音（手动）或自动退出（频道无人 / 断线 / 北京时间强制结束时间）
  → 自动分析（Qoder CN CLI）→ 渲染 digest.png
  → 同一个飞书群收到图片，随后收到图片上文字的 digest.md 文件（没有别的文字消息）
```

- 仅 `OOPZ_FEISHU_ADMIN_CHAT_ID` 指定的群可控制机器人；私聊被禁用，且消息必须 @ 机器人。该群所有成员权限相同。
- 没有审核、批准、公开文档、Base 索引或撤回环节。
- 默认成功转写后删除分片音频；会话（转写与图）默认保留 15 天，详见 [架构与数据生命周期](docs/CURRENT_ARCHITECTURE.md)。

## 安装与运行

**服务器**：见 [部署指南](docs/DEPLOYMENT.md)（Ubuntu 24.04，发布包分阶段安装，事务式更新与回滚）。服务器尚未完成验收，实际状态以 [部署状态](docs/DEPLOYMENT_STATE.md) 为准。

**本地开发**（Python 3.12）：

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[speech,feishu]"
python scripts/download_sensevoice_model.py --target models/SenseVoiceSmall
python scripts/download_fonts.py                  # 出图字体（assets/fonts，不入 Git）
python -m playwright install --no-shell chromium  # 录音浏览器
cp .env.example .env                              # 填写 OOPZ 登录与 OOPZ_ANALYZER_* 项
oopz-feishu setup                                 # 飞书扫码一键配置，自动写入 App ID/Secret
oopz-feishu serve                                 # 启动网关
```

同一飞书应用只允许一个长连接网关；本地运行前请确认服务器上的网关没有在用同一应用。

`OOPZ_FEISHU_ADMIN_CHAT_ID` 可以留空。首次启动后将机器人邀请进目标群，程序会自动保存首次邀请对应的群 ID，且以后不会被其他邀请覆盖。若机器人已在群内，可运行 `oopz-feishu discover-ids`，然后在目标群 @ 机器人发送"帮助"，终端会打印该 ID。

## 群内指令

所有指令都需要在受控群中 @ 机器人，全部说法见群内"帮助"。

| 目的 | 示例 |
| --- | --- |
| 开始录音 | `开始录音`、`开始录音 45分钟`（也可只说 `录音`），然后点选频道 |
| 结束录音（之后自动出图） | `结束录音`（`结束`、`停止` 也行） |
| 查看进度 | `状态`（`进度`） |
| 给没出图的录音重新分析 | `重新出图`（`待分析`） |
| 把已出的图再发一次 | `重发图片`（`最近图片`） |
| 删除本地录音和图片 | `删除录音`，再通过确认卡片执行 |
| 查看/修改非敏感运行参数 | `设置`（即 `设置状态`）、`设置 分片时长=300` |
| 帮助 | `帮助` |

没有填写时长的录音持续进行，直到成员结束，或触发频道无人、断线保护、北京时间强制结束时间等安全条件。

## 分析器

分析使用服务器上已登录的 **Qoder CN CLI**（无头、禁用全部工具，当作纯文本补全），默认模型 `Qwen3.8-Flash`。不再使用任何 HTTP API 密钥。必填配置：

```text
OOPZ_ANALYZER_CLI=      # qoderclicn 可执行文件路径
OOPZ_ANALYZER_HOME=     # CLI 的 HOME（保存其登录，运行用户可读写）
```

可选：`OOPZ_ANALYZER_MODEL`、`OOPZ_ANALYZER_TIMEOUT_SECONDS`、`OOPZ_NODE_PATH`（CLI 需要 Node）、`OOPZ_FONT_DIR`。整场录音完整送入模型、不抽样；一场 12 小时的录音约需 10–25 分钟，模型调用约 15–25 次。

手动运行（排查或调试）：

```bash
python -m oopz_capture.analyzer analyze <会话目录> --out <输出目录>   # 分析并写出 content.json 等
python -m oopz_capture.analyzer render <输出目录>                    # 只重新渲染 digest.png
```

## 云服务器与发布

最低部署要求为 Ubuntu 24.04 LTS x86_64、4 vCPU / 8 GiB、80 GiB SSD；容量与运维见 [运维说明](docs/OPERATIONS.md#服务器容量)。本项目只通过出站连接访问 OOPZ 与飞书，**不要求开放业务入站端口**。

发布包只由 `scripts/build_release.ps1` 从干净的已提交 `HEAD` 构建（ZIP + SHA-256），服务器用 `scripts/linux/` 安装到独立版本目录；配置、模型、输出和状态保存在 `shared/`。不要把包含 `.env`、模型或会话数据的开发目录上传，也不要直接修改服务器版本目录。现有 GitHub Release（≤0.11.15）是 Windows 版；Windows 更新脚本读取 GitHub 的 latest Release，所以 Linux 版本必须使用 `linux-vX.Y.Z` 标签并且不设为 Latest（`gh release create --latest=false`）。下一次发布应使用新的版本号（建议 0.12.0）。

## 开发验证

```bash
python -m pytest
```

提交约定与变更记录规则见 [AGENTS.md](AGENTS.md)。文档索引：[项目状态](docs/PROJECT_STATUS.md)、[分析与出图设计](docs/DESIGN_DIGEST_PIPELINE.md)、[架构与数据生命周期](docs/CURRENT_ARCHITECTURE.md)、[运维说明](docs/OPERATIONS.md)、[部署指南](docs/DEPLOYMENT.md)、[部署状态](docs/DEPLOYMENT_STATE.md)、[Ubuntu 验证记录](docs/VALIDATION_UBUNTU.md)、[QQ 发图](docs/QQ_BRIDGE.md)、[飞书应用配置](README_FEISHU_BOT_SETUP.md)。
