# 当前架构与状态机

本文是当前实现的规范说明。里程碑文档只记录开发历史；发生冲突时，以本文、根目录 README 和代码测试为准。

## 组件边界

| 组件 | 职责 | 不应执行的操作 |
|---|---|---|
| NapCatQQ | 普通 QQ 登录与 OneBot 11 协议 | 不直接操作 OOPZ 或分析 API |
| OneBot 网关 | 鉴权、QQ 收发、持久化重试、好友申请 | 不直接录音或分析 |
| QQ 控制器 | 管理员指令、Session 状态机、启动录音和分析 | 不持有 QQ 登录会话 |
| 连续录音器 | OOPZ 加入、300 秒轮转、断线重连、成员检测 | 不发送 QQ 消息 |
| 转写器 | VAD、SenseVoiceSmall、UTF-8 转写 | 不生成最终报告 |
| 分析器 | 300 秒总结、60 分钟摘要、最终总结、报告 | 不控制录音 |
| QQ 自愈监视器 | 只恢复 NapCat/OneBot | 不终止控制器、录音、转写或分析 |

## Session 状态

典型状态顺序：

```text
starting → connecting → recording/reconnecting → stopping
→ ready_for_analysis | ready_for_analysis_with_errors
→ waiting_analysis_decision → analyzing
→ analysis_completed_report_queued | analysis_failed
```

控制器以 Session 的 `lifecycle.json` 为录音/转写事实来源，以分析 variant 的 `lifecycle.json` 为分析事实来源。`controller.json` 是便于 QQ 查询的索引，不应覆盖 Session 生命周期。

结束责任人规则：

- `/oopz 离开`：分析确认和首次报告发送给下达有效离开指令的管理员；
- 自动退出：发送给最初发起 `/oopz 开始` 的管理员；
- 每位管理员的报告/待分析多步流程独立保存；
- 同一 Session 在一个控制器进程内只允许一个分析任务。

## 数据与删除

- 成功转写分片：删除音频；
- 空频道无语音：写入“无文本”标记后视为成功；
- 失败分片：默认自动修复一轮，仍失败则保留音频并通知管理员；
- 300 秒分析：生产路线默认四窗口合并请求；批量结果结构异常时自动拆分为单窗口重试；
- Session：按 `delete_after` 清理，默认 168 小时；
- PDF：由 `report_archive.json` 记录归属，随 Session 一起清理；
- `.env`、`controller_state`、`output`、`models`、NapCat、日志和临时文件均不进入 Git。

## 分析兼容契约

生产接口要求 OpenAI Chat Completions 兼容的 HTTPS endpoint。供应商、Base URL、模型、JSON mode 和 thinking mode 都是显式配置。`configured-api` 是 CLI 与 QQ 的统一默认入口；`mimo-go` 只是固定模型的兼容入口。

费用只有在程序内存在已核验价格表时估算；未知供应商或未知模型必须显示“未估算”，不能套用其他模型价格。

## 不可消除的外部风险

- NapCat 和普通 QQ 账号可能因协议变化或风控掉线；程序只能恢复服务或提示重新验证；
- OOPZ/Agora 页面结构或 SDK 行为变化可能导致连接失败；录音器会在限定窗口重连，但不能保证外部接口长期不变；
- ASR 与大模型输出存在事实错误风险，关键决定仍需人工复核；
- 公有 API 的限流、模型下线和价格变化需要定期核验。
