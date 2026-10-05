# 当前架构

## 控制边界

飞书长连接网关是唯一远程控制面。SDK 策略禁用私聊，只允许 `OOPZ_FEISHU_ADMIN_CHAT_ID` 指定群内、且 @ 机器人的消息进入系统。网关将每名群成员映射为仅内存存在的控制器身份；这不是权限分级，受控群的所有成员都可执行同一组操作。

命令解析是固定规则，不调用模型猜测意图。无法可靠识别时，网关要求用户查看帮助或通过卡片选择。

## 处理链路

```text
飞书消息/卡片
  → FeishuGateway → ControllerService → OOPZ 录音与分片
  → 本地转写与修复 → 录音结束后自动分析（Qoder CN CLI）
  → analysis/ 目录（content.json 等）+ digest/digest.png
  → 发件箱（图片消息 + digest.md 文件）→ 飞书群
```

会话文件保存在 `OOPZ_OUTPUT_ROOT`（默认 `output`），网关事件、发件箱和审计日志保存在 `OOPZ_FEISHU_STATE_ROOT`（默认 `feishu_state`）。控制器、转写和分析可在后续命令中从文件状态恢复。

录音使用 OOPZ SDK 的无头浏览器语音后端；每个远端 Agora UID 单独采集 PCM。录音按最多 300 秒分片，分片结束后以本地 Silero VAD 和 SenseVoiceSmall（CPU）转写；默认 `OOPZ_RETAIN_AUDIO=false`，成功转写的分片音频随即删除。

## 出入记录、头像与身份

- **出入记录**：录音期间每次成功的成员刷新（默认 30 秒）都写进 `presence_observations.json`（`oopz.presence.observations.v1`，时钟为录音开始起的毫秒数）：每条快照有谁在频道里；断线、连接不健康、刷新失败写成"间断"记录，间断前后不连起来。它只记录观察到的事实，不推断；发言频率统计只用它（两次连续成功快照之间都在的人才算在场）。录音机器人自己不计入。
- **头像**：从成员刷新拿到的头像地址，经 `digest/avatars.py`（只允许公网 https、限大小和像素、不带凭据）在后台下载到 `<会话>/avatars/`，`index.json` 记录 `oopz_uid → 文件`。下载失败只是用占位头像，不影响录音。出图时只用名单上的人的头像，渲染器会再次校验每个文件。
- **身份映射**：音轨（Agora uid）靠 OOPZ 数据流里的 `uid/cid` 对应到成员；本场出现过的数据流对应关系会累积使用，成员离开频道后他的音轨仍能认出。分析时若恰好剩一条没认出的音轨、且恰好剩一个只有"按 person id 猜"的成员，就按排除法归给他（写入 `analysis/identity.json`）；不唯一时保持"未识别成员"。

## 录音后的自动分析

录音与转写结束（`ready_for_analysis`）后，控制器在后台线程运行 `digest_job.run_digest(会话目录)`，不需要任何确认：

1. `analyzer.transcript.load_session` 读 `session.json`、`lifecycle.json`、`users.json`、`transcript.jsonl`，按说话人合并成连续发言（run）。
2. 按字数和静音切成窗口，每个窗口交给模型生成带证据的笔记；窗口多于 12 个时先分组合并。
3. 汇总：模型从各窗口笔记里挑出整场的"今日之最"、话题、转场、悬念、人物；每一项都带 1–6 个证据 id 和逐字锚点，由 `digest/contract.py` 校验，被拒绝时带着具体错误重试。
4. 编辑改写：另一次调用把汇总改写成"标题 + 一句吐槽"，同时给出各块的角标和时间线短标题；证据字段不得改动，仍走同一校验；实际渲染一次，超高则退回缩写。
5. `digest/render` 离线渲染 `digest.png` 和 `digest.md`（Pillow，字体在 `OOPZ_FONT_DIR`）。

输出写入 `<会话>/analysis/`：`content.json`、`bundle.json`、`meta.json`、`stats.json`、`windows.json`、`coverage.json`、`calls.jsonl`（每次模型调用的审计）、`digest/digest.png`。失败时写 `failure.json`，并向群里发一条文字说明；`重新出图` 重新运行。控制器重启会把"分析中"的会话标成"分析被中断"，同样可用 `重新出图` 重试。

发件箱（`feishu_state/send_requests`）持久保存待发消息；网关每秒检查，发送失败会重试。成功时依次发图片消息 `{"image": {"source": 路径}}` 和 `digest.md` 文件（图片上的文字稿），没有别的文字消息；失败时发一条带原因的文字。

## 录音浏览器依赖

录音后端使用 Playwright 的 Chromium 通道，必须通过该版本 Python 环境执行 `python -m playwright install --no-shell chromium` 并验证启动；已安装系统浏览器不会自动满足此要求。Qoder CLI 需要 Node（`OOPZ_NODE_PATH`），分析进程只调用 CLI，不使用 Node 渲染。

## 保留与删除

会话（转写、出入记录、头像、分析文件、图和 digest.md 全在会话目录里）默认保留 30 天（`OOPZ_RETENTION_HOURS=720`，上限 720），到期由网关每分钟清理一次，整个会话目录连同过期的控制文件一起删除。`删除录音` 在二次确认后只删除本地会话目录。

## QQ 发图（外挂模块）

录音、分析、飞书三块之外的第四个模块，代码在 `qq_bridge/`，与前三者互不依赖：控制器在飞书三条消息排队后调用它一次，把 `digest.png` 经 MaiBot 的本机接口发到一个 QQ 群；`OOPZ_QQ_GROUP_ID` 留空时整个模块关闭。QQ 的任何失败只会在飞书群里提示一条文字，不影响分析结果。细节、重试规则与排查见 [QQ 发图](QQ_BRIDGE.md)。
