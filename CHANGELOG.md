# 项目变更记录

本文件记录 OOPZ Capture 的功能、Bug 修复、配置、依赖、脚本、测试和文档变化，供本地开发、代码审阅与版本发布共同使用。Git 提交仍是精确差异的最终依据。

记录中不得包含密钥、账号、服务器地址、用户数据或其他敏感信息。会影响部署、配置、运行、数据或回滚的修改，还必须同步记录到 `docs/DEPLOYMENT_CHANGELOG.md`。

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
