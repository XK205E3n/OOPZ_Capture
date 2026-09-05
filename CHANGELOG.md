# 项目变更记录

本文件记录 OOPZ Capture 的功能、Bug 修复、配置、依赖、脚本、测试和文档变化，供本地开发、代码审阅与版本发布共同使用。Git 提交仍是精确差异的最终依据。

记录中不得包含密钥、账号、服务器地址、用户数据或其他敏感信息。会影响部署、配置、运行、数据或回滚的修改，还必须同步记录到 `docs/DEPLOYMENT_CHANGELOG.md`。

## 未发布

### 2026-09-04 — 全仓复审修复：网关健壮性、分析链路容错与部署缺口

- 类型：Bug 修复、健壮性加固、部署脚本、文档、测试。
- 修改：
  - 分析链路：`.prepare.lock` 获得与其他分析锁一致的死进程回收（原先死进程残留会永久卡死该会话分析）；prepare 的 checkpoint/job/lifecycle 改为原子写且损坏文件按“无检查点”容错重建（原先截断 JSON 会让分析永久崩溃）；窗口并行汇总在首个失败后取消队列中剩余窗口（不再继续消耗 API 费用）；`analysis_fingerprint` 不再包含窗口并行度（调整并行不再使已完成窗口作废）；变体名拒绝 Windows 保留设备名与结尾点；未配置单价时费用小节保留完整 Token 用量表并使用供应商中性文案；PDF 渲染错误按类型+消息去重；提示词参与者直接使用昵称集合（昵称含“，”不再被拆碎）。
  - 飞书网关：serve 循环对 drain/reconcile/cleanup 逐项异常隔离（损坏的 send_requests JSON 或被占用的会话文件不再杀死整个网关进程并连带取消进行中的录音）；send_requests 列表跳过损坏条目；批准发布卡片补齐锁与事件去重（并发/重投不再产生双份公开文档或孤儿公开文档），历史“先撤回后批准”记录可被 approve 覆盖，未发布时点击撤回改为提示且不再写入阻断记录；消息与卡片回复发送失败时撤销去重标记，允许飞书重投重试；保留清理对本地删除失败记录后跳过；replies/feishu_events/send_requests 增加按保留期的清理。
  - 控制器：重启后不再“收编”无驱动的录音会话，改为写入 `interrupted` 终态并释放录音占用（修复升级/崩溃后“录音已占用”最长卡 15 天的问题）；收编仅在本进程仍有存活录音任务时发生；start_flow 频道选择流程增加 10 分钟过期，发起者弃选不再阻塞全群。
  - 录音与修复链路：`oopz-continuous repair` 兼容硬杀会话——缺失 `stopped_at` 时回退 `interrupted_at`/`started_at`（原先修复在全部转写完成后必然 KeyError）、缺失 `delete_after` 时按最长保留期推算、排队未处理的分片（无 lifecycle.json）不再让修复整体中止；repair 的活动会话保护补上 `reconnecting` 状态；重连退避限制指数上限（小上限+低初始延迟的合法组合下约 17 分钟后溢出崩溃）；合并转写时同步重基准 `start_time`/`end_time`（原先与重基准后的毫秒值相差整数个分片时长）；单个损坏音频块不再被误判为连接丢失触发整轮重连，停机排水阶段也不再因坏块终止会话；VAD 跳过非数字 WAV 文件名（崩溃残留的 `.part.wav` 不再使分片转写失败）。
  - 发布器：群成员显示名解析改用工作线程并按页翻页（超 100 人群不再解析失败，慢响应不再阻塞事件循环）；Base 索引时间戳显式按北京时区（海外服务器时区不再导致日期偏移 8 小时）；文档刷新按页统计旧块。
  - 基础设施：`jsonio.atomic_json` 在 Windows 上遇读者短暂占用目标文件时重试 3 次；分析 API 传输层把 `http.client.HTTPException`（含 IncompleteRead）归入可重试；PDF 渲染 subprocess 增加 180 秒超时并先删除陈旧输出；`settings` 数值校验拒绝 `1_0`/`+1` 写法、`.env` 解析统一为成对引号剥离（与 `env_loader` 一致）；诊断脚本跳过非数字 WAV 文件名；`normalize_intent` docstring 记录 @提及截断的已知限制。
  - 部署脚本与文档：`启动OOPZ全流程.bat` 补入 `invoke_full_stack_launcher.ps1` 所需的 `OOPZ full-stack launcher` 标记串（自启动入口此前必然抛错）；`install_release.ps1` 新增 `shared\tools\node\node.exe` 校验并把发布目录 `tools\node` 联接到共享目录（修复发布包不含 Node 运行时导致服务器首次 PDF 渲染必然失败的缺口）；云部署指南补充 Node 运行时放置步骤、示例 Release ID 更新为 v0.11.7、Python 版本措辞修正；README 指令措辞、飞书配置手册“原子写入”表述、部署状态基线 Python 版本与目录结构同步修正。
- 范围：`src/oopz_capture/` 下 10 个模块、`scripts/install_release.ps1`、`启动OOPZ全流程.bat`、根目录 3 份 README 与 2 份 docs 部署文档；测试新增 5 项。
- 验证：完整测试 `182 passed, 1 skipped`（新增 prepare 死锁回收、损坏分析文件容错、start_flow 过期、atomic_json 重试、仅存活任务时收编）。
- 部署影响：见 `docs/DEPLOYMENT_CHANGELOG.md` 未发布条目（安装脚本新增 tools\node 校验与一次性 Node 运行时放置步骤）。

## 0.11.7 — 2026-09-04

Release：[OOPZ Capture v0.11.7 (7791e58)](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.7-7791e58f0359)

### 2026-09-04 — 测试合成凭据改为运行时生成并清理验证草稿

- 类型：测试卫生、安全扫描合规。
- 修改：`tests/test_deepseek_client.py` 与 `tests/test_final_m11_13_fixes.py` 中 4 处硬编码的合成凭据字面量（如 `secret-key-must-not-leak`、`go-secret`、`vendor-secret`）改为每次测试运行时生成的 `synthetic-key-<uuid>` 假值，相关断言同步引用生成值，测试语义不变；`tmp/rollback/` 未跟踪历史验证草稿迁移至系统临时目录归档，不进入 Git。生产代码两处 SSRF 扫描标记（`deepseek_client.py` 按设计调用用户配置的 https-only 分析端点、`feishu_setup.py` 调用飞书固定官方注册域名）为产品本义，保持不变并在发布审计中记录。
- 范围：上述两个测试文件；测试语义等价。
- 验证：完整测试 `177 passed, 1 skipped`。
- 部署影响：无。

### 2026-09-04 — 修复 `.env` 写入破坏发布目录硬链接

- 类型：Bug 修复、部署正确性。
- 修改：`settings.py` 的 `.env` 文件写入由“临时文件 + `os.replace`”改为原地截断重写并 `fsync` 落盘（`_atomic_write` 改名为 `_write_env_file`）。原实现会在首次写入时切断 `install_release.ps1` 为发布目录 `.env` 建立的指向 `shared\config\.env` 的硬链接，导致服务器上首次入群自动绑定（`bind_admin_chat_id`）、一键配置（`oopz-feishu setup`）和群内“设置”命令的修改只落在当前版本目录；下次升级重建硬链接后这些修改全部丢失，典型表现为控制群绑定失效、网关停留在等待不会再触发的入群事件。本地开发环境无硬链接，因此此前测试未暴露。
- 范围：`src/oopz_capture/settings.py`、`tests/test_settings.py`（新增硬链接保持测试，文件系统不支持硬链接时自动跳过）。
- 验证：新增测试验证经硬链接写入后两个文件名内容一致且链接数保持 2；完整测试 `177 passed, 1 skipped`。
- 部署影响：见 `docs/DEPLOYMENT_CHANGELOG.md` 未发布条目。当前生产服务器尚未部署，无存量脱钩数据需要修复。

### 2026-09-04 — 部署文档补一键配置凭据获取路径并刷新项目进度

- 类型：文档（部署指南、项目进度），纯文档变更。
- 修改：`README_CLOUD_SERVER_DEPLOYMENT.md` 第 7 节补充获取 `OOPZ_FEISHU_APP_ID`/`OOPZ_FEISHU_APP_SECRET` 的三种方式（本地一键配置后人工抄写两行、服务器安装完成后用发布虚拟环境运行 `setup`、按 `README_FEISHU_BOT_SETUP.md` 手动配置），并注明首次安装前服务器无虚拟环境；`docs/DEPLOYMENT_STATE.md` 在硬链接说明中注明 `.env` 写入必须保持原地写并更新时间；`PROJECT_PROGRESS.md` 更新时间并补充一键配置随 0.11.6 发布的事项。
- 范围：上述三个文档；已与 `scripts/install_release.ps1`、`src/oopz_capture/feishu_setup.py`、`feishu_cli.py` 现行为逐项核对。
- 验证：文档描述与代码逐项一致；不涉及代码运行。
- 部署影响：无。

## 0.11.6 — 2026-09-03

### 2026-09-03 — 结构清理：删除历史归档文档与无引用代码

- 类型：文档清理、死代码删除。
- 修改：删除以下在现行文档中零引用、且内容已被 README、`docs/OPERATIONS.md`、`README_FEISHU_BOT_SETUP.md` 与部署文档取代的历史/孤立说明文件：`docs/MILESTONES_7_10.md`、`docs/MILESTONES_11_13.md`（自述为历史归档）、`docs/FEISHU_IMPLEMENTATION_HANDOFF.md`、`docs/FEISHU_MIGRATION_MILESTONES.md`（迁移已完成，交接内容已并入运维与配置文档）、`docs/CONTINUOUS_RECORDING.md`（录音/恢复规则已并入 README 与运维文档）。删除 `workflow.cleanup_expired` 及其专属测试：保留清理职责已由飞书网关的 `cleanup_expired_sessions` 承担（远程优先删除），该函数在生产链路无任何调用入口。
- 范围：上述 5 个文档文件、`src/oopz_capture/workflow.py`、`tests/test_workflow.py`；`examples/worker_request.example.json` 与 `schemas/` 经核对仍与现行 v1 契约一致，保留。
- 验证：全仓（除按规则保留的历史 CHANGELOG 条目与审计基线中的陈旧指纹）无悬空引用；完整测试 `176 passed, 1 skipped`（较此前少 1 项为被删除的死代码专属测试）。
- 部署影响：无；不改变任何运行行为、配置、依赖与数据格式。回滚代码可完整恢复被删文件。

### 2026-09-03 — 新增飞书机器人一键配置（oopz-feishu setup）

- 类型：新功能、配置入口、依赖、测试。
- 修改：新增 `src/oopz_capture/feishu_setup.py` 与 `oopz-feishu setup` 子命令。参考飞书官方 `@larksuiteoapi/node-sdk` 的 `registerApp` 设备注册流（RFC 8628 风格，参考实现为 PlutoKeating/dsh-lark-bot）：终端展示确认二维码（新增 `qrcode` 依赖渲染，缺包时自动退回打印确认链接），用户用飞书 App 扫码确认后，应用创建/更新与本项目所需的 11 项应用身份权限、长连接事件（`im.message.receive_v1`、`p2.im.chat.member.bot.added_v1`）和卡片回调（`card.action.trigger`)在确认页一次性完成；App ID/Secret 自动写入本机 `.env`（Secret 不在终端显示）。默认更新 `.env` 中已有应用，覆盖为另一应用需显式 `--force`；`--create-only` 只允许新建；国际版（Lark）租户自动切换轮询域名；失败时输出含完整权限清单的手动配置指引。`pyproject.toml` 的 `feishu` extra 新增 `qrcode>=8,<9`。
- 范围：`src/oopz_capture/feishu_setup.py`（新增）、`feishu_cli.py`（setup 子命令与 serve 循环提取）、`pyproject.toml`、`README.md`、`README_FEISHU_BOT_SETUP.md`、`docs/OPERATIONS.md`、`tests/test_feishu_setup.py`（新增）。
- 验证：真实注册端点 `action=begin` 请求返回协议约定字段（device_code、expires_in、interval、user_code、verification_uri_complete）；新增 12 项测试覆盖 addons 编码、确认链接构造、轮询/slow_down 降速/国际版域名切换/终止错误/超时、凭据写入保护与手动回退指引；完整测试 `177 passed, 1 skipped`。
- 部署影响：`feishu` extra 依赖集新增 `qrcode`（连带 `colorama`）；详见 `docs/DEPLOYMENT_CHANGELOG.md` 待发布条目。不新增环境变量、无数据迁移。

### 2026-09-03 — 代码结构精简与重复实现收编（不改变行为）

- 类型：代码精简、重复实现收编、可读性。
- 修改：
  - `settings.py`：收编 4 处逐字重复的 `.env` 解析为 `_env_file_values`、2 处逐字重复的键行写入为 `_write_env_line`；删除恒忽略入参的 `_effective_defaults`（`setting_description` 不再读取整个 `.env`）。
  - `analysis_pipeline.py`：删除与 `reports.split_text` 逐字节相同的 `_split_report`，直接复用；将仅内部使用的运行锁 `_acquire_run_lock` 移至 `analyzer_job.py` 与 `_release_lock` 同处管理；两处重复的用量阶段标签提为模块常量 `_USAGE_STAGE_LABELS`。
  - `pdf_reports.py`：报告归档清单改用 `jsonio.atomic_json`，删除等价的内联原子写与多余导入。
  - `feishu_gateway.py`：`handle_card_action` 的发布批准/撤回分支提取为 `_handle_publication_card_action`，控制流不变。
  - `controller.py`：分析生效设置集合提为模块常量 `_ANALYSIS_SETTING_KEYS`。
  - `feishu_cli.py`：serve 主循环提取为模块级 `serve_gateway`，便于独立测试。
- 范围：`src/oopz_capture/` 下 6 个模块；不含数据格式、协议、环境变量与依赖变化。
- 验证：完整测试与重构前基线一致（重构时点为 `165 passed, 1 skipped`）；所有对外行为、文件格式与回复文案保持不变。
- 部署影响：无；升级并重启后无需额外操作。

## 0.11.5 — 2026-08-29

### 2026-08-29 — README 第 2 节补充依赖软件官方下载地址

- 类型：文档（部署指南），纯文档变更。
- 修改：`README_CLOUD_SERVER_DEPLOYMENT.md` 第 2 节「服务器要求」由无链接的软件清单改为带官方下载地址与版本约束的表格：Git for Windows、GitHub CLI、Python 3.12（须 3.12.x，勿用 3.13/3.14）、Node.js LTS（安装脚本经 `npx pnpm@10.15.0 install --frozen-lockfile` 固定 pnpm 版本，无需预装 pnpm）、Chrome/Edge。
- 范围：仅文档；不含代码、配置、依赖、启动方式或数据格式变化。
- 部署影响：无；升级并重启后无需额外操作。

## 0.11.4 — 2026-08-28

### 2026-08-28 — 对齐云服务器部署指南与发布规划

- 类型：文档（部署指南），纯文档变更。
- 修改：`README_CLOUD_SERVER_DEPLOYMENT.md` 三处与 0.11.3 发布规划不一致：第 6 节示例 Release ID 由过时的 `v0.11.1` 改为 `v0.11.3-9a64897bc97d`；第 9 节补充说明安装脚本经 `npx pnpm@10.15.0 install --frozen-lockfile` 安装 Node 依赖（`pnpm-lock.yaml` 随发布包提供）；第 12 节更新流程补齐 release-audit 与 `-SkipTests`，对齐 AGENTS.md 发布规则。
- 范围：仅文档；不含代码、配置、依赖、启动方式或数据格式变化。
- 部署影响：无；升级并重启后无需额外操作。

## 0.11.3 — 2026-08-28

### 2026-08-28 — 分析锁释放容错

- 类型：Bug 修复、健壮性。
- 修改：`prepare_analysis`（`analyzer_job.py`）与 `run_analysis`（`analysis_pipeline.py`）的 `finally` 清理路径直接调用 `lock_path.unlink()`；若释放锁时 `unlink` 抛 `OSError`（如被杀软/编辑器占用），异常会冒泡并让整个分析以失败告终。新增 `_release_lock(lock_path)` 容错辅助（仅记录 warning、不阻断），三处 `finally` 改为调用它；`analyzer_job.py` 补充 `LOGGER`。预获取路径上严格的过期锁清除保持原样（失败应正确冒泡）。
- 范围：`src/oopz_capture/analyzer_job.py`、`src/oopz_capture/analysis_pipeline.py`；不涉及配置、依赖、启动方式与数据格式。
- 验证：lock 相关测试 `tests/test_analyzer_job.py`、`tests/test_analysis_pipeline.py` 共 18 passed；完整测试 `164 passed, 2 failed` —— 2 个失败为前述 Windows 沙箱 `rmdir` 语义差异所致，与本次改动无关。
- 部署影响：不新增配置、不改变依赖与启动方式、无数据迁移。升级并重启后生效。

### 2026-08-28 — 精简代码并消除保护性编程盲区

- 类型：代码精简、死代码清理、重复实现收编、可观测性。
- 修改：
  - 删除全仓零引用的死代码：`src/oopz_capture/recovery_guard.py` 整个模块（144 行，自述为临时方案，无入口、无引用、无测试）、`workflow.new_request`、`send_request.expedite_pending_send_requests`、`audio_io.write_mono_pcm16`、`analysis_pipeline._compact_turns`、`controller.ControllerService.wait_until_idle`，以及随之失效的 9 处无用导入。
  - 新增 `src/oopz_capture/jsonio.py`，收编此前分散在最多 5 个模块中、实现逐字节相同的 `_iso`（9 处）、`_atomic_json`（5 处）与 `_read_json`（4 处），并新增容错读 `read_json_or_none`；各模块以别名引用，调用点行为不变。
  - 合并 `analysis_pipeline` 中函数体逐行同构、仅差输出键后缀的 `_stage_cost` 与 `_opencode_go_stage_cost`，改为同一函数的 `suffix` 参数。
  - 消除保护性编程盲区：4 处 `except Exception: pass` 与进度回调的静默吞异常改为记录 debug/warning 日志，控制流与容错语义保持不变；修复 `_acquire_run_lock` 只捕获 `ValueError/TypeError/JSONDecodeError` 而漏掉 `AttributeError` 与 `OSError` 的缺陷（锁文件为非 dict 内容时会异常冒泡）；`live_config_fields`（19 项）由每次调用重建的局部变量改为模块级常量 `LIVE_CONFIG_FIELDS`；移除恒为空集、导致条件恒假的 `restart_keys` 死分支。
  - PDF 渲染在缺失 `node_modules` 时给出含恢复命令的明确提示，替代原先难以定位的 Node 模块错误。
- 范围：`src/oopz_capture/` 下 13 个模块及新增 `jsonio.py`；不含测试改动，不涉及依赖、环境变量、启动方式与数据格式。
- 验证：编译通过、无未用导入、模块导入全通过；完整测试 `164 passed`。另有 2 个测试（`test_safe_session_file_rejects_symlink_before_resolving`、`test_cleanup_only_deletes_expired_managed_sessions`）在当前 Windows 沙箱下必失败，已通过削减前后对照脚本验证与本次改动无关：该沙箱的 `rmdir()` 对非空目录会成功并连带删除目录内文件，而测试正依赖"非空时 rmdir 失败"这一语义。
- 部署影响：不新增配置、不改变依赖与启动方式、无数据迁移。`recovery_guard` 无入口亦无引用，删除不影响部署；升级并重启后生效。
- 未采用：将 `md-to-pdf` 改为可选依赖。默认 `pnpm install` 仍会安装该依赖，改动无实际体积收益，且当前环境无法验证 `pnpm-lock.yaml` 与 `--frozen-lockfile` 的一致性，故回退以避免部署风险。

### 2026-08-27 — 恢复异常退出后不可见的分析会话

- 类型：Bug 修复、断点恢复、飞书交互、测试。
- 修改：分析锁现在只在所属 PID 仍存活时阻止操作；网关重启会回收已退出进程遗留的安全锁，将生命周期标为“中断可恢复”，并让会话重新出现在“待分析”和“删除会话”中。“状态”会给出恢复指引，恢复过程复用已完成窗口检查点。
- 范围：报告会话发现、控制器启动恢复与状态、飞书待分析卡片、运维/部署文档及测试。
- 验证：覆盖死锁可见与回收、存活锁不被抢占、控制器启动状态恢复；完整测试 `165 passed, 1 skipped`。
- 部署影响：不新增配置或数据迁移。升级并重启网关后生效；现有会话及检查点保持不变。回滚代码不会删除持久会话数据，但旧版本不会自动回收遗留锁。

### 2026-08-23 — 分析 API 配置改为全量显式必填

- 类型：配置契约、启动校验、文档、测试。
- 修改：移除分析供应商、API 地址、模型及运行参数的环境默认值；生产网关启动时校验全部 11 个 `ANALYZER_*` 项，设置状态对缺失项统一显示“未设置”。OpenCode Go + `mimo-v2.5` 仅作为低成本、效果良好的当前推荐方案，不再由程序自动选择；300 秒窗口的默认 4 路并行设置不变。
- 范围：分析客户端、控制器与飞书网关配置、`.env.example`、项目/运维/架构/部署文档及相关测试。
- 验证：缺项、端点/模型无回退、设置状态与生产启动失败均有自动化覆盖；完整测试 `162 passed, 1 skipped`。
- 部署影响：现有本地和服务器 `.env` 必须在更新代码前显式填写全部 `ANALYZER_*` 项；不涉及会话数据迁移。回滚代码可恢复旧默认行为，已显式填写的配置仍可保留。

### 2026-08-23 — 修正飞书卡片中的旧式回复提示

- 类型：Bug 修复、飞书交互、测试。
- 修改：分析确认卡不再显示“回复：是 / 否”，统一引导使用“开始分析 / 暂不分析”按钮；域和频道选择卡移除“回复编号”，新增“取消选择”按钮；补充旧 `/oopz` 设置提示的飞书文案转换。
- 范围：`src/oopz_capture/feishu_gateway.py`、`tests/test_feishu_gateway.py`。
- 验证：覆盖长句分析提示、Outbox 分析卡、录音目标选择及取消按钮；完整测试 `160 passed, 1 skipped`。
- 部署影响：仅改变飞书消息和卡片展示及交互，不改变环境变量、状态格式或数据；更新代码并重启飞书网关后生效。

### 2026-08-22 — 建立统一变更记录规则

- 类型：项目治理、文档。
- 修改：新增根目录 `CHANGELOG.md`，并在 `AGENTS.md` 中强制要求每个修改 Git 跟踪文件的任务在完成前同步记录实际变化。
- 范围：普通功能、Bug 修复、配置、依赖、脚本、测试和文档；部署相关变化仍需同时维护专项部署记录。
- 验证：检查规则覆盖范围、发布归档要求和敏感信息限制；不影响应用运行、配置或服务器部署。

## 0.11.2 — 2026-08-22

### Windows 云服务器模型部署修正

- 类型：Bug 修复、部署安全。
- 修改：服务器改为从 ModelScope 官方社区自动下载固定修订版的 `iic/SenseVoiceSmall`，校验必需文件 SHA-256 后才允许切换版本；发布 ZIP 增加内容与路径校验。
- 部署：首次下载约 0.94 GB，服务器需要能够出站访问 `modelscope.cn`；模型不再由开发机复制或上传到 GitHub。
- 验证：自动化测试 159 项通过、1 项跳过，发布审计通过，部署包 SHA-256 已在 GitHub Release 中登记。
- Release：[OOPZ Capture v0.11.2 (16acd19)](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.2-16acd19a6fde)

### Release 说明可读性调整

- 类型：发布文档。
- 修改：将 v0.11.2 的 GitHub Release 正文改为分区中文说明，明确部署要求、正确下载文件、校验信息及部署指南入口，并提示不要使用 GitHub 自动生成的源码包部署。
- 验证：Release 标签、目标提交、部署 ZIP 和 SHA-256 附件均保持不变；不影响程序与服务器配置。
