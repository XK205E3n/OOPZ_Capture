# Ubuntu 24.04 LTS 适配方案与问题清单

更新日期：2026-10-01。目标：Ubuntu Server 24.04 LTS、x86_64、无桌面环境。

## 仓库与交接状态

- 仓库：<https://github.com/XK205E3n/OOPZ_Capture>。
- 本方案分支：`codex/ubuntu-adaptation-handoff`；执行提示词见 [UBUNTU_ADAPTATION_TASK.md](UBUNTU_ADAPTATION_TASK.md)。
- 本文档分支从远端 Windows 基线 `037b988990fccd21d5c3ed544f70720d9187b4f1` 创建，只推送方案及文档记录，不包含新的运行程序。
- 核对时远端 main 仍为上述基线。Ubuntu 开发提交 `a307e93`、`f637516`、`b712bd7`、`02dc4f6` 及后续修复仅在原开发电脑上，**没有随本次文档推送上传**。不能假设读者已能从 Git 获取这些草稿。
- 原开发电脑的最新草稿已跑过348项成功测试、1项因 Windows 符号链接权限跳过；这是未推送草稿的测试结果，**不是本文档分支的运行验收**。
- Ubuntu 实际安装、systemd、录音、语音转写、PDF、飞书和持续负载均未验收。当前仍不属于已验证的生产目标。

接手者从本分支开展适配；如负责人后续另行提供草稿提交或补丁，先核对差异后复用，不覆盖现有工作。本方案独立包含需求和失败场景，不依赖本机被忽略的日志文件。

## 范围与配置

完整流程：OOPZ 浏览器录音 → 本地CPU语音转写 → 外部API文本分析 → Markdown/PDF报告 → 飞书投递。

初始最低配置保持4 vCPU / 8 GiB，建议80 GiB SSD。不得仅因 Ubuntu 无桌面就降低最低内存；实测后再决定支持范围。保留 Windows Server 运行路径和现有分析配置契约，不更换模型或修改分析提示词。

建议目录：`/opt/oopz/{releases,current,shared,artifacts}`。`shared`保存私有配置、模型、会话、飞书状态和日志；每个发布目录使用独立依赖环境，`current`只指向正式发布版本。

## 需求表

“实现位置”中尚不存在的Linux文件是建议新增位置，不代表当前Git已提供。

| 编号 | 任务及主要位置 | 必须满足的要求 | 验收 |
| --- | --- | --- | --- |
| L01 | Python/ASR：`pyproject.toml`、转写模块 | Python3.12，重新建立Linux venv；核对FunASR、VAD、ONNXRuntime、PyTorch/torchaudio的CPU兼容版本，保存实际依赖清单；不复制Windows环境 | pip check、模型加载、VAD、真实音频转写成功 |
| L02 | 浏览器录音：先核查`oopz-sdk`后端 | 安装匹配的Playwright Chromium和系统库，用专用普通用户无桌面运行 | 入群、收音、分片、持续录音及断线重连通过 |
| L03 | Node：`src/oopz_capture/pdf_reports.py`、Linux前置脚本 | 解除node.exe固定路径；Node版本满足锁文件全部依赖，现有Puppeteer链要求≥22.12.0；npx也使用同一Node | 旧Node被识别，锁定依赖安装和实际模块导入成功 |
| L04 | PDF：`tools/md_to_pdf.mjs`、字体安装 | 配置或找到真正安装的浏览器，兼容Playwright新旧缓存布局；中文字体；普通用户运行 | 长中文报告、跨页布局、错误状态提示及无残留进程 |
| L05 | 配置：`env_loader.py`、CLI和安装器 | 持久化.env；明确加载优先级，群内写回不丢失；准备阶段与setup即使用共享配置，不能激活时覆盖setup生成的凭据 | 特殊字符/UTF-8、写回及升级保留；不输出密钥 |
| L06 | 目录/路径：Linux安装与核心路径代码 | shared与版本分离；跨平台绝对/相对路径、大小写、中文和空格；切换current不覆盖共享数据 | 新版和回滚版访问同一共享数据 |
| L07 | 链接检查：`analyzer_job.py`、`workflow.py` | 合法共享目录链接可用，保留会话内部越界/非法链接校验 | 合法路径成功，越界和非法链接拒绝 |
| L08 | systemd：建议`scripts/linux/oopz-capture.service` | 专用普通用户、固定HOME/WorkingDirectory；状态/启停/重启、自启及单实例；停止清理cgroup子进程 | 开机恢复、异常恢复、停止无残留；不得结束其他脚本进程 |
| L09 | 中断恢复：网关、控制器、`process_utils.py` | 核查SIGTERM、录音/转写子进程、分析线程、PID锁及生命周期；保留已完成数据 | 分别在录音、转写、等待API时停止并重启，无卡死或重复投递 |
| L10 | 首次安装：建议`scripts/linux/install_release.sh` | 依赖准备→配置/setup→激活三阶段；失败可同包重试，拒绝清理当前版本；不依赖尚不存在的current | 空白环境走通；下载失败后可恢复；准备失败不碰旧服务 |
| L11 | 正式包：`scripts/build_release.ps1` | 同一已提交HEAD包含两平台必需文件；SHA-256及清单；脚本LF及执行方式；helper完整 | 固定包校验、解压布局正确；敏感/运行文件不入包 |
| L12 | 更新/回滚：建议`scripts/linux/` | 先校验再执行新包代码；安装器与helper同包；保存实际文件和服务状态，覆盖所有切换失败 | 成功升级、失败恢复、回滚各实测；活任务拒绝普通切换 |
| L13 | 飞书/API：保留现有核心契约 | 支持供应商URL/key、并行、超时、思考及缓存配置；当前部署目标为DeepSeek Flash、思考开启、默认强度 | 请求实际采用配置；飞书只发总体摘要；审核缺失/PDF错误明确 |
| L14 | 网络/日志 | 验证OOPZ、飞书、API与依赖/模型下载出站；无新增业务入站端口；持久日志与轮转 | 重连、错误定位、日志容量受控，凭据不进日志 |
| L15 | Windows数据迁移 | 清点路径型配置、报告/outbox绝对路径和机器PID；停机备份，保留业务状态；不直接清空state | 老会话可读取和分析、附件可获取、不重复发送，备份可恢复 |
| L16 | 实际负载 | 连续数小时日常会话，观察整机CPU/内存/swap/磁盘、分片耗时及队列 | 无丢块和持续积压；以关闭至转写完成≤240秒为初始目标，报告实际范围 |
| L17 | Windows回归/文档 | 保留Windows支持；独立Ubuntu指南及相关变更记录/配置示例 | 回归通过、指南可从零执行；未实测项目明确标注 |

## 已知问题及新实现必须覆盖的失败场景

以下来自未推送草稿的审查，用作新实现的验收约束；不是在断言main已有相应Linux脚本。后续本地修复尚未交付给Git读者，接手者必须自行在提交中实现并验证。

| 问题 | 草稿中的表现 | 要求 |
| --- | --- | --- |
| U1：安装器辅助文件丢失 | 更新器只提取安装器单文件；真实新版安装器需要三个Python helper和事务helper，校验后仍启动失败 | 校验后完整提取管理脚本，验证helper完整性；联动测试执行真实安装器，不能只跑打印标记的假安装器 |
| U2：恢复不完整 | ln/mv、部分单元/logrotate写入失败会绕过回退；旧服务停止；按旧模板恢复会丢实际修改，自启状态也会改变 | 停服前保存实际文件、current、active/enabled状态；任何停服后失败都进入恢复；恢复失败明确报告，不启动不确定版本 |
| U3：未知锁误判安全 | 损坏JSON/非法PID的分析锁被视为陈旧，允许切换 | 仅有效死锁判陈旧；不可判读、非法PID、符号链接及非法文件拒绝；显式force才能覆盖 |
| Node布局/PATH | tarball的node/npx位于bin；链接却指向运行时根；npx通过env node可能使用旧系统Node | 统一实际路径和PATH，检测版本后用同一运行时执行安装及PDF |
| 首次setup配置丢失 | prepare后没有共享.env链接，setup写入版本内文件，activate替换链接后凭据丢失 | 激活前的配置入口读写共享文件，验证写回后激活仍生效 |
| PDF浏览器查找不匹配 | 只查发行版浏览器或旧chrome-linux路径，实际安装的是Playwright chrome-linux64 | 核对实际SDK/浏览器版本和可执行路径，服务环境真实渲染；避免只用虚构目录验证 |
| 测试垫片失效 | Windows Git Bash启动器重排PATH，调用真实chown/ln/mv而非替代命令 | 测试证明外部副作用已隔离；Git Bash不是Ubuntu/systemd验收 |

## 实施顺序与交付

1. 从本分支核对源码、依赖与接口，先列实际差异；不要假设未推送文件存在。
2. 实现核心跨平台改造、Linux管理脚本及上述失败保护；补行为测试，保留Windows回归。
3. 在指定的干净Ubuntu24.04测试机执行从零安装、服务用户录音/PDF、配置、更新及回滚。
4. 在已授权测试群/API完成真实链路、停止恢复和持续负载。缺凭据/测试机时明确留下未验证项，不编造结果。
5. 更新CHANGELOG、部署状态/变更记录/Ubuntu指南及必要.env.example；提交/发布前release-audit。

交付：代码与文件清单；可复制安装/启停/升级/回滚命令；测试环境版本、命令、结果、资源记录；迁移与恢复步骤；Windows回归；未验证范围及已知问题。生产部署、历史数据清理和发送生产飞书消息须另行授权。

## 参考文件

先阅读仓库`AGENTS.md`、`docs/DEPLOYMENT_STATE.md`、`docs/DEPLOYMENT.md`、`docs/OPERATIONS.md`、`.env.example`、`pyproject.toml`、`package.json`及`pnpm-lock.yaml`。

现有模块重点：`src/oopz_capture/pdf_reports.py`、`env_loader.py`、`process_utils.py`、`analyzer_job.py`、`workflow.py`、`feishu_cli.py`、`controller.py`及`tools/md_to_pdf.mjs`。Windows部署脚本提供语义参考，不能直接视为Linux可执行。
