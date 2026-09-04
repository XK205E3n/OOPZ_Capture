# 部署状态基线

> 这是本地代码与生产服务器差异的唯一事实来源。任何部署相关修改和每次生产发布都必须同步更新本文件。禁止记录密钥、密码、完整服务器地址或个人信息。

更新时间：2026-09-04

## 当前状态

| 项目 | 本地开发环境 | 生产服务器 | 是否需同步 |
| --- | --- | --- | --- |
| Git 提交 | `main` 跟踪私有 GitHub 仓库；精确提交以 `git rev-parse HEAD` 和发布清单为准 | 尚未部署 | 是：发布并部署首个 GitHub Release 后登记发布 ID |
| 应用版本 | `0.11.7` | 待部署（Release `v0.11.7-7791e58f0359` 已上传 GitHub，含 `.env` 硬链接修复与部署文档对齐） | 是 |
| 操作系统 | Windows | 目标为 Windows Server 2022/2025 x64 Desktop Experience | 待实施 |
| Python | 3.12.13 | 未安装/未确认 | 是：建议 3.12 x64 |
| Node.js | 本地已安装，版本待确认 | 未安装/未确认 | 是：建议当前 LTS x64 |
| 应用依赖 | `pip install -e ".[speech,feishu]"`、`npx pnpm@10.15.0 install --frozen-lockfile` | 未安装 | 是 |
| ASR 模型 | 本地 `models/SenseVoiceSmall`（不进 Git） | 计划由服务器从魔搭 `iic/SenseVoiceSmall` 自动下载固定修订版并校验 | 首次安装待实施 |
| 生产配置 | 本地 `.env`（不进 Git）；全部 `ANALYZER_*` 项显式配置 | 尚未创建 | 是：服务器独立配置全部分析器变量，不复制本地密钥文件作为长期同步方式 |
| 输出/状态/日志 | `output`、`feishu_state`、`logs`；分析检查点与中断恢复状态保存在会话目录 | 尚未创建 | 否：属于各环境持久数据，禁止互相覆盖；重启后仅回收已退出进程留下的分析锁 |
| 启动方式 | 交互式批处理 | 目标为任务计划程序调用稳定 `current` 路径 | 待实施 |
| 代码远端 | `origin=https://github.com/XK205E3n/OOPZ_Capture.git`（Private） | 计划通过只读认证下载指定 Release | 首次服务器配置待实施 |

## 生产目录约定

默认安装根目录为 `C:\OOPZ`：

```text
C:\OOPZ\
  current -> releases\<release-id>       # 只读使用的当前版本目录联接
  releases\<release-id>\                 # 每次发布独立目录，含独立 .venv/node_modules
  shared\config\.env                     # 生产配置，发布间共享
  shared\models\SenseVoiceSmall\         # 大模型，发布间共享
  shared\output\                          # 会话与报告
  shared\feishu_state\                    # 网关状态与审计
  shared\logs\                            # 运行日志
  artifacts\                              # 已上传发布包与 SHA-256 文件
```

发布目录中的 `.env` 使用同盘硬链接指向 `shared\config\.env`；`models`、`output`、`feishu_state`、`logs` 使用目录联接指向 `shared`。因此代码回滚不会回滚或清空业务数据和配置。程序对 `.env` 的写入（首次入群控制群绑定、一键配置、群内“设置”命令）必须保持原地写：改回“临时文件替换”式写入会切断硬链接，使这些修改在下次升级时丢失（见部署变更记录）。

## 服务器专属差异（允许存在）

- `.env` 中的密钥、账号、控制群 ID、绝对数据路径及性能参数；实际值不得进入本文档。
- Windows 任务计划程序、云防火墙/RDP 白名单、页面文件、磁盘告警和系统补丁策略。
- 模型、会话数据、飞书状态和日志。

除以上项目外，生产代码、Schema、模板、脚本及 Python/Node 依赖声明必须来自同一发布包，不允许服务器手改。

## 首次部署待办

- [x] 将已审查、测试的当前版本生成并上传为首个 GitHub Release（Release ID 以发布清单为准）。
- [x] 建立私有 Git 远端并推送 `main`。
- [ ] 准备 Windows Server，安装 Python 3.12 x64、Node.js LTS、Git、Chrome/Edge。
- [ ] 建立 `C:\OOPZ\shared`，安全创建生产 `.env`，由安装脚本从魔搭社区下载并校验 SenseVoiceSmall。
- [ ] 本地生成首个发布包及 SHA-256，传到 `C:\OOPZ\artifacts`。
- [ ] 执行服务器安装脚本，验证飞书长连接、录音、转写、分析和发布。
- [ ] 创建开机/登录启动任务，并执行一次服务器重启演练。
- [ ] 执行一次版本更新和回滚演练。

## 最近一次生产发布

尚未部署。首次成功后填写：发布 ID、Git 提交、应用版本、部署时间（含时区）、操作者、验证结果和回滚点。
