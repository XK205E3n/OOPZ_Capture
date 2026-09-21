# 项目变更记录

本文件记录 OOPZ Capture 的功能、Bug 修复、配置、依赖、脚本、测试和文档变化，供本地开发、代码审阅与版本发布共同使用。Git 提交仍是精确差异的最终依据。

记录中不得包含密钥、账号、服务器地址、用户数据或其他敏感信息。会影响部署、配置、运行、数据或回滚的修改，还必须同步记录到 `docs/DEPLOYMENT_CHANGELOG.md`。

## 未发布

### 2026-09-21 — 更新：DeepSeek 官方 Flash 接入与当前费用文本

- 服务器与本地云配置切换至 deepseek / https://api.deepseek.com / deepseek-flash，保持思考开启、默认强度与 600 秒超时；独立服务器 JSON 请求验证成功。录音自然结束且无分析锁后重载网关，原分析许可状态校验保留。凭据仅保存在受保护配置和备份，不进入 Git。
- 按 2026-09-21 官方价格修正 Flash 估算：空闲缓存命中/未命中/输出为 0.02/1/4 元每百万 Token，高峰为 0.04/2/8 元。修正北京时间工作日峰段及周末/2026 法定节假日判断，报告标注价格快照与日历范围。
- 更新报告与飞书费用说明，去掉旧的“2026-08-17 待生效”文本；只对 Flash 及官方兼容旧别名使用 Flash 费率，Pro 或其他返回模型不套价。当前部署只选 deepseek-flash，无模型自动升级到 Pro。
- 报告格式升至 3.10.0；无新增配置或原始数据迁移，不修改正在运行的生产程序。价格逻辑需后续正式包部署才生效，历史报告重新渲染采用当前参考快照，不还原历史账单。
- 验证：官方费率、峰段边界、周末、节假日、时区转换和不对 Pro 套价专项检查通过；全量 263 passed、1 skipped，release-audit 无新增命中。

## 0.11.14 — 2026-09-12

- 正式包提交 `934f916eeda8`；在线部署入口同步固定版本及 SHA-256，构建复测 251 passed、1 skipped。

Release：[v0.11.14](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.14)。包含以下此前未发布修复；服务器仍需用户升级验收。

### 2026-09-12 — 修复：飞书摘要范围、PDF 浏览器与思考默认强度

- 飞书正文仅发送整体性总结（保留必要的分析缺失声明），不混入小时明细或按时间进展；旧文本/公开报告回退也提取总体章节。完整附件仍保留细节，报告格式升至 3.9.0。
- PDF 渲染器不再无条件选取不存在的第一个 Chrome 路径，改为检查实际文件后选择 Chrome/Edge；显式 MD_TO_PDF_CHROME_PATH 无效时明确报错。复现旧库在浏览器启动失败后遗留临时 HTTP 服务、最终被外层记为 180 秒超时；新渲染流程直接加载 HTML/CSS，不启临时 HTTP 服务，不等待外部资源，并在 finally 关闭浏览器。保留渲染阶段和 stderr 摘要，首次分析及缓存重试的 PDF 失败均通知分析状态窗口。
- THINKING_MODE=enabled 对短摘要、长摘要和最终综合均生效，分析请求省略 reasoning_effort，使用服务端默认强度；云部署配置已单独备份并开启思考，密钥和其他配置保持不变。生成策略纳入缓存标识，避免复用旧阶段策略结果。
- 无原始数据迁移。新浏览器路径配置可选；默认自动查找。更换思考策略可能重算摘要并增加耗时/用量，需重启应用加载配置。本次统一纳入 v0.11.14 发布包。
- 验证：全量 251 passed、1 skipped，发布审计无新增命中；本机真实渲染生成有效中文 PDF；浏览器选择、正文范围、PDF 失败进度和各阶段开启思考/默认强度回归检查通过。服务器错误已确认是渲染 180 秒超时；本机复现底层异常被进程挂起掩盖的路径，修复后真实 PDF 生成通过。服务器尚未升级，不能宣称其 PDF 已恢复。

### 2026-09-12 — 修复：统一供应商配置生效与缓存边界

- 所有分析接入模式统一读取 OOPZ_ANALYSIS_MAX_PARALLELISM（1–8），移除非 OpenCode Go 强制串行限制；短/长窗口的实际线程池均使用该值。
- 短/长摘要使用配置的普通初始 Token 预算，最终综合按实际思考模式选择普通或思考预算，不再由流水线固定值覆盖。思考、JSON、Token 设置加入生成缓存标识；密钥、超时、重试、请求间隔与并行度不改变成功内容缓存。
- 增加可选 ANALYZER_THINKING_FORMAT=auto/standard/deepseek/qwen，切换代理 URL/模型时可显式选择厂商协议；DeepSeek 关闭思考显式下发关闭标记。OpenCode 官方域名请求补充稳定的分析会话头。
- 兼容范围仍为本项目支持的 Chat Completions API，供应商模型、参数能力及限额不由本地配置保证；修改 .env 后须重启网关。JSON_MODE 控制请求的 JSON 格式参数，不取消应用的 JSON 响应校验。
- 新字段可省略，默认 auto；无原始数据迁移。因生成预算/缓存字段修正，旧版摘要缓存首次升级可能重新生成；后续仅调整连接/调度设置不会使成功摘要失效。回滚旧版恢复原有串行及固定预算行为。
- 验证：三种 provider、替换 URL/模型/key、Token/JSON/重试参数传递、实际 4 路并发、无效并行值、缓存变化边界与会话头回归检查通过；全量 245 passed、1 skipped，审计新增测试凭据命中经逐项复核确认仅为随机 UUID 假凭据并登记误报；本次统一纳入 v0.11.14 发布包。

### 2026-09-12 — 部署：已有服务器一键更新到最新正式版

- 部署指南新增第 12.1 节，完整嵌入 update_latest_release.ps1：查询 GitHub Latest 正式版，校验标签提交、GitHub 资产摘要、旁置 SHA-256 和清单，调用新包安装器并核对 current。
- 已是最新版不重装、不重启；拒绝降级、活动任务/分析锁、遗留目标版本目录及不完整的 Release 元数据。保留 shared，提供仅下载准备模式，不自动合并配置契约变化。
- 验证：脚本文档一致性和模拟流程检查通过；本机隔离目录实际匿名下载、校验 v0.11.13 并完成 PrepareOnly，未在服务器执行升级；全量 220 passed、1 skipped，发布审计无新增命中。
- 不新增环境变量或迁移业务数据；更新脚本纳入 v0.11.14，旧 v0.11.13 ZIP 不变。回滚本脚本不改变已安装版本或共享数据。

## 0.11.13 — 2026-09-12

- 正式包提交 `9bf093db9a6e`；在线部署指南及准备脚本同步固定版本与 SHA-256，构建复测 218 passed、1 skipped。

Release：[v0.11.13](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.13)。

### 2026-09-12 — 分析：内容审核拦截时拆分一次并标明缺失

- API 客户端识别 data_inspection_failed（含 OpenCode Go 的上游错误包装），只保留安全错误码，不输出错误响应中的原文。
- 单个短窗口首次被拦截时按时间中点拆分一次，正常 300 秒窗口为两个 150 秒片段。半段仍被同类错误拦截则跳过；两个均失败则均跳过。无文本半段不调用 API，普通 400、认证失败和超时仍按原失败逻辑处理，不递归拆分。
- 保存各半段时间范围/状态及缺失列表，内部、公开和简版报告直接渲染缺失说明。保留原转写、成功窗口缓存，完整摘要由成功半段按顺序合并，无额外合并请求；边界跨越的句子在两半保留完整文字。
- 使用量按原请求及各半段计数；被拒绝请求的 Token/费用未返回时，不声称费用估算完整。报告格式升至 3.8.0，不改变成功短摘要缓存指纹。
- 无新增配置或数据迁移；升级后重试原会话，跳过状态会缓存。回滚旧版仍可读取已生成报告，但旧程序不具备拆分处理能力。服务器未自动升级。
- 验证：覆盖两半成功、单边/双边跳过、空半段、其他错误失败、报告说明与缓存复用的回归测试通过；全量 218 passed、1 skipped，发布审计无新增命中。

## 0.11.12 — 2026-09-12

Release：[v0.11.12](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.12)。

### 2026-09-12 — 修复：静音分片阻断分析初始化

- continuous.no_text_marker 原先写入非 UUID 的 no-speech，分析器严格 UUID 校验导致包含静音分片的会话无法初始化。新记录改用稳定 UUID；analyzer_job 仅对匹配旧版静音标记特征的记录进行内存归一化，保留其他无效 ID、重复记录和时间范围校验。
- 旧转写文件、文本、时间戳及指纹输入不改写，无需重录或数据迁移；升级后从“待分析”重试原会话。若服务器错误行并非旧静音标记，仍需根据实际字段进一步诊断。
- 无配置变化。回滚旧版本会恢复其对旧静音记录的拒绝行为；共享原始数据不受影响。
- 验证：静音分片生成、合并、旧数据稳定读取与原文件保留、异常 ID 和重复记录拒绝专项测试通过；发布构建 209 passed、1 skipped，审计通过；在线下载入口固定为 v0.11.12、提交 `85ec1c9d4643` 及 SHA-256。

## 0.11.11 — 2026-09-12

- 发布包提交 `3f077e0ddc4d`；在线部署指南与准备脚本固定到新版 ZIP 和 SHA-256，构建复测 202 passed、1 skipped。无服务器自动部署。

Release：[v0.11.11](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.11)。包含安装器编码修复与最低 4 vCPU / 8 GiB 部署要求；无配置或数据迁移。

### 2026-09-12 — 修复：PowerShell 5.1 安装健康检查误判

- 原 v0.11.10 安装器无 BOM UTF-8 中文健康标记在 Windows PowerShell 5.1/代码页 936 下被误读，成功日志不能匹配，触发停机及首次安装 current 撤回；重复安装随后被目录存在保护拒绝。
- install_release.ps1 改用 ASCII 源码中的 Unicode 码点构造标记，保留仅检查本次新增日志的逻辑；新增原生 PowerShell 回归测试。部署指南第 9.5 节补充严格前提下恢复首次启动入口的步骤，并登记用户恢复正常的结果。
- 不新增配置、不迁移数据；现有 v0.11.10 ZIP 不变，安装器修复纳入 v0.11.11。已正常运行的实例无需重装；回滚安装器会恢复编码隐患，共享数据不受影响。
- 验证：原发布包误解码已复现，修复后真实 UTF-8 就绪日志匹配、空日志与失败日志拒绝检查通过；全量 202 passed、1 skipped，发布审计无新增命中。

### 2026-09-12 — 文档：最低部署要求统一为 4 vCPU / 8 GiB

- README、云部署指南、部署说明、运维说明和状态基线统一最低 CPU/内存要求，移除低配可部署或试运行建议；保留历史测量和版本记录，不再作为低配部署依据。
- 验证：全文检索与差异检查确认现行部署建议一致；仅文档修改，未变更代码、环境变量或已发布 ZIP，无数据迁移或运行时回滚影响。

## 0.11.10 — 2026-09-11

Release：[v0.11.10](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.10)。本版本包含以下此前未发布修复；不代表新服务器已验收。

### 2026-09-11 — 部署：新版下载入口

- 在线指南与 prepare_release.ps1 固定到 v0.11.10、提交 `351fee9b773b` 及构建后的 SHA-256，保留 v0.11.9 专用恢复段落及历史记录；构建快照中的旧下载入口不用于新安装。
- 发布包由干净提交生成，构建复测 201 passed、1 skipped；安装仍需新服务器端到端验收，无数据迁移。

### 2026-09-11 — 修复：云部署与百炼分析可用性

- 客户端为百炼官方兼容端点的 qwen3.8-flash 显式映射 enable_thinking，disabled/auto 不再意外启用思考，保留其他提供商行为；补充参数回归覆盖。
- 正式安装器增加 pip 官方源、隔离用户配置和重试，npm/pnpm 有界网络重试与环境恢复，补充依赖一致性和报告模块检查；包含此前 Chromium 安装及启动门槛。
- 部署文档补充 401 与超时诊断、账户使用范围、180 秒配置建议和旧版恢复边界；更新新服务器 4 vCPU/8 GiB 状态，并区分云端待验收项。云配置单独备份后只调整超时，不进入 Git。
- 验证：客户端专项 31 项通过；修复后合成材料 API 请求 HTTP 200、有效 JSON、无思考内容，耗时 52.8 秒，说明仍有等待波动。全量 201 passed、1 skipped，PowerShell 脚本语法检查通过；发布审计无新增命中。
- 部署影响：需要新发布包；无新增必填变量或数据迁移。共享配置不自动覆盖，超时需显式调整。回滚旧程序会恢复 Qwen 参数缺失行为，已下载浏览器及共享数据保留。

### 2026-09-11 — 部署：补齐录音 Chromium 安装与就绪检查

- 正式安装脚本在 Python 依赖安装后安装匹配的 Playwright Chromium 并实际启动验证，失败不切换 current；续装脚本同样在首次启动就绪标记前检查浏览器。
- README、架构和部署/运维说明区分录音 Chromium 与 PDF 的系统 Edge/Chrome；补充同账户、同虚拟环境的安装命令，以及旧 v0.11.9 包的手动补装要求。
- 无应用配置或数据迁移，不修改已有发布包；Playwright 升级、更换运行账户或清理缓存后须重新验证。
- 验证：191 passed、1 skipped，17 段 PowerShell 语法检查与文档/脚本一致性检查通过；本机 Chromium 通道启动验证通过，发布审计无新增命中。服务器补装结果仍待用户验证。

### 2026-09-11 — 部署：从 Node 依赖失败处继续安装

- 新增 `resume_node_install.ps1`，针对 v0.11.9 首装中 Python / 模型已完成而 npm ECONNRESET 的场景，只重试锁定的 Node 依赖，不重建 Python 环境、不移动版本目录、不重复下载模型。
- 临时使用官方 npm registry 与有界重试，保留 TLS 校验、冻结锁文件和禁止安装脚本；结束恢复原 npm 环境。检查 pip 依赖一致性、关键导入、模型哈希与报告模块后才生成首次启动准备标记。
- 部署说明新增阶段区分和首次启动命令，现有 current 或已修改的版本文件会被拒绝；准备完成不等同于启动验收通过。
- 验证：隔离目录实际安装锁定 Node 依赖并成功导入报告模块，package.json 和锁文件保持不变；191 passed、1 skipped，16 段 PowerShell 语法检查通过。模拟分支验证失败保留、环境恢复、就绪标记及运行中保护。服务器实际续装尚待执行。
- 范围：恢复脚本、对应测试、部署说明与状态；无应用数据/配置契约变化，现有发布包保持不变。

### 2026-09-10 — 部署：首次依赖安装失败的诊断与恢复

- 新增 `scripts/retry_dependency_install.ps1` 及文档可复制代码：检查 Python 3.12 x64 和官方 NumPy wheel 解析，临时隔离 pip 配置并使用独立缓存，通过后备份失败版本目录，再执行校验包内的原安装脚本。
- 仅适用于 v0.11.9 的首次依赖安装失败；已有 `current` 或仍有进程引用目标目录时拒绝恢复，不终止进程，不递归删除共享配置、模型或数据。pip 环境变量在退出时恢复。
- 同步部署状态中的用户终端已确认信息；NumPy 候选失败原因保持待服务器诊断，不误判为 Python 不兼容或内存不足。
- 验证范围：本机官方 PyPI dry-run 成功解析兼容 wheel；恢复及文档同步专项测试 5 项通过，全量 189 passed、1 skipped。模拟测试验证诊断失败、运行中保护、备份与共享链接保留、退出恢复环境。浏览器集成测试输出原生异常诊断但返回通过，已保留日志且未改动其代码；未在服务器实际重试。
- 部署/配置影响：不修改运行逻辑、依赖范围、发布包或 `.env` 契约；新增独立恢复入口，迁移与回滚见部署变更记录。

### 2026-09-10 — 部署：移除服务器 Git 工具依赖

- 从基础环境脚本及从零部署说明中移除 Git for Windows、GitHub CLI 的检测、下载、安装与版本检查，保留匿名 Release 下载路径；已安装的软件不卸载。
- 同步 README、部署基线和分支测试，确保全新服务器仅安装应用运行依赖。开发端版本管理流程不变。
- 主要文件：`scripts/install_prerequisites.ps1`、`tests/prerequisites_checks.ps1` 与部署说明。无配置契约或数据迁移；187 passed、1 skipped，脚本与文档一致性、跳过/安装分支及发布审计通过。

### 2026-09-10 — 部署：匿名下载与 PowerShell 全流程

- 实查仓库为 Public，匿名访问正式 Release 成功；本次未改变仓库可见性。新增 `prepare_release.ps1`，下载固定 ZIP 与 SHA-256，校验固定摘要/提交及提取文件，重复执行跳过下载；不调用 GitHub 登录或 Git 克隆。
- 新增 `configure_server_env.ps1`，从已校验模板创建配置，只询问缺少字段，敏感值隐藏输入，保留既有值并原地写入。部署说明将配置、服务器一键飞书入口、安装和登录启动任务统一为 PowerShell 命令。
- 范围：部署/飞书/README 说明、两个准备脚本与对应测试；无应用运行逻辑或配置契约变化，旧 Release 保持不变，新增脚本可从在线文档复制。
- 验证：187 passed、1 skipped；13 段部署 PowerShell 语法检查通过，文档/脚本一致性、下载复用、文件损坏拒绝、配置保留和硬链接测试通过。正式 ZIP 已实际匿名下载、校验和提取，第二次执行跳过下载；来宾系统实际安装尚未执行。
- 部署影响：移除服务器 GitHub 登录及克隆前置；账户授权、页面文件和真实负载仍需部署验收，迁移/回滚见部署变更记录。

### 2026-09-10 — 部署：自动准备基础环境

- 新增可重复执行的 Windows PowerShell 引导脚本，缺少才下载和安装 Visual C++ x64 运行库、Git、gh、Python 3.12 x64、Node/npm/npx 及浏览器，支持仅检查、安装器签名校验与失败中止；补齐共享 Node 运行时准备。
- 从零部署指南包含与脚本一致的完整可复制代码、官方下载来源及安装路径；同步 README、部署说明和状态基线。既有组件不升级或卸载，现有 Release 不变。
- 主要文件：`scripts/install_prerequisites.ps1`、`README_CLOUD_SERVER_DEPLOYMENT.md`、相关部署文档、`tests/test_prerequisites.py` 与 PowerShell 分支测试。
- 验证：184 passed、1 skipped；Windows PowerShell 检查模式、模拟分支及文档代码一致性测试通过，6 个官方下载地址经 HTTP 检查可用。覆盖重复执行、缺少 npm/npx、安装失败及不可信安装器阻断；未在服务器执行实际安装。
- 部署影响：新增可选的基础软件准备入口；不变更应用配置/数据契约，迁移与回滚边界见部署变更记录。

## 0.11.9 — 2026-09-10

Release：[OOPZ Capture v0.11.9](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.9)

### 2026-09-10 — 文档：项目首页与试运行说明同步

- 修改：重整 README 的能力、模块目录、处理流程与安装依赖；飞书配置以一键创建/更新为主，手动配置收为折叠保底；说明和配置模板仅推荐 MiMo V2.5，不推荐供应商。同步部署/运维文档的低负载试运行边界和未部署的试用实例状态。
- 主要文件：根目录 README/飞书及云部署手册、`.env.example` 注释、`PROJECT_PROGRESS.md` 与 `docs/` 架构/部署/运维说明。
- 验证：模块路径与命令已对照源码；完整测试 182 passed、1 skipped。现有发布包 SHA-256 与 GitHub 附件摘要一致，120 个文件与发布提交一致（仅 CRLF 换行转换）。
- 发布补充：统一包元数据与模块版本为 0.11.9；经逐项复核、用户确认后补充 11 条既有测试/变量引用的误报指纹，未加入敏感值。重新构建发布包，使离线说明与 GitHub 首页保持一致。
- 部署/配置影响：无处理逻辑、依赖范围、环境变量或数据格式变化；无需数据迁移，按标准安装/回滚流程使用版本包。

## 0.11.8 — 2026-09-05

Release：[OOPZ Capture v0.11.8 (0ebf17f)](https://github.com/XK205E3n/OOPZ_Capture/releases/tag/v0.11.8-0ebf17f9f464)

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
  - 合并 `analysis_pipeline` 中函数体逐行同构、仅差输出键后缀的两条阶段费用统计路径，改为同一函数的 `suffix` 参数。
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
- 修改：移除分析供应商、API 地址、模型及运行参数的环境默认值；生产网关启动时校验全部 11 个 `ANALYZER_*` 项，设置状态对缺失项统一显示“未设置”。推荐说明于 2026-09-10 同步为仅推荐 MiMo V2.5，不推荐供应商；程序不自动选择，300 秒窗口的默认 4 路并行设置不变。
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
