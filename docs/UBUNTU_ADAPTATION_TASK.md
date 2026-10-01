# 可复制给接手者或AI的任务提示词

请适配 OOPZ Capture 至 Ubuntu Server 24.04 LTS（x86_64、无桌面），保留Windows兼容，并完成真实测试环境验收。

仓库：<https://github.com/XK205E3n/OOPZ_Capture>。

交接分支：`codex/ubuntu-adaptation-handoff`。

先读取 `docs/UBUNTU_ADAPTATION_PLAN.md`：其中包含完整L01–L17需求、已知问题、验收及交付标准。此分支只有方案文档，运行代码仍是Windows基线；本机另有未推送Ubuntu草稿，不能假设它已在Git。

```bash
git clone --branch codex/ubuntu-adaptation-handoff https://github.com/XK205E3n/OOPZ_Capture.git
cd OOPZ_Capture
git switch -c codex/ubuntu-adaptation-implementation
```

如已克隆，先检查工作区、获取该交接分支，使用独立分支或工作区继续，不覆盖已有修改。

执行要求：

1. 阅读AGENTS.md、DEPLOYMENT_STATE和DEPLOYMENT，核对Git实际代码及依赖。目标配置暂保持4核8GiB、建议80GiB SSD。
2. 完成跨平台Node/PDF浏览器查找、Python/CPU转写依赖、中文字体、共享配置/目录、无桌面录音、systemd启动与SIGTERM恢复。
3. 增加Linux首次安装、升级、回滚流程；正式包继续来自干净已提交HEAD的build_release.ps1。首次安装必须能准备→setup/配置→激活，并能恢复失败准备。
4. 必须覆盖U1–U3：校验后提取真实安装器的全部helper；停服后各类失败恢复实际文件/current及active/enabled状态；损坏或非法分析锁拒绝自动切换。补真实脚本联动和失败注入，不只断言源码字符串。
5. 保持供应商配置和分析语义，当前目标DeepSeek Flash、推理开启、默认强度。飞书正文只投递总体总结；不将此前未批准的提示词重写纳入此次任务。
6. 在指定Ubuntu测试机完成从零安装、录音/转写、中文PDF、飞书/API链路、更新/回滚、三种阶段停止恢复及数小时负载。区分mock结果与Ubuntu实测；缺测试机或凭据就列未验证项。
7. 同步根CHANGELOG、部署变更记录/状态、Ubuntu指南及必要配置示例；提交或发布前按release-audit和既有基线审计。回归不得损坏Windows路径。

操作边界：当前Windows生产机器人保持关闭；不要操作现有抢购脚本，不按Python安装目录批量杀进程。不得直接修改服务器current/releases程序；配置、凭据、服务器地址、私钥、模型和运行数据不进入Git/发布包。未经另行授权，不做生产部署、数据清理或生产飞书发信。

最终交付：可审查的代码改动、执行文档、测试命令和真实结果、资源/稳定性记录、迁移及回滚步骤、明确未验证范围。仅单元测试或浏览器启动成功不足以宣称Ubuntu生产可用。
