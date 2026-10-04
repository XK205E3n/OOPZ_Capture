# 目标流程与设计：录音 → 自动分析 → 图文回顾 → 飞书群

本文是 Linux 主线的目标设计（2026-10-03 起实施）。取代 Windows 版“分析 → 群内审核 → 批准发布到飞书文档/Base”的流程；旧流程只保留在 `windows-legacy`。

## 目标流程

```text
飞书群 @机器人「开始录音」
  → OOPZ 录音 + 分片转写 + 出入频道记录（presence）+ 头像下载
  → 手动「停止」或自动退出（无人/断线/到点）
  → 自动分析（Qoder CN CLI）→ 受校验的回顾内容 JSON
  → 程序统计（发言频率）+ V7 模板渲染 → digest.png + digest.md
  → 自动发送到飞书群：图片消息 + digest.md 文件
```

没有审核、批准、公开文档、Base 索引、撤回环节。后续任务（不在本阶段）：渲染好的图片与目标 QQ 群号交给 MaiBot 插件发送到 QQ 群。

## 数据契约（沿用 V7 交付包，不改）

- 模型只产出 `oopz.digest.content.v2`（`content` + `people`）。每条必须带 1–6 个 `evidence_ids` 与一个原文 `anchor`；程序用 `contract.validate_content` 核对证据 ID、锚点、`speaker_id` 与昵称。模型输出里的 SVG/HTML/URL/路径只当文字。
- 统计（发言频率）由程序用 `stats.compute_frequency_stats(transcript, duration_ms, presence, coverage_complete)` 确定性计算，模型不得给出数量、排名、百分比。
- 运行层提供元信息（日期、时间、页脚、虚构标记）与头像（`speaker_id` → 已校验的本地 PNG）。
- 叙述规则以 `NARRATIVE_RULES.txt` 为准，作为分析器系统提示词的基础。

## 组件与代码位置

| 组件 | 位置 | 来源 / 状态 |
| --- | --- | --- |
| 回顾契约、统计、头像下载、受控图标、Markdown 转义 | `src/oopz_capture/digest/` | V7 包 `contract_kit_upstream`（逐字复用，仅改相对导入） |
| 渲染器（版面、绘制、Markdown） | `src/oopz_capture/digest/render/` | V7 包 `src/digest_render` |
| 字体 | `assets/fonts/`（不入 Git） | `scripts/download_fonts.py`：固定 URL + SHA-256 |
| 出入频道记录与头像采集 | `continuous.py`（已有 30 秒成员刷新） | 新增：每次成功刷新写一条 presence 观察；头像经 `digest.avatars` 下载 |
| 分析器 | `src/oopz_capture/analyzer/` | 新写：证据构造 + Qoder CLI 后端 + 校验/重试；参考此前在服务器上留下的分析实验 |
| 自动分析与投递编排 | `controller.py` + 飞书网关 | 精简：停止后自动分析，去掉审核/发布；网关新增图片消息 |

## Qoder CN CLI 后端（实测事实）

- 路径：`/opt/oopz/shared/tools/qodercn-1.1.65/node_modules/.bin/qoderclicn`，需以服务账户运行并使用其独立 HOME（已登录；免费额度，费用 0）。
- 无头调用：`-p`（print）、`--output-format json`、`--tools ""`（禁用全部工具，等同纯文本补全）、`--no-session-persistence`、`--system-prompt`、`-m`。
- `--thinking enabled` 必须同时给 `--thinking-budget <tokens>`，否则直接报错；此前的带思考调用曾返回空结果，需要在后端做“空结果即失败并重试”。
- 单次调用约 11–65 秒（证据约 12 KB）。输出的 `result` 字段是模型文本，需解析 JSON。

## 分阶段实施

1. **渲染器集成（离线可测）**：移植 V7 渲染器与契约代码，字体脚本，保留有价值的测试，用 V7 的合成样例验证。
2. **分析器**：证据构造（长会话分窗/抽样）、Qoder 后端、提示词（以 `NARRATIVE_RULES` 为基础）、程序校验 + 一次带错误反馈的重试；评审者（二次 LLM 核查）作为可选项，按真实效果再决定默认值。
3. **录音端升级**：presence 观察落盘（契约 `oopz.presence.observations.v1`）、头像下载。
4. **飞书流程精简**：停止/自动退出 → 自动分析 → 发送图片与 md；重写指令文本与卡片；删除审核/发布/撤回/Base。
5. **清理**：删除被取代的 HTTP 分析流水线、PDF 报告、发布器及其测试；全仓精简防御性代码。
6. **（之后）** MaiBot 联动：把图片与 QQ 群号交给 MaiBot 插件。

每个阶段完成后：测试通过、更新 `CHANGELOG.md` 与 `docs/PROJECT_STATUS.md`。部署（服务化）不在这些阶段内，待流程在服务器上端到端跑通后再做。
